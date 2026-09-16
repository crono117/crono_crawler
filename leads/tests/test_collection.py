from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from leads.models import DomainState, Lead, Observation, PageJob, PageSnapshot, Run, Source, SourceCandidate, WorkerLease
from leads.services.extraction import extract, validate_record
from leads.services.network import Response
from leads.services.storage import save_records
from leads.services.worker import acquire_lease, enqueue, pause_source, release_lease, retry_delay, schedule_due, tick

def demo_source(**kwargs):
    defaults = dict(name="Synthetic test", url="https://example.com/team/", approved=True,
                    company="Example Payments", collector="demo", max_pages=1, follow_links=False)
    defaults.update(kwargs)
    return Source.objects.create(**defaults)

class CollectionTests(TestCase):
    def setUp(self):
        self.source = demo_source()
        self.html = (Path(settings.BASE_DIR) / "examples/demo-team.html").read_text()

    def collect(self):
        run = enqueue(self.source)
        token = acquire_lease()
        try:
            self.assertTrue(tick(token))
        finally:
            release_lease(token)
        run.refresh_from_db()
        return run

    def test_offline_end_to_end_deduplicates_and_refreshes(self):
        run = self.collect()
        self.assertEqual(run.status, "completed")
        self.assertEqual(Lead.objects.count(), 3)
        self.assertEqual(Observation.objects.count(), 3)
        self.assertEqual(run.contacts_seen, 3)
        with patch("leads.services.worker.extract", side_effect=AssertionError("unchanged page must skip extraction")):
            self.collect()
        self.assertEqual(Lead.objects.count(), 3)
        self.assertEqual(PageSnapshot.objects.count(), 1)

    def test_person_and_company_services_are_separate(self):
        records, company_tags = extract(self.html, self.source)
        alex = next(r for r in records if r["name"] == "Alex Example")
        jordan = next(r for r in records if r["name"] == "Jordan Sample")
        self.assertIn("payroll", company_tags)
        self.assertNotIn("payroll", alex["person_tags"])
        self.assertEqual(jordan["person_tags"], ["payroll"])
        self.collect()
        obs = Observation.objects.get(lead__name="Jordan Sample")
        self.assertEqual(obs.source_category, "merchant_services")

    def test_generic_contacts_are_not_treated_as_direct(self):
        records, _ = extract(self.html, self.source)
        taylor = next(r for r in records if r["name"] == "Taylor Fixture")
        self.assertEqual(taylor["contact_scope"], "shared")

    def test_same_shared_email_does_not_merge_people(self):
        records, company_tags = extract(self.html, self.source)
        for record in records:
            record["email"] = "sales@example.com"
            record["contact_scope"] = "shared"
        save_records(self.source, self.source.url, "a" * 64, records, company_tags)
        self.assertEqual(Lead.objects.count(), 3)

    def test_suppression_and_notes_survive_collection(self):
        self.collect()
        lead = Lead.objects.get(name="Alex Example")
        lead.status = "suppressed"
        lead.notes = "User asked not to be contacted."
        lead.save()
        self.collect()
        lead.refresh_from_db()
        self.assertEqual(lead.status, "suppressed")
        self.assertEqual(lead.notes, "User asked not to be contacted.")

    def test_successful_changed_page_marks_missing_observation(self):
        self.collect()
        records, company_tags = extract(self.html, self.source)
        save_records(self.source, self.source.url, "b" * 64, records[1:], company_tags)
        self.assertFalse(Observation.objects.get(lead__name="Alex Example").present)
        self.assertEqual(Lead.objects.count(), 3)

    def test_recipe_change_invalidates_cache(self):
        self.collect()
        self.source.recipe = {"row": ".does-not-exist"}
        self.source.save()
        self.collect()
        self.assertFalse(Observation.objects.filter(present=True).exists())

    def test_unapproved_source_cannot_be_queued(self):
        self.source.approved = False
        self.source.save()
        with self.assertRaises(ValueError):
            enqueue(self.source)

    def test_queue_coalesces_duplicate_runs(self):
        first = enqueue(self.source)
        self.assertEqual(first.pk, enqueue(self.source).pk)
        self.assertEqual(PageJob.objects.count(), 1)

    def test_pause_stops_work_and_resume_reuses_run(self):
        run = enqueue(self.source)
        pause_source(self.source)
        token = acquire_lease()
        self.assertFalse(tick(token))
        self.assertEqual(enqueue(self.source).pk, run.pk)
        self.assertTrue(tick(token))
        release_lease(token)
        self.assertEqual(Lead.objects.count(), 3)

    def test_stale_worker_recovery_requeues_processing_page(self):
        run = enqueue(self.source)
        job = run.jobs.get()
        job.status = "processing"
        job.save()
        WorkerLease.objects.create(key="collector", token="crashed-worker", heartbeat_at=timezone.now() - timedelta(minutes=11))
        token = acquire_lease()
        self.assertIsNotNone(token)
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertTrue(tick(token))
        release_lease(token)
        self.assertEqual(Lead.objects.count(), 3)

    def test_second_worker_cannot_run_while_lease_is_live(self):
        token = acquire_lease()
        self.assertIsNone(acquire_lease())
        release_lease(token)
        self.assertIsNotNone(acquire_lease())

    def test_scheduler_only_queues_due_approved_active_sources(self):
        self.source.active = True
        self.source.save()
        schedule_due()
        self.assertEqual(Run.objects.count(), 1)
        schedule_due()
        self.assertEqual(Run.objects.count(), 1)

    def test_css_extraction_rejects_unsupported_contact(self):
        record = {"name": "Alex Example", "title": "Sales consultant", "email": "invented@example.com", "phone": ""}
        self.assertIsNone(validate_record(record, "Alex Example Sales consultant", self.source))

    def test_non_sales_role_is_excluded_by_default(self):
        record = {"name": "Alex Example", "title": "Software engineer", "email": "alex@example.com"}
        self.assertIsNone(validate_record(record, "Alex Example Software engineer alex@example.com", self.source))

    def test_contact_substrings_do_not_count_as_published_contacts(self):
        record = {"name": "Alex Example", "title": "Sales", "email": "alex@example.com", "phone": "1234567"}
        self.assertIsNone(validate_record(record, "Alex Example Sales notalex@example.com 0123456789", self.source))

    @override_settings(OLLAMA_MODEL="test-model")
    @patch("leads.services.extraction.httpx.Client")
    def test_local_ai_cannot_invent_evidence(self, mock_client):
        import json
        self.source.extractor = "ollama"
        fake = {"people": [{"name": "Invented Person", "title": "Sales", "company": "", "phone": "",
                            "email": "invented@example.com", "evidence": "Invented Person Sales invented@example.com"}]}
        mock_client.return_value.__enter__.return_value.post.return_value.json.return_value = {"message": {"content": json.dumps(fake)}}
        records, _ = extract(self.html, self.source)
        self.assertEqual(records, [])

