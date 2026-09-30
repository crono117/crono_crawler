"""Active-inventory parking, intake churn and the shared direct-intent gate. Offline, fictional data."""
import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from discovery.models import Campaign, DiscoveredURL, DiscoveryJob, DiscoveryRun
from discovery.ranking import (direct_contact_page_intent, low_value_page, ordinary_page_eligible)
from discovery.services import import_collector_backlog, intake_due, process, register_batch, start
from discovery.turnover import PARKED_REASON, park, parkable, restore
from leads.models import DomainState, Lead, Observation, PageSnapshot, Source, SourceCandidate
from leads.services.network import Response
from leads.services.reporting import production_leads

SITE = "https://payments.example.org"


def approved_row(campaign, source, path, label="", **kwargs):
    return DiscoveredURL.objects.create(campaign=campaign, source=source, url=SITE + path, origin=SITE,
                                        label=label, decision="approved", **kwargs)


class SharedIntentGateTests(TestCase):
    GENERIC = ("/sales", "/sales/", "/contact", "/contact-us/", "/partner", "/partners/", "/about", "/about-us/",
               "/representative", "/reseller", "/resellers/", "/brochure", "/sales-brochure/", "/schedule",
               "/schedule-a-demo/", "/sales-deck/", "/pricing/", "/")
    DIRECT = ("/team/", "/our-team/", "/sales-representatives/", "/people/jane-doe/", "/rep/jane-doe/",
              "/leadership/", "/staff/")
    TAXONOMY = ("/tag/sales-team/", "/tags/our-people/", "/category/team/", "/blog/topics/sales-representatives/")

    def setUp(self):
        self.campaign = Campaign.objects.create(name="Gate", min_score=0)

    def test_generic_terms_alone_never_qualify(self):
        for path in self.GENERIC:
            with self.subTest(path=path):
                self.assertFalse(direct_contact_page_intent(SITE + path))
                self.assertTrue(low_value_page(SITE + path))

    def test_direct_person_pages_qualify(self):
        for path in self.DIRECT:
            with self.subTest(path=path):
                self.assertTrue(direct_contact_page_intent(SITE + path))
                self.assertFalse(low_value_page(SITE + path))

    def test_taxonomy_listings_never_establish_intent(self):
        for path in self.TAXONOMY:
            with self.subTest(path=path):
                self.assertFalse(direct_contact_page_intent(SITE + path, "Sales team"))

    def test_nearby_context_ranks_but_cannot_authorize(self):
        context = "Meet our merchant services sales team and representatives"
        eligible, score, _ = ordinary_page_eligible(self.campaign, SITE + "/pricing/", "Pricing", context)
        self.assertGreater(score, 0)
        self.assertFalse(eligible)
        self.assertTrue(low_value_page(SITE + "/pricing/", "Pricing"))

    def test_exact_start_and_sitemaps_are_exempt(self):
        self.assertFalse(low_value_page(SITE + "/pricing/", kind="sitemap"))
        self.assertFalse(low_value_page(SITE + "/pricing/", explicit_start=True))

    def test_eligibility_and_parking_use_the_same_gate(self):
        for path in self.GENERIC + self.DIRECT + self.TAXONOMY:
            eligible, _, _ = ordinary_page_eligible(self.campaign, SITE + path, "Our team" if path == "/" else "")
            self.assertEqual(eligible, not low_value_page(SITE + path, "Our team" if path == "/" else ""), path)


class ParkingRegistrationTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="Fixture site", url=SITE + "/", approved=True, collector="demo")
        self.campaign = Campaign.objects.create(name="Parking", max_candidates=5, min_score=0, active=True)
        self.campaign.sources.add(self.source)
        self.run = DiscoveryRun.objects.create(campaign=self.campaign, status="running")

    def enable(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(park_low_value_urls=True)
        self.campaign.refresh_from_db()

    def test_default_off_preserves_existing_capacity_semantics(self):
        results = register_batch(self.run, [{"url": SITE + "/pricing/", "label": "Pricing"}])
        self.assertIsNone(results[0].archived_at)
        self.assertEqual(self.campaign.saved_url_count(), 1)

    def test_in_scope_low_value_pages_are_parked_outside_capacity_with_binding_intact(self):
        self.enable()
        specs = [{"url": SITE + f"/services/item-{index}/", "label": f"Item {index}"} for index in range(20)]
        specs.append({"url": SITE + "/team/", "label": "Our team"})
        specs.append({"url": "https://other.example.net/pricing/", "label": "Pricing"})
        results = register_batch(self.run, specs, campaign=Campaign.objects.get(pk=self.campaign.pk))
        parked = [row for row in results[:20]]
        self.assertTrue(all(row.archived_at and row.archive_reason == PARKED_REASON for row in parked))
        self.assertTrue(all(row.decision == "approved" and row.source_id == self.source.pk for row in parked))
        team, external = results[20], results[21]
        self.assertIsNone(team.archived_at)
        self.assertTrue(self.run.jobs.filter(url=team.url).exists())
        self.assertEqual(external.decision, "pending")  # never approved automatically
        self.assertIsNone(external.archived_at)
        self.assertEqual(self.campaign.saved_url_count(), 2)
        self.run.refresh_from_db()
        self.assertEqual(self.run.parked, 20)
        self.assertEqual(self.run.capacity_rejected, 0)
        self.assertFalse(self.run.jobs.filter(url__contains="/services/").exists())

    def test_capacity_is_still_honored_for_active_urls(self):
        self.enable()
        for index in range(5):
            approved_row(self.campaign, self.source, f"/people/p{index}/")
        results = register_batch(self.run, [{"url": SITE + "/staff/", "label": "Staff"},
                                            {"url": SITE + "/pricing/", "label": "Pricing"}])
        self.assertIsNone(results[0])
        self.assertEqual(results[1].archive_reason, PARKED_REASON)
        self.run.refresh_from_db()
        self.assertEqual(self.run.capacity_rejected, 1)
        self.assertIn("1 URL(s) in this batch not saved", self.run.message)

    def test_seeds_sitemaps_and_company_followups_are_never_parked(self):
        self.enable()
        results = register_batch(self.run, [
            {"url": SITE + "/", "seed": True, "method": "seed", "source_hint": self.source.pk},
            {"url": SITE + "/sitemap.xml", "kind": "sitemap", "seed": True, "method": "sitemap"},
        ])
        self.assertTrue(all(row.archived_at is None for row in results))

    def test_rediscovery_with_direct_label_unparks_only_automatic_parking(self):
        self.enable()
        parked = register_batch(self.run, [{"url": SITE + "/meet-us/", "label": "Meet us"}])[0]
        self.assertEqual(parked.archive_reason, PARKED_REASON)
        other = approved_row(self.campaign, self.source, "/our-people/", archived_at=timezone.now(),
                             archive_reason="Made room for a relevant source search result.")
        results = register_batch(self.run, [{"url": parked.url, "label": "Meet our sales team"},
                                            {"url": other.url, "label": "Our people"}])
        results[0].refresh_from_db()
        results[1].refresh_from_db()
        self.assertIsNone(results[0].archived_at)
        self.assertIsNotNone(results[1].archived_at)  # turnover archives never revive automatically

    def test_rediscovery_does_not_unpark_into_a_full_inventory(self):
        self.enable()
        parked = register_batch(self.run, [{"url": SITE + "/meet-us/", "label": "Meet us"}])[0]
        for index in range(5):
            approved_row(self.campaign, self.source, f"/people/p{index}/")
        register_batch(self.run, [{"url": parked.url, "label": "Sales team"}])
        parked.refresh_from_db()
        self.assertEqual(parked.archive_reason, PARKED_REASON)

    def test_tracking_variants_dedupe_to_the_parked_row(self):
        self.enable()
        register_batch(self.run, [{"url": SITE + "/pricing/"}])
        register_batch(self.run, [{"url": SITE + "/pricing/?utm_source=x&gclid=1#top"}])
        self.assertEqual(self.campaign.urls.filter(url=SITE + "/pricing/").count(), 1)
        self.assertEqual(self.campaign.urls.count(), 1)

    def test_parked_row_cannot_be_dispatched_even_if_a_job_exists(self):
        self.enable()
        parked = register_batch(self.run, [{"url": SITE + "/pricing/"}])[0]
        job = DiscoveryJob.objects.create(run=self.run, candidate=parked, source=self.source, url=parked.url)
        with patch("discovery.services.demo_response", side_effect=AssertionError("must not fetch")):
            process(job)
        self.assertEqual(DiscoveryJob.objects.get(pk=job.pk).status, "skipped")

    def test_restore_is_capacity_checked_and_keeps_approval(self):
        self.enable()
        parked = register_batch(self.run, [{"url": SITE + "/pricing/"}])[0]
        restored = restore(parked)
        self.assertIsNone(restored.archived_at)
        self.assertEqual(restored.decision, "approved")


class ParkingQueryCountTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="Fixture site", url=SITE + "/", approved=True, collector="demo")
        self.campaign = Campaign.objects.create(name="Queries", max_candidates=5000, min_score=0,
                                                park_low_value_urls=True, active=True)
        self.campaign.sources.add(self.source)

    def batch_queries(self, count, prefix):
        run = DiscoveryRun.objects.create(campaign=self.campaign, status="running") \
            if not self.campaign.runs.filter(status="running").exists() else self.campaign.runs.get(status="running")
        specs = [{"url": SITE + f"/{prefix}/post-{index}/"} for index in range(count)]
        with CaptureQueriesContext(connection) as queries:
            register_batch(run, specs)
        return len(queries)

    def test_parked_registration_costs_one_insert_per_new_row(self):
        small = self.batch_queries(10, "a")
        large = self.batch_queries(40, "b")
        self.assertLessEqual(large - small, 30)

    def test_parkable_scan_and_park_are_constant_query(self):
        for index in range(10):
            approved_row(self.campaign, self.source, f"/news/n{index}/")
        with CaptureQueriesContext(connection) as small:
            park(self.campaign, parkable(self.campaign))
        for index in range(200):
            approved_row(self.campaign, self.source, f"/news/m{index}/")
        with CaptureQueriesContext(connection) as large:
            park(self.campaign, parkable(self.campaign))
        self.assertEqual(len(small), len(large))


class ParkableProtectionTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="Productive", url=SITE + "/", approved=True)
        self.campaign = Campaign.objects.create(name="Protections", min_score=0)
        self.campaign.sources.add(self.source)
        self.run = DiscoveryRun.objects.create(campaign=self.campaign, status="running")

    def test_only_unprotected_low_value_approved_pages_are_parkable(self):
        target = approved_row(self.campaign, self.source, "/blog/one/")
        protected = [
            approved_row(self.campaign, self.source, "/", method="seed"),
            approved_row(self.campaign, self.source, "/imported/", method="operator_import"),
            approved_row(self.campaign, self.source, "/manual/", manual_review_required=True),
            approved_row(self.campaign, self.source, "/sitemap.xml", kind="sitemap"),
            approved_row(self.campaign, self.source, "/team/"),  # direct intent
            DiscoveredURL.objects.create(campaign=self.campaign, url="https://new.example.net/blog/",
                                         origin="https://new.example.net", decision="pending"),
            approved_row(self.campaign, self.source, "/x-dismissed/", dismissal_scope="url"),
        ]
        protected[-1].decision = "dismissed"
        protected[-1].save()
        busy = approved_row(self.campaign, self.source, "/busy/")
        DiscoveryJob.objects.create(run=self.run, candidate=busy, source=self.source, url=busy.url)
        productive = approved_row(self.campaign, self.source, "/about-us/")
        DiscoveryJob.objects.create(run=self.run, candidate=productive, source=self.source, url=productive.url + "?v=1",
                                    status="done", contacts_seen=2)
        evidenced = approved_row(self.campaign, self.source, "/contact/")
        lead = Lead.objects.create(identity="fixture-1", name="Fixture Person")
        Observation.objects.create(lead=lead, source=self.source, page_url=evidenced.url,
                                   source_category="merchant_services", evidence="fixture", content_hash="x")
        ids = set(parkable(self.campaign))
        # The productive source does not shield its other no-intent pages.
        self.assertEqual(ids, {target.pk})

    def test_parking_changes_only_archive_fields(self):
        target = approved_row(self.campaign, self.source, "/blog/one/", score=12, reasons=["fixture"])
        before = DiscoveredURL.objects.filter(pk=target.pk).values(
            "decision", "dismissal_scope", "source_id", "score", "reasons", "method", "label").get()
        self.assertEqual(park(self.campaign, parkable(self.campaign)), 1)
        after = DiscoveredURL.objects.filter(pk=target.pk).values(
            "decision", "dismissal_scope", "source_id", "score", "reasons", "method", "label").get()
        self.assertEqual(before, after)
        target.refresh_from_db()
        self.assertEqual(target.archive_reason, PARKED_REASON)

    def test_sources_in_automated_setup_are_protected(self):
        from automation.models import SiteAutomationJob
        from automation.services import RUNNABLE
        approved_row(self.campaign, self.source, "/blog/one/")
        SiteAutomationJob.objects.create(source=self.source, campaign=self.campaign, state=sorted(RUNNABLE)[0])
        self.assertEqual(parkable(self.campaign), [])


class IntakeChurnTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="Directory", url="https://dir.example/", approved=True,
                                            collector="demo", discover_external=True)
        self.campaign = Campaign.objects.create(name="Intake", active=True, max_candidates=1)
        self.campaign.sources.add(self.source)
        DiscoveredURL.objects.create(campaign=self.campaign, url="https://held.example/team/",
                                     origin="https://held.example", decision="pending")
        self.link = SourceCandidate.objects.create(url="https://fresh.example/our-team/", label="Team",
                                                   discovered_from=self.source, evidence_url=self.source.url, status="new")

    def test_full_inventory_does_not_open_empty_runs_every_intake(self):
        for _ in range(3):
            Campaign.objects.filter(pk=self.campaign.pk).update(next_intake_at=timezone.now())
            intake_due()
        self.assertFalse(DiscoveryRun.objects.filter(campaign=self.campaign).exists())
        self.link.refresh_from_db()
        self.assertEqual(self.link.status, "new")  # retried once there is room, never lost

    def test_intake_run_is_labelled_and_is_not_a_worker_or_extraction_failure(self):
        Campaign.objects.filter(pk=self.campaign.pk).update(max_candidates=10)
        self.campaign.refresh_from_db()
        self.assertEqual(import_collector_backlog(self.campaign), 1)
        run = DiscoveryRun.objects.get(campaign=self.campaign)
        self.assertTrue(run.intake_only)
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.pages_done, 0)
        self.assertFalse(run.needs_recipe_review)
        self.assertIn("Collector link intake only", run.message)
        self.assertIn("not a worker failure", run.message)


TEAM_HTML = (b'<nav><a href="/pricing/">Pricing</a><a href="/blog/">Blog</a><a href="/tag/sales-team/">Sales team tag</a>'
             b'<a href="/about-us/">About</a><a href="/team/">Our sales team</a>'
             b'<a href="https://vendor.example.net/reps/">Vendor reps</a></nav>')
PEOPLE_HTML = (b'<article class="team-member"><h3>Morgan Fixture</h3><p class="role">Merchant services sales '
               b'representative</p><a href="mailto:morgan@example.com">Email</a></article>'
               b'<article class="team-member"><h3>Jordan Sample</h3><p class="role">POS sales consultant</p>'
               b'<a href="mailto:jordan@example.com">Email</a></article>')


