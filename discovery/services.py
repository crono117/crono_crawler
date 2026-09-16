"""Persistent discovery jobs, executed by the existing single collector worker."""
import hashlib
import logging
from datetime import datetime, time, timedelta, timezone as dt_timezone
from pathlib import Path
from urllib.parse import urljoin
from django.conf import settings
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone
from leads.models import DomainState, Lead, Observation, PageSnapshot, Source
from leads.services.extraction import diagnostic_message, extract, signature, soup_for
from leads.services.network import FetchError, Response, fetch, in_scope, origin, require_success
from leads.services.storage import save_records, touch_unchanged
from .models import Campaign, DailyUsage, DiscoveredURL, DiscoveryJob, DiscoveryRun
from .providers import BRAVE_ORIGIN, MAX_SITEMAP_BYTES, brave_search, search_ready, sitemap_entries
from .ranking import clean_url, rank

logger = logging.getLogger(__name__)
OPEN = ("queued", "running", "paused")
DEMO_ORIGIN = "https://discovery.example.test"


def tomorrow():
    return datetime.combine(timezone.now().date() + timedelta(days=1), time.min, tzinfo=dt_timezone.utc)


def approved_source(campaign, url):
    return next((source for source in campaign.sources.filter(approved=True).order_by("id") if in_scope(source, url)), None)


def refresh_priorities(campaign):
    """Re-score saved URLs without changing operator approvals or dismissals."""
    candidates = list(campaign.urls.select_related("source"))
    for candidate in candidates:
        candidate.score, candidate.reasons = rank(campaign, candidate.url, candidate.label, candidate.context)
    DiscoveredURL.objects.bulk_update(candidates, ["score", "reasons"], batch_size=200)
    return candidates


@transaction.atomic
def register(run, raw_url, *, label="", context="", found_on="", method="link", query="", depth=0, kind="page", seed=False):
    """Record an exact URL, without fetching or DNS-resolving a new domain."""
    try:
        url = clean_url(raw_url)
    except (ValueError, UnicodeError):
        return None
    campaign = run.campaign
    score, reasons = rank(campaign, url, label, context)
    if seed and score >= 0:
        score, reasons = max(score, 90), reasons + ["Explicit starting source"]
    source = approved_source(campaign, url)
    if source and score >= 0 and Observation.objects.filter(source=source, lead__status="reviewed", present=True).exists():
        score = min(100, score + 10)
        reasons.append("Source has human-reviewed contacts (+10)")
    candidate = DiscoveredURL.objects.filter(campaign=campaign, url=url).first()
    if candidate:
        DiscoveryRun.objects.filter(pk=run.pk).update(duplicates_seen=F("duplicates_seen") + 1)
        candidate.last_seen = timezone.now()
        candidate.score, candidate.reasons = score, reasons
        if label or context:
            candidate.label, candidate.context = str(label)[:200], str(context)[:600]
        if found_on or query:
            candidate.found_on, candidate.search_query = found_on[:1500], query[:600]
            candidate.method = method
        if kind == "sitemap":
            candidate.kind = kind
        candidate.save()
    else:
        if campaign.urls.count() >= campaign.max_candidates:
            DiscoveryRun.objects.filter(pk=run.pk).update(message="Saved URL limit reached; raise the campaign limit to expand further.")
            return None
        new_domain = not source and not campaign.urls.filter(origin=origin(url)).exists()
        run.refresh_from_db(fields=["new_domains"])
        if new_domain and run.new_domains >= campaign.max_new_domains:
            return None
        candidate = DiscoveredURL.objects.create(campaign=campaign, first_run=run, url=url, origin=origin(url),
            label=str(label)[:200], context=str(context)[:600], found_on=found_on[:1500], method=method,
            search_query=query[:600], score=score, reasons=reasons, kind=kind,
            decision="dismissed" if score < 0 else ("approved" if source else "pending"), source=source)
        DiscoveryRun.objects.filter(pk=run.pk).update(candidates_found=F("candidates_found") + 1, new_domains=F("new_domains") + int(new_domain))
    if candidate.decision != "dismissed" and source:
        candidate.source, candidate.decision = source, "approved"
        candidate.save(update_fields=["source", "decision"])
        queue_candidate(run, candidate, depth=depth, kind=candidate.kind, seed=seed)
    return candidate


