"""Bounded source searches, scheduled independently and owned by the collector lease."""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from leads.models import DomainState
from leads.services.extraction import soup_for
from leads.services.network import FetchError
from .models import Campaign, DailyUsage, DiscoveryJob, DiscoveryRun, SourceSearch
from .providers import BRAVE_ORIGIN, brave_search, search_ready
from .ranking import clean_url, rank
from .turnover import has_room


# Title words that name pages listing people. Each maps to a DIRECT_TARGETS/PLURAL_ROLE_TARGETS term in
# ranking.direct_contact_page_intent, so a result whose title matches is a direct-intent candidate.
# Brave documents ``intitle:`` as a (still experimental) operator; one bare term each, no nested quotes.
PRECISION_TITLES = ("team", "staff", "people", "agents", "representatives")


def query_pool(campaign):
    queries = [q.strip() for q in campaign.search_queries.splitlines() if q.strip()][:10]
    if campaign.auto_search_queries:
        def phrase(value):
            return " ".join(value.replace('"', "").replace("\\", "").split())[:160]
        industries = list(dict.fromkeys(phrase(k) for k in campaign.keywords.splitlines() if phrase(k)))[:6]
        roles = list(dict.fromkeys(phrase(k) for k in campaign.sales_terms.splitlines() if phrase(k)))[:2]
        region = f' "{phrase(campaign.region)}"' if phrase(campaign.region) else ""
        if campaign.search_precision:
            # Ask for pages whose own title names a team/people listing, not for homepages that merely
            # mention "contact". Bare "contact" is company-page intent, never direct intent.
            queries.extend(f'"{industry}" intitle:{word}{region}' for word in PRECISION_TITLES for industry in industries)
        else:
            intents = ["our team", "sales team", "contact"] + [f"{role} team" for role in roles]
            # Rotate industry before moving to another intent; no domains or people are invented.
            queries.extend(f'"{industry}" "{intent}"{region}' for intent in intents for industry in industries)
    return list(dict.fromkeys(q for q in queries if len(q) <= 600 and len(q.split()) <= 75))


def search_status(campaign):
    """Read-only status shared by the UI, Lab and diagnostic command. Never expose keys."""
    now = timezone.now()
    usage = DailyUsage.objects.filter(campaign=campaign, day=now.date()).first()
    global_used = DailyUsage.objects.filter(day=now.date()).aggregate(n=Sum("searches"))["n"] or 0
    latest = campaign.source_searches.first()
    queued = campaign.source_searches.filter(status__in=("queued", "processing")).first()
    legacy = campaign.runs.filter(status__in=("queued", "running", "paused"),
        jobs__kind="search", jobs__status__in=("queued", "processing")).exists()
    used = usage.searches if usage else 0
    active_urls = campaign.saved_url_count()
    if not campaign.active:
        reason = "Campaign is paused."
    elif not campaign.search_enabled:
        reason = "Source search is disabled for this campaign."
    elif not settings.BRAVE_SEARCH_ENABLED:
        reason = "Brave search is disabled in the server configuration."
    elif not settings.BRAVE_SEARCH_API_KEY:
        reason = "Brave API key is missing."
    elif not query_pool(campaign):
        reason = "Add exact queries or enable generated queries with industry phrases."
    elif used >= campaign.daily_search_limit or global_used >= settings.BRAVE_DAILY_SEARCH_LIMIT:
        reason = "Daily search budget reached; resumes next UTC day."
    elif legacy:
        reason = "Waiting for previously queued search jobs to finish; their retry delays are preserved."
    elif not has_room(campaign):
        reason = "Active URL inventory is full; no eligible turnover space."
    elif queued:
        reason = queued.message or ("Source search is running." if queued.status == "processing" else "Source search queued.")
    elif campaign.next_search_at > now:
        reason = "Waiting for the next source search or query cooldown."
    else:
        reason = "Ready for a source search on the next worker tick."
    return {"campaign_id": campaign.pk, "name": campaign.name, "reason": reason,
        "provider_enabled": bool(settings.BRAVE_SEARCH_ENABLED), "key_present": bool(settings.BRAVE_SEARCH_API_KEY),
        "enabled": campaign.search_enabled, "generated_queries": campaign.auto_search_queries,
        "query_count": len(query_pool(campaign)), "next_at": queued.available_at if queued else campaign.next_search_at,
        "searches_today": used, "daily_limit": campaign.daily_search_limit,
        "global_searches_today": global_used, "global_daily_limit": settings.BRAVE_DAILY_SEARCH_LIMIT,
        "active_urls": active_urls, "url_limit": campaign.max_candidates,
        "archived_urls": campaign.urls.filter(archived_at__isnull=False).count(),
        "latest": ({"query": latest.query, "status": latest.status, "message": latest.message,
                    "summary": latest.summary, "finished_at": latest.finished_at} if latest else None)}


