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
from leads.models import DomainState, Lead, PageSnapshot, Source
from leads.services.extraction import diagnostic_message, extract, signature, soup_for
from leads.services.network import FetchError, Response, fetch, in_scope, origin, require_success
from leads.services.storage import save_records, touch_unchanged
from .models import Campaign, DailyUsage, DiscoveredURL, DiscoveryJob, DiscoveryRun
from .providers import BRAVE_ORIGIN, MAX_SITEMAP_BYTES, brave_search, search_ready, sitemap_entries
from .ranking import (canonical_exact_start, clean_url, current_candidate_score, ordinary_page_eligible,
                      rank, reviewed_sources)
from .yields import origin_yields as lookup_origin_yields, safe_origin

logger = logging.getLogger(__name__)
OPEN = ("queued", "running", "paused")
DEMO_ORIGIN = "https://discovery.example.test"


def order_discovery_batch(items, *, url, source_id, priority):
    """Order exact payload objects by novelty, then fair origin rotation."""
    items = list(items)
    sourced = [(source_id(item), url(item)) for item in items if source_id(item) is not None]
    fetched = set()
    if sourced:
        source_ids = {item_source_id for item_source_id, _ in sourced}
        urls = {item_url for _, item_url in sourced}
        fetched = set(PageSnapshot.objects.filter(source_id__in=source_ids, url__in=urls)
                      .values_list("source_id", "url"))

    tiers = ([], [])
    for position, item in enumerate(items):
        item_url = url(item)
        item_source_id = source_id(item)
        previously_fetched = item_source_id is not None and (item_source_id, item_url) in fetched
        tiers[int(previously_fetched)].append((position, item, item_url))

    ordered = []
    for tier in tiers:
        by_origin = {}
        for position, item, item_url in tier:
            by_origin.setdefault(origin(item_url), []).append((position, item))
        groups = [(item_origin, sorted(group, key=lambda row: (-priority(row[1]), row[0])))
                  for item_origin, group in by_origin.items()]
        groups.sort(key=lambda bucket: (-priority(bucket[1][0][1]), bucket[1][0][0], bucket[0]))
        sorted_groups = [group for _, group in groups]
        for offset in range(max((len(group) for group in sorted_groups), default=0)):
            ordered.extend(group[offset][1] for group in sorted_groups if offset < len(group))
    return ordered


def tomorrow():
    return datetime.combine(timezone.now().date() + timedelta(days=1), time.min, tzinfo=dt_timezone.utc)


def approved_source(campaign, url):
    return next((source for source in campaign.sources.filter(approved=True).order_by("id") if in_scope(source, url)), None)


def hinted_source(campaign, url, source_hint):
    """Use a caller's exact source only when its current campaign scope still authorizes the URL."""
    if source_hint is not None:
        source_id = source_hint.pk if isinstance(source_hint, Source) else source_hint
        source = campaign.sources.filter(pk=source_id, approved=True).first()
        if source and in_scope(source, url):
            return source
    return approved_source(campaign, url)


def refresh_priorities(campaign, reviewed_source_ids=None, origin_yields=None):
    """Re-score saved URLs without changing operator approvals or dismissals."""
    candidates = list(campaign.urls.select_related("source")[:10000])
    if reviewed_source_ids is None:
        reviewed_source_ids = reviewed_sources(candidate.source_id for candidate in candidates if candidate.source_id)
    if origin_yields is None:
        origin_yields = lookup_origin_yields(candidate.origin for candidate in candidates)
    changed = []
    for candidate in candidates:
        score, reasons = current_candidate_score(
            campaign, candidate.url, candidate.label, candidate.context, candidate.source,
            reviewed_source_ids=reviewed_source_ids, origin_yields=origin_yields,
            explicit_start=(candidate.method == "seed" and candidate.source is not None and
                            canonical_exact_start(candidate.url, candidate.source.url)))
        if (candidate.score, candidate.reasons) != (score, reasons):
            candidate.score, candidate.reasons = score, reasons
            changed.append(candidate)
    if changed:
        DiscoveredURL.objects.bulk_update(changed, ["score", "reasons"], batch_size=200)
    return candidates


