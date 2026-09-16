import gzip
from datetime import timedelta
from unittest.mock import patch
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from leads.models import DomainState, Lead, Observation, Source, WorkerLease
from leads.services.network import FetchError, Response
from leads.services.worker import acquire_lease, enqueue, pause_source, release_lease, tick
from discovery.models import Campaign, DailyUsage, DiscoveredURL, DiscoveryJob, DiscoveryRun
from discovery.providers import brave_search, sitemap_entries
from discovery.ranking import CONTACT_EXCLUSIONS, clean_url, rank
from discovery.services import DEMO_ORIGIN, approve, pause, record_links, register, start


class DiscoveryTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="Fictional directory", url=DEMO_ORIGIN + "/", approved=True,
                                            collector="demo", company="Example Payments")
        self.campaign = Campaign.objects.create(name="Test campaign", max_pages=10)
        self.campaign.sources.add(self.source)
        self.token = acquire_lease()

    def tearDown(self):
        release_lease(self.token)

    def finish(self, run):
        for _ in range(30):
            tick(self.token, prefer_discovery=True)
            run.refresh_from_db()
            if run.status in ("completed", "failed", "paused"):
                return
        self.fail("Discovery run did not finish")

    @patch("discovery.services.fetch", side_effect=AssertionError("Offline demo must not use network"))
    def test_demo_discovers_paths_sitemap_contacts_and_external_review(self, fetch):
        run = start(self.campaign)
        self.finish(run)
        self.assertEqual(run.status, "completed")
        self.assertEqual(set(Lead.objects.values_list("name", flat=True)), {"Dana Discovery", "Riley Example", "Casey Sample"})
        self.assertEqual(run.new_contacts, 3)
        self.assertTrue(run.jobs.filter(url__endswith="/team/hidden/", status="done").exists())
        external = self.campaign.urls.get(origin="https://new-vendor.example.org")
        self.assertEqual(external.url, "https://new-vendor.example.org/reps/")
        self.assertEqual(external.decision, "pending")
        self.assertFalse(external.jobs.exists())
        Lead.objects.filter(name="Dana Discovery").update(status="suppressed", notes="Keep suppression")
        with patch("discovery.services.extract", side_effect=AssertionError("Unchanged pages must not re-extract")):
            rerun = start(self.campaign)
            self.finish(rerun)
        self.assertEqual(rerun.status, "completed")
        self.assertEqual(rerun.new_contacts, 0)
        self.assertEqual(Lead.objects.count(), 3)
        self.assertEqual(Lead.objects.get(name="Dana Discovery").status, "suppressed")
        self.assertGreater(rerun.duplicates_seen, 0)

    def test_campaign_pause_resume_and_source_pause(self):
        run = start(self.campaign)
        pause(self.campaign)
        self.assertFalse(tick(self.token, prefer_discovery=True))
        self.assertEqual(start(self.campaign).pk, run.pk)
        pause_source(self.source)
        self.campaign.refresh_from_db()
        run.refresh_from_db()
        self.assertFalse(self.campaign.active)
        self.assertEqual(run.status, "paused")

    def test_approval_revocation_prevents_requests(self):
        run = start(self.campaign)
        self.source.approved = False
        self.source.save()
        with patch("discovery.services.demo_response", side_effect=AssertionError("Unapproved source fetched")):
            self.finish(run)
        self.assertFalse(Lead.objects.exists())
        self.assertTrue(run.jobs.filter(status="skipped").exists())

    def test_restart_recovers_processing_jobs(self):
        run = start(self.campaign)
        run.jobs.update(status="processing")
        WorkerLease.objects.update(heartbeat_at=timezone.now() - timedelta(minutes=11))
        self.token = acquire_lease()
        self.assertIsNotNone(self.token)
        self.assertFalse(run.jobs.filter(status="processing").exists())
        self.finish(run)
        self.assertEqual(Lead.objects.count(), 3)

    def test_scheduler_recurs_and_coalesces_due_campaign(self):
        first = start(self.campaign)
        self.finish(first)
        Campaign.objects.filter(pk=self.campaign.pk).update(next_due_at=timezone.now() - timedelta(hours=1))
        tick(self.token, prefer_discovery=True)
        tick(self.token, prefer_discovery=True)
        self.assertEqual(self.campaign.runs.count(), 2)
        self.assertEqual(self.campaign.runs.filter(status__in=("queued", "running")).count(), 1)

    def test_new_domain_page_and_candidate_caps(self):
        self.campaign.max_pages, self.campaign.max_new_domains = 2, 1
        self.campaign.save()
        run = start(self.campaign)
        first = register(run, "https://one.example.org/reps", label="Sales representative")
        duplicate = register(run, "https://one.example.org/reps?utm_source=x#top")
        self.assertEqual(first.pk, duplicate.pk)
        self.assertIsNone(register(run, "https://two.example.org/reps"))
        self.finish(run)
        self.assertLessEqual(run.jobs.count(), 2)
        self.campaign.max_candidates = self.campaign.urls.count()
        self.campaign.save()
        run.campaign = self.campaign
        self.assertIsNone(register(run, DEMO_ORIGIN + "/new-team"))

    def test_dismissal_survives_rediscovery_and_stops_queued_fetch(self):
        run = start(self.campaign)
        candidate = self.campaign.urls.get(url=self.source.url)
        candidate.decision = "dismissed"
        candidate.save()
        register(run, candidate.url, seed=True)
        candidate.refresh_from_db()
        self.assertEqual(candidate.decision, "dismissed")
        self.finish(run)
        self.assertEqual(candidate.jobs.get().status, "skipped")

    def test_review_only_expands_to_explicitly_approved_scope(self):
        run = start(self.campaign)
        candidate = register(run, "https://vendor.example.org/reps/alex")
        bad = Source.objects.create(name="Unreviewed", url=candidate.url, approved=False)
        with self.assertRaises(ValueError):
            approve(candidate, bad)
        bad.approved, bad.allowed_paths = True, "/different"
        bad.save()
        with self.assertRaises(ValueError):
            approve(candidate, bad)
        bad.allowed_paths = "/reps"
        bad.save()
        approve(candidate, bad)
        candidate.refresh_from_db()
        self.assertEqual(candidate.decision, "approved")
        self.assertTrue(self.campaign.sources.filter(pk=bad.pk).exists())
        self.assertFalse(self.campaign.urls.filter(origin="https://vendor.example.org", decision="pending").exists())

    @patch("leads.services.worker.fetch")
    def test_fetch_budget_includes_robots_persists_and_cannot_be_reset_by_resume(self, fetch):
        self.source.collector = "http"
        self.source.save()
        self.campaign.daily_requests, self.campaign.use_sitemaps = 1, False
        self.campaign.save()
        fetch.return_value = Response(DEMO_ORIGIN + "/robots.txt", 200, {"content-type": "text/plain"}, b"User-agent: *\nAllow: /")
        run = start(self.campaign)
        tick(self.token, prefer_discovery=True)
        DomainState.objects.update(next_allowed_at=timezone.now())
        run.jobs.update(available_at=timezone.now())
        with patch("discovery.services.fetch", side_effect=AssertionError("Budget exceeded")):
            tick(self.token, prefer_discovery=True)
        job = run.jobs.get()
        self.assertGreater(job.available_at, timezone.now() + timedelta(minutes=1))
        pause(self.campaign)
        start(self.campaign)
        job.refresh_from_db()
        self.assertGreater(job.available_at, timezone.now())
        self.assertEqual(DailyUsage.objects.get().requests, 1)
        self.assertEqual(fetch.call_count, 1)

    def test_discovery_and_collection_both_progress(self):
        run = start(self.campaign)
        normal = Source.objects.create(name="Regular fixture", url="https://example.com/team/", approved=True, collector="demo", follow_links=False)
        normal_run = enqueue(normal)
        tick(self.token, prefer_discovery=True)
        self.assertTrue(run.jobs.filter(status="done").exists())
        tick(self.token, prefer_discovery=False)
        normal_run.refresh_from_db()
        self.assertEqual(normal_run.status, "completed")

    def test_zero_contacts_are_visible_and_extraction_error_preserves_evidence(self):
        run = start(self.campaign)
        self.finish(run)
        self.assertTrue(run.jobs.filter(message__contains="No validated contacts").exists())
        count = Observation.objects.filter(present=True).count()
        self.source.recipe = {"row": ".team-member"}
        self.source.save()
        run = start(self.campaign)
        with patch("discovery.services.extract", side_effect=ValueError("Fixture extraction error")):
            self.finish(run)
        self.assertEqual(run.status, "failed")
        self.assertEqual(Observation.objects.filter(present=True).count(), count)

    def test_zero_contact_diagnostics_survive_worker_completion(self):
        self.campaign.use_sitemaps, self.campaign.max_pages = False, 1
        self.campaign.save()
        run = start(self.campaign)
        self.finish(run)
        self.assertTrue(run.needs_recipe_review)
        self.assertIn("No person cards matched", run.jobs.get().message)

    def test_navigation_context_does_not_promote_product_links(self):
        self.campaign.use_sitemaps, self.campaign.min_score = False, 35
        self.campaign.save()
        run = start(self.campaign)
        html = '<nav><div>Merchant services sales team<a href="/terminal/">Terminal</a></div><a href="/executive-team/">Executive team</a></nav>'
        record_links(run.jobs.get(), html, self.source.url)
        product = self.campaign.urls.get(url=DEMO_ORIGIN + "/terminal/")
        team = self.campaign.urls.get(url=DEMO_ORIGIN + "/executive-team/")
        self.assertLess(product.score, 35)
        self.assertFalse(product.jobs.exists())
        self.assertTrue(team.jobs.exists())

    def test_new_high_scoring_domain_still_requires_manual_review(self):
        candidate = register(start(self.campaign), "https://other.example.org/sales/representatives/", label="Merchant services sales team")
        self.assertGreaterEqual(candidate.score, 70)
        self.assertEqual(candidate.decision, "pending")
        self.assertIsNone(candidate.source)
        self.assertFalse(candidate.jobs.exists())

    def test_current_exclusions_stop_previously_queued_requests(self):
        self.campaign.use_sitemaps = False
        self.campaign.save()
        run = start(self.campaign)
        candidate = register(run, DEMO_ORIGIN + "/product/team/", label="Merchant services sales team")
        self.assertTrue(candidate.jobs.exists())
        self.campaign.refresh_from_db()
        self.campaign.exclusions = "path:product"
        self.campaign.save()
        from discovery.services import demo_response
        with patch("discovery.services.demo_response", wraps=demo_response) as fetch:
            self.finish(run)
        self.assertNotIn(candidate.url, [call.args[0] for call in fetch.call_args_list])
        self.assertIn("excluded", candidate.jobs.get().message)

    def test_empty_excluded_campaign_finishes_without_stuck_run(self):
        self.campaign.exclusions, self.campaign.use_sitemaps = "fictional", False
        self.campaign.save()
        run = start(self.campaign)
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")

    def test_offline_demo_command_is_idempotent(self):
        call_command("init_discovery_demo", verbosity=0)
        call_command("init_discovery_demo", verbosity=0)
        self.assertEqual(Campaign.objects.filter(name="Offline discovery demo").count(), 1)
        self.assertEqual(DiscoveryRun.objects.filter(campaign__name="Offline discovery demo").count(), 1)


