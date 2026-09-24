"""Durable, bounded evaluations executed only by the existing collector."""
import random
import time
from datetime import datetime, timedelta, timezone as utc
from email.utils import parsedate_to_datetime
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from . import accounting, provider
from .contracts import ContractError, digest, input_count, validate_request, validate_response
from .models import CompanyJob, Evaluation, EvaluationUse, Judgment
from .questions import LAYERED_VERSION, VERSION, company_questions, page_questions, person_questions


@transaction.atomic
def enqueue_subject(doc, company, span, *, person=None, contacts=(), provider='mock', company_job=None):
    questions = person_questions(person.name, company.name, contacts) if person else company_questions(company.name)
    state = {'company': company.name, 'spans': [{'id': span.pk, 'text': span.text}],
             'contacts': [{'id': c.pk, 'value': c.value, 'kind': c.kind, 'shared_hint': c.shared_hint} for c in contacts]}
    if person:
        state['person'] = person.name
    bindings = {key: [span.pk] for key in questions}
    return _enqueue(doc, company, state, questions, bindings, person=person, provider=provider,
                    company_job=company_job)


@transaction.atomic
def enqueue_page_blocks(doc, company, company_span, blocks, *, provider='mock'):
    state = {'company': company.name, 'blocks': [
        {'id': block['id'], 'span_id': block['span'].pk, 'text': block['span'].text} for block in blocks]}
    questions = page_questions([block['id'] for block in blocks])
    bindings = {'page_purpose': [company_span.pk] + [block['span'].pk for block in blocks]}
    bindings.update({f"candidate_block_{block['id']}": [block['span'].pk] for block in blocks})
    return _enqueue(doc, company, state, questions, bindings, provider=provider,
                    catalog_version=LAYERED_VERSION)


def _enqueue(doc, company, state, questions, bindings, *, person=None, provider='mock', company_job=None,
             catalog_version=VERSION):
    if provider not in ('mock', 'live'):
        raise ValueError('Provider must be mock or live.')
    base = {'model': settings.JEV_MODEL, 'state': state, 'questions': {}}
    batches, current = [], {}
    for key, question in questions.items():
        candidate = base | {'questions': current | {key: question}}
        if input_count(candidate)[0] > settings.JEV_MAX_INPUT_TOKENS and current:
            batches.append(current)
            current = {}
        current[key] = question
    if current:
        batches.append(current)
    results = []
    for group in batches:
        request_state = state
        group_bindings = {key: bindings[key] for key in group}
        if state.get('blocks'):
            group_ids = {key.removeprefix('candidate_block_') for key in group
                         if key.startswith('candidate_block_')}
            if group_ids:
                request_state = state | {'blocks': [block for block in state['blocks']
                                                   if block['id'] in group_ids]}
            if 'page_purpose' in group:
                all_block_spans = {block['span_id'] for block in state['blocks']}
                company_spans = [span_id for span_id in bindings['page_purpose']
                                 if span_id not in all_block_spans]
                supplied_spans = [block['span_id'] for block in request_state['blocks']]
                group_bindings['page_purpose'] = company_spans + supplied_spans
        request = {'model': settings.JEV_MODEL, 'state': request_state, 'questions': group}
        cache_window = int(timezone.now().timestamp()) // (7 * 86400)
        key = digest([doc.fingerprint, company.pk, person.pk if person else None, provider,
                      catalog_version, request, cache_window])
        existing = Evaluation.objects.filter(cache_key=key).first()
        if company_job:
            if existing and existing.state != 'succeeded' and existing.company_job_id != company_job.pk:
                # Do not make an unowned pending request spend on behalf of a job.
                key = digest([key, company_job.pk])
                existing = Evaluation.objects.filter(cache_key=key).first()
            job = CompanyJob.objects.select_for_update().get(pk=company_job.pk)
            uses = EvaluationUse.objects.filter(company_job=job)
            if not (existing and uses.filter(evaluation=existing).exists()) and uses.values('evaluation_id').distinct().count() >= job.max_evaluations:
                break
        evaluation, _ = Evaluation.objects.get_or_create(cache_key=key, defaults={
            'document': doc, 'company': company, 'person': person, 'company_job': company_job,
            'provider': provider, 'catalog_version': catalog_version, 'model': settings.JEV_MODEL,
            'request': request, 'bindings': group_bindings,
            'state': 'queued' if input_count(request)[0] <= settings.JEV_MAX_INPUT_TOKENS else 'too_large',
            'reason': '' if input_count(request)[0] <= settings.JEV_MAX_INPUT_TOKENS else 'Evidence block/question needs a smaller packet.'})
        EvaluationUse.objects.create(evaluation=evaluation, document=doc, checked_at=doc.last_checked,
                                     company_job=company_job, cache_hit=evaluation.state == 'succeeded')
        results.append(evaluation)
    return results


def retry_at(ordinal, result):
    delay = random.uniform(0.5, 1.0) * min(60, 2 * 2**max(0, ordinal - 1))
    for value, milliseconds in ((result.retry_after, False), (result.retry_after_ms, True)):
        if not value:
            continue
        try:
            number = float(value) / (1000 if milliseconds else 1)
            if number != number or abs(number) == float('inf'):
                continue
        except (ValueError, TypeError):
            if milliseconds:
                continue
            try:
                stamp = parsedate_to_datetime(value)
                if not stamp.tzinfo:
                    continue
                number = (stamp - timezone.now()).total_seconds()
            except (ValueError, TypeError, OverflowError):
                continue
        # Unrepresentably long requests remain deferred indefinitely, never clamped shorter.
        if number > (datetime.max.replace(tzinfo=utc.utc) - timezone.now()).total_seconds() - 60:
            return datetime.max.replace(tzinfo=utc.utc)
        delay = max(delay, number)
    return timezone.now() + timedelta(seconds=delay)


