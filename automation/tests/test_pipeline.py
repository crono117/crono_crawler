import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from discovery.models import Campaign, DailyUsage, DiscoveredURL, DiscoveryRun
from discovery.services import register, start
from leads.models import DomainState, Lead, Observation, Source, WorkerLease
from leads.services.network import FetchError, Response, in_scope
from leads.services.worker import acquire_lease, enqueue, release_lease
from automation import services
from automation.models import ProbePage, RecipeVersion, SiteAutomationJob, SitePolicy
from automation.policy import collection_allowed, consider, consider_pending
from automation.private_pages import purge_expired, read_html
from automation.recipes import evaluate, recon_page

HTML = '''<html><main><article class="agent-tile"><h3>Alex Example</h3><p class="role">Merchant services sales consultant</p><a href="mailto:alex@example.com">Email Alex</a></article>
<article class="agent-tile"><h3>Casey Fixture</h3><p class="role">POS sales representative</p><a href="mailto:casey@example.com">Email Casey</a></article></main>
<footer><a href="mailto:sales@example.com">Company office</a><a href="tel:202-555-0100">Switchboard</a></footer></html>'''


class AutomationCase(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.settings_override = override_settings(DATA_DIR=Path(self.temp.name))
        self.settings_override.enable()
        self.campaign = Campaign.objects.create(name="Autonomous payments", active=True, use_sitemaps=False, daily_requests=40)
        self.policy = SitePolicy.objects.create(campaign=self.campaign, enabled=True, probe_pages=2, canary_pages=5)
        self.run = DiscoveryRun.objects.create(campaign=self.campaign)
        self.token = acquire_lease()
        self.responses = {"/team/": HTML, "/contact/": "<h1>Contact our company</h1>"}
        self.calls = []

    def tearDown(self):
        release_lease(self.token)
        self.settings_override.disable()
        self.temp.cleanup()

    def candidate(self, url="https://vendor.example.org/team/"):
        return register(self.run, url, label="Merchant services sales team")

    def fetch(self, url, *args, **kwargs):
        from urllib.parse import urlsplit
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return Response(url, 200, {"content-type": "text/plain"}, b"User-agent: *\nAllow: /\nSitemap: https://vendor.example.org/sitemap.xml\n")
        if kwargs.get("guard") and not kwargs["guard"](url):
            raise AssertionError("Out of scope")
        html = self.responses.get(urlsplit(url).path, "<h1>No people</h1>")
        if isinstance(html, Exception):
            raise html
        return Response(url, 200, {"content-type": "text/html; charset=utf-8"}, html.encode())

    def run_until(self, job, states=("active", "paused", "failed")):
        with patch("automation.services.fetch", side_effect=self.fetch), patch("leads.services.worker.fetch", side_effect=self.fetch):
            for _ in range(80):
                DomainState.objects.update(next_allowed_at=timezone.now())
                ProbePage.objects.filter(status="queued").update(available_at=timezone.now())
                SiteAutomationJob.objects.update(available_at=timezone.now())
                services.tick(self.token)
                job.refresh_from_db()
                if job.state in states:
                    return
        self.fail(f"Job did not settle: {job.state} {job.message}")

class AutomationTests(AutomationCase):
    def test_pending_rescan_advances_past_excluded_high_scoring_sites(self):
        DiscoveredURL.objects.bulk_create([DiscoveredURL(campaign=self.campaign,
            url=f"https://linkedin.com/team/{index}/", origin="https://linkedin.com", score=100)
            for index in range(100)])
        candidate = DiscoveredURL.objects.create(campaign=self.campaign, url="https://vendor.example.org/team/",
            origin="https://vendor.example.org", label="Merchant services sales team", score=80)
        consider_pending()
        self.assertFalse(SiteAutomationJob.objects.exists())
        consider_pending()
        candidate.refresh_from_db()
        self.assertEqual(candidate.decision, "approved")

    def test_old_response_cannot_modify_a_restarted_setup_generation(self):
        for outcome in ("response", "blocked"):
            with self.subTest(outcome=outcome):
                job = self.candidate(f"https://{outcome}.example.org/team/").source.automation_job
                DomainState.objects.create(origin=f"https://{outcome}.example.org", robots_text="User-agent: *\nAllow: /", robots_checked_at=timezone.now())
                def restart(url, *args, **kwargs):
                    services.pause_job(job, "Operator restarted setup", rollback=False)
                    services.start_setup(job.source, job.campaign)
                    if outcome == "blocked":
                        raise FetchError("Old response was blocked", status=403)
                    return self.fetch(url, *args, **kwargs)
                with patch("automation.services.fetch", side_effect=restart):
                    services.tick(self.token)
                job.refresh_from_db()
                self.assertEqual(job.generation, 2)
                self.assertEqual(job.state, "probe_queued")
                self.assertFalse(job.pages.exclude(private_key="").exists())
                services.pause_job(job, "Finish this test case", rollback=False)

    def test_stale_workers_failure_cannot_pause_the_replacement_worker(self):
        job = self.candidate().source.automation_job
        DomainState.objects.create(origin="https://vendor.example.org", robots_text="User-agent: *\nAllow: /", robots_checked_at=timezone.now())
        def replace_and_fail(*args, **kwargs):
            WorkerLease.objects.filter(key="collector").update(token="replacement-worker")
            raise FetchError("Old connection failed", status=403)
        with patch("automation.services.fetch", side_effect=replace_and_fail):
            services.tick(self.token)
        job.refresh_from_db()
        self.assertEqual(job.state, "probing")
        self.assertEqual(job.pages.first().attempts, 0)

    def test_canary_preserves_depth_one_from_original_start_page(self):
        self.responses["/team/"] += '<a href="/team/child/">Sales team</a>'
        self.responses["/team/child/"] = HTML + '<a href="/team/child/grandchild/">Sales team</a>'
        job = self.candidate().source.automation_job
        self.run_until(job)
        self.assertEqual(job.state, "active", job.message)
        self.assertFalse(any("grandchild" in url for url in self.calls))

    def test_automatic_filter_does_not_masquerade_as_operator_domain_dismissal(self):
        register(self.run, "https://vendor.example.org/software/", label="Software")
        self.assertEqual(self.candidate().decision, "approved")

    def test_old_worker_cannot_save_after_its_lease_is_replaced(self):
        source = self.candidate().source
        job = source.automation_job
        DomainState.objects.create(origin="https://vendor.example.org", robots_text="User-agent: *\nAllow: /", robots_checked_at=timezone.now())
        def replace_lease(url, *args, **kwargs):
            WorkerLease.objects.filter(key="collector").update(token="another-worker")
            return self.fetch(url, *args, **kwargs)
        with patch("automation.services.fetch", side_effect=replace_lease):
            services.tick(self.token)
        self.assertFalse(job.pages.exclude(private_key="").exists())

    def test_policy_qualifies_site_without_manual_approval_and_requires_canary(self):
        candidate = self.candidate()
        self.assertEqual(candidate.decision, "approved")
        source = candidate.source
        self.assertEqual(source.approval_kind, "policy")
        self.assertEqual(source.setup_mode, "automatic")
        self.assertFalse(source.active)
        self.assertFalse(candidate.jobs.exists())
        with self.assertRaises(ValueError):
            enqueue(source)
        job = source.automation_job
        self.run_until(job, states=("recipe_released", "paused", "failed"))
        self.assertEqual(job.state, "recipe_released", job.message)
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(collection_allowed(source))
        self.run_until(job)
        self.assertEqual(job.state, "active", job.message)
        source.refresh_from_db()
        self.assertTrue(collection_allowed(source))
        self.assertEqual(set(Lead.objects.values_list("name", flat=True)), {"Alex Example", "Casey Fixture"})
        self.assertEqual(job.current_recipe.status, "known_good")
        self.assertEqual(DailyUsage.objects.get().requests, len(self.calls))
        report = json.dumps(job.recon)
        self.assertNotIn("Alex Example", report)
        self.assertNotIn("alex@example.com", report)
        self.assertNotIn("<article", report)
        self.assertGreaterEqual(len(job.recon["pages"][0]["candidate_containers"]), 1)
        self.assertEqual(job.events.first().state, "active")

    def test_policy_off_low_score_social_private_and_out_of_scope_stay_unfetched(self):
        self.policy.enabled = False
        self.policy.save()
        self.assertEqual(self.candidate().decision, "pending")
        self.policy.enabled = True
        self.policy.save()
        for url in ("https://linkedin.com/team/", "https://another.example.org/products/team/", "https://www.x.com/team/"):
            self.candidate(url)
        register(self.run, "https://low.example.org/contact/", label="Contact")
        self.assertIsNone(register(self.run, "http://127.0.0.1/team/", label="Merchant services sales team"))
        self.assertFalse(SiteAutomationJob.objects.exists())

    def test_policy_cannot_onboard_high_scoring_generic_sales_collateral(self):
        self.policy.allowed_paths = "/"
        self.policy.min_url_score = 35
        self.policy.save(update_fields=["allowed_paths", "min_url_score"])
        candidate = register(self.run, "https://collateral.example.org/resources/sales-playbook/",
            label="Merchant services sales representative playbook",
            context="Payment processing sales team representatives")
        self.assertGreaterEqual(candidate.score, self.policy.min_url_score)
        self.assertEqual(candidate.decision, "pending")
        self.assertIsNone(consider(candidate))
        self.assertFalse(Source.objects.filter(url=candidate.url).exists())

    def test_recon_proposes_only_scored_links_with_direct_contact_intent(self):
        source = Source.objects.create(name="Recon", url="https://recon.example.org/", approved=True,
                                       allowed_paths="/", allow_homepage=True)
        campaign = Campaign.objects.create(name="Recon campaign", min_score=0)
        response = Response(source.url, 200, {"content-type": "text/html"}, b'''
            <a href="/resources/sales-playbook/">Merchant services sales representative playbook</a>
            <a href="/leadership/">Leadership</a>
        ''')
        _, links = recon_page(response, source, campaign)
        self.assertEqual(links, ["https://recon.example.org/leadership/"])

    def test_recon_uses_reviewed_source_bonus_at_threshold(self):
        source = Source.objects.create(name="Reviewed recon", url="https://reviewed-recon.example.org/",
                                       approved=True, allowed_paths="/", allow_homepage=True)
        campaign = Campaign.objects.create(name="Reviewed recon campaign", min_score=40)
        lead = Lead.objects.create(identity="reviewed-recon", name="Reviewed Recon", status="reviewed")
        Observation.objects.create(lead=lead, source=source, page_url=source.url, facts={},
            source_category=source.category, evidence="Reviewed", content_hash="reviewed-recon", present=True)
        response = Response(source.url, 200, {"content-type": "text/html"},
                            b'<a href="/team/">Team</a>')
        _, links = recon_page(response, source, campaign)
        self.assertEqual(links, ["https://reviewed-recon.example.org/team/"])

    def test_no_per_site_approval_for_next_qualified_domain_but_daily_site_cap_persists(self):
        self.policy.max_sites_per_day = 1
        self.policy.save()
        first = self.candidate()
        second = self.candidate("https://second.example.org/team/")
        self.assertEqual(first.decision, "approved")
        self.assertEqual(second.decision, "pending")
        self.assertIsNone(consider(second))
        self.assertEqual(SiteAutomationJob.objects.count(), 1)

    def test_scoped_homepage_is_not_permission_for_other_paths(self):
        candidate = self.candidate("https://home.example.org/")
        self.assertTrue(in_scope(candidate.source, "https://home.example.org/"))
        self.assertFalse(in_scope(candidate.source, "https://home.example.org/private/"))
        self.assertFalse(in_scope(candidate.source, "https://home.example.org/?account=123"))
        self.assertFalse(in_scope(candidate.source, "https://www.home.example.org/team/"))

    def test_probe_does_not_follow_external_or_excluded_links(self):
        self.responses["/team/"] += '<a href="/contact/">Contact</a><a href="/products/">Merchant services sales team</a><a href="https://elsewhere.example.org/team/">Sales team</a>'
        job = self.candidate().source.automation_job
        self.run_until(job)
        self.assertEqual(job.state, "active", job.message)
        self.assertFalse(any("elsewhere" in url or "/products/" in url for url in self.calls))
        self.assertLessEqual(job.pages.filter(phase="probe").count(), 2)
        self.assertLessEqual(job.pages.filter(phase="canary").count(), 5)

    def test_zero_contact_categories_pause_without_inventing_contacts(self):
        for html, result in (("<h1>Welcome</h1>", "no_matching_cards"),
                (HTML.replace('href="mailto:alex@example.com"', 'href="#"').replace('href="mailto:casey@example.com"', 'href="#"'), "cards_without_contacts")):
            with self.subTest(result=result):
                job = self.candidate("https://" + result + ".example.org/team/").source.automation_job
                self.responses["/team/"] = html
                self.run_until(job)
                self.assertEqual(job.state, "paused", job.message)
                self.assertIn(result, job.message)
        self.assertFalse(Lead.objects.exists())

    def test_blocked_probe_pauses_without_alternative_fetches(self):
        self.responses["/team/"] = '<h1>Performing security verification</h1>'
        job = self.candidate().source.automation_job
        self.run_until(job)
        self.assertEqual(job.state, "paused")
        self.assertEqual(self.calls, ["https://vendor.example.org/robots.txt", "https://vendor.example.org/team/"])
        self.assertFalse(RecipeVersion.objects.exists())

    def test_unapproved_or_changed_scope_is_rechecked_before_any_fetch(self):
        source = self.candidate().source
        job = source.automation_job
        source.approved = False
        source.save()
        with patch("automation.services.fetch", side_effect=AssertionError("No fetch allowed")), patch("leads.services.worker.fetch", side_effect=AssertionError("No robots allowed")):
            services.tick(self.token)
        job.refresh_from_db()
        self.assertEqual(job.state, "paused")
        self.assertFalse(Lead.objects.exists())

    def test_source_edit_during_fetch_discards_response(self):
        source = self.candidate().source
        job = source.automation_job
        DomainState.objects.create(origin="https://vendor.example.org", robots_text="User-agent: *\nAllow: /", robots_checked_at=timezone.now())
        def change(url, *args, **kwargs):
            Source.objects.filter(pk=source.pk).update(allowed_paths="/different")
            return self.fetch(url, *args, **kwargs)
        with patch("automation.services.fetch", side_effect=change):
            services.tick(self.token)
        job.refresh_from_db()
        self.assertEqual(job.state, "paused")
        self.assertFalse(job.pages.exclude(private_key="").exists())

    def test_request_quota_survives_retries_and_restart(self):
        self.campaign.daily_requests = 1
        self.campaign.save()
        job = self.candidate().source.automation_job
        with patch("leads.services.worker.fetch", side_effect=self.fetch), patch("automation.services.fetch", side_effect=AssertionError("Quota exceeded")):
            services.tick(self.token)
            DomainState.objects.update(next_allowed_at=timezone.now())
            ProbePage.objects.update(available_at=timezone.now())
            services.tick(self.token)
        self.assertEqual(DailyUsage.objects.get().requests, 1)
        self.assertIn("budget", job.pages.first().message)
        SiteAutomationJob.objects.filter(pk=job.pk).update(processing=True)
        WorkerLease.objects.update(heartbeat_at=timezone.now() - timedelta(minutes=11))
        self.token = acquire_lease()
        job.refresh_from_db()
        self.assertFalse(job.processing)
        self.assertEqual(DailyUsage.objects.get().requests, 1)

    def test_reprobe_keeps_versions_reviews_and_suppression(self):
        job = self.candidate().source.automation_job
        self.run_until(job)
        lead = Lead.objects.get(name="Alex Example")
        lead.status, lead.notes = "suppressed", "Keep this decision"
        lead.save()
        old_version = job.current_recipe_id
        old_recipe = job.current_recipe.recipe
        services.start_setup(job.source, job.campaign)
        self.run_until(job)
        self.assertEqual(job.state, "active", job.message)
        self.assertNotEqual(job.current_recipe_id, old_version)
        self.assertEqual(RecipeVersion.objects.get(pk=old_version).recipe, old_recipe)
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.notes), ("suppressed", "Keep this decision"))
        self.assertEqual(Lead.objects.count(), 2)

    def test_failed_canary_rolls_back_without_replacing_existing_evidence(self):
        job = self.candidate().source.automation_job
        self.run_until(job)
        old = job.current_recipe_id
        services.start_setup(job.source, job.campaign)
        self.run_until(job, ("recipe_released", "paused", "failed"))
        self.responses["/team/"] = "<h1>Team has moved</h1>"
        self.run_until(job)
        self.assertEqual(job.state, "paused")
        self.assertEqual(job.current_recipe_id, old)
        self.assertEqual(Observation.objects.filter(present=True).count(), 2)

    def test_normal_collection_drift_reprobes_and_keeps_evidence(self):
        job = self.candidate().source.automation_job
        self.run_until(job)
        job.source.refresh_from_db()
        result = services.monitor_page(job.source, Response(job.source.url, 200, {"content-type": "text/html"}, b"<h1>New layout</h1>"))
        self.assertIsNone(result)
        job.refresh_from_db()
        self.assertEqual(job.state, "probe_queued")
        self.assertEqual(Observation.objects.filter(present=True).count(), 2)

    def test_robots_change_pauses_instead_of_reprobing(self):
        job = self.candidate().source.automation_job
        self.run_until(job)
        job.source.refresh_from_db()
        DomainState.objects.update(robots_text="User-agent: *\nDisallow: /")
        self.assertIsNone(services.monitor_page(job.source, Response(job.source.url, 200, {}, HTML.encode())))
        job.refresh_from_db()
        self.assertEqual(job.state, "paused")
        self.assertIn("Robots policy changed", job.message)

    def test_snapshot_retention_expires_without_exporting_html(self):
        job = self.candidate().source.automation_job
        self.run_until(job)
        page = job.pages.exclude(private_key="").first()
        path = Path(self.temp.name) / "automation-private" / page.private_key
        self.assertTrue(path.exists())
        page.expires_at = timezone.now() - timedelta(seconds=1)
        page.save()
        with self.assertRaises(ValueError):
            read_html(page)
        purge_expired()
        self.assertFalse(path.exists())

    def test_global_switchboard_and_footer_inbox_never_become_person_contacts(self):
        source = Source(require_sales_role=True)
        html = HTML.replace("alex@example.com", "sales@example.com").replace("casey@example.com", "sales@example.com")
        records, _, stats = evaluate(html, source, {"row": ".agent-tile", "name": "h3", "title": ".role", "evidence": ".agent-tile"})
        self.assertEqual(records, [])
        self.assertEqual(stats["primary_rejections"], {"shared_or_global_contact": 2})

    def test_explicit_dismissal_is_not_undone_by_policy(self):
        DiscoveredURL.objects.create(campaign=self.campaign, url="https://vendor.example.org/contact/", origin="https://vendor.example.org",
            decision="dismissed", dismissal_scope="origin")
        self.assertEqual(self.candidate().decision, "pending")
        self.assertFalse(SiteAutomationJob.objects.exists())
