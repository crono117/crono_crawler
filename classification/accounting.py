"""The only admission path for paid attempts. Integer money, durable reservations."""
from datetime import datetime, time, timedelta, timezone as utc
from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone
from leads.models import WorkerLease
from .contracts import ContractError, digest, usage_of, validate_request
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
    from .models import EvidenceSpan
    from automation.policy import scope_hash
    from leads.services.extraction import signature
    doc = evaluation.document
    doc.source.refresh_from_db()
    if not source_authorized(doc.source, doc.url) or scope_hash(doc.source) != doc.scope_hash or signature(doc.source) != doc.extraction_signature:
        return False
    if doc.expires_at and doc.expires_at <= timezone.now():
        return False
    ids = {i for values in evaluation.bindings.values() for i in values}
    spans = list(EvidenceSpan.objects.filter(pk__in=ids, document=doc).select_related('document'))
    return bool(ids) and len(spans) == len(ids) and all(valid_span(s) for s in spans)


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
