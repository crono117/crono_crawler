"""One campaign authorization policy replaces per-domain clicks when enabled."""
import hashlib
import json
from urllib.parse import urlsplit
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from discovery.ranking import direct_contact_page_intent, lines, rank
from leads.models import Source
from leads.services.network import in_scope, origin
from .learning import site_family as learning_family
from .models import DEFAULT_DENY, SiteAutomationJob, SitePolicy

POLICY_FIELDS = ("min_url_score", "min_recipe_score", "allowed_paths", "allow_homepage", "denied_domains",
                 "max_sites_per_day", "probe_pages", "canary_pages", "delay_seconds", "recheck_days", "notes")


# Fields that bound what an admitted site's setup may do. Admission-only fields (min_url_score,
# max_sites_per_day, notes, require_page_intent and explore_share, which were never snapshotted) decide which
# new sites enter setup; changing them must not revoke setups that were already admitted.
SETUP_FIELDS = ("min_recipe_score", "allowed_paths", "allow_homepage", "denied_domains",
                "probe_pages", "canary_pages", "delay_seconds", "recheck_days")
ADMISSION_FIELDS = ("min_url_score", "max_sites_per_day", "require_page_intent", "explore_share")


def snapshot(policy):
    return {"campaign_id": policy.campaign_id, **{name: getattr(policy, name) for name in POLICY_FIELDS}}


def setup_view(snap):
    return {name: snap.get(name) for name in ("campaign_id",) + SETUP_FIELDS}


def same_setup(policy, job):
    """Whether the enabled policy still grants exactly what this job's setup was admitted under."""
    return setup_view(snapshot(policy)) == setup_view(job.policy_snapshot)


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
                scope_hash(job.source) == job.scope_hash and same_setup(policy, job) and
                not denied(policy, job.source.url) and
                not job.campaign.urls.filter(origin=origin(job.source.url), dismissal_scope="origin").exists() and
                not job.campaign.urls.filter(url=job.source.url, dismissal_scope="url").exists())


def collection_allowed(source):
    if (source.recipe or {}).get("engine"):
        from leads.services.extraction import validate_recipe
        try:
            validate_recipe(source.recipe)
        except ValueError:
            return False
    if source.setup_mode != "automatic":
        return True
    job = SiteAutomationJob.objects.select_related("source", "campaign", "current_recipe").filter(source=source).first()
    return bool(job and job.state == "active" and authorized(job) and job.current_recipe and
                job.current_recipe.status == "known_good" and source.recipe == job.current_recipe.recipe)


def admission_gate(policy, campaign, candidate):
    """Whether this candidate may enter setup, ignoring daily limits. Writes nothing.

    Returns the live campaign score, or None. Used by ``consider`` and by the read-only intake
    preview, so both always agree on what is admissible.
    """
    if candidate.archived_at or candidate.manual_review_required or candidate.decision != "pending" or candidate.kind != "page":
        return None
    if not policy or not campaign.active or denied(policy, candidate.url) or urlsplit(candidate.url).query:
        return None
    score, _ = rank(campaign, candidate.url, candidate.label, candidate.context)
    if policy.require_page_intent:
        if (score < policy.min_url_score or not direct_contact_page_intent(candidate.url, candidate.label) or
                not in_scope(policy_scope(policy, candidate.url), candidate.url)):
            return None
    elif score < 0 or not in_scope(policy_scope(policy, candidate.url), candidate.url):
        # Intent wording is advisory in this mode, but an explicit exclusion/negative score
        # is never optional, and scope/robots enforcement is never optional either.
        return None
    # Preserve unreviewed/operator-created sources and previous explicit dismissals. The same
    # website under its http/https and www/bare forms counts as one site here: a source or setup
    # job for any form blocks a duplicate, and an origin dismissal of any form is honored.
    from .canonical import site_origins
    variants = site_origins(candidate.origin)
    same_site_sources = Q()
    for variant in variants:
        same_site_sources |= Q(url__startswith=variant + "/")
    if Source.objects.filter(same_site_sources).exists() or campaign.urls.filter(origin__in=variants, dismissal_scope="origin").exists():
        return None
    if SiteAutomationJob.objects.filter(campaign=campaign, source__in=Source.objects.filter(same_site_sources)).exists():
        return None
    return score