class RankingAndProviderTests(TestCase):
    def test_contact_filters_match_plural_paths_and_social_hosts(self):
        campaign = Campaign(exclusions="\n".join(CONTACT_EXCLUSIONS))
        for path in ("/blog/", "/blogs/", "/articles/", "/products/", "/software/", "/emv-credit-card-machines/device/"):
            with self.subTest(path=path):
                self.assertLess(rank(campaign, "https://example.com" + path, "Sales team")[0], 0)
        for url in ("https://www.facebook.com/", "https://linkedin.com/in/example", "https://x.com/company"):
            with self.subTest(url=url):
                self.assertLess(rank(campaign, url, "Sales team")[0], 0)
        for path in ("/team/", "/sales/", "/agent/", "/partners/", "/representatives/", "/reps/", "/executive/", "/contact/"):
            with self.subTest(path=path):
                self.assertGreaterEqual(rank(campaign, "https://example.com" + path)[0], 35)

    def test_path_exclusions_do_not_reject_team_context_about_software(self):
        campaign = Campaign(exclusions="path:software\ndomain:x.com")
        self.assertGreaterEqual(rank(campaign, "https://example.com/team/", "Sales representatives", "Our software product specialists")[0], 35)
        self.assertGreaterEqual(rank(campaign, "https://notx.com/team/")[0], 35)
        self.assertGreaterEqual(rank(campaign, "https://x.com.example.org/team/")[0], 35)
        campaign.exclusions = "x.com"
        self.assertLess(rank(campaign, "https://www.x.com/company")[0], 0)

    def test_team_context_alone_does_not_award_team_page_bonus(self):
        campaign = Campaign()
        score, reasons = rank(campaign, "https://example.com/product/device/", "Terminal", "Merchant services sales team")
        self.assertLess(score, 30)
        self.assertFalse(any("(+35)" in reason for reason in reasons))

    def test_ranking_prefers_relevant_team_and_explains_exclusions(self):
        campaign = Campaign(name="Test", exclusions="casino")
        high, reasons = rank(campaign, "https://example.com/team/", "Merchant services sales representatives")
        low, _ = rank(campaign, "https://example.com/blog/", "Latest updates")
        excluded, _ = rank(campaign, "https://example.com/casino/team/")
        self.assertGreater(high, low)
        self.assertTrue(any("Industry" in reason for reason in reasons))
        self.assertLess(excluded, 0)

    def test_url_canonicalization_preserves_useful_path_and_drops_tracking(self):
        self.assertEqual(clean_url("https://EXAMPLE.com/team/alex?page=2&utm_source=x#bio"), "https://example.com/team/alex?page=2")
        for url in ("http://127.0.0.1/", "http://[::1]/", "http://localhost/", "http://10.1.2.3/", "https://example.com/calendar/", "https://example.com/?sort=abc", "https://example.com/team/../private", "javascript:alert(1)"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                clean_url(url)

    def test_sitemap_plain_gzip_indexes_and_entity_rejection(self):
        xml = b'<urlset><url><loc>https://example.com/team</loc></url></urlset>'
        self.assertEqual(sitemap_entries(xml), [("page", "https://example.com/team")])
        self.assertEqual(sitemap_entries(gzip.compress(xml)), sitemap_entries(xml))
        self.assertEqual(sitemap_entries(b'<sitemapindex><sitemap><loc>https://example.com/map.xml</loc></sitemap></sitemapindex>'), [("sitemap", "https://example.com/map.xml")])
        with self.assertRaises(ValueError):
            sitemap_entries(gzip.compress(b"x" * (2 * 1024 * 1024 + 1)))
        with self.assertRaises(Exception):
            sitemap_entries(b'<!DOCTYPE x [<!ENTITY read SYSTEM "file:///etc/passwd">]><urlset><url><loc>&read;</loc></url></urlset>')

    @patch("discovery.providers.fetch")
    def test_search_is_disabled_by_default(self, fetch):
        with self.assertRaises(ValueError):
            brave_search("merchant services sales")
        fetch.assert_not_called()

    @override_settings(BRAVE_SEARCH_ENABLED=True, BRAVE_SEARCH_API_KEY="synthetic-key")
    @patch("discovery.providers.fetch")
    def test_search_adapter_bounds_results_and_forbids_token_redirects(self, fetch):
        import json
        fetch.return_value = Response("https://api.search.brave.com/res/v1/web/search", 200, {}, json.dumps({"web": {"results": [{"url": f"https://example.com/{i}"} for i in range(30)]}}).encode())
        self.assertEqual(len(brave_search("merchant services")), 20)
        self.assertFalse(fetch.call_args.kwargs["allow_redirects"])
        self.assertEqual(fetch.call_args.kwargs["request_headers"]["X-Subscription-Token"], "synthetic-key")

    @override_settings(BRAVE_SEARCH_ENABLED=True, BRAVE_SEARCH_API_KEY="synthetic-key", BRAVE_DAILY_SEARCH_LIMIT=1)
    @patch("discovery.services.brave_search")
    def test_search_budget_is_shared_and_results_stay_unfetched(self, search):
        search.return_value = [{"url": "https://new.example.org/reps/alex", "title": "Payment processing sales representative"}]
        first = Campaign.objects.create(name="Search A", search_enabled=True, search_queries="merchant services")
        second = Campaign.objects.create(name="Search B", search_enabled=True, search_queries="POS sales")
        run_a, run_b = start(first), start(second)
        token = acquire_lease()
        try:
            tick(token, prefer_discovery=True)
            DomainState.objects.update(next_allowed_at=timezone.now())
            tick(token, prefer_discovery=True)
        finally:
            release_lease(token)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(DiscoveredURL.objects.get().decision, "pending")
        self.assertFalse(DiscoveredURL.objects.get().jobs.exists())
        self.assertIn("budget", run_b.jobs.get().message)

    @override_settings(BRAVE_SEARCH_ENABLED=True, BRAVE_SEARCH_API_KEY="synthetic-key")
    @patch("discovery.services.brave_search", side_effect=FetchError("Rate limit", retryable=True, status=429, retry_after="120"))
    def test_search_rate_limit_uses_persisted_backoff(self, search):
        campaign = Campaign.objects.create(name="Search", search_enabled=True, search_queries="POS sales")
        run = start(campaign)
        token = acquire_lease()
        try:
            tick(token, prefer_discovery=True)
        finally:
            release_lease(token)
        job = run.jobs.get()
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.attempts, 1)
        self.assertGreater(job.available_at, timezone.now() + timedelta(seconds=100))
