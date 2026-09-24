"""The only admission path for paid attempts. Integer money, durable reservations."""
from datetime import datetime, time, timedelta, timezone as utc
from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone
from leads.models import WorkerLease
from .contracts import ContractError, digest, input_count, packet_state, usage_of, validate_request
from .models import Attempt, ControlEvent, DailyUsage, Evaluation, JevControl

RESERVATION_TOKENS = 66000
PRICE_NUSD = 42
RESERVATION_NUSD = RESERVATION_TOKENS * PRICE_NUSD


class Deferred(Exception):
    def __init__(self, reason, until=None):
        self.reason = reason
        self.until = until or timezone.now() + timedelta(seconds=60)
        super().__init__(reason)


def tomorrow(now=None):
    now = (now or timezone.now()).astimezone(utc.utc)
    return datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=utc.utc)


def check_lease(token):
    if not token or not WorkerLease.objects.filter(key='collector', token=token,
            heartbeat_at__gt=timezone.now() - timedelta(seconds=600)).exists():
        raise Deferred('Collector lease is not current.')


def readiness():
    problems = []
    if settings.JEV_MODE != 'live':
        problems.append('Live mode is disabled.')
    if not settings.TYPESAFE_API_KEY:
        problems.append('TypeSafe API key is absent.')
    if not settings.JEV_PRICE_CONFIRMED:
        problems.append('Confirm model/account access and the documented price before live mode.')
    if not settings.JEV_TOKEN_COUNTER and not settings.JEV_ALLOW_ESTIMATED_TOKENS:
        problems.append('Configure a verified token counter or explicitly opt into estimated-token calibration.')
    if not settings.JEV_DAILY_ALLOWANCE_NUSD:
        problems.append('Configure a positive daily spending allowance.')
    if connection.vendor == 'sqlite':
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA synchronous')
            if cursor.fetchone()[0] < 2:
                problems.append('SQLite synchronous=FULL is required.')
        if connection.settings_dict.get('OPTIONS', {}).get('transaction_mode') != 'IMMEDIATE':
            problems.append('SQLite IMMEDIATE transactions are required.')
    return problems


def verify_ledger(control):
    totals = Attempt.objects.aggregate(spent=Sum('cost_nusd'))
    reserved = Attempt.objects.filter(cost_nusd__isnull=True).aggregate(total=Sum('reserved_nusd'))['total'] or 0
    if control.spent_nusd != (totals['spent'] or 0) or control.reserved_nusd != reserved:
        raise Deferred('Ledger totals disagree; reconcile before dispatch.')


def daily_exposure_nusd(day):
    settled = Attempt.objects.filter(day=day).aggregate(total=Sum('cost_nusd'))['total'] or 0
    reserved = Attempt.objects.filter(day=day, cost_nusd__isnull=True).aggregate(total=Sum('reserved_nusd'))['total'] or 0
    return settled + reserved


def diagnostics(control):
    """Read-only operator preflight; reserve/dispatch still recheck all gates."""
    problems = readiness()
    try:
        verify_ledger(control)
    except Deferred as exc:
        problems.append(exc.reason)
    if control.paused:
        problems.append('Jev is paused; review the saved pause reason before resuming.')
    if control.active_attempt:
        problems.append('An attempt owns dispatch; uncertain owners require recovery.')
    if control.next_allowed_at > timezone.now():
        problems.append('Global request cooldown is still active.')
    today = timezone.now().astimezone(utc.utc).date()
    if settings.JEV_DAILY_ALLOWANCE_NUSD and daily_exposure_nusd(today) + RESERVATION_NUSD > settings.JEV_DAILY_ALLOWANCE_NUSD:
        problems.append('Daily spending allowance cannot cover another full reservation.')
    return problems


