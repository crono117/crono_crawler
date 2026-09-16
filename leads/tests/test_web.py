from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from leads.models import Lead
from leads.services.worker import acquire_lease, enqueue, release_lease, tick
from .test_collection import demo_source

class WebTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("operator", password="Test-only-long-password-123", is_staff=True)
        self.source = demo_source()
        self.run = enqueue(self.source)
        token = acquire_lease()
        tick(token)
        release_lease(token)

    def test_anonymous_cannot_access_contacts_or_export(self):
        for route in ("dashboard", "sources", "leads", "runs", "candidates", "export"):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route)).status_code, 302)

    def test_nonstaff_account_cannot_access_console(self):
        other = get_user_model().objects.create_user("nonstaff", password="Test-only-long-password-123")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("leads")).status_code, 302)

    def test_pages_render_with_collected_evidence(self):
        self.client.force_login(self.user)
        lead = Lead.objects.first()
        routes = [("dashboard", []), ("sources", []), ("source_detail", [self.source.pk]), ("source_new", []),
                  ("leads", []), ("lead_detail", [lead.pk]), ("runs", []), ("run_detail", [self.run.pk]), ("candidates", [])]
        for route, args in routes:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route, args=args)).status_code, 200)
        response = self.client.get(reverse("lead_detail", args=[lead.pk]))
        self.assertContains(response, "Source evidence")
        self.assertContains(response, "Seen on last successful check")

    def test_mutations_require_post_and_csrf(self):
        self.client.force_login(self.user)
        url = reverse("source_action", args=[self.source.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(url, {"action": "pause"}).status_code, 403)

    def test_filters_distinguish_source_and_person_services(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("leads"), {"service": "payroll"})
        self.assertContains(response, "Jordan Sample")
        self.assertNotContains(response, "Alex Example")
        response = self.client.get(reverse("leads"), {"category": "merchant_services"})
        self.assertContains(response, "Jordan Sample")
        self.assertContains(response, "Alex Example")

    def test_csv_excludes_suppressed_and_escapes_formulas(self):
        self.client.force_login(self.user)
        Lead.objects.filter(name="Alex Example").update(status="suppressed")
        Lead.objects.filter(name="Jordan Sample").update(notes="=HYPERLINK(\"https://example.com\")")
        response = self.client.get(reverse("export"))
        text = response.content.decode()
        self.assertNotIn("Alex Example", text)
        self.assertIn("Jordan Sample", text)
        self.assertIn("'=HYPERLINK", text)

    @patch("leads.forms.public_addresses", return_value=["8.8.8.8"])
    def test_valid_source_can_be_saved_paused(self, dns):
        self.client.force_login(self.user)
        response = self.client.post(reverse("source_new"), {"name": "Real source configuration", "url": "https://public.example.org/team/",
            "category": "merchant_services", "collector": "http", "extractor": "rules", "allowed_paths": "/team",
            "interval_hours": 168, "delay_seconds": 5, "max_pages": 5, "max_depth": 1, "approved": "on",
            "approval_notes": "Source reviewed by operator."})
        self.assertEqual(response.status_code, 302)
        from leads.models import Source
        source = Source.objects.get(name="Real source configuration")
        self.assertFalse(source.active)
        self.assertTrue(source.approved)

    def test_review_suppression_is_saved(self):
        self.client.force_login(self.user)
        lead = Lead.objects.first()
        response = self.client.post(reverse("lead_detail", args=[lead.pk]), {"status": "suppressed", "notes": "Do not contact."})
        self.assertEqual(response.status_code, 302)
        lead.refresh_from_db()
        self.assertEqual(lead.status, "suppressed")

    @patch("leads.forms.public_addresses", return_value=["8.8.8.8"])
    def test_crawl_limits_are_enforced_on_the_server(self, dns):
        from leads.forms import SourceForm
        form = SourceForm(data={"name": "Overlarge", "url": "https://limits.example.org/",
            "category": "merchant_services", "collector": "http", "extractor": "rules", "allowed_paths": "/",
            "interval_hours": 0, "delay_seconds": 0, "max_pages": 100000, "max_depth": 20})
        self.assertFalse(form.is_valid())
        for name in ("interval_hours", "delay_seconds", "max_pages", "max_depth"):
            self.assertIn(name, form.errors)
