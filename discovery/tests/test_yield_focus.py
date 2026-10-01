import io
import json
import tempfile
from datetime import timedelta

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from discovery import acquisition
from discovery.models import Campaign, DiscoveredURL, DiscoveryJob, DiscoveryRun
from discovery.ranking import exclusion_matches, rank
from discovery.services import (SITEMAPS_PER_SOURCE_PER_RUN, ZERO_YIELD_MIN_FETCHED, ZERO_YIELD_PAGES_PER_RUN,
                                finish, queue_candidate)
from leads.models import PageJob, Run, Source


class HostRuleTests(TestCase):
    def test_host_rule_matches_bank_hostnames_only(self):
        for url in ("https://www.pncbank.com/x", "https://first-bank.example/", "https://bank.example/"):
            self.assertTrue(exclusion_matches("host:bank", url, "", ""), url)
        for url in ("https://bankcardusa.com/team", "https://example.bank/", "https://agents.example/bank-fees"):
            self.assertFalse(exclusion_matches("host:bank", url, "", ""), url)
        self.assertFalse(exclusion_matches("host:", "https://bank.example/", "", ""))

    def test_host_rule_ignores_page_prose(self):
        campaign = Campaign(exclusions="host:bank")
        self.assertGreaterEqual(rank(campaign, "https://agents.example/team", "Our bank partners", "bank")[0], 0)
        self.assertEqual(rank(campaign, "https://pncbank.com/team")[0], -100)

    def test_generated_queries_get_negative_terms(self):
        campaign = Campaign(auto_search_queries=True, keywords="merchant services", sales_terms="",
                            exclusions="host:bank\ndomain:pnc.com\nhost:bad word")
        pool = acquisition.query_pool(campaign)
        self.assertTrue(pool and all(q.endswith(" -bank") for q in pool))
        self.assertIn('"merchant services" "independent sales agent" -bank', pool)


class QueueAllowanceTests(TestCase):
    def setUp(self):
        self.campaign = Campaign.objects.create(name="c", active=True, max_pages=50, use_sitemaps=True)
        self.source = Source.objects.create(name="s", url="https://reps.example.org/", approved=True, active=True)
        self.campaign.sources.add(self.source)
        self.run = DiscoveryRun.objects.create(campaign=self.campaign, status="running")

    def url(self, path, kind="page"):
        return DiscoveredURL.objects.create(campaign=self.campaign, url=f"https://reps.example.org{path}",
            origin="https://reps.example.org", label="Merchant services sales team", decision="approved",
            source=self.source, kind=kind, method="link")

    def test_sitemaps_beyond_allowance_are_not_queued(self):
        state = {"count": 0, "keys": set(), "collection_allowed": {}}
        for i in range(SITEMAPS_PER_SOURCE_PER_RUN + 3):
            queue_candidate(self.run, self.url(f"/sitemap-{i}.xml", "sitemap"), kind="sitemap", capacity=state)
        self.assertEqual(self.run.jobs.filter(kind="sitemap").count(), SITEMAPS_PER_SOURCE_PER_RUN)

    def test_zero_yield_origin_pages_capped_at_queue_time(self):
        from discovery.yields import OriginYield
        yields = {"https://reps.example.org": OriginYield(ZERO_YIELD_MIN_FETCHED, 0)}
        state = {"count": 0, "keys": set(), "collection_allowed": {}}
        for i in range(ZERO_YIELD_PAGES_PER_RUN + 4):
            queue_candidate(self.run, self.url(f"/team/{i}/"), capacity=state, origin_yields=yields)
        self.assertEqual(self.run.jobs.filter(kind="page").count(), ZERO_YIELD_PAGES_PER_RUN)
        self.assertEqual(state["count"], ZERO_YIELD_PAGES_PER_RUN)

    def test_productive_origin_not_capped(self):
        from discovery.yields import OriginYield
        yields = {"https://reps.example.org": OriginYield(ZERO_YIELD_MIN_FETCHED, 1)}
        state = {"count": 0, "keys": set(), "collection_allowed": {}}
        for i in range(ZERO_YIELD_PAGES_PER_RUN + 4):
            queue_candidate(self.run, self.url(f"/team/{i}/"), capacity=state, origin_yields=yields)
        self.assertEqual(self.run.jobs.count(), ZERO_YIELD_PAGES_PER_RUN + 4)

    def test_finished_run_explains_outcome(self):
        DiscoveryJob.objects.create(run=self.run, url="https://a.example/1", status="failed", message="Access blocked: 403")
        DiscoveryJob.objects.create(run=self.run, url="https://a.example/2", status="skipped",
                                    message="Sitemap allowance for this source used in this run (5); no request made.")
        DiscoveryJob.objects.create(run=self.run, url="https://a.example/3", status="done")
        finish(self.run)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "failed")
        self.assertIn("1 done, 1 failed, 1 skipped", self.run.message)
        self.assertIn("Top failures: Access blocked (1)", self.run.message)
        self.assertIn("Top skips: Sitemap allowance", self.run.message)


