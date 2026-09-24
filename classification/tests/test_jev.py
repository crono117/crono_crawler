import asyncio
import copy
import hashlib
import io
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from automation.models import SitePolicy
from discovery.models import DiscoveredURL, DiscoveryJob, DiscoveryRun
from leads.models import Lead, WorkerLease
from leads.services.worker import acquire_lease, release_lease
from classification import accounting, contracts, provider, routing, services
from classification.evidence import capture_page, purge_expired, valid_span
from classification.fixtures import DEMO_ORIGIN, demo_response, seed_demo
from classification.models import (Attempt, Company, CompanyDomain, CompanyJob, ContactCandidate, DailyUsage,
    Evaluation, EvidenceDocument, EvidenceSpan, JevControl, Judgment, Person)


def huge_counter(request):
    return 5001


def recache(evaluation):
    window = int(evaluation.created_at.timestamp()) // (7 * 86400)
    evaluation.cache_key = contracts.digest([
        evaluation.document.fingerprint, evaluation.company_id, evaluation.person_id,
        evaluation.provider, evaluation.catalog_version, evaluation.request, window,
    ])
    evaluation.save(update_fields=['cache_key'])
    evaluation.refresh_from_db()
    return evaluation


LIVE = dict(JEV_MODE='live', TYPESAFE_API_KEY='synthetic-test-key', JEV_PRICE_CONFIRMED=True,
            JEV_ALLOW_ESTIMATED_TOKENS=True, JEV_TOKEN_COUNTER='')