def queue_candidate(run, candidate, *, depth=0, kind="page", seed=False):
    campaign = run.campaign
    if run.status not in ("queued", "running") or candidate.decision != "approved" or not candidate.source_id:
        return
    if candidate.score < 0 or (not seed and kind == "page" and candidate.score < campaign.min_score):
        return
    if depth > (3 if kind == "sitemap" else campaign.max_depth):
        return
    if run.jobs.exclude(kind="search").count() >= campaign.max_pages:
        return
    DiscoveryJob.objects.get_or_create(run=run, kind=kind, url=candidate.url, defaults={
        "candidate": candidate, "source": candidate.source, "depth": depth,
        "priority": 95 if kind == "sitemap" else candidate.score})


@transaction.atomic
def start(campaign):
    campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
    if not campaign.sources.filter(approved=True).exists() and not campaign.search_enabled:
        raise ValueError("Choose at least one approved starting source or enable configured search.")
    if campaign.search_enabled and (not search_ready() or not campaign.search_queries.strip()):
        raise ValueError("Configure Brave search and add a query, or turn search off.")
    run = campaign.runs.filter(status__in=OPEN).first()
    campaign.active, campaign.last_error = True, ""
    campaign.next_due_at = timezone.now() + timedelta(hours=campaign.interval_hours)
    campaign.save()
    if run:
        run.status, run.message = "queued", "Resumed by operator."
        run.save()
        # Preserve retry, rate-limit, and budget deferrals across pause/resume.
        return run
    run = DiscoveryRun.objects.create(campaign=campaign)
    for source in campaign.sources.filter(approved=True).order_by("id"):
        register(run, source.url, label=source.name, context=source.company, method="seed", seed=True)
        if campaign.use_sitemaps:
            sitemap = origin(source.url) + "/sitemap.xml"
            if in_scope(source, sitemap):
                register(run, sitemap, found_on=source.url, method="sitemap", kind="sitemap", seed=True)
    # Reviewed deeper paths remain useful even if their original directory disappears.
    remembered = [candidate for candidate in refresh_priorities(campaign)
                  if candidate.decision == "approved" and candidate.method != "seed"]
    selected_ids = set(campaign.sources.filter(approved=True).values_list("id", flat=True))
    for candidate in sorted(remembered, key=lambda item: item.score, reverse=True):
        if candidate.source_id in selected_ids and in_scope(candidate.source, candidate.url):
            kind = candidate.kind
            if kind != "sitemap" or campaign.use_sitemaps:
                queue_candidate(run, candidate, kind=kind)
    if campaign.search_enabled:
        for query in list(dict.fromkeys(q.strip() for q in campaign.search_queries.splitlines() if q.strip()))[:10]:
            DiscoveryJob.objects.create(run=run, kind="search", url=query, priority=85)
    finish(run)
    return run


@transaction.atomic
def pause(campaign, message="Paused by operator."):
    Campaign.objects.filter(pk=campaign.pk).update(active=False, last_error=message)
    campaign.runs.filter(status__in=("queued", "running")).update(status="paused", message=message)


def pause_for_source(source, message="A source used by this campaign was paused or edited."):
    for campaign in source.discovery_campaigns.filter(active=True):
        pause(campaign, message)


