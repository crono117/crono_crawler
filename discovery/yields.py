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


def origin_yields(origins):
    """Return {origin: OriginYield} for the given exact origins (missing = no history)."""
    origins = {item for item in origins if item}
    if not origins:
        return {}
    rows = (page_jobs().filter(candidate__origin__in=origins).values("candidate__origin")
            .annotate(fetched=Count("url", distinct=True),
                      productive=Count("url", distinct=True, filter=Q(contacts_seen__gt=0))))
    return {row["candidate__origin"]: OriginYield(row["fetched"], row["productive"]) for row in rows}


def yield_adjustment(stats):
    """Bounded, explainable score change for one origin's history."""
    if stats.fetched >= LOW_YIELD_MIN_PAGES and stats.productive == 0:
        return LOW_YIELD_PENALTY, (f"Site stored no validated contacts on {stats.fetched} fetched pages "
                                   f"({LOW_YIELD_PENALTY})")
    if stats.productive >= PROVEN_MIN_PRODUCTIVE and stats.rate >= 0.5:
        return PROVEN_BONUS, (f"Site stored contacts on {stats.productive} of {stats.fetched} fetched pages "
                              f"(+{PROVEN_BONUS})")
    return 0, ""
