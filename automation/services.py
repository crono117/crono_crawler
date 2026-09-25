"""Restartable site setup stages owned by the existing collector lease."""
import hashlib
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from discovery.ranking import rank
from leads.models import DomainState, PageSnapshot, Source, WorkerLease
from leads.services.extraction import signature, validate_recipe
from leads.services.network import FetchError, fetch, in_scope, origin, require_success
from leads.services.storage import save_records
from .models import AutomationEvent, ProbePage, RecipeVersion, SiteAutomationJob, SitePolicy
from .policy import authorized, scope_hash, snapshot
from .private_pages import MAX_BYTES, purge_expired, read_html, write_html
from .recipes import PROBE_VERSION, evaluate, proposals, readiness, recon_page

RUNNABLE = ("probe_queued", "probing", "probe_complete", "recipe_queued", "recipe_testing", "recipe_ready", "recipe_released", "canary")


def transition(job, state, message):
    job.state, job.message = state, message[:1000]
    job.attempts = 0
    job.save(update_fields=["state", "message", "attempts", "updated_at"])
    AutomationEvent.objects.create(job=job, state=state, message=job.message)


@transaction.atomic
def start_setup(source, campaign):
    policy = SitePolicy.objects.get(campaign=campaign)
    if not source.approved or not policy.enabled:
        raise ValueError("Source authorization and an enabled campaign policy are required.")
    if not campaign.sources.filter(pk=source.pk).exists():
        raise ValueError("The source must belong to this campaign.")
    source.active, source.setup_mode = False, "automatic"
    source.save(update_fields=["active", "setup_mode"])
    from leads.models import Run
    Run.objects.filter(source=source, status__in=("queued", "running", "paused")).update(status="cancelled", message="Automatic recipe setup owns this source.", finished_at=timezone.now())
    job, created = SiteAutomationJob.objects.get_or_create(source=source, defaults={
        "campaign": campaign, "scope_hash": scope_hash(source), "policy_snapshot": snapshot(policy)})
    if not created and job.state in RUNNABLE and job.scope_hash == scope_hash(source) and job.policy_snapshot == snapshot(policy):
        return job
    if not created:
        job.generation += 1
        job.campaign = campaign
        job.scope_hash, job.policy_snapshot = scope_hash(source), snapshot(policy)
        job.recon, job.processing, job.available_at = {}, False, timezone.now()
        job.save()
    ProbePage.objects.get_or_create(job=job, generation=job.generation, phase="probe", url=source.url)
    transition(job, "probe_queued", "Source setup queued; ordinary collection is paused.")
    return job


@transaction.atomic
def pause_job(job, message, *, rollback=True, failed=False):
    job.refresh_from_db()
    if rollback and job.current_recipe_id:
        version = job.current_recipe
        previous = job.last_good_recipe
        if previous and previous.pk == version.pk:
            previous = job.recipes.filter(status="known_good").exclude(pk=version.pk).first()
        version.status = "paused"
        version.save(update_fields=["status"])
        # Do not overwrite a recipe the operator edited during an in-flight fetch.
        Source.objects.filter(pk=job.source_id, recipe=version.recipe).update(recipe=previous.recipe if previous else {})
        job.current_recipe, job.last_good_recipe = previous, previous
        job.save(update_fields=["current_recipe", "last_good_recipe"])
    Source.objects.filter(pk=job.source_id).update(active=False, last_error=message[:1000])
    transition(job, "failed" if failed else "paused", message)


def refresh_authorization(job):
    generation = getattr(job, "_worker_generation", job.generation)
    if getattr(job, "_worker_token", None) and not WorkerLease.objects.filter(key="collector", token=job._worker_token).exists():
        return False
    job.refresh_from_db()
    if job.generation != generation:
        return False
    job.source.refresh_from_db()
    job.campaign.refresh_from_db()
    if job.state not in RUNNABLE or not authorized(job):
        if job.state in RUNNABLE:
            pause_job(job, "Source scope, campaign policy or authorization changed. Review setup before resuming.", rollback=False)
        return False
    return True


def defer_page(page, message, until):
    page.status, page.message, page.available_at = "queued", message[:1000], until
    page.save(update_fields=["status", "message", "available_at"])


