"""Fresh index URLs must be able to enter a run, and a full run must say so."""
import threading
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from discovery.models import Campaign, DiscoveredURL, DiscoveryJob, DiscoveryRun
from discovery.services import register_common_crawl, start
from leads.models import Source, WorkerLease
from leads.services.network import FetchError
from leads.services.worker import acquire_lease, release_lease, tick

ENABLED = dict(COMMON_CRAWL_ENABLED=True, COMMON_CRAWL_COLLECTION="")


def spec(url, label=""):
    return {"url": url, "label": label, "context": "", "found_on": "", "method": "commoncrawl",
            "depth": 1, "kind": "page"}


class QueueFixture(TestCase):
    def make_campaign(self, name, *, max_pages=10, min_score=35):
        campaign = Campaign.objects.create(
            name=name, keywords="", sales_terms="sales representative", min_score=min_score,
            max_pages=max_pages, use_sitemaps=False, active=True)
        return campaign

    def make_source(self, campaign, host):
        source = Source.objects.create(name=host, url=f"https://{host}/", approved=True, allow_homepage=True)
        campaign.sources.add(source)
        return source

    def remember(self, campaign, source, path, label="Team"):
        return DiscoveredURL.objects.create(
            campaign=campaign, source=source, url=source.url.rstrip("/") + path, origin="https://" + source.url.split("/")[2],
            label=label, decision="approved", method="link", kind="page")

    def empty_history(self, campaign, source, pages=3):
        old = DiscoveryRun.objects.create(campaign=campaign, status="completed")
        origin = "https://" + source.url.split("/")[2]
        for index in range(pages):
            url = f"{origin}/staff/history-{index}/"
            candidate = DiscoveredURL.objects.create(
                campaign=campaign, url=url, origin=origin, source=source, decision="approved", label="Staff")
            DiscoveryJob.objects.create(
                run=old, candidate=candidate, source=source, url=url, status="done", contacts_seen=0)
        return old


