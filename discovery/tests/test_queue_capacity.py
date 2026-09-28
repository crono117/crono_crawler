from django.test import TestCase
from django.utils import timezone

from discovery.models import Campaign, DiscoveredURL, DiscoveryRun
from discovery.services import register_batch, start
from leads.models import PageSnapshot, Source


class CandidateCapacityTests(TestCase):
    def test_noise_does_not_spend_inventory_or_new_domain_slots_in_the_same_batch(self):
        campaign = Campaign.objects.create(name="Noise batch", max_candidates=10, max_new_domains=1)
        run = DiscoveryRun.objects.create(campaign=campaign)
        noise = [{"url": f"https://facebook.com/fixture-{index}"} for index in range(10)]
        wanted = {"url": "https://payments.example.org/team/", "label": "Merchant services sales team"}
        results = register_batch(run, noise + [wanted])
        run.refresh_from_db()
        self.assertIsNotNone(results[-1])
        self.assertEqual(results[-1].decision, "pending")
        self.assertEqual(campaign.saved_url_count(), 1)
        self.assertEqual(campaign.urls.filter(decision="dismissed").count(), 10)
        self.assertEqual(run.new_domains, 1)
        self.assertEqual(run.jobs.count(), 0)  # registration never grants permission to fetch

    def test_noise_cannot_spend_next_calls_domain_allowance_or_hide_a_useful_origin(self):
        campaign = Campaign.objects.create(name="Domain count", max_new_domains=1)
        run = DiscoveryRun.objects.create(campaign=campaign)
        register_batch(run, [{"url": "https://payments.example.org/privacy-policy"}])
        run.refresh_from_db()
        self.assertEqual(run.new_domains, 0)
        result = register_batch(run, [{"url": "https://payments.example.org/team/", "label": "Team"}])[0]
        run.refresh_from_db()
        self.assertIsNotNone(result)
        self.assertEqual(run.new_domains, 1)
        self.assertIsNone(register_batch(run, [{"url": "https://other.example.org/team/"}])[0])

    def test_newly_filtered_existing_row_releases_its_slot_inside_the_batch(self):
        campaign = Campaign.objects.create(name="Reclassified noise", max_candidates=1)
        run = DiscoveryRun.objects.create(campaign=campaign)
        old = DiscoveredURL.objects.create(campaign=campaign, url="https://facebook.com/fixture",
                                           origin="https://facebook.com", decision="pending")
        results = register_batch(run, [{"url": old.url}, {"url": "https://payments.example.org/team/"}])
        self.assertEqual(results[0].decision, "dismissed")
        self.assertIsNotNone(results[1])
        self.assertEqual(campaign.saved_url_count(), 1)

    def test_operator_dismissals_keep_their_capacity_and_are_never_revived(self):
        campaign = Campaign.objects.create(name="Operator decision", max_candidates=1)
        run = DiscoveryRun.objects.create(campaign=campaign)
        old = DiscoveredURL.objects.create(campaign=campaign, url="https://payments.example.org/team/",
            origin="https://payments.example.org", decision="dismissed", dismissal_scope="origin")
        results = register_batch(run, [{"url": old.url}, {"url": "https://other.example.org/team/"}])
        self.assertEqual(results[0].dismissal_scope, "origin")
        self.assertIsNone(results[1])
        self.assertEqual(campaign.saved_url_count(), 1)


class RunAllocationTests(TestCase):
    def setUp(self):
        self.campaign = Campaign.objects.create(name="Fresh-page allocation", max_pages=4, use_sitemaps=True)
        self.sources = [Source.objects.create(name=f"Fixture {index}",
            url=f"https://site{index}.example.org/", approved=True) for index in range(2)]
        self.campaign.sources.add(*self.sources)
        for source in self.sources:
            PageSnapshot.objects.create(source=source, url=source.url, content_hash="fixture", extraction_signature="fixture")

    def profile(self, source, path, **kwargs):
        return DiscoveredURL.objects.create(campaign=self.campaign, source=source, url=source.url + path,
            origin=source.url.rstrip("/"), label="Merchant services sales team", decision="approved", **kwargs)

    def test_unseen_profile_gets_capacity_before_repeated_seeds_and_sitemaps(self):
        novel = self.profile(self.sources[0], "people/alex")
        run = start(self.campaign)
        self.assertTrue(run.jobs.filter(url=novel.url).exists())
        self.assertTrue(all(run.jobs.filter(url=source.url).exists() for source in self.sources))
        self.assertEqual(run.jobs.count(), 4)
        self.assertEqual(run.jobs.filter(kind="sitemap").count(), 1)

    def test_fresh_allowance_rotates_origins_and_keeps_room_for_refreshes(self):
        for source in self.sources:
            for index in range(5):
                self.profile(source, f"people/rep-{index}")
        run = start(self.campaign)
        profiles = run.jobs.filter(url__contains="/people/")
        self.assertEqual(profiles.count(), 2)
        self.assertEqual(set(profiles.values_list("source_id", flat=True)), {source.pk for source in self.sources})
        self.assertTrue(all(run.jobs.filter(url=source.url).exists() for source in self.sources))
        self.assertEqual(run.jobs.count(), self.campaign.max_pages)

    def test_reservation_never_bypasses_review_setup_scope_or_intent_gates(self):
        source = self.sources[0]
        dismissed = self.profile(source, "people/dismissed")
        dismissed.decision, dismissed.dismissal_scope = "dismissed", "url"
        dismissed.save()
        manual = self.profile(source, "people/manual", manual_review_required=True)
        generic = self.profile(source, "pricing")
        generic.label = "Pricing"
        generic.save()
        source.allowed_paths = "/people"
        source.allow_homepage = True
        source.save()
        outside = self.profile(source, "team/outside")
        source2 = self.sources[1]
        source2.setup_mode = "automatic"
        source2.save()
        gated = self.profile(source2, "people/not-released")
        run = start(self.campaign)
        self.assertFalse(run.jobs.filter(candidate__in=[dismissed, manual, generic, outside, gated]).exists())
        self.assertTrue(run.jobs.filter(url=source.url).exists())
        self.assertLessEqual(run.jobs.count(), self.campaign.max_pages)

    def test_blocked_source_is_not_brought_back_by_reservation(self):
        blocked = self.sources[0]
        blocked.blocked_at = timezone.now()
        blocked.save()
        candidate = self.profile(blocked, "people/blocked")
        run = start(self.campaign)
        self.assertFalse(run.jobs.filter(source=blocked).exists())
        candidate.refresh_from_db()
        self.assertEqual(candidate.decision, "approved")
