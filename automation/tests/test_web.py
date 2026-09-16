import json
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core import signing
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from automation.coordination import token_hash, verify_bundle
from automation.models import CoordinatorClient, SiteAutomationJob
from automation.services import pause_job
from discovery.models import Campaign
from .test_pipeline import AutomationCase


class AutomationWebTests(AutomationCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user("site-operator", is_staff=True)
        self.api_token = "synthetic-coordinator-secret-" + "a" * 40
        self.coordinator = CoordinatorClient.objects.create(name="Fixture coordinator", campaign=self.campaign,
            token_hash=token_hash(self.api_token))
        self.job = self.candidate().source.automation_job

    def api_get(self, suffix="recon", token=None, **extra):
        return self.client.get(f"/automation/api/jobs/{self.job.pk}/{suffix}/",
            HTTP_AUTHORIZATION="Bearer " + (token or self.api_token), **extra)

    def test_console_is_staff_only_and_actions_require_csrf(self):
        for path in ("/automation/", f"/automation/jobs/{self.job.pk}/", f"/automation/policies/{self.campaign.pk}/"):
            self.assertEqual(self.client.get(path).status_code, 302)
        ordinary = get_user_model().objects.create_user("ordinary-user")
        self.client.force_login(ordinary)
        self.assertEqual(self.client.get("/automation/").status_code, 302)
        self.client.force_login(self.user)
        for path in ("/automation/", f"/automation/jobs/{self.job.pk}/", f"/automation/policies/{self.campaign.pk}/"):
            self.assertEqual(self.client.get(path).status_code, 200)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(reverse("automation:action", args=[self.job.pk]), {"action": "pause"}).status_code, 403)

    def test_metadata_api_requires_token_and_campaign_scope(self):
        self.run_until(self.job)
        self.assertEqual(self.client.get(f"/automation/api/jobs/{self.job.pk}/recon/").status_code, 401)
        report = self.api_get()
        self.assertEqual(report.status_code, 200)
        self.assertNotIn("alex@example.com", report.content.decode())
        self.assertNotIn("Alex Example", report.content.decode())
        other = Campaign.objects.create(name="Different campaign")
        self.coordinator.campaign = other
        self.coordinator.save()
        self.assertEqual(self.api_get().status_code, 404)
        self.coordinator.active = False
        self.coordinator.save()
        self.assertEqual(self.api_get().status_code, 401)

    def test_remote_plain_http_and_forged_proxy_header_are_rejected(self):
        self.assertEqual(self.api_get(REMOTE_ADDR="8.8.8.8").status_code, 403)
        self.assertEqual(self.api_get(REMOTE_ADDR="8.8.8.8", HTTP_X_FORWARDED_PROTO="https").status_code, 403)
        self.assertEqual(self.api_get(REMOTE_ADDR="8.8.8.8", secure=True).status_code, 200)

    def test_signed_bundle_binds_scope_and_rejects_tampering(self):
        self.assertEqual(self.api_get("bundle").status_code, 409)
        self.run_until(self.job)
        result = self.api_get("bundle")
        self.assertEqual(result.status_code, 200)
        bundle = result.json()["bundle"]
        args = dict(source_id=self.job.source_id, origin="https://vendor.example.org", scope_hash=self.job.scope_hash)
        payload = verify_bundle(bundle, self.api_token, **args)
        self.assertEqual(payload["status"], "known_good")
        with self.assertRaises(signing.BadSignature):
            verify_bundle(bundle + "x", self.api_token, **args)
        with self.assertRaises(ValueError):
            verify_bundle(bundle, self.api_token, **(args | {"source_id": 99999}))
        pause_job(self.job, "Review", rollback=False)
        self.assertEqual(self.api_get("bundle").status_code, 409)

    def test_remote_recipe_is_only_a_candidate_and_rejects_raw_data(self):
        self.run_until(self.job, ("recipe_ready", "paused", "failed"))
        pause_job(self.job, "Operator review", rollback=False)
        recipe = {"row": ".agent-tile", "name": "h3", "title": ".role", "evidence": ".agent-tile"}
        url = f"/automation/api/jobs/{self.job.pk}/candidate/"
        data = {"recipe": recipe, "generation": self.job.generation, "scope_hash": self.job.scope_hash}
        invalid = self.client.post(url, data | {"html": "<article>private</article>"}, content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.api_token)
        self.assertEqual(invalid.status_code, 400)
        valid = self.client.post(url, data, content_type="application/json", HTTP_AUTHORIZATION="Bearer " + self.api_token)
        self.assertEqual(valid.status_code, 202)
        self.job.refresh_from_db()
        self.job.source.refresh_from_db()
        self.assertEqual(self.job.state, "recipe_queued")
        self.assertEqual(self.job.source.recipe, {})
        self.assertTrue(self.job.recipes.filter(status="proposed", recipe=recipe).exists())

    def test_policy_form_bounds_and_configuration_pause(self):
        self.client.force_login(self.user)
        fields = ("min_url_score", "min_recipe_score", "allowed_paths", "denied_domains", "max_sites_per_day",
                  "probe_pages", "canary_pages", "delay_seconds", "recheck_days", "notes")
        data = {name: getattr(self.policy, name) for name in fields}
        data.update(enabled="on", allow_homepage="on")
        invalid = self.client.post(reverse("automation:policy", args=[self.campaign.pk]), data | {"canary_pages": 1000})
        self.assertEqual(invalid.status_code, 200)
        self.assertIn("canary_pages", invalid.context["form"].errors)
        response = self.client.post(reverse("automation:policy", args=[self.campaign.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, "paused")
