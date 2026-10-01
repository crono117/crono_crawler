"""Ranked, tiered admission of new sites into automatic setup.

Problem this solves: with ``require_page_intent`` off, whichever generic homepage registered
first took each of the day's setup slots, and none of them converted. Requiring intent instead
starves the queue, because search results rarely land on team pages yet.

With ``SitePolicy.explore_share`` below 100 the daily slots are allocated, not first-come:

* **Tiers.** ``direct`` pages (team/staff/people/agents/reps in the URL path or label) are
  high-confidence. ``company`` pages (about/contact) and ``generic`` pages (a bare homepage)
  are exploratory. At most ``explore_share`` percent of ``max_sites_per_day`` may go to
  exploratory candidates, so the rest stay free for direct pages instead of being spent on
  whatever arrived first.
* **One page per site.** Every pending page of a site competes as that site's setup start,
  so a company that also has an about or team page found elsewhere starts there, not at ``/``.
* **Ranking.** Tier, the existing campaign score, the origin's observed page yield, the
  search query's setup history and the company's earlier durable outcomes decide the order
  (``automation.learning``). Every adjustment is bounded and recorded on the job.
* **Diversity.** An exploratory candidate whose company family already took a slot today waits.
* **Deferred.** Exploratory candidates are never admitted at registration time; only this
  ranked pass admits them, after it has seen the whole pending pool.

Nothing here weakens a gate: ``policy.admission_gate`` (score, exclusions, scope, duplicates,
dismissals) and the probe/recipe/canary pipeline are unchanged, and the daily cap still applies.
It never fetches anything. ``explore_share`` 100 keeps the historical behavior exactly.
"""
import time
from dataclasses import dataclass, field

from django.utils import timezone

from discovery.ranking import company_page_tier, direct_contact_page_intent, rank
from discovery.yields import EMPTY, origin_yields, query_yields, safe_origin, yield_adjustment
from leads.services.network import canonical_url

from . import learning

TIER_BASE = {"direct": 300, "company": 200, "generic": 100}
POOL_LIMIT = 2000
MAX_CONSIDERED = 300
MIN_INTERVAL_SECONDS = 60
_last_pass = {}


def tier_for(url, label=""):
    """direct (team/person page), company (about/contact page) or generic (anything else, e.g. a homepage)."""
    if direct_contact_page_intent(url, label):
        return "direct"
    if company_page_tier(url, label):
        return "company"
    return "generic"


def explore_cap(policy, share=None):
    """Daily slots open to candidates without direct intent (rounded up so a nonzero share is never zero)."""
    share = policy.explore_share if share is None else share
    return (policy.max_sites_per_day * share + 99) // 100


def site_key(url):
    """One key per website: scheme and a leading ``www.`` do not make a different site."""
    from .canonical import bare
    from urllib.parse import urlsplit
    try:
        return bare(urlsplit(canonical_url(url)).hostname)
    except (ValueError, UnicodeError):
        return ""


def jobs_today(campaign):
    from .models import SiteAutomationJob
    return SiteAutomationJob.objects.filter(campaign=campaign, created_at__date=timezone.now().date())


def explore_used(campaign):
    """Setups created today that were not direct-intent. Jobs admitted before ranking existed count too."""
    return jobs_today(campaign).exclude(admission_tier="direct").count()


def families_today(campaign):
    return {learning.site_family(url) for url in jobs_today(campaign).values_list("source__url", flat=True)}


@dataclass
class Ranked:
    candidate: object
    tier: str
    value: int
    family: str
    reasons: list = field(default_factory=list)
    score: int = 0

    @property
    def url(self):
        return self.candidate.url


def rank_pool(policy, candidates):
    """Rank candidates; the caller passes one batch so history is read with a few bounded queries."""
    campaign = policy.campaign
    origins = {safe_origin(candidate.url) for candidate in candidates}
    yields = origin_yields(origins)
    queries = {candidate.search_query for candidate in candidates if candidate.method == "search" and candidate.search_query}
    query_history = learning.merge_yields(query_yields(queries), learning.query_setup_stats(queries)) if queries else {}
    families = learning.family_outcomes()
    ranked = []
    for candidate in candidates:
        tier = tier_for(candidate.url, candidate.label)
        score, _ = rank(campaign, candidate.url, candidate.label, candidate.context)
        value = TIER_BASE[tier] + score
        reasons = [f"{tier} intake tier (+{TIER_BASE[tier]})", f"Campaign score {score}"]
        family = learning.site_family(candidate.url)
        adjustments = [yield_adjustment(yields.get(safe_origin(candidate.url), EMPTY)),
                       learning.query_adjustment(query_history.get(candidate.search_query, EMPTY)
                                                 if candidate.method == "search" else EMPTY),
                       learning.family_adjustment(families.get(family))]
        for delta, reason in adjustments:
            if delta:
                value += delta
                reasons.append(reason)
        ranked.append(Ranked(candidate, tier, value, family, reasons, score))
    return ranked