def valid_evidence(evaluation):
    from .evidence import source_authorized, valid_span
    from .models import ContactCandidate, EvidenceSpan
    from automation.policy import scope_hash
    from leads.services.extraction import signature
    doc = evaluation.document
    doc.source.refresh_from_db()
    from .questions import LAYERED_VERSION, VERSION, company_questions, page_questions, person_questions
    if (not evaluation.company_id or evaluation.catalog_version not in (VERSION, LAYERED_VERSION) or
            not isinstance(evaluation.request, dict) or evaluation.model != evaluation.request.get('model')):
        return False
    cache_window = int(evaluation.created_at.timestamp()) // (7 * 86400)
    base_key = digest([doc.fingerprint, evaluation.company_id, evaluation.person_id, evaluation.provider,
                       evaluation.catalog_version, evaluation.request, cache_window])
    valid_cache_keys = {base_key}
    if evaluation.company_job_id:
        valid_cache_keys.add(digest([base_key, evaluation.company_job_id]))
    if evaluation.cache_key not in valid_cache_keys:
        return False
    if not source_authorized(doc.source, doc.url) or scope_hash(doc.source) != doc.scope_hash or signature(doc.source) != doc.extraction_signature:
        return False
    if doc.expires_at and doc.expires_at <= timezone.now():
        return False
    questions = evaluation.request.get('questions') if isinstance(evaluation.request, dict) else None
    bindings = evaluation.bindings
    if (not isinstance(questions, dict) or not isinstance(bindings, dict) or
            set(bindings) != set(questions) or
            any(not isinstance(values, list) or not values or
                any(type(span_id) is not int or span_id < 1 for span_id in values)
                for values in bindings.values())):
        return False
    ids = {i for values in bindings.values() for i in values}
    spans = list(EvidenceSpan.objects.filter(pk__in=ids, document=doc).select_related('document'))
    if not ids or len(spans) != len(ids) or not all(valid_span(s) for s in spans):
        return False
    span_map = {span.pk: span for span in spans}
    state = evaluation.request.get('state')
    if not isinstance(state, dict):
        return False
    state_spans, state_blocks = state.get('spans', []), state.get('blocks', [])
    if not isinstance(state_spans, list) or not isinstance(state_blocks, list):
        return False
    if state.get('company') != evaluation.company.name:
        return False
    if evaluation.person_id:
        if state.get('person') != evaluation.person.name:
            return False
    elif 'person' in state:
        return False
    supplied = []
    for item in state_spans:
        if not isinstance(item, dict) or set(item) != {'id', 'text'} or type(item['id']) is not int:
            return False
        supplied.append((item['id'], item['text']))
    for item in state_blocks:
        if (not isinstance(item, dict) or set(item) != {'id', 'span_id', 'text'} or
                not isinstance(item['id'], str) or not item['id'] or
                type(item['span_id']) is not int or
                not isinstance(item['text'], str) or not item['text']):
            return False
        supplied.append((item['span_id'], item['text']))
    block_ids = [item['id'] for item in state_blocks]
    if len(block_ids) != len(set(block_ids)):
        return False
    if len(supplied) != len(ids) or {span_id for span_id, _ in supplied} != ids:
        return False
    if any(span_id not in span_map or text != span_map[span_id].document.text[
            span_map[span_id].start:span_map[span_id].end] for span_id, text in supplied):
        return False
    contacts = state.get('contacts', [])
    if not isinstance(contacts, list):
        return False
    contact_ids = [item.get('id') for item in contacts if isinstance(item, dict)]
    if (len(contact_ids) != len(contacts) or
            any(type(contact_id) is not int for contact_id in contact_ids) or
            len(contact_ids) != len(set(contact_ids))):
        return False
    contact_questions = {key for key in questions if key.startswith('contact_')}
    if contact_questions != {f'contact_{contact_id}' for contact_id in contact_ids}:
        return False
    candidates = ContactCandidate.objects.in_bulk(contact_ids)
    for item in contacts:
        candidate = candidates.get(item['id'])
        if (not candidate or set(item) != {'id', 'value', 'kind', 'shared_hint'} or
                candidate.span_id not in bindings[f"contact_{item['id']}"] or
                candidate.company_id != evaluation.company_id or
                not (candidate.person_id == evaluation.person_id or
                     (candidate.person_id is None and candidate.shared_hint)) or
                item['value'] != candidate.value or
                item['kind'] != candidate.kind or item['shared_hint'] is not candidate.shared_hint):
            return False
    if evaluation.catalog_version == LAYERED_VERSION:
        expected_state_keys = {'company', 'blocks'} | ({'spans'} if 'page_purpose' in questions else set())
        if set(state) != expected_state_keys:
            return False
        expected = page_questions(block_ids)
        expected_keys = {f'candidate_block_{block_id}' for block_id in block_ids}
        if 'page_purpose' in questions:
            expected_keys.add('page_purpose')
        if set(questions) != expected_keys:
            return False
        block_map = {item['id']: item['span_id'] for item in state_blocks}
        for key in expected_keys - {'page_purpose'}:
            if bindings[key] != [block_map[key.removeprefix('candidate_block_')]]:
                return False
        if 'page_purpose' in expected_keys:
            expected_binding = [item['id'] for item in state_spans] + [item['span_id'] for item in state_blocks]
            if bindings['page_purpose'] != expected_binding:
                return False
    elif evaluation.person_id:
        if set(state) != {'company', 'person', 'spans', 'contacts'} or len(state_spans) != 1:
            return False
        expected = person_questions(evaluation.person.name, evaluation.company.name,
                                    [candidates[contact_id] for contact_id in contact_ids])
    else:
        if set(state) != {'company', 'spans', 'contacts'} or len(state_spans) != 1:
            return False
        expected = company_questions(evaluation.company.name)
    if evaluation.catalog_version == VERSION:
        batches, current = [], {}
        for key, question in expected.items():
            candidate_group = current | {key: question}
            candidate = {'model': evaluation.model, 'state': packet_state(state, candidate_group),
                         'questions': candidate_group}
            if input_count(candidate)[0] > settings.JEV_MAX_INPUT_TOKENS and current:
                batches.append(current)
                current = {}
            current[key] = question
        if current:
            batches.append(current)
        if questions not in batches:
            return False
        subject_span = state_spans[0]['id']
        if any(values != [subject_span] for values in bindings.values()):
            return False
    if any(question != expected[key] for key, question in questions.items()):
        return False
    return True


