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
DIRECT_TARGETS = ("team", "staff", "people", "profile", "bio", "biography", "leadership")
PLURAL_ROLE_TARGETS = ("agents", "reps", "representatives")
SALES_ROLE_TARGETS = ("sales team", "sales representative", "sales rep", "sales agent")
PRIORITY_TARGETS = ("team", "staff", "people", "representative", "rep", "dealer", "partner", "agent",
                    "executive", "sales", "contact", "about", "directory", "member", "profile", "bio",
                    "biography", "leadership")
EDITORIAL = ("blog", "article", "news", "careers", "vacancies", "privacy", "terms")
PRODUCT = ("product", "device", "software", "emv credit card machines")
DIRECT_NOISE = ("collateral", "document", "document sharing", "social", "deck", "sheet", "playbook",
                "brochure", "schedule", "scheduling", "calendar", "booking", "appointment", "login",
                "sign in", "signin", "business agent", "business rep", "business representative")
SOCIAL_HOSTS = ("facebook.com", "linkedin.com", "x.com", "twitter.com", "instagram.com", "tiktok.com",
                "youtube.com", "reddit.com", "bsky.app")
DOCUMENT_HOSTS = ("docs.google.com", "drive.google.com", "dropbox.com", "box.com", "docsend.com",
                  "slideshare.net", "scribd.com", "notion.site", "notion.so")
SCHEDULING_HOSTS = ("calendly.com", "cal.com", "acuityscheduling.com", "calendar.google.com", "meetings.hubspot.com")
# Blog/CMS taxonomy listings (/tag/sales-team/, /category/our-people/) reuse person words in their
# slugs but list articles, not people. They never establish direct contact-page intent.
TAXONOMY_SEGMENTS = frozenset({"tag", "tags", "category", "categories", "topic", "topics"})

# Outbound links that are page chrome, not candidate sources. They are recorded as automatically
# dismissed with the reason (never silently dropped) and never reach the review queue.
NOISE_HOSTS = (
    ("social profile or share link", SOCIAL_HOSTS + ("pinterest.com", "threads.net")),
    ("document or file share", DOCUMENT_HOSTS),
    ("scheduling page", SCHEDULING_HOSTS),
    ("link shortener", ("goo.gl", "bit.ly", "t.co", "tinyurl.com", "ow.ly", "buff.ly", "lnkd.in",
                        "rebrand.ly", "cutt.ly")),
    ("app store listing", ("apps.apple.com", "itunes.apple.com", "play.google.com", "chromewebstore.google.com")),
    ("review or event platform", ("trustpilot.com", "g2.com", "capterra.com", "luma.com", "eventbrite.com",
                                  "meetup.com")),
    ("policy page", ("policies.google.com",)),
)
NOISE_SUBDOMAINS = (("login or app screen", ("auth", "login", "accounts", "sso", "signin", "dash", "dashboard", "app")),
                    ("documentation or help site", ("docs", "help", "support", "developers")),
                    ("CDN or asset host", ("cdn", "static", "assets")))
NOISE_PATH = (
    ("share button", re.compile(r"/(?:sharer(?:\.php)?|share|sharearticle|intent/(?:tweet|post))(?:/|$)", re.I)),
    ("signup or application form", re.compile(r"/(?:signup|sign-up|register|registration|apply|application)(?:/|$)", re.I)),
    ("legal page", re.compile(r"/(?:privacy(?:-policy)?|terms(?:-of-service|-of-use)?|legal|cookies?(?:-policy)?)(?:/|$)", re.I)),
    ("feed, sitemap or CDN helper", re.compile(r"(?:\.(?:rss|atom)$|/(?:feed|rss|sitemap|cdn-cgi)(?:/|\.xml|$))", re.I)),
    ("help or sponsorship page", re.compile(r"/(?:help|faq|sponsor|sponsors|sponsorship|pass-picker|tickets?)(?:/|$)", re.I)),
)


def noise_reason(url):
    """Why an outbound link is page chrome rather than a candidate source, or None."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host.endswith(("http", "https")) or re.search(r"https?:?//", parsed.path):
        return "malformed link (URL pasted into another URL)"
    for reason, domains in NOISE_HOSTS:
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return reason
    first = host.split(".")[0]
    for reason, prefixes in NOISE_SUBDOMAINS:
        if host.count(".") >= 2 and (first in prefixes or re.fullmatch(r"(?:%s)\d*|.*-cdn" % "|".join(prefixes), first)):
            return reason
    path = unquote(parsed.path)
    for reason, pattern in NOISE_PATH:
        if pattern.search(path):
            return reason
    return None


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
        if "." not in host:
            # e.g. a broken href="http://saas": single-label names are not public websites and could
            # resolve through a local DNS search domain.
            raise ValueError("Single-label hostnames are excluded.")
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


def direct_contact_page_intent(url, label=""):
    """Whether the exact path/label directly describes a contact or person page."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if any(host == domain or host.endswith("." + domain)
           for domain in SOCIAL_HOSTS + DOCUMENT_HOSTS + SCHEDULING_HOSTS):
        return False
    if any(segment.lower() in TAXONOMY_SEGMENTS for segment in unquote(parsed.path).split("/")):
        return False
    direct = " ".join((unquote(parsed.path), label))
    if any(phrase_matches(direct, term) for term in DIRECT_NOISE):
        return False
    if any(phrase_matches(direct, term) for term in DIRECT_TARGETS + PLURAL_ROLE_TARGETS + SALES_ROLE_TARGETS):
        return True
    segments = [words(segment) for segment in unquote(parsed.path).strip("/").split("/") if segment]
    generic = {"login", "portal", "business", "directory", "search", "find", "contact", "about",
               "apply", "application", "support", "resources"}
    return any(segment in ("rep", "representative") and index + 1 < len(segments) and
               segments[index + 1] not in generic for index, segment in enumerate(segments))