@override_settings(JEV_MODE='mock', JEV_TOKEN_COUNTER='')
class EvidenceTests(TestCase):
    def setUp(self):
        self.pilot, self.evaluations = seed_demo()
        self.source = self.pilot.campaign.sources.get()

    def test_engineer_without_channel_retained_without_lead(self):
        self.assertTrue(Person.objects.filter(name='Jordan Example').exists())
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(ContactCandidate.objects.filter(person__name='Jordan Example').exists())

    def test_footer_shared_and_not_assigned_to_engineer(self):
        item = ContactCandidate.objects.get(value='support@aster.example')
        self.assertTrue(item.shared_hint)
        self.assertIsNone(item.person_id)
        self.assertEqual(item.deliverability, 'not_checked')

    def test_spans_hash_and_offsets_are_exact(self):
        self.assertTrue(all(valid_span(s) for s in EvidenceSpan.objects.select_related('document')))
        doc = EvidenceDocument.objects.get()
        self.assertEqual(doc.content_hash, hashlib.sha256(demo_response(self.source.url).body).hexdigest())

    def test_repeat_capture_is_cached_and_records_use(self):
        before = Evaluation.objects.count()
        seed_demo()
        self.assertEqual(Evaluation.objects.count(), before)
        self.assertEqual(EvidenceDocument.objects.count(), 1)
        self.assertEqual(self.evaluations[0].uses.count(), 2)

    def test_capture_never_queues_discovery(self):
        self.assertFalse(DiscoveredURL.objects.exists())
        self.assertFalse(DiscoveryJob.objects.exists())

    def test_unapproved_source_creates_no_new_evidence(self):
        self.source.approved = False
        self.source.save()
        self.assertEqual(capture_page(self.source, self.source.url, 'different', '<h1>New company</h1>'), [])
        self.assertEqual(EvidenceDocument.objects.count(), 1)

    def test_external_url_is_not_authorized_by_company_judgment(self):
        self.assertEqual(capture_page(self.source, 'https://elsewhere.example/', 'hash', '<h1>Company</h1>'), [])

    def test_scope_revocation_invalidates_existing_evidence(self):
        self.source.allowed_paths = '/different'
        self.source.save()
        self.assertFalse(accounting.valid_evidence(self.evaluations[0]))

    def test_evidence_tampering_is_detected(self):
        EvidenceDocument.objects.update(text='tampered')
        fresh = Evaluation.objects.select_related('document__source').get(pk=self.evaluations[0].pk)
        self.assertFalse(accounting.valid_evidence(fresh))

    def test_request_state_tampering_is_detected_even_when_spans_remain_valid(self):
        evaluation = self.evaluations[0]
        request = copy.deepcopy(evaluation.request)
        request['state']['spans'][0]['text'] = 'unrelated replacement text'
        evaluation.request = request
        evaluation.save(update_fields=['request'])
        evaluation.refresh_from_db()
        self.assertFalse(accounting.valid_evidence(evaluation))

    def test_request_and_binding_question_keys_must_match_exactly(self):
        evaluation = self.evaluations[0]
        bindings = copy.deepcopy(evaluation.bindings)
        bindings.pop(next(iter(bindings)))
        evaluation.bindings = bindings
        evaluation.save(update_fields=['bindings'])
        evaluation.refresh_from_db()
        self.assertFalse(accounting.valid_evidence(evaluation))

    def test_contact_questions_must_match_transmitted_contact_state(self):
        subject = Evaluation.objects.filter(person__isnull=False).first()
        span = EvidenceSpan.objects.get(pk=next(iter(subject.bindings.values()))[0])
        candidate = ContactCandidate.objects.create(
            span=span, person=subject.person, company=subject.company, kind='email',
            value='person@example.test', normalized='person@example.test')
        evaluation = services.enqueue_subject(
            subject.document, subject.company, span, person=subject.person, contacts=[candidate])[0]
        request = copy.deepcopy(evaluation.request)
        request['state']['contacts'] = []
        evaluation.request = request
        evaluation.save(update_fields=['request'])
        evaluation.refresh_from_db()
        self.assertFalse(accounting.valid_evidence(evaluation))

    @override_settings(JEV_MAX_INPUT_TOKENS=2000)
    def test_split_person_packets_keep_only_packet_local_contacts(self):
        subject = Evaluation.objects.filter(person__isnull=False).first()
        span = EvidenceSpan.objects.get(pk=next(iter(subject.bindings.values()))[0])
        candidate = ContactCandidate.objects.create(
            span=span, person=subject.person, company=subject.company, kind='email',
            value='split@example.test', normalized='split@example.test')
        evaluations = services.enqueue_subject(
            subject.document, subject.company, span, person=subject.person, contacts=[candidate])
        self.assertGreater(len(evaluations), 1)
        self.assertTrue(all(accounting.valid_evidence(item) for item in evaluations))
        for item in evaluations:
            has_contact_question = f'contact_{candidate.pk}' in item.request['questions']
            self.assertEqual(bool(item.request['state']['contacts']), has_contact_question)

    def test_versioned_question_body_tampering_is_detected(self):
        evaluation = self.evaluations[0]
        request = copy.deepcopy(evaluation.request)
        next(iter(request['questions'].values()))['instructions'] = 'replacement question'
        evaluation.request = request
        evaluation.save(update_fields=['request'])
        evaluation.refresh_from_db()
        self.assertFalse(accounting.valid_evidence(evaluation))

    def test_removing_question_and_binding_is_detected(self):
        evaluation = self.evaluations[0]
        request, bindings = copy.deepcopy(evaluation.request), copy.deepcopy(evaluation.bindings)
        key = next(iter(request['questions']))
        request['questions'].pop(key)
        bindings.pop(key)
        evaluation.request, evaluation.bindings = request, bindings
        evaluation.save(update_fields=['request', 'bindings'])
        evaluation.refresh_from_db()
        self.assertFalse(accounting.valid_evidence(evaluation))

    def test_malformed_state_collections_fail_closed(self):
        evaluation = self.evaluations[0]
        original = copy.deepcopy(evaluation.request)
        for field in ('spans', 'contacts'):
            with self.subTest(field=field):
                request = copy.deepcopy(original)
                request['state'][field] = None
                evaluation.request = request
                evaluation.save(update_fields=['request'])
                evaluation.refresh_from_db()
                self.assertFalse(accounting.valid_evidence(evaluation))

    def test_unhashable_contact_id_fails_closed(self):
        subject = Evaluation.objects.filter(person__isnull=False).first()
        span = EvidenceSpan.objects.get(pk=next(iter(subject.bindings.values()))[0])
        candidate = ContactCandidate.objects.create(
            span=span, person=subject.person, company=subject.company, kind='email',
            value='badid@example.test', normalized='badid@example.test')
        evaluation = services.enqueue_subject(
            subject.document, subject.company, span, person=subject.person, contacts=[candidate])[0]
        request = copy.deepcopy(evaluation.request)
        request['state']['contacts'][0]['id'] = []
        evaluation.request = request
        evaluation.save(update_fields=['request'])
        recache(evaluation)
        self.assertFalse(accounting.valid_evidence(evaluation))

    def test_shared_company_contact_is_valid_in_person_packet(self):
        subject = Evaluation.objects.filter(person__isnull=False).first()
        span = EvidenceSpan.objects.get(pk=next(iter(subject.bindings.values()))[0])
        candidate = ContactCandidate.objects.create(
            span=span, person=None, company=subject.company, kind='email',
            value='shared@example.test', normalized='shared@example.test', shared_hint=True)
        evaluation = services.enqueue_subject(
            subject.document, subject.company, span, person=subject.person, contacts=[candidate])[0]
        self.assertTrue(accounting.valid_evidence(evaluation))

    def test_unknown_catalog_and_same_name_subject_swap_are_detected(self):
        evaluation = self.evaluations[0]
        original_company = evaluation.company
        evaluation.catalog_version = 'forged-version'
        evaluation.save(update_fields=['catalog_version'])
        evaluation.refresh_from_db()
        self.assertFalse(accounting.valid_evidence(evaluation))
        evaluation.catalog_version = services.VERSION
        evaluation.company = Company.objects.create(identity='same-name-swap', name=original_company.name)
        evaluation.save(update_fields=['catalog_version', 'company'])
        evaluation.refresh_from_db()
        self.assertFalse(accounting.valid_evidence(evaluation))

    def test_every_bound_span_is_transmitted_with_exact_text(self):
        for evaluation in self.evaluations:
            state = evaluation.request['state']
            supplied = {item['id']: item['text'] for item in state.get('spans', [])}
            supplied.update({item['span_id']: item['text'] for item in state.get('blocks', [])})
            bound_ids = {span_id for values in evaluation.bindings.values() for span_id in values}
            spans = EvidenceSpan.objects.in_bulk(bound_ids)
            self.assertEqual(set(supplied), bound_ids)
            self.assertTrue(all(supplied[span_id] == spans[span_id].document.text[
                spans[span_id].start:spans[span_id].end] for span_id in bound_ids))

    def test_expiry_removes_contact_values_and_payload(self):
        EvidenceDocument.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        purge_expired()
        self.assertEqual(EvidenceDocument.objects.get().text, '')
        self.assertFalse(ContactCandidate.objects.exists())
        self.assertEqual(Evaluation.objects.first().state, 'evidence_expired')

    def test_live_and_mock_results_never_share_cache(self):
        response = demo_response(self.source.url)
        live = capture_page(self.source, self.source.url, hashlib.sha256(response.body).hexdigest(), response.text, provider='live')
        self.assertTrue(set(e.pk for e in live).isdisjoint(e.pk for e in self.evaluations))

    def test_source_reapproval_does_not_mutate_prior_evidence(self):
        original = EvidenceDocument.objects.get()
        self.source.allowed_paths += '\n/about'
        self.source.save()
        response = demo_response(self.source.url)
        capture_page(self.source, self.source.url, original.content_hash, response.text)
        self.assertEqual(EvidenceDocument.objects.count(), 2)
        original.refresh_from_db()
        self.assertNotEqual(original.scope_hash, EvidenceDocument.objects.exclude(pk=original.pk).get().scope_hash)

    def test_recipe_change_invalidates_evidence_and_creates_new_version(self):
        old = self.evaluations[0]
        self.source.recipe = {'row': '.team-member', 'name': '.name'}
        self.source.save()
        self.assertFalse(accounting.valid_evidence(old))
        response = demo_response(self.source.url)
        fresh = capture_page(self.source, self.source.url, old.document.content_hash, response.text)
        self.assertNotEqual(fresh[0].document_id, old.document_id)

    def test_recapture_after_expiry_creates_new_retained_version(self):
        old_id = EvidenceDocument.objects.get().pk
        EvidenceDocument.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        purge_expired()
        seed_demo()
        self.assertEqual(EvidenceDocument.objects.count(), 2)
        self.assertEqual(EvidenceDocument.objects.get(pk=old_id).text, '')
        seed_demo()
        self.assertEqual(EvidenceDocument.objects.count(), 2)

    def test_unchanged_content_gets_new_evaluation_after_cache_window(self):
        before = Evaluation.objects.count()
        later = timezone.now() + timedelta(days=8)
        with patch('classification.services.timezone.now', return_value=later):
            seed_demo()
        self.assertEqual(Evaluation.objects.count(), before * 2)


