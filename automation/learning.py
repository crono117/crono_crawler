"""Learn from durable setup outcomes (SiteAssessment) without ever deciding access.

Every automatic setup ends in a durable ``SiteAssessment`` outcome. This module turns those
outcomes into three bounded, explainable signals that only *rank* future candidates and
search queries:

* per search query: how many sites it admitted that later produced contacts vs. durable misses
  (``query_setup_stats``), merged into the existing Thompson-sampled query choice;
* per site family: whether a sibling site already refused automated access or published no
  usable people (``family_adjustment``), so a second subdomain of the same company does not
  take another slot for the same reason;
* per admission tier / method / query: a read-only funnel (``admission_funnel``) so an
  operator can see whether exploratory slots are worth their share.

Nothing here authorizes, fetches, dismisses or widens scope. Counts only: no names, emails or
phone numbers are read.
"""
from collections import Counter
from urllib.parse import urlsplit

from discovery.yields import OriginYield

# The site refuses (or its policy forbids) automated access. This describes the whole company
# and its other hostnames too, not just the one page that was probed.
SITE_WIDE_NEGATIVE = frozenset({"blocked", "robots_disallowed", "collection_prohibited"})
# The probed site was reachable but did not publish named sales people with evidence-backed
# contacts. A different query is more likely to find a better site than retrying this kind.
CONTENT_NEGATIVE = frozenset({"no_people_listed", "cards_without_people", "people_without_personal_contacts",
                              "contacts_rejected", "no_pages"})
POSITIVE = frozenset({"contacts_found"})
# Deliberately not counted against a query or family: unavailable pages, redirect-scope and
# canonical conflicts, and generic setup errors describe our transport or scope handling, not
# whether the query/site was a good place to look.

FAMILY_SITE_WIDE_PENALTY = -60
FAMILY_CONTENT_PENALTY = -20
FAMILY_PROVEN_BONUS = 15
QUERY_MIN_EVIDENCE = 2
QUERY_SPAN = 20

_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne"})
# Hosting platforms whose subdomains belong to unrelated companies (acme.wixsite.com and
# zeta.wixsite.com share nothing). Each such host is its own family, never grouped.
SHARED_HOSTING = frozenset({
    "wixsite.com", "wix.com", "github.io", "gitlab.io", "myshopify.com", "squarespace.com", "weebly.com",
    "godaddysites.com", "business.site", "blogspot.com", "wordpress.com", "netlify.app", "vercel.app",
    "herokuapp.com", "pages.dev", "webflow.io", "carrd.co", "strikingly.com", "site123.me", "jimdofree.com",
    "jimdosite.com", "webnode.page", "mystrikingly.com", "ueniweb.com", "hubspotpagebuilder.com",
    "azurewebsites.net", "framer.website", "my.canva.site", "cloudfront.net"})