class FreshUrlAdmissionTests(QueueFixture):
    def test_index_urls_take_a_slot_before_seeds_fill_a_tight_run(self):
        campaign = self.make_campaign("Tight", max_pages=2, min_score=0)
        remembered = self.make_source(campaign, "remembered.example.test")
        fresh = self.make_source(campaign, "fresh.example.test")
        self.remember(campaign, remembered, "/our-team/old/", label="sales representative")
        fresh_url = "https://fresh.example.test/our-team/new/"
        run = start(campaign, priority_specs=[spec(fresh_url)])
        self.assertTrue(run.jobs.filter(url=fresh_url).exists())
        self.assertLessEqual(run.jobs.count(), 2)
        self.assertEqual(run.jobs.filter(url=fresh_url).count(), 1)

    def test_an_open_run_still_receives_priority_specs(self):
        campaign = self.make_campaign("Resume", max_pages=5, min_score=0)
        source = self.make_source(campaign, "resume.example.test")
        run = DiscoveryRun.objects.create(campaign=campaign, status="queued")
        url = "https://resume.example.test/our-team/"
        start(campaign, priority_specs=[spec(url)])
        run.refresh_from_db()
        self.assertEqual(run.status, "queued")
        self.assertTrue(run.jobs.filter(url=url, source=source).exists())
        self.assertEqual(run.jobs.filter(url=url).count(), 1)

    def test_a_full_queue_records_a_deferred_outcome(self):
        campaign = self.make_campaign("Full", max_pages=1, min_score=0)
        source = self.make_source(campaign, "full.example.test")
        run = DiscoveryRun.objects.create(campaign=campaign, status="queued")
        DiscoveryJob.objects.create(run=run, source=source, kind="page", url="https://full.example.test/our-team/held/")
        url = "https://full.example.test/our-team/waiting/"
        self.assertEqual(register_common_crawl(campaign, [spec(url)]), 1)
        candidate = campaign.urls.get(url=url)
        self.assertEqual(candidate.decision, "approved")
        self.assertIn("already at its page limit", candidate.last_result)
        self.assertFalse(candidate.jobs.exists())
        self.assertEqual(run.jobs.count(), 1)

    @override_settings(**ENABLED)
    def test_a_full_run_keeps_a_stored_fetch_summary(self):
        campaign = self.make_campaign("Summary", max_pages=1, min_score=0)
        source = self.make_source(campaign, "summary.example.test")
        run = DiscoveryRun.objects.create(campaign=campaign, status="running")
        DiscoveryJob.objects.create(run=run, source=source, kind="page", url="https://summary.example.test/our-team/held/")
        url = "https://summary.example.test/our-team/known/"
        known = DiscoveredURL.objects.create(
            campaign=campaign, source=source, url=url, origin="https://summary.example.test",
            decision="approved", method="commoncrawl", last_result="2 contact(s); 1 new.")
        out = StringIO()
        with patch("discovery.management.commands.discovery_commoncrawl.common_crawl_specs",
                   return_value=([spec(url)], {"Summary": 1})):
            call_command("discovery_commoncrawl", campaign=campaign.pk, collection="CC-MAIN-2026-35",
                         start_if_idle=True, stdout=out)
        self.assertIn("Processed 1 index URL(s); 0 newly registered; 1 approved; 0 queued; 1 deferred.", out.getvalue())
        known.refresh_from_db()
        self.assertEqual(known.last_result, "2 contact(s); 1 new.")
        self.assertFalse(run.jobs.filter(url=url).exists())
        self.assertEqual(run.jobs.count(), 1)

    def test_five_hundred_jobs_defer_every_new_eligible_url_without_duplicates(self):
        campaign = self.make_campaign("Saturated", max_pages=500, min_score=0)
        source = self.make_source(campaign, "saturated.example.test")
        run = DiscoveryRun.objects.create(campaign=campaign, status="queued")
        DiscoveryJob.objects.bulk_create([
            DiscoveryJob(run=run, source=source, kind="page", status="queued",
                         url=f"https://saturated.example.test/our-team/held-{index}/")
            for index in range(500)
        ], batch_size=200)
        specs = [spec(f"https://saturated.example.test/staff/new-{index}/") for index in range(55)]
        register_common_crawl(campaign, specs)
        self.assertEqual(run.jobs.count(), 500)
        deferred = campaign.urls.filter(url__contains="/staff/new-")
        self.assertEqual(deferred.count(), 55)
        self.assertTrue(all(row.decision == "approved" for row in deferred))
        self.assertTrue(all("already at its page limit" in row.last_result for row in deferred))
        self.assertEqual(deferred.filter(jobs__isnull=False).count(), 0)
        register_common_crawl(campaign, specs)
        self.assertEqual(run.jobs.count(), 500)
        self.assertEqual(campaign.urls.filter(url__contains="/staff/new-").count(), 55)

    def test_penalized_direct_pages_enter_before_remembered_inventory_and_keep_priority(self):
        campaign = self.make_campaign("Replay", max_pages=12, min_score=35)
        remembered = self.make_source(campaign, "inventory.example.test")
        fresh = self.make_source(campaign, "index.example.test")
        for index in range(20):
            self.remember(campaign, remembered, f"/our-team/old-{index}/")
        self.empty_history(campaign, fresh)
        plain = [f"https://index.example.test/our-team/new-{index}/" for index in range(4)]
        labeled = [f"https://index.example.test/staff/named-{index}/" for index in range(2)]
        specs = [spec(url) for url in plain] + [spec(url, "sales representative") for url in labeled]
        run = start(campaign, priority_specs=specs)
        for url in plain:
            job = run.jobs.get(url=url)
            self.assertEqual(job.priority, 20, url)
        for url in labeled:
            job = run.jobs.get(url=url)
            self.assertEqual(job.priority, 40, url)
        self.assertTrue(run.jobs.filter(source=remembered).exists())
        self.assertLessEqual(run.jobs.count(), 12)
        self.assertEqual(run.jobs.filter(url__in=plain + labeled).count(), 6)

    def test_other_origins_still_receive_jobs_when_index_urls_are_admitted_first(self):
        campaign = self.make_campaign("Fair", max_pages=6, min_score=0)
        remembered = self.make_source(campaign, "fair-old.example.test")
        fresh = self.make_source(campaign, "fair-new.example.test")
        for index in range(8):
            self.remember(campaign, remembered, f"/our-team/old-{index}/")
        urls = [f"https://fair-new.example.test/our-team/new-{index}/" for index in range(3)]
        run = start(campaign, priority_specs=[spec(url) for url in urls])
        self.assertEqual(run.jobs.filter(url__in=urls).count(), 3)
        self.assertTrue(run.jobs.filter(source=remembered).exists())

    def test_a_later_run_still_queues_a_productive_origin_after_an_index_batch(self):
        campaign = self.make_campaign("Later", max_pages=4, min_score=0)
        remembered = self.make_source(campaign, "later-old.example.test")
        fresh = self.make_source(campaign, "later-new.example.test")
        waiting = self.remember(campaign, remembered, "/our-team/waiting/")
        first = start(campaign, priority_specs=[spec("https://later-new.example.test/our-team/now/")])
        DiscoveryRun.objects.filter(pk=first.pk).update(status="completed")
        second = start(campaign)
        self.assertTrue(second.jobs.filter(url=waiting.url).exists())

    def test_blocked_dismissed_archived_and_unapproved_urls_are_not_queued(self):
        campaign = self.make_campaign("Protected", max_pages=10, min_score=0)
        blocked = self.make_source(campaign, "blocked.example.test")
        blocked.blocked_at = timezone.now()
        blocked.save(update_fields=["blocked_at"])
        dismissed_source = self.make_source(campaign, "dismissed.example.test")
        archived_source = self.make_source(campaign, "archived.example.test")
        run = DiscoveryRun.objects.create(campaign=campaign, status="queued")
        dismissed_url = "https://dismissed.example.test/our-team/"
        DiscoveredURL.objects.create(
            campaign=campaign, source=dismissed_source, url=dismissed_url,
            origin="https://dismissed.example.test", decision="dismissed", dismissal_scope="url")
        archived_url = "https://archived.example.test/our-team/"
        DiscoveredURL.objects.create(
            campaign=campaign, source=archived_source, url=archived_url,
            origin="https://archived.example.test", decision="approved", archived_at=timezone.now(),
            archive_reason="operator archive")
        register_common_crawl(campaign, [
            spec("https://blocked.example.test/our-team/"),
            spec(dismissed_url),
            spec(archived_url),
            spec("https://unknown.example.test/our-team/"),
        ])
        self.assertFalse(run.jobs.exists())
        self.assertEqual(campaign.urls.get(url=dismissed_url).decision, "dismissed")
        self.assertIsNotNone(campaign.urls.get(url=archived_url).archived_at)
        pending = campaign.urls.get(url="https://unknown.example.test/our-team/")
        self.assertEqual(pending.decision, "pending")
        self.assertFalse(pending.jobs.exists())

    def test_an_unreleased_automatic_source_is_not_queued(self):
        campaign = self.make_campaign("Automatic", max_pages=5, min_score=0)
        source = self.make_source(campaign, "automatic.example.test")
        source.setup_mode = "automatic"
        source.save(update_fields=["setup_mode"])
        run = DiscoveryRun.objects.create(campaign=campaign, status="queued")
        register_common_crawl(campaign, [spec("https://automatic.example.test/our-team/")])
        self.assertFalse(run.jobs.filter(source=source).exists())

    def test_registration_is_idempotent(self):
        campaign = self.make_campaign("Idempotent", max_pages=5, min_score=0)
        self.make_source(campaign, "once.example.test")
        run = DiscoveryRun.objects.create(campaign=campaign, status="queued")
        url = "https://once.example.test/our-team/"
        self.assertEqual(register_common_crawl(campaign, [spec(url)]), 1)
        self.assertEqual(register_common_crawl(campaign, [spec(url)]), 0)
        self.assertEqual(run.jobs.filter(url=url).count(), 1)

    def test_registering_while_the_collector_holds_the_lease_does_not_corrupt_the_queue(self):
        campaign = self.make_campaign("Lease", max_pages=5, min_score=0)
        source = self.make_source(campaign, "lease.example.test")
        run = DiscoveryRun.objects.create(campaign=campaign, status="running")
        token = acquire_lease()
        self.assertIsNotNone(token)
        # Created after the lease is taken, so lease recovery does not requeue it.
        held = DiscoveryJob.objects.create(
            run=run, source=source, kind="page", url="https://lease.example.test/our-team/held/", status="processing")
        url = "https://lease.example.test/staff/added/"
        register_common_crawl(campaign, [spec(url)])
        held.refresh_from_db()
        self.assertEqual(held.status, "processing")
        self.assertEqual(run.jobs.filter(url=url).count(), 1)
        self.assertEqual(WorkerLease.objects.get(key="collector").token, token)
        release_lease(token)


