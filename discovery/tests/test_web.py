from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from discovery.forms import CampaignForm
from discovery.models import Campaign, DiscoveredURL
from discovery.services import DEMO_ORIGIN, register, start
from leads.models import Source


class DiscoveryWebTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("discovery-operator", password="Test-only-password-12345", is_staff=True)
        cls.source = Source.objects.create(name="Example directory", url=DEMO_ORIGIN + "/", approved=True, collector="demo")
        cls.campaign = Campaign.objects.create(name="Payments discovery")
        cls.campaign.sources.add(cls.source)

    def data(self, **overrides):
        data = {"name": "New campaign", "category": "merchant_services", "sources": [self.source.pk],
                "keywords": "merchant services", "sales_terms": "sales", "exclusions": "", "region": "",
                "max_pages": 25, "max_depth": 2, "max_candidates": 500, "max_new_domains": 10,
                "daily_requests": 100, "min_score": 10, "interval_hours": 24, "daily_search_limit": 5}
        data.update(overrides)
        return data

    def test_staff_authentication_is_required(self):
        for name in ("home", "new", "candidates"):
            self.assertEqual(self.client.get(reverse("discovery:" + name)).status_code, 302)
        nonstaff = get_user_model().objects.create_user("ordinary")
        self.client.force_login(nonstaff)
        self.assertEqual(self.client.get(reverse("discovery:home")).status_code, 302)

    def test_pages_render_and_campaign_can_be_started_and_paused(self):
        self.client.force_login(self.user)
        run = start(self.campaign)
        candidate = register(run, "https://vendor.example.org/team", label="Vendor sales team")
        routes = [("home", []), ("new", []), ("campaign", [self.campaign.pk]), ("edit", [self.campaign.pk]),
                  ("candidates", []), ("candidate", [candidate.pk]), ("run", [run.pk])]
        for name, args in routes:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse("discovery:" + name, args=args)).status_code, 200)
        url = reverse("discovery:action", args=[self.campaign.pk])
        self.client.post(url, {"action": "pause"})
        self.campaign.refresh_from_db()
        self.assertFalse(self.campaign.active)
        self.client.post(url, {"action": "start"})
        self.campaign.refresh_from_db()
        self.assertTrue(self.campaign.active)

    def test_campaign_post_csrf_and_server_side_limits(self):
        self.client.force_login(self.user)
        url = reverse("discovery:action", args=[self.campaign.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(url, {"action": "start"}).status_code, 403)
        form = CampaignForm(self.data(max_pages=100000, max_depth=8, daily_requests=0))
        self.assertFalse(form.is_valid())
        for field in ("max_pages", "max_depth", "daily_requests"):
            self.assertIn(field, form.errors)

    def test_saving_campaign_pauses_and_edit_cancels_old_run(self):
        self.client.force_login(self.user)
        run = start(self.campaign)
        self.client.post(reverse("discovery:edit", args=[self.campaign.pk]), self.data())
        self.campaign.refresh_from_db()
        run.refresh_from_db()
        self.assertFalse(self.campaign.active)
        self.assertEqual(run.status, "cancelled")

    def test_search_requires_configuration_and_unapproved_seeds_rejected(self):
        form = CampaignForm(self.data(search_enabled="on", search_queries="merchant services"))
        self.assertFalse(form.is_valid())
        self.assertIn("search_enabled", form.errors)
        self.source.approved = False
        self.source.save()
        form = CampaignForm(self.data())
        self.assertFalse(form.is_valid())
        self.assertIn("sources", form.errors)

    def test_review_approval_and_origin_dismissal(self):
        self.client.force_login(self.user)
        run = start(self.campaign)
        candidate = register(run, "https://vendor.example.org/team")
        sibling = register(run, "https://vendor.example.org/team/other")
        source = Source.objects.create(name="Reviewed vendor", url=candidate.url, allowed_paths="/team", approved=True)
        url = reverse("discovery:candidate", args=[candidate.pk])
        self.client.post(url, {"action": "approve", "source": source.pk})
        candidate.refresh_from_db()
        self.assertEqual(candidate.decision, "approved")
        self.client.post(url, {"action": "dismiss_origin"})
        sibling.refresh_from_db()
        self.assertEqual(sibling.decision, "dismissed")

    @patch("leads.forms.public_addresses", return_value=["8.8.8.8"])
    def test_new_source_review_attaches_campaign_and_preserves_exact_url(self, dns):
        self.client.force_login(self.user)
        run = start(self.campaign)
        candidate = register(run, "https://vendor.example.org/team")
        url = reverse("source_new") + f"?discovery_url={candidate.pk}"
        self.assertContains(self.client.get(url), candidate.url)
        data = {"name": "Reviewed vendor", "url": candidate.url, "category": "merchant_services", "collector": "http",
                "extractor": "rules", "allowed_paths": "/team", "interval_hours": 168, "delay_seconds": 5,
                "max_pages": 5, "max_depth": 1, "approved": "on", "approval_notes": "Synthetic reviewed source"}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        candidate.refresh_from_db()
        self.assertEqual(candidate.decision, "approved")
        self.assertTrue(self.campaign.sources.filter(pk=candidate.source_id).exists())

    @patch("leads.forms.public_addresses", return_value=["8.8.8.8"])
    def test_source_review_rejects_mismatched_origin_without_error(self, dns):
        self.client.force_login(self.user)
        candidate = register(start(self.campaign), "https://vendor.example.org/team")
        data = {"name": "Wrong domain", "url": "https://other.example.org/", "category": "merchant_services", "collector": "http",
                "extractor": "rules", "allowed_paths": "/", "interval_hours": 168, "delay_seconds": 5,
                "max_pages": 5, "max_depth": 1, "approved": "on", "approval_notes": "Review"}
        response = self.client.post(reverse("source_new") + f"?discovery_url={candidate.pk}", data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must cover the discovered URL")
        self.assertFalse(Source.objects.filter(name="Wrong domain").exists())
