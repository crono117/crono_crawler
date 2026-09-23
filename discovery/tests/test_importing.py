"""Metadata-only imports and full-inventory scheduling, entirely offline."""
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.core.management import call_command, CommandError
from django.test import TestCase
from automation.models import SiteAutomationJob, SitePolicy
from automation.policy import consider, consider_pending
from automation.services import start_setup
from discovery.importing import MAX_BYTES, import_metadata, parse_metadata
from discovery.models import Campaign, DiscoveredURL, DiscoveryJob, DiscoveryRun
from discovery.services import register, start
from leads.models import Source


def rows(*urls):
    return parse_metadata(json.dumps([{'url': u, 'label': 'Merchant services sales team'} for u in urls]).encode())


class ImportTests(TestCase):
    def setUp(self):
        self.campaign = Campaign.objects.create(name='Fictional import', active=True, max_candidates=10)
        SitePolicy.objects.create(campaign=self.campaign, enabled=True)

    def test_dry_run_then_apply_no_network_or_onboarding(self):
        metadata = rows('https://fictional.example.org/team/', 'https://fictional.example.org/team/#fragment')
        with patch('socket.getaddrinfo', side_effect=AssertionError('No DNS')):
            result = import_metadata(self.campaign.pk, metadata)
            self.assertFalse(DiscoveredURL.objects.exists())
            self.assertEqual([r['outcome'] for r in result['results']], ['would_create', 'duplicate_in_batch'])
            result = import_metadata(self.campaign.pk, metadata, apply=True)
            consider_pending()
        candidate = DiscoveredURL.objects.get()
        self.assertEqual(candidate.decision, 'pending')
        self.assertTrue(candidate.manual_review_required)
        self.assertIsNone(candidate.source_id)
        self.assertFalse(Source.objects.exists())
        self.assertFalse(DiscoveryJob.objects.exists())
        self.assertFalse(SiteAutomationJob.objects.exists())
        self.assertEqual(result['network_requests'], 0)

    def test_capacity_preserves_existing_decisions_and_revoked_source(self):
        source = Source.objects.create(name='Revoked source', url='https://revoked.example.org/', approved=False)
        for i, decision in enumerate(('pending', 'approved', 'dismissed')):
            DiscoveredURL.objects.create(campaign=self.campaign, url=f'https://existing.example.org/team/{i}',
                origin='https://existing.example.org', decision=decision, source=source if decision=='approved' else None)
        self.campaign.max_candidates = 3; self.campaign.save()
        result = import_metadata(self.campaign.pk, rows(*[f'https://existing.example.org/team/{i}' for i in range(4)]), apply=True)
        self.assertEqual([r['outcome'] for r in result['results']], ['existing_pending', 'existing_approved', 'existing_dismissed', 'inventory_full'])
        consider_pending()
        self.assertEqual(self.campaign.urls.count(), 3)
        self.assertTrue(self.campaign.urls.get(decision='pending').manual_review_required)
        source.refresh_from_db(); self.assertFalse(source.approved)

    def test_stale_policy_candidate_cannot_bypass_import_review(self):
        candidate = DiscoveredURL.objects.create(campaign=self.campaign, url='https://stale.example.org/team/',
            origin='https://stale.example.org', label='Merchant services sales team', score=80)
        import_metadata(self.campaign.pk, rows(candidate.url), apply=True)
        self.assertIsNone(consider(candidate))
        self.assertFalse(Source.objects.exists())

    def test_origin_dismissal_and_exclusions_preserved(self):
        DiscoveredURL.objects.create(campaign=self.campaign, url='https://dismissed.example.org/',
            origin='https://dismissed.example.org', decision='dismissed', dismissal_scope='origin')
        self.campaign.exclusions='domain:excluded.example.org'; self.campaign.save()
        result=import_metadata(self.campaign.pk, rows('https://dismissed.example.org/team/', 'https://excluded.example.org/team/'), apply=True)
        self.assertEqual([r['outcome'] for r in result['results']], ['dismissed_origin', 'excluded'])
        self.assertEqual(self.campaign.urls.count(), 1)

    def test_boundary_dedupe_and_diagnostics_do_not_echo_contact_or_secret(self):
        self.campaign.max_candidates=1; self.campaign.save()
        metadata=rows('https://safe.example.org/team/?token=SECRET&email=someone@example.org', 'https://next.example.org/team/')
        result=import_metadata(self.campaign.pk, metadata, apply=True)
        self.assertEqual([r['outcome'] for r in result['results']], ['created', 'inventory_full'])
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('someone', json.dumps(result))
        self.assertEqual(self.campaign.urls.count(), 1)

    def test_invalid_raw_urls_are_rejected_without_echo_or_dns(self):
        urls=['https://user:SECRET@example.org/', 'http://127.0.0.1/', 'http://10.0.0.1/',
              'http://169.254.169.254/', 'http://[::1]/', 'http://[fc00::1]/', 'http://[fe80::1]/',
              'http://[::ffff:127.0.0.1]/', 'http://localhost./', 'http://a.internal/', 'file:///etc/passwd',
              'https://example.org/\n', 'https://example.org/\x7f', 'https://example.org/%0a',
              'http://2130706433/', 'http://127.1/', 'https://[invalid/', 'https://example.org:999999/']
        with patch('socket.getaddrinfo', side_effect=AssertionError('No DNS')):
            for url in urls:
                with self.subTest(url=url):
                    with self.assertRaises(ValueError) as caught: rows(url)
                    self.assertNotIn('SECRET', str(caught.exception))

    def test_malformed_json_types_and_bounds(self):
        for body in (b'{', b'\xff', b'{}', b'[]', b'['*2000+b']'*2000, b' '* (MAX_BYTES+1),
                     b'[{"url":1}]', b'[{"url":"https://example.org/","contact":"x"}]',
                     json.dumps([{'url':'https://example.org/'}]*11).encode()):
            with self.subTest(body=body[:40]), self.assertRaises(ValueError): parse_metadata(body)
        with self.assertRaises(ValueError): parse_metadata(b'[]', limit=11)

    def test_command_defaults_dry_and_reads_only_bounded_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'metadata.json'; path.write_text('[{"url":"https://fictional.example.org/team/"}]')
            output=io.StringIO()
            call_command('import_discovery_candidates', campaign=self.campaign.pk, file=str(path), stdout=output)
            self.assertTrue(json.loads(output.getvalue())['dry_run'])
            self.assertFalse(DiscoveredURL.objects.exists())
            with path.open('wb') as stream: stream.truncate(10_000_000)
            with patch('discovery.importing.json.loads', side_effect=AssertionError('Must reject before JSON load')):
                with self.assertRaises(CommandError):
                    call_command('import_discovery_candidates', campaign=self.campaign.pk, file=str(path))