@override_settings(**ENABLED)
class CommonCrawlCommandQueueTests(QueueFixture):
    def test_start_if_idle_counts_index_urls_separately_from_seeds(self):
        campaign = self.make_campaign("Count", max_pages=10, min_score=0)
        self.make_source(campaign, "count.example.test")
        url = "https://count.example.test/our-team/"
        out = StringIO()
        with patch("discovery.management.commands.discovery_commoncrawl.common_crawl_specs",
                   return_value=([spec(url)], {"Count": 1})):
            call_command("discovery_commoncrawl", campaign=campaign.pk, collection="CC-MAIN-2026-35",
                         start_if_idle=True, stdout=out)
        text = out.getvalue()
        self.assertIn("Started run", text)
        self.assertIn("Processed 1 index URL(s); 1 newly registered; 1 approved; 1 queued; 0 deferred.", text)
        self.assertTrue(campaign.runs.get().jobs.filter(url=url).exists())

    def test_start_if_idle_appends_to_an_existing_run(self):
        campaign = self.make_campaign("Append", max_pages=5, min_score=0)
        self.make_source(campaign, "append.example.test")
        run = DiscoveryRun.objects.create(campaign=campaign, status="queued")
        url = "https://append.example.test/our-team/"
        out = StringIO()
        with patch("discovery.management.commands.discovery_commoncrawl.common_crawl_specs",
                   return_value=([spec(url)], {"Append": 1})):
            call_command("discovery_commoncrawl", campaign=campaign.pk, collection="CC-MAIN-2026-35",
                         start_if_idle=True, stdout=out)
        self.assertNotIn("Started run", out.getvalue())
        self.assertIn("1 queued; 0 deferred.", out.getvalue())
        self.assertTrue(run.jobs.filter(url=url).exists())

    def test_start_if_idle_reports_deferral_when_the_open_run_is_full(self):
        campaign = self.make_campaign("Defer", max_pages=1, min_score=0)
        source = self.make_source(campaign, "defer.example.test")
        run = DiscoveryRun.objects.create(campaign=campaign, status="running")
        DiscoveryJob.objects.create(run=run, source=source, kind="page", url="https://defer.example.test/our-team/held/")
        url = "https://defer.example.test/staff/waiting/"
        out = StringIO()
        with patch("discovery.management.commands.discovery_commoncrawl.common_crawl_specs",
                   return_value=([spec(url)], {"Defer": 1})):
            call_command("discovery_commoncrawl", campaign=campaign.pk, collection="CC-MAIN-2026-35",
                         start_if_idle=True, stdout=out)
        self.assertNotIn("Started run", out.getvalue())
        self.assertIn("Processed 1 index URL(s); 1 newly registered; 1 approved; 0 queued; 1 deferred.", out.getvalue())
        self.assertFalse(run.jobs.filter(url=url).exists())

    def test_start_if_idle_marks_index_urls_that_do_not_fit(self):
        campaign = self.make_campaign("Spill", max_pages=2, min_score=0)
        self.make_source(campaign, "spill.example.test")
        urls = [f"https://spill.example.test/our-team/{index}/" for index in range(4)]
        urls.append("https://spill.example.test/contact/")
        out = StringIO()
        with patch("discovery.management.commands.discovery_commoncrawl.common_crawl_specs",
                   return_value=([spec(url) for url in urls], {"Spill": len(urls)})):
            call_command("discovery_commoncrawl", campaign=campaign.pk, collection="CC-MAIN-2026-35",
                         start_if_idle=True, stdout=out)
        text = out.getvalue()
        self.assertIn("Started run", text)
        self.assertIn("Processed 5 index URL(s); 5 newly registered; 5 approved; 1 queued; 3 deferred.", text)
        run = campaign.runs.get()
        self.assertEqual(run.jobs.exclude(kind="search").count(), 2)
        queued = [url for url in urls if run.jobs.filter(url=url).exists()]
        self.assertEqual(len(queued), 1)
        contact = campaign.urls.get(url="https://spill.example.test/contact/")
        self.assertEqual(contact.last_result, "")
        self.assertFalse(run.jobs.filter(url=contact.url).exists())
        for url in urls:
            if url == contact.url or url in queued:
                continue
            self.assertIn("already at its page limit", campaign.urls.get(url=url).last_result)

    def test_a_blocked_url_is_not_counted_as_deferred(self):
        from discovery.services import QUEUE_FULL_RESULT
        campaign = self.make_campaign("Stale", max_pages=5, min_score=0)
        source = self.make_source(campaign, "stale.example.test")
        source.blocked_at = timezone.now()
        source.save(update_fields=["blocked_at"])
        run = DiscoveryRun.objects.create(campaign=campaign, status="running")
        url = "https://stale.example.test/our-team/"
        row = DiscoveredURL.objects.create(
            campaign=campaign, source=source, url=url, origin="https://stale.example.test",
            decision="approved", method="commoncrawl", last_result=QUEUE_FULL_RESULT)
        out = StringIO()
        with patch("discovery.management.commands.discovery_commoncrawl.common_crawl_specs",
                   return_value=([spec(url)], {"Stale": 1})):
            call_command("discovery_commoncrawl", campaign=campaign.pk, collection="CC-MAIN-2026-35",
                         start_if_idle=True, stdout=out)
        self.assertIn("Processed 1 index URL(s); 0 newly registered; 1 approved; 0 queued; 0 deferred.", out.getvalue())
        row.refresh_from_db()
        self.assertEqual(row.last_result, "")
        self.assertEqual(run.jobs.count(), 0)

    def test_an_inactive_campaign_is_not_started(self):
        campaign = self.make_campaign("Inactive", max_pages=5, min_score=0)
        campaign.active = False
        campaign.save(update_fields=["active"])
        self.make_source(campaign, "inactive.example.test")
        with patch("discovery.management.commands.discovery_commoncrawl.common_crawl_specs",
                   return_value=([spec("https://inactive.example.test/our-team/")], {"Inactive": 1})):
            with self.assertRaises(CommandError):
                call_command("discovery_commoncrawl", campaign=campaign.pk, collection="CC-MAIN-2026-35",
                             start_if_idle=True, stdout=StringIO())
        self.assertFalse(campaign.runs.exists())


