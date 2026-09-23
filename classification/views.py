from decimal import Decimal
from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from leads.views import staff_required
from . import accounting
from .models import Attempt, CompanyDomain, CompanyJob, ControlEvent, Evaluation, JevControl, Judgment, Pilot


@staff_required
def home(request):
    wallet = JevControl.objects.get(pk='jev')
    evaluations = Evaluation.objects.select_related('company', 'person', 'document__source').order_by('-created_at')
    return render(request, 'classification/home.html', {
        'mode': settings.JEV_MODE, 'capture': settings.JEV_CAPTURE_ENABLED, 'routing': settings.JEV_ROUTING_ENABLED,
        'wallet': wallet, 'spent': Decimal(wallet.spent_nusd) / 10**9,
        'reserved': Decimal(wallet.reserved_nusd) / 10**9, 'remaining': Decimal(wallet.remaining_nusd) / 10**9,
        'readiness': accounting.diagnostics(wallet), 'evaluations': Paginator(evaluations, 30).get_page(request.GET.get('page')),
        'domains': CompanyDomain.objects.select_related('company', 'span__document').filter(state='candidate')[:30],
        'jobs': CompanyJob.objects.select_related('company', 'pilot', 'source').order_by('-created_at')[:30],
        'pilots': Pilot.objects.order_by('-created_at')[:10], 'attempts': Attempt.objects.order_by('-created_at')[:20],
    })


@staff_required
def detail(request, pk):
    evaluation = get_object_or_404(Evaluation.objects.select_related('document__source', 'company', 'person'), pk=pk)
    ids = {i for values in evaluation.bindings.values() for i in values}
    return render(request, 'classification/detail.html', {'evaluation': evaluation,
        'spans': evaluation.document.spans.filter(pk__in=ids), 'judgments': evaluation.judgments.all(),
        'attempts': evaluation.attempt_history.all(), 'uses': evaluation.uses.count()})


@require_POST
@staff_required
def control(request):
    reason = request.POST.get('reason', '').strip()[:1000]
    try:
        if not reason:
            raise ValueError('A reason is required for the audit log.')
        action = request.POST.get('action')
        if action in ('pause', 'resume'):
            accounting.set_paused(action == 'pause', request.user.username, reason)
        elif action == 'allowance':
            accounting.set_allowance(request.POST.get('usd', ''), request.user.username, reason)
        else:
            raise ValueError('Unknown control action.')
        messages.success(request, 'Control saved. Existing usage and retry deadlines are retained.')
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect('classification:home')


@require_POST
@staff_required
def domain(request, pk):
    get_object_or_404(CompanyDomain, pk=pk)
    from .routing import verify_domain
    try:
        verify_domain(pk, request.user.username, request.POST.get('reason', '')[:1000])
        messages.success(request, 'Company-domain relationship verified. Source approval and scope are still required.')
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect('classification:home')


@require_POST
@staff_required
def review(request, pk):
    judgment = get_object_or_404(Judgment, pk=pk)
    state = request.POST.get('state')
    reason = request.POST.get('reason', '').strip()[:1000]
    if state not in ('confirmed', 'rejected', 'needs_evidence') or not reason:
        messages.error(request, 'Choose a review outcome and supply a reason.')
    else:
        from django.db import transaction
        with transaction.atomic():
            before = judgment.review_state
            judgment.review_state = state
            judgment.save(update_fields=['review_state'])
            ControlEvent.objects.create(actor=request.user.username, action='review_judgment', reason=reason,
                data={'judgment': judgment.pk, 'before': before, 'after': state})
        messages.success(request, 'Review saved. Accepted contacts and source permissions are unchanged.')
    return redirect('classification:detail', pk=judgment.evaluation_id)