class CapacityTests(TestCase):
    def setUp(self):
        self.campaign=Campaign.objects.create(name='Full inventory', active=True, max_candidates=10, use_sitemaps=False)
        self.source=Source.objects.create(name='Runnable approved source', url='https://runnable.example.org/',
            approved=True, allowed_paths='/team', allow_homepage=True, active=False)
        self.campaign.sources.add(self.source)
        for i in range(10):
            DiscoveredURL.objects.create(campaign=self.campaign, url=f'https://runnable.example.org/team/{i}',
                origin='https://runnable.example.org', decision='approved', source=self.source,
                label='Merchant services sales team')

    def test_full_inventory_schedules_known_approved_urls_without_storing_new(self):
        run=start(self.campaign)
        self.assertEqual(run.jobs.count(), 10)
        self.assertEqual(self.campaign.urls.count(), 10)
        self.assertIsNone(register(run, 'https://runnable.example.org/team/new'))
        self.assertEqual(self.campaign.urls.count(), 10)

    def test_full_inventory_preserves_paused_automatic_source_gate(self):
        SitePolicy.objects.create(campaign=self.campaign, enabled=True)
        self.source.setup_mode='automatic'; self.source.approval_kind='policy'; self.source.save()
        setup=start_setup(self.source, self.campaign)
        setup.state='paused'; setup.pause_reason='no_matching_cards'; setup.save()
        run=start(self.campaign)
        self.assertFalse(run.jobs.exists())
        self.assertEqual(self.campaign.urls.count(), 10)
        setup.refresh_from_db(); self.assertEqual(setup.state, 'paused')

    def test_registration_preserves_origin_dismissal_with_approved_source(self):
        self.campaign.max_candidates=20; self.campaign.save()
        self.campaign.urls.update(decision='dismissed', dismissal_scope='origin')
        run = DiscoveryRun.objects.create(campaign=self.campaign)
        register(run, 'https://runnable.example.org/team/new', label='Merchant services sales team')
        self.assertFalse(run.jobs.exists())
        self.assertEqual(self.campaign.urls.count(), 10)
        from discovery.services import approve
        chosen=self.campaign.urls.first()
        approve(chosen, self.source)
        register(run, chosen.url, label='Merchant services sales team')
        self.assertEqual(run.jobs.count(), 1)
        self.assertEqual(self.campaign.urls.filter(decision='dismissed').count(), 9)