class IndexRetryTests(QueueFixture):
    def test_an_overloaded_index_is_skipped_after_one_retry(self):
        from discovery.services import common_crawl_specs
        campaign = self.make_campaign("Busy", max_pages=5, min_score=0)
        self.make_source(campaign, "busy.example.test")

        def lookup(host, collection):
            raise FetchError("HTTP 504; retry scheduled.", retryable=True, status=504)

        _, counts = common_crawl_specs(campaign, "CC-MAIN-2026-35", lookup=lookup, pause=0)
        text = " ".join(counts)
        self.assertIn("HTTP 504", text)
        self.assertIn("skipped after one retry", text)
        self.assertNotIn("retry scheduled", text)


@override_settings(**ENABLED)
class ConcurrentRegistrationTests(TransactionTestCase):
    def test_overlapping_registration_leaves_one_job(self):
        campaign = Campaign.objects.create(
            name="Concurrent", keywords="", sales_terms="", min_score=0, max_pages=5,
            use_sitemaps=False, active=True)
        source = Source.objects.create(name="Concurrent", url="https://concurrent.example.test/", approved=True,
                                       allow_homepage=True)
        campaign.sources.add(source)
        run = DiscoveryRun.objects.create(campaign=campaign, status="queued")
        url = "https://concurrent.example.test/our-team/"
        errors = []

        def register():
            connection.close()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("PRAGMA busy_timeout = 5000")
                register_common_crawl(campaign, [spec(url)])
            except Exception as exc:  # noqa: BLE001 - the assertion reports the thread error
                errors.append(exc)
            finally:
                connection.close()

        # Django's in-memory SQLite test database uses a shared cache. One writer can
        # receive "database table is locked" instead of waiting. That must roll back,
        # and a retry must still leave a single job.
        connection.close()
        threads = [threading.Thread(target=register) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertLess(len(errors), 2, errors)
        self.assertTrue(all(isinstance(error, OperationalError) for error in errors), errors)
        self.assertEqual(DiscoveryJob.objects.filter(run=run, url=url).count(), 1)
        self.assertEqual(campaign.urls.filter(url=url).count(), 1)

    def test_tick_beside_registration_keeps_one_job_and_the_processing_row(self):
        later = timezone.now() + timedelta(days=1)
        campaign = Campaign.objects.create(
            name="Lease tick", keywords="", sales_terms="", min_score=0, max_pages=5,
            use_sitemaps=False, active=True, next_due_at=later, next_intake_at=later)
        source = Source.objects.create(
            name="Lease tick", url="https://lease-tick.example.test/", approved=True,
            allow_homepage=True, collector="demo")
        campaign.sources.add(source)
        run = DiscoveryRun.objects.create(campaign=campaign, status="running")
        token = acquire_lease()
        self.assertIsNotNone(token)
        held_url = "https://lease-tick.example.test/our-team/held/"
        held = DiscoveryJob.objects.create(
            run=run, source=source, kind="page", url=held_url, status="processing")
        url = "https://lease-tick.example.test/staff/added/"
        register_common_crawl(campaign, [spec(url)])
        started, release = threading.Event(), threading.Event()
        from discovery.services import demo_response

        def offline_page(page_url):
            if page_url == url:
                started.set()
                self.assertTrue(release.wait(8))
            return demo_response(page_url)

        errors = []

        def run_tick():
            connection.close()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("PRAGMA busy_timeout = 5000")
                tick(token, prefer_discovery=True)
            except Exception as exc:  # noqa: BLE001 - the assertion reports the thread error
                errors.append(exc)
            finally:
                connection.close()

        thread = threading.Thread(target=run_tick)
        with patch("discovery.services.demo_response", side_effect=offline_page):
            thread.start()
            self.assertTrue(started.wait(8))
            register_common_crawl(campaign, [spec(url)])
            release.set()
            thread.join(8)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        held.refresh_from_db()
        self.assertEqual(held.status, "processing")
        self.assertEqual(DiscoveryJob.objects.filter(run=run, url=url).count(), 1)
        self.assertEqual(WorkerLease.objects.get(key="collector").token, token)
        release_lease(token)
