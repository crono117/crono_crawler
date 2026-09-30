#!/usr/bin/env python3
"""Inventory diagnosis and low-value parking against a temporary synthetic database.

Run with .venv/bin/python scripts/smoke_inventory.py. Builds a fictional campaign at the size of a
full real pilot (53 sources, 3,000-URL limit), then checks the read-only report, the dry run, the
applied parking, intake behavior and query cost. No network requests; no real database is touched.
The row mix is synthetic: it proves mechanics and cost, not what a real inventory contains.
"""
import io
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(condition, message):
    if not condition:
        raise SystemExit("FAIL: " + message)


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix="lead-console-inventory-") as directory:
        os.environ.update(DATA_DIR=directory, DATABASE_URL="", DJANGO_DEBUG="1", ACTIVITY_LOG="off",
                          DJANGO_SECRET_KEY=secrets.token_urlsafe(48), DJANGO_SETTINGS_MODULE="config.settings",
                          JEV_MODE="off", JEV_CAPTURE_ENABLED="0", JEV_ROUTING_ENABLED="0",
                          BRAVE_SEARCH_ENABLED="0", OLLAMA_MODEL="", EXTRACTION_PACKS_ENABLED="0")
        import django
        django.setup()
        from django.core.management import call_command
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from django.utils import timezone
        from discovery.models import Campaign, DiscoveredURL, DiscoveryRun
        from discovery.services import intake_due
        from leads.models import Lead, Observation, Source, SourceCandidate

        call_command("migrate", interactive=False, verbosity=0)
        campaign = Campaign.objects.create(name="Synthetic pilot", max_candidates=3000, min_score=10, active=True)
        sources = [Source.objects.create(name=f"Fixture {i}", url=f"https://site{i}.example/", approved=True,
                                         discover_external=True, blocked_at=timezone.now() if i < 6 else None)
                   for i in range(53)]
        campaign.sources.add(*sources)
        rows = []
        for i, source in enumerate(sources):
            base = source.url.rstrip("/")
            rows.append(DiscoveredURL(campaign=campaign, source=source, url=source.url, origin=base,
                                      method="seed", decision="approved", score=90))
            rows.append(DiscoveredURL(campaign=campaign, source=source, url=base + "/sitemap.xml", origin=base,
                                      method="sitemap", kind="sitemap", decision="approved", score=95))
            for j in range(60):
                path = f"/team/person-{j}/" if j < 4 else f"/merchant-services/page-{j}/"
                rows.append(DiscoveredURL(campaign=campaign, source=source, url=base + path, origin=base,
                                          method="sitemap", decision="approved", score=25))
        rows = rows[:2936]
        rows += [DiscoveredURL(campaign=campaign, url=f"https://pending{i}.example/team/",
                               origin=f"https://pending{i}.example", decision="pending", score=35) for i in range(75)]
        rows += [DiscoveredURL(campaign=campaign, url=f"https://noise{i}.example/share", origin=f"https://noise{i}.example",
                               decision="dismissed", score=0) for i in range(1100)]
        rows += [DiscoveredURL(campaign=campaign, url=f"https://gone{i}.example/team/", origin=f"https://gone{i}.example",
                               decision="dismissed", dismissal_scope="origin", score=35) for i in range(13)]
        DiscoveredURL.objects.bulk_create(rows, batch_size=500)
        demo = Source.objects.create(name="Demo", url="https://demo.example.test/", approved=True, collector="demo")
        for i in range(15):
            lead = Lead.objects.create(identity=f"fixture-{i}", name=f"Fixture Person {i}")
            Observation.objects.create(lead=lead, source=sources[10] if i < 9 else demo, page_url="https://x.example/",
                                       source_category="merchant_services", evidence="fixture", content_hash="x")
        for i in range(40):
            SourceCandidate.objects.create(url=f"https://collector{i}.example/our-team/", label="Team",
                                           discovered_from=sources[10], evidence_url=sources[10].url, status="new")

        def report(**options):
            out = io.StringIO()
            started = time.perf_counter()
            with CaptureQueriesContext(connection) as queries:
                call_command("discovery_inventory", campaign=campaign.pk, stdout=out, **options)
            return json.loads(out.getvalue()), time.perf_counter() - started, len(queries)

        before, seconds, queries = report()
        b = before["before"]
        print("BEFORE", json.dumps({k: b[k] for k in ("campaign", "decisions", "inventory_state",
                                                     "active_approved", "parkable_now", "turnover_eligible_now", "leads")}, default=str))
        print(f"report: {seconds:.2f}s, {queries} queries for {DiscoveredURL.objects.count()} rows")
        check(b["campaign"]["active_inventory"] >= 3000, "fixture should start full")
        check(b["leads"] == {**b["leads"], "non_synthetic": 9, "demo_only": 6}, "lead provenance split")
        check(queries < 40, "report query count must not scale with rows")

        intake_due()
        check(not DiscoveryRun.objects.exists(), "full inventory must not open empty intake runs")
        check(SourceCandidate.objects.filter(status="new").count() == 40, "skipped collector links stay queued")
        print("PASS: full inventory -> no empty intake run; 40 collector links retained.")

        dry, _, _ = report(park_low_value=True, enable_parking=True)
        check(DiscoveredURL.objects.filter(archived_at__isnull=False).count() == 0, "dry run wrote rows")
        print(f"PASS: dry run would park {dry['dry_run']['would_park']}; nothing written.")

        decisions = dict(DiscoveredURL.objects.values_list("pk", "decision"))
        bindings = dict(DiscoveredURL.objects.values_list("pk", "source_id"))
        scopes = dict(DiscoveredURL.objects.values_list("pk", "dismissal_scope"))
        applied, seconds, queries = report(park_low_value=True, enable_parking=True, apply=True)
        a = applied["after"]
        print("AFTER", json.dumps({k: a[k] for k in ("campaign", "decisions", "inventory_state",
                                                              "active_approved", "parkable_now")},
                                  default=str))
        print(f"apply: {seconds:.2f}s, {queries} queries; parked {applied['applied']['parked']}")
        check(decisions == dict(DiscoveredURL.objects.values_list("pk", "decision")), "decisions changed")
        check(bindings == dict(DiscoveredURL.objects.values_list("pk", "source_id")), "source bindings changed")
        check(scopes == dict(DiscoveredURL.objects.values_list("pk", "dismissal_scope")), "dismissal scopes changed")
        check(a["campaign"]["active_inventory"] < 3000, "parking should free capacity")
        check(Source.objects.count() == 54, "no source created")
        print("PASS: decisions, bindings and dismissal scopes unchanged; no source created.")

        Campaign.objects.filter(pk=campaign.pk).update(next_intake_at=timezone.now())
        intake_due()
        run = DiscoveryRun.objects.get()
        check(run.intake_only and run.status == "completed", "intake run labelled and completed")
        admitted = campaign.urls.filter(method="collector", decision="pending").count()
        waiting = SourceCandidate.objects.filter(status="new").count()
        check(admitted == campaign.max_new_domains and admitted + waiting == 40, "collector links admitted within domain cap")
        print(f"PASS: intake after parking -> {run.message} ({waiting} left for the next pass: new-domain cap)")


if __name__ == "__main__":
    main()
