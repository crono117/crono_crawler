"""Yield penalties change priority. They do not veto an already-eligible direct page."""
from django.test import TestCase

from discovery.models import Campaign, DiscoveredURL, DiscoveryJob, DiscoveryRun
from discovery.ranking import current_candidate_score, ordinary_page_eligible
from leads.models import Source

ORIGIN = "https://yield-gate.example.test"


class YieldGateTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="Yield gate", url=ORIGIN + "/", approved=True)
        self.campaign = Campaign.objects.create(
            name="Yield gate", keywords="", sales_terms="sales representative", min_score=35)
        self.campaign.sources.add(self.source)
        self.run = DiscoveryRun.objects.create(campaign=self.campaign, status="completed")

    def empty_history(self, pages=3):
        for index in range(pages):
            url = f"{ORIGIN}/staff/history-{index}/"
            candidate = DiscoveredURL.objects.create(
                campaign=self.campaign, url=url, origin=ORIGIN, source=self.source,
                decision="approved", label="Staff")
            DiscoveryJob.objects.create(
                run=self.run, candidate=candidate, source=self.source, url=url, status="done",
                contacts_seen=0)

    def test_penalty_lowers_priority_but_a_direct_page_stays_eligible(self):
        self.empty_history()
        url = ORIGIN + "/our-team/"
        score, reasons = current_candidate_score(self.campaign, url, "")
        eligible, eligible_score, _ = ordinary_page_eligible(self.campaign, url, "")
        self.assertEqual(score, 20)  # +35 direct, then -15, not raised back to the minimum
        self.assertEqual(eligible_score, 20)
        self.assertTrue(eligible)
        self.assertTrue(any("no validated contacts" in reason for reason in reasons))

    def test_same_page_on_a_productive_source_keeps_the_bonus(self):
        for index in range(2):
            url = f"{ORIGIN}/staff/paid-{index}/"
            candidate = DiscoveredURL.objects.create(
                campaign=self.campaign, url=url, origin=ORIGIN, source=self.source,
                decision="approved")
            DiscoveryJob.objects.create(
                run=self.run, candidate=candidate, source=self.source, url=url, status="done",
                contacts_seen=1)
        score, reasons = current_candidate_score(self.campaign, ORIGIN + "/our-team/", "")
        eligible, _, _ = ordinary_page_eligible(self.campaign, ORIGIN + "/our-team/", "")
        self.assertEqual(score, 45)  # +35 direct and +10 proven yield
        self.assertTrue(eligible)
        self.assertTrue(any("stored contacts" in reason for reason in reasons))

    def test_a_new_direct_page_with_no_history_meets_the_threshold(self):
        eligible, score, reasons = ordinary_page_eligible(self.campaign, ORIGIN + "/our-team/", "")
        self.assertEqual(score, 35)
        self.assertTrue(eligible)
        self.assertFalse(any("no validated contacts" in reason for reason in reasons))

    def test_a_sales_label_keeps_its_extra_priority_after_the_penalty(self):
        self.empty_history()
        score, _ = current_candidate_score(
            self.campaign, ORIGIN + "/staff/ada/", "sales representative")
        self.assertEqual(score, 40)  # +35 direct, +20 role, -15 yield

    def test_homepage_and_contact_form_stay_below_the_gate(self):
        self.empty_history()
        for url, label in ((ORIGIN + "/", "Home"), (ORIGIN + "/contact/", "Contact form")):
            eligible, score, _ = ordinary_page_eligible(self.campaign, url, label)
            self.assertFalse(eligible, url)
            self.assertLess(score, self.campaign.min_score)

    def test_article_blog_and_job_paths_stay_excluded(self):
        self.campaign.exclusions = "path:blog\npath:article\npath:jobs"
        self.campaign.save()
        self.empty_history()
        for url in (ORIGIN + "/blog/our-team/", ORIGIN + "/article/sales-team/",
                    ORIGIN + "/jobs/sales-representative/"):
            score, _ = current_candidate_score(self.campaign, url, "sales representative")
            eligible, _, _ = ordinary_page_eligible(self.campaign, url, "sales representative")
            self.assertEqual(score, -100, url)
            self.assertFalse(eligible, url)

    def test_a_careers_path_keeps_both_penalties_until_an_exclusion_rejects_it(self):
        self.empty_history()
        url = ORIGIN + "/careers/sales-representative/"
        eligible, score, reasons = ordinary_page_eligible(self.campaign, url, "sales representative")
        # +35 direct, +20 role, -20 editorial, -15 yield. The editorial penalty remains,
        # and the yield penalty is not erased by raising the score to the minimum.
        self.assertEqual(score, 20)
        self.assertTrue(any("Editorial" in reason for reason in reasons))
        self.assertTrue(eligible)
        self.campaign.exclusions = "path:careers"
        self.campaign.save()
        excluded, excluded_score, _ = ordinary_page_eligible(self.campaign, url, "sales representative")
        self.assertEqual(excluded_score, -100)
        self.assertFalse(excluded)

    def test_penalty_on_one_origin_does_not_change_another(self):
        self.empty_history()
        other = Source.objects.create(name="Fresh", url="https://fresh-gate.example.test/", approved=True)
        self.campaign.sources.add(other)
        penalized, _ = current_candidate_score(self.campaign, ORIGIN + "/our-team/", "")
        fresh, _ = current_candidate_score(self.campaign, "https://fresh-gate.example.test/our-team/", "")
        self.assertEqual((penalized, fresh), (20, 35))
        self.assertLess(penalized, fresh)