@transaction.atomic
def approve(candidate, source):
    if not source.approved or not in_scope(source, candidate.url):
        raise ValueError("Choose a reviewed source whose approved scope contains this exact URL.")
    candidate.campaign.sources.add(source)
    # Approve the selected URL even if explicitly dismissed earlier. Other dismissals persist.
    candidate.decision, candidate.source = "approved", source
    candidate.save(update_fields=["decision", "source"])
    run = candidate.campaign.runs.filter(status__in=("queued", "running")).first()
    for sibling in candidate.campaign.urls.filter(origin=origin(source.url)).exclude(decision="dismissed"):
        if in_scope(source, sibling.url):
            sibling.decision, sibling.source = "approved", source
            sibling.save(update_fields=["decision", "source"])
            if run and candidate.campaign.active:
                queue_candidate(run, sibling, kind=sibling.kind)


def schedule_due():
    for campaign in Campaign.objects.filter(active=True, next_due_at__lte=timezone.now()):
        if not campaign.runs.filter(status__in=OPEN).exists():
            try:
                start(campaign)
            except ValueError as exc:
                pause(campaign, str(exc))


def defer(job, until, message):
    job.status, job.available_at, job.message = "queued", until, message
    job.save(update_fields=["status", "available_at", "message"])


@transaction.atomic
def reserve(job, search=False):
    """Durable quotas; retries/crashes never refund a possibly executed request."""
    campaign = job.run.campaign
    usage, _ = DailyUsage.objects.get_or_create(campaign=campaign, day=timezone.now().date())
    usage = DailyUsage.objects.select_for_update().get(pk=usage.pk)
    if search:
        total = DailyUsage.objects.filter(day=usage.day).aggregate(total=Sum("searches"))["total"] or 0
        if usage.searches >= campaign.daily_search_limit or total >= settings.BRAVE_DAILY_SEARCH_LIMIT:
            defer(job, tomorrow(), "Daily search budget reached; resumes next UTC day.")
            return False
        usage.searches += 1
    else:
        if usage.requests >= campaign.daily_requests:
            defer(job, tomorrow(), "Daily fetch budget reached; resumes next UTC day.")
            return False
        usage.requests += 1
    usage.save()
    return True


def record_links(job, html, base_url):
    links = []
    campaign = job.run.campaign
    for node in soup_for(html).select("a[href]")[:2000]:
        try:
            url = clean_url(urljoin(base_url, node["href"]))
        except (ValueError, UnicodeError):
            continue
        label = node.get_text(" ", strip=True)[:200]
        # A whole navigation menu/body must not make every link look like a team
        # page. Use nearby card text only when the container is small and local.
        parent = node.parent
        context = label
        navigation = node.find_parent(["nav", "header", "footer"]) or node.find_parent(attrs={"role": "navigation"})
        if not navigation and parent and parent.name in ("article", "li", "tr", "p", "div", "section") and len(parent.find_all("a", limit=3)) < 3:
            parts, length = [], 0
            for value in parent.stripped_strings:
                parts.append(value[:600 - length])
                length += len(parts[-1]) + 1
                if length >= 600:
                    break
            context = " ".join(parts)[:600]
        score, _ = rank(campaign, url, label, context)
        links.append((score, url, label, context))
    for _, url, label, context in sorted(links, key=lambda row: row[0], reverse=True):
        register(job.run, url, label=label, context=context, found_on=base_url, depth=job.depth + 1)


def demo_response(url):
    html = (Path(settings.BASE_DIR) / "examples" / "discovery-team.html").read_text()
    cards = soup_for(html).select(".team-member")
    pages = {
        DEMO_ORIGIN + "/": b'<h1>Fictional payment processing directory</h1><a href="/team/">Merchant services sales team</a><a href="https://new-vendor.example.org/reps/">POS reseller representatives (review only)</a>',
        DEMO_ORIGIN + "/team/": b'<h1>Example sales team directory</h1><a href="/team/people/">Meet our merchant services sales representatives</a>',
        DEMO_ORIGIN + "/team/people/": "".join(str(card) for card in cards[:2]).encode(),
        DEMO_ORIGIN + "/team/hidden/": str(cards[2]).encode(),
        DEMO_ORIGIN + "/sitemap.xml": b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://discovery.example.test/team/hidden/</loc></url></urlset>',
    }
    body = pages.get(url, b"Not found in the fixed offline fixture")
    return Response(url, 200 if url in pages else 404, {"content-type": "application/xml" if url.endswith(".xml") else "text/html"}, body)


