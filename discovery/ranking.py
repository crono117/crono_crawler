"""Explainable URL scoring; a score is a priority, not a probability."""
import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote
from leads.services.network import canonical_url

ASSETS = re.compile(r"\.(?:pdf|jpe?g|png|gif|svg|webp|zip|exe|mp[34]|css|js|ico|woff2?|docx?|xlsx?)$", re.I)
TRACKING = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid"}
TRAPS = {"sort", "order", "filter", "session", "sessionid", "sid", "calendar", "replytocom"}
SKIP_PATH = re.compile(r"/(?:login|logout|signin|signout|search|cart|checkout|calendar|wp-admin)(?:/|$)", re.I)


def lines(text):
    return list(dict.fromkeys(line.strip().lower() for line in text.splitlines() if line.strip()))


def clean_url(raw):
    url = canonical_url(raw)
    p = urlsplit(url)
    host = p.hostname
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Local destinations are excluded.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("Private addresses are excluded.")
    path = unquote(p.path)
    if ".." in path.split("/") or "\\" in path or SKIP_PATH.search(path) or ASSETS.search(path):
        raise ValueError("Non-content URL or traversal path.")
    pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True, max_num_fields=40)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING]
    if len(pairs) > 8 or any(k.lower() in TRAPS for k, _ in pairs):
        raise ValueError("Unbounded filter or session URL.")
    url = urlunsplit((p.scheme, p.netloc, p.path, urlencode(sorted(pairs)), ""))
    if len(url) > 1500:
        raise ValueError("URL is too long.")
    return url


def contains(text, term):
    return bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.I))


def rank(campaign, url, label="", context=""):
    text = " ".join((unquote(urlsplit(url).path), label, context)).lower()
    excluded = [term for term in lines(campaign.exclusions) if contains(text, term)]
    if excluded:
        return -100, ["Excluded phrase: " + term for term in excluded[:3]]
    score, reasons = 0, []
    targets = ("team", "staff", "people", "representatives", "reps", "dealers", "partners", "contact", "about", "directory", "members")
    if any(contains(text.replace("-", " ").replace("_", " "), word) for word in targets):
        score += 35
        reasons.append("Team, contact, partner or directory page (+35)")
    matched = [term for term in lines(campaign.keywords) if contains(text, term)]
    if matched:
        score += 25
        reasons.append("Industry: " + ", ".join(matched[:3]) + " (+25)")
    matched = [term for term in lines(campaign.sales_terms) if contains(text, term)]
    if matched:
        score += 20
        reasons.append("Sales role: " + ", ".join(matched[:3]) + " (+20)")
    if campaign.region and contains(text, campaign.region):
        score += 10
        reasons.append("Region mentioned (+10; not verified)")
    if any(contains(text, word) for word in ("blog", "news", "careers", "vacancies", "privacy", "terms")):
        score -= 20
        reasons.append("Editorial, legal or recruitment page (-20)")
    return score, reasons or ["No strong relevance signal yet"]
