"""Reversible inventory turnover. Archiving never changes a review or source permission."""
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from leads.models import Observation, PageJob
from .models import Campaign, DiscoveredURL, DiscoveryJob

ARCHIVE_BATCH_LIMIT = 20


def eligible(campaign):
    """Conservative candidates that may make room for a relevant search result.

    Ten distinct successful pages without contacts are needed to retire an origin's
    backlog. A stale unreviewed URL can also turn over after seven days. Seeds,
    evidence-bearing sources, manual imports and active work remain protected.
    """
    productive_sources = Observation.objects.values("source_id")
    from automation.models import SiteAutomationJob
    from automation.services import RUNNABLE
    setup_sources = SiteAutomationJob.objects.filter(state__in=RUNNABLE).values("source_id")
    collecting_sources = PageJob.objects.filter(status__in=("queued", "processing"),
        run__status__in=("queued", "running", "paused")).values("run__source_id")
    working = DiscoveryJob.objects.filter(candidate__isnull=False, status__in=("queued", "processing"),
        run__status__in=("queued", "running", "paused")).values("candidate_id")
    productive = DiscoveryJob.objects.filter(candidate__isnull=False, contacts_seen__gt=0).values("candidate_id")
    zero_sources = (DiscoveryJob.objects.filter(source__isnull=False, kind="page", status="done")
        .values("source_id").annotate(pages=Count("url", distinct=True), contacts=Sum("contacts_seen"))
        .filter(pages__gte=10, contacts=0).values("source_id"))
    starting_urls = campaign.urls.filter(url=F("source__url")).values("pk")
    base = (campaign.urls.filter(archived_at__isnull=True, manual_review_required=False, kind="page")
        .exclude(method__in=("seed", "operator_import"))
        .exclude(source_id__in=productive_sources).exclude(pk__in=working)
        .exclude(source_id__in=setup_sources).exclude(source_id__in=collecting_sources)
        .exclude(pk__in=productive).exclude(pk__in=starting_urls)
        .exclude(jobs__company_job__isnull=False)
        .exclude(decision="dismissed", dismissal_scope=""))
    return base.filter(
        Q(decision="dismissed") | Q(source_id__in=zero_sources) |
        Q(decision="pending", last_seen__lt=timezone.now() - timedelta(days=7))
    ).order_by("score", "last_seen", "id").distinct()


def has_room(campaign):
    needed = campaign.saved_url_count() - campaign.max_candidates + 1
    return (needed <= 0 or (campaign.auto_archive_candidates and needed <= ARCHIVE_BATCH_LIMIT and
                           eligible(campaign)[:needed].count() >= needed))


def archive_for_result(campaign, *, incoming_score, exclude_urls=(), slots=1):
    """Caller holds the campaign lock. Do not replace a stronger pending URL."""
    if not campaign.auto_archive_candidates or incoming_score < campaign.min_score:
        return []
    if not 1 <= slots <= ARCHIVE_BATCH_LIMIT:
        return []
    candidates = list(eligible(campaign).exclude(url__in=exclude_urls).filter(
        Q(decision="dismissed") | Q(score__lte=incoming_score))[:slots])
    if len(candidates) != slots:
        return []  # No partial archival when an overfull inventory cannot admit this result.
    from leads.services.activity import emit
    for candidate in candidates:
        candidate.archived_at = timezone.now()
        candidate.archive_reason = "Made room for a relevant source search result; dismissed, stale or zero-yield backlog."
        candidate.save(update_fields=["archived_at", "archive_reason"])
        emit("archived", f"Archived {candidate.url} to make room for source discovery; review history retained.",
             lane="discovery", url=candidate.url)
    return candidates


@transaction.atomic
def restore(candidate):
    campaign = Campaign.objects.select_for_update().get(pk=candidate.campaign_id)
    candidate = DiscoveredURL.objects.get(pk=candidate.pk)
    if candidate.archived_at is None:
        return candidate
    counted = not (candidate.decision == "dismissed" and not candidate.dismissal_scope)
    if counted and campaign.saved_url_count() >= campaign.max_candidates:
        raise ValueError("Active URL inventory is full. Make room before restoring this URL.")
    candidate.archived_at, candidate.archive_reason = None, ""
    candidate.save(update_fields=["archived_at", "archive_reason"])
    return candidate