@override_settings(**LIVE)
class AccountingTests(TestCase):
    def setUp(self):
        self.pilot, _ = seed_demo()
        self.source = self.pilot.campaign.sources.get()
        response = demo_response(self.source.url)
        self.evaluation = capture_page(self.source, self.source.url, hashlib.sha256(response.body).hexdigest(), response.text, provider='live')[0]
        self.token = acquire_lease()

    def reserve(self):
        return accounting.reserve(self.evaluation.pk, self.token)

    def result(self, status=200, usage=True):
        body = provider.mock_response(self.evaluation.request)
        if usage:
            body['usage'] = {'input_tokens': 120, 'output_tokens': 10}
        else:
            body.pop('usage')
        return provider.Result(status, body)

    def test_reservation_precedes_dispatch(self):
        attempt = self.reserve()
        control = JevControl.objects.get()
        self.assertEqual(control.active_attempt, attempt.pk)
        self.assertEqual(control.reserved_nusd, accounting.RESERVATION_NUSD)
        self.assertEqual(DailyUsage.objects.get().attempts, 1)
        self.assertIsNone(attempt.dispatched_at)

    def test_permit_is_single_use(self):
        attempt = self.reserve()
        accounting.dispatch(attempt.pk, self.evaluation.request, self.token)
        with self.assertRaises(contracts.ContractError):
            accounting.dispatch(attempt.pk, self.evaluation.request, self.token)

    def test_another_evaluation_cannot_dispatch_concurrently(self):
        self.reserve()
        other = Evaluation.objects.filter(provider='live').exclude(pk=self.evaluation.pk).first()
        with self.assertRaises(accounting.Deferred):
            accounting.reserve(other.pk, self.token)
        self.assertEqual(Attempt.objects.count(), 1)

    def test_settlement_is_idempotent_and_integer_money(self):
        attempt = self.reserve()
        accounting.settle(attempt.pk, self.result(), 12)
        accounting.settle(attempt.pk, self.result(), 12)
        control = JevControl.objects.get()
        self.assertEqual(control.spent_nusd, 120 * 42)
        self.assertEqual(control.reserved_nusd, 0)
        accounting.verify_ledger(control)

    def test_missing_usage_retains_full_reservation(self):
        attempt = self.reserve()
        accounting.settle(attempt.pk, self.result(usage=False), 12)
        self.assertEqual(JevControl.objects.get().reserved_nusd, accounting.RESERVATION_NUSD)
        self.assertIsNone(JevControl.objects.get().active_attempt)

    def test_pause_revokes_unsent_permit_without_refunding_attempt(self):
        attempt = self.reserve()
        accounting.set_paused(True, 'test', 'stop')
        with self.assertRaises(accounting.Deferred):
            accounting.dispatch(attempt.pk, self.evaluation.request, self.token)
        self.assertTrue(accounting.abort_unsent(attempt.pk, 'stop'))
        self.assertEqual(DailyUsage.objects.get().attempts, 1)
        self.assertEqual(JevControl.objects.get().reserved_nusd, 0)

    def test_dispatched_attempt_cannot_be_refunded_as_unsent(self):
        attempt = self.reserve()
        accounting.dispatch(attempt.pk, self.evaluation.request, self.token)
        self.assertFalse(accounting.abort_unsent(attempt.pk, 'unknown'))
        self.assertEqual(JevControl.objects.get().reserved_nusd, accounting.RESERVATION_NUSD)

    def test_request_mutation_invalidates_permit(self):
        attempt = self.reserve()
        request = copy.deepcopy(self.evaluation.request)
        request['state']['company'] = 'changed'
        with self.assertRaises(contracts.ContractError):
            accounting.dispatch(attempt.pk, request, self.token)

    def test_revoked_source_stops_reserved_call(self):
        attempt = self.reserve()
        self.source.approved = False
        self.source.save()
        with self.assertRaises(contracts.ContractError):
            accounting.dispatch(attempt.pk, self.evaluation.request, self.token)

    def test_key_alone_does_not_enable_paid_calls(self):
        with override_settings(JEV_MODE='off'):
            with self.assertRaises(accounting.Deferred):
                self.reserve()
        self.assertFalse(Attempt.objects.exists())

    @override_settings(JEV_DAILY_ALLOWANCE_NUSD=0)
    def test_live_requires_daily_money_gate(self):
        self.assertIn('Configure a positive daily spending allowance.', accounting.readiness())
        with self.assertRaises(accounting.Deferred):
            self.reserve()

    def test_unverified_token_counter_blocks_default_live(self):
        with override_settings(JEV_ALLOW_ESTIMATED_TOKENS=False):
            with self.assertRaises(accounting.Deferred):
                self.reserve()

    def test_full_request_token_count_capped(self):
        with override_settings(JEV_TOKEN_COUNTER=__name__ + '.huge_counter'):
            with self.assertRaises(contracts.ContractError):
                self.reserve()
        self.assertFalse(Attempt.objects.exists())

    def test_attempt_history_does_not_stop_daily_money_budget(self):
        DailyUsage.objects.create(day=timezone.now().date(), attempts=100)
        attempt = self.reserve()
        self.assertEqual(attempt.state, 'reserved')

    @override_settings(JEV_DAILY_ALLOWANCE_NUSD=accounting.RESERVATION_NUSD)
    def test_daily_money_limit_blocks_next_reservation(self):
        Attempt.objects.create(evaluation=self.evaluation, ordinal=99, day=timezone.now().date(),
            state='settled', lease_token='prior', request_hash='prior', reserved_nusd=accounting.RESERVATION_NUSD,
            cost_nusd=1, estimated_tokens=1, counter='test')
        JevControl.objects.update(spent_nusd=1)

        with self.assertRaises(accounting.Deferred) as caught:
            self.reserve()

        self.assertIn('Daily spending allowance', str(caught.exception))
        self.assertEqual(caught.exception.until.hour, 0)
        self.assertEqual(Attempt.objects.count(), 1)

    @override_settings(JEV_DAILY_ALLOWANCE_NUSD=2_000_000_000)
    def test_daily_only_mode_ignores_legacy_attempt_and_cumulative_money_stops(self):
        JevControl.objects.update(allowance_nusd=1, cumulative_attempt_limit=0)

        attempt = self.reserve()

        self.assertEqual(attempt.state, 'reserved')

    def test_legacy_cumulative_wallet_does_not_stop_daily_budget(self):
        JevControl.objects.update(allowance_nusd=1)
        attempt = self.reserve()
        self.assertEqual(attempt.state, 'reserved')
        self.assertEqual(JevControl.objects.get().allowance_nusd, 1)

    def test_inconsistent_ledger_fails_closed(self):
        JevControl.objects.update(spent_nusd=50)
        with self.assertRaises(accounting.Deferred):
            self.reserve()

    def test_credentials_error_pauses_globally(self):
        attempt = self.reserve()
        accounting.settle(attempt.pk, provider.Result(401, None), 3)
        self.assertTrue(JevControl.objects.get().paused)

    def test_excess_usage_is_recorded_and_pauses(self):
        attempt = self.reserve()
        result = self.result()
        result.body['usage']['input_tokens'] = 5001
        accounting.settle(attempt.pk, result, 1)
        self.assertTrue(JevControl.objects.get().paused)
        self.assertEqual(JevControl.objects.get().spent_nusd, 5001 * 42)

    def test_pause_resume_preserves_wait_and_money(self):
        when = timezone.now() + timedelta(hours=1)
        JevControl.objects.update(next_allowed_at=when)
        accounting.set_paused(True, 'test', 'pause')
        accounting.set_paused(False, 'test', 'resume')
        self.assertEqual(JevControl.objects.get().next_allowed_at, when)
        with self.assertRaises(accounting.Deferred):
            self.reserve()

    def test_crash_recovery_retains_reservation(self):
        attempt = self.reserve()
        accounting.dispatch(attempt.pk, self.evaluation.request, self.token)
        with self.assertRaises(ValueError):
            accounting.recover_attempt(attempt.pk, 'test', 'crash')
        accounting.recover_attempt(attempt.pk, 'test', 'transport stopped', True)
        self.assertIsNone(JevControl.objects.get().active_attempt)
        self.assertEqual(JevControl.objects.get().reserved_nusd, accounting.RESERVATION_NUSD)
        self.evaluation.refresh_from_db()
        self.assertEqual(self.evaluation.state, 'waiting')

    def test_verified_billing_can_reconcile_uncertain_charge(self):
        attempt = self.reserve()
        accounting.settle(attempt.pk, provider.Result(None, None, error='timeout'), 30)
        accounting.recover_attempt(attempt.pk, 'test', 'checked account invoice', billed_nusd=84)
        self.assertEqual(JevControl.objects.get().spent_nusd, 84)
        self.assertEqual(JevControl.objects.get().reserved_nusd, 0)

    def test_stale_lease_cannot_admit(self):
        release_lease(self.token)
        with self.assertRaises(accounting.Deferred):
            self.reserve()

    def test_retry_after_persists_and_counts_failed_attempt(self):
        with patch('classification.provider._post', new=AsyncMock(return_value=provider.Result(
                429, {'usage': {'input_tokens': 0, 'output_tokens': 0}}, retry_after='120'))):
            services.process(self.evaluation, self.token)
        self.evaluation.refresh_from_db()
        self.assertEqual(self.evaluation.state, 'retry_wait')
        self.assertGreater(self.evaluation.available_at, timezone.now() + timedelta(seconds=119))
        self.assertEqual(self.evaluation.attempts, 1)
        self.assertEqual(DailyUsage.objects.get().failed, 1)

    def test_malformed_success_is_billed_but_not_accepted(self):
        result = self.result()
        result.body['answers'] = {}
        with patch('classification.provider._post', new=AsyncMock(return_value=result)):
            services.process(self.evaluation, self.token)
        self.evaluation.refresh_from_db()
        self.assertEqual(self.evaluation.state, 'failed_contract')
        self.assertFalse(self.evaluation.judgments.exists())
        self.assertEqual(JevControl.objects.get().spent_nusd, 5040)

    def test_small_provider_rounding_drift_is_normalized_and_audited(self):
        from classification.models import ControlEvent
        result = self.result()
        answer = next(iter(result.body['answers'].values()))
        answer['probabilities'] = {key: value * .99 for key, value in answer['probabilities'].items()}
        with patch('classification.provider._post', new=AsyncMock(return_value=result)):
            services.process(self.evaluation, self.token)
        self.evaluation.refresh_from_db()
        self.assertEqual(self.evaluation.state, 'succeeded')
        self.assertTrue(all(abs(sum(j.probabilities.values()) - 1) < 1e-12
                            for j in self.evaluation.judgments.all()))
        event = ControlEvent.objects.get(action='normalize_provider_probabilities')
        self.assertEqual(event.data['evaluation'], str(self.evaluation.pk))
        self.assertAlmostEqual(event.data['original_sum'], .99)
        self.assertAlmostEqual(event.data['delta'], .01)

    def test_sub_tolerance_nonzero_drift_is_still_normalized_and_audited(self):
        from classification.models import ControlEvent
        result = self.result()
        answer = next(iter(result.body['answers'].values()))
        answer['probabilities'] = {key: value * .9995 for key, value in answer['probabilities'].items()}
        with patch('classification.provider._post', new=AsyncMock(return_value=result)):
            services.process(self.evaluation, self.token)
        self.evaluation.refresh_from_db()
        self.assertEqual(self.evaluation.state, 'succeeded')
        self.assertTrue(ControlEvent.objects.filter(action='normalize_provider_probabilities').exists())

    def test_above_machine_epsilon_drift_is_normalized_and_audited(self):
        from classification.models import ControlEvent
        result = self.result()
        answer = next(iter(result.body['answers'].values()))
        answer['probabilities'] = {
            key: value * .9999999999995 for key, value in answer['probabilities'].items()}
        with patch('classification.provider._post', new=AsyncMock(return_value=result)):
            services.process(self.evaluation, self.token)
        self.evaluation.refresh_from_db()
        self.assertEqual(self.evaluation.state, 'succeeded')
        self.assertTrue(ControlEvent.objects.filter(action='normalize_provider_probabilities').exists())

    def test_post_settlement_persistence_failure_has_explicit_recovery(self):
        result = self.result()
        answer = next(iter(result.body['answers'].values()))
        answer['probabilities'] = {key: value * .99 for key, value in answer['probabilities'].items()}
        with patch('classification.provider._post', new=AsyncMock(return_value=result)), \
                patch('classification.services.ControlEvent.objects.create', side_effect=RuntimeError('db write')):
            self.assertTrue(services.process(self.evaluation, self.token))
        self.evaluation.refresh_from_db()
        attempt = self.evaluation.attempt_history.get()
        self.assertEqual(self.evaluation.state, 'failed_persistence')
        self.assertEqual(attempt.state, 'settled')
        self.assertIsNotNone(attempt.cost_nusd)
        self.assertFalse(self.evaluation.judgments.exists())
        self.assertIsNone(JevControl.objects.get().active_attempt)
        accounting.retry_settled_persistence(self.evaluation.pk, 'test', 'retry paid persistence failure')
        self.evaluation.refresh_from_db()
        self.assertEqual(self.evaluation.state, 'waiting')

    def test_provider_cannot_spoof_internal_normalization_audit(self):
        from classification.models import ControlEvent
        result = self.result()
        answer = next(iter(result.body['answers'].values()))
        answer['_probability_normalization'] = {'original_sum': 'forged'}
        with patch('classification.provider._post', new=AsyncMock(return_value=result)):
            services.process(self.evaluation, self.token)
        self.evaluation.refresh_from_db()
        self.assertEqual(self.evaluation.state, 'succeeded')
        self.assertFalse(ControlEvent.objects.filter(action='normalize_provider_probabilities').exists())

    def test_success_keeps_legacy_review_untouched(self):
        lead = Lead.objects.create(identity='fixture-suppressed', name='Prior Example', status='suppressed', notes='keep')
        with patch('classification.provider._post', new=AsyncMock(return_value=self.result())):
            services.process(self.evaluation, self.token)
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.notes), ('suppressed', 'keep'))
        self.assertTrue(self.evaluation.judgments.exists())

    def test_stale_worker_cannot_overwrite_new_running_owner(self):
        Evaluation.objects.filter(pk=self.evaluation.pk).update(state='running', lease_token='replacement')
        services.process(self.evaluation, self.token)
        self.evaluation.refresh_from_db()
        self.assertEqual((self.evaluation.state, self.evaluation.lease_token), ('running', 'replacement'))

    def test_three_attempt_ceiling(self):
        Evaluation.objects.filter(pk=self.evaluation.pk).update(attempts=3)
        with self.assertRaises(contracts.ContractError):
            self.reserve()