def taken_sites(campaign):
    """Site keys that already have a source or an operator origin dismissal. Two bulk queries.

    Mirrors the duplicate/dismissal rules of ``policy.admission_gate`` (which stays authoritative).
    """
    from leads.models import Source
    taken = set()
    for url in Source.objects.values_list("url", flat=True):
        key = site_key(url)
        if key:
            taken.add(key)
    for item_origin in campaign.urls.filter(dismissal_scope="origin").values_list("origin", flat=True).distinct():
        key = site_key(item_origin)
        if key:
            taken.add(key)
    return taken


def pending_pool(campaign, limit=POOL_LIMIT):
    return list(campaign.urls.filter(decision="pending", kind="page", manual_review_required=False,
                                     archived_at__isnull=True).order_by("-score", "id")[:limit])


def ranked_sites(policy):
    """The best start page per website, best site first. Same-site pages compete as that site's start."""
    from urllib.parse import urlsplit
    from leads.services.network import in_scope
    from .policy import denied, policy_scope
    campaign = policy.campaign
    taken = taken_sites(campaign)
    best = {}
    for item in rank_pool(policy, pending_pool(campaign)):
        key = site_key(item.url)
        # Pages consider() could never admit must not hide a site's admissible page, nor crowd the
        # bounded ranked window: no usable key, excluded, query string, denied host, a site that
        # already has a source or an origin dismissal, or a path outside the policy's scope.
        if (not key or key in taken or item.score < 0 or urlsplit(item.url).query or denied(policy, item.url)
                or not in_scope(policy_scope(policy, item.url), item.url)):
            continue
        if key not in best or (item.value, -item.candidate.pk) > (best[key].value, -best[key].candidate.pk):
            best[key] = item
    return sorted(best.values(), key=lambda item: (-item.value, item.candidate.pk))


def admit(policy, *, force=False):
    """Spend today's remaining setup slots on the best-ranked sites. Returns the jobs created.

    Runs from the worker's maintenance tick, at most once a minute per policy unless forced.
    ``consider`` re-checks every gate and both caps under the campaign lock; the checks here only
    avoid pointless attempts.
    """
    from .policy import consider
    now = time.monotonic()
    if not force and now - _last_pass.get(policy.pk, -MIN_INTERVAL_SECONDS) < MIN_INTERVAL_SECONDS:
        return []
    _last_pass[policy.pk] = now
    campaign = policy.campaign
    remaining = policy.max_sites_per_day - jobs_today(campaign).count()
    if remaining <= 0:
        return []
    explore_left = explore_cap(policy) - explore_used(campaign)
    created = []
    for item in ranked_sites(policy)[:MAX_CONSIDERED]:
        if len(created) >= remaining:
            break
        if item.tier != "direct" and explore_left <= 0:
            continue
        job = consider(item.candidate, ranked=item)
        if job:
            created.append(job)
            explore_left -= item.tier != "direct"
    return created


def plan(policy, *, explore_share=None, limit=30):
    """Read-only preview of the allocator: pool by tier, slots, and who would be admitted now.

    Applies the same gates, tier caps and diversity rule as a real pass but writes nothing, so an
    operator can size ``explore_share`` from live data before changing it.
    """
    from .policy import admission_gate
    campaign = policy.campaign
    share = policy.explore_share if explore_share is None else explore_share
    ranked = ranked_sites(policy)
    used_today = jobs_today(campaign).count()
    remaining = max(0, policy.max_sites_per_day - used_today)
    explore_left = max(0, explore_cap(policy, share) - explore_used(campaign)) if share < 100 else remaining
    taken = families_today(campaign)
    sites_by_tier, would, blocked = {"direct": 0, "company": 0, "generic": 0}, [], {"gate": 0, "explore_cap": 0, "family": 0}
    for item in ranked:
        if admission_gate(policy, campaign, item.candidate) is None:
            blocked["gate"] += 1
            continue
        sites_by_tier[item.tier] += 1
        if len(would) >= remaining:
            continue
        if share < 100 and item.tier != "direct":
            if explore_left <= 0:
                blocked["explore_cap"] += 1
                continue
            if item.family in taken:
                blocked["family"] += 1
                continue
        would.append(item)
        taken.add(item.family)
        if share < 100 and item.tier != "direct":
            explore_left -= 1
    row = lambda item: {"url": item.url, "tier": item.tier, "rank": item.value, "reasons": item.reasons,
                        "query": item.candidate.search_query if item.candidate.method == "search" else ""}
    return {"campaign": campaign.pk, "explore_share": share, "max_sites_per_day": policy.max_sites_per_day,
            "created_today": used_today, "slots_remaining": remaining,
            "explore_cap": explore_cap(policy, share), "explore_used_today": explore_used(campaign),
            "admissible_sites_by_tier": sites_by_tier, "ranked_sites": len(ranked),
            "not_admissible": blocked, "would_admit": [row(item) for item in would[:limit]],
            "top_ranked": [row(item) for item in ranked[:limit]]}