def _source_for_url(sources, source_map, url, source_hint=None):
    if source_hint is not None:
        source_id = source_hint.pk if isinstance(source_hint, Source) else source_hint
        source = source_map.get(source_id)
        if source and in_scope(source, url):
            return source
    return next((source for source in sources if in_scope(source, url)), None)


def _queue_state(run):
    keys = set(run.jobs.exclude(kind="search").values_list("kind", "url"))
    return {"count": len(keys), "keys": keys, "collection_allowed": {}}


@transaction.atomic
def register_batch(run, specs, *, cleaned=False, campaign=None, order=False,
                   approved_sources=None, reviewed_source_ids=None):
    """Register an ordered batch while holding one campaign capacity lock."""
    prepared = []
    for original in specs:
        spec = dict(original)
        try:
            spec["url"] = spec["url"] if cleaned else clean_url(spec["url"])
        except (KeyError, TypeError, ValueError, UnicodeError):
            prepared.append((spec, None))
            continue
        prepared.append((spec, spec["url"]))
    urls = {url for _, url in prepared if url is not None}
    if campaign is None:
        campaign = Campaign.objects.select_for_update().get(pk=run.campaign_id)
    else:
        if campaign.pk != run.campaign_id:
            raise ValueError("Run and campaign must match.")
    run_state = DiscoveryRun.objects.get(pk=run.pk)
    run_state.campaign = campaign
    sources = (list(approved_sources) if approved_sources is not None else
               list(campaign.sources.filter(approved=True).order_by("id")))
    source_map = {source.pk: source for source in sources}
    if order:
        valid = []
        invalid = []
        for spec, url in prepared:
            if url is None:
                invalid.append((spec, url))
                continue
            source = _source_for_url(sources, source_map, url, spec.get("source_hint"))
            spec["source_hint"] = source.pk if source else None
            valid.append((spec, url))
        prepared = [
            (spec, spec["url"]) for spec in order_discovery_batch(
                [spec for spec, _ in valid], url=lambda item: item["url"],
                source_id=lambda item: item.get("source_hint"),
                priority=lambda item: item.get("priority", 0))
        ] + invalid
    if reviewed_source_ids is None:
        reviewed_source_ids = reviewed_sources(source_map)
    origin_yields = lookup_origin_yields(safe_origin(url) for url in urls)
    candidates = {candidate.url: candidate for candidate in
                  campaign.urls.filter(url__in=urls).select_related("source")}
    inventory_count = campaign.urls.count()
    origin_rows = list(campaign.urls.values_list("origin", "dismissal_scope"))
    known_origins = {item_origin for item_origin, _ in origin_rows}
    dismissed_origins = {item_origin for item_origin, scope in origin_rows if scope == "origin"}
    queue_state = _queue_state(run_state)
    results = []
    duplicate_count = candidate_count = new_domain_count = 0
    inventory_full = False
    pending = []

    for spec, url in prepared:
        if url is None:
            results.append(None)
            continue
        label = spec.get("label", "")
        context = spec.get("context", "")
        found_on = spec.get("found_on", "")
        method = spec.get("method", "link")
        query = spec.get("query", "")
        depth = spec.get("depth", 0)
        kind = spec.get("kind", "page")
        seed = spec.get("seed", False)
        company_job = spec.get("company_job")
        job_priority = spec.get("job_priority")
        source = _source_for_url(sources, source_map, url, spec.get("source_hint"))
        exact_seed = bool(seed and source and canonical_exact_start(url, source.url))
        score, reasons = current_candidate_score(
            campaign, url, label, context, source, reviewed_source_ids=reviewed_source_ids,
            explicit_start=exact_seed, origin_yields=origin_yields)
        manual = bool(company_job and not in_scope(company_job.source, url))
        if manual:
            from classification.models import Lineage
            if (Lineage.objects.filter(company_job=company_job, relation="external_proposal").count() >= 5 or
                    Lineage.objects.filter(company_job__pilot=company_job.pilot,
                                           relation__in=("external_proposal", "domain_proposal")).count() >= 25):
                results.append(None)
                continue

        candidate = candidates.get(url)
        preserve_existing_approval = bool(candidate and candidate.decision == "approved")
        item_origin = origin(url)
        if source and not preserve_existing_approval and item_origin in dismissed_origins:
            results.append(candidate)
            continue
        if candidate:
            duplicate_count += 1
            candidate.last_seen = timezone.now()
            candidate.score, candidate.reasons = score, reasons
            if label or context:
                candidate.label, candidate.context = str(label)[:200], str(context)[:600]
            if found_on or query:
                candidate.found_on, candidate.search_query = found_on[:1500], query[:600]
                candidate.method = method
            elif seed:
                candidate.method = method
            if kind == "sitemap":
                candidate.kind = kind
            candidate.save()
        else:
            if inventory_count >= campaign.max_candidates:
                inventory_full = True
                results.append(None)
                continue
            new_domain = not source and item_origin not in known_origins
            if new_domain and run_state.new_domains + new_domain_count >= campaign.max_new_domains:
                results.append(None)
                continue
            candidate = DiscoveredURL.objects.create(
                campaign=campaign, first_run=run_state, url=url, origin=item_origin,
                label=str(label)[:200], context=str(context)[:600], found_on=found_on[:1500], method=method,
                search_query=query[:600], score=score, reasons=reasons, kind=kind,
                decision="dismissed" if score < 0 else ("approved" if source else "pending"), source=source)
            candidates[url] = candidate
            known_origins.add(item_origin)
            inventory_count += 1
            candidate_count += 1
            new_domain_count += int(new_domain)
        if manual and not preserve_existing_approval:
            candidate.manual_review_required = True
            if candidate.decision != "dismissed":
                candidate.decision = "pending"
            candidate.save(update_fields=["manual_review_required", "decision"])
            from classification.models import Lineage
            parent = company_job.lineage.first()
            if parent:
                Lineage.objects.get_or_create(company_job=company_job, document=parent.document, url=url,
                                              relation="external_proposal")
        if candidate.manual_review_required or manual:
            results.append(candidate)
            continue
        if candidate.decision != "dismissed" and source:
            if candidate.source_id != source.pk or candidate.decision != "approved":
                candidate.source, candidate.decision = source, "approved"
                candidate.save(update_fields=["source", "decision"])
            queue_candidate(
                run_state, candidate, depth=depth, kind=candidate.kind, seed=seed, company_job=company_job,
                reviewed_source_ids=reviewed_source_ids, job_priority=job_priority, capacity=queue_state,
                origin_yields=origin_yields)
        elif candidate.decision == "pending":
            pending.append(candidate)
        results.append(candidate)

    updates = {}
    if duplicate_count:
        updates["duplicates_seen"] = F("duplicates_seen") + duplicate_count
    if candidate_count:
        updates["candidates_found"] = F("candidates_found") + candidate_count
    if new_domain_count:
        updates["new_domains"] = F("new_domains") + new_domain_count
    if inventory_full:
        updates["message"] = "Saved URL limit reached; raise the campaign limit to expand further."
    if updates:
        DiscoveryRun.objects.filter(pk=run.pk).update(**updates)
    if pending:
        from automation.policy import consider
        for candidate in pending:
            consider(candidate)
            candidate.refresh_from_db()
    return results