def fetch_probe_page(job, page):
    from leads.services.worker import prepare_domain, parse_robots, retry_delay
    from discovery.services import reserve_budget
    source = job.source
    if not in_scope(source, page.url) or rank(job.campaign, page.url)[0] < 0 or job.campaign.urls.filter(url=page.url, decision="dismissed").exists():
        page.status, page.message = "skipped", "Outside source scope or excluded by campaign."
        page.save(update_fields=["status", "message"])
        return
    try:
        if source.collector == "demo":
            from .fixtures import demo_response
            response = demo_response(source, page.url)
            DomainState.objects.get_or_create(origin=origin(source.url), defaults={
                "robots_text": "User-agent: *\nAllow: /", "robots_checked_at": timezone.now()})
        else:
            state = DomainState.objects.filter(origin=origin(source.url)).first()
            if state and state.next_allowed_at > timezone.now():
                defer_page(page, "Waiting for the origin's request delay.", state.next_allowed_at)
                return
            if not reserve_budget(job.campaign, page):
                return
            guard = prepare_domain(source, page)
            if guard is None:
                return
            from classification.routing import before_fetch
            response = fetch(page.url, settings.BOT_USER_AGENT, guard, max_bytes=MAX_BYTES, before_attempt=before_fetch(page))
        require_success(response)
        if any(marker in response.text.lower() for marker in ("cf-chl-", "verify you are human", "g-recaptcha", "hcaptcha-container", "performing security verification")):
            raise FetchError("Access challenge; site setup paused.", status=403)
        if "html" not in response.headers.get("content-type", "").lower():
            page.status, page.message = "skipped", "Not an HTML page."
            page.save(update_fields=["status", "message"])
            return
        if not refresh_authorization(job):
            return
        metadata, links = recon_page(response, source, job.campaign)
        if any(marker in response.text.lower() for marker in ("automated access is prohibited", "scraping is prohibited", "do not scrape")):
            raise FetchError("An explicit collection restriction needs operator review.", status=403)
        from classification.evidence import capture_if_enabled
        capture_if_enabled(source, response, company_job=job.company_job)
        state = DomainState.objects.get(origin=origin(source.url))
        metadata["robots"] = {"allowed": True, "policy_hash": hashlib.sha256(state.robots_text.encode()).hexdigest(),
                              "sitemaps": (parse_robots(state).site_maps() or [])[:20]}
        with transaction.atomic():
            if not refresh_authorization(job):
                return
            page.metadata, page.expires_at = metadata, timezone.now() + timedelta(hours=24)
            page.private_key = write_html(page, response.text)
            page.status, page.message = "done", "Saved private HTML for local validation; recon contains metadata only."
            page.save()
            limit = job.policy_snapshot["probe_pages" if page.phase == "probe" else "canary_pages"]
            if page.depth < 1:
                for url in links:
                    if job.pages.filter(generation=job.generation, phase=page.phase).count() >= limit:
                        break
                    ProbePage.objects.get_or_create(job=job, generation=job.generation, phase=page.phase, url=url, defaults={"depth": 1})
    except Exception as exc:
        if not refresh_authorization(job):
            return
        page.refresh_from_db()
        page.attempts += 1
        page.save(update_fields=["attempts"])
        if isinstance(exc, FetchError) and exc.status in (401, 403):
            page.metadata = {"url": page.url, "result": "page_blocked_or_unavailable", "status": exc.status}
            page.save(update_fields=["metadata"])
            pause_job(job, str(exc))
        elif isinstance(exc, FetchError) and exc.retryable and page.attempts < 4:
            until = timezone.now() + timedelta(seconds=retry_delay(page.attempts, exc.retry_after))
            defer_page(page, str(exc), until)
            DomainState.objects.filter(origin=origin(source.url)).update(next_allowed_at=until)
        else:
            page.status, page.message = "failed", str(exc)[:1000]
            page.save(update_fields=["status", "message"])
            pause_job(job, "Page unavailable: " + str(exc), failed=True)


def pages_for(job, phase):
    return job.pages.filter(generation=job.generation, phase=phase)


def build_recon(job):
    pages = list(pages_for(job, "probe").filter(status="done"))
    job.recon = {"schema_version": 1, "source_id": job.source_id, "origin": origin(job.source.url),
                 "scope": job.source.allowed_paths.splitlines(), "allow_homepage": job.source.allow_homepage,
                 "scope_hash": job.scope_hash, "probe_version": PROBE_VERSION, "generation": job.generation,
                 "pages": [p.metadata for p in pages], "page_count": len(pages),
                 "response_bytes": sum(p.metadata.get("response_bytes", 0) for p in pages)}
    job.save(update_fields=["recon"])
    if not pages:
        pause_job(job, "No available HTML pages in the approved scope.")
    else:
        transition(job, "recipe_queued", "Recon ready; deterministic recipe candidates will be tested locally.")


