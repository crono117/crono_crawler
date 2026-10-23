"""Intake precision: admission-only policy changes, external parking, per-origin share, reports."""
import json
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from automation.models import SiteAutomationJob, SitePolicy
from automation.policy import authorized
from automation.tests.test_pipeline import AutomationCase
from discovery.models import Campaign, DiscoveredURL, DiscoveryJob, DiscoveryRun
from discovery.services import register_batch
from discovery.turnover import (ORIGIN_CAP_REASON, PARKED_EXTERNAL_REASON, origin_excess, park,
                                parkable_external)
from leads.models import Source

SITE = "https://payments.example.org"


class AdmissionOnlyPolicyTests(AutomationCase):
    def test_admission_fields_do_not_revoke_admitted_setups(self):
        job = self.candidate().source.automation_job
        self.assertTrue(authorized(job))
        SitePolicy.objects.filter(pk=self.policy.pk).update(require_page_intent=False, min_url_score=40,
                                                            max_sites_per_day=1, notes="changed")
        job.refresh_from_db()
        self.assertTrue(authorized(job))
        SitePolicy.objects.filter(pk=self.policy.pk).update(canary_pages=self.policy.canary_pages + 1)
        self.assertFalse(authorized(job))

    def test_policy_command_is_dry_run_bounded_and_leaves_setup_fields(self):
        # The live shape: a stored recipe floor below the form's bounds, snapshotted into admitted jobs.
        SitePolicy.objects.filter(pk=self.policy.pk).update(min_recipe_score=70, require_page_intent=False)
        job = self.candidate().source.automation_job
        out = StringIO()
        call_command("automation_policy", campaign=self.campaign.pk, require_page_intent="on", stdout=out)
        self.assertFalse(json.loads(out.getvalue())["applied"])
        self.assertFalse(SitePolicy.objects.get(pk=self.policy.pk).require_page_intent)
        with self.assertRaises(CommandError):
            call_command("automation_policy", campaign=self.campaign.pk, min_url_score=10, apply=True, stdout=StringIO())
        out = StringIO()
        call_command("automation_policy", campaign=self.campaign.pk, require_page_intent="on", min_url_score=50,
                     apply=True, stdout=out)
        result = json.loads(out.getvalue())
        policy = SitePolicy.objects.get(pk=self.policy.pk)
        self.assertEqual((policy.require_page_intent, policy.min_url_score, policy.min_recipe_score), (True, 50, 70))
        self.assertEqual(result["setup_fields_unchanged"]["min_recipe_score"], 70)
        job.refresh_from_db()
        self.assertNotIn(job.state, ("paused", "failed"))
        self.assertTrue(authorized(job))


class RegistrationTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="Fixture", url=SITE + "/", approved=True, collector="demo")
        self.campaign = Campaign.objects.create(name="Precision", max_candidates=100, min_score=0,
                                                park_low_value_urls=True, max_new_domains=50, active=True)
        self.campaign.sources.add(self.source)
        self.run = DiscoveryRun.objects.create(campaign=self.campaign, status="running")

    def policy(self, require_intent=True, enabled=True):
        return SitePolicy.objects.create(campaign=self.campaign, enabled=enabled, require_page_intent=require_intent)

    def register(self, *specs):
        return register_batch(self.run, list(specs), campaign=Campaign.objects.get(pk=self.campaign.pk))

    def test_external_pages_are_parked_only_when_an_enabled_policy_requires_intent(self):
        home = {"url": "https://vendor-a.example.net/", "label": "Vendor A"}
        team = {"url": "https://vendor-b.example.net/our-team/", "label": "Our sales team"}
        without = self.register(home)[0]
        self.assertIsNone(without.archived_at)  # no policy: operator review keeps it
        policy = self.policy(require_intent=False)
        self.assertIsNone(self.register({"url": "https://vendor-c.example.net/"})[0].archived_at)
        SitePolicy.objects.filter(pk=policy.pk).update(require_page_intent=True)
        parked, active = self.register({"url": "https://vendor-d.example.net/", "label": "Vendor D"}, team)
        self.assertEqual((parked.decision, parked.source_id, parked.archive_reason),
                         ("pending", None, PARKED_EXTERNAL_REASON))
        self.assertIsNone(active.archived_at)
        self.assertEqual(active.decision, "pending")
        self.run.refresh_from_db()
        self.assertEqual(self.run.parked, 1)

    def test_parked_external_returns_when_rediscovered_with_intent(self):
        self.policy()
        parked = self.register({"url": "https://vendor-d.example.net/meet-us/", "label": "Meet us"})[0]
        again = self.register({"url": parked.url, "label": "Meet our sales team"})[0]
        again.refresh_from_db()
        self.assertIsNone(again.archived_at)
        self.assertEqual(again.decision, "pending")

    def test_origin_share_parks_excess_new_pages_and_exempts_seeds(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(max_active_per_origin=3)
        specs = [{"url": SITE + f"/team/person-{i}/", "label": "Sales team"} for i in range(5)]
        results = self.register(*specs)
        self.assertEqual(sum(1 for row in results if row.archived_at is None), 3)
        capped = [row for row in results if row.archived_at]
        self.assertEqual({row.archive_reason for row in capped}, {ORIGIN_CAP_REASON})
        self.assertTrue(all(row.decision == "approved" and row.source_id == self.source.pk for row in capped))
        seed = self.register({"url": SITE + "/", "seed": True, "method": "seed", "source_hint": self.source.pk})[0]
        self.assertIsNone(seed.archived_at)
        other = self.register({"url": "https://other.example.net/team/", "label": "Team"})[0]
        self.assertIsNone(other.archived_at)
        self.assertEqual(self.campaign.saved_url_count(), 5)

    def test_origin_share_zero_means_unlimited(self):
        results = self.register(*[{"url": SITE + f"/team/p{i}/", "label": "Team"} for i in range(8)])
        self.assertTrue(all(row.archived_at is None for row in results))

    def test_origin_share_adds_no_per_row_queries(self):
        def cost(cap, prefix):
            Campaign.objects.filter(pk=self.campaign.pk).update(max_active_per_origin=cap)
            with CaptureQueriesContext(connection) as queries:
                self.register(*[{"url": SITE + f"/{prefix}/team-{i}/", "label": "Team"} for i in range(20)])
            return len(queries)
        self.assertLessEqual(cost(1000, "a"), cost(0, "b") + 2)


class MaintenanceTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="Fixture", url=SITE + "/", approved=True)
        self.campaign = Campaign.objects.create(name="Maintenance", max_candidates=100, min_score=0)
        self.campaign.sources.add(self.source)
        self.run = DiscoveryRun.objects.create(campaign=self.campaign, status="running")

    def row(self, path, score=10, **kwargs):
        return DiscoveredURL.objects.create(campaign=self.campaign, source=self.source, url=SITE + path, origin=SITE,
                                            decision="approved", score=score, **kwargs)

    def test_origin_excess_parks_lowest_scores_and_never_protected_rows(self):
        seed = self.row("/", score=0, method="seed")
        busy = self.row("/team/busy/", score=1)
        DiscoveryJob.objects.create(run=self.run, candidate=busy, source=self.source, url=busy.url)
        rows = [self.row(f"/team/p{i}/", score=10 * i) for i in range(8)]
        selected = set(origin_excess(self.campaign, 4))
        self.assertEqual(len(selected), 6)  # 10 counted - 4
        self.assertNotIn(seed.pk, selected)
        self.assertNotIn(busy.pk, selected)
        self.assertEqual(selected, {row.pk for row in rows[:6]})
        self.assertEqual(park(self.campaign, selected, ORIGIN_CAP_REASON), 6)
        kept = set(self.campaign.urls.filter(archived_at__isnull=True).values_list("pk", flat=True))
        self.assertEqual(kept, {seed.pk, busy.pk, rows[6].pk, rows[7].pk})

    def test_parkable_external_requires_an_intent_policy(self):
        home = DiscoveredURL.objects.create(campaign=self.campaign, url="https://x.example.net/",
                                            origin="https://x.example.net", decision="pending", score=5)
        DiscoveredURL.objects.create(campaign=self.campaign, url="https://y.example.net/team/",
                                     origin="https://y.example.net", decision="pending", score=40)
        self.assertEqual(parkable_external(self.campaign), [])
        SitePolicy.objects.create(campaign=self.campaign, enabled=True, require_page_intent=True)
        self.assertEqual(parkable_external(self.campaign), [home.pk])

    def test_inventory_report_shows_composition_and_origin_cap_is_dry_run_first(self):
        for i in range(6):
            self.row(f"/team/p{i}/", score=i)
        DiscoveredURL.objects.create(campaign=self.campaign, url="https://x.example.net/",
                                     origin="https://x.example.net", decision="pending", score=5)
        out = StringIO()
        call_command("discovery_inventory", campaign=self.campaign.pk, origin_cap=2, stdout=out)
        result = json.loads(out.getvalue())
        report = result["before"]
        self.assertEqual(report["active_by_origin_top"][0]["origin"], SITE)
        self.assertEqual(report["active_by_origin_top"][0]["counted"], 6)
        self.assertEqual(report["last_24h"]["counted_now"], 7)
        self.assertEqual(report["active_pending"]["total"], 1)
        self.assertEqual(result["dry_run"]["would_park_origin_excess"], 4)
        self.assertEqual(self.campaign.urls.filter(archived_at__isnull=False).count(), 0)
        out = StringIO()
        call_command("discovery_inventory", campaign=self.campaign.pk, origin_cap=2, apply=True, stdout=out)
        applied = json.loads(out.getvalue())["applied"]
        self.assertEqual((applied["parked_origin_excess"], applied["max_active_per_origin"]), (4, 2))
        self.assertEqual(Campaign.objects.get(pk=self.campaign.pk).max_active_per_origin, 2)
        decisions = set(self.campaign.urls.values_list("decision", flat=True))
        self.assertEqual(decisions, {"approved", "pending"})


class PendingPoolAuditTests(AutomationCase):
    def test_pending_pool_counts_sites_admissible_with_and_without_intent(self):
        SitePolicy.objects.filter(pk=self.policy.pk).update(require_page_intent=False, max_sites_per_day=1)
        for host in ("a", "b"):
            DiscoveredURL.objects.create(campaign=self.campaign, url=f"https://{host}.example.net/",
                                         origin=f"https://{host}.example.net", decision="pending", score=20)
        DiscoveredURL.objects.create(campaign=self.campaign, url="https://c.example.net/our-team/",
                                     label="Merchant services sales team", origin="https://c.example.net",
                                     decision="pending", score=90)
        out = StringIO()
        call_command("automation_audit", campaign=self.campaign.pk, no_jobs=True, stdout=out)
        pool = json.loads(out.getvalue())["summary"]["pending_pool"]
        self.assertEqual(pool["pending_sites"], 3)
        self.assertEqual(pool["admissible_intent_optional"]["sites"], 3)
        self.assertEqual(pool["admissible_intent_required_min_35"]["sites"], 1)
        self.assertEqual(SiteAutomationJob.objects.count(), 0)  # read-only
