"""Bounded company work reuses DiscoveryJob; no second crawler or approval engine."""
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from automation.policy import collection_allowed, scope_hash
from leads.services.network import in_scope, origin
from discovery.models import Campaign, DiscoveredURL, DiscoveryJob, DiscoveryRun
from discovery.ranking import rank
from .accounting import Deferred, check_lease, valid_evidence
from .contracts import digest
from .models import (Company, CompanyDomain, CompanyJob, ControlEvent, Evaluation, Lineage,
                     OPEN_COMPANY_STATES, Pilot)


def owner_of(job):
    if getattr(job, 'company_job_id', None):
        return job.company_job_id
    setup = getattr(job, 'job', None)
    return getattr(setup, 'company_job_id', None)


def eligible(evaluation):
    if evaluation.person_id or evaluation.company_job_id or evaluation.state != 'succeeded' or not valid_evidence(evaluation):
        return False
    if (not evaluation.completed_at or not evaluation.document.retrieved_at or
            min(evaluation.completed_at, evaluation.document.last_checked) < timezone.now() - timedelta(days=7)):
        return False
    if evaluation.provider == 'mock' and evaluation.document.source.collector != 'demo':
        return False
    for answer in evaluation.judgments.all():
        if answer.review_state == 'rejected':
            continue
        if ((answer.question_id == 'company_technology' and answer.label == 'technology') or
                (answer.question_id == 'company_merchant_services' and answer.label in ('provider', 'both'))):
            probabilities = sorted(answer.probabilities.values(), reverse=True)
            if answer.probabilities.get(answer.label, 0) >= .9 and (answer.confidence or 0) >= .8 and probabilities[0] - probabilities[1] >= .2:
                return True
    return False


@transaction.atomic
def verify_domain(domain_id, actor, reason):
    domain = CompanyDomain.objects.select_related('span__document').get(pk=domain_id)
    from .evidence import valid_span
    if not reason.strip() or not valid_span(domain.span):
        raise ValueError('A review reason and retained source evidence are required.')
    domain.state, domain.reviewed_by, domain.review_note, domain.verified_at = 'verified', actor, reason, timezone.now()
    domain.save()
    ControlEvent.objects.create(actor=actor, action='verify_domain', reason=reason, data={'domain_id': domain.pk})
    # This does NOT approve a source, activate a campaign or widen its scope.
    return domain


@transaction.atomic
def queue_company(pilot_id, evaluation_id):
    pilot = Pilot.objects.select_for_update().select_related('campaign').get(pk=pilot_id)
    evaluation = Evaluation.objects.select_related('document__source').get(pk=evaluation_id)
    if not eligible(evaluation):
        raise ValueError('A recent, evidence-supported company relevance judgment is required.')
    if not pilot.active or pilot.expires_at <= timezone.now() or not pilot.campaign.active:
        raise ValueError('Pilot and campaign must be active and unexpired.')
    if not pilot.campaign.sources.filter(pk=evaluation.document.source_id).exists():
        raise ValueError('Triggering source is not in this campaign.')
    domains = list(CompanyDomain.objects.filter(company=evaluation.company, state='verified'))
    sources = list(pilot.campaign.sources.filter(approved=True))
    source = next((s for d in domains for s in sources if origin(s.url) == d.origin), None)
    key = digest([evaluation.company_id, origin(source.url) if source else 'unresolved', scope_hash(source) if source else '', 'sales_team'])
    existing = CompanyJob.objects.filter(key=key, state__in=OPEN_COMPANY_STATES).first()
    existing = existing or CompanyJob.objects.filter(key=key, created_at__gt=timezone.now() - timedelta(days=7)).first()
    if existing:
        Lineage.objects.get_or_create(company_job=existing, evaluation=evaluation, document=evaluation.document, relation='company_trigger')
        return existing
    if pilot.jobs.count() >= pilot.max_companies:
        raise ValueError('Pilot company limit reached; existing counters are retained.')
    job = CompanyJob.objects.create(company=evaluation.company, pilot=pilot, source=source, key=key,
        scope_hash=scope_hash(source) if source else '', state='queued' if source else ('pending_approval' if domains else 'pending_domain'),
        expires_at=min(pilot.expires_at, timezone.now() + timedelta(hours=24)))
    Lineage.objects.create(company_job=job, evaluation=evaluation, document=evaluation.document, relation='company_trigger')
    return job


def pause(job, message):
    CompanyJob.objects.filter(pk=job.pk).update(state='paused', reason=message[:1000])