@transaction.atomic
def register(run, raw_url, *, label="", context="", found_on="", method="link", query="", depth=0, kind="page", seed=False, company_job=None,
             job_priority=None, source_hint=None):
    """Record one exact URL through the shared batch registration path."""
    return register_batch(run, [{
        "url": raw_url, "label": label, "context": context, "found_on": found_on, "method": method,
        "query": query, "depth": depth, "kind": kind, "seed": seed, "company_job": company_job,
        "job_priority": job_priority, "source_hint": source_hint,
    }])[0]


def queue_candidate(run, candidate, *, depth=0, kind="page", seed=False, company_job=None,
                    reviewed_source_ids=None, job_priority=None, capacity=None, origin_yields=None):
    from automation.policy import collection_allowed
    campaign = run.campaign
    if candidate.manual_review_required:
        return
    if company_job:
        from classification.routing import can_queue
        if not can_queue(company_job, candidate.url, depth):
            return
    if run.status not in ("queued", "running") or candidate.decision != "approved" or not candidate.source_id:
        return
    exact_start = bool(candidate.source and canonical_exact_start(candidate.url, candidate.source.url))
    eligible, score, reasons = ordinary_page_eligible(
        campaign, candidate.url, candidate.label, candidate.context, candidate.source,
        reviewed_source_ids=reviewed_source_ids, origin_yields=origin_yields,
        explicit_start=(candidate.method == "seed" and exact_start))
    if (candidate.score, candidate.reasons) != (score, reasons):
        candidate.score, candidate.reasons = score, reasons
        candidate.save(update_fields=["score", "reasons"])
    if score < 0 or (kind == "page" and not exact_start and not eligible):
        return
    allowed = None if capacity is None else capacity["collection_allowed"].get(candidate.source_id)
    if allowed is None:
        allowed = collection_allowed(candidate.source)
        if capacity is not None:
            capacity["collection_allowed"][candidate.source_id] = allowed
    if not allowed:
        return
    if depth > (3 if kind == "sitemap" else campaign.max_depth):
        return
    if capacity is None:
        if run.jobs.exclude(kind="search").count() >= campaign.max_pages:
            return
    elif capacity["count"] >= campaign.max_pages:
        return
    key = (kind, candidate.url)
    if capacity is not None and key in capacity["keys"]:
        return
    _, created = DiscoveryJob.objects.get_or_create(run=run, kind=kind, url=candidate.url, defaults={
        "candidate": candidate, "source": candidate.source, "depth": depth, "company_job": company_job,
        "priority": job_priority if job_priority is not None else (95 if kind == "sitemap" else score)})
    if capacity is not None:
        capacity["keys"].add(key)
        capacity["count"] += int(created)


