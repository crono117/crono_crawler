import gzip
from datetime import timedelta
from unittest.mock import patch
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from leads.models import DomainState, Lead, Observation, PageSnapshot, Source, WorkerLease
from leads.services.network import FetchError, Response
from leads.services.worker import acquire_lease, enqueue, pause_source, release_lease, tick
from discovery.models import Campaign, DailyUsage, DiscoveredURL, DiscoveryJob, DiscoveryRun
from discovery.providers import brave_search, sitemap_entries
from discovery import ranking
from discovery.ranking import CONTACT_EXCLUSIONS, clean_url, rank
from discovery.services import (DEMO_ORIGIN, approve, order_discovery_batch, pause, process, record_links,
                                refresh_priorities, register, register_batch, start)


class DiscoveryBatchOrderingTests(TestCase):
    def test_prefers_unsnapshotted_exact_url(self):
        source = Source.objects.create(name="Ordering source", url="https://a.example/", approved=True)
        fetched = {"url": "https://a.example/fetched", "source_id": source.pk, "priority": 100}
        novel = {"url": "https://a.example/novel", "source_id": source.pk, "priority": 10}
        PageSnapshot.objects.create(source=source, url=fetched["url"], content_hash="fetched", extraction_signature="sig")

        ordered = order_discovery_batch(
            [fetched, novel], url=lambda item: item["url"],
            source_id=lambda item: item["source_id"], priority=lambda item: item["priority"])

        self.assertEqual(ordered, [novel, fetched])

    def test_round_robins_origins_before_spillover(self):
        items = [
            {"name": "A2", "url": "https://a.example/2", "source_id": None, "priority": 20},
            {"name": "A1", "url": "https://a.example/1", "source_id": None, "priority": 30},
            {"name": "B1", "url": "https://b.example/1", "source_id": None, "priority": 10},
            {"name": "A3", "url": "https://a.example/3", "source_id": None, "priority": 5},
        ]

        ordered = order_discovery_batch(
            items, url=lambda item: item["url"], source_id=lambda item: item["source_id"],
            priority=lambda item: item["priority"])

        self.assertEqual([item["name"] for item in ordered], ["A1", "B1", "A2", "A3"])

    def test_single_origin_uses_priority_then_stable_input_order(self):
        items = [
            {"name": "second-tie", "url": "https://a.example/2", "source_id": None, "priority": 20},
            {"name": "first", "url": "https://a.example/1", "source_id": None, "priority": 30},
            {"name": "third-tie", "url": "https://a.example/3", "source_id": None, "priority": 20},
        ]

        ordered = order_discovery_batch(
            items, url=lambda item: item["url"], source_id=lambda item: item["source_id"],
            priority=lambda item: item["priority"])

        self.assertEqual([item["name"] for item in ordered], ["first", "second-tie", "third-tie"])

    def test_origin_with_higher_best_priority_leads_even_if_seen_later(self):
        items = [
            {"name": "A", "url": "https://a.example/team/", "source_id": None, "priority": 10},
            {"name": "B", "url": "https://b.example/team/", "source_id": None, "priority": 80},
        ]

        ordered = order_discovery_batch(
            items, url=lambda item: item["url"], source_id=lambda item: item["source_id"],
            priority=lambda item: item["priority"])

        self.assertEqual([item["name"] for item in ordered], ["B", "A"])

    def test_equal_priority_origins_use_best_item_positions_not_first_origin_sighting(self):
        items = [
            {"name": "A-low", "url": "https://a.example/low", "source_id": None, "priority": 1},
            {"name": "B-best", "url": "https://b.example/best", "source_id": None, "priority": 50},
            {"name": "A-best", "url": "https://a.example/best", "source_id": None, "priority": 50},
        ]

        ordered = order_discovery_batch(
            items, url=lambda item: item["url"], source_id=lambda item: item["source_id"],
            priority=lambda item: item["priority"])

        self.assertEqual([item["name"] for item in ordered], ["B-best", "A-best", "A-low"])

    def test_uses_one_page_snapshot_query_for_many_items(self):
        source = Source.objects.create(name="Query source", url="https://a.example/", approved=True)
        items = [{"url": f"https://a.example/{index}", "source_id": source.pk, "priority": index}
                 for index in range(20)]

        with CaptureQueriesContext(connection) as queries:
            order_discovery_batch(
                items, url=lambda item: item["url"], source_id=lambda item: item["source_id"],
                priority=lambda item: item["priority"])

        snapshot_queries = [query["sql"] for query in queries.captured_queries
                            if "leads_pagesnapshot" in query["sql"].lower()]
        self.assertEqual(len(snapshot_queries), 1)

    def test_preserves_payload_identity_provenance_and_duplicate_order(self):
        first = {"url": "https://a.example/same", "source_id": None, "priority": 10,
                 "label": "first", "context": "alpha", "found_on": "https://seed.example/one",
                 "score": 17, "reasons": ["first reason"]}
        duplicate = {"url": "https://a.example/same", "source_id": None, "priority": 10,
                     "label": "second", "context": "beta", "found_on": "https://seed.example/two",
                     "score": 19, "reasons": ["second reason"]}
        first_before, duplicate_before = first.copy(), duplicate.copy()
        first_before["reasons"], duplicate_before["reasons"] = list(first["reasons"]), list(duplicate["reasons"])

        ordered = order_discovery_batch(
            [first, duplicate], url=lambda item: item["url"], source_id=lambda item: item["source_id"],
            priority=lambda item: item["priority"])

        self.assertIs(ordered[0], first)
        self.assertIs(ordered[1], duplicate)
        self.assertEqual(ordered, [first_before, duplicate_before])


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

    def test_start_queues_each_exact_source_before_sitemap_spillover(self):
        second = Source.objects.create(name="Second directory", url="https://second.example/team/",
                                       approved=True, collector="demo")
        self.campaign.sources.add(second)
        self.campaign.max_pages = 2
        self.campaign.save()

        run = start(self.campaign)

        self.assertEqual(list(run.jobs.order_by("id").values_list("url", flat=True)),
                         [self.source.url, second.url])
        self.assertEqual(list(run.jobs.order_by("id").values_list("priority", flat=True)), [100, 100])

    def test_overlapping_exact_starts_keep_the_source_each_start_represents(self):
        broad = Source.objects.create(name="Broad source", url="https://overlap.example/",
                                      allowed_paths="/", allow_homepage=True, approved=True, collector="demo")
        exact = Source.objects.create(name="Exact source", url="https://overlap.example/team/",
                                      allowed_paths="/team", approved=True, collector="demo")
        campaign = Campaign.objects.create(name="Overlapping starts", use_sitemaps=False, max_pages=2)
        campaign.sources.add(broad, exact)

        run = start(campaign)

        self.assertEqual(run.jobs.get(url=broad.url).source_id, broad.pk)
        self.assertEqual(run.jobs.get(url=exact.url).source_id, exact.pk)
        self.assertEqual(campaign.urls.get(url=broad.url).source_id, broad.pk)
        self.assertEqual(campaign.urls.get(url=exact.url).source_id, exact.pk)
        for job in run.jobs.order_by("id"):
            response = Response(job.url, 200, {"content-type": "text/html"}, b"<h1>No contacts</h1>")
            with patch("discovery.services.demo_response", return_value=response):
                process(job)
        self.assertTrue(PageSnapshot.objects.filter(source=broad, url=broad.url).exists())
        self.assertTrue(PageSnapshot.objects.filter(source=exact, url=exact.url).exists())

    def test_start_remembered_prefers_novel_url_over_higher_score_repeat(self):
        self.campaign.use_sitemaps, self.campaign.max_pages = False, 2
        self.campaign.save()
        repeated = DiscoveredURL.objects.create(
            campaign=self.campaign, url=DEMO_ORIGIN + "/sales/team/", origin=DEMO_ORIGIN,
            source=self.source, decision="approved", label="Merchant services sales representatives")
        novel = DiscoveredURL.objects.create(
            campaign=self.campaign, url=DEMO_ORIGIN + "/team/", origin=DEMO_ORIGIN,
            source=self.source, decision="approved", label="Team")
        PageSnapshot.objects.create(source=self.source, url=repeated.url,
                                    content_hash="seen", extraction_signature="sig")

        run = start(self.campaign)

        self.assertTrue(run.jobs.filter(url=novel.url).exists())
        self.assertFalse(run.jobs.filter(url=repeated.url).exists())

    def test_start_remembered_single_origin_uses_all_remaining_capacity(self):
        self.campaign.use_sitemaps, self.campaign.max_pages = False, 4
        self.campaign.save()
        remembered = [DiscoveredURL.objects.create(
            campaign=self.campaign, url=f"{DEMO_ORIGIN}/team/{index}/", origin=DEMO_ORIGIN,
            source=self.source, decision="approved", label="Team") for index in range(3)]

        run = start(self.campaign)

        self.assertEqual(run.jobs.count(), 4)
        self.assertEqual(set(run.jobs.values_list("url", flat=True)),
                         {self.source.url, *(candidate.url for candidate in remembered)})

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

    def test_batch_registration_preserves_caps_duplicates_and_metadata(self):
        self.campaign.max_candidates, self.campaign.max_new_domains = 4, 1
        self.campaign.save()
        run = DiscoveryRun.objects.create(campaign=self.campaign, status="running")
        existing = DiscoveredURL.objects.create(
            campaign=self.campaign, first_run=run, url="https://existing.example.org/team/",
            origin="https://existing.example.org", label="Old", decision="pending")
        specs = [
            {"url": existing.url, "label": "Updated", "context": "Updated context",
             "found_on": "https://ref.example/one"},
            {"url": DEMO_ORIGIN + "/team/new/", "label": "Approved team"},
            {"url": "https://new.example.org/team/one", "label": "First new origin"},
            {"url": "https://new.example.org/team/two", "label": "Same new origin"},
            {"url": "https://blocked.example.org/team/", "label": "Second new origin"},
            {"url": "http://127.0.0.1/team/", "label": "Invalid"},
        ]

        results = register_batch(run, specs)

        self.assertEqual([candidate and candidate.url for candidate in results], [
            existing.url, DEMO_ORIGIN + "/team/new/", "https://new.example.org/team/one",
            "https://new.example.org/team/two", None, None,
        ])
        existing.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual((existing.label, existing.context, existing.found_on),
                         ("Updated", "Updated context", "https://ref.example/one"))
        self.assertEqual(self.campaign.urls.count(), 4)
        self.assertEqual((run.duplicates_seen, run.candidates_found, run.new_domains), (1, 3, 1))
        self.assertEqual(run.jobs.count(), 1)
        self.assertEqual(run.jobs.get().url, DEMO_ORIGIN + "/team/new/")

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

    def test_record_links_round_robins_approved_origins_before_spillover(self):
        second = Source.objects.create(name="Second approved", url="https://second.example/",
                                       approved=True, collector="demo")
        self.campaign.sources.add(second)
        self.campaign.use_sitemaps, self.campaign.max_pages = False, 2
        self.campaign.save()
        run = DiscoveryRun.objects.create(campaign=self.campaign, status="running")
        parent = DiscoveryJob(run=run, source=self.source, depth=0)
        html = """
            <ul>
              <li>Alpha context <a href="/team/alpha/">Alpha Team</a></li>
              <li>Second alpha <a href="/team/beta/">Beta Team</a></li>
              <li>Bravo context <a href="https://second.example/team/bravo/">Bravo Team</a></li>
            </ul>
        """

        record_links(parent, html, self.source.url)

        self.assertEqual(list(run.jobs.order_by("id").values_list("url", flat=True)),
                         [DEMO_ORIGIN + "/team/alpha/", "https://second.example/team/bravo/"])
        bravo = self.campaign.urls.get(url="https://second.example/team/bravo/")
        self.assertEqual((bravo.label, bravo.context, bravo.found_on),
                         ("Bravo Team", "Bravo context Bravo Team", self.source.url))

    def test_record_links_batches_registration_queries(self):
        self.campaign.use_sitemaps, self.campaign.max_pages = False, 20
        self.campaign.save()
        lead = Lead.objects.create(identity="reviewed-batch-source", name="Reviewed", status="reviewed")
        Observation.objects.create(lead=lead, source=self.source, page_url=self.source.url, facts={},
            source_category=self.source.category, evidence="Reviewed", content_hash="reviewed-batch", present=True)
        run = DiscoveryRun.objects.create(campaign=self.campaign, status="running")
        parent = DiscoveryJob(run=run, source=self.source, depth=0)
        html = "".join(
            f'<a href="/team/{index}/">Merchant services sales team {index}</a>'
            for index in range(10)
        )

        with CaptureQueriesContext(connection) as queries:
            record_links(parent, html, self.source.url)

        sql = [query["sql"].lower() for query in queries.captured_queries]
        observation_queries = [query for query in sql if "leads_observation" in query]
        campaign_source_queries = [query for query in sql
                                   if "discovery_campaign_sources" in query and "leads_source" in query]
        yield_queries = [query for query in sql if 'as "fetched"' in query]
        per_item_job_counts = [query for query in sql
                               if "count(" in query and "discovery_discoveryjob" in query
                               and query not in yield_queries]
        self.assertEqual(len(observation_queries), 1)
        self.assertEqual(len(campaign_source_queries), 1)
        self.assertEqual(len(yield_queries), 1)  # one batched origin-yield aggregate, not per item
        self.assertEqual(per_item_job_counts, [])
        self.assertLessEqual(len(queries), 70)
        self.assertEqual(run.jobs.count(), 10)
        self.assertEqual(self.campaign.urls.count(), 10)

    def test_start_stops_remembered_queue_attempts_when_page_capacity_is_full(self):
        self.campaign.use_sitemaps, self.campaign.max_pages = False, 1
        self.campaign.save()
        remembered = [DiscoveredURL.objects.create(
            campaign=self.campaign, url=f"{DEMO_ORIGIN}/team/remembered-{index}/", origin=DEMO_ORIGIN,
            source=self.source, decision="approved", label="Team", score=35)
            for index in range(1000)]
        before = list(self.campaign.urls.order_by("id").values_list("id", "url", "decision", "source_id"))
        from discovery.services import queue_candidate

        with (patch("discovery.services.refresh_priorities", return_value=remembered),
              patch("discovery.services.queue_candidate", wraps=queue_candidate) as queue):
            run = start(self.campaign)

        self.assertEqual(queue.call_count, 1)
        self.assertEqual(run.jobs.exclude(kind="search").count(), 1)
        self.assertEqual(
            list(self.campaign.urls.exclude(method="seed").order_by("id")
                 .values_list("id", "url", "decision", "source_id")), before)

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

    def test_sales_collateral_with_high_context_remains_approved_but_unqueued(self):
        self.campaign.use_sitemaps, self.campaign.min_score = False, 0
        self.campaign.save()
        run = start(self.campaign)
        for path, label in (("sales-sheet", "Sales Sheet"), ("sales-deck", "Sales Deck"),
                            ("sales-playbook", "Sales Playbook"), ("sales-brochure", "Sales Brochure"),
                            ("product-collateral", "Sales representatives product collateral")):
            with self.subTest(path=path):
                candidate = register(run, f"{DEMO_ORIGIN}/{path}/", label=label,
                                     context="Merchant services sales team representatives")
                self.assertEqual(candidate.decision, "approved")
                self.assertFalse(candidate.jobs.exists())

    def test_document_host_link_with_direct_sounding_label_does_not_queue(self):
        source = Source.objects.create(name="Reviewed document host", url="https://docs.google.com/",
                                       approved=True, collector="demo")
        campaign = Campaign.objects.create(name="Document host", min_score=0, use_sitemaps=False)
        campaign.sources.add(source)
        run = DiscoveryRun.objects.create(campaign=campaign)
        candidate = register(run, "https://docs.google.com/presentation/d/example/",
                             label="Merchant services sales representatives",
                             context="Meet our sales team")
        self.assertEqual(candidate.decision, "approved")
        self.assertFalse(candidate.jobs.exists())

    def test_exact_reviewed_starting_url_queues_despite_unconventional_path(self):
        source = Source.objects.create(name="Reviewed exact start",
            url="https://reviewed.example.org/resources/quarterly-playbook/", approved=True, collector="demo")
        campaign = Campaign.objects.create(name="Exact start", min_score=100, use_sitemaps=False)
        campaign.sources.add(source)
        run = start(campaign)
        candidate = campaign.urls.get(url=source.url)
        self.assertEqual(candidate.method, "seed")
        self.assertGreaterEqual(candidate.score, 90)
        self.assertIn("Explicit starting source", candidate.reasons)
        self.assertTrue(candidate.jobs.exists())

    def test_existing_exact_candidate_becomes_explicit_seed_without_losing_seed_score(self):
        source = Source.objects.create(name="Existing exact start",
            url="https://existing.example.org/resources/quarterly-playbook/",
            approved=True, collector="demo")
        campaign = Campaign.objects.create(name="Existing exact start", min_score=100, use_sitemaps=False)
        campaign.sources.add(source)
        DiscoveredURL.objects.create(campaign=campaign, url=source.url, origin="https://existing.example.org",
                                     source=source, decision="approved", method="link")

        start(campaign)

        candidate = campaign.urls.get()
        self.assertEqual(candidate.method, "seed")
        self.assertGreaterEqual(candidate.score, 90)
        self.assertIn("Explicit starting source", candidate.reasons)
        self.assertTrue(candidate.jobs.exists())

    def test_canonical_exact_start_queues_with_host_case_tracking_and_query_order_changes(self):
        source = Source.objects.create(name="Canonical exact start",
            url="https://EXAMPLE.org/resources/quarterly-playbook/?z=2&utm_source=review&a=1",
            approved=True, collector="demo")
        campaign = Campaign.objects.create(name="Canonical exact start", min_score=100, use_sitemaps=False)
        campaign.sources.add(source)
        run = start(campaign)
        candidate = campaign.urls.get()
        self.assertEqual(candidate.url, "https://example.org/resources/quarterly-playbook/?a=1&z=2")
        self.assertTrue(candidate.jobs.exists())

    def test_canonical_exact_start_dispatches_with_host_case_tracking_and_query_order_changes(self):
        source = Source.objects.create(name="Canonical dispatch",
            url="https://EXAMPLE.org/resources/quarterly-playbook/?z=2&utm_source=review&a=1",
            approved=True, collector="demo")
        campaign = Campaign.objects.create(name="Canonical dispatch", min_score=100, use_sitemaps=False, active=True)
        campaign.sources.add(source)
        run = DiscoveryRun.objects.create(campaign=campaign, status="running")
        url = "https://example.org/resources/quarterly-playbook/?a=1&z=2"
        candidate = DiscoveredURL.objects.create(campaign=campaign, first_run=run, url=url,
            origin="https://example.org", source=source, decision="approved", label="Quarterly playbook")
        job = DiscoveryJob.objects.create(run=run, candidate=candidate, source=source, url=url)
        response = Response(url, 200, {"content-type": "text/html"}, b"<h1>No contacts</h1>")
        with patch("discovery.services.demo_response", return_value=response) as fetch:
            process(job)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(DiscoveryJob.objects.get(pk=job.pk).status, "done")

    def test_sitemap_prefers_novel_exact_url_without_weakening_scope(self):
        source = Source.objects.create(name="Scoped sitemap", url="https://maps.example/team/",
                                       allowed_paths="/team\n/sitemap.xml", approved=True, collector="demo")
        campaign = Campaign.objects.create(name="Sitemap novelty", max_pages=2, min_score=0)
        campaign.sources.add(source)
        run = DiscoveryRun.objects.create(campaign=campaign, status="running")
        sitemap_url = "https://maps.example/sitemap.xml"
        sitemap_candidate = DiscoveredURL.objects.create(
            campaign=campaign, first_run=run, url=sitemap_url, origin="https://maps.example",
            source=source, decision="approved", kind="sitemap")
        job = DiscoveryJob.objects.create(run=run, candidate=sitemap_candidate, source=source,
                                          kind="sitemap", url=sitemap_url, priority=95)
        repeated = "https://maps.example/team/sales/representatives/"
        novel = "https://maps.example/team/people/"
        outside = "https://maps.example/private/team/"
        PageSnapshot.objects.create(source=source, url=repeated,
                                    content_hash="seen", extraction_signature="sig")
        body = ("<urlset>" + "".join(f"<url><loc>{url}</loc></url>" for url in (repeated, novel, outside)) +
                "</urlset>").encode()
        response = Response(sitemap_url, 200, {"content-type": "application/xml"}, body)

        with patch("discovery.services.demo_response", return_value=response):
            process(job)

        self.assertTrue(run.jobs.filter(url=novel).exists())
        self.assertFalse(run.jobs.filter(url=repeated).exists())
        outside_candidate = campaign.urls.get(url=outside)
        self.assertIsNone(outside_candidate.source_id)
        self.assertEqual(outside_candidate.decision, "pending")
        self.assertFalse(outside_candidate.jobs.exists())

    def test_loaded_job_reloads_revoked_source_before_fetch(self):
        self.campaign.use_sitemaps = False
        self.campaign.save()
        run = start(self.campaign)
        job = DiscoveryJob.objects.select_related("source", "run__campaign", "candidate").get()
        Source.objects.filter(pk=self.source.pk).update(approved=False)
        with patch("discovery.services.demo_response") as fetch:
            process(job)
        fetch.assert_not_called()
        self.assertEqual(DiscoveryJob.objects.get(pk=job.pk).status, "skipped")

    def test_loaded_job_reloads_narrowed_source_scope_before_fetch(self):
        self.campaign.use_sitemaps = False
        self.campaign.save()
        run = start(self.campaign)
        job = DiscoveryJob.objects.select_related("source", "run__campaign", "candidate").get()
        Source.objects.filter(pk=self.source.pk).update(allowed_paths="/different", allow_homepage=False)
        with patch("discovery.services.demo_response") as fetch:
            process(job)
        fetch.assert_not_called()
        self.assertEqual(DiscoveryJob.objects.get(pk=job.pk).status, "skipped")

    def test_reviewed_source_bonus_keeps_threshold_page_queued_and_dispatchable(self):
        self.campaign.use_sitemaps, self.campaign.min_score = False, 40
        self.campaign.save()
        lead = Lead.objects.create(identity="reviewed-source", name="Reviewed Person", status="reviewed")
        Observation.objects.create(lead=lead, source=self.source, page_url=self.source.url, facts={},
            source_category=self.source.category, evidence="Reviewed", content_hash="reviewed", present=True)
        run = start(self.campaign)
        candidate = register(run, DEMO_ORIGIN + "/team/", label="Team")
        self.assertEqual(candidate.score, 45)
        self.assertIn("Source has human-reviewed contacts (+10)", candidate.reasons)
        job = candidate.jobs.get()
        response = Response(candidate.url, 200, {"content-type": "text/html"}, b"<h1>No contacts</h1>")
        with patch("discovery.services.demo_response", return_value=response) as fetch:
            process(job)
        self.assertEqual(fetch.call_count, 1)
        candidate.refresh_from_db()
        self.assertEqual(candidate.score, 45)
        self.assertEqual(candidate.reasons.count("Source has human-reviewed contacts (+10)"), 1)

    def test_refresh_priorities_precomputes_reviewed_sources_once(self):
        lead = Lead.objects.create(identity="reviewed-priority-source", name="Reviewed", status="reviewed")
        Observation.objects.create(lead=lead, source=self.source, page_url=self.source.url, facts={},
            source_category=self.source.category, evidence="Reviewed", content_hash="reviewed-priorities", present=True)
        DiscoveredURL.objects.bulk_create([
            DiscoveredURL(campaign=self.campaign, url=f"{DEMO_ORIGIN}/team/{index}/",
                          origin=DEMO_ORIGIN, source=self.source, decision="approved", label="Team")
            for index in range(8)
        ])
        with CaptureQueriesContext(connection) as queries:
            candidates = refresh_priorities(self.campaign)
        observation_queries = [query["sql"] for query in queries.captured_queries
                               if "leads_observation" in query["sql"].lower()]
        yield_queries = [query["sql"] for query in queries.captured_queries
                         if 'as "fetched"' in query["sql"].lower()]
        self.assertEqual(len(observation_queries), 1)
        self.assertEqual(len(yield_queries), 1)
        self.assertLessEqual(len(queries), 4)
        for candidate in candidates:
            self.assertEqual(candidate.score, 45)
            self.assertEqual(candidate.reasons.count("Source has human-reviewed contacts (+10)"), 1)

    def test_start_precomputes_reviewed_sources_once_for_remembered_candidates(self):
        self.campaign.use_sitemaps, self.campaign.max_pages = False, 50
        self.campaign.save()
        lead = Lead.objects.create(identity="reviewed-start-source", name="Reviewed", status="reviewed")
        Observation.objects.create(lead=lead, source=self.source, page_url=self.source.url, facts={},
            source_category=self.source.category, evidence="Reviewed", content_hash="reviewed-start", present=True)
        DiscoveredURL.objects.bulk_create([
            DiscoveredURL(campaign=self.campaign, url=f"{DEMO_ORIGIN}/team/{index}/",
                          origin=DEMO_ORIGIN, source=self.source, decision="approved", label="Team")
            for index in range(8)
        ])
        remembered = list(self.campaign.urls.select_related("source"))
        with (patch("discovery.services.register_batch"),
              patch("discovery.services.refresh_priorities", return_value=remembered),
              patch("discovery.services.finish"),
              CaptureQueriesContext(connection) as queries):
            run = start(self.campaign)
        observation_queries = [query["sql"] for query in queries.captured_queries
                               if "leads_observation" in query["sql"].lower()]
        self.assertEqual(len(observation_queries), 1)
        self.assertEqual(run.jobs.count(), len(remembered))

    def test_approve_precomputes_reviewed_sources_once_for_sibling_queueing(self):
        self.campaign.active, self.campaign.max_pages = True, 50
        self.campaign.save()
        run = DiscoveryRun.objects.create(campaign=self.campaign, status="running")
        lead = Lead.objects.create(identity="reviewed-approve-source", name="Reviewed", status="reviewed")
        Observation.objects.create(lead=lead, source=self.source, page_url=self.source.url, facts={},
            source_category=self.source.category, evidence="Reviewed", content_hash="reviewed-approve", present=True)
        DiscoveredURL.objects.bulk_create([
            DiscoveredURL(campaign=self.campaign, first_run=run, url=f"{DEMO_ORIGIN}/team/{index}/",
                          origin=DEMO_ORIGIN, decision="pending", label="Team")
            for index in range(8)
        ])
        candidate = self.campaign.urls.order_by("id").first()
        with CaptureQueriesContext(connection) as queries:
            approve(candidate, self.source)
        observation_queries = [query["sql"] for query in queries.captured_queries
                               if "leads_observation" in query["sql"].lower()]
        self.assertEqual(len(observation_queries), 1)
        self.assertEqual(run.jobs.count(), self.campaign.urls.count())

    def test_stale_ordinary_job_without_current_intent_skips_before_fetch(self):
        self.campaign.use_sitemaps, self.campaign.min_score = False, 35
        self.campaign.save()
        run = start(self.campaign)
        candidate = register(run, DEMO_ORIGIN + "/resources/", label="Sales team")
        self.assertTrue(candidate.jobs.exists())
        candidate.label = "Quarterly sales playbook"
        candidate.context = "Merchant services sales representatives"
        candidate.save(update_fields=["label", "context"])
        from discovery.services import demo_response
        with patch("discovery.services.demo_response", wraps=demo_response) as fetch:
            self.finish(run)
        self.assertNotIn(candidate.url, [call.args[0] for call in fetch.call_args_list])
        self.assertEqual(candidate.jobs.get().status, "skipped")

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
    def test_bare_generic_terms_do_not_establish_direct_contact_intent(self):
        for term in ("dealer", "partner", "agent", "executive", "contact", "about", "directory", "member"):
            with self.subTest(term=term):
                self.assertFalse(ranking.direct_contact_page_intent(f"https://example.com/{term}/", term.title()))

    def test_direct_contact_intent_accepts_strong_compound_and_person_role_pages(self):
        cases = (
            ("https://example.com/team/", ""),
            ("https://example.com/staff/", ""),
            ("https://example.com/people/", ""),
            ("https://example.com/profile/alex-example/", ""),
            ("https://example.com/bio/alex-example/", ""),
            ("https://example.com/biography/alex-example/", ""),
            ("https://example.com/leadership/", ""),
            ("https://example.com/representative/alex-example/", ""),
            ("https://example.com/rep/alex-example/", ""),
            ("https://example.com/agents/", ""),
            ("https://example.com/reps/", ""),
            ("https://example.com/representatives/", ""),
            ("https://example.com/sales/", "Sales team"),
            ("https://example.com/sales/", "Sales representative"),
            ("https://example.com/sales/", "Sales rep"),
            ("https://example.com/sales/", "Sales agents"),
        )
        for url, label in cases:
            with self.subTest(url=url, label=label):
                self.assertTrue(ranking.direct_contact_page_intent(url, label))

    def test_direct_contact_intent_rejects_generic_role_and_noise_usage(self):
        cases = (
            ("https://example.com/agent/", "Agent"),
            ("https://example.com/representative/", "Representative"),
            ("https://example.com/rep/login/", "Rep login"),
            ("https://example.com/agents/login/", "Agent login"),
            ("https://example.com/business-agents/", "Business agents"),
            ("https://example.com/partner/portal/", "Partner portal"),
            ("https://example.com/collateral/", "Sales representatives"),
            ("https://example.com/documents/", "Sales team document sharing"),
            ("https://example.com/social/", "Meet our agents"),
            ("https://example.com/deck/", "Sales representatives"),
            ("https://example.com/sheet/", "Sales representatives"),
            ("https://example.com/playbook/", "Sales representatives"),
            ("https://example.com/brochure/", "Sales representatives"),
            ("https://example.com/scheduling/", "Sales representatives"),
            ("https://notion.site/sales-team/", "Sales team"),
            ("https://workspace.notion.so/sales-team/", "Sales team"),
            ("https://bsky.app/profile/example.com", "Sales representative"),
        )
        for url, label in cases:
            with self.subTest(url=url, label=label):
                self.assertFalse(ranking.direct_contact_page_intent(url, label))

    def test_direct_contact_intent_rejects_sales_collateral_and_hosted_documents(self):
        campaign = Campaign()
        cases = (
            ("https://example.com/sales-sheet/", "Sales Sheet"),
            ("https://example.com/resources/", "Sales Deck"),
            ("https://example.com/playbooks/sales/", "Quarterly Playbook"),
            ("https://docs.google.com/presentation/d/example", "Sales representatives"),
            ("https://calendly.com/example/sales", "Schedule with sales"),
            ("https://linkedin.com/in/example", "Sales representative"),
        )
        for url, label in cases:
            with self.subTest(url=url):
                score, reasons = rank(campaign, url, label, "Our team and sales representatives")
                self.assertFalse(ranking.direct_contact_page_intent(url, label))
                self.assertFalse(any("(+35)" in reason for reason in reasons))

    def test_direct_contact_intent_accepts_people_and_explicit_sales_team_pages(self):
        campaign = Campaign()
        for url, label in (
            ("https://example.com/sales/team/", ""),
            ("https://example.com/resources/", "Sales representatives"),
            ("https://example.com/profile/alex/", ""),
            ("https://example.com/leadership/", ""),
        ):
            with self.subTest(url=url, label=label):
                self.assertTrue(ranking.direct_contact_page_intent(url, label))
                self.assertGreaterEqual(rank(campaign, url, label)[0], 35)

    def test_context_never_establishes_direct_contact_intent(self):
        campaign = Campaign(keywords="merchant services", sales_terms="sales\nrepresentative")
        score, reasons = rank(campaign, "https://example.com/resources/", "Quarterly update",
                              "Meet our merchant services sales team representatives")
        self.assertFalse(ranking.direct_contact_page_intent("https://example.com/resources/", "Quarterly update"))
        self.assertEqual(score, 45)
        self.assertFalse(any("(+35)" in reason for reason in reasons))

    def test_contact_filters_match_plural_paths_and_social_hosts(self):
        campaign = Campaign(exclusions="\n".join(CONTACT_EXCLUSIONS))
        for path in ("/blog/", "/blogs/", "/articles/", "/products/", "/software/", "/emv-credit-card-machines/device/"):
            with self.subTest(path=path):
                self.assertLess(rank(campaign, "https://example.com" + path, "Sales team")[0], 0)
        for url in ("https://www.facebook.com/", "https://linkedin.com/in/example", "https://x.com/company"):
            with self.subTest(url=url):
                self.assertLess(rank(campaign, url, "Sales team")[0], 0)
        for path in ("/team/", "/sales/team/", "/agents/", "/representatives/", "/reps/", "/profile/alex/", "/leadership/"):
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

    @override_settings(BRAVE_SEARCH_ENABLED=True, BRAVE_SEARCH_API_KEY="synthetic-key")
    @patch("discovery.services.brave_search")
    def test_search_admission_round_robins_origins_and_preserves_metadata(self, search):
        search.return_value = [
            {"url": "https://a.example/team/one", "title": "A one", "description": "A first description"},
            {"url": "https://a.example/team/two", "title": "A two", "description": "A second description"},
            {"url": "https://b.example/team/one", "title": "B one", "description": "B first description"},
        ]
        campaign = Campaign.objects.create(name="Search diversity", active=True, search_enabled=True,
                                           search_queries="merchant services", max_candidates=2)
        run = DiscoveryRun.objects.create(campaign=campaign, status="running")
        job = DiscoveryJob.objects.create(run=run, kind="search", url="merchant services", priority=85)

        process(job)

        self.assertEqual(list(campaign.urls.order_by("id").values_list("url", flat=True)),
                         ["https://a.example/team/one", "https://b.example/team/one"])
        b_result = campaign.urls.get(url="https://b.example/team/one")
        self.assertEqual((b_result.label, b_result.context, b_result.search_query, b_result.method),
                         ("B one", "B first description", "merchant services", "search"))
        self.assertIsNone(b_result.source_id)
        self.assertEqual(b_result.decision, "pending")
        self.assertFalse(b_result.jobs.exists())

    @override_settings(BRAVE_SEARCH_ENABLED=True, BRAVE_SEARCH_API_KEY="synthetic-key")
    def test_stale_claimed_search_job_reloads_disabled_inactive_campaign_before_any_search_work(self):
        campaign = Campaign.objects.create(name="Stale search", active=True, search_enabled=True,
                                           search_queries="merchant services")
        run = DiscoveryRun.objects.create(campaign=campaign, status="running")
        DiscoveryJob.objects.create(run=run, kind="search", url="merchant services", status="processing")
        job = DiscoveryJob.objects.select_related("run__campaign").get()
        Campaign.objects.filter(pk=campaign.pk).update(active=False, search_enabled=False)

        with (patch("discovery.services.search_ready", side_effect=AssertionError("readiness checked")) as ready,
              patch("discovery.services.reserve", side_effect=AssertionError("search budget reserved")) as reserve,
              patch("discovery.services.brave_search", side_effect=AssertionError("provider called")) as search):
            process(job)

        ready.assert_not_called()
        reserve.assert_not_called()
        search.assert_not_called()
        self.assertFalse(DailyUsage.objects.exists())
        self.assertFalse(DomainState.objects.filter(origin="https://api.search.brave.com").exists())
        campaign.refresh_from_db()
        self.assertFalse(campaign.active)
        self.assertFalse(campaign.search_enabled)
        self.assertEqual(DiscoveryJob.objects.get(pk=job.pk).status, "skipped")

    @override_settings(BRAVE_SEARCH_ENABLED=True, BRAVE_SEARCH_API_KEY="synthetic-key")
    @patch("discovery.services.brave_search")
    def test_search_skips_each_malformed_or_unsafe_result_without_discarding_valid_results(self, search):
        search.return_value = [
            {},
            {"url": 123, "title": "Non-string"},
            {"url": "https://example.com:444/team/", "title": "Invalid port"},
            {"url": "http://127.0.0.1/team/", "title": "Private"},
            {"url": "http://localhost/team/", "title": "Local"},
            {"url": "https://example.com/team/../private/", "title": "Traversal"},
            {"url": "https://example.com/team/?session=secret", "title": "Session"},
            {"url": "https://EXAMPLE.com/team/?utm_source=brave#people",
             "title": "<b>Valid Team</b>", "description": "<p>Valid description</p>"},
            {"url": "http://[::1", "title": "Malformed"},
        ]
        campaign = Campaign.objects.create(name="Mixed search", active=True, search_enabled=True,
                                           search_queries="merchant services")
        run = DiscoveryRun.objects.create(campaign=campaign, status="running")
        job = DiscoveryJob.objects.create(run=run, kind="search", url="merchant services")

        process(job)

        self.assertEqual(DiscoveryJob.objects.get(pk=job.pk).status, "done")
        self.assertEqual(campaign.urls.count(), 1)
        candidate = campaign.urls.get()
        self.assertEqual(candidate.url, "https://example.com/team/")
        self.assertEqual((candidate.label, candidate.context, candidate.search_query, candidate.method),
                         ("Valid Team", "Valid description", "merchant services", "search"))

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
