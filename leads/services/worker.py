import hashlib
import logging
import re
import uuid
from datetime import timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from leads.models import DomainState, PageJob, PageSnapshot, Run, Source, SourceCandidate, WorkerLease
from .extraction import diagnostic_message, extract, signature, soup_for
from .network import FetchError, Response, canonical_url, fetch, fetch_browser, in_scope, origin, require_success
from .storage import save_records, touch_unchanged

logger = logging.getLogger(__name__)
LEASE_SECONDS = 600
OPEN_STATUSES = ("queued", "running", "paused")
SKIP_EXTENSIONS = re.compile(r"\.(?:pdf|jpe?g|png|gif|svg|webp|zip|exe|mp[34]|css|js|ico|woff2?)$", re.I)

@transaction.atomic
def enqueue(source):
    source = Source.objects.select_for_update().get(pk=source.pk)
    if not source.approved:
        raise ValueError("Review and approve the source before running it.")
    if source.setup_mode == "automatic":
        raise ValueError("This source is managed by Site automation. Use its campaign and setup controls.")
    run = Run.objects.filter(source=source, status__in=OPEN_STATUSES).first()
    if run:
        if run.status == "paused":
            run.status = "queued"
            run.message = "Resumed by operator."
            run.save()
            PageJob.objects.filter(run=run, status="queued").update(available_at=timezone.now())
        source.active = True
        source.last_error = ""
        source.save()
        return run
    run = Run.objects.create(source=source)
    PageJob.objects.create(run=run, url=source.url)
    source.active = True
    source.next_due_at = timezone.now() + timedelta(hours=source.interval_hours)
    source.last_error = ""
    source.save()
    return run

@transaction.atomic
def pause_source(source, message="Paused by operator."):
    from discovery.services import pause_for_source
    Source.objects.filter(pk=source.pk).update(active=False, last_error=message)
    Run.objects.filter(source=source, status__in=("queued", "running")).update(status="paused", message=message)
    if source.setup_mode == "automatic":
        from automation.models import SiteAutomationJob
        from automation.services import pause_job
        job = SiteAutomationJob.objects.filter(source=source).first()
        if job:
            pause_job(job, message)
    else:
        pause_for_source(source, message)

@transaction.atomic
def acquire_lease():
    from discovery.models import DiscoveryJob
    now = timezone.now()
    lease, _ = WorkerLease.objects.get_or_create(key="collector", defaults={"heartbeat_at": now - timedelta(days=1)})
    lease = WorkerLease.objects.select_for_update().get(pk=lease.pk)
    if lease.token and lease.heartbeat_at > now - timedelta(seconds=LEASE_SECONDS):
        return None
    lease.token = uuid.uuid4().hex
    lease.heartbeat_at = now
    lease.save()
    # Checkpointed pages survive hard stops. Repeating an unfinished page is idempotent.
    PageJob.objects.filter(status="processing", run__status__in=OPEN_STATUSES).update(status="queued", available_at=now)
    DiscoveryJob.objects.filter(status="processing", run__status__in=OPEN_STATUSES).update(status="queued", available_at=now)
    from automation.models import SiteAutomationJob
    SiteAutomationJob.objects.filter(processing=True).update(processing=False)
    from classification.models import Evaluation
    Evaluation.objects.filter(provider='mock', state='running').update(state='queued', lease_token='')
    # Paid attempts retain their dispatch owner/reservation until explicit recovery.
    return lease.token

def heartbeat(token):
    if not WorkerLease.objects.filter(key="collector", token=token).update(heartbeat_at=timezone.now()):
        raise RuntimeError("Worker lease lost; refusing to run another collector.")

def release_lease(token):
    WorkerLease.objects.filter(key="collector", token=token).update(token="", heartbeat_at=timezone.now())

def schedule_due():
    for source in Source.objects.filter(active=True, approved=True, setup_mode="rules_only", next_due_at__lte=timezone.now()):
        if not source.runs.filter(status__in=OPEN_STATUSES).exists():
            enqueue(source)

def parse_robots(state):
    rules = RobotFileParser()
    rules.parse(state.robots_text.splitlines())
    return rules

def guard_with_robots(source, rules):
    return lambda url: in_scope(source, url) and rules.can_fetch(settings.BOT_USER_AGENT, url)

