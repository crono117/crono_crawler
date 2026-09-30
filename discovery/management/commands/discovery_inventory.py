"""Explain what occupies a campaign's active URL inventory. Read-only unless --apply is given.

No network requests. With --park-low-value --apply, rows that ranking.low_value_page says can
never be queued are archived with turnover.PARKED_REASON; decisions, source bindings, dismissals,
jobs, snapshots and leads are untouched. --enable-parking --apply turns on park_low_value_urls.
"""
import json
import subprocess
from collections import Counter
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.db.models import Count
from django.utils import timezone

from discovery.models import Campaign, DiscoveryJob
from discovery.ranking import canonical_exact_start, direct_contact_page_intent
from discovery.turnover import PARKED_REASON, eligible as turnover_eligible, park, parkable
from leads.models import Lead, Observation, PageSnapshot, WorkerLease
from leads.services.reporting import production_leads


def duplicate_key(url):
    """www/bare host and trailing-slash variants of one page share a key; scheme and query are kept."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    return parts.scheme, host, parts.path.rstrip("/") or "/", parts.query


def checkout():
    base = Path(settings.BASE_DIR)
    try:
        commit = subprocess.run(["git", "-C", str(base), "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    return {"base_dir": str(base), "commit": commit or "unknown"}


def report(campaign):
    now = timezone.now()
    rows = list(campaign.urls.values_list("pk", "url", "label", "kind", "method", "decision", "dismissal_scope",
                                          "archived_at", "archive_reason", "manual_review_required", "source_id",
                                          "source__url", "source__blocked_at", "score", "origin", "last_seen"))
    active = [row for row in rows if row[7] is None and not (row[5] == "dismissed" and not row[6])]
    archived = [row for row in rows if row[7] is not None]

    # Review decisions and inventory state are independent: parking never changes a decision.
    decisions = Counter()
    inventory_state = Counter()
    for row in rows:
        if row[5] == "dismissed":
            decisions["dismissed_operator" if row[6] else "dismissed_auto_filtered"] += 1
        else:
            decisions[row[5]] += 1
        if row[7] is not None:
            inventory_state["archived_parked" if row[8] == PARKED_REASON else "archived_other"] += 1
        elif row[5] == "dismissed" and not row[6]:
            inventory_state["auto_filtered_uncounted"] += 1
        else:
            inventory_state["active_counted"] += 1

    snapshots = set(PageSnapshot.objects.filter(source_id__in={row[10] for row in active if row[10]})
                    .values_list("source_id", "url"))
    evidence = set(Observation.objects.filter(source_id__in={row[10] for row in active if row[10]})
                   .values_list("source_id", "page_url"))
    job_rows = (DiscoveryJob.objects.filter(run__campaign=campaign, candidate__isnull=False)
                .values("candidate_id", "status", "contacts_seen"))
    jobs_by_candidate = {}
    for job in job_rows:
        entry = jobs_by_candidate.setdefault(job["candidate_id"], {"done": 0, "failed": 0, "contacts": 0, "open": 0})
        if job["status"] == "done":
            entry["done"] += 1
        elif job["status"] == "failed":
            entry["failed"] += 1
        elif job["status"] in ("queued", "processing"):
            entry["open"] += 1
        entry["contacts"] += job["contacts_seen"]

    active_approved = Counter()
    fetch_state = Counter()
    no_intent_origins = Counter()
    for pk, url, label, kind, method, decision, _, _, _, manual, source_id, source_url, blocked, score, item_origin, last_seen in active:
        history = jobs_by_candidate.get(pk, {})
        if (source_id, url) in evidence or history.get("contacts"):
            fetch_state["produced_contacts"] += 1
        elif (source_id, url) in snapshots or history.get("done"):
            fetch_state["fetched_no_contacts"] += 1
        elif history.get("failed"):
            fetch_state["failed_only"] += 1
        else:
            fetch_state["never_fetched"] += 1
        if blocked:
            fetch_state["on_blocked_source"] += 1
        if decision != "approved":
            continue
        if kind == "sitemap":
            active_approved["sitemap"] += 1
        elif method == "seed" or (source_url and canonical_exact_start(url, source_url)):
            active_approved["starting_url"] += 1
        elif manual or method == "operator_import":
            active_approved["manual_or_import"] += 1
        elif not direct_contact_page_intent(url, label):
            active_approved["no_direct_intent"] += 1
            no_intent_origins[item_origin] += 1
        elif score < campaign.min_score:
            active_approved["intent_below_threshold"] += 1
        else:
            active_approved["queueable"] += 1

    groups = Counter(duplicate_key(row[1]) for row in active)
    duplicate_rows = sum(count - 1 for count in groups.values() if count > 1)
    parkable_ids = parkable(campaign)
    stale_pending = sum(1 for row in active if row[5] == "pending" and row[15] < now - timedelta(days=7))

    week = now - timedelta(days=7)
    runs = campaign.runs.filter(created_at__gte=week)
    run_summary = {
        "last_7_days": runs.count(),
        "by_status": dict(runs.values_list("status").annotate(n=Count("pk")).order_by()),
        "with_no_jobs": runs.filter(jobs__isnull=True).count(),
        "intake_only": runs.filter(intake_only=True).count(),
        "capacity_message": runs.filter(message__startswith="Active URL limit reached").count(),
        "completed_with_pages_zero_contacts": runs.filter(status="completed", pages_done__gt=0, contacts_seen=0).count(),
    }
    jobs = {f"{kind}:{status}": n for kind, status, n in
            DiscoveryJob.objects.filter(run__campaign=campaign).values_list("kind", "status")
            .annotate(n=Count("pk")).order_by("kind", "status")}
    lease = WorkerLease.objects.filter(key="collector").first()
    real = production_leads().count()
    all_leads = Lead.objects.count()
    counted = len(active)
    return {
        "campaign": {"id": campaign.pk, "name": campaign.name, "active": campaign.active,
                     "max_candidates": campaign.max_candidates, "active_inventory": counted,
                     "over_limit_by": max(0, counted - campaign.max_candidates),
                     "min_score": campaign.min_score, "max_pages": campaign.max_pages,
                     "park_low_value_urls": campaign.park_low_value_urls,
                     "auto_archive_candidates": campaign.auto_archive_candidates,
                     "search_enabled": campaign.search_enabled,
                     "sources": campaign.sources.count(),
                     "blocked_sources": campaign.sources.filter(blocked_at__isnull=False).count()},
        "decisions": dict(decisions),
        "inventory_state": dict(inventory_state),
        "active_approved": dict(active_approved),
        "active_fetch_state": dict(fetch_state),
        "active_canonical_duplicates": duplicate_rows,
        "archived": len(archived),
        "parkable_now": len(parkable_ids),
        "parkable_top_origins": no_intent_origins.most_common(10),
        "turnover_eligible_now": turnover_eligible(campaign).count(),
        "stale_pending": stale_pending,
        "runs": run_summary,
        "jobs": jobs,
        "leads": {"non_synthetic": real, "demo_only": all_leads - real,
                  "basis": "non-synthetic = at least one observation from a non-demo source (Source.collector)"},
        "worker_lease": ({"token_present": bool(lease.token), "heartbeat_at": lease.heartbeat_at,
                          "fresh": bool(lease.token) and lease.heartbeat_at > now - timedelta(seconds=600)}
                         if lease else None),
        "database": str(connection.settings_dict.get("NAME")),
        "checkout": checkout(),
    }


class Command(BaseCommand):
    help = ("Explain what fills a campaign's active URL inventory and why discovery reports it full. "
            "Read-only unless --apply. No network requests.")

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, required=True)
        parser.add_argument("--park-low-value", action="store_true",
                            help="Archive active approved pages that can never pass the direct-intent gate.")
        parser.add_argument("--enable-parking", action="store_true",
                            help="Turn on park_low_value_urls for this campaign.")
        parser.add_argument("--apply", action="store_true", help="Write changes. Without it, only report.")

    def handle(self, *args, **options):
        try:
            campaign = Campaign.objects.get(pk=options["campaign"])
        except Campaign.DoesNotExist as exc:
            raise CommandError("Campaign does not exist.") from exc
        before = report(campaign)
        result = {"before": before}
        if options["park_low_value"] or options["enable_parking"]:
            if not options["apply"]:
                result["dry_run"] = {"would_park": before["parkable_now"],
                                     "would_enable_parking": options["enable_parking"] and not campaign.park_low_value_urls}
            else:
                with transaction.atomic():
                    campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
                    parked = park(campaign, parkable(campaign)) if options["park_low_value"] else 0
                    if options["enable_parking"] and not campaign.park_low_value_urls:
                        campaign.park_low_value_urls = True
                        campaign.save(update_fields=["park_low_value_urls"])
                result["applied"] = {"parked": parked, "parking_enabled": campaign.park_low_value_urls}
                result["after"] = report(campaign)
                if not campaign.park_low_value_urls:
                    result["warning"] = ("park_low_value_urls is off: new in-scope links without contact-page "
                                         "intent will refill active inventory.")
        self.stdout.write(json.dumps(result, cls=DjangoJSONEncoder, indent=2))