def propose_domains(job, domains):
    """Manual review metadata only; never invoke discovery.register or policy.consider."""
    campaign = job.pilot.campaign
    for domain in domains[:5]:
        if (campaign.urls.count() >= campaign.max_candidates or
                Lineage.objects.filter(company_job__pilot=job.pilot, relation__in=('external_proposal', 'domain_proposal')).count() >= 25):
            break
        candidate, _ = DiscoveredURL.objects.get_or_create(campaign=campaign, url=domain.url, defaults={
            'origin': domain.origin, 'label': job.company.name, 'found_on': domain.span.document.url,
            'manual_review_required': True, 'method': 'company', 'score': 0})
        Lineage.objects.get_or_create(company_job=job, document=domain.span.document,
            url=domain.url, relation='domain_proposal')


def check_work(job):
    if not job.pilot.active or not job.pilot.campaign.active:
        raise Deferred('Company pilot/campaign is paused.')
    if job.state not in ('queued', 'discovering', 'classifying', 'needs_recipe_review'):
        raise Deferred('Company work is not runnable.')
    if min(job.expires_at, job.pilot.expires_at) <= timezone.now():
        raise Deferred('Company/pilot deadline reached.')
    if not job.source_id or not job.source.approved or scope_hash(job.source) != job.scope_hash:
        raise Deferred('Company source approval/scope changed.')
    if not job.pilot.campaign.sources.filter(pk=job.source_id).exists():
        raise Deferred('Company source removed from campaign.')


@transaction.atomic
def reserve_fetch(job_id, url):
    # Lock pilot before job in every counter path. All redirect hops call this.
    pilot_id = CompanyJob.objects.values_list('pilot_id', flat=True).get(pk=job_id)
    pilot = Pilot.objects.select_for_update().get(pk=pilot_id)
    job = CompanyJob.objects.select_for_update().select_related('source', 'pilot__campaign').get(pk=job_id)
    check_work(job)
    if origin(url) != origin(job.source.url):
        raise Deferred('Company job cannot fetch another origin.')
    if job.fetches >= job.max_fetches or pilot.fetches >= pilot.max_fetches:
        raise Deferred('Company/pilot fetch budget reached.')
    if job.processing_seconds + 30 > 900:
        raise Deferred('Company processing-time budget reached.')
    job.fetches += 1
    job.processing_seconds += 30  # reserve the full operation slice; crashes cannot refund it
    pilot.fetches += 1
    job.save(update_fields=['fetches', 'processing_seconds'])
    pilot.save(update_fields=['fetches'])


@transaction.atomic
def reserve_model_attempt(job_id):
    pilot_id = CompanyJob.objects.values_list('pilot_id', flat=True).get(pk=job_id)
    pilot = Pilot.objects.select_for_update().get(pk=pilot_id)
    job = CompanyJob.objects.select_for_update().select_related('source', 'pilot__campaign').get(pk=job_id)
    check_work(job)
    if job.attempts >= job.max_attempts or pilot.attempts >= pilot.max_attempts or job.processing_seconds + 30 > 900:
        raise Deferred('Company/pilot model or processing-time budget reached.')
    job.attempts += 1
    job.processing_seconds += 30
    pilot.attempts += 1
    job.save(update_fields=['attempts', 'processing_seconds'])
    pilot.save(update_fields=['attempts'])


def before_fetch(page_job):
    job_id = owner_of(page_job)
    if not job_id:
        return None
    def guard(url):
        try:
            reserve_fetch(job_id, url)
        except Deferred as exc:
            CompanyJob.objects.filter(pk=job_id).update(state='paused', reason=exc.reason)
            from leads.services.network import FetchError
            raise FetchError('Company work paused: ' + exc.reason) from exc
    return guard


def can_queue(job, url, depth):
    try:
        check_work(job)
    except Deferred:
        return False
    if not in_scope(job.source, url) or depth > min(2, job.source.max_depth, job.pilot.campaign.max_depth):
        return False
    count = DiscoveryJob.objects.filter(company_job=job).count()
    return count < min(job.max_pages, job.source.max_pages)