class HttpPipelineTests(TestCase):
    def setUp(self):
        self.source = demo_source(collector="http", follow_links=True, discover_external=True, max_pages=2, max_depth=1)
        self.token = acquire_lease()
    def tearDown(self):
        release_lease(self.token)
    def ready_domain(self, robots="User-agent: *\nAllow: /"):
        return DomainState.objects.create(origin="https://example.com", robots_text=robots, robots_checked_at=timezone.now())

    @patch("leads.services.worker.fetch")
    def test_robots_is_checked_before_any_page(self, mocked):
        mocked.return_value = Response("https://example.com/robots.txt", 200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /")
        run = enqueue(self.source)
        tick(self.token)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(mocked.call_args.args[0], "https://example.com/robots.txt")
        DomainState.objects.update(next_allowed_at=timezone.now())
        PageJob.objects.update(available_at=timezone.now())
        tick(self.token)
        self.source.refresh_from_db()
        run.refresh_from_db()
        self.assertFalse(self.source.active)
        self.assertEqual(run.status, "paused")
        self.assertEqual(mocked.call_count, 1)

    @patch("leads.services.worker.fetch")
    def test_retry_after_is_persisted_and_domain_is_throttled(self, mocked):
        self.ready_domain()
        mocked.return_value = Response(self.source.url, 429, {"retry-after": "120"}, b"wait")
        run = enqueue(self.source)
        tick(self.token)
        job = run.jobs.get()
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.attempts, 1)
        self.assertGreater(job.available_at, timezone.now() + timedelta(seconds=110))
        self.assertEqual(DomainState.objects.get().next_allowed_at, job.available_at)

    @patch("leads.services.worker.fetch")
    def test_forbidden_source_is_paused_without_bypass(self, mocked):
        self.ready_domain()
        mocked.return_value = Response(self.source.url, 403, {}, b"Forbidden")
        run = enqueue(self.source)
        tick(self.token)
        run.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(run.status, "paused")
        self.assertFalse(self.source.active)

    @patch("leads.services.worker.fetch")
    def test_failed_extraction_does_not_erase_old_evidence(self, mocked):
        self.ready_domain()
        html = (Path(settings.BASE_DIR) / "examples/demo-team.html").read_text()
        records, company_tags = extract(html, self.source)
        save_records(self.source, self.source.url, "a" * 64, records, company_tags)
        mocked.return_value = Response(self.source.url, 200, {"content-type": "text/html"}, html.encode())
        enqueue(self.source)
        with patch("leads.services.worker.extract", side_effect=ValueError("Invalid model output")):
            tick(self.token)
        self.assertEqual(Observation.objects.filter(present=True).count(), 3)
        self.assertFalse(PageSnapshot.objects.exists())

    @patch("leads.services.worker.fetch")
    def test_bounded_discovery_saves_external_links_without_fetching(self, mocked):
        self.ready_domain()
        body = b'<a href="/team/one">one</a><a href="/team/two">two</a><a href="https://vendor.example.org/team">Vendor</a>'
        mocked.return_value = Response(self.source.url, 200, {"content-type": "text/html"}, body)
        run = enqueue(self.source)
        tick(self.token)
        self.assertEqual(run.jobs.count(), 2)
        self.assertEqual(SourceCandidate.objects.get().url, "https://vendor.example.org/team")
        self.assertEqual(mocked.call_count, 1)

    def test_retry_after_invalid_header_is_bounded(self):
        self.assertEqual(retry_delay(1, "nonsense"), 30)
        self.assertEqual(retry_delay(1, "999999999"), 86400)
