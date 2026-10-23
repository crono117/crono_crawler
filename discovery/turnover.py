"""Reversible inventory turnover. Archiving never changes a review or source permission."""
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from leads.models import Observation, PageJob
from .models import Campaign, DiscoveredURL, DiscoveryJob

ARCHIVE_BATCH_LIMIT = 20
# Parking is a distinct, automatic archive class: the URL failed the shared direct-intent gate
# (ranking.low_value_page), so the ordinary queue could never dispatch it. Only rows with this
# exact reason may leave the archive automatically, and only when the same gate later passes.
PARKED_REASON = "Parked: in-scope page without direct contact-page intent; never queued, kept for history."
PARKED_EXTERNAL_REASON = ("Parked: external page without direct contact-page intent; the site policy requires intent, "
                          "so automation can never admit it. Kept for history.")
ORIGIN_CAP_REASON = "Parked: its origin already holds the campaign's active URL share; kept for history, not queued."
PARK_BATCH_SIZE = 500


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


def _protected(campaign):
    """Candidate IDs and source IDs that parking must never touch (evidence, work or setup)."""
    from automation.models import SiteAutomationJob
    from automation.services import RUNNABLE
    setup_sources = set(SiteAutomationJob.objects.filter(state__in=RUNNABLE).values_list("source_id", flat=True))
    jobs = DiscoveryJob.objects.filter(run__campaign=campaign, candidate__isnull=False)
    working = set(jobs.filter(status__in=("queued", "processing"), run__status__in=("queued", "running", "paused"))
                  .values_list("candidate_id", flat=True))
    productive = set(jobs.filter(contacts_seen__gt=0).values_list("candidate_id", flat=True))
    followups = set(jobs.filter(company_job__isnull=False).values_list("candidate_id", flat=True))
    return working | productive | followups, setup_sources


def _unprotected_rows(campaign, rows, protected_ids, setup_sources):
    """rows: (pk, url, label, source_id, source_url, ...). Drops evidence, work, setup and start URLs."""
    from .ranking import canonical_exact_start
    evidence = set(Observation.objects.filter(source_id__in={row[3] for row in rows if row[3]},
                                              page_url__in=[row[1] for row in rows])
                   .values_list("source_id", "page_url"))
    return [row for row in rows
            if row[0] not in protected_ids and row[3] not in setup_sources and (row[3], row[1]) not in evidence
            and not (row[4] and canonical_exact_start(row[1], row[4]))]


def _active_pages(campaign):
    return (campaign.urls.filter(archived_at__isnull=True, kind="page", manual_review_required=False)
            .exclude(method__in=("seed", "operator_import")))


def parkable(campaign):
    """Active approved pages the ordinary gate can never queue, minus anything with evidence or work.

    Uses ranking.low_value_page (the intent half of ordinary_page_eligible), so the result does not
    depend on score, campaign keywords or site yield. Protected: seeds, operator imports, exact
    starting URLs, sitemaps, manual-review rows, company follow-ups, URLs with active jobs, URLs
    whose own fetch stored contacts or whose exact page has a contact observation, and sources in
    automated setup. Source productivity does NOT protect a page: a blog post on a productive site
    is still never queued. Bounded queries; no per-row lookups.
    """
    from .ranking import low_value_page
    protected_ids, setup_sources = _protected(campaign)
    rows = list(_active_pages(campaign).filter(decision="approved", source__isnull=False)
                .values_list("pk", "url", "label", "source_id", "source__url"))
    return [row[0] for row in _unprotected_rows(campaign, rows, protected_ids, setup_sources)
            if low_value_page(row[1], row[2])]


def parkable_external(campaign):
    """Pending external pages an enabled intent-requiring site policy can never admit."""
    from automation.models import SitePolicy
    from .ranking import low_value_page
    if not SitePolicy.objects.filter(campaign=campaign, enabled=True, require_page_intent=True).exists():
        return []
    protected_ids, setup_sources = _protected(campaign)
    rows = list(_active_pages(campaign).filter(decision="pending", source__isnull=True)
                .values_list("pk", "url", "label", "source_id", "source__url"))
    return [row[0] for row in _unprotected_rows(campaign, rows, protected_ids, setup_sources)
            if low_value_page(row[1], row[2])]


