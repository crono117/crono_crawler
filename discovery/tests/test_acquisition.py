import io
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from automation.models import SiteAutomationJob, SitePolicy
from discovery import acquisition
from discovery.models import Campaign, DailyUsage, DiscoveredURL, DiscoveryJob, DiscoveryRun, SourceSearch
from discovery.services import approve, process as process_page, queue_candidate, register_batch, start
from discovery.turnover import archive_for_result, eligible, restore
from leads.models import DomainState, Lead, Observation, Source
from leads.services.network import FetchError
from leads.services.worker import acquire_lease, release_lease, tick as worker_tick


@override_settings(BRAVE_SEARCH_ENABLED=True, BRAVE_SEARCH_API_KEY="synthetic-secret", BRAVE_DAILY_SEARCH_LIMIT=20)
class AcquisitionTests(TestCase):
    def setUp(self):
        self.campaign = Campaign.objects.create(name="Source pilot", active=True, search_enabled=True,
            search_queries='"payment processing" "our team"', use_sitemaps=False)
        self.provider = patch("discovery.acquisition.brave_search", return_value=[{
            "url": "https://fresh.example.org/team", "title": "Payment processing sales team",
            "description": "Fictional sales representatives. Nothing here is contact evidence."}]).start()
        self.addCleanup(patch.stopall)

    def due(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(next_search_at=timezone.now())
        self.campaign.refresh_from_db()
        DomainState.objects.update(next_allowed_at=timezone.now())
        return acquisition.schedule(self.campaign)

    def test_full_deferred_run_does_not_hold_up_source_acquisition(self):
        self.campaign.max_pages = 1
        self.campaign.max_new_domains = 1
        self.campaign.save()
        run = DiscoveryRun.objects.create(campaign=self.campaign, status="running", new_domains=1)
        queued = DiscoveryJob.objects.create(run=run, url="https://old.example.org/team", status="queued",
            available_at=timezone.now() + timedelta(days=1), message="Daily fetch budget reached")
        SitePolicy.objects.create(campaign=self.campaign, enabled=True, require_page_intent=False)
        token = acquire_lease()
        self.assertIsNone(acquire_lease())
        try:
            worker_tick(token, prefer_discovery=True)
        finally:
            release_lease(token)
        attempt = self.campaign.source_searches.get()
        self.assertEqual(attempt.status, "done")
        self.assertEqual(attempt.run, run)
        self.assertEqual(attempt.summary["new_origins"], 1)
        self.assertEqual(run.jobs.count(), 1)
        queued.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual((queued.status, queued.message, run.status), ("queued", "Daily fetch budget reached", "running"))
        candidate = self.campaign.urls.get()
        self.assertEqual(candidate.decision, "approved")
        self.assertEqual(SiteAutomationJob.objects.get().state, "probe_queued")
        self.assertFalse(candidate.jobs.exists())
        self.assertFalse(Lead.objects.exists())
        self.assertEqual(self.campaign.usage.get().searches, 1)
        self.assertEqual(self.campaign.usage.get().requests, 0)

    def test_generated_queries_use_campaign_context_without_domains(self):
        self.campaign.auto_search_queries = True
        self.campaign.region = "Florida"
        self.campaign.keywords = "merchant services\npayment processing"
        self.campaign.sales_terms = "account executive\nsales representative"
        pool = acquisition.query_pool(self.campaign)
        self.assertEqual(len(pool), len(set(pool)))
        self.assertIn('"merchant services" "meet our team" "Florida"', pool)
        self.assertIn('"merchant services" "independent sales agent" "Florida"', pool)
        self.assertFalse(any('"contact"' in q for q in pool))  # bare contact pages yield shared inboxes
        self.assertIn('"payment processing" "account executive team" "Florida"', pool)
        self.assertTrue(all(len(q) <= 600 and len(q.split()) <= 75 and "https://" not in q for q in pool))

    def test_rotation_survives_restart_and_zero_novelty_cools_down(self):
        self.campaign.search_queries = "query one\nquery two"
        self.campaign.save()
        first = self.due()
        acquisition.tick()
        second = self.due()
        self.assertNotEqual(first.query, second.query)
        acquisition.tick()  # same result URL: duplicate, no new origin
        second.refresh_from_db()
        self.assertEqual(second.summary["new_origins"], 0)
        self.assertIsNone(self.due())
        self.assertEqual(self.provider.call_count, 2)
        self.campaign.refresh_from_db()
        self.assertGreater(self.campaign.next_search_at, timezone.now() + timedelta(hours=23))
        # After one day the productive query can be revisited, duplicate-only query still rests.
        SourceSearch.objects.filter(pk=first.pk).update(finished_at=timezone.now() - timedelta(days=2))
        self.assertEqual(self.due().query, first.query)

    def test_campaign_provider_pause_and_inventory_gates_make_no_requests(self):
        for fields, settings in [({"search_enabled": False}, {}), ({"active": False}, {}),
                ({}, {"BRAVE_SEARCH_ENABLED": False}), ({}, {"BRAVE_SEARCH_API_KEY": ""})]:
            with self.subTest(fields=fields, settings=settings), override_settings(**settings):
                Campaign.objects.filter(pk=self.campaign.pk).update(active=True, search_enabled=True)
                Campaign.objects.filter(pk=self.campaign.pk).update(**fields)
                self.assertIsNone(self.due())
        Campaign.objects.filter(pk=self.campaign.pk).update(active=True, search_enabled=True, max_candidates=1)
        self.campaign.urls.create(url="https://held.example.org/team", origin="https://held.example.org")
        self.assertIsNone(self.due())
        self.assertIn("inventory is full", acquisition.search_status(self.campaign)["reason"])
        self.provider.assert_not_called()
        self.assertFalse(DailyUsage.objects.exists())

    def test_global_budget_zero_and_campaign_budget_defer_without_refund(self):
        attempt = self.due()
        with override_settings(BRAVE_DAILY_SEARCH_LIMIT=0):
            acquisition.tick()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "queued")
        self.assertGreater(attempt.available_at, timezone.now())
        self.assertEqual(self.campaign.usage.get().searches, 0)
        SourceSearch.objects.filter(pk=attempt.pk).update(available_at=timezone.now())
        DailyUsage.objects.filter(campaign=self.campaign).update(searches=self.campaign.daily_search_limit)
        acquisition.tick()
        self.provider.assert_not_called()
        self.assertEqual(self.campaign.usage.get().searches, self.campaign.daily_search_limit)

    def test_crash_recovery_preserves_reservations_and_deferral(self):
        attempt = self.due()
        until = timezone.now() + timedelta(hours=2)
        SourceSearch.objects.filter(pk=attempt.pk).update(status="processing", available_at=until, attempts=2)
        DailyUsage.objects.create(campaign=self.campaign, day=timezone.now().date(), searches=2)
        token = acquire_lease()
        try:
            attempt.refresh_from_db()
            self.assertEqual((attempt.status, attempt.available_at, attempt.attempts), ("queued", until, 2))
            self.assertFalse(acquisition.tick())
        finally:
            release_lease(token)
        self.assertEqual(self.campaign.usage.get().searches, 2)

    def test_utc_reset_resumes_search_without_resetting_previous_counters(self):
        today = timezone.now().date()
        DailyUsage.objects.create(campaign=self.campaign, day=today, searches=self.campaign.daily_search_limit)
        attempt = self.due()
        acquisition.tick()
        attempt.refresh_from_db()
        self.provider.assert_not_called()
        after_reset = attempt.available_at + timedelta(seconds=1)
        with patch("django.utils.timezone.now", return_value=after_reset):
            self.assertTrue(acquisition.tick())
        attempt.refresh_from_db()
        self.campaign.refresh_from_db()
        self.assertEqual(attempt.status, "done")
        self.assertEqual(self.campaign.usage.get(day=today).searches, self.campaign.daily_search_limit)
        self.assertEqual(self.campaign.usage.get(day=after_reset.date()).searches, 1)
        self.assertGreaterEqual(self.campaign.next_search_at, after_reset + timedelta(minutes=60))

    def test_request_failure_and_retry_keep_spend_and_sanitize_messages(self):
        self.provider.side_effect = FetchError("synthetic-secret", status=429, retryable=True, retry_after="120")
        attempt = self.due()
        acquisition.tick()
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.attempts), ("queued", 1))
        self.assertGreater(attempt.available_at, timezone.now() + timedelta(seconds=100))
        self.assertNotIn("synthetic-secret", attempt.message)
        self.assertEqual(self.campaign.usage.get().searches, 1)
        self.assertFalse(acquisition.tick())
        self.provider.assert_called_once()

    def test_provider_denial_does_not_pause_page_collection(self):
        self.provider.side_effect = FetchError("synthetic-secret", status=401)
        attempt = self.due()
        acquisition.tick()
        self.campaign.refresh_from_db()
        attempt.refresh_from_db()
        self.assertTrue(self.campaign.active)
        self.assertIn("denied access", attempt.message)
        self.assertGreater(attempt.available_at, timezone.now() + timedelta(hours=23))

    def test_pause_during_request_discards_results_but_keeps_reservation(self):
        def paused(query):
            Campaign.objects.filter(pk=self.campaign.pk).update(active=False)
            return [{"url": "https://fresh.example.org/team"}]
        self.provider.side_effect = paused
        attempt = self.due()
        acquisition.tick()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "skipped")
        self.assertFalse(self.campaign.urls.exists())
        self.assertEqual(self.campaign.usage.get().searches, 1)

    def test_legacy_search_drains_before_independent_search_without_releasing_delay(self):
        run = DiscoveryRun.objects.create(campaign=self.campaign, status="running")
        until = timezone.now() + timedelta(hours=2)
        old = DiscoveryJob.objects.create(run=run, kind="search", url="legacy", available_at=until)
        self.assertIsNone(self.due())
        start(self.campaign)
        old.refresh_from_db()
        self.assertEqual(old.available_at, until)
        self.assertEqual(run.jobs.count(), 1)
        old.status = "done"
        old.save()
        self.assertIsNotNone(self.due())

    def test_outcomes_distinguish_duplicates_filters_and_capacity_rejection(self):
        self.campaign.max_candidates = 2
        self.campaign.max_new_domains = 1
        self.campaign.save()
        self.campaign.urls.create(url="https://old.example.org/team", origin="https://old.example.org")
        self.provider.return_value = [
            {"url": "https://old.example.org/team"},
            {"url": "https://fresh.example.org/team", "title": "Sales representatives"},
            {"url": "https://fresh.example.org/contact", "title": "Sales contacts"},
            {"url": "https://other.example.org/team", "title": "Sales representatives"},
            {"url": "https://linkedin.com/in/fictional"},
            {"url": "http://127.0.0.1/private"},
        ]
        attempt = self.due()
        acquisition.tick()
        attempt.refresh_from_db()
        stats = attempt.summary
        self.assertEqual((stats["new_urls"], stats["new_origins"], stats["duplicates"], stats["filtered"],
                          stats["capacity_rejected"], stats["domain_rejected"], stats["invalid"]), (1, 1, 1, 1, 1, 1, 1))
        self.assertFalse(Lead.objects.exists())

    def test_ingestion_error_rolls_back_inventory_but_does_not_refund_search(self):
        attempt = self.due()
        with patch("discovery.services.register_batch", side_effect=ValueError("synthetic-secret")):
            acquisition.tick()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "failed")
        self.assertIsNone(attempt.run_id)
        self.assertEqual(self.campaign.usage.get().searches, 1)
        self.assertFalse(self.campaign.urls.exists())
        self.assertNotIn("synthetic-secret", attempt.message)


