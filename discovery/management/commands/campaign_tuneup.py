"""Yield-focus tune-up for one campaign. Dry run unless --apply. Makes no network requests.

Each step is opt-in and reversible, and none touches the automation policy, so no setup job is
paused:

  --block-banks        add host:bank plus the given bank domains to campaign exclusions and
                       auto-filter matching pending URLs (approved ones stop queueing via ranking).
  --queries FILE       replace the exact search queries (max 10) and turn generated queries off.
  --pause-zero-yield   deactivate campaign sources whose origin has --min-fetched+ fetched pages
                       and no contacts, plus any --pause-host; add domain: exclusions for them
                       and skip their queued work.
  --clear-blocked      skip queued discovery/collector jobs on origins of access-blocked sources.

Output is JSON with before/after values so every change can be reverted by hand. It prints no
contact values, page content or credentials.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from discovery.models import Campaign, DiscoveredURL, DiscoveryJob
from discovery.ranking import exclusion_matches, lines
from discovery.yields import origin_yields
from leads.models import PageJob, Source
from leads.services.network import origin

DEFAULT_BANKS = ("pnc.com", "truist.com", "mtb.com", "wellsfargo.com", "firstmerchants.com", "chase.com",
                 "bankofamerica.com", "usbank.com", "citi.com", "regions.com", "fifththird.com",
                 "huntington.com", "keybank.com", "td.com", "capitalone.com")
TUNEUP_NOTE = "campaign_tuneup"


def merged(existing, additions, limit=3000):
    current = lines(existing)
    out = current + [rule for rule in additions if rule not in current]
    text = "\n".join(out)
    if len(text) > limit:
        raise CommandError(f"Exclusions would exceed {limit} characters; trim them first.")
    return text, [rule for rule in out if rule not in current]


class Command(BaseCommand):
    help = "Bank exclusions, rep-focused queries, zero-yield source pausing and blocked-job cleanup. Dry run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, required=True)
        parser.add_argument("--block-banks", action="store_true")
        parser.add_argument("--bank-domain", action="append", default=[],
                            help="Extra bank domain to exclude (repeatable).")
        parser.add_argument("--queries", help="File with one exact search query per line (max 10).")
        parser.add_argument("--pause-zero-yield", action="store_true")
        parser.add_argument("--min-fetched", type=int, default=50)
        parser.add_argument("--pause-host", action="append", default=[],
                            help="Also pause campaign sources on this host (repeatable).")
        parser.add_argument("--clear-blocked", action="store_true")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        campaign = Campaign.objects.filter(pk=opts["campaign"]).first()
        if not campaign:
            raise CommandError("No such campaign.")
        if not any(opts[k] for k in ("block_banks", "queries", "pause_zero_yield", "clear_blocked")) and not opts["pause_host"]:
            raise CommandError("Choose at least one step.")
        apply = opts["apply"]
        report = {"campaign": campaign.pk, "applied": apply, "before": {
            "exclusions": campaign.exclusions, "search_queries": campaign.search_queries,
            "auto_search_queries": campaign.auto_search_queries}}
        with transaction.atomic():
            campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
            additions = []
            if opts["block_banks"]:
                report["banks"] = self.banks(campaign, opts["bank_domain"], additions, apply)
            if opts["pause_zero_yield"] or opts["pause_host"]:
                report["paused_sources"] = self.pause(campaign, opts, additions, apply)
            if additions:
                text, added = merged(campaign.exclusions, additions)
                report["exclusions_added"] = added
                campaign.exclusions = text
            if opts["queries"]:
                queries = [q.strip() for q in Path(opts["queries"]).read_text().splitlines() if q.strip()]
                if not queries or len(queries) > 10 or any(len(q) > 600 for q in queries):
                    raise CommandError("Provide 1-10 queries of at most 600 characters.")
                report["queries"] = queries
                campaign.search_queries, campaign.auto_search_queries = "\n".join(queries), False
                campaign.search_query_cursor = 0
            if opts["clear_blocked"]:
                report["blocked_jobs"] = self.clear_blocked(campaign, apply)
            if apply:
                campaign.save(update_fields=["exclusions", "search_queries", "auto_search_queries",
                                             "search_query_cursor"])
            else:
                transaction.set_rollback(True)
        report["after"] = {"exclusions": campaign.exclusions, "search_queries": campaign.search_queries,
                           "auto_search_queries": campaign.auto_search_queries}
        self.stdout.write(json.dumps(report, indent=2, default=str))

    def banks(self, campaign, extra, additions, apply):
        rules = ["host:bank"] + [f"domain:{d.lower().strip()}" for d in (*DEFAULT_BANKS, *extra) if d.strip()]
        additions.extend(rules)
        matched = {"pending": [], "approved": []}
        rows = campaign.urls.filter(archived_at__isnull=True, decision__in=("pending", "approved"))
        for item in rows.only("pk", "url", "decision", "reasons", "label", "context"):
            hit = next((r for r in rules if exclusion_matches(r, item.url, "", "")), None)
            if not hit:
                continue
            matched[item.decision].append({"id": item.pk, "url": item.url[:200], "rule": hit})
            if apply and item.decision == "pending":
                DiscoveredURL.objects.filter(pk=item.pk).update(
                    decision="dismissed", reasons=list(item.reasons) + [f"Filtered automatically: excluded by {hit}"])
        # Approved bank URLs stay approved for the record, but rank() now scores them -100,
        # so queue_candidate never queues them again.
        return {"pending_dismissed" if apply else "pending_would_dismiss": matched["pending"],
                "approved_now_unqueueable": matched["approved"]}

    def pause(self, campaign, opts, additions, apply):
        hosts = {h.lower().strip().removeprefix("www.") for h in opts["pause_host"] if h.strip()}
        sources = list(campaign.sources.filter(active=True))
        yields = origin_yields({origin(s.url) for s in sources})
        paused = []
        for source in sources:
            site = origin(source.url)
            host = site.split("://", 1)[-1].removeprefix("www.")
            history = yields.get(site)
            zero = bool(opts["pause_zero_yield"] and history and history.fetched >= opts["min_fetched"]
                        and not history.productive)
            if not (zero or host in hosts):
                continue
            paused.append({"source": source.pk, "url": source.url, "fetched": getattr(history, "fetched", 0),
                           "reason": "zero_yield" if zero else "operator_host"})
            additions.append(f"domain:{host}")
            if apply:
                note = f"\n[{TUNEUP_NOTE} {timezone.now():%Y-%m-%d}] paused: zero yield / marketplace."
                Source.objects.filter(pk=source.pk).update(active=False,
                    approval_notes=(source.approval_notes + note)[-5000:])
                DiscoveryJob.objects.filter(source=source, status="queued").update(
                    status="skipped", message="Source paused by campaign_tuneup; no request made.")
                PageJob.objects.filter(run__source=source, status="queued").update(
                    status="skipped", message="Source paused by campaign_tuneup; no request made.")
        return paused

    def clear_blocked(self, campaign, apply):
        blocked = {origin(s.url) for s in Source.objects.filter(blocked_at__isnull=False)}
        jobs = []
        for job in DiscoveryJob.objects.filter(run__campaign=campaign, status="queued").exclude(kind="search"):
            try:
                site = origin(job.url)
            except Exception:
                continue
            if site in blocked:
                jobs.append({"kind": "discovery", "id": job.pk, "origin": site})
        for job in PageJob.objects.filter(status="queued").select_related("run__source"):
            try:
                site = origin(job.url)
            except Exception:
                continue
            if site in blocked:
                jobs.append({"kind": "collector", "id": job.pk, "origin": site})
        if apply:
            message = "Origin blocked automated access; no request made."
            DiscoveryJob.objects.filter(pk__in=[j["id"] for j in jobs if j["kind"] == "discovery"]).update(
                status="skipped", message=message)
            PageJob.objects.filter(pk__in=[j["id"] for j in jobs if j["kind"] == "collector"]).update(
                status="skipped", message=message)
        return jobs