def save_page(job, response):
    source = job.source
    content_hash = hashlib.sha256(response.body).hexdigest()
    sig = signature(source)
    cached = PageSnapshot.objects.filter(source=source, url=response.url, content_hash=content_hash, extraction_signature=sig).exists()
    diagnostics = {}
    records, tags = (None, None) if cached else extract(response.text, source, diagnostics=diagnostics)
    with transaction.atomic():
        before = Lead.objects.count()
        count = touch_unchanged(source, response.url) if cached else save_records(source, response.url, content_hash, records, tags)
        created = Lead.objects.count() - before
        PageSnapshot.objects.update_or_create(source=source, url=response.url, defaults={
            "content_hash": content_hash, "extraction_signature": sig, "last_checked": timezone.now()})
        record_links(job, response.text, response.url)
        DiscoveryRun.objects.filter(pk=job.run_id).update(pages_done=F("pages_done") + 1, contacts_seen=F("contacts_seen") + count, new_contacts=F("new_contacts") + created)
        job.contacts_seen = count
        message = f"{count} contact(s); {created} new." if count else "No validated contacts; review page suitability and CSS recipe."
        if diagnostics:
            message += " " + diagnostic_message(diagnostics)
        if cached:
            message += " Content unchanged."
        done(job, message)


def done(job, message, status="done"):
    job.status, job.message = status, message[:1000]
    job.save()
    if job.candidate_id:
        DiscoveredURL.objects.filter(pk=job.candidate_id).update(last_result=job.message)


def finish(run):
    if not run.jobs.filter(status__in=("queued", "processing")).exists():
        failed = run.jobs.filter(status="failed").count()
        DiscoveryRun.objects.filter(pk=run.pk, status__in=("queued", "running")).update(
            status="failed" if failed else "completed", finished_at=timezone.now())


