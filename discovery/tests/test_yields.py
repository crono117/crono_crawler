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