@transaction.atomic
def schedule(campaign):
    campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
    now = timezone.now()
    if not campaign.active or not campaign.search_enabled or campaign.next_search_at > now:
        return None
    if campaign.source_searches.filter(status__in=("queued", "processing")).exists():
        return None
    # Upgrades drain legacy searches once, without resetting their persisted budget/backoff.
    if DiscoveryJob.objects.filter(run__campaign=campaign, run__status__in=("queued", "running", "paused"),
            kind="search", status__in=("queued", "processing")).exists():
        return None
    campaign.next_search_at = now + timedelta(minutes=campaign.search_interval_minutes)
    pool = query_pool(campaign)
    if not search_ready() or not pool or not has_room(campaign):
        campaign.save(update_fields=["next_search_at"])
        return None
    recent = {}
    for attempt in campaign.source_searches.filter(query__in=pool, finished_at__gte=now - timedelta(days=7)):
        recent.setdefault(attempt.query, attempt)
    rotated = pool[campaign.search_query_cursor % len(pool):] + pool[:campaign.search_query_cursor % len(pool)]
    available, cooldowns = [], []
    for query in rotated:
        last = recent.get(query)
        if last:
            delay = (timedelta(days=1 if last.summary.get("new_origins", 0) else 7)
                     if last.status == "done" else timedelta(hours=1))
            until = last.finished_at + delay
            if until > now:
                cooldowns.append(until)
                continue
        available.append(query)
    attempt = None
    if available:
        from .yields import EMPTY, query_yields, search_priority
        from automation.learning import merge_yields, query_setup_stats
        # Page yield plus what the sites each query admitted later produced in automatic setup, so a
        # query that keeps returning homepages with no people stops outranking one that finds contacts.
        history = merge_yields(query_yields(available), query_setup_stats(available))
        query = max(available, key=lambda q: search_priority(history.get(q, EMPTY)))
        campaign.search_query_cursor = (pool.index(query) + 1) % len(pool)
        attempt = SourceSearch.objects.create(campaign=campaign, query=query)
    elif cooldowns:
        campaign.next_search_at = min(cooldowns)
    campaign.save(update_fields=["next_search_at", "search_query_cursor"])
    return attempt


def schedule_due():
    for campaign in Campaign.objects.filter(active=True, search_enabled=True, next_search_at__lte=timezone.now()):
        schedule(campaign)


def result_specs(campaign, query, results):
    specs = []
    for result in results[:20]:
        if not isinstance(result, dict) or not isinstance(result.get("url"), str):
            continue
        try:
            url = clean_url(result["url"])
        except (ValueError, UnicodeError):
            continue
        def plain(key):
            value = result.get(key, "")
            return soup_for(value if isinstance(value, str) else "").get_text(" ", strip=True)
        label, context = plain("title"), plain("description")
        specs.append({"url": url, "label": label, "context": context, "query": query, "method": "search",
                      "priority": rank(campaign, url, label, context)[0]})
    return specs


def complete(attempt, message, status="done"):
    attempt.status, attempt.message, attempt.finished_at = status, message[:1000], timezone.now()
    attempt.save(update_fields=["status", "message", "finished_at", "summary", "run"])
    campaign = Campaign.objects.get(pk=attempt.campaign_id)
    next_at = timezone.now() + timedelta(minutes=campaign.search_interval_minutes)
    Campaign.objects.filter(pk=campaign.pk, next_search_at__lt=next_at).update(next_search_at=next_at)
    from leads.services.activity import emit
    emit("result" if status == "done" else "error", f"Source search #{attempt.pk}: {message}", lane="discovery")


