"""Explainable URL scoring; a score is a priority, not a probability."""
import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote
from leads.services.network import canonical_url

ASSETS = re.compile(r"\.(?:pdf|jpe?g|png|gif|svg|webp|zip|exe|mp[34]|css|js|ico|woff2?|docx?|xlsx?)$", re.I)
TRACKING = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid"}
TRAPS = {"sort", "order", "filter", "session", "sessionid", "sid", "calendar", "replytocom"}
SKIP_PATH = re.compile(r"/(?:login|logout|signin|signout|search|cart|checkout|calendar|wp-admin)(?:/|$)", re.I)
CONTACT_EXCLUSIONS = (
    "path:blog", "path:article", "path:emv-credit-card-machines", "path:product", "path:software",
    "domain:facebook.com", "domain:linkedin.com", "domain:x.com", "domain:twitter.com",
)
TARGETS = ("team", "staff", "people", "representative", "rep", "dealer", "partner", "agent",
           "executive", "sales", "contact", "about", "directory", "member")
PROFILE_TARGETS = ("profile", "bio", "biography", "leadership")
EDITORIAL = ("blog", "article", "news", "careers", "vacancies", "privacy", "terms")
PRODUCT = ("product", "device", "software", "emv credit card machines")


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


def words(text):
    return text.lower().replace("-", " ").replace("_", " ")


def phrase_matches(text, term):
    text, term = words(text), words(term)
    return contains(text, term) or (len(term) > 2 and not term.endswith("s") and contains(text, term + "s"))


def exclusion_matches(rule, url, label, context):
    parsed = urlsplit(url)
    host, path = parsed.hostname.lower().rstrip("."), unquote(parsed.path)
    if rule.startswith("path:"):
        return phrase_matches(path, rule[5:].strip())
    # Bare domain names are accepted too, so x.com matches a hostname, not page prose.
    if rule.startswith("domain:") or re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", rule):
        domain = rule.removeprefix("domain:").strip().lower().rstrip(".")
        return host == domain or host.endswith("." + domain)
    return phrase_matches(" ".join((host, path, label, context)), rule)


def rank(campaign, url, label="", context=""):
    direct = " ".join((unquote(urlsplit(url).path), label))
    text = words(" ".join((direct, context)))
    excluded = [term for term in lines(campaign.exclusions) if exclusion_matches(term, url, label, context)]
    if excluded:
        return -100, ["Excluded phrase: " + term for term in excluded[:3]]
    score, reasons = 0, []
    # Nearby sales/team prose must not turn every product link into a team page.
    if any(phrase_matches(direct, word) for word in TARGETS):
        score += 35
        reasons.append("Team, contact, partner or directory page (+35)")
    matched = [term for term in lines(campaign.keywords) if phrase_matches(text, term)]
    if matched:
        score += 25
        reasons.append("Industry: " + ", ".join(matched[:3]) + " (+25)")
    matched = [term for term in lines(campaign.sales_terms) if phrase_matches(text, term)]
    if matched:
        score += 20
        reasons.append("Sales role: " + ", ".join(matched[:3]) + " (+20)")
    if campaign.region and phrase_matches(text, campaign.region):
        score += 10
        reasons.append("Region mentioned (+10; not verified)")
    if any(phrase_matches(direct, word) for word in EDITORIAL):
        score -= 20
        reasons.append("Editorial, legal or recruitment page (-20)")
    if any(phrase_matches(direct, word) for word in PRODUCT):
        score -= 25
        reasons.append("Product, device or software page (-25)")
    return score, reasons or ["No strong relevance signal yet"]


def link_priority(url, label=""):
    """Campaign-free crawl order for an already in-scope link. Never an exclusion."""
    direct = " ".join((unquote(urlsplit(url).path), label))
    score = 35 if any(phrase_matches(direct, word) for word in TARGETS + PROFILE_TARGETS) else 0
    if any(phrase_matches(direct, word) for word in EDITORIAL):
        score -= 20
    if any(phrase_matches(direct, word) for word in PRODUCT):
        score -= 25
    return score