COLLECTOR_IMPORT_LIMIT = 200


def collector_link_specs(sources, since=None):
    """Recent unreviewed external links the regular collector found on these approved sources."""
    from leads.models import SourceCandidate
    from .yields import window_days
    since = since or timezone.now() - timedelta(days=window_days())
    rows = (SourceCandidate.objects.filter(status="new", discovered_from__in=sources, created_at__gte=since)
            .order_by("-created_at")[:COLLECTOR_IMPORT_LIMIT])
    return [{"url": row.url, "label": row.label, "context": "", "found_on": row.evidence_url,
             "method": "collector", "depth": 1, "kind": "page"} for row in rows]


def bridge_collector_links(source, links, found_on):
    """Hand external links from regular collection to each active campaign's open run.

    ``links`` is [(url, label)]. Registration uses the ordinary batch path, so scoring,
    exclusions, pending review for new domains, policy authorization and every queueing
    gate apply exactly as for links found by discovery itself. Nothing is fetched here.
    Returns the number of campaigns that received the links.
    """
    if not links:
        return 0
    specs = [{"url": url, "label": label, "context": "", "found_on": found_on, "method": "collector",
              "depth": 1, "kind": "page"} for url, label in links]
    handed = 0
    for campaign_id in (Campaign.objects.filter(active=True, sources=source, sources__approved=True)
                        .values_list("pk", flat=True).distinct()):
        with transaction.atomic():
            campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
            run = campaign.runs.filter(status__in=("queued", "running")).first()
            if not campaign.active or not run:
                continue  # the next start() imports recent collector links
            register_batch(run, specs, campaign=campaign)
            handed += 1
    return handed