COMPANY_PAGE_TARGETS = ("about", "about us", "who we are", "our company", "our story", "meet us")
CONTACT_PAGE_TARGETS = ("contact", "contact us")


def company_page_tier(url, label=""):
    """Fallback tier for pages small businesses often use instead of a /team page.

    Returns 1 for about/company pages, 2 for contact pages, None otherwise. This is only a
    probe-ordering hint inside an already-authorized site's approved scope; it never counts as
    direct contact-page intent and never authorizes a site or a discovery page.
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if any(host == domain or host.endswith("." + domain)
           for domain in SOCIAL_HOSTS + DOCUMENT_HOSTS + SCHEDULING_HOSTS):
        return None
    text = " ".join((unquote(parsed.path), label))
    if any(phrase_matches(text, term) for term in DIRECT_NOISE + EDITORIAL + PRODUCT):
        return None
    if any(phrase_matches(text, term) for term in COMPANY_PAGE_TARGETS):
        return 1
    if any(phrase_matches(text, term) for term in CONTACT_PAGE_TARGETS):
        return 2
    return None


def canonical_exact_start(url, source_url):
    """Compare cleaned URLs and fail closed when either cannot be canonicalized."""
    try:
        return clean_url(url) == clean_url(source_url)
    except (ValueError, UnicodeError):
        return False


def host_word_matches(host, word):
    """True when a hostname label part equals or ends with ``word`` (``host:bank`` matches
    pncbank.com, first-bank.com and bank.example, not bankcardusa.com). The public suffix is
    ignored, so the rule never matches a TLD. Only hostnames are inspected, never page prose."""
    word = word.strip().lower()
    if not word or not re.fullmatch(r"[a-z0-9]+", word):
        return False
    labels = host.split(".")[:-1]
    return any(part == word or part.endswith(word) for label in labels for part in label.split("-"))


def exclusion_matches(rule, url, label, context):
    parsed = urlsplit(url)
    host, path = parsed.hostname.lower().rstrip("."), unquote(parsed.path)
    if rule.startswith("path:"):
        return phrase_matches(path, rule[5:].strip())
    if rule.startswith("host:"):
        return host_word_matches(host, rule[5:])
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
    if direct_contact_page_intent(url, label):
        score += 35
        reasons.append("Direct contact or person page (+35)")
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


def reviewed_sources(source_ids):
    """Return source IDs with at least one present human-reviewed contact."""
    source_ids = set(source_ids)
    if not source_ids:
        return set()
    from leads.models import Observation
    return set(Observation.objects.filter(source_id__in=source_ids, lead__status="reviewed", present=True)
               .values_list("source_id", flat=True).distinct())


def current_candidate_score(campaign, url, label="", context="", source=None, reviewed_source_ids=None,
                            explicit_start=False, origin_yields=None):
    """Current score, including the documented reviewed-source and observed-yield adjustments.

    ``origin_yields`` is an optional precomputed {origin: OriginYield} map for batch callers;
    single-URL callers leave it None and one bounded lookup is made.
    """
    score, reasons = rank(campaign, url, label, context)
    if score >= 0:
        from .yields import EMPTY, origin_yields as lookup, safe_origin, yield_adjustment
        item_origin = safe_origin(url)
        stats = (origin_yields if origin_yields is not None else lookup({item_origin})).get(item_origin, EMPTY)
        delta, reason = yield_adjustment(stats)
        if delta:
            score = max(0, min(100, score + delta))
            reasons = reasons + [reason]
    if source and score >= 0:
        reviewed = (source.pk in reviewed_source_ids if reviewed_source_ids is not None else
                    bool(reviewed_sources({source.pk})))
        if reviewed:
            score = min(100, score + 10)
            reasons = reasons + ["Source has human-reviewed contacts (+10)"]
    if explicit_start and score >= 0:
        score = max(score, 90)
        if "Explicit starting source" not in reasons:
            reasons = reasons + ["Explicit starting source"]
    return score, reasons


def ordinary_page_eligible(campaign, url, label="", context="", source=None, reviewed_source_ids=None,
                           explicit_start=False, origin_yields=None):
    score, reasons = current_candidate_score(
        campaign, url, label, context, source, reviewed_source_ids=reviewed_source_ids,
        explicit_start=explicit_start, origin_yields=origin_yields)
    # Same intent gate that parks low-value inventory (see low_value_page); exact-start and
    # sitemap exemptions stay with the callers that already apply them.
    return score >= campaign.min_score and not low_value_page(url, label), score, reasons


def low_value_page(url, label="", *, kind="page", explicit_start=False):
    """Whether an in-scope URL can never pass the ordinary page gate, whatever its campaign score.

    This is the intent half of ``ordinary_page_eligible``: sitemaps and exact starting pages are
    exempt there too. Score/yield are deliberately ignored because they change over time (a
    yield penalty recovers); missing direct intent does not, for the same URL and label.
    Nearby context is never consulted, so it cannot rescue or condemn a page.
    """
    return kind == "page" and not explicit_start and not direct_contact_page_intent(url, label)


def link_priority(url, label=""):
    """Campaign-free crawl order for an already in-scope link. Never an exclusion."""
    direct = " ".join((unquote(urlsplit(url).path), label))
    score = 35 if any(phrase_matches(direct, word) for word in PRIORITY_TARGETS) else 0
    if any(phrase_matches(direct, word) for word in EDITORIAL):
        score -= 20
    if any(phrase_matches(direct, word) for word in PRODUCT):
        score -= 25
    return score
