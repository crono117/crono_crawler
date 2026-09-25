from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from discovery.models import Campaign, DiscoveredURL, DiscoveryJob, DiscoveryRun
from discovery.ranking import current_candidate_score, ordinary_page_eligible
from discovery.services import refresh_priorities
from discovery.yields import EMPTY, OriginYield, origin_yields, safe_origin, yield_adjustment
from leads.models import Source

ORIGIN = "https://yield.example"


class YieldFixtureMixin:
    def setUp(self):
        self.source = Source.objects.create(name="Yield source", url=ORIGIN + "/", approved=True)
        self.campaign = Campaign.objects.create(name="Yield campaign", keywords="", sales_terms="")
        self.campaign.sources.add(self.source)
        self.run = DiscoveryRun.objects.create(campaign=self.campaign, status="completed")

    def page(self, path, contacts, status="done", kind="page", run=None, campaign=None):
        campaign = campaign or self.campaign
        run = run or self.run
        url = ORIGIN + path
        candidate, _ = DiscoveredURL.objects.get_or_create(
            campaign=campaign, url=url,
            defaults={"origin": ORIGIN, "source": self.source, "decision": "approved", "label": "Team"})
        return DiscoveryJob.objects.create(run=run, candidate=candidate, source=self.source, kind=kind,
                                           url=url, status=status, contacts_seen=contacts)


class YieldAdjustmentRuleTests(TestCase):
    def test_no_history_is_neutral(self):
        self.assertEqual(yield_adjustment(EMPTY), (0, ""))

    def test_low_yield_needs_three_empty_pages(self):
        self.assertEqual(yield_adjustment(OriginYield(2, 0))[0], 0)
        delta, reason = yield_adjustment(OriginYield(3, 0))
        self.assertEqual(delta, -15)
        self.assertIn("3 fetched pages", reason)

    def test_proven_needs_two_productive_pages_and_half_rate(self):
        self.assertEqual(yield_adjustment(OriginYield(1, 1))[0], 0)
        self.assertEqual(yield_adjustment(OriginYield(10, 2))[0], 0)
        self.assertEqual(yield_adjustment(OriginYield(3, 2))[0], 10)

    def test_rate_is_smoothed(self):
        self.assertEqual(OriginYield(0, 0).rate, 0.5)
        self.assertAlmostEqual(OriginYield(1, 0).rate, 1 / 3)

    def test_safe_origin_never_raises(self):
        self.assertEqual(safe_origin("https://A.example/x"), "https://a.example")
        self.assertEqual(safe_origin("not a url\x00"), "")
        self.assertEqual(safe_origin(None), "")


class OriginYieldQueryTests(YieldFixtureMixin, TestCase):
    def test_counts_distinct_done_page_urls(self):
        self.page("/team/a/", 2)
        self.page("/team/a/", 2, run=DiscoveryRun.objects.create(campaign=self.campaign, status="completed"))
        self.page("/team/b/", 0)
        self.assertEqual(origin_yields({ORIGIN})[ORIGIN], OriginYield(fetched=2, productive=1))

    def test_ignores_failures_skips_and_sitemaps(self):
        self.page("/team/a/", 0, status="failed")
        self.page("/team/b/", 0, status="skipped")
        self.page("/sitemap.xml", 0, kind="sitemap")
        self.assertEqual(origin_yields({ORIGIN}), {})

    def test_history_is_shared_across_campaigns(self):
        other = Campaign.objects.create(name="Other", keywords="", sales_terms="")
        other_run = DiscoveryRun.objects.create(campaign=other, status="completed")
        for index in range(3):
            self.page(f"/team/{index}/", 0, run=other_run, campaign=other)
        self.assertEqual(origin_yields({ORIGIN})[ORIGIN].fetched, 3)

    @override_settings(DISCOVERY_YIELD_WINDOW_DAYS=30)
    def test_old_runs_fall_outside_window(self):
        old = DiscoveryRun.objects.create(campaign=self.campaign, status="completed")
        DiscoveryRun.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=31))
        for index in range(3):
            self.page(f"/team/{index}/", 0, run=old)
        self.assertEqual(origin_yields({ORIGIN}), {})


