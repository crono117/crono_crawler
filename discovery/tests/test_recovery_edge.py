from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from discovery.models import Campaign, DiscoveryRun
from leads.models import WorkerLease
from leads.services.worker import acquire_lease, release_lease, LEASE_SECONDS


class EmptyRunRecoveryEdgeTests(TestCase):
    def setUp(self):
        self.campaign_active = Campaign.objects.create(name="Active", active=True, use_sitemaps=False)
        self.campaign_paused = Campaign.objects.create(name="Paused", active=False, use_sitemaps=False)
        # One empty open run on the active campaign
        self.orphan1 = DiscoveryRun.objects.create(campaign=self.campaign_active, status="queued")
        # And a second orphan on a different active campaign to exercise batch handling
        self.campaign_active_2 = Campaign.objects.create(name="Active 2", active=True, use_sitemaps=False)
        self.orphan2 = DiscoveryRun.objects.create(campaign=self.campaign_active_2, status="queued")
        # A paused campaign with an empty open run that must remain untouched
        self.paused_orphan = DiscoveryRun.objects.create(campaign=self.campaign_paused, status="queued")
        cut = timezone.now() - timedelta(seconds=LEASE_SECONDS + 5)
        DiscoveryRun.objects.filter(pk__in=[self.orphan1.pk, self.orphan2.pk, self.paused_orphan.pk]).update(created_at=cut)

    def test_multiple_orphans_closed_and_campaign_due_once_preserve_paused(self):
        # Make campaigns' next_due_at far in the future to verify they are advanced "now"
        later = timezone.now() + timedelta(days=30)
        Campaign.objects.filter(pk__in=[self.campaign_active.pk, self.campaign_active_2.pk]).update(next_due_at=later)
        # Expired/empty lease triggers recovery
        WorkerLease.objects.update_or_create(key="collector", defaults={
            "token": "", "heartbeat_at": timezone.now() - timedelta(days=1),
        })
        token = acquire_lease()
        try:
            # Both active-campaign orphans are closed; paused campaign untouched
            self.orphan1.refresh_from_db()
            self.orphan2.refresh_from_db()
            self.paused_orphan.refresh_from_db()
            for run in (self.orphan1, self.orphan2):
                self.assertEqual(run.status, "completed")
                self.assertEqual(run.terminal_reason, "recovered_empty")
                self.assertIn("empty run", run.message.lower())
            # Paused campaign's empty run remains open
            self.assertIn(self.paused_orphan.status, ("queued", "running"))
            # Active campaigns made due now (not still in the future)
            self.campaign_active.refresh_from_db()
            self.campaign_active_2.refresh_from_db()
            self.assertLessEqual(self.campaign_active.next_due_at, timezone.now())
            self.assertLessEqual(self.campaign_active_2.next_due_at, timezone.now())
        finally:
            release_lease(token)

    def test_orphan_age_threshold_boundary(self):
        now = timezone.now()
        # Fresh setup: three separate campaigns with one run each around the threshold
        c_under = Campaign.objects.create(name="Boundary-under", active=True, use_sitemaps=False)
        c_exact = Campaign.objects.create(name="Boundary-exact", active=True, use_sitemaps=False)
        c_over = Campaign.objects.create(name="Boundary-over", active=True, use_sitemaps=False)
        under = DiscoveryRun.objects.create(campaign=c_under, status="queued")
        exact = DiscoveryRun.objects.create(campaign=c_exact, status="queued")
        over = DiscoveryRun.objects.create(campaign=c_over, status="queued")
        # Calibrate against a cut taken immediately before acquire_lease
        cut = timezone.now()
        DiscoveryRun.objects.filter(pk=under.pk).update(created_at=cut - timedelta(seconds=LEASE_SECONDS - 2))
        DiscoveryRun.objects.filter(pk=exact.pk).update(created_at=cut - timedelta(seconds=LEASE_SECONDS))
        DiscoveryRun.objects.filter(pk=over.pk).update(created_at=cut - timedelta(seconds=LEASE_SECONDS + 2))
        WorkerLease.objects.update_or_create(key="collector", defaults={
            "token": "", "heartbeat_at": now - timedelta(days=1),
        })
        token = acquire_lease()
        try:
            # Only the strictly older-than-threshold run is closed
            under.refresh_from_db(); exact.refresh_from_db(); over.refresh_from_db()
            self.assertIn(under.status, ("queued", "running"))
            # Exact-at-threshold can race to 'over' by a few milliseconds without time freezing; assert only the strict cases.
            self.assertEqual(over.status, "completed")
            self.assertEqual(over.terminal_reason, "recovered_empty")
        finally:
            release_lease(token)

    def test_active_lease_does_not_reap_orphans_but_expired_lease_does(self):
        # Fresh lease inside window: acquire_lease should return None and not change runs
        fresh = timezone.now()
        WorkerLease.objects.update_or_create(key="collector", defaults={
            "token": "live", "heartbeat_at": fresh,
        })
        self.assertIsNone(acquire_lease())
        self.orphan1.refresh_from_db(); self.orphan2.refresh_from_db()
        self.assertIn(self.orphan1.status, ("queued", "running"))
        self.assertIn(self.orphan2.status, ("queued", "running"))

        # Expire the lease: a new token is taken and orphans are closed
        WorkerLease.objects.filter(key="collector").update(heartbeat_at=timezone.now() - timedelta(seconds=LEASE_SECONDS + 5))
        token = acquire_lease()
        try:
            self.orphan1.refresh_from_db(); self.orphan2.refresh_from_db()
            self.assertEqual(self.orphan1.status, "completed")
            self.assertEqual(self.orphan2.status, "completed")
        finally:
            release_lease(token)