@transaction.atomic
def save_answers(evaluation_id, response, token):
    evaluation = Evaluation.objects.select_for_update().select_related('document__source').get(pk=evaluation_id)
    accounting.check_lease(token)
    if evaluation.lease_token != token or evaluation.state != 'running' or not accounting.valid_evidence(evaluation):
        raise ContractError('Stale evaluation/lease or revoked evidence.')
    answers = validate_response(evaluation.request, response)
    for key, answer in answers.items():
        label = answer['choice']
        review = 'needs_evidence' if label == 'unknown' else 'needs_review'
        Judgment.objects.update_or_create(evaluation=evaluation, question_id=key, defaults={
            'label': label, 'probabilities': answer['probabilities'], 'confidence': answer['confidence'],
            'evidence_ids': evaluation.bindings[key], 'review_state': review})
    evaluation.actual_model = response['model']
    evaluation.state, evaluation.completed_at, evaluation.reason = 'succeeded', timezone.now(), ''
    evaluation.save(update_fields=['actual_model', 'state', 'completed_at', 'reason'])
    # No Lead, ContactCandidate ownership, Source approval or discovery mutations here.


def process(evaluation, token, *, mock_only=False):
    attempt = None
    try:
        if evaluation.provider == 'mock':
            if not mock_only and settings.JEV_MODE != 'mock':
                return False
            with transaction.atomic():
                accounting.check_lease(token)
                current = Evaluation.objects.select_for_update().get(pk=evaluation.pk)
                if current.state not in ('queued', 'waiting', 'retry_wait'):
                    return False
                current.state, current.lease_token = 'running', token
                current.save(update_fields=['state', 'lease_token'])
            validate_request(evaluation.request)
            save_answers(evaluation.pk, provider.mock_response(evaluation.request), token)
            return True
        if mock_only or settings.JEV_MODE != 'live':
            return False
        attempt = accounting.reserve(evaluation.pk, token)
        start = time.monotonic()
        result = provider.evaluate_once(attempt.pk, evaluation.request, token)
        attempt = accounting.settle(attempt.pk, result, (time.monotonic() - start) * 1000)
        if attempt.state == 'uncertain' and result.status != 200:
            Evaluation.objects.filter(pk=evaluation.pk, lease_token=token).update(state='uncertain',
                available_at=retry_at(attempt.ordinal, result),
                reason='Usage unknown; full reservation retained. Explicit recovery required before another attempt.')
            return True
        if result.status == 200:
            if result.error:
                raise ContractError('Provider response could not be read within bounds.')
            save_answers(evaluation.pk, result.body, token)
        elif result.status in (429, 529, 500, 502, 503, 504) or result.error in ('timeout', 'transport_error'):
            state = 'retry_wait' if attempt.ordinal < 3 else 'failed'
            Evaluation.objects.filter(pk=evaluation.pk, lease_token=token).update(state=state,
                available_at=retry_at(attempt.ordinal, result), reason=result.error or f'Provider HTTP {result.status}; attempt counted.')
        else:
            Evaluation.objects.filter(pk=evaluation.pk, lease_token=token).update(state='failed',
                reason=f'Provider HTTP {result.status or "unknown"}; review configuration/contract.')
        return True
    except accounting.Deferred as exc:
        if attempt:
            accounting.abort_unsent(attempt.pk, exc.reason)
        Evaluation.objects.filter(Q(lease_token=token, state='running') | Q(state__in=('queued', 'waiting', 'retry_wait')),
            pk=evaluation.pk).update(state='waiting',
            reason=exc.reason[:1000], available_at=exc.until)
        return False
    except ContractError as exc:
        if attempt:
            accounting.abort_unsent(attempt.pk, str(exc))
        Evaluation.objects.filter(Q(lease_token=token, state='running') | Q(state__in=('queued', 'waiting', 'retry_wait')),
            pk=evaluation.pk).update(state='failed_contract', reason=str(exc)[:1000])
        return True
    except Exception:
        # Unknown failures after admission may be billable. Leave the dispatch owner
        # and reservation intact; recovery requires an explicit operator action.
        if attempt:
            Evaluation.objects.filter(pk=evaluation.pk, lease_token=token).update(
                state='uncertain', reason='Unexpected failure after reservation; inspect attempt and recover explicitly.')
        raise


def tick(token, *, mock_only=False):
    if settings.JEV_MODE == 'off' and not mock_only:
        return False
    from .models import JevControl
    control = JevControl.objects.filter(pk='jev').first()
    if not control or control.paused:
        return False
    provider_name = 'mock' if mock_only else settings.JEV_MODE
    evaluation = Evaluation.objects.filter(provider=provider_name, state__in=('queued', 'retry_wait', 'waiting'),
        available_at__lte=timezone.now()).select_related('document__source').order_by('available_at', 'created_at').first()
    if evaluation:
        return process(evaluation, token, mock_only=mock_only)
    if settings.JEV_ROUTING_ENABLED and not mock_only:
        from .routing import tick as route_tick
        return route_tick(token)
    return False