@transaction.atomic
def queue_activated_source(source, campaign):
    """Queue a newly active source's approved URLs into the campaign's open run.

    Policy authorization approves in-scope URLs before setup finishes, while
    collection is still gated, so queue_candidate skips them. Without this
    handoff they would wait for the campaign's next scheduled run. Every
    ordinary gate still applies through queue_candidate: approval, score and
    direct intent, collection_allowed, depth, per-run page limit and dedup.
    URLs the canary just checked for this source are skipped to save budget.
    Returns the number of jobs created.
    """
    campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
    run = campaign.runs.filter(status__in=("queued", "running")).first()
    if not campaign.active or not run:
        return 0  # a paused or finished campaign picks these up on its next start()
    run.campaign = campaign
    checked = set(PageSnapshot.objects.filter(source=source).values_list("url", flat=True))
    candidates = [candidate for candidate in
                  campaign.urls.filter(source=source, decision="approved", manual_review_required=False)
                  .select_related("source")
                  if candidate.url not in checked and in_scope(source, candidate.url) and
                  (candidate.kind != "sitemap" or campaign.use_sitemaps)]
    if not candidates:
        return 0
    queue_state = _queue_state(run)
    before = queue_state["count"]
    reviewed_source_ids = reviewed_sources({source.pk})
    origin_yields = lookup_origin_yields({origin(source.url)})
    for candidate in order_discovery_batch(candidates, url=lambda item: item.url,
                                           source_id=lambda item: item.source_id,
                                           priority=lambda item: item.score):
        if queue_state["count"] >= campaign.max_pages:
            break
        queue_candidate(run, candidate, kind=candidate.kind, reviewed_source_ids=reviewed_source_ids,
                        capacity=queue_state, origin_yields=origin_yields)
    return queue_state["count"] - before


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
    sources = list(campaign.sources.filter(approved=True).order_by("id"))
    reviewed_source_ids = reviewed_sources(source.pk for source in sources)
    seed_specs = []
    for source in sources:
        seed_specs.append({"url": source.url, "source_id": source.pk, "priority": 100,
                           "label": source.name, "context": source.company, "found_on": "",
                           "method": "seed", "kind": "page"})
        if campaign.use_sitemaps:
            sitemap = origin(source.url) + "/sitemap.xml"
            if in_scope(source, sitemap):
                seed_specs.append({"url": sitemap, "source_id": source.pk, "priority": 95,
                                   "label": "", "context": "", "found_on": source.url,
                                   "method": "sitemap", "kind": "sitemap"})
    for spec in seed_specs:
        spec.update(seed=True, job_priority=spec["priority"], source_hint=spec.pop("source_id"))
    register_batch(run, seed_specs, campaign=campaign, order=True, approved_sources=sources,
                   reviewed_source_ids=reviewed_source_ids)
    # External links the regular collector found on these sources since they were last reviewed.
    collector_specs = collector_link_specs(sources)
    if collector_specs:
        register_batch(run, collector_specs, campaign=campaign, approved_sources=sources,
                       reviewed_source_ids=reviewed_source_ids)
    # Reviewed deeper paths remain useful even if their original directory disappears.
    saved = refresh_priorities(campaign, reviewed_source_ids=reviewed_source_ids)
    origin_yields = lookup_origin_yields(candidate.origin for candidate in saved)
    remembered = [candidate for candidate in saved
                  if candidate.decision == "approved" and candidate.method != "seed"]
    selected_ids = set(source.pk for source in sources)
    remembered_in_scope = []
    for candidate in remembered:
        if candidate.source_id in selected_ids and in_scope(candidate.source, candidate.url):
            if candidate.kind != "sitemap" or campaign.use_sitemaps:
                remembered_in_scope.append(candidate)
    queue_state = _queue_state(run)
    for candidate in order_discovery_batch(
            remembered_in_scope, url=lambda item: item.url, source_id=lambda item: item.source_id,
            priority=lambda item: item.score):
        if queue_state["count"] >= campaign.max_pages:
            break
        queue_candidate(run, candidate, kind=candidate.kind, reviewed_source_ids=reviewed_source_ids,
                        capacity=queue_state, origin_yields=origin_yields)
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
    if source.setup_mode == "automatic":
        from automation.services import start_setup
        start_setup(source, candidate.campaign)
    # Approve the selected URL even if explicitly dismissed earlier. Other dismissals persist.
    candidate.decision, candidate.source = "approved", source
    candidate.dismissal_scope = ""
    candidate.manual_review_required = False
    candidate.save(update_fields=["decision", "source", "dismissal_scope", "manual_review_required"])
    run = candidate.campaign.runs.filter(status__in=("queued", "running")).first()
    active = bool(run and candidate.campaign.active)
    reviewed_source_ids = reviewed_sources({source.pk}) if active else None
    origin_yields = lookup_origin_yields({origin(source.url)}) if active else None
    for sibling in candidate.campaign.urls.filter(origin=origin(source.url), manual_review_required=False).exclude(decision="dismissed"):
        if in_scope(source, sibling.url):
            sibling.decision, sibling.source = "approved", source
            sibling.save(update_fields=["decision", "source"])
            if run and candidate.campaign.active:
                queue_candidate(run, sibling, kind=sibling.kind, reviewed_source_ids=reviewed_source_ids,
                                origin_yields=origin_yields)


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
    return reserve_budget(job.run.campaign, job, search=search)