class TemporaryDatabaseIntegrationTests(TestCase):
    """A small copy of the live shape: approved low-value inventory has filled the campaign."""

    def setUp(self):
        self.source = Source.objects.create(name="Fixture merchant site", url=SITE + "/", approved=True,
                                            collector="http", delay_seconds=0)
        self.campaign = Campaign.objects.create(name="Pilot copy", max_candidates=30, max_pages=10, min_score=10,
                                                use_sitemaps=False, active=True,
                                                keywords="merchant services", sales_terms="sales team")
        self.campaign.sources.add(self.source)
        DomainState.objects.create(origin=SITE, robots_checked_at=timezone.now(), robots_text="User-agent: *\nAllow: /",
                                   next_allowed_at=timezone.now() - timedelta(minutes=1))
        approved_row(self.campaign, self.source, "/", method="seed")
        for index in range(26):
            approved_row(self.campaign, self.source, f"/merchant-services/article-{index}/", score=25)
        self.pending = [DiscoveredURL.objects.create(campaign=self.campaign, url=f"https://p{i}.example.net/team/",
                                                     origin=f"https://p{i}.example.net", decision="pending", score=35)
                        for i in range(2)]
        self.dismissed = DiscoveredURL.objects.create(campaign=self.campaign, url="https://gone.example.net/team/",
            origin="https://gone.example.net", decision="dismissed", dismissal_scope="origin")
        self.bindings = dict(self.campaign.urls.filter(decision="approved").values_list("pk", "source_id"))
        self.sources_before = Source.objects.count()

    def fake_fetch(self, url, *args, **kwargs):
        body = PEOPLE_HTML if url.rstrip("/").endswith("/team") else TEAM_HTML
        return Response(url, 200, {"content-type": "text/html"}, body)

    def drain(self, run, fetch=None):
        with patch("discovery.services.fetch", side_effect=fetch or self.fake_fetch):
            for _ in range(20):
                job = run.jobs.filter(status="queued").exclude(kind="search").order_by("-priority", "id").first()
                if not job:
                    break
                DomainState.objects.filter(origin=SITE).update(next_allowed_at=timezone.now() - timedelta(seconds=1))
                process(job)
        from discovery.services import finish
        finish(run)
        run.refresh_from_db()
        return run

    def test_parking_frees_capacity_and_yields_validated_non_demo_leads(self):
        self.assertGreaterEqual(self.campaign.saved_url_count(), self.campaign.max_candidates)
        out = StringIO()
        call_command("discovery_inventory", campaign=self.campaign.pk, park_low_value=True, stdout=out)
        dry = json.loads(out.getvalue())
        self.assertEqual(dry["dry_run"]["would_park"], 26)
        self.assertEqual(self.campaign.urls.filter(archived_at__isnull=False).count(), 0)  # dry run wrote nothing

        out = StringIO()
        call_command("discovery_inventory", campaign=self.campaign.pk, park_low_value=True, enable_parking=True,
                     apply=True, stdout=out)
        applied = json.loads(out.getvalue())
        self.assertEqual(applied["applied"]["parked"], 26)
        self.assertTrue(applied["applied"]["parking_enabled"])
        self.assertEqual(applied["after"]["campaign"]["active_inventory"], 4)

        run = self.drain(start(Campaign.objects.get(pk=self.campaign.pk)))
        self.assertEqual(run.status, "completed")
        team = self.campaign.urls.get(url=SITE + "/team/")
        self.assertIsNone(team.archived_at)
        self.assertTrue(team.jobs.filter(status="done", contacts_seen=2).exists())
        for path in ("/pricing/", "/tag/sales-team/", "/about-us/"):
            self.assertEqual(self.campaign.urls.get(url=SITE + path).archive_reason, PARKED_REASON, path)
        blog = self.campaign.urls.get(url=SITE + "/blog/")  # negative score: existing automatic filter
        self.assertEqual((blog.decision, blog.dismissal_scope, blog.archived_at), ("dismissed", "", None))
        vendor = self.campaign.urls.get(url="https://vendor.example.net/reps/")
        self.assertEqual(vendor.decision, "pending")
        self.assertEqual(Source.objects.count(), self.sources_before)  # no external approval created
        self.assertEqual(production_leads().count(), 2)
        self.assertEqual(Lead.objects.count() - production_leads().count(), 0)

        # Operator decisions and bindings survive parking plus rescoring.
        for pk, source_id in self.bindings.items():
            row = DiscoveredURL.objects.get(pk=pk)
            self.assertEqual((row.decision, row.source_id), ("approved", source_id))
        for row in self.pending:
            row.refresh_from_db()
            self.assertEqual(row.decision, "pending")
        self.dismissed.refresh_from_db()
        self.assertEqual((self.dismissed.decision, self.dismissed.dismissal_scope), ("dismissed", "origin"))
        self.assertLess(self.campaign.saved_url_count(), self.campaign.max_candidates)

    def test_zero_contact_crawl_completes_as_an_extraction_result(self):
        self.campaign.urls.exclude(method="seed").delete()
        run = start(Campaign.objects.get(pk=self.campaign.pk))
        run = self.drain(run, fetch=lambda url, *a, **k: Response(url, 200, {"content-type": "text/html"},
                                                                  b"<h1>No people here</h1>"))
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.pages_done, 1)
        self.assertTrue(run.needs_recipe_review)
        self.assertIn("No validated contacts", run.jobs.get().message)

    def test_report_is_read_only(self):
        counts = (DiscoveredURL.objects.count(), DiscoveryRun.objects.count(), PageSnapshot.objects.count())
        out = StringIO()
        call_command("discovery_inventory", campaign=self.campaign.pk, stdout=out)
        report = json.loads(out.getvalue())["before"]
        self.assertEqual(report["active_approved"]["no_direct_intent"], 26)
        self.assertEqual(report["parkable_now"], 26)
        self.assertEqual(report["decisions"]["pending"], 2)
        self.assertEqual(report["inventory_state"]["active_counted"], 30)
        self.assertEqual(report["decisions"]["dismissed_operator"], 1)
        self.assertEqual(counts, (DiscoveredURL.objects.count(), DiscoveryRun.objects.count(), PageSnapshot.objects.count()))


class RunPageTests(TestCase):
    def test_run_page_shows_intake_and_capacity_counters(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        user = get_user_model().objects.create_user("inventory-operator", is_staff=True)
        campaign = Campaign.objects.create(name="Counters")
        run = DiscoveryRun.objects.create(campaign=campaign, status="completed", intake_only=True,
                                          capacity_rejected=7, parked=3, message="Collector link intake only")
        self.client.force_login(user)
        page = self.client.get(reverse("discovery:run", args=[run.pk])).content.decode()
        self.assertIn("intake only", page)
        self.assertIn("Not saved: URL limit", page)
        self.assertIn("3 low-value page(s) parked", page)