@transaction.atomic
def reserve(evaluation_id, token):
    # SQLite acquires its write lock on BEGIN IMMEDIATE; PostgreSQL locks this singleton.
    control = JevControl.objects.select_for_update().get(pk='jev')
    verify_ledger(control)
    check_lease(token)
    evaluation = Evaluation.objects.select_for_update().select_related('document__source').get(pk=evaluation_id)
    if evaluation.provider != 'live' or evaluation.state not in ('queued', 'retry_wait', 'waiting'):
        raise Deferred('Evaluation is not eligible for live dispatch.')
    if evaluation.attempt_history.filter(state__in=('uncertain', 'dispatching', 'reserved')).exists():
        raise Deferred('Previous attempt is unresolved; explicit recovery required.')
    problems = readiness()
    if problems:
        raise Deferred(problems[0])
    if control.paused:
        raise Deferred('Jev paused: ' + control.reason)
    if not valid_evidence(evaluation):
        raise ContractError('Evidence expired, changed, or its scope was revoked.')
    now = timezone.now()
    if control.active_attempt:
        raise Deferred('An attempt owns dispatch; uncertain owners require recovery.')

    if control.next_allowed_at > now:
        raise Deferred('Global request cooldown.', control.next_allowed_at)
    if evaluation.attempts >= 3:
        raise ContractError('Three-attempt ceiling reached.')
    if evaluation.available_at > now:
        raise Deferred('Evaluation backoff.', evaluation.available_at)
    count, method, exact = validate_request(evaluation.request)
    if count > settings.JEV_MAX_INPUT_TOKENS:
        raise ContractError('Complete request exceeds the configured input-size bound.')
    if not exact and not settings.JEV_ALLOW_ESTIMATED_TOKENS:
        raise Deferred('Exact token counter required.')
    day, _ = DailyUsage.objects.get_or_create(day=now.astimezone(utc.utc).date())
    if settings.JEV_DAILY_ALLOWANCE_NUSD and daily_exposure_nusd(day.day) + RESERVATION_NUSD > settings.JEV_DAILY_ALLOWANCE_NUSD:
        raise Deferred('Daily spending allowance exhausted or reserved.', tomorrow(now))
    if evaluation.company_job_id:
        from .routing import reserve_model_attempt
        reserve_model_attempt(evaluation.company_job_id)
    attempt = Attempt.objects.create(evaluation=evaluation, ordinal=evaluation.attempts + 1, day=day.day,
        lease_token=token, request_hash=digest(evaluation.request), reserved_nusd=RESERVATION_NUSD,
        estimated_tokens=count, counter=method)
    day.attempts += 1
    day.save(update_fields=['attempts'])
    control.reserved_nusd += RESERVATION_NUSD
    control.active_attempt = attempt.pk
    control.save(update_fields=['reserved_nusd', 'active_attempt'])
    evaluation.attempts += 1
    evaluation.state, evaluation.lease_token = 'running', token
    evaluation.save(update_fields=['attempts', 'state', 'lease_token'])
    return attempt