class TurnoverTests(TestCase):
    def setUp(self):
        self.campaign = Campaign.objects.create(name="Inventory", active=True, max_candidates=1,
                                                auto_archive_candidates=True)
        self.run = DiscoveryRun.objects.create(campaign=self.campaign, status="running")

    def stale(self, slug="old", **kwargs):
        return self.campaign.urls.create(url=f"https://{slug}.example.org/team", origin=f"https://{slug}.example.org",
            score=0, last_seen=timezone.now() - timedelta(days=8), **kwargs)

    def test_turnover_admits_new_site_preserves_history_and_prevents_automatic_revival(self):
        old = self.stale(decision="dismissed", dismissal_scope="origin")
        stats = {}
        new = register_batch(self.run, [{"url": "https://fresh.example.org/team", "label": "Payment processing sales team"}],
                             allow_turnover=True, stats=stats)[0]
        old.refresh_from_db()
        self.assertIsNotNone(old.archived_at)
        self.assertEqual((old.decision, old.dismissal_scope), ("dismissed", "origin"))
        self.assertEqual(self.campaign.saved_url_count(), 1)
        self.assertEqual(self.campaign.urls.count(), 2)
        self.assertEqual(stats["archived"], 1)
        repeat = register_batch(self.run, [{"url": old.url, "label": "Payment processing sales team"}])[0]
        self.assertIsNotNone(repeat.archived_at)
        with self.assertRaisesMessage(ValueError, "inventory is full"):
            restore(old)
        DiscoveredURL.objects.filter(pk=new.pk).update(archived_at=timezone.now())
        restored = restore(old)
        self.assertIsNone(restored.archived_at)
        self.assertEqual((restored.decision, restored.dismissal_scope), ("dismissed", "origin"))

    def test_active_jobs_manual_imports_and_seeds_are_protected(self):
        active = self.stale("active")
        DiscoveryJob.objects.create(run=self.run, candidate=active, url=active.url)
        manual = self.stale("manual", manual_review_required=True)
        imported = self.stale("imported", method="operator_import")
        seed = self.stale("seed", method="seed")
        sitemap = self.stale("map", kind="sitemap")
        expected = self.stale("eligible")
        self.assertEqual(list(eligible(self.campaign)), [expected])
        self.assertEqual({active.pk, manual.pk, imported.pk, seed.pk, sitemap.pk} &
                         set(eligible(self.campaign).values_list("pk", flat=True)), set())

    def test_sources_with_suppressed_or_missing_contact_evidence_are_protected(self):
        source = Source.objects.create(name="Productive", url="https://productive.example.org/", approved=True)
        lead = Lead.objects.create(name="Fictional Person", identity="fictional", status="suppressed", contact_scope="shared", notes="Operator suppression stays intact")
        Observation.objects.create(source=source, lead=lead, page_url=source.url, present=False)
        self.stale("protected", source=source)
        self.assertFalse(eligible(self.campaign).exists())
        self.assertFalse(archive_for_result(self.campaign, incoming_score=100))
        lead.refresh_from_db()
        self.assertEqual(lead.status, "suppressed")

    def test_ten_distinct_zero_contact_pages_make_source_backlog_eligible(self):
        source = Source.objects.create(name="Empty", url="https://empty.example.org/", approved=True)
        completed = DiscoveryRun.objects.create(campaign=self.campaign, status="completed")
        for n in range(10):
            DiscoveryJob.objects.create(run=completed, source=source, url=f"{source.url}team/{n}", status="done")
        backlog = self.stale("backlog", source=source, decision="approved")
        DiscoveredURL.objects.filter(pk=backlog.pk).update(last_seen=timezone.now())
        self.assertIn(backlog, eligible(self.campaign))
        candidate = archive_for_result(self.campaign, incoming_score=50)[0]
        self.assertEqual(candidate.pk, backlog.pk)
        self.assertEqual(candidate.decision, "approved")

    def test_three_pages_failures_and_retries_are_not_enough_to_archive_source(self):
        source = Source.objects.create(name="Uncertain", url="https://uncertain.example.org/", approved=True)
        self.stale(source=source, decision="approved")
        for n in range(10):
            run = DiscoveryRun.objects.create(campaign=self.campaign, status="completed")
            DiscoveryJob.objects.create(run=run, source=source, url=source.url, status="done")
        self.assertFalse(eligible(self.campaign).exists())

    def test_archived_approval_cannot_queue_fetch_or_enter_policy(self):
        from automation.policy import consider
        source = Source.objects.create(name="Allowed", url="https://old.example.org/", approved=True)
        self.campaign.sources.add(source)
        old = self.stale(source=source, decision="approved", archived_at=timezone.now())
        queue_candidate(self.run, old)
        self.assertFalse(old.jobs.exists())
        with self.assertRaisesMessage(ValueError, "Restore"):
            approve(old, source)
        SitePolicy.objects.create(campaign=self.campaign, enabled=True, require_page_intent=False)
        self.assertIsNone(consider(old))
        job = DiscoveryJob.objects.create(run=self.run, source=source, candidate=old, url=old.url)
        with patch("discovery.services.fetch", side_effect=AssertionError("must not fetch")):
            process_page(job)
        job.refresh_from_db()
        self.assertEqual(job.status, "skipped")

    def test_no_turnover_when_disabled_or_new_result_is_weaker(self):
        old = self.stale()
        DiscoveredURL.objects.filter(pk=old.pk).update(score=80)
        self.assertFalse(archive_for_result(self.campaign, incoming_score=50))
        self.campaign.auto_archive_candidates = False
        self.assertFalse(archive_for_result(self.campaign, incoming_score=100))

    def test_overfull_inventory_does_not_partially_archive_without_admitting_result(self):
        old = self.stale()
        self.stale("manual", manual_review_required=True)
        stats = {}
        result = register_batch(self.run, [{"url": "https://fresh.example.org/team", "label": "Payment processing sales team"}],
                                allow_turnover=True, stats=stats)[0]
        self.assertIsNone(result)
        old.refresh_from_db()
        self.assertIsNone(old.archived_at)
        self.assertEqual((stats["archived"], stats["capacity_rejected"]), (0, 1))

    def test_pending_site_setup_and_regular_collection_protect_their_sources(self):
        from leads.models import PageJob, Run
        source = Source.objects.create(name="Setting up", url="https://setup.example.org/", approved=True)
        self.stale("setting-up", source=source)
        SiteAutomationJob.objects.create(source=source, campaign=self.campaign, state="canary")
        other = Source.objects.create(name="Collecting", url="https://collector.example.org/", approved=True)
        self.stale("collecting", source=other)
        run = Run.objects.create(source=other, status="paused")
        PageJob.objects.create(run=run, url=other.url)
        self.assertFalse(eligible(self.campaign).exists())


