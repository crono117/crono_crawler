from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from discovery.models import Campaign, DiscoveryRun
from leads.models import WorkerLease
from leads.services.worker import acquire_lease, release_lease, LEASE_SECONDS


class EmptyRunRecoveryFrozenClockTests(TestCase):
    def setUp(self):
        # Fixed, timezone-aware 'now' for deterministic threshold checks
        self.fixed_now = timezone.now()

    def test_exact_threshold_and_neighbors_with_frozen_now(self):
        """
        Code uses created_at__lt (strictly less than) now - LEASE_SECONDS for orphan selection.
        Therefore:
          - exactly at the threshold: NOT reaped
          - 1 second (or microsecond) older: reaped
          - 1 second newer: NOT reaped
        """
        # Prepare three separate campaigns with one empty run each
        c_under = Campaign.objects.create(name="under", active=True, use_sitemaps=False)
        c_exact = Campaign.objects.create(name="exact", active=True, use_sitemaps=False)
        c_over = Campaign.objects.create(name="over", active=True, use_sitemaps=False)
        r_under = DiscoveryRun.objects.create(campaign=c_under, status="queued")
        r_exact = DiscoveryRun.objects.create(campaign=c_exact, status="queued")
        r_over = DiscoveryRun.objects.create(campaign=c_over, status="queued")
        # Set created_at timestamps precisely around the threshold
        DiscoveryRun.objects.filter(pk=r_under.pk).update(
            created_at=self.fixed_now - timedelta(seconds=LEASE_SECONDS - 1))
        DiscoveryRun.objects.filter(pk=r_exact.pk).update(
            created_at=self.fixed_now - timedelta(seconds=LEASE_SECONDS))
        DiscoveryRun.objects.filter(pk=r_over.pk).update(
            created_at=self.fixed_now - timedelta(seconds=LEASE_SECONDS + 1))
        # Expired/empty lease triggers recovery
        WorkerLease.objects.update_or_create(key="collector", defaults={
            "token": "", "heartbeat_at": self.fixed_now - timedelta(days=1),
        })
        with patch("leads.services.worker.timezone.now", return_value=self.fixed_now):
            token = acquire_lease()
        try:
            r_under.refresh_from_db(); r_exact.refresh_from_db(); r_over.refresh_from_db()
            # Strict 'lt' semantics: only strictly older-than-threshold is reaped
            self.assertIn(r_under.status, ("queued", "running"))
            self.assertIn(r_exact.status, ("queued", "running"))
            self.assertEqual(r_over.status, "completed")
            self.assertEqual(r_over.terminal_reason, "recovered_empty")
        finally:
            release_lease(token)

