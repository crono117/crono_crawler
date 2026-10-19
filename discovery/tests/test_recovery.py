from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from discovery.models import Campaign, DiscoveryJob, DiscoveryRun
from discovery.services import register_common_crawl, schedule_due, tick
from leads.models import Source, WorkerLease
from leads.services.worker import acquire_lease, release_lease


class QueueRecoveryTests(TestCase):
    def setUp(self):
        self.campaign = Campaign.objects.create(name='Offline recovery', active=True,
            use_sitemaps=False, next_due_at=timezone.now() + timedelta(days=1))
        self.source = Source.objects.create(name='Fixture', url='https://recovery.example.test/team',
            approved=True, collector='demo')
        self.campaign.sources.add(self.source)
        self.run = DiscoveryRun.objects.create(campaign=self.campaign)

    def test_queued_run_survives_empty_or_stale_lease_without_duplicates(self):
        for old_token in ('', 'dead-worker'):
            with self.subTest(token=old_token):
                WorkerLease.objects.update_or_create(key='collector', defaults={
                    'token': old_token, 'heartbeat_at': timezone.now() - timedelta(days=1)})
                job, _ = DiscoveryJob.objects.get_or_create(run=self.run, url=self.source.url,
                    defaults={'source': self.source})
                token = acquire_lease()
                self.assertTrue(token)
                self.assertIsNone(acquire_lease())
                schedule_due()
                self.assertEqual(DiscoveryRun.objects.count(), 1)
                self.assertEqual(DiscoveryJob.objects.count(), 1)
                with patch('discovery.services.process') as process:
                    self.assertTrue(tick())
                    self.assertEqual(process.call_args.args[0].pk, job.pk)
                job.refresh_from_db()
                self.assertEqual(job.status, 'processing')
                release_lease(token)

    def test_processing_job_requeues_and_paused_run_stays_paused(self):
        job = DiscoveryJob.objects.create(run=self.run, source=self.source,
            url=self.source.url, status='processing', attempts=2)
        paused = Campaign.objects.create(name='Paused', active=False)
        run = DiscoveryRun.objects.create(campaign=paused, status='paused', message='Operator pause')
        held = DiscoveryJob.objects.create(run=run, url='https://held.example.test/team', status='processing')
        token = acquire_lease()
        job.refresh_from_db()
        held.refresh_from_db()
        self.assertEqual((job.status, job.attempts), ('queued', 2))
        self.assertEqual(job.diagnostics['worker_interruptions'], 1)
        # Paused checkpoint can be requeued but never dispatched while paused.
        self.assertEqual(held.status, 'queued')
        with patch('discovery.services.process') as process:
            tick()
            self.assertEqual(process.call_args.args[0].pk, job.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, 'paused')
        release_lease(token)

    def test_commoncrawl_registration_while_offline_is_resumed_once(self):
        specs = [{'url': self.source.url, 'method': 'commoncrawl', 'kind': 'page'}]
        register_common_crawl(self.campaign, specs)
        register_common_crawl(self.campaign, specs)
        self.assertFalse(WorkerLease.objects.exists())
        self.assertEqual(self.run.jobs.count(), 1)
        token = acquire_lease()
        with patch('discovery.services.process') as process:
            self.assertTrue(tick())
            self.assertFalse(tick())
            self.assertEqual(process.call_count, 1)
        release_lease(token)

    def test_restart_closes_empty_orphan_run_but_preserves_paused_run(self):
        DiscoveryRun.objects.filter(pk=self.run.pk).update(created_at=timezone.now() - timedelta(days=1))
        token = acquire_lease()
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, 'completed')
        self.assertIn('empty', self.run.message.lower())
        release_lease(token)

    def test_old_empty_run_makes_active_campaign_due_and_resumes_work(self):
        later = timezone.now() + timedelta(days=30)
        Campaign.objects.filter(pk=self.campaign.pk).update(next_due_at=later)
        DiscoveryRun.objects.filter(pk=self.run.pk).update(created_at=timezone.now() - timedelta(days=1))
        token = acquire_lease()
        self.campaign.refresh_from_db()
        self.assertLessEqual(self.campaign.next_due_at, timezone.now())
        schedule_due()
        replacement = self.campaign.runs.get(status__in=('queued', 'running'))
        self.assertNotEqual(replacement.pk, self.run.pk)
        self.assertTrue(replacement.jobs.filter(status='queued').exists())
        self.assertIsNone(acquire_lease())
        release_lease(token)
