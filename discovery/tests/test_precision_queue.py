from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from discovery import acquisition
from discovery.models import Campaign, DailyUsage, DiscoveryJob, DiscoveryRun, SourceSearch
from discovery.services import process


class PrecisionQueueTests(TestCase):
    def setUp(self):
        self.campaign = Campaign.objects.create(name='Precision fixture', active=True,
            search_enabled=True, auto_search_queries=True, search_precision=True,
            keywords='merchant services', search_queries='')
        self.old = '"merchant services" "our team"'

    def test_stale_generated_row_is_skipped_even_with_provider_disabled(self):
        attempt = SourceSearch.objects.create(campaign=self.campaign, query=self.old)
        with patch('discovery.acquisition.brave_search') as provider:
            acquisition.process(attempt)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'skipped')
        self.assertEqual(attempt.attempts, 0)
        self.assertIn('no request made', attempt.message)
        self.assertFalse(DailyUsage.objects.exists())
        provider.assert_not_called()

    def test_legacy_generated_job_is_also_invalidated_before_request_or_quota(self):
        run = DiscoveryRun.objects.create(campaign=self.campaign)
        job = DiscoveryJob.objects.create(run=run, kind='search', url=self.old)
        with patch('discovery.services.brave_search') as provider:
            process(job)
        job.refresh_from_db()
        self.assertEqual(job.status, 'skipped')
        self.assertIn('queries changed', job.message)
        self.assertFalse(DailyUsage.objects.exists())
        provider.assert_not_called()

    @override_settings(BRAVE_SEARCH_ENABLED=True, BRAVE_SEARCH_API_KEY='fixture')
    def test_generated_rows_use_precision_pool_and_cursor_ties_rotate(self):
        pool = acquisition.query_pool(self.campaign)
        self.assertEqual(len(pool), 5)
        self.assertTrue(all('intitle:' in q for q in pool))
        for expected in pool:
            Campaign.objects.filter(pk=self.campaign.pk).update(next_search_at=timezone.now())
            with patch('discovery.yields.search_priority', return_value=0):
                attempt = acquisition.schedule(self.campaign)
            self.assertEqual(attempt.query, expected)
            acquisition.complete(attempt, 'Fixture only; no request', 'skipped')
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.search_query_cursor, 0)
        self.assertFalse(DailyUsage.objects.exists())

    def test_operator_exact_query_is_not_rewritten_or_invalidated(self):
        self.campaign.search_queries = self.old
        self.campaign.save()
        self.assertIn(self.old, acquisition.query_pool(self.campaign))
        attempt = SourceSearch.objects.create(campaign=self.campaign, query=self.old)
        acquisition.process(attempt)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'queued')  # disabled provider, exact query stays valid

    @override_settings(BRAVE_SEARCH_ENABLED=True, BRAVE_SEARCH_API_KEY='fixture')
    def test_stale_row_allows_replacement_on_next_schedule_without_provider_call(self):
        attempt = SourceSearch.objects.create(campaign=self.campaign, query=self.old)
        with patch('discovery.acquisition.brave_search') as provider:
            acquisition.process(attempt)
            replacement = acquisition.schedule(self.campaign)
        self.assertIsNotNone(replacement)
        self.assertIn('intitle:', replacement.query)
        self.assertFalse(DailyUsage.objects.exists())
        provider.assert_not_called()
