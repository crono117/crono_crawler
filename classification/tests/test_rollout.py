"""Offline regressions for the bounded local rollout; no provider sockets."""
import hashlib
import io
import json
import copy
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from automation.models import SitePolicy
from automation.policy import consider_pending
from automation.services import start_setup
from classification import accounting, contracts, provider, routing, services
from classification.evidence import capture_page
from classification.fixtures import demo_response, seed_demo, seed_calibration, CALIBRATION_HTML, CALIBRATION_URL
from classification.models import (Attempt, CompanyDomain, JevControl, DailyUsage, Evaluation,
                                   EvidenceDocument, CompanyJob, Pilot)
from classification.tests.test_jev import LIVE
from discovery.models import DiscoveredURL, Campaign, DiscoveryJob
from discovery import services as discovery_services
from leads.models import Lead, Observation, PageJob, Run, Source, WorkerLease
from leads.services.network import Response
from leads.services.worker import acquire_lease


@override_settings(**LIVE)
class RolloutRegressions(TestCase):
    def setUp(self):
        self.pilot, _ = seed_demo()
        self.source = self.pilot.campaign.sources.get()
        body = demo_response(self.source.url)
        self.evaluation = capture_page(self.source, self.source.url, hashlib.sha256(body.body).hexdigest(),
                                       body.text, provider='live')[0]
        self.token = acquire_lease()

    def fresh_valid_evaluation(self, ordinal):
        created_at = timezone.now() - timedelta(days=8 * (ordinal + 1))
        cache_window = int(created_at.timestamp()) // (7 * 86400)
        cache_key = contracts.digest([
            self.evaluation.document.fingerprint, self.evaluation.company_id, None, 'live',
            self.evaluation.catalog_version, self.evaluation.request, cache_window,
        ])
        return Evaluation.objects.create(document=self.evaluation.document,
            company=self.evaluation.company, provider='live', request=self.evaluation.request,
            bindings=self.evaluation.bindings, model=self.evaluation.model,
            catalog_version=self.evaluation.catalog_version, cache_key=cache_key, created_at=created_at)

    def test_unhashable_choice_is_contract_failure_and_usage_still_settles(self):
        body = provider.mock_response(self.evaluation.request)
        body['usage'] = {'input_tokens': 120, 'output_tokens': 10}
        next(iter(body['answers'].values()))['choice'] = ['unknown']
        with patch('classification.provider._post', new=AsyncMock(return_value=provider.Result(200, body))):
            services.process(self.evaluation, self.token)
        self.evaluation.refresh_from_db()
        self.assertEqual(self.evaluation.state, 'failed_contract')
        self.assertFalse(self.evaluation.judgments.exists())
        self.assertEqual(Attempt.objects.get().cost_nusd, 120 * 42)
        accounting.verify_ledger(JevControl.objects.get())

    def test_large_numeric_probability_is_contract_error(self):
        body = provider.mock_response(self.evaluation.request)
        answer = next(iter(body['answers'].values()))
        answer['probabilities'][answer['choice']] = 10 ** 400
        with self.assertRaises(contracts.ContractError):
            contracts.validate_response(self.evaluation.request, body)

    def test_revoked_policy_blocks_paid_admission_of_existing_evidence(self):
        policy = SitePolicy.objects.create(campaign=self.pilot.campaign, enabled=True)
        self.source.setup_mode, self.source.approval_kind = 'automatic', 'policy'
        self.source.save()
        start_setup(self.source, self.pilot.campaign)
        body = demo_response(self.source.url)
        evaluation = capture_page(self.source, self.source.url, hashlib.sha256(body.body).hexdigest(),
                                  body.text, provider='live')[0]
        self.assertTrue(accounting.valid_evidence(evaluation))
        policy.enabled = False
        policy.save()
        with self.assertRaises(contracts.ContractError):
            accounting.reserve(evaluation.pk, self.token)
        self.assertFalse(Attempt.objects.exists())

    def test_existing_model_domain_proposal_stays_manual_even_at_inventory_cap(self):
        # A previously saved pending link must not escape the model-review gate.
        job = self.pilot.jobs.create(company=self.evaluation.company, key='rollout-domain',
                                     expires_at=self.pilot.expires_at)
        domain = CompanyDomain.objects.create(company=self.evaluation.company,
            url='https://new.example.org/team/', origin='https://new.example.org',
            span=self.evaluation.document.spans.first(), state='verified')
        candidate = DiscoveredURL.objects.create(campaign=self.pilot.campaign, url=domain.url,
            origin=domain.origin, label='Merchant services sales team', score=80)
        self.pilot.campaign.max_candidates = 1
        self.pilot.campaign.save()
        SitePolicy.objects.create(campaign=self.pilot.campaign, enabled=True)
        routing.propose_domains(job, [domain])
        candidate.refresh_from_db()
        self.assertTrue(candidate.manual_review_required)
        consider_pending()
        candidate.refresh_from_db()
        self.assertEqual(candidate.decision, 'pending')
        self.assertIsNone(candidate.source_id)

    def test_doctor_reports_missing_daily_allowance_as_not_ready(self):
        output = io.StringIO()
        with override_settings(JEV_DAILY_ALLOWANCE_NUSD=0):
            call_command('jev', 'doctor', stdout=output)
        result = json.loads(output.getvalue())
        self.assertFalse(result['live_ready'])
        self.assertIn('allowance', ' '.join(result['checks']))

    def test_uncertain_transport_never_automatically_retries(self):
        for result in (provider.Result(None, None, error='timeout'), provider.Result(None, None, error='transport_error'),
                       provider.Result(503, {'error': 'RAW-SECRET'}), provider.Result(429, None)):
            with self.subTest(status=result.status, error=result.error):
                # A fresh packet for each distinct failed request.
                evaluation = self.fresh_valid_evaluation(
                    (result.status or 0) + {'timeout': 1, 'transport_error': 2}.get(result.error, 3))
                JevControl.objects.update(next_allowed_at=timezone.now())
                with patch('classification.provider._post', new=AsyncMock(return_value=result)) as post:
                    services.process(evaluation, self.token)
                    evaluation.refresh_from_db()
                    self.assertEqual(evaluation.state, 'uncertain')
                    services.process(evaluation, self.token)
                    self.assertEqual(post.await_count, 1)
                self.assertNotIn('RAW-SECRET', evaluation.reason)
                self.assertIsNone(evaluation.attempt_history.get().cost_nusd)
        accounting.verify_ledger(JevControl.objects.get())

    def test_doctor_inconsistent_ledger_is_safe_json(self):
        JevControl.objects.update(spent_nusd=1)
        output = io.StringIO()
        call_command('jev', 'doctor', stdout=output)
        result = json.loads(output.getvalue())
        self.assertFalse(result['live_ready'])
        self.assertFalse(result['ledger_consistent'])
        self.assertIn('Ledger', ' '.join(result['checks']))

    def test_dispatch_rechecks_revoked_policy_after_reservation(self):
        policy = SitePolicy.objects.create(campaign=self.pilot.campaign, enabled=True)
        self.source.setup_mode, self.source.approval_kind = 'automatic', 'policy'
        self.source.save()
        start_setup(self.source, self.pilot.campaign)
        body = demo_response(self.source.url)
        evaluation = capture_page(self.source, self.source.url, hashlib.sha256(body.body).hexdigest(), body.text, provider='live')[0]
        attempt = accounting.reserve(evaluation.pk, self.token)
        policy.enabled = False
        policy.save()
        with self.assertRaises(contracts.ContractError):
            accounting.dispatch(attempt.pk, evaluation.request, self.token)
        self.assertTrue(accounting.abort_unsent(attempt.pk, 'revoked'))
        self.assertEqual(Attempt.objects.get().cost_nusd, 0)

    def test_diagnostics_distinguish_all_admission_gates(self):
        gates = [({'JEV_MODE': 'off'}, 'disabled'), ({'TYPESAFE_API_KEY': ''}, 'absent'),
                 ({'JEV_PRICE_CONFIRMED': False}, 'price'),
                 ({'JEV_ALLOW_ESTIMATED_TOKENS': False}, 'token counter')]
        for config, expected in gates:
            with self.subTest(config=config), override_settings(**config):
                self.assertIn(expected, ' '.join(accounting.diagnostics(JevControl.objects.get())))
        JevControl.objects.update(paused=True, active_attempt=uuid.uuid4(), next_allowed_at=timezone.now()+timedelta(hours=1))
        text = ' '.join(accounting.diagnostics(JevControl.objects.get()))
        for word in ('paused', 'owns dispatch', 'cooldown'):
            self.assertIn(word, text)

    def test_attempt_history_survives_days_without_becoming_an_admission_stop(self):
        JevControl.objects.update(cumulative_attempt_limit=3)
        original = timezone.now()
        for day in range(3):
            now = original + timedelta(days=day)
            with patch('django.utils.timezone.now', return_value=now):
                WorkerLease.objects.update(heartbeat_at=now)
                self.evaluation.refresh_from_db()
                # Retry consumption across UTC days, including verified zero billing.
                attempt = accounting.reserve(self.evaluation.pk, self.token)
                accounting.dispatch(attempt.pk, self.evaluation.request, self.token)
                accounting.settle(attempt.pk, provider.Result(None, None, error='timeout'), 1)
                accounting.recover_attempt(attempt.pk, 'test', 'Verified no billing', billed_nusd=0)
        now = original + timedelta(days=3)
        with patch('django.utils.timezone.now', return_value=now):
            WorkerLease.objects.update(heartbeat_at=now)
            other = Evaluation.objects.filter(provider='live').exclude(pk=self.evaluation.pk).first()
            fourth = accounting.reserve(other.pk, self.token)
            self.assertEqual(fourth.state, 'reserved')
        self.assertEqual(Attempt.objects.count(), 4)