@override_settings(JEV_MODE='mock', JEV_CAPTURE_ENABLED=True, JEV_ROUTING_ENABLED=True, JEV_TOKEN_COUNTER='')
class RoutingTests(TestCase):
    def setUp(self):
        self.pilot, evaluations = seed_demo()
        self.source = self.pilot.campaign.sources.get()
        self.token = acquire_lease()
        for evaluation in evaluations:
            services.process(evaluation, self.token, mock_only=True)
        self.trigger = Evaluation.objects.filter(person__isnull=True).first()

    def job(self):
        self.trigger.judgments.update(review_state='confirmed')
        return routing.queue_company(self.pilot.pk, self.trigger.pk)

    def test_unreviewed_model_judgment_cannot_route(self):
        self.real_trigger()
        self.assertTrue(self.trigger.judgments.filter(review_state='needs_review').exists())
        self.assertFalse(routing.eligible(self.trigger))

    def test_confirmed_model_judgment_can_route(self):
        self.real_trigger()
        self.trigger.judgments.update(review_state='confirmed')
        self.assertTrue(routing.eligible(self.trigger))

    def test_engineer_role_not_inferred_from_company(self):
        self.assertEqual(Judgment.objects.get(question_id='person_sales_role').label, 'non_sales')

    def test_company_trigger_deduplicates(self):
        first = self.job()
        second = self.job()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.lineage.count(), 1)

    def test_unverified_domain_blocks_fetch(self):
        self.real_trigger()
        CompanyDomain.objects.update(state='candidate')
        job = self.job()
        self.assertEqual(job.state, 'pending_domain')
        self.assertFalse(routing.advance(job.pk))
        self.assertFalse(DiscoveryJob.objects.exists())

    def test_verified_external_domain_requires_manual_approval(self):
        self.real_trigger()
        CompanyDomain.objects.update(url='https://new.example.org/team/', origin='https://new.example.org')
        job = self.job()
        self.assertEqual(job.state, 'pending_approval')
        routing.advance(job.pk)
        candidate = DiscoveredURL.objects.get()
        self.assertTrue(candidate.manual_review_required)
        self.assertEqual(candidate.decision, 'pending')
        self.assertFalse(DiscoveryJob.objects.exists())

    def real_trigger(self):
        # Exercise real-source routing with locally supplied fictional HTML and
        # a validated stub response; never a provider or business-site request.
        response = demo_response(self.source.url)
        self.source.collector = 'http'
        self.source.save()
        self.trigger = capture_page(self.source, self.source.url, hashlib.sha256(response.body).hexdigest(),
                                    response.text, provider='live')[0]
        Evaluation.objects.filter(pk=self.trigger.pk).update(state='running', lease_token=self.token)
        services.save_answers(self.trigger.pk, provider.mock_response(self.trigger.request), self.token)
        self.trigger.refresh_from_db()

    def test_expired_pilot_does_not_start(self):
        self.pilot.expires_at = timezone.now() - timedelta(seconds=1)
        self.pilot.save()
        with self.assertRaises(ValueError):
            self.job()

    def test_no_recursive_company_trigger(self):
        job = self.job()
        self.trigger.company_job = job
        self.trigger.save()
        self.assertFalse(routing.eligible(self.trigger))

    def test_fetch_caps_apply_to_retries_and_redirects(self):
        job = self.job()
        CompanyJob.objects.filter(pk=job.pk).update(max_fetches=1)
        routing.reserve_fetch(job.pk, self.source.url)
        with self.assertRaises(accounting.Deferred):
            routing.reserve_fetch(job.pk, self.source.url)
        job.refresh_from_db()
        self.assertEqual(job.fetches, 1)

    def test_scope_change_stops_company_work(self):
        job = self.job()
        self.source.allowed_paths = '/different'
        self.source.save()
        with self.assertRaises(accounting.Deferred):
            routing.reserve_fetch(job.pk, self.source.url)

    def test_external_proposal_cannot_trigger_policy(self):
        from discovery.services import register
        from automation.policy import consider_pending
        job = self.job()
        routing.advance(job.pk)
        job.refresh_from_db()
        SitePolicy.objects.create(campaign=self.pilot.campaign, enabled=True, min_url_score=35)
        with patch('automation.policy.consider') as consider:
            candidate = register(job.run, 'https://unapproved.example.org/sales/', label='Merchant services sales team', company_job=job)
            consider_pending()
        consider.assert_not_called()
        self.assertTrue(candidate.manual_review_required)
        self.assertFalse(DiscoveryJob.objects.filter(url=candidate.url).exists())

    def test_demo_end_to_end_and_idempotent(self):
        release_lease(self.token)
        output = io.StringIO()
        call_command('jev', 'demo', stdout=output)
        self.assertEqual(CompanyJob.objects.get().state, 'completed')
        self.assertTrue(Judgment.objects.filter(question_id='person_sales_role', label='direct_sales').exists())
        self.assertTrue(Person.objects.filter(name='Jordan Example').exists())
        self.assertFalse(Attempt.objects.exists())
        counts = (Evaluation.objects.count(), DiscoveryJob.objects.count(), Lead.objects.count())
        call_command('jev', 'demo', stdout=io.StringIO())
        self.assertEqual(counts, (Evaluation.objects.count(), DiscoveryJob.objects.count(), Lead.objects.count()))
        self.assertEqual(CompanyJob.objects.count(), 1)

    def test_mock_results_from_real_sources_cannot_route(self):
        self.source.collector = 'html'
        self.source.save()
        self.assertFalse(routing.eligible(self.trigger))

    def test_cache_hit_attributed_and_counted_against_job_packet_limit(self):
        from classification.models import EvaluationUse
        job = self.job()
        CompanyJob.objects.filter(pk=job.pk).update(max_evaluations=1)
        response = demo_response(self.source.url)
        before = Evaluation.objects.count()
        capture_page(self.source, self.source.url, self.trigger.document.content_hash, response.text, company_job=job)
        self.assertEqual(Evaluation.objects.count(), before)
        uses = EvaluationUse.objects.filter(company_job=job)
        self.assertEqual(uses.values('evaluation_id').distinct().count(), 1)
        self.assertTrue(uses.get().cache_hit)

    def test_manual_external_review_flag_survives_normal_registration(self):
        from discovery.services import register
        job = self.job()
        routing.advance(job.pk)
        job.refresh_from_db()
        url = 'https://unapproved.example.org/sales/'
        register(job.run, url, label='Merchant services sales team', company_job=job)
        with patch('automation.policy.consider') as consider:
            candidate = register(job.run, url, label='Merchant services sales team')
        consider.assert_not_called()
        self.assertTrue(candidate.manual_review_required)

    def test_model_confidence_does_not_override_human_rejection(self):
        self.trigger.judgments.update(review_state='rejected')
        self.assertFalse(routing.eligible(self.trigger))

    def test_pilot_pause_keeps_queued_page_without_consuming_quota(self):
        from discovery.services import process
        job = self.job()
        routing.advance(job.pk)
        self.pilot.active = False
        self.pilot.save()
        page = DiscoveryJob.objects.filter(company_job=job).first()
        process(page)
        page.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(page.status, 'queued')
        self.assertEqual(job.fetches, 0)

    def test_old_retrieval_cannot_route_even_with_new_model_result(self):
        EvidenceDocument.objects.update(retrieved_at=timezone.now() - timedelta(days=8), last_checked=timezone.now() - timedelta(days=8))
        self.trigger.refresh_from_db()
        self.assertFalse(routing.eligible(self.trigger))