@transaction.atomic
def consider(candidate, ranked=None):
    """Authorize only a bounded probe, never normal collection or scope expansion.

    ``ranked`` is the intake allocator's ranking of this candidate. Without it (registration time,
    operator actions) a policy with ``explore_share`` below 100 admits only direct-intent pages;
    exploratory ones wait for the ranked pass. Every admission records its tier, query and rank.
    """
    from discovery.models import Campaign, DiscoveredURL
    # Share the import/register lock and reload review state after acquiring it.
    campaign = Campaign.objects.select_for_update().get(pk=candidate.campaign_id)
    candidate = DiscoveredURL.objects.get(pk=candidate.pk)
    candidate.campaign = campaign
    policy = SitePolicy.objects.filter(campaign=campaign, enabled=True).first()
    score = admission_gate(policy, campaign, candidate)
    if score is None:
        return None
    if jobs_created_today(campaign) >= policy.max_sites_per_day:
        return None
    from . import intake
    tier = ranked.tier if ranked else intake.tier_for(candidate.url, candidate.label)
    if policy.explore_share < 100 and tier != "direct":
        # Slots are reserved for direct-intent pages; exploratory ones are bounded and de-duplicated per company.
        if (ranked is None or intake.explore_used(campaign) >= intake.explore_cap(policy)
                or learning_family(candidate.url) in intake.families_today(campaign)):
            return None
    source = Source.objects.create(name=urlsplit(candidate.url).hostname, url=candidate.url, company="",
        category=campaign.category, approved=True, approval_kind="policy", setup_mode="automatic",
        approval_notes=f"Authorized for scoped automatic setup by campaign {campaign.pk}. Policy note: {policy.notes}",
        allowed_paths=policy.allowed_paths, allow_homepage=policy.allow_homepage, delay_seconds=policy.delay_seconds,
        max_pages=min(10, campaign.max_pages), max_depth=1, follow_links=True, discover_external=False)
    campaign.sources.add(source)
    from .services import start_setup
    job = start_setup(source, campaign)
    job.admission_tier, job.admission_method = tier, candidate.method[:20]
    job.admission_query = candidate.search_query[:600] if candidate.method == "search" else ""
    job.admission_rank = ranked.value if ranked else None
    job.admission_reasons = ranked.reasons if ranked else [f"First-eligible admission ({tier} tier)"]
    job.save(update_fields=["admission_tier", "admission_method", "admission_query", "admission_rank", "admission_reasons"])
    from leads.services.activity import emit
    emit("approved", f"Policy approved {candidate.url} (score {score}, {tier} tier) -> site setup #{job.pk}", lane="approval",
         url=candidate.url, by="policy")
    for sibling in campaign.urls.filter(origin=origin(source.url), decision="pending", manual_review_required=False,
                                       archived_at__isnull=True):
        if in_scope(source, sibling.url):
            sibling.source, sibling.decision = source, "approved"
            sibling.last_result = f"Policy authorized setup job #{job.pk}; collection waits for a validated recipe and canary."
            sibling.save(update_fields=["source", "decision", "last_result"])
    return job


def jobs_created_today(campaign):
    return SiteAutomationJob.objects.filter(campaign=campaign, created_at__date=timezone.now().date()).count()


def consider_pending():
    # Runs without a live discovery run too, so deferred site budgets recover next day.
    from discovery.models import DiscoveredURL
    for policy in SitePolicy.objects.filter(enabled=True, campaign__active=True).select_related("campaign"):
        if policy.explore_share < 100:
            # Ranked, tiered allocation of the daily slots (see automation.intake); the legacy scan
            # below remains the behavior at explore_share 100.
            from .intake import admit
            admit(policy)
            continue
        # Must mirror consider()'s own gate: min_url_score only applies when page intent is
        # required, otherwise only an explicit exclusion/negative score is disqualifying.
        floor = policy.min_url_score if policy.require_page_intent else 0
        pending = DiscoveredURL.objects.filter(campaign_id=policy.campaign_id, decision="pending",
            kind="page", manual_review_required=False, archived_at__isnull=True, score__gte=floor).select_related("campaign").order_by("id")
        candidates = list(pending.filter(id__gt=policy.pending_scan_cursor)[:100])
        if not candidates and policy.pending_scan_cursor:
            candidates = list(pending[:100])
        # Rotate through bounded batches so excluded high scorers cannot starve later sites.
        cursor = candidates[-1].pk if candidates else 0
        SitePolicy.objects.filter(pk=policy.pk).update(pending_scan_cursor=cursor)
        for candidate in sorted(candidates, key=lambda item: (-item.score, item.pk)):
            consider(candidate)
