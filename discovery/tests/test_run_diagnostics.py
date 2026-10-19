import io
import json
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from discovery.models import Campaign, DiscoveryJob, DiscoveryRun
from discovery.services import finish, pause, process, register_batch, save_page
from leads.models import Source
from leads.services.network import FetchError, Response


class RunDiagnosticsTests(TestCase):
    def setUp(self):
        self.campaign = Campaign.objects.create(name='Diagnostic fixture', active=True, use_sitemaps=False)
        self.source = Source.objects.create(url='https://diagnostic.example.test/team', approved=True,
            collector='demo')
        self.campaign.sources.add(self.source)
        self.run = DiscoveryRun.objects.create(campaign=self.campaign)
        register_batch(self.run, [{'url': self.source.url, 'method': 'seed', 'seed': True,
            'source_hint': self.source}])
        self.job = self.run.jobs.get()

    def test_failed_run_has_safe_durable_reason_and_job_category(self):
        cases = [(TimeoutError('secret raw text'), 'timeout'),
                 (FetchError('Access challenge secret', status=403), 'access_blocked'),
                 (FetchError('robots.txt does not allow this page.', status=403), 'robots_denied'),
                 (ValueError('secret parser body'), 'parser_or_extraction_error')]
        for exc, code in cases:
            with self.subTest(code=code):
                DiscoveryRun.objects.filter(pk=self.run.pk).update(status='running', message='')
                DiscoveryJob.objects.filter(pk=self.job.pk).update(status='processing', attempts=3)
                Source.objects.filter(pk=self.source.pk).update(blocked_at=None)
                self.job.refresh_from_db()
                with patch('discovery.services.demo_response', side_effect=exc):
                    process(self.job)
                self.job.refresh_from_db()
                self.run.refresh_from_db()
                self.assertEqual(self.run.status, 'failed')
                self.assertEqual(self.run.terminal_reason, 'job_failures')
                self.assertEqual(self.job.failure_code, code)
                self.assertTrue(self.run.message)
                self.assertNotIn('secret', self.run.message + self.job.message)
                self.assertEqual(self.run.diagnostics['failures'][code], 1)

    def test_successful_refresh_with_failure_explains_zero_novelty(self):
        DiscoveryRun.objects.filter(pk=self.run.pk).update(pages_done=35, contacts_seen=8, new_contacts=0)
        DiscoveryJob.objects.filter(pk=self.job.pk).update(status='failed', failure_code='timeout')
        finish(self.run)
        self.run.refresh_from_db()
        self.assertIn('35', self.run.message)
        self.assertIn('8', self.run.message)
        self.assertEqual(self.run.diagnostics['accepted_observations'], 8)
        self.assertIsNone(self.run.diagnostics['new_non_synthetic_leads'])

    def test_extraction_rejections_and_counts_are_persisted_without_personal_data(self):
        html = '''<div class="team-member"><h3>Robin Sample</h3><h4>Engineer</h4>
        <a href="mailto:robin@fixture.example.test">Email</a></div>'''
        save_page(self.job, Response(self.source.url, 200, {'content-type': 'text/html'}, html.encode()))
        finish(self.run)
        self.run.refresh_from_db()
        stats = self.run.diagnostics
        self.assertEqual(stats['candidate_people'], 1)
        self.assertEqual(stats['candidate_contact_methods'], 1)
        self.assertEqual(stats['rejected_candidates']['non_sales_role'], 1)
        self.assertEqual(self.run.terminal_reason, 'extraction_rejected')
        self.assertNotIn('Robin', json.dumps(stats))
        self.assertNotIn('robin@', json.dumps(stats))

    def test_empty_pause_gets_operator_reason(self):
        pause(self.campaign, '')
        self.run.refresh_from_db()
        self.assertEqual(self.run.terminal_reason, 'campaign_paused')
        self.assertTrue(self.run.message)

    def test_report_explains_legacy_unknown_failure_without_writes(self):
        self.run.status = 'failed'
        self.run.save()
        output = io.StringIO()
        call_command('crawler_status', stdout=output)
        report = json.loads(output.getvalue())
        self.assertTrue(report['runs'][0]['message'])
        self.run.refresh_from_db()
        self.assertEqual(self.run.message, report['runs'][0]['message'])

    def test_database_rejects_blank_terminal_reason_on_bulk_update(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            DiscoveryRun.objects.filter(pk=self.run.pk).update(status='failed', message='', terminal_reason='')

    def test_status_distinguishes_stale_lease_and_unhealthy_http(self):
        from datetime import timedelta
        from django.utils import timezone
        from leads.models import WorkerLease
        WorkerLease.objects.create(key='collector', token='not-displayed',
            heartbeat_at=timezone.now() - timedelta(days=1))
        output = io.StringIO()
        with patch('leads.management.commands.crawler_status.HTTPConnection') as connection:
            connection.return_value.getresponse.return_value.status = 503
            call_command('crawler_status', http='http://127.0.0.1:8017/', stdout=output)
        data = json.loads(output.getvalue())
        self.assertEqual(data['lease']['state'], 'stale')
        self.assertEqual(data['http']['state'], 'unhealthy_http')
        self.assertNotIn('not-displayed', output.getvalue())

    def test_uppercase_contact_reaches_non_demo_observation_then_refreshes(self):
        from leads.models import Lead, Observation
        self.source.collector = 'http'
        self.source.save()
        self.job.refresh_from_db()
        html = '''<div class="team-member"><h3>Robin Sample</h3><h4>Sales Representative</h4>
        <a href="MAILTO:robin@fixture.example.test">Email me</a></div>'''
        response = Response(self.source.url, 200, {'content-type': 'text/html'}, html.encode())
        with patch('discovery.services.fetch') as fetch:
            save_page(self.job, response)
            finish(self.run)
        fetch.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual((Lead.objects.count(), Observation.objects.count()), (1, 1))
        self.assertEqual(self.run.diagnostics['new_non_synthetic_leads'], 1)
        next_run = DiscoveryRun.objects.create(campaign=self.campaign)
        next_job = DiscoveryJob.objects.create(run=next_run, source=self.source, url=self.source.url)
        save_page(next_job, response)
        finish(next_run)
        next_run.refresh_from_db()
        self.assertEqual((Lead.objects.count(), Observation.objects.count()), (1, 1))
        self.assertEqual(next_run.terminal_reason, 'refresh_only')
        self.assertEqual(next_run.contacts_seen, 1)
        self.assertEqual(next_run.diagnostics['new_non_synthetic_leads'], 0)

    def test_bad_http_ports_are_command_errors_without_connections(self):
        from django.core.management.base import CommandError
        for url in ('http://127.0.0.1:abc/', 'http://127.0.0.1:65536/', 'http://127.0.0.1:0/', 'http://[bad/'):
            with self.subTest(url=url), patch('leads.management.commands.crawler_status.HTTPConnection') as connection:
                with self.assertRaises(CommandError):
                    call_command('crawler_status', http=url, stdout=io.StringIO())
                connection.assert_not_called()