def prepare_domain(source, job):
    now = timezone.now()
    state, _ = DomainState.objects.get_or_create(origin=origin(source.url), defaults={"next_allowed_at": now})
    if state.next_allowed_at > now:
        job.available_at = state.next_allowed_at
        job.status = "queued"
        job.save()
        return None
    if not state.robots_checked_at or state.robots_checked_at < now - timedelta(hours=24):
        state.next_allowed_at = now + timedelta(seconds=source.delay_seconds)
        state.save()
        from classification.routing import before_fetch
        res = fetch(state.origin + "/robots.txt", settings.BOT_USER_AGENT,
                    guard=lambda url: origin(url) == state.origin, max_bytes=512 * 1024, before_attempt=before_fetch(job))
        if res.status in (404, 410):
            state.robots_text = "User-agent: *\nAllow: /"
        else:
            require_success(res)
            if "html" in res.headers.get("content-type", "").lower():
                raise FetchError("robots.txt returned HTML; review source access.", status=403)
            state.robots_text = res.text
        state.robots_checked_at = timezone.now()
        state.save()
        job.available_at = state.next_allowed_at
        job.status = "queued"
        job.save()
        return None
    rules = parse_robots(state)
    delay = max(source.delay_seconds, rules.crawl_delay(settings.BOT_USER_AGENT) or rules.crawl_delay("*") or 0)
    rate = rules.request_rate(settings.BOT_USER_AGENT) or rules.request_rate("*")
    if rate and rate.requests:
        delay = max(delay, rate.seconds / rate.requests)
    state.next_allowed_at = now + timedelta(seconds=delay)
    state.save()
    if not rules.can_fetch(settings.BOT_USER_AGENT, job.url):
        raise FetchError("robots.txt does not allow this page.", status=403)
    return guard_with_robots(source, rules)

def collect_links(source, job, html, base_url):
    """Queue in-scope follow-ups; record and return external links as [(url, label)]."""
    if not source.follow_links and not source.discover_external:
        return []
    from discovery.ranking import link_priority
    remaining = max(0, source.max_pages - job.run.jobs.count())
    seen, follow, external = set(), [], []
    for link in soup_for(html).select("a[href]")[:2000]:
        try:
            url = canonical_url(urljoin(base_url, link["href"]))
        except (ValueError, UnicodeError):
            continue
        if url in seen or len(url) > 1500 or SKIP_EXTENSIONS.search(urlsplit(url).path):
            continue
        seen.add(url)
        # Avoid unbounded calendars/search, logouts, add-to-cart and tracking links.
        path = urlsplit(url).path.lower()
        if any(part in path for part in ("logout", "signout", "add-to-cart", "/search", "/login", "/signin")):
            continue
        if in_scope(source, url):
            if source.follow_links and remaining and job.depth < source.max_depth:
                follow.append((-link_priority(url, link.get_text(" ", strip=True)[:200]), len(follow), url))
        elif source.discover_external and origin(url) != origin(source.url) and len(seen) <= 300:
            # Preserve the actual useful path without fetching or approving it.
            candidate = url
            if not Source.objects.filter(url=candidate).exists():
                label = link.get_text(" ", strip=True)[:200]
                SourceCandidate.objects.get_or_create(url=candidate, defaults={
                    "label": label, "discovered_from": source, "evidence_url": base_url,
                })
                external.append((candidate, label))
    # Team/profile/contact links claim the page allowance first; ties keep document order.
    for _, _, url in sorted(follow):
        if not remaining:
            break
        _, created = PageJob.objects.get_or_create(run=job.run, url=url, defaults={"depth": job.depth + 1})
        remaining -= int(created)
    return external

def retry_delay(attempts, header):
    seconds = min(3600, 30 * (2 ** max(0, attempts - 1)))
    if header:
        try:
            requested = float(header)
        except (ValueError, TypeError):
            try:
                requested = (parsedate_to_datetime(header) - timezone.now()).total_seconds()
            except (ValueError, TypeError, OverflowError):
                requested = 0
        seconds = max(seconds, requested)
    return max(1, min(seconds, 86400))

def finish_run(run):
    if run.jobs.filter(status__in=("queued", "processing")).exists():
        return
    failures = run.jobs.filter(status="failed").count()
    Run.objects.filter(pk=run.pk).exclude(status="cancelled").update(
        status="failed" if failures else "completed", finished_at=timezone.now(),
        message=f"{failures} page(s) failed; see page details." if failures else "Collection finished.")

def handle_error(job, source, exc):
    job.refresh_from_db()
    job.attempts += 1
    job.message = str(exc)[:1500]
    retryable = isinstance(exc, FetchError) and exc.retryable
    blocked = isinstance(exc, FetchError) and exc.status in (401, 403)
    if blocked:
        job.status = "queued"
        job.available_at = timezone.now() + timedelta(hours=1)
        pause_source(source, job.message)
    elif retryable and job.attempts < 4:
        job.status = "queued"
        job.available_at = timezone.now() + timedelta(seconds=retry_delay(job.attempts, exc.retry_after))
        DomainState.objects.filter(origin=origin(source.url)).update(next_allowed_at=job.available_at)
    else:
        job.status = "failed"
        Source.objects.filter(pk=source.pk).update(last_error=job.message)
    job.save()
    finish_run(job.run)