class TuneupCommandTests(TestCase):
    def setUp(self):
        self.campaign = Campaign.objects.create(name="c", active=True, exclusions="path:/careers",
                                                auto_search_queries=True)
        self.bank = DiscoveredURL.objects.create(campaign=self.campaign, url="https://www.truist.com/contact",
                                                 origin="https://www.truist.com", decision="pending")
        self.agent = DiscoveredURL.objects.create(campaign=self.campaign, url="https://agents.example/team",
                                                  origin="https://agents.example", decision="pending")

    def call(self, *args):
        out = io.StringIO()
        call_command("campaign_tuneup", "--campaign", str(self.campaign.pk), *args, stdout=out)
        return json.loads(out.getvalue())

    def test_dry_run_writes_nothing(self):
        report = self.call("--block-banks")
        self.assertFalse(report["applied"])
        self.assertEqual(len(report["banks"]["pending_would_dismiss"]), 1)
        self.campaign.refresh_from_db(); self.bank.refresh_from_db()
        self.assertEqual((self.campaign.exclusions, self.bank.decision), ("path:/careers", "pending"))

    def test_apply_banks_and_queries(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write('"independent sales agent" "merchant services" -bank\n"ISO agent program"\n')
        report = self.call("--block-banks", "--queries", f.name, "--apply")
        self.campaign.refresh_from_db(); self.bank.refresh_from_db(); self.agent.refresh_from_db()
        self.assertTrue(self.campaign.exclusions.startswith("path:/careers\nhost:bank"))
        self.assertIn("domain:truist.com", self.campaign.exclusions)
        self.assertEqual((self.bank.decision, self.agent.decision), ("dismissed", "pending"))
        self.assertFalse(self.campaign.auto_search_queries)
        self.assertEqual(acquisition.query_pool(self.campaign), report["queries"])
        # Idempotent: re-applying adds no duplicate rules.
        self.assertEqual(self.call("--block-banks", "--apply")["exclusions_added"], [])

    def test_pause_zero_yield_and_host(self):
        quiet = Source.objects.create(name="q", url="https://board.example/", approved=True, active=True)
        good = Source.objects.create(name="g", url="https://agency.example/", approved=True, active=True)
        self.campaign.sources.add(quiet, good)
        run = DiscoveryRun.objects.create(campaign=self.campaign, status="running")
        job = DiscoveryJob.objects.create(run=run, source=quiet, url="https://board.example/x", status="queued")
        report = self.call("--pause-host", "board.example", "--apply")
        quiet.refresh_from_db(); good.refresh_from_db(); job.refresh_from_db(); self.campaign.refresh_from_db()
        self.assertEqual([p["source"] for p in report["paused_sources"]], [quiet.pk])
        self.assertEqual((quiet.active, good.active, job.status), (False, True, "skipped"))
        self.assertIn("domain:board.example", self.campaign.exclusions)

    def test_clear_blocked_jobs(self):
        blocked = Source.objects.create(name="b", url="https://www.ziprecruiter.com/", blocked_at=timezone.now())
        other = Source.objects.create(name="o", url="https://other.example/")
        run = DiscoveryRun.objects.create(campaign=self.campaign, status="running")
        stuck = DiscoveryJob.objects.create(run=run, url="https://www.ziprecruiter.com/jobs", status="queued")
        fine = DiscoveryJob.objects.create(run=run, url="https://other.example/team", status="queued")
        collector = PageJob.objects.create(run=Run.objects.create(source=other), url="https://www.ziprecruiter.com/a")
        report = self.call("--clear-blocked", "--apply")
        stuck.refresh_from_db(); fine.refresh_from_db(); collector.refresh_from_db()
        self.assertEqual(len(report["blocked_jobs"]), 2)
        self.assertEqual((stuck.status, fine.status, collector.status), ("skipped", "queued", "skipped"))