@override_settings(JEV_MODE='mock', JEV_TOKEN_COUNTER='')
class ContractAndConsoleTests(TestCase):
    def setUp(self):
        _, evaluations = seed_demo()
        self.evaluation = evaluations[0]
        self.response = provider.mock_response(self.evaluation.request)

    def test_contract_rejects_unexpected_model(self):
        self.response['model'] = 'jev-latest'
        with self.assertRaises(contracts.ContractError):
            contracts.validate_response(self.evaluation.request, self.response)

    def test_contract_rejects_bad_distributions_and_confidence(self):
        for bad in (float('nan'), -1, 2, True, None):
            response = copy.deepcopy(self.response)
            next(iter(response['answers'].values()))['confidence'] = bad
            with self.subTest(value=bad), self.assertRaises(contracts.ContractError):
                contracts.validate_response(self.evaluation.request, response)

    def test_material_probability_drift_has_bounded_sanitized_diagnostic(self):
        answer = next(iter(self.response['answers'].values()))
        answer['probabilities'] = {key: value * .95 for key, value in answer['probabilities'].items()}
        with self.assertRaises(contracts.ContractError) as failure:
            contracts.validate_response(self.evaluation.request, self.response)
        message = str(failure.exception)
        self.assertIn('choices=', message)
        self.assertIn('sum=0.950000', message)
        self.assertIn('delta=0.050000', message)
        self.assertLessEqual(len(message), 180)
        self.assertNotIn(self.evaluation.document.text[:20], message)

    def test_probability_drift_just_over_one_percent_is_rejected(self):
        answer = next(iter(self.response['answers'].values()))
        answer['probabilities'] = {key: value * .9899999995 for key, value in answer['probabilities'].items()}
        with self.assertRaises(contracts.ContractError):
            contracts.validate_response(self.evaluation.request, self.response)

    def test_null_and_missing_usage_are_unknown(self):
        for body in (None, [], {'usage': None}, {'usage': {'input_tokens': True, 'output_tokens': 2}}):
            self.assertIsNone(contracts.usage_of(body))

    def test_unknown_answer_is_retained_for_review(self):
        token = acquire_lease()
        person = Evaluation.objects.filter(person__isnull=False).first()
        services.process(person, token, mock_only=True)
        judgment = person.judgments.get(question_id='person_merchant_services')
        self.assertEqual((judgment.label, judgment.review_state), ('unknown', 'needs_evidence'))

    def test_staff_only_console_and_csrf_controls(self):
        self.assertEqual(self.client.get('/classification/').status_code, 302)
        user = get_user_model().objects.create_user('operator', password='local-fixture-password', is_staff=True)
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        self.assertContains(client.get('/classification/'), 'Jev evidence review')
        self.assertEqual(client.post('/classification/control/', {'action': 'pause', 'reason': 'test'}).status_code, 403)
        self.assertEqual(client.get('/classification/control/').status_code, 405)
        self.assertContains(client.get(f'/classification/evaluations/{self.evaluation.pk}/'), 'Exact saved evidence')

    def test_doctor_never_calls_transport_or_prints_key(self):
        output = io.StringIO()
        with override_settings(TYPESAFE_API_KEY='DO-NOT-PRINT-THIS'), patch('classification.provider._post') as post:
            call_command('jev', 'doctor', stdout=output)
        post.assert_not_called()
        self.assertNotIn('DO-NOT-PRINT-THIS', output.getvalue())
        self.assertTrue(json.loads(output.getvalue())['key_present'])

    def test_retry_after_milliseconds_and_http_date(self):
        from email.utils import format_datetime
        date = format_datetime(timezone.now() + timedelta(minutes=5))
        when = services.retry_at(1, provider.Result(429, None, retry_after=date, retry_after_ms='600000'))
        self.assertGreater(when, timezone.now() + timedelta(seconds=599))

    def test_budget_input_rejects_nonfinite(self):
        for amount in ('NaN', 'Infinity', '-1', 'abc', '0.0000000001'):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                accounting.set_allowance(amount, 'test', 'reason')

    def test_human_review_has_audit_event_and_no_contact_promotion(self):
        from classification.models import ControlEvent
        token = acquire_lease()
        services.process(self.evaluation, token, mock_only=True)
        user = get_user_model().objects.create_user('reviewer', is_staff=True)
        self.client.force_login(user)
        judgment = self.evaluation.judgments.first()
        response = self.client.post(f'/classification/judgments/{judgment.pk}/review/',
            {'state': 'confirmed', 'reason': 'Matches saved company evidence.'})
        self.assertEqual(response.status_code, 302)
        judgment.refresh_from_db()
        self.assertEqual(judgment.review_state, 'confirmed')
        self.assertTrue(ControlEvent.objects.filter(action='review_judgment').exists())
        self.assertFalse(Lead.objects.exists())


