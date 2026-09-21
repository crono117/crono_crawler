"""One campaign authorization policy replaces per-domain clicks when enabled."""
import hashlib
import json
from urllib.parse import urlsplit
from django.db import transaction
from django.utils import timezone
from discovery.ranking import lines, rank
from leads.models import Source
from leads.services.network import in_scope, origin
from .models import DEFAULT_DENY, SiteAutomationJob, SitePolicy

POLICY_FIELDS = ("min_url_score", "min_recipe_score", "allowed_paths", "allow_homepage", "denied_domains",
                 "max_sites_per_day", "probe_pages", "canary_pages", "delay_seconds", "recheck_days", "notes")


def snapshot(policy):
    return {"campaign_id": policy.campaign_id, **{name: getattr(policy, name) for name in POLICY_FIELDS}}


def scope_hash(source):
    fields = (source.url, source.allowed_paths, source.allow_homepage, source.approved, source.category,
              source.collector, source.require_sales_role, source.delay_seconds, source.setup_mode)
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def denied(policy, url):
    host = urlsplit(url).hostname.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain)
               for domain in set(lines(DEFAULT_DENY) + lines(policy.denied_domains)))


def policy_scope(policy, url):
    return Source(url=url, allowed_paths=policy.allowed_paths, allow_homepage=policy.allow_homepage)


def authorized(job):
    policy = SitePolicy.objects.filter(campaign_id=job.campaign_id, enabled=True).first()
    return bool(policy and job.campaign.active and job.source.approved and
                job.campaign.sources.filter(pk=job.source_id).exists() and
                scope_hash(job.source) == job.scope_hash and snapshot(policy) == job.policy_snapshot and
                not denied(policy, job.source.url) and
                not job.campaign.urls.filter(origin=origin(job.source.url), dismissal_scope="origin").exists() and
                not job.campaign.urls.filter(url=job.source.url, dismissal_scope="url").exists())


def collection_allowed(source):
    if source.setup_mode != "automatic":
        return True
    job = SiteAutomationJob.objects.select_related("source", "campaign", "current_recipe").filter(source=source).first()
    return bool(job and job.state == "active" and authorized(job) and job.current_recipe and
                job.current_recipe.status == "known_good" and source.recipe == job.current_recipe.recipe)


@transaction.atomic
def consider(candidate):
    """Authorize only a bounded probe, never normal collection or scope expansion."""
    if candidate.manual_review_required or candidate.decision != "pending" or candidate.kind != "page":
        return None
    campaign = candidate.campaign
    policy = SitePolicy.objects.filter(campaign=campaign, enabled=True).first()
    if not policy or not campaign.active or denied(policy, candidate.url) or urlsplit(candidate.url).query:
        return None
    score, _ = rank(campaign, candidate.url, candidate.label, candidate.context)
    if score < policy.min_url_score or not in_scope(policy_scope(policy, candidate.url), candidate.url):
        return None
    # Preserve unreviewed/operator-created sources and previous explicit dismissals.
    if Source.objects.filter(url__startswith=candidate.origin + "/").exists() or campaign.urls.filter(origin=candidate.origin, dismissal_scope="origin").exists():
        return None
    if SiteAutomationJob.objects.filter(campaign=campaign, source__url__startswith=candidate.origin + "/").exists():
        return None
    if SiteAutomationJob.objects.filter(campaign=campaign, created_at__date=timezone.now().date()).count() >= policy.max_sites_per_day:
        return None
    source = Source.objects.create(name=urlsplit(candidate.url).hostname, url=candidate.url, company="",
        category=campaign.category, approved=True, approval_kind="policy", setup_mode="automatic",
        approval_notes=f"Authorized for scoped automatic setup by campaign {campaign.pk}. Policy note: {policy.notes}",
        allowed_paths=policy.allowed_paths, allow_homepage=policy.allow_homepage, delay_seconds=policy.delay_seconds,
        max_pages=min(10, campaign.max_pages), max_depth=1, follow_links=True, discover_external=False)
    campaign.sources.add(source)
    from .services import start_setup
    job = start_setup(source, campaign)
    for sibling in campaign.urls.filter(origin=origin(source.url), decision="pending", manual_review_required=False):
        if in_scope(source, sibling.url):
            sibling.source, sibling.decision = source, "approved"
            sibling.last_result = f"Policy authorized setup job #{job.pk}; collection waits for a validated recipe and canary."
            sibling.save(update_fields=["source", "decision", "last_result"])
    return job


def consider_pending():
    # Runs without a live discovery run too, so deferred site budgets recover next day.
    from discovery.models import DiscoveredURL
    for policy in SitePolicy.objects.filter(enabled=True, campaign__active=True):
        pending = DiscoveredURL.objects.filter(campaign_id=policy.campaign_id, decision="pending",
            kind="page", manual_review_required=False, score__gte=policy.min_url_score).select_related("campaign").order_by("id")
        candidates = list(pending.filter(id__gt=policy.pending_scan_cursor)[:100])
        if not candidates and policy.pending_scan_cursor:
            candidates = list(pending[:100])
        # Rotate through bounded batches so excluded high scorers cannot starve later sites.
        cursor = candidates[-1].pk if candidates else 0
        SitePolicy.objects.filter(pk=policy.pk).update(pending_scan_cursor=cursor)
        for candidate in sorted(candidates, key=lambda item: (-item.score, item.pk)):
            consider(candidate)