@transaction.atomic
def advance(job_id):
    from leads.models import Source
    job = CompanyJob.objects.select_for_update().select_related('pilot__campaign', 'source', 'company').get(pk=job_id)
    if job.state in ('pending_domain', 'pending_approval'):
        domains = list(job.company.domains.filter(state='verified'))
        if not domains:
            return False
        job.state = 'pending_approval'
        job.source = next((s for s in job.pilot.campaign.sources.filter(approved=True) for d in domains if origin(s.url) == d.origin), None)
        if not job.source:
            job.save(update_fields=['state'])
            propose_domains(job, domains)
            return False
        job.scope_hash = scope_hash(job.source)
        job.key = digest([job.company_id, origin(job.source.url), job.scope_hash, 'sales_team'])
        duplicate = CompanyJob.objects.filter(key=job.key, state__in=OPEN_COMPANY_STATES).exclude(pk=job.pk).first()
        if duplicate:
            Lineage.objects.filter(company_job=job).update(company_job=duplicate)
            job.state, job.reason = 'coalesced', f'Uses existing company job {duplicate.pk}.'
            job.save()
            return True
        job.state = 'queued'
        job.save()
    try:
        check_work(job)
    except Deferred as exc:
        pause(job, exc.reason)
        return False
    if not collection_allowed(job.source):
        from automation.models import SiteAutomationJob
        SiteAutomationJob.objects.filter(source=job.source, company_job__isnull=True).update(company_job=job)
        job.state, job.reason = 'needs_recipe_review', 'Source setup/canary must pass; company relevance cannot release it.'
        job.save(update_fields=['state', 'reason'])
        return False
    campaign = Campaign.objects.select_for_update().get(pk=job.pilot.campaign_id)
    run = campaign.runs.filter(status__in=('queued', 'running', 'paused')).first()
    if run and run.status == 'paused':
        pause(job, 'Existing discovery run is paused.')
        return False
    if job.run_id:
        unfinished = DiscoveryJob.objects.filter(company_job=job, status__in=('queued', 'processing')).exists()
        evaluations = Evaluation.objects.filter(Q(company_job=job) | Q(uses__company_job=job)).distinct()
        pending_eval = evaluations.exclude(state__in=('succeeded', 'failed', 'failed_contract', 'too_large', 'evidence_expired')).exists()
        if not unfinished and not pending_eval:
            supported = evaluations.filter(state='succeeded', judgments__question_id='person_sales_role',
                judgments__label__in=('direct_sales', 'sales_leadership')).exclude(judgments__review_state='rejected').exists()
            failed = DiscoveryJob.objects.filter(company_job=job, status='failed').exists()
            job.state = 'completed' if supported else ('failed' if failed else 'insufficient_evidence')
            job.reason = 'Bounded discovery complete; see evidence and review.' if supported else 'No supported sales role found in the evaluated evidence; this is not proof of absence.'
            if failed:
                job.reason += ' Some page requests failed; inspect discovery job details.'
            job.save(update_fields=['state', 'reason'])
            return True
        return False
    if not run:
        run = DiscoveryRun.objects.create(campaign=campaign, status='running', started_at=timezone.now())
    # Exact stored links only; no guessed /team paths or generated domains.
    links = [{'url': job.source.url, 'label': job.source.name}]
    for edge in job.lineage.select_related('document'):
        links.extend(edge.document.links)
    links.sort(key=lambda x: rank(campaign, x['url'], x.get('label', ''))[0], reverse=True)
    queued = 0
    for item in links:
        url = item['url']
        score, reasons = rank(campaign, url, item.get('label', ''))
        if score < 0 or not can_queue(job, url, 0) or run.jobs.count() >= campaign.max_pages:
            continue
        candidate, _ = DiscoveredURL.objects.get_or_create(campaign=campaign, url=url, defaults={
            'origin': origin(url), 'source': job.source, 'decision': 'approved', 'score': score,
            'label': item.get('label', '')[:200], 'reasons': reasons, 'first_run': run, 'method': 'company'})
        if candidate.decision == 'dismissed' or candidate.manual_review_required:
            continue
        if candidate.source_id != job.source_id:
            candidate.source, candidate.decision = job.source, 'approved'
            candidate.save(update_fields=['source', 'decision'])
        page, created = DiscoveryJob.objects.get_or_create(run=run, kind='page', url=url, defaults={
            'source': job.source, 'candidate': candidate, 'priority': score + 10, 'company_job': job})
        if not created and page.company_job_id != job.pk:
            # Do not take over another operation's counters or replay its requests.
            continue
        queued += int(created)
    job.run, job.state = run, 'discovering'
    job.reason = f'{queued} bounded page jobs queued.'
    if not queued and not DiscoveryJob.objects.filter(company_job=job, status__in=('queued', 'processing')).exists():
        job.state, job.reason = 'paused', 'No queue capacity or eligible exact URLs; existing jobs were not taken over.'
    job.save(update_fields=['run', 'state', 'reason'])
    return bool(queued)


def tick(token):
    check_lease(token)
    for pilot in Pilot.objects.filter(active=True, expires_at__gt=timezone.now(), campaign__active=True)[:10]:
        evaluations = Evaluation.objects.filter(provider=settings.JEV_MODE, state='succeeded', person__isnull=True,
            company_job__isnull=True, document__source__discovery_campaigns=pilot.campaign).exclude(
            lineage__company_job__pilot=pilot).order_by('created_at')[:50]
        for evaluation in evaluations:
            if eligible(evaluation):
                try:
                    queue_company(pilot.pk, evaluation.pk)
                except ValueError:
                    break
        for job in pilot.jobs.filter(state__in=OPEN_COMPANY_STATES).exclude(state='paused').order_by('created_at')[:10]:
            if advance(job.pk):
                return True
    return False