@transaction.atomic
def dispatch(attempt_id, request, token):
    control = JevControl.objects.select_for_update().get(pk='jev')
    attempt = Attempt.objects.select_related('evaluation__document__source').get(pk=attempt_id)
    check_lease(token)
    if readiness() or control.paused or control.active_attempt != attempt.pk:
        raise Deferred('Live dispatch gate changed.')
    verify_ledger(control)
    if settings.JEV_DAILY_ALLOWANCE_NUSD and daily_exposure_nusd(attempt.day) > settings.JEV_DAILY_ALLOWANCE_NUSD:
        raise Deferred('A spending allowance was reduced after admission.')
    if attempt.state != 'reserved' or attempt.lease_token != token or attempt.request_hash != digest(request):
        raise ContractError('Spent, stale or mismatched admission permit.')
    if attempt.day != timezone.now().astimezone(utc.utc).date():
        raise Deferred('Admission permit expired at UTC midnight.', timezone.now())
    if not valid_evidence(attempt.evaluation):
        raise ContractError('Source/evidence changed before dispatch.')
    if attempt.evaluation.company_job_id:
        from .routing import check_work
        check_work(attempt.evaluation.company_job)
    attempt.state, attempt.dispatched_at = 'dispatching', timezone.now()
    attempt.save(update_fields=['state', 'dispatched_at'])


@transaction.atomic
def settle(attempt_id, result, elapsed_ms):
    control = JevControl.objects.select_for_update().get(pk='jev')
    attempt = Attempt.objects.select_for_update().get(pk=attempt_id)
    if attempt.completed_at:
        return attempt
    usage = usage_of(result.body)
    if usage is not None:
        attempt.input_tokens, attempt.output_tokens = usage['input_tokens'], usage['output_tokens']
        attempt.cost_nusd = usage['input_tokens'] * attempt.unit_price_nusd
        control.reserved_nusd -= attempt.reserved_nusd
        control.spent_nusd += attempt.cost_nusd
        attempt.state = 'settled'
        if usage['input_tokens'] > settings.JEV_MAX_INPUT_TOKENS or attempt.cost_nusd > attempt.reserved_nusd:
            control.paused, control.reason = True, 'Reported usage exceeded input/reservation bounds. Review token counting.'
    else:
        attempt.state = 'uncertain'
    attempt.http_status, attempt.request_id = result.status, result.request_id
    attempt.error_code = result.error
    attempt.latency_ms = max(0, int(elapsed_ms))
    attempt.completed_at = timezone.now()
    attempt.save()
    if control.active_attempt == attempt.pk:
        control.active_attempt = None  # local request/transport has ended, unlike an unknown crashed owner
    control.next_allowed_at = max(control.next_allowed_at, timezone.now() + timedelta(seconds=1))
    if result.status in (401, 403):
        control.paused, control.reason = True, 'Provider denied credentials/account; review configuration.'
    control.save()
    day = DailyUsage.objects.get(day=attempt.day)
    if result.status == 200 and not result.error:
        day.succeeded += 1
    else:
        day.failed += 1
    day.save()
    return attempt


@transaction.atomic
def abort_unsent(attempt_id, reason):
    """Only a still-reserved attempt is provably unsent; attempts stay consumed."""
    control = JevControl.objects.select_for_update().get(pk='jev')
    attempt = Attempt.objects.get(pk=attempt_id)
    if attempt.state != 'reserved':
        return False
    attempt.cost_nusd, attempt.state, attempt.completed_at, attempt.error_code = 0, 'not_sent', timezone.now(), 'admission_revoked'
    attempt.save()
    control.reserved_nusd -= attempt.reserved_nusd
    if control.active_attempt == attempt.pk:
        control.active_attempt = None
    control.next_allowed_at = max(control.next_allowed_at, timezone.now() + timedelta(seconds=1))
    control.save()
    return True