def process(job):
    from leads.services.worker import prepare_domain, parse_robots, pause_source, retry_delay
    campaign = job.run.campaign
    try:
        if job.kind == "search":
            if not campaign.search_enabled or not search_ready():
                pause(campaign, "Search disabled or missing API key; turn search off or configure it before resuming.")
                defer(job, timezone.now(), "Search configuration required.")
                return
            state, _ = DomainState.objects.get_or_create(origin=BRAVE_ORIGIN)
            if state.next_allowed_at > timezone.now():
                defer(job, state.next_allowed_at, "Waiting for search provider rate limit.")
                return
            if not reserve(job, search=True):
                return
            state.next_allowed_at = timezone.now() + timedelta(seconds=2)
            state.save()
            for result in brave_search(job.url):
                register(job.run, result["url"], label=str(result.get("title", "")),
                         context=soup_for(str(result.get("description", ""))).get_text(" ", strip=True),
                         method="search", query=job.url)
            done(job, "Search results saved; new domains await source review.")
            return
        source = job.source
        if not source or not source.approved or not campaign.sources.filter(pk=source.pk).exists() or not in_scope(source, job.url):
            done(job, "Source approval or scope changed; no request made.", "skipped")
            return
        if not job.candidate or job.candidate.decision != "approved":
            done(job, "URL was dismissed or is awaiting review.", "skipped")
            return
        score, _ = rank(campaign, job.url, job.candidate.label, job.candidate.context)
        if score < 0:
            done(job, "URL excluded by current campaign filters; no request made.", "skipped")
            return
        if source.collector == "demo":
            response = demo_response(job.url)
        else:
            state = DomainState.objects.filter(origin=origin(source.url)).first()
            if state and state.next_allowed_at > timezone.now():
                defer(job, state.next_allowed_at, "Waiting for this website's request delay.")
                return
            if not reserve(job):
                return
            guard = prepare_domain(source, job)
            if guard is None:
                return
            if campaign.use_sitemaps:
                state = DomainState.objects.get(origin=origin(source.url))
                for sitemap in (parse_robots(state).site_maps() or [])[:20]:
                    if in_scope(source, sitemap):
                        register(job.run, sitemap, found_on=state.origin + "/robots.txt", method="sitemap", kind="sitemap", seed=True)
            # Discovery is intentionally HTML-first. Existing browser sources can still
            # be collected separately, using the collector's browser option.
            response = fetch(job.url, settings.BOT_USER_AGENT, guard,
                             max_bytes=MAX_SITEMAP_BYTES if job.kind == "sitemap" else 6 * 1024 * 1024)
        if job.kind == "sitemap" and response.status in (404, 410):
            done(job, "No sitemap at this address.", "skipped")
            return
        require_success(response)
        if any(marker in response.text.lower() for marker in ("cf-chl-", "verify you are human", "g-recaptcha", "hcaptcha-container")):
            raise FetchError("Access challenge detected; source paused for review.", status=403)
        if job.kind == "sitemap":
            entries = []
            for kind, url in sitemap_entries(response.body):
                try:
                    entries.append((kind, clean_url(url)))
                except (ValueError, UnicodeError):
                    continue
            # Rank all URLs before applying the bounded job budget.
            entries.sort(key=lambda item: rank(campaign, item[1])[0], reverse=True)
            for kind, url in entries:
                register(job.run, url, found_on=response.url, method="sitemap", kind=kind,
                         depth=job.depth + 1 if kind == "sitemap" else 1)
            done(job, f"Read {len(entries)} sitemap entries; queued matching approved paths within limits.")
        else:
            if "html" not in response.headers.get("content-type", "").lower():
                done(job, "Not an HTML page.", "skipped")
                return
            save_page(job, response)
    except Exception as exc:
        job.refresh_from_db()
        job.attempts += 1
        job.save(update_fields=["attempts"])
        # Messages never include request headers, API keys, or provider response bodies.
        message = str(exc)[:1000]
        if isinstance(exc, FetchError) and exc.status in (401, 403):
            if job.kind == "search":
                pause(campaign, "Search provider denied access; review API configuration.")
            else:
                pause_source(job.source, message)
            defer(job, timezone.now() + timedelta(hours=1), message)
        elif isinstance(exc, FetchError) and exc.retryable and job.attempts < 4:
            until = timezone.now() + timedelta(seconds=retry_delay(job.attempts, exc.retry_after))
            defer(job, until, message)
            DomainState.objects.filter(origin=BRAVE_ORIGIN if job.kind == "search" else origin(job.source.url)).update(next_allowed_at=until)
        else:
            done(job, "Fetch, parsing or extraction failed: " + message, "failed")
            Campaign.objects.filter(pk=campaign.pk).update(last_error=job.message)
        logger.warning("Discovery job %s: %s", job.pk, job.message)
    finally:
        finish(job.run)


def tick():
    """Caller must hold the collector lease; no network I/O in a DB transaction."""
    with transaction.atomic():
        job = DiscoveryJob.objects.select_related("run__campaign", "source", "candidate").filter(
            status="queued", available_at__lte=timezone.now(), run__status__in=("queued", "running"),
            run__campaign__active=True).order_by("run__last_tick_at", "-priority", "id").first()
        if not job:
            return False
        job.status = "processing"
        job.save(update_fields=["status"])
        DiscoveryRun.objects.filter(pk=job.run_id).update(status="running", last_tick_at=timezone.now())
        DiscoveryRun.objects.filter(pk=job.run_id, started_at__isnull=True).update(started_at=timezone.now())
    process(job)
    return True