def validate_candidates(job):
    samples = [(page, read_html(page)) for page in pages_for(job, "probe").filter(status="done")]
    recipes = proposals([html for _, html in samples])
    if job.source.recipe:
        recipes.insert(0, job.source.recipe)
    # Authenticated coordinator proposals still pass exactly the same local validator.
    recipes = [r.recipe for r in job.recipes.filter(generation=job.generation, status="proposed")] + recipes
    results, seen = [], set()
    import json
    for recipe in recipes[:65]:
        key = json.dumps(recipe, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        page_stats = {page.metadata["url"]: evaluate(html, job.source, recipe)[2] for page, html in samples}
        score = readiness(list(page_stats.values()))
        accepted = sum(s["accepted_records"] for s in page_stats.values())
        matched = sum(s["matched_cards"] for s in page_stats.values())
        results.append((score, accepted, -matched, recipe, page_stats))
    results.sort(key=lambda result: result[:3], reverse=True)
    with transaction.atomic():
        if not refresh_authorization(job):
            return
        start_version = (job.recipes.aggregate(last=Max("version"))["last"] or 0) + 1
        best = None
        for number, (score, accepted, _, recipe, stats) in enumerate(results[:5], start_version):
            version = RecipeVersion.objects.create(source=job.source, job=job, version=number, generation=job.generation,
                recipe=recipe, score=score, scope_hash=job.scope_hash,
                validation={"pages": stats, "accepted_records": accepted,
                    "matched_cards": sum(s["matched_cards"] for s in stats.values()),
                    "rejected_records": sum(s["rejected_records"] for s in stats.values()),
                    "content_hashes": {p.metadata["url"]: p.metadata["content_hash"] for p, _ in samples}})
            best = best or version
        if best and best.score >= job.policy_snapshot["min_recipe_score"] and best.validation["accepted_records"]:
            job.current_recipe = best
            job.save(update_fields=["current_recipe"])
            transition(job, "recipe_ready", f"Recipe v{best.version} passed local checks with readiness {best.score}; release gate pending.")
        else:
            outcomes = sorted({s["result"] for r in results[:1] for s in r[4].values()})
            pause_job(job, "Recipe review needed: " + (", ".join(outcomes) or "no matching cards") + ". No contacts were stored.", rollback=False)


@transaction.atomic
def release_recipe(job):
    if not refresh_authorization(job):
        return
    version = job.current_recipe
    if not version or version.scope_hash != job.scope_hash or version.generation != job.generation or version.score < job.policy_snapshot["min_recipe_score"] or not version.validation.get("accepted_records"):
        pause_job(job, "Recipe did not satisfy the local release gate.", rollback=False)
        return
    try:
        validate_recipe(version.recipe)
    except ValueError as exc:
        pause_job(job, str(exc), rollback=False)
        return
    version.status, version.released_at = "released", timezone.now()
    version.save(update_fields=["status", "released_at"])
    Source.objects.filter(pk=job.source_id).update(recipe=version.recipe, extractor="rules", active=False)
    transition(job, "recipe_released", f"Released recipe v{version.version} to a bounded canary only.")


def seed_canary(job):
    depths = {job.source.url: 0}
    depths.update(pages_for(job, "probe").filter(status="done").values_list("url", "depth"))
    for url, depth in list(depths.items())[:job.policy_snapshot["canary_pages"]]:
        ProbePage.objects.get_or_create(job=job, generation=job.generation, phase="canary", url=url, defaults={"depth": depth})
    transition(job, "canary", "Canary queued: depth 1, no external links or search, shared daily budget.")


def validate_canary(job):
    pages = list(pages_for(job, "canary"))
    if any(page.status != "done" for page in pages):
        pause_job(job, "Canary included unavailable or non-HTML pages; previous recipe retained.")
        return
    prepared = []
    for page in pages:
        records, tags, stats = evaluate(read_html(page), job.source, job.current_recipe.recipe)
        baseline = job.current_recipe.validation["pages"].get(page.metadata["url"])
        if baseline and baseline["accepted_records"] and (not records or len(records) < baseline["accepted_records"] * .5 or
                stats["structure_hash"] != baseline["structure_hash"]):
            pause_job(job, "Canary degraded relative to probe evidence or page structure; recipe rolled back.")
            return
        if page.metadata["robots"]["policy_hash"] not in {p["robots"]["policy_hash"] for p in job.recon["pages"]}:
            pause_job(job, "Robots policy changed during setup; review required.")
            return
        prepared.append((page, records, tags, stats))
    if not prepared or readiness([s for _, _, _, s in prepared]) < job.policy_snapshot["min_recipe_score"] or not any(r for _, r, _, _ in prepared):
        pause_job(job, "Canary evidence did not satisfy the recipe release threshold.")
        return
    with transaction.atomic():
        if not refresh_authorization(job):
            return
        job.source.refresh_from_db()
        for page, records, tags, _ in prepared:
            final_url = page.metadata["url"]
            save_records(job.source, final_url, page.metadata["content_hash"], records, tags)
            PageSnapshot.objects.update_or_create(source=job.source, url=final_url, defaults={
                "content_hash": page.metadata["content_hash"], "extraction_signature": signature(job.source), "last_checked": timezone.now()})
        version = job.current_recipe
        # Keep original validation immutable. Canary observations remain in private-page metadata.
        for page, _, _, stats in prepared:
            page.metadata["validation"] = stats
            page.save(update_fields=["metadata"])
        version.status = "known_good"
        version.save(update_fields=["status"])
        job.last_good_recipe, job.next_probe_at = version, timezone.now() + timedelta(days=job.policy_snapshot["recheck_days"])
        job.save(update_fields=["last_good_recipe", "next_probe_at"])
        Source.objects.filter(pk=job.source_id).update(last_error="")
        transition(job, "active", "Canary passed. Normal discovery collection is enabled within this source's scope.")
        # Hand already-approved URLs to the open discovery run instead of waiting for the next one.
        from discovery.services import queue_activated_source
        queued = queue_activated_source(job.source, job.campaign)
        if queued:
            AutomationEvent.objects.create(job=job, state="active",
                                           message=f"Queued {queued} approved page(s) into the open discovery run.")


def advance(job):
    if not refresh_authorization(job):
        return
    if job.state == "probe_queued":
        transition(job, "probing", "Probing authorized static HTML only.")
    if job.state in ("probing", "canary"):
        phase = "probe" if job.state == "probing" else "canary"
        pending = pages_for(job, phase).filter(status="queued").order_by("available_at", "id").first()
        if pending:
            if pending.available_at <= timezone.now():
                fetch_probe_page(job, pending)
            else:
                job.available_at = pending.available_at
                job.save(update_fields=["available_at"])
            return
        if phase == "probe":
            transition(job, "probe_complete", "Bounded probe finished.")
        else:
            validate_canary(job)
    elif job.state == "probe_complete":
        build_recon(job)
    elif job.state == "recipe_queued":
        transition(job, "recipe_testing", "Testing selector candidates against private local snapshots.")
    elif job.state == "recipe_testing":
        validate_candidates(job)
    elif job.state == "recipe_ready":
        release_recipe(job)
    elif job.state == "recipe_released":
        seed_canary(job)


def tick(token):
    with transaction.atomic():
        job = SiteAutomationJob.objects.select_related("source", "campaign", "current_recipe", "last_good_recipe").filter(
            state__in=RUNNABLE, processing=False, available_at__lte=timezone.now(), campaign__active=True,
            campaign__site_policy__enabled=True).order_by("updated_at", "id").first()
        if not job:
            return False
        job.processing, job.lease_token = True, token
        job.save(update_fields=["processing", "lease_token"])
    try:
        job._worker_token = token
        job._worker_generation = job.generation
        advance(job)
    except Exception as exc:
        if refresh_authorization(job):
            pause_job(job, "Setup could not continue: " + str(exc), failed=True)
    finally:
        SiteAutomationJob.objects.filter(pk=job.pk, lease_token=token).update(processing=False)
    return True


def maintenance():
    from .policy import consider_pending
    purge_expired()
    consider_pending()
    for job in SiteAutomationJob.objects.filter(state="active", next_probe_at__lte=timezone.now(),
            campaign__active=True, campaign__site_policy__enabled=True).select_related("source", "campaign")[:5]:
        start_setup(job.source, job.campaign)


def monitor_page(source, response):
    """Check a normal page before any observation is replaced or inserted."""
    from .policy import collection_allowed
    if not collection_allowed(source):
        return None
    job = SiteAutomationJob.objects.select_related("source", "campaign", "current_recipe").get(source=source)
    records, page_tags, stats = evaluate(response.text, source, job.current_recipe.recipe)
    baseline = job.current_recipe.validation.get("pages", {}).get(response.url)
    robots_hashes = {page.get("robots", {}).get("policy_hash") for page in job.recon.get("pages", [])}
    state = DomainState.objects.filter(origin=origin(source.url)).first()
    if state and hashlib.sha256(state.robots_text.encode()).hexdigest() not in robots_hashes:
        pause_job(job, "Robots policy changed; collection paused for access review.")
        return None
    bad = sum(count for reason, count in stats["primary_rejections"].items()
              if reason not in ("missing_contact", "non_sales_role", "shared_or_global_contact"))
    degraded = bool(bad or stats["row_limit_reached"])
    if baseline and baseline["accepted_records"]:
        degraded |= not records or len(records) < baseline["accepted_records"] * .5 or stats["structure_hash"] != baseline["structure_hash"]
        degraded |= stats["rejected_records"] > max(3, baseline["rejected_records"] * 2)
    if degraded:
        pause_job(job, "Evidence or structure drift detected; rolled back and queued a bounded re-probe.")
        job.source.refresh_from_db()
        start_setup(job.source, job.campaign)
        return None
    return records, page_tags, stats
