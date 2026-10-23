"""Canonical-origin onboarding for policy-created sources.

Policy-created sources keep an exact origin: ``in_scope`` never lets them follow a redirect to
another origin. Many sites redirect every request from ``example.com`` to ``www.example.com``
(or http to https), so setup failed closed before seeing a single page.

This module lets setup *rebind* the source, not widen it. When the starting page's first
redirect leaves the exact origin for the same site (``leads.services.network.same_site``:
http->https and/or one leading ``www.``; never a downgrade, another subdomain, another port or
credentials) and the destination path is inside the policy's allowed paths, the source's URL is
replaced with the canonical URL, the job starts a new generation with a fresh scope hash, and
probing restarts there. Every later request is checked against the new exact origin again,
including DNS, robots, scope, policy snapshot and lease/generation checks. Anything else keeps
the existing fail-closed behavior. Rebinds are counted and capped, and always judged against
the URL the policy originally authorized, so a chain cannot walk away from the approved site.
"""
import re
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

from leads.models import Source
from leads.services.network import canonical_url, in_scope, origin, same_site

MAX_REBINDS = 2
SCOPE_ERROR = re.compile(r"outside the approved scope: (\S+)")


def bare(host):
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def site_origins(url):
    """The origins same_site() treats as one website, for duplicate and dismissal checks."""
    host = bare(urlsplit(canonical_url(url)).hostname)
    return [f"{scheme}://{prefix}{host}" for scheme in ("http", "https") for prefix in ("", "www.")]


def redirect_target(message):
    match = SCOPE_ERROR.search(message or "")
    return match.group(1).rstrip(".,;)") if match else ""


def classify(authorized_url, target_url, *, allowed_paths, allow_homepage):
    """Classify a redirect destination relative to the URL the policy authorized.

    canonical                   same site, destination path inside the policy scope: rebindable
    canonical_path_out_of_scope same site, but the path is outside the allowed paths
    same_origin                 not a cross-origin redirect (robots or path rejection)
    downgrade                   https -> http on the same site
    other_subdomain             a different subdomain of the same parent host
    cross_domain                a different website
    invalid                     credentials, non-standard port, unparseable
    """
    if not target_url:
        return "unparsed"
    try:
        raw = urlsplit(target_url)
        if raw.username is not None or raw.password is not None:
            return "invalid"
        target = canonical_url(target_url)
        source_origin, target_origin = origin(authorized_url), origin(target)
    except (ValueError, UnicodeError):
        return "invalid"
    if source_origin == target_origin:
        return "same_origin"
    a, b = urlsplit(source_origin), urlsplit(target_origin)
    if bare(a.hostname) == bare(b.hostname) and a.scheme == "https" and b.scheme == "http":
        return "downgrade"
    if same_site(source_origin, target_origin):
        scope = Source(url=target, allowed_paths=allowed_paths, allow_homepage=allow_homepage)
        return "canonical" if in_scope(scope, target) else "canonical_path_out_of_scope"
    ha, hb = bare(a.hostname), bare(b.hostname)
    if hb.endswith("." + ha) or ha.endswith("." + hb):
        return "other_subdomain"
    return "cross_domain"


def rebind(job, page, target_url):
    """Rebind a policy-created source to its same-site canonical URL and restart probing.

    Returns "rebound", "conflict" (a canonical duplicate needs operator review) or None when the
    redirect is not eligible, in which case the caller keeps its normal fail-closed handling.
    """
    from .models import ProbePage, SiteAutomationJob, SitePolicy
    from .policy import denied, same_setup, scope_hash
    from .services import transition
    with transaction.atomic():
        job = SiteAutomationJob.objects.select_for_update().select_related("source", "campaign").get(pk=job.pk)
        source = Source.objects.select_for_update().get(pk=job.source_id)
        policy = SitePolicy.objects.filter(campaign_id=job.campaign_id, enabled=True).first()
        if (job.generation != page.generation or job.state != "probing" or page.phase != "probe" or page.depth
                or page.url != source.url or source.approval_kind != "policy" or not source.approved
                or not policy or not same_setup(policy, job) or scope_hash(source) != job.scope_hash
                or job.canonical_rebinds >= MAX_REBINDS):
            return None
        authorized_url = job.original_url or source.url
        if classify(authorized_url, target_url, allowed_paths=source.allowed_paths,
                    allow_homepage=source.allow_homepage) != "canonical":
            return None
        new_url = canonical_url(target_url)
        if denied(policy, new_url):
            return None
        variants = site_origins(authorized_url)
        campaign = job.campaign
        if (campaign.urls.filter(origin__in=variants, dismissal_scope="origin").exists()
                or campaign.urls.filter(url=new_url, dismissal_scope="url").exists()):
            return None
        new_origin = origin(new_url)
        if (Source.objects.filter(url__startswith=new_origin + "/").exclude(pk=source.pk).exists()
                or SiteAutomationJob.objects.filter(source__url__startswith=new_origin + "/").exclude(pk=job.pk).exists()):
            return "conflict"
        previous = source.url
        source.url, source.name = new_url, urlsplit(new_url).hostname
        source.approval_notes = (source.approval_notes + f" Setup rebound {origin(previous)} -> {new_origin} "
                                 "(same site: https/www canonical form).")[:2000]
        source.save(update_fields=["url", "name", "approval_notes"])
        job.original_url = job.original_url or previous
        job.canonical_rebinds += 1
        job.generation += 1
        job.scope_hash = scope_hash(source)
        job.recon, job.available_at = {}, timezone.now()
        job.save(update_fields=["original_url", "canonical_rebinds", "generation", "scope_hash", "recon",
                                "available_at", "updated_at"])
        ProbePage.objects.get_or_create(job=job, generation=job.generation, phase="probe", url=new_url)
        transition(job, "probe_queued", f"Canonical origin: {origin(previous)} redirects to {new_origin} "
                                        "(same site). Source rebound; probing restarts under the exact new origin.")
        return "rebound"