class YieldScoringTests(YieldFixtureMixin, TestCase):
    url = ORIGIN + "/our-team/"

    def test_low_yield_origin_is_demoted_but_still_eligible(self):
        base, _ = current_candidate_score(self.campaign, self.url, "Our team")
        for index in range(3):
            self.page(f"/staff/{index}/", 0)
        eligible, score, reasons = ordinary_page_eligible(self.campaign, self.url, "Our team")
        self.assertEqual(score, base - 15)
        self.assertTrue(eligible)  # priority change only; direct intent still decides
        self.assertTrue(any("no validated contacts" in reason for reason in reasons))

    def test_penalty_never_turns_a_page_into_an_exclusion(self):
        for index in range(3):
            self.page(f"/staff/{index}/", 0)
        score, _ = current_candidate_score(self.campaign, ORIGIN + "/misc/", "")
        self.assertEqual(score, 0)  # clamped; negative scores mean excluded/dismissed

    def test_proven_origin_is_boosted(self):
        base, _ = current_candidate_score(self.campaign, self.url, "Our team")
        self.page("/staff/1/", 3)
        self.page("/staff/2/", 1)
        score, reasons = current_candidate_score(self.campaign, self.url, "Our team")
        self.assertEqual(score, base + 10)
        self.assertTrue(any("stored contacts on 2 of 2" in reason for reason in reasons))

    def test_bonus_cannot_create_direct_intent(self):
        self.page("/staff/1/", 3)
        self.page("/staff/2/", 1)
        eligible, _, _ = ordinary_page_eligible(self.campaign, ORIGIN + "/pricing/", "Pricing")
        self.assertFalse(eligible)

    def test_explicit_exclusion_still_wins(self):
        self.campaign.exclusions = "path:our-team"
        self.page("/staff/1/", 3)
        self.page("/staff/2/", 1)
        score, _ = current_candidate_score(self.campaign, self.url, "Our team")
        self.assertEqual(score, -100)

    def test_refresh_priorities_applies_yield(self):
        for index in range(3):
            self.page(f"/staff/{index}/", 0)
        candidate = DiscoveredURL.objects.create(campaign=self.campaign, url=self.url, origin=ORIGIN,
                                                 source=self.source, decision="approved", label="Our team")
        refresh_priorities(self.campaign)
        candidate.refresh_from_db()
        self.assertTrue(any("no validated contacts" in reason for reason in candidate.reasons))


class DiscoveryYieldCommandTests(YieldFixtureMixin, TestCase):
    def test_reports_harvest_rate_by_method_query_and_origin(self):
        self.page("/team/a/", 2)
        self.page("/team/b/", 0)
        DiscoveredURL.objects.filter(url=ORIGIN + "/team/b/").update(method="search", search_query='"our team" ISO')
        out = StringIO()
        call_command("discovery_yield", stdout=out)
        text = out.getvalue()
        self.assertIn("Harvest rate: 1/2 pages (50%); 0 new contact(s)", text)
        self.assertIn('"our team" ISO', text)
        self.assertIn(ORIGIN, text)

    def test_campaign_filter_and_empty_state(self):
        other = Campaign.objects.create(name="Empty", keywords="", sales_terms="")
        out = StringIO()
        call_command("discovery_yield", campaign=other.pk, stdout=out)
        self.assertIn("No completed page jobs", out.getvalue())


    def test_reports_new_contacts_per_group(self):
        job = self.page("/team/a/", 3)
        job.new_contacts = 2
        job.save()
        out = StringIO()
        call_command("discovery_yield", stdout=out)
        self.assertIn("Harvest rate: 1/1 pages (100%); 2 new contact(s)", out.getvalue())
        self.assertIn("1/1 pages (100%), 2 new", out.getvalue())


class CollectorBridgeTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="Directory", url="https://dir.example/", approved=True)
        self.campaign = Campaign.objects.create(name="Bridge", keywords="", sales_terms="")
        self.campaign.sources.add(self.source)

    def link(self, url, status="new", source=None, age_days=0):
        from leads.models import SourceCandidate
        row = SourceCandidate.objects.create(url=url, label="Merchant services sales team",
                                             discovered_from=source or self.source,
                                             evidence_url="https://dir.example/", status=status)
        if age_days:
            SourceCandidate.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(days=age_days))
        return row

    def test_start_imports_recent_unreviewed_collector_links(self):
        from discovery.services import start
        other = Source.objects.create(name="Elsewhere", url="https://else.example/", approved=True)
        self.link("https://rep-one.example/our-team/")
        self.link("https://rep-two.example/our-team/", status="dismissed")
        self.link("https://rep-three.example/our-team/", age_days=400)
        self.link("https://rep-four.example/our-team/", source=other)
        start(self.campaign)
        imported = set(self.campaign.urls.filter(method="collector").values_list("url", flat=True))
        self.assertEqual(imported, {"https://rep-one.example/our-team/"})
        self.assertEqual(self.campaign.urls.get(url="https://rep-one.example/our-team/").decision, "pending")

    def test_bridge_needs_active_campaign_with_open_run(self):
        from discovery.services import bridge_collector_links
        links = [("https://rep.example/our-team/", "Sales team")]
        self.assertEqual(bridge_collector_links(self.source, links, "https://dir.example/"), 0)
        self.campaign.active = True
        self.campaign.save()
        self.assertEqual(bridge_collector_links(self.source, links, "https://dir.example/"), 0)  # no open run
        DiscoveryRun.objects.create(campaign=self.campaign)
        self.assertEqual(bridge_collector_links(self.source, links, "https://dir.example/"), 1)
        self.assertTrue(self.campaign.urls.filter(url="https://rep.example/our-team/", method="collector").exists())
        self.assertEqual(bridge_collector_links(self.source, links, "https://dir.example/"), 1)
        self.assertEqual(self.campaign.urls.filter(url="https://rep.example/our-team/").count(), 1)  # deduped

    def test_bridge_respects_exclusions(self):
        from discovery.services import bridge_collector_links
        self.campaign.active, self.campaign.exclusions = True, "domain:rep.example"
        self.campaign.save()
        DiscoveryRun.objects.create(campaign=self.campaign)
        bridge_collector_links(self.source, [("https://rep.example/our-team/", "Sales team")], "https://dir.example/")
        candidate = self.campaign.urls.get(url="https://rep.example/our-team/")
        self.assertEqual(candidate.decision, "dismissed")
        self.assertLess(candidate.score, 0)
        self.assertFalse(candidate.jobs.exists())


class SearchQueryAllocationTests(TestCase):
    Q_GOOD, Q_BAD, Q_NEW = '"our team" merchant services', "payments blog", '"meet the team" ISO'

    def setUp(self):
        self.source = Source.objects.create(name="Found", url=ORIGIN + "/", approved=True)
        self.campaign = Campaign.objects.create(name="Search", keywords="", sales_terms="")
        self.run = DiscoveryRun.objects.create(campaign=self.campaign, status="completed")

    def found(self, query, path, contacts):
        candidate = DiscoveredURL.objects.create(campaign=self.campaign, url=ORIGIN + path, origin=ORIGIN,
            source=self.source, decision="approved", method="search", search_query=query)
        DiscoveryJob.objects.create(run=self.run, candidate=candidate, source=self.source, kind="page",
                                    url=candidate.url, status="done", contacts_seen=contacts)

    def history(self):
        for index in range(4):
            self.found(self.Q_GOOD, f"/team/{index}/", 2)
            self.found(self.Q_BAD, f"/blog/{index}/", 0)

    def test_query_yields_group_by_search_query(self):
        from discovery.yields import query_yields
        self.history()
        stats = query_yields({self.Q_GOOD, self.Q_BAD, self.Q_NEW})
        self.assertEqual(stats[self.Q_GOOD], OriginYield(4, 4))
        self.assertEqual(stats[self.Q_BAD], OriginYield(4, 0))
        self.assertNotIn(self.Q_NEW, stats)

    def test_sampling_stays_in_band_and_favors_proven_queries(self):
        import random
        from discovery.yields import search_priority
        rng = random.Random(7)
        good = [search_priority(OriginYield(10, 9), rng) for _ in range(300)]
        bad = [search_priority(OriginYield(10, 0), rng) for _ in range(300)]
        new = [search_priority(EMPTY, rng) for _ in range(300)]
        for values in (good, bad, new):
            self.assertTrue(all(80 <= value <= 90 for value in values))
        self.assertGreater(sum(good) / 300, sum(new) / 300)
        self.assertGreater(sum(new) / 300, sum(bad) / 300)  # untried queries still get explored
        self.assertGreater(len(set(new)), 3)

    @override_settings(BRAVE_SEARCH_ENABLED=True, BRAVE_SEARCH_API_KEY="synthetic-key")
    def test_start_orders_search_jobs_by_sampled_yield(self):
        from unittest.mock import patch
        from discovery.services import start
        self.history()
        self.campaign.search_enabled = True
        self.campaign.search_queries = "\n".join([self.Q_BAD, self.Q_NEW, self.Q_GOOD])
        self.campaign.save()
        mean = lambda stats, rng=None: 80 + round(10 * stats.rate)  # deterministic stand-in for sampling
        with patch("discovery.yields.search_priority", side_effect=mean):
            run = start(self.campaign)
        jobs = list(run.jobs.filter(kind="search").order_by("-priority", "id").values_list("url", "priority", "message"))
        self.assertEqual([url for url, _, _ in jobs], [self.Q_GOOD, self.Q_NEW, self.Q_BAD])
        self.assertIn("4/4 fetched result pages", jobs[0][2])