def site_family(url_or_origin):
    """Approximate registrable domain, for soft diversity/learning only.

    ``blog.acme.com`` and ``www.acme.com`` share the family ``acme.com``; ``x.acme.co.uk`` is
    ``acme.co.uk``. This is a heuristic (no public-suffix list is bundled), so it is never used to
    authorize, block or dismiss anything.
    """
    try:
        host = (urlsplit(url_or_origin).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        family = ".".join(labels[-3:])
    else:
        family = ".".join(labels[-2:])
    # Shared platforms: the tenant subdomain is the company.
    if family in SHARED_HOSTING or ".".join(labels[-3:]) in SHARED_HOSTING:
        return host
    return family


def classify(job_state, outcome):
    """'success', 'failure' or None (still unknown, or not about the site)."""
    if job_state == "active" or outcome in POSITIVE:
        return "success"
    if outcome in SITE_WIDE_NEGATIVE or outcome in CONTENT_NEGATIVE:
        return "failure"
    return None


def latest_outcomes(job_ids):
    """{job_id: outcome} using each job's most recent assessment."""
    from .models import SiteAssessment
    result = {}
    job_ids = list(job_ids)
    for start in range(0, len(job_ids), 500):
        rows = (SiteAssessment.objects.filter(job_id__in=job_ids[start:start + 500])
                .order_by("id").values_list("job_id", "outcome"))
        result.update(rows)
    return result


def query_setup_stats(queries):
    """{query: OriginYield} counted in sites: ``productive`` reached contacts, ``fetched`` also failed.

    Each admitted site counts once, attributed to the query recorded on its job at admission.
    Sites whose setup has not reached a durable outcome yet add no evidence.
    """
    from .models import SiteAutomationJob
    queries = {query for query in queries if query}
    if not queries:
        return {}
    jobs = list(SiteAutomationJob.objects.filter(admission_query__in=queries)
                .values_list("pk", "state", "admission_query"))
    outcomes = latest_outcomes(pk for pk, _, _ in jobs)
    totals = {}
    for pk, state, query in jobs:
        verdict = classify(state, outcomes.get(pk, ""))
        if verdict is None:
            continue
        fetched, productive = totals.get(query, (0, 0))
        totals[query] = (fetched + 1, productive + (verdict == "success"))
    return {query: OriginYield(*pair) for query, pair in totals.items()}


def merge_yields(first, second):
    """Add two {key: OriginYield} maps."""
    merged = dict(first)
    for key, stats in second.items():
        base = merged.get(key, OriginYield())
        merged[key] = OriginYield(base.fetched + stats.fetched, base.productive + stats.productive)
    return merged


def query_adjustment(stats):
    """Bounded rank change for a query's admitted-site history; zero until there is evidence."""
    if stats.fetched < QUERY_MIN_EVIDENCE:
        return 0, ""
    delta = round(QUERY_SPAN * 2 * (stats.rate - 0.5))
    if not delta:
        return 0, ""
    return delta, (f"Query admitted {stats.fetched} sites, {stats.productive} reached contacts "
                   f"({'+' if delta > 0 else ''}{delta})")


def family_outcomes():
    """{family: Counter(outcome)} across every durable assessment."""
    from .models import SiteAssessment
    families = {}
    for origin_value, outcome in SiteAssessment.objects.values_list("origin", "outcome"):
        family = site_family(origin_value)
        if family:
            families.setdefault(family, Counter())[outcome] += 1
    return families


def family_adjustment(counter):
    """Soft rank change for a site whose family already has durable outcomes."""
    if not counter:
        return 0, ""
    if any(counter[name] for name in POSITIVE):
        return FAMILY_PROVEN_BONUS, f"Same company already produced contacts (+{FAMILY_PROVEN_BONUS})"
    if any(counter[name] for name in SITE_WIDE_NEGATIVE):
        return FAMILY_SITE_WIDE_PENALTY, f"Same company refused automated access ({FAMILY_SITE_WIDE_PENALTY})"
    if any(counter[name] for name in CONTENT_NEGATIVE):
        return FAMILY_CONTENT_PENALTY, f"Same company published no usable people ({FAMILY_CONTENT_PENALTY})"
    return 0, ""


def admission_funnel(campaign=None):
    """Read-only: setup results by how each site was admitted (tier, method), plus the best/worst queries."""
    from .models import SiteAutomationJob
    jobs = SiteAutomationJob.objects.all()
    if campaign is not None:
        jobs = jobs.filter(campaign=campaign)
    rows = list(jobs.values_list("pk", "state", "admission_tier", "admission_method", "admission_query"))
    outcomes = latest_outcomes(pk for pk, *_ in rows)
    by_tier, by_query = {}, {}
    for pk, state, tier, method, query in rows:
        outcome = outcomes.get(pk, "")
        for table, key in ((by_tier, f"{tier or 'unranked'}/{method or 'unknown'}"), (by_query, query)):
            if table is by_query and not key:
                continue
            entry = table.setdefault(key, {"jobs": 0, "active": 0, "outcomes": Counter()})
            entry["jobs"] += 1
            entry["active"] += state == "active"
            entry["outcomes"][outcome or state] += 1
    def render(table, limit=None):
        items = sorted(table.items(), key=lambda kv: (-kv[1]["active"], -kv[1]["jobs"], kv[0]))
        return [{"key": key, "jobs": value["jobs"], "active": value["active"],
                 "outcomes": dict(sorted(value["outcomes"].items()))} for key, value in items[:limit]]
    return {"by_tier_and_method": render(by_tier), "by_query": render(by_query, 20)}