def process(job):
    source = job.run.source
    if source.setup_mode == "automatic":
        job.status, job.message = "skipped", "Automatic setup owns this source; use its campaign."
        job.save()
        finish_run(job.run)
        return
    try:
        if source.collector == "demo":
            from discovery.services import DEMO_ORIGIN, demo_response
            if source.url.startswith(DEMO_ORIGIN + "/"):
                response = demo_response(job.url)
                require_success(response)
                html = response.text
            else:
                html = (Path(settings.BASE_DIR) / "examples" / "demo-team.html").read_text()
                response = Response(source.url, 200, {"content-type": "text/html"}, html.encode())
        else:
            guard = prepare_domain(source, job)
            if guard is None:
                return
            collector = fetch_browser if source.collector == "browser" else fetch
            response = collector(job.url, settings.BOT_USER_AGENT, guard)
            require_success(response)
            if not any(t in response.headers.get("content-type", "").lower() for t in ("text/html", "application/xhtml")):
                job.status = "skipped"
                job.message = "Not an HTML page."
                job.save()
                finish_run(job.run)
                return
            html = response.text
            challenge = html.lower()
            if any(marker in challenge for marker in ("cf-chl-", "verify you are human", "g-recaptcha", "hcaptcha-container")):
                raise FetchError("Access challenge detected; source paused for review.", status=403)
        from classification.evidence import capture_if_enabled
        capture_if_enabled(source, response)
        content_hash = hashlib.sha256(response.body).hexdigest()
        sig = signature(source)
        cached = PageSnapshot.objects.filter(source=source, url=response.url,
                   content_hash=content_hash, extraction_signature=sig).exists()
        diagnostics = {}
        records, company_tags = (None, None) if cached else extract(html, source, diagnostics=diagnostics)
        with transaction.atomic():
            # A pause takes effect between pages. In-flight pages may still be saved.
            count = touch_unchanged(source, response.url) if cached else save_records(
                source, response.url, content_hash, records, company_tags)
            PageSnapshot.objects.update_or_create(source=source, url=response.url, defaults={
                "content_hash": content_hash, "extraction_signature": sig, "last_checked": timezone.now(),
            })
            external = collect_links(source, job, html, response.url)
            job.status = "done"
            job.message = f"{count} contact(s)." + (" Content unchanged." if cached else "")
            if not count:
                job.message += " No validated contacts; review page suitability and CSS recipe."
            if diagnostics:
                job.message += " " + diagnostic_message(diagnostics)
            job.save()
            Run.objects.filter(pk=job.run_id).update(pages_done=F("pages_done") + 1, contacts_seen=F("contacts_seen") + count)
        finish_run(job.run)
        if external:
            try:
                from discovery.services import bridge_collector_links
                bridge_collector_links(source, external, response.url)
            except Exception as exc:  # the hand-off must never fail an already-saved collection page
                logger.warning("Collector link hand-off for page %s: %s", job.pk, exc)
    except Exception as exc:
        logger.warning("Page %s: %s", job.pk, exc)
        handle_error(job, source, exc)

def tick(token, prefer_discovery=False, prefer_classification=False):
    from discovery import services as discovery
    from automation import services as automation
    from classification import services as classification
    heartbeat(token)
    schedule_due()
    discovery.schedule_due()
    automation.maintenance()
    if prefer_classification:
        from classification.evidence import purge_expired
        purge_expired()
        if classification.tick(token):
            heartbeat(token)
            return True
    if prefer_discovery and automation.tick(token):
        heartbeat(token)
        return True
    if prefer_discovery and discovery.tick():
        heartbeat(token)
        return True
    with transaction.atomic():
        job = PageJob.objects.select_related("run__source").filter(status="queued", available_at__lte=timezone.now(),
            run__status__in=("queued", "running"), run__source__active=True, run__source__approved=True).order_by("available_at", "id").first()
        if job:
            job.status = "processing"
            job.save()
            Run.objects.filter(pk=job.run_id, status="queued").update(status="running", started_at=timezone.now())
    if job:
        process(job)
    else:
        worked = discovery.tick() or automation.tick(token) or classification.tick(token)
        heartbeat(token)
        return worked
    heartbeat(token)
    return True