class TransportTests(SimpleTestCase):
    def test_company_quota_callback_runs_before_dns_and_each_redirect(self):
        from leads.services.network import fetch, FetchError
        from unittest.mock import MagicMock
        response = MagicMock(status=302)
        response.getheaders.return_value = [('Location', 'https://approved.example/team/')]
        connection = MagicMock()
        connection.getresponse.return_value = response
        attempts = []
        def quota(url):
            attempts.append(url)
            if len(attempts) == 2:
                raise FetchError('Fixture quota reached.')
        with patch('leads.services.network.public_addresses', return_value=['8.8.8.8']) as dns, \
                patch('leads.services.network.PinnedHTTPS', return_value=connection):
            with self.assertRaises(FetchError):
                fetch('https://approved.example/', 'fixture', before_attempt=quota)
        self.assertEqual(len(attempts), 2)
        dns.assert_called_once()
        connection.request.assert_called_once()

    def test_http_adapter_makes_one_request_without_redirects_or_hidden_retries(self):
        import httpx
        seen = []
        async def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={'Location': 'https://elsewhere.example/'}, json={})
        with override_settings(TYPESAFE_API_KEY='synthetic-key'), patch('classification.provider.httpx.AsyncHTTPTransport',
                return_value=httpx.MockTransport(handler)) as transport:
            result = asyncio.run(provider._post({'model': 'fixture', 'state': {}, 'questions': {}}))
        transport.assert_called_once_with(retries=0)
        self.assertEqual(result.status, 302)
        self.assertEqual(len(seen), 1)
        self.assertEqual(str(seen[0].url), provider.ENDPOINT)

    def test_transport_timeout_is_sanitized(self):
        import httpx
        async def handler(request):
            raise httpx.ReadTimeout('secret provider body', request=request)
        with patch('classification.provider.httpx.AsyncHTTPTransport', return_value=httpx.MockTransport(handler)):
            result = asyncio.run(provider._post({}))
        self.assertEqual(result.error, 'timeout')
        self.assertIsNone(result.body)

    def test_oversize_response_rejected(self):
        import httpx
        async def handler(request):
            return httpx.Response(200, content=b'x' * (256 * 1024 + 1))
        with patch('classification.provider.httpx.AsyncHTTPTransport', return_value=httpx.MockTransport(handler)):
            result = asyncio.run(provider._post({}))
        self.assertEqual(result.error, 'response_too_large')