@override_settings(BRAVE_SEARCH_ENABLED=True, BRAVE_SEARCH_API_KEY="synthetic-secret")
class AcquisitionWebTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator", is_staff=True)
        self.client.force_login(self.user)
        self.campaign = Campaign.objects.create(name="Live pilot", active=True, daily_requests=5000)
        self.run = DiscoveryRun.objects.create(campaign=self.campaign, status="running")

    def test_search_configuration_preserves_active_run_jobs_counters_and_policies(self):
        job = DiscoveryJob.objects.create(run=self.run, url="https://old.example.org/team")
        DailyUsage.objects.create(campaign=self.campaign, day=timezone.now().date(), requests=808, searches=2)
        policy = SitePolicy.objects.create(campaign=self.campaign, enabled=True, min_recipe_score=70)
        response = self.client.post(reverse("discovery:source_search", args=[self.campaign.pk]), {
            "search_enabled": "on", "auto_search_queries": "on", "auto_archive_candidates": "on",
            "search_interval_minutes": 60, "daily_search_limit": 5, "search_queries": ""})
        self.assertEqual(response.status_code, 302)
        self.campaign.refresh_from_db()
        self.run.refresh_from_db()
        job.refresh_from_db()
        policy.refresh_from_db()
        self.assertEqual((self.campaign.active, self.campaign.daily_requests, self.run.status, job.status,
                          policy.min_recipe_score), (True, 5000, "running", "queued", 70))
        self.assertEqual((self.campaign.usage.get().requests, self.campaign.usage.get().searches), (808, 2))
        self.assertTrue(self.campaign.auto_search_queries)

    def test_settings_require_staff_csrf_and_configured_provider(self):
        url = reverse("discovery:source_search", args=[self.campaign.pk])
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(url, {}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.user)
        with override_settings(BRAVE_SEARCH_ENABLED=False):
            response = self.client.post(url, {"search_enabled": "on", "auto_search_queries": "on",
                "search_interval_minutes": 60, "daily_search_limit": 5})
        self.assertContains(response, "configure its API key")
        self.campaign.refresh_from_db()
        self.assertFalse(self.campaign.search_enabled)

    def test_status_command_and_lab_explain_disabled_search_without_secrets(self):
        out = io.StringIO()
        with patch("discovery.acquisition.brave_search", side_effect=AssertionError("read only")):
            call_command("discovery_status", campaign=self.campaign.pk, stdout=out)
        status = json.loads(out.getvalue())
        self.assertIn("disabled", status["reason"])
        self.assertTrue(status["key_present"])
        self.assertNotIn("synthetic-secret", out.getvalue())
        self.assertContains(self.client.get(reverse("discovery:campaign", args=[self.campaign.pk])), "Finding new sources")
        response = self.client.get(reverse("automation:lab_feed"))
        self.assertEqual(response.json()["source_searches"][0]["campaign_id"], self.campaign.pk)
        self.assertNotContains(response, "synthetic-secret")

    def test_archived_candidates_are_separate_from_review_backlog(self):
        old = self.campaign.urls.create(url="https://old.example.org/team", origin="https://old.example.org",
                                        archived_at=timezone.now(), archive_reason="Synthetic archive")
        self.assertNotContains(self.client.get(reverse("discovery:candidates")), old.url)
        self.assertContains(self.client.get(reverse("discovery:candidates") + "?decision=archived"), old.url)
        self.assertContains(self.client.get(reverse("discovery:candidate", args=[old.pk])), "Restore to active inventory")
        self.assertEqual(self.client.get(reverse("automation:lab_feed")).json()["review"]["worth_a_look"], 0)
        configure = self.client.get(reverse("source_new") + f"?discovery_url={old.pk}")
        self.assertRedirects(configure, reverse("discovery:candidate", args=[old.pk]))
        self.assertFalse(Source.objects.exists())