@transaction.atomic
def recover_attempt(attempt_id, actor, reason, worker_stopped=False, billed_nusd=None):
    if not reason.strip():
        raise ValueError('A recovery reason is required.')
    control = JevControl.objects.select_for_update().get(pk='jev')
    attempt = Attempt.objects.get(pk=attempt_id)
    if attempt.cost_nusd is not None or (attempt.state == 'recovered' and billed_nusd is None):
        raise ValueError('This attempt is already reconciled or explicitly recovered; no recovery action remains.')
    if control.active_attempt == attempt.pk:
        if not worker_stopped:
            raise ValueError('Confirm the previous worker/transport is stopped before releasing dispatch.')
        control.active_attempt = None
        attempt.state, attempt.completed_at = 'uncertain', timezone.now()
    if billed_nusd is not None:
        if attempt.cost_nusd is not None:
            raise ValueError('This attempt has already been reconciled.')
        if type(billed_nusd) is not int or billed_nusd < 0:
            raise ValueError('A nonnegative verified billing amount is required.')
        control.reserved_nusd -= attempt.reserved_nusd
        control.spent_nusd += billed_nusd
        attempt.cost_nusd, attempt.state = billed_nusd, 'settled'
    elif attempt.state == 'uncertain':
        # Explicit authorization to retry, with the full unknown charge retained.
        attempt.state = 'recovered'
    attempt.save()
    control.next_allowed_at = max(control.next_allowed_at, timezone.now() + timedelta(seconds=1))
    control.save()
    evaluation = Evaluation.objects.select_for_update().get(pk=attempt.evaluation_id)
    if attempt.ordinal == evaluation.attempts and evaluation.state in ('running', 'uncertain'):
        evaluation.state = 'waiting'
        evaluation.reason = 'Recovered uncertain attempt; retained charges/reservations.'
        evaluation.available_at = max(evaluation.available_at, control.next_allowed_at)
        evaluation.save(update_fields=['state', 'reason', 'available_at'])
    ControlEvent.objects.create(actor=actor, action='recover_attempt', reason=reason, data={
        'attempt': str(attempt.pk), 'worker_stopped': worker_stopped, 'billed_nusd': billed_nusd})


@transaction.atomic
def retry_settled_persistence(evaluation_id, actor, reason):
    if not reason.strip():
        raise ValueError('A recovery reason is required.')
    control = JevControl.objects.select_for_update().get(pk='jev')
    evaluation = Evaluation.objects.select_for_update().select_related('document__source').get(pk=evaluation_id)
    attempt = evaluation.attempt_history.order_by('-ordinal').first()
    if (evaluation.state != 'failed_persistence' or not attempt or attempt.cost_nusd is None or
            attempt.state != 'settled'):
        raise ValueError('Only a locally failed result with settled billing can be explicitly requeued.')
    if evaluation.attempts >= 3:
        raise ValueError('Three-attempt ceiling reached; the paid persistence failure is terminal.')
    if not valid_evidence(evaluation):
        raise ContractError('Evidence expired, changed, or its scope was revoked.')
    evaluation.state, evaluation.lease_token = 'waiting', ''
    evaluation.reason = 'Explicitly requeued after a settled local persistence failure.'
    evaluation.available_at = max(timezone.now(), control.next_allowed_at)
    evaluation.save(update_fields=['state', 'lease_token', 'reason', 'available_at'])
    ControlEvent.objects.create(actor=actor, action='retry_settled_persistence', reason=reason,
                                data={'evaluation': str(evaluation.pk), 'attempt': str(attempt.pk)})


@transaction.atomic
def set_paused(paused, actor, reason):
    control = JevControl.objects.select_for_update().get(pk='jev')
    control.paused, control.reason = paused, reason
    control.save(update_fields=['paused', 'reason'])
    ControlEvent.objects.create(actor=actor, action='pause' if paused else 'resume', reason=reason)
    # Resume cannot bypass waiting retry/rate/day timestamps or replenish a wallet.
    return control


@transaction.atomic
def set_allowance(usd, actor, reason):
    try:
        value = Decimal(usd) * 1_000_000_000
        if not value.is_finite() or value < 0 or value != value.to_integral_value() or value > 1_000_000_000_000:
            raise ValueError
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Use a nonnegative USD amount with at most nine decimal places (maximum $1000).')
    control = JevControl.objects.select_for_update().get(pk='jev')

    if not reason.strip():
        raise ValueError('A budget change reason is required.')
    before = control.allowance_nusd
    control.allowance_nusd = int(value)
    control.save(update_fields=['allowance_nusd'])
    ControlEvent.objects.create(actor=actor, action='allowance', reason=reason, data={
        'before_nusd': before, 'after_nusd': int(value), 'admission_gate': False})