@override_settings(JEV_MODE='mock', JEV_TOKEN_COUNTER='', JEV_ROUTING_ENABLED=True)
class SyntheticRegressions(TestCase):
    def test_seed_is_idempotent_no_fetch_dispatch_contact_or_jobs(self):
        with patch('socket.getaddrinfo', side_effect=AssertionError('No DNS')), patch('classification.provider._post', side_effect=AssertionError('No provider')):
            first = seed_calibration()
            doc = first[0].document
            again = seed_calibration()
        self.assertEqual([e.pk for e in first], [e.pk for e in again])
        self.assertEqual(EvidenceDocument.objects.count(), 1)
        self.assertIsNone(doc.retrieved_at)
        self.assertEqual(doc.provenance, 'synthetic-fixture')
        self.assertEqual(doc.content_hash, hashlib.sha256(CALIBRATION_HTML.encode()).hexdigest())
        self.assertTrue(doc.parser_version)
        for model in (Lead, PageJob, Run, DiscoveryJob, Campaign, CompanyJob, Attempt):
            self.assertFalse(model.objects.exists(), model.__name__)
        self.assertFalse(Source.objects.get().active)

    def test_changed_fixture_is_never_silently_restored(self):
        seed_calibration()
        source = Source.objects.get()
        for field, value in [('approved', False), ('company', 'Changed'), ('recipe', {'row': 'article'}),
                             ('allowed_paths', '/'), ('allow_homepage', False), ('follow_links', True),
                             ('approval_kind', 'policy'), ('collector', 'http')]:
            with self.subTest(field=field):
                old = getattr(source, field)
                setattr(source, field, value); source.save()
                with self.assertRaises(ValueError): seed_calibration()
                source.refresh_from_db()
                self.assertEqual(getattr(source, field), value)
                setattr(source, field, old); source.save()

    def test_synthetic_judgments_cannot_route_even_with_verified_domains_and_campaign(self):
        evaluation = seed_calibration()[0]
        token = acquire_lease()
        services.process(evaluation, token, mock_only=True)
        evaluation.refresh_from_db()
        domain = evaluation.company.domains.first()
        routing.verify_domain(domain.pk, 'test', 'Fictional label review')
        campaign = Campaign.objects.create(name='Existing live campaign', active=True)
        campaign.sources.add(evaluation.document.source)
        pilot = Pilot.objects.create(name='Later pilot', campaign=campaign, active=True, expires_at=timezone.now()+timedelta(days=1))
        self.assertFalse(routing.eligible(evaluation))
        with self.assertRaises(ValueError): routing.queue_company(pilot.pk, evaluation.pk)
        # Even a cached live-provider answer to fictional text remains synthetic.
        live = services.enqueue_subject(evaluation.document, evaluation.company, evaluation.document.spans.first(), provider='live')[0]
        Evaluation.objects.filter(pk=live.pk).update(state='running', lease_token=token)
        services.save_answers(live.pk, provider.mock_response(live.request), token)
        live.refresh_from_db()
        self.assertFalse(routing.eligible(live))
        cached = services.enqueue_subject(evaluation.document, evaluation.company,
            evaluation.document.spans.get(kind='company'), provider='mock')[0]
        self.assertEqual(cached.pk, evaluation.pk)
        self.assertTrue(cached.uses.filter(cache_hit=True).exists())
        self.assertFalse(routing.eligible(cached))
        # An edit cannot relabel the saved immutable synthetic document as real.
        source = live.document.source
        source.collector = 'http'; source.save()
        live.refresh_from_db()
        self.assertFalse(routing.eligible(live))
        self.assertFalse(CompanyJob.objects.exists())

    def test_expired_synthetic_cannot_reuse_fetched_provenance(self):
        old = seed_calibration()[0]
        doc = old.document
        # A legacy non-synthetic capture at the same URL/body must not be selected.
        other = copy.copy(doc); other.pk = None; other.fingerprint = 'legacy-fetched'; other.provenance = 'fetched'
        other.retrieved_at = timezone.now(); other.save()
        EvidenceDocument.objects.filter(pk=doc.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        fresh = seed_calibration()[0]
        self.assertNotEqual(fresh.document_id, other.pk)
        self.assertEqual(fresh.document.provenance, 'synthetic-fixture')
        self.assertIsNone(fresh.document.retrieved_at)

    def test_mock_result_cannot_select_real_source_or_escape_after_source_edit(self):
        pilot, evaluations = seed_demo()
        evaluation = evaluations[0]
        token = acquire_lease()
        services.process(evaluation, token, mock_only=True)
        real = Source.objects.create(name='Reviewed real scope', url='https://real.example.org/team/', approved=True)
        pilot.campaign.sources.add(real)
        evaluation.company.domains.update(state='candidate')
        CompanyDomain.objects.create(company=evaluation.company, url=real.url, origin='https://real.example.org',
                                     span=evaluation.document.spans.first(), state='verified')
        with self.assertRaises(ValueError): routing.queue_company(pilot.pk, evaluation.pk)
        evaluation.company.domains.filter(url=evaluation.document.source.url).update(state='verified')
        job = routing.queue_company(pilot.pk, evaluation.pk)
        source = evaluation.document.source
        source.collector = 'http'; source.save()
        job.refresh_from_db()
        with self.assertRaises(accounting.Deferred): routing.check_work(job)
        self.assertFalse(routing.advance(job.pk))
        self.assertFalse(DiscoveryJob.objects.exists())

    def test_company_routing_at_capacity_reuses_known_url_without_new_inventory(self):
        pilot, evaluations = seed_demo()
        token = acquire_lease()
        services.process(evaluations[0], token, mock_only=True)
        source = evaluations[0].document.source
        campaign = pilot.campaign
        campaign.max_candidates=1; campaign.save()
        DiscoveredURL.objects.create(campaign=campaign, url=source.url, origin='https://jev.example.test',
            decision='approved', source=source)
        job = routing.queue_company(pilot.pk, evaluations[0].pk)
        routing.advance(job.pk)
        self.assertEqual(campaign.urls.count(), 1)
        self.assertEqual(DiscoveryJob.objects.filter(company_job=job).count(), 1)

    def test_company_routing_does_not_create_ineligible_ordinary_page_job(self):
        pilot, _ = seed_demo()
        source = pilot.campaign.sources.get()
        source.allowed_paths = '/'
        source.save(update_fields=['allowed_paths'])
        body = demo_response(source.url)
        evaluation = capture_page(source, source.url, hashlib.sha256(body.body).hexdigest(),
                                  body.text, provider='mock')[0]
        token = acquire_lease()
        services.process(evaluation, token, mock_only=True)
        campaign = pilot.campaign
        campaign.min_score = 40
        campaign.save(update_fields=['min_score'])
        document = evaluation.document
        document.links = [{'url': 'https://jev.example.test/resources/',
                           'label': 'Merchant services representative overview'}]
        document.save(update_fields=['links'])
        job = routing.queue_company(pilot.pk, evaluation.pk)
        routing.advance(job.pk)
        self.assertFalse(DiscoveryJob.objects.filter(
            company_job=job, url='https://jev.example.test/resources/').exists())

    def test_company_routing_updates_stale_metadata_before_dispatch(self):
        pilot, _ = seed_demo()
        source = pilot.campaign.sources.get()
        source.allowed_paths = '/'
        source.save(update_fields=['allowed_paths'])
        body = demo_response(source.url)
        evaluation = capture_page(source, source.url, hashlib.sha256(body.body).hexdigest(),
                                  body.text, provider='mock')[0]
        token = acquire_lease()
        services.process(evaluation, token, mock_only=True)
        campaign = pilot.campaign
        campaign.min_score = 35
        campaign.save(update_fields=['min_score'])
        url = 'https://jev.example.test/resources/'
        candidate = DiscoveredURL.objects.create(campaign=campaign, url=url,
            origin='https://jev.example.test', source=source, decision='approved',
            label='Quarterly sales collateral', context='Old collateral context')
        evaluation.document.links = [{'url': url, 'label': 'Sales representatives'}]
        evaluation.document.save(update_fields=['links'])
        company_job = routing.queue_company(pilot.pk, evaluation.pk)
        routing.advance(company_job.pk)
        page = DiscoveryJob.objects.get(company_job=company_job, url=url)
        candidate.refresh_from_db()
        self.assertEqual(candidate.label, 'Sales representatives')
        self.assertEqual(candidate.context, 'Old collateral context')
        response = Response(url, 200, {'content-type': 'text/html'}, b'<h1>No contacts</h1>')
        with patch('classification.fixtures.demo_response', return_value=response) as fetch:
            discovery_services.process(page)
        self.assertEqual(fetch.call_count, 1)
        page.refresh_from_db()
        self.assertEqual(page.status, 'done')

    def test_company_routing_precomputes_reviewed_source_once(self):
        pilot, _ = seed_demo()
        source = pilot.campaign.sources.get()
        source.allowed_paths = '/'
        source.save(update_fields=['allowed_paths'])
        body = demo_response(source.url)
        evaluation = capture_page(source, source.url, hashlib.sha256(body.body).hexdigest(),
                                  body.text, provider='mock')[0]
        token = acquire_lease()
        services.process(evaluation, token, mock_only=True)
        lead = Lead.objects.create(identity='routing-reviewed-source', name='Reviewed', status='reviewed')
        Observation.objects.create(lead=lead, source=source, page_url=source.url, facts={},
            source_category=source.category, evidence='Reviewed', content_hash='routing-reviewed', present=True)
        evaluation.document.links = [
            {'url': f'https://jev.example.test/team/{index}/', 'label': 'Sales representatives'}
            for index in range(5)
        ]
        evaluation.document.save(update_fields=['links'])
        company_job = routing.queue_company(pilot.pk, evaluation.pk)
        with CaptureQueriesContext(connection) as queries:
            routing.advance(company_job.pk)
        observation_queries = [query['sql'] for query in queries.captured_queries
                               if 'leads_observation' in query['sql'].lower()]
        self.assertEqual(len(observation_queries), 1)
        for candidate in pilot.campaign.urls.exclude(url=source.url):
            self.assertEqual(candidate.reasons.count('Source has human-reviewed contacts (+10)'), 1)

    def test_company_routing_cannot_add_new_url_to_full_inventory(self):
        pilot, evaluations = seed_demo()
        token = acquire_lease()
        services.process(evaluations[0], token, mock_only=True)
        campaign = pilot.campaign
        campaign.max_candidates=1; campaign.save()
        DiscoveredURL.objects.create(campaign=campaign, url='https://unrelated.example.org/', origin='https://unrelated.example.org')
        job = routing.queue_company(pilot.pk, evaluations[0].pk)
        routing.advance(job.pk)
        self.assertEqual(campaign.urls.count(), 1)
        self.assertFalse(DiscoveryJob.objects.exists())

    def test_fixture_url_edit_does_not_create_replacement_permission(self):
        seed_calibration()
        source = Source.objects.get()
        source.url='https://changed.example.org/'; source.save()
        with self.assertRaises(ValueError): seed_calibration()
        self.assertEqual(Source.objects.count(), 1)

    def test_synthetic_body_is_fixed_and_cannot_be_claimed_from_arbitrary_html(self):
        source = seed_calibration()[0].document.source
        with self.assertRaises(ValueError):
            capture_page(source, source.url, 'invented', '<h1>Different</h1>', synthetic=True)


    def test_company_new_urls_cannot_bypass_origin_dismissal(self):
        pilot, evaluations = seed_demo()
        token = acquire_lease()
        services.process(evaluations[0], token, mock_only=True)
        DiscoveredURL.objects.create(campaign=pilot.campaign, url='https://jev.example.test/dismissed/',
            origin='https://jev.example.test', decision='dismissed', dismissal_scope='origin')
        job=routing.queue_company(pilot.pk, evaluations[0].pk)
        routing.advance(job.pk)
        self.assertEqual(pilot.campaign.urls.count(), 1)
        self.assertFalse(DiscoveryJob.objects.exists())


@override_settings(**LIVE)
class AdditionalMoneyRegressions(TestCase):
    setUp = RolloutRegressions.setUp
    fresh_valid_evaluation = RolloutRegressions.fresh_valid_evaluation
    def test_lowered_daily_allowance_revokes_unsent_permit(self):
        attempt = accounting.reserve(self.evaluation.pk, self.token)
        with override_settings(JEV_DAILY_ALLOWANCE_NUSD=accounting.RESERVATION_NUSD - 1):
            with self.assertRaises(accounting.Deferred):
                accounting.dispatch(attempt.pk, self.evaluation.request, self.token)
        self.assertTrue(accounting.abort_unsent(attempt.pk, 'revoked'))
        accounting.verify_ledger(JevControl.objects.get())

    def test_legacy_retry_wait_with_uncertain_attempt_requires_explicit_recovery(self):
        attempt = accounting.reserve(self.evaluation.pk, self.token)
        accounting.dispatch(attempt.pk, self.evaluation.request, self.token)
        accounting.settle(attempt.pk, provider.Result(None, None, error='timeout'), 1)
        Evaluation.objects.filter(pk=self.evaluation.pk).update(state='retry_wait')
        JevControl.objects.update(next_allowed_at=timezone.now())
        with self.assertRaisesMessage(accounting.Deferred, 'unresolved'):
            accounting.reserve(self.evaluation.pk, self.token)
        accounting.recover_attempt(attempt.pk, 'test', 'Deliberate retry; billing unknown')
        self.assertEqual(Attempt.objects.get().state, 'recovered')
        self.assertEqual(JevControl.objects.get().reserved_nusd, accounting.RESERVATION_NUSD)

    def test_invalid_business_contract_always_settles_valid_usage(self):
        mutations = [lambda r: r.update(model='wrong'), lambda r: r.update(answers=[]),
            lambda r: r['answers'].update(unexpected={}),
            lambda r: next(iter(r['answers'].values())).update(type='score'),
            lambda r: next(iter(r['answers'].values())).update(choice={}),
            lambda r: next(iter(r['answers'].values())).update(confidence=True),
            lambda r: next(iter(r['answers'].values())).update(confidence=float('inf')),
            lambda r: next(iter(r['answers'].values())).update(confidence=float('nan')),
            lambda r: next(iter(r['answers'].values())).update(probabilities={'unknown': 1}),
            lambda r: next(iter(r['answers'].values())).update(confidence='0.9')]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                evaluation = self.fresh_valid_evaluation(index + 100)
                body=provider.mock_response(evaluation.request); body['usage']={'input_tokens': 120, 'output_tokens': 10}
                mutate(body)
                JevControl.objects.update(next_allowed_at=timezone.now())
                with patch('classification.provider._post', new=AsyncMock(return_value=provider.Result(200, body))):
                    services.process(evaluation, self.token)
                evaluation.refresh_from_db()
                self.assertEqual(evaluation.state, 'failed_contract')
                self.assertFalse(evaluation.judgments.exists())
                self.assertEqual(evaluation.attempt_history.get().cost_nusd, 5040)
        accounting.verify_ledger(JevControl.objects.get())

    def test_reconciling_old_attempt_never_disturbs_new_dispatch(self):
        first = accounting.reserve(self.evaluation.pk, self.token)
        accounting.dispatch(first.pk, self.evaluation.request, self.token)
        accounting.settle(first.pk, provider.Result(None, None, error='timeout'), 1)
        accounting.recover_attempt(first.pk, 'test', 'Explicitly allow next attempt, unknown billing')
        JevControl.objects.update(next_allowed_at=timezone.now())
        Evaluation.objects.filter(pk=self.evaluation.pk).update(available_at=timezone.now())
        second = accounting.reserve(self.evaluation.pk, self.token)
        accounting.dispatch(second.pk, self.evaluation.request, self.token)
        accounting.recover_attempt(first.pk, 'test', 'Older invoice verified', billed_nusd=42)
        self.evaluation.refresh_from_db()
        self.assertEqual(self.evaluation.state, 'running')
        self.assertEqual(JevControl.objects.get().active_attempt, second.pk)
        body=provider.mock_response(self.evaluation.request)
        body['usage']={'input_tokens': 120, 'output_tokens': 10}
        accounting.settle(second.pk, provider.Result(200, body), 1)
        services.save_answers(self.evaluation.pk, body, self.token)
        self.evaluation.refresh_from_db()
        self.assertEqual(self.evaluation.state, 'succeeded')
        accounting.verify_ledger(JevControl.objects.get())
        with self.assertRaises(ValueError):
            accounting.recover_attempt(first.pk, 'test', 'Repeated recovery must be rejected')

    def test_daily_allowance_resets_on_next_utc_day_while_lifetime_spend_remains(self):
        accounting.set_allowance('0.002772', 'test', 'One reservation only')
        attempt=accounting.reserve(self.evaluation.pk, self.token)
        accounting.dispatch(attempt.pk, self.evaluation.request, self.token)
        accounting.settle(attempt.pk, provider.Result(200, {'usage': {'input_tokens': 120, 'output_tokens': 0}}), 1)
        tomorrow=timezone.now()+timedelta(days=1)
        with patch('django.utils.timezone.now', return_value=tomorrow):
            WorkerLease.objects.update(heartbeat_at=tomorrow)
            other=Evaluation.objects.filter(provider='live').exclude(pk=self.evaluation.pk).first()
            second = accounting.reserve(other.pk, self.token)
            self.assertEqual(second.state, 'reserved')
        self.assertEqual(JevControl.objects.get().spent_nusd, 5040)
        self.assertEqual(Attempt.objects.count(), 2)