def origin_excess(campaign, cap):
    """Lowest-scoring unprotected active pages above ``cap`` per origin, highest scores kept.

    Protected rows (seeds, sitemaps, imports, manual review, start URLs, work, evidence, setup) are
    never selected but still count toward their origin's total.
    """
    if not cap:
        return []
    counted = (campaign.urls.filter(archived_at__isnull=True).exclude(decision="dismissed", dismissal_scope="")
               .order_by().values_list("origin").annotate(n=Count("pk")).filter(n__gt=cap))
    totals = dict(counted)
    if not totals:
        return []
    protected_ids, setup_sources = _protected(campaign)
    rows = list(_active_pages(campaign).filter(origin__in=totals).exclude(decision="dismissed", dismissal_scope="")
                .values_list("pk", "url", "label", "source_id", "source__url", "origin", "score"))
    by_origin = {}
    for row in _unprotected_rows(campaign, rows, protected_ids, setup_sources):
        by_origin.setdefault(row[5], []).append(row)
    selected = []
    for item_origin, total in totals.items():
        candidates = sorted(by_origin.get(item_origin, []), key=lambda row: (row[6], -row[0]))
        selected.extend(row[0] for row in candidates[:total - cap])
    return selected


def protection_breakdown(campaign, origins):
    """{origin: {reason: rows}} for the counted rows of ``origins``: why origin-share parking cannot take them.

    Reasons, in precedence order: seed, operator_import, manual_review, sitemap, active_job,
    produced_contacts, company_followup, setup_source, contact_evidence, starting_url, and
    ``parkable`` for rows the origin cap *can* archive. Read-only; bounded queries. It explains a
    large origin that stays large after ``--origin-cap`` (protected history is never parked).
    """
    from automation.models import SiteAutomationJob
    from automation.services import RUNNABLE
    from .ranking import canonical_exact_start
    origins = set(origins)
    if not origins:
        return {}
    jobs = DiscoveryJob.objects.filter(run__campaign=campaign, candidate__isnull=False)
    working = set(jobs.filter(status__in=("queued", "processing"), run__status__in=("queued", "running", "paused"))
                  .values_list("candidate_id", flat=True))
    productive = set(jobs.filter(contacts_seen__gt=0).values_list("candidate_id", flat=True))
    followups = set(jobs.filter(company_job__isnull=False).values_list("candidate_id", flat=True))
    setup_sources = set(SiteAutomationJob.objects.filter(state__in=RUNNABLE).values_list("source_id", flat=True))
    rows = list(campaign.urls.filter(origin__in=origins, archived_at__isnull=True)
                .exclude(decision="dismissed", dismissal_scope="")
                .values_list("pk", "url", "source_id", "source__url", "origin", "method", "kind", "manual_review_required"))
    evidence = set(Observation.objects.filter(source_id__in={row[2] for row in rows if row[2]},
                                              page_url__in=[row[1] for row in rows])
                   .values_list("source_id", "page_url"))
    result = {item: {} for item in origins}
    for pk, url, source_id, source_url, item_origin, method, kind, manual in rows:
        if method == "seed":
            reason = "seed"
        elif method == "operator_import":
            reason = "operator_import"
        elif manual:
            reason = "manual_review"
        elif kind != "page":
            reason = "sitemap"
        elif pk in working:
            reason = "active_job"
        elif pk in productive:
            reason = "produced_contacts"
        elif pk in followups:
            reason = "company_followup"
        elif source_id in setup_sources:
            reason = "setup_source"
        elif (source_id, url) in evidence:
            reason = "contact_evidence"
        elif source_url and canonical_exact_start(url, source_url):
            reason = "starting_url"
        else:
            reason = "parkable"
        result[item_origin][reason] = result[item_origin].get(reason, 0) + 1
    return result


def park(campaign, ids, reason=PARKED_REASON):
    """Archive rows with a parking reason. Decisions, sources and history are untouched."""
    ids = list(ids)
    now = timezone.now()
    parked = 0
    for start in range(0, len(ids), PARK_BATCH_SIZE):
        parked += (campaign.urls.filter(pk__in=ids[start:start + PARK_BATCH_SIZE], archived_at__isnull=True)
                   .update(archived_at=now, archive_reason=reason))
    return parked