@transaction.atomic
def reserve_budget(campaign, job, search=False):
    """Collection setup and discovery consume the same persisted request allowance."""
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
        links.append({"priority": score, "url": url,
                      "label": label, "context": context, "found_on": base_url,
                      "depth": job.depth + 1, "company_job": job.company_job})
    register_batch(job.run, links, cleaned=True, order=True)


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
    from classification.evidence import capture_if_enabled
    capture_if_enabled(source, response, company_job=job.company_job)
    content_hash = hashlib.sha256(response.body).hexdigest()
    sig = signature(source)
    cached = PageSnapshot.objects.filter(source=source, url=response.url, content_hash=content_hash, extraction_signature=sig).exists()
    diagnostics = {}
    if source.setup_mode == "automatic":
        from automation.services import monitor_page
        validated = monitor_page(source, response)
        if validated is None:
            done(job, "Source health requires recipe setup; previous contact evidence retained.", "skipped")
            return
        records, tags, metrics = validated
        cached = False
    else:
        records, tags = (None, None) if cached else extract(response.text, source, diagnostics=diagnostics)
    with transaction.atomic():
        if source.setup_mode == "automatic":
            from automation.policy import collection_allowed
            source.refresh_from_db()
            if not collection_allowed(source):
                done(job, "Source setup changed while fetching; no contacts stored.", "skipped")
                return
        before = Lead.objects.count()
        count = touch_unchanged(source, response.url) if cached else save_records(source, response.url, content_hash, records, tags)
        created = Lead.objects.count() - before
        PageSnapshot.objects.update_or_create(source=source, url=response.url, defaults={
            "content_hash": content_hash, "extraction_signature": sig, "last_checked": timezone.now()})
        record_links(job, response.text, response.url)
        DiscoveryRun.objects.filter(pk=job.run_id).update(pages_done=F("pages_done") + 1, contacts_seen=F("contacts_seen") + count, new_contacts=F("new_contacts") + created)
        job.contacts_seen, job.new_contacts = count, created
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
        campaign.refresh_from_db()
        if job.company_job_id:
            from classification.models import JevControl
            if not job.company_job.pilot.active or JevControl.objects.filter(pk='jev', paused=True).exists():
                defer(job, timezone.now() + timedelta(seconds=60), 'Company pilot/Jev is paused; queued work retained.')
                return
        if job.kind == "search":
            if not campaign.active or not campaign.search_enabled:
                done(job, "Campaign or search was disabled after this job was claimed; no search made.", "skipped")
                return
            if not search_ready():
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
            results = []
            for result in brave_search(job.url):
                if not isinstance(result, dict) or not isinstance(result.get("url"), str):
                    continue
                try:
                    result_url = clean_url(result["url"])
                except (ValueError, UnicodeError):
                    continue
                raw_title = result.get("title", "")
                raw_description = result.get("description", "")
                label = soup_for(raw_title if isinstance(raw_title, str) else "").get_text(" ", strip=True)
                context = soup_for(raw_description if isinstance(raw_description, str) else "").get_text(" ", strip=True)
                results.append({"url": result_url, "label": label, "context": context,
                                "query": job.url, "method": "search",
                                "priority": rank(campaign, result_url, label, context)[0]})
            register_batch(job.run, results, cleaned=True, order=True)
            done(job, "Search results saved; new domains await source review.")
            return
        source = Source.objects.filter(pk=job.source_id).first() if job.source_id else None
        if not source or not source.approved or not campaign.sources.filter(pk=source.pk).exists() or not in_scope(source, job.url):
            done(job, "Source approval or scope changed; no request made.", "skipped")
            return
        job.source = source
        from automation.policy import collection_allowed
        if not collection_allowed(source):
            done(job, "Source awaits recipe setup or health review; no request made.", "skipped")
            return
        if job.candidate:
            job.candidate.refresh_from_db()
        if not job.candidate or job.candidate.decision != "approved":
            done(job, "URL was dismissed or is awaiting review.", "skipped")
            return
        exact_start = canonical_exact_start(job.url, source.url)
        eligible, score, reasons = ordinary_page_eligible(
            campaign, job.url, job.candidate.label, job.candidate.context, source,
            explicit_start=(job.candidate.method == "seed" and exact_start))
        if (job.candidate.score, job.candidate.reasons) != (score, reasons):
            DiscoveredURL.objects.filter(pk=job.candidate_id).update(score=score, reasons=reasons)
            job.candidate.score, job.candidate.reasons = score, reasons
        if score < 0:
            done(job, "URL excluded by current campaign filters; no request made.", "skipped")
            return
        if job.kind == "page" and not exact_start and not eligible:
            done(job, "URL no longer meets the current score and direct contact-page intent gate; no request made.", "skipped")
            return
        if source.collector == "demo":
            from classification.fixtures import DEMO_ORIGIN as JEV_DEMO_ORIGIN, demo_response as jev_demo
            from automation.fixtures import DEMO_ORIGIN as AUTO_DEMO_ORIGIN, demo_response as automation_demo
            if origin(source.url) == JEV_DEMO_ORIGIN:
                response = jev_demo(job.url)
            else:
                response = automation_demo(source, job.url) if origin(source.url) == AUTO_DEMO_ORIGIN else demo_response(job.url)
            if job.company_job_id:
                from classification.routing import reserve_fetch
                reserve_fetch(job.company_job_id, job.url)
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
            if campaign.use_sitemaps and not job.company_job_id:
                state = DomainState.objects.get(origin=origin(source.url))
                for sitemap in (parse_robots(state).site_maps() or [])[:20]:
                    if in_scope(source, sitemap):
                        register(job.run, sitemap, found_on=state.origin + "/robots.txt", method="sitemap",
                                 kind="sitemap", seed=True, source_hint=source)
            # Discovery is intentionally HTML-first. Existing browser sources can still
            # be collected separately, using the collector's browser option.
            from classification.routing import before_fetch
            response = fetch(job.url, settings.BOT_USER_AGENT, guard, before_attempt=before_fetch(job),
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
                    clean = clean_url(url)
                except (ValueError, UnicodeError):
                    continue
                entries.append({"kind": kind, "url": clean,
                                "priority": 95 if kind == "sitemap" else rank(campaign, clean)[0],
                                "source_id": source.pk if in_scope(source, clean) else None})
            for entry in entries:
                entry.update(found_on=response.url, method="sitemap",
                             depth=job.depth + 1 if entry["kind"] == "sitemap" else 1,
                             company_job=job.company_job, source_hint=entry.pop("source_id"))
            register_batch(job.run, entries, cleaned=True, order=True)
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
