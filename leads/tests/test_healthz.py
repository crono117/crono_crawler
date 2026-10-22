from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse
from leads.models import WorkerLease


class HealthzTests(TestCase):
    def test_healthy_with_recent_lease(self):
        WorkerLease.objects.update_or_create(
            key="collector",
            defaults={"token": "t", "heartbeat_at": timezone.now() - timedelta(seconds=2)},
        )
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(set(data.keys()), {"status", "db", "worker_heartbeat_age_seconds"})
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["db"], "ok")
        self.assertIsInstance(data["worker_heartbeat_age_seconds"], int)
        self.assertGreaterEqual(data["worker_heartbeat_age_seconds"], 0)
        self.assertLess(data["worker_heartbeat_age_seconds"], 60)

    def test_no_lease_returns_null_age(self):
        WorkerLease.objects.all().delete()
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data["worker_heartbeat_age_seconds"])
        self.assertEqual(data["db"], "ok")
        self.assertEqual(data["status"], "ok")

    def test_stale_lease_is_reported_not_503(self):
        WorkerLease.objects.update_or_create(
            key="collector",
            defaults={"token": "t", "heartbeat_at": timezone.now() - timedelta(hours=2)},
        )
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreaterEqual(data["worker_heartbeat_age_seconds"], 2 * 60 * 60)
        self.assertEqual(data["db"], "ok")
        self.assertEqual(data["status"], "ok")

    @patch("leads.views.connection.cursor", side_effect=Exception("DB is down"))
    def test_db_down_returns_503_and_no_leakage(self, _cursor):
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 503)
        body = response.content.decode()
        data = response.json()
        # Expected minimal fields only
        self.assertEqual(set(data.keys()), {"status", "db", "worker_heartbeat_age_seconds"})
        self.assertEqual(data["db"], "error")
        self.assertEqual(data["status"], "error")
        self.assertIsNone(data["worker_heartbeat_age_seconds"])
        # No leakage of exception text, settings, env values, or paths
        self.assertNotIn("DB is down", body)
        self.assertNotIn("SECRET", body)
        self.assertNotIn("DATABASE_URL", body)
        self.assertNotIn("/workspace", body)
        self.assertNotIn("\\", body)  # Avoid Windows-style path leaks too

