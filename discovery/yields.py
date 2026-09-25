"""Observed discovery yield per exact origin, derived from completed page jobs.

Yield is a priority signal only. It never authorizes a URL, widens scope, or
establishes direct contact-page intent; the ordinary eligibility gate still
requires direct intent, and a score is not a confidence percentage.

A "fetched page" is a distinct exact URL whose page job finished with status
``done`` inside the window. A "productive page" is one of those that stored at
least one validated contact (``DiscoveryJob.contacts_seen > 0``). Fetch failures,
skips and sitemap jobs are not evidence about page content and are ignored.
Counts are global across campaigns because they describe the site, not a
campaign's configuration.
"""
import random
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

PROVEN_BONUS = 10
LOW_YIELD_PENALTY = -15
LOW_YIELD_MIN_PAGES = 3
PROVEN_MIN_PRODUCTIVE = 2


@dataclass(frozen=True)
class OriginYield:
    fetched: int = 0
    productive: int = 0

    @property
    def rate(self):
        """Beta(1, 1) posterior mean, so one fetch never swings to 0% or 100%."""
        return (1 + self.productive) / (2 + self.fetched)


EMPTY = OriginYield()


def safe_origin(url):
    """Exact origin, or "" when the URL cannot be canonicalized (no adjustment then)."""
    from leads.services.network import origin
    if not isinstance(url, str):
        return ""
    try:
        return origin(url)
    except (TypeError, ValueError, UnicodeError):
        return ""


def window_days():
    return max(1, int(getattr(settings, "DISCOVERY_YIELD_WINDOW_DAYS", 60)))


def page_jobs(since=None):
    from .models import DiscoveryJob
    since = since or timezone.now() - timedelta(days=window_days())
    return DiscoveryJob.objects.filter(kind="page", status="done", candidate__isnull=False,
                                       run__created_at__gte=since)


def canary_pages(since=None):
    """Released setup-canary pages: fetched by automation, validated against the recipe.

    Only canaries that passed carry ``metadata["validation"]``; failed canaries leave the
    site paused and gated, so they cost no discovery budget and are not counted.
    """
    from automation.models import ProbePage
    since = since or timezone.now() - timedelta(days=window_days())
    return ProbePage.objects.filter(phase="canary", status="done", metadata__has_key="validation",
                                    available_at__gte=since)


def canary_evidence(origins, since=None):
    """{origin: {url: productive}} for released canary pages on the given origins."""
    evidence = {}
    rows = canary_pages(since).values_list("job__source__url", "metadata__url", "url",
                                           "metadata__validation__accepted_records")
    for source_url, final_url, url, accepted in rows:
        item_origin = safe_origin(source_url)
        if item_origin in origins:
            key = final_url or url
            pages = evidence.setdefault(item_origin, {})
            pages[key] = pages.get(key, False) or bool(accepted)
    return evidence


def origin_yields(origins):
    """Return {origin: OriginYield} for the given exact origins (missing = no history).

    Discovery page jobs and released setup canaries both count, as distinct exact URLs,
    so a newly onboarded site starts with the evidence its canary already produced.
    """
    origins = {item for item in origins if item}
    if not origins:
        return {}
    jobs = page_jobs().filter(candidate__origin__in=origins)
    rows = (jobs.values("candidate__origin")
            .annotate(fetched=Count("url", distinct=True),
                      productive=Count("url", distinct=True, filter=Q(contacts_seen__gt=0))))
    result = {row["candidate__origin"]: OriginYield(row["fetched"], row["productive"]) for row in rows}
    canaries = canary_evidence(origins)
    if canaries:
        # Union exact URLs for the few origins with canary evidence, so a recrawled canary page counts once.
        for url, item_origin, seen in (jobs.filter(candidate__origin__in=canaries)
                                       .values_list("url", "candidate__origin", "contacts_seen")):
            pages = canaries[item_origin]
            pages[url] = pages.get(url, False) or seen > 0
        for item_origin, pages in canaries.items():
            result[item_origin] = OriginYield(len(pages), sum(pages.values()))
    return result


def yield_adjustment(stats):
    """Bounded, explainable score change for one origin's history."""
    if stats.fetched >= LOW_YIELD_MIN_PAGES and stats.productive == 0:
        return LOW_YIELD_PENALTY, (f"Site stored no validated contacts on {stats.fetched} fetched pages "
                                   f"({LOW_YIELD_PENALTY})")
    if stats.productive >= PROVEN_MIN_PRODUCTIVE and stats.rate >= 0.5:
        return PROVEN_BONUS, (f"Site stored contacts on {stats.productive} of {stats.fetched} fetched pages "
                              f"(+{PROVEN_BONUS})")
    return 0, ""


SEARCH_PRIORITY_BASE = 80
SEARCH_PRIORITY_SPAN = 10


def query_yields(queries):
    """Return {query: OriginYield} for pages found by each configured search query.

    Credit follows each URL's latest discovery context (``DiscoveredURL.search_query``).
    Results that were never fetched (for example new domains still awaiting review)
    add no evidence, so such a query keeps its exploratory prior.
    """
    queries = {item for item in queries if item}
    if not queries:
        return {}
    rows = (page_jobs().filter(candidate__method="search", candidate__search_query__in=queries)
            .values("candidate__search_query")
            .annotate(fetched=Count("url", distinct=True),
                      productive=Count("url", distinct=True, filter=Q(contacts_seen__gt=0))))
    return {row["candidate__search_query"]: OriginYield(row["fetched"], row["productive"]) for row in rows}


def search_priority(stats, rng=random):
    """Thompson sample from Beta(1 + productive, 1 + empty), mapped into the search band.

    Proven queries usually run first when the daily search budget cannot cover every
    query; untried queries keep a uniform prior so they still get explored.
    """
    theta = rng.betavariate(1 + stats.productive, 1 + stats.fetched - stats.productive)
    return SEARCH_PRIORITY_BASE + round(SEARCH_PRIORITY_SPAN * theta)