@transaction.atomic
def ingest(attempt, results):
    from .services import OPEN, finish, register_batch
    campaign = Campaign.objects.select_for_update().get(pk=attempt.campaign_id)
    if not campaign.active or not campaign.search_enabled:
        complete(attempt, "Campaign or search paused during request; results were not admitted.", "skipped")
        return
    specs = result_specs(campaign, attempt.query, results)
    run = campaign.runs.filter(status__in=OPEN).first()
    opened = run is None
    if opened:
        run = DiscoveryRun.objects.create(campaign=campaign)
    stats = {"results": len(results), "invalid": len(results) - len(specs)}
    register_batch(run, specs, campaign=campaign, cleaned=True, order=True, allow_turnover=True,
                   domain_budget=campaign.max_new_domains, stats=stats)
    attempt.summary, attempt.run = stats, run
    complete(attempt, f"{stats['new_urls']} new URLs, {stats['new_origins']} new origins, "
        f"{stats['duplicates']} duplicates, {stats['filtered']} filtered, "
        f"{stats['capacity_rejected']} capacity-rejected, {stats['domain_rejected']} domain-limit rejected, "
        f"{stats['archived_ignored']} already archived; {stats['archived']} old URLs archived. "
        "New sites use the existing source review and setup policy.")
    if opened:
        finish(run)


def process(attempt):
    from .services import defer, reserve_budget
    from leads.services.worker import retry_delay
    campaign = Campaign.objects.get(pk=attempt.campaign_id)
    if not campaign.active or not campaign.search_enabled:
        complete(attempt, "Campaign or source search is disabled; no request made.", "skipped")
        return
    if not search_ready() or not query_pool(campaign):
        defer(attempt, timezone.now() + timedelta(hours=1), "Search configuration required; page collection can continue.")
        return
    if attempt.query not in query_pool(campaign):
        complete(attempt, "Search queries changed; no request made.", "skipped")
        return
    if not has_room(campaign):
        defer(attempt, timezone.now() + timedelta(hours=1), "Active URL inventory is full; no eligible turnover space.")
        return
    state, _ = DomainState.objects.get_or_create(origin=BRAVE_ORIGIN)
    if state.next_allowed_at > timezone.now():
        defer(attempt, state.next_allowed_at, "Waiting for search provider rate limit.")
        return
    if not reserve_budget(campaign, attempt, search=True):
        return
    state.next_allowed_at = timezone.now() + timedelta(seconds=2)
    state.save(update_fields=["next_allowed_at"])
    attempt.attempts += 1
    attempt.save(update_fields=["attempts"])
    try:
        results = brave_search(attempt.query)
        ingest(attempt, results)
    except Exception as exc:
        # No raw exception/provider bodies: they can contain request credentials.
        attempt.refresh_from_db()
        if isinstance(exc, FetchError) and exc.status in (401, 403) and attempt.attempts < 4:
            message = "Search provider denied access; review API configuration. Page collection can continue."
            until = timezone.now() + timedelta(hours=24)
        elif isinstance(exc, FetchError) and exc.retryable and attempt.attempts < 4:
            message = "Search provider temporarily unavailable; retry scheduled."
            until = timezone.now() + timedelta(seconds=retry_delay(attempt.attempts, exc.retry_after))
        else:
            complete(attempt, "Source search failed; inspect provider configuration and worker diagnostics.", "failed")
            return
        DomainState.objects.filter(pk=state.pk).update(next_allowed_at=until)
        defer(attempt, until, message)


def tick():
    """Only called while the existing collector lease is held; no new worker."""
    with transaction.atomic():
        attempt = SourceSearch.objects.filter(status="queued", available_at__lte=timezone.now(),
            campaign__active=True, campaign__search_enabled=True).order_by("available_at", "id").first()
        if attempt is None:
            return False
        attempt.status, attempt.started_at = "processing", timezone.now()
        attempt.save(update_fields=["status", "started_at"])
    from leads.services.activity import emit
    emit("pick", f"Source search #{attempt.pk}: {attempt.query}", lane="discovery")
    process(attempt)
    return True
