"""Operator controls and a fixed offline end-to-end demo. No independent daemon."""
import hashlib
import json
from datetime import timedelta, timezone as dt_timezone
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from leads.models import Source
from leads.services.worker import acquire_lease, heartbeat, release_lease
from classification import accounting, services
from classification.models import (Attempt, CompanyDomain, CompanyJob, ControlEvent, Evaluation,
                                    JevControl, Judgment, Person, Pilot)


def report():
    control = JevControl.objects.get(pk='jev')
    today = timezone.now().astimezone(dt_timezone.utc).date()
    return {'mode': settings.JEV_MODE, 'model': settings.JEV_MODEL, 'paused': control.paused,
        'spent_nusd': control.spent_nusd, 'reserved_nusd': control.reserved_nusd,
        'legacy_audit': {'cumulative_allowance_nusd': control.allowance_nusd,
                         'cumulative_attempt_limit': control.cumulative_attempt_limit,
                         'remaining_nusd': control.remaining_nusd, 'admission_effect': False},
        'active_attempt': str(control.active_attempt) if control.active_attempt else None,
        'next_allowed_at': control.next_allowed_at.isoformat(), 'paid_attempts': Attempt.objects.count(),
        'daily_attempt_limit': None,
        'daily_allowance_nusd': settings.JEV_DAILY_ALLOWANCE_NUSD,
        'daily_exposure_nusd': accounting.daily_exposure_nusd(today),
        'cumulative_allowance_enforced': False,
        'candidate_people': Person.objects.count(), 'evaluations': Evaluation.objects.count(),
        'succeeded': Evaluation.objects.filter(state='succeeded').count(), 'judgments': Judgment.objects.count(),
        'company_jobs': list(CompanyJob.objects.values('id', 'state', 'fetches', 'attempts', 'reason'))}


class Command(BaseCommand):
    help = 'Jev controls. Start with jev doctor, jev demo, and docs/JEV_TESTING.md.'

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest='action', required=True)
        sub.add_parser('doctor')
        sub.add_parser('status')
        sub.add_parser('report')
        demo = sub.add_parser('demo', help='Fixed fictional end-to-end demo; never calls Jev or external sites.')
        demo.add_argument('--seed-only', action='store_true')
        synthetic = sub.add_parser('seed-synthetic', help='Prepare fixed fictional calibration evidence; no fetch, pilot or API call.')
        synthetic.add_argument('--provider', choices=['mock', 'live'], default='mock')
        run = sub.add_parser('run', help='Consume selected classification work under the collector lease, then exit.')
        run.add_argument('--limit', type=int, default=1)
        run.add_argument('--evaluation')
        run.add_argument('--live', action='store_true', help='Required in addition to configured live gates.')
        run.add_argument('--mock-only', action='store_true')
        imported = sub.add_parser('import-html', help='Capture already-authorized, locally saved HTML; no network.')
        imported.add_argument('--source', type=int, required=True)
        imported.add_argument('--url', required=True)
        imported.add_argument('--file', required=True)
        imported.add_argument('--retrieved-at', required=True, help='Actual retrieval timestamp including UTC offset.')
        imported.add_argument('--provider', choices=['mock', 'live'], default='mock')
        verify = sub.add_parser('verify-domain')
        verify.add_argument('domain_id', type=int)
        verify.add_argument('--reason', required=True)
        pilot = sub.add_parser('pilot')
        pilot.add_argument('--campaign', type=int, required=True)
        pilot.add_argument('--name', required=True)
        pilot.add_argument('--hours', type=int, default=24)
        pilot.add_argument('--activate', action='store_true')
        control_pilot = sub.add_parser('pilot-state')
        control_pilot.add_argument('pilot_id', type=int)
        control_pilot.add_argument('state', choices=['pause', 'resume'])
        control_pilot.add_argument('--reason', required=True)
        job = sub.add_parser('job-resume', help='Resume a paused company job without resetting limits, deadlines or fetch retries.')
        job.add_argument('job_id', type=int)
        job.add_argument('--reason', required=True)
        for name in ('pause', 'resume'):
            command = sub.add_parser(name)
            command.add_argument('--reason', required=True)

        recover = sub.add_parser('recover')
        recover.add_argument('attempt_id')
        recover.add_argument('--worker-stopped', action='store_true')
        recover.add_argument('--billed-nusd', type=int)
        recover.add_argument('--reason', required=True)
        retry_persistence = sub.add_parser('retry-persistence')
        retry_persistence.add_argument('evaluation_id')
        retry_persistence.add_argument('--reason', required=True)
        sub.add_parser('purge', help='Expire retained candidate text; money/audit records remain.')

    def handle(self, *args, **options):
        try:
            self.execute_action(options)
        except (ValueError, accounting.Deferred, Source.DoesNotExist, CompanyDomain.DoesNotExist,
                Evaluation.DoesNotExist, Attempt.DoesNotExist, Pilot.DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc

    def execute_action(self, o):
        action = o['action']
        if action == 'doctor':
            wallet = JevControl.objects.get(pk='jev')
            consistent = True
            try:
                accounting.verify_ledger(wallet)
            except accounting.Deferred:
                consistent = False
            checks = accounting.diagnostics(wallet)
            self.stdout.write(json.dumps({'live_ready': not checks,
                'checks': checks, 'token_counter': settings.JEV_TOKEN_COUNTER or 'unverified estimate',
                'daily_attempt_limit': None,
                'reservation_nusd': accounting.RESERVATION_NUSD,
                'scope': 'Configuration and wallet only; each evaluation rechecks evidence, authorization and lease.',
                'key_present': bool(settings.TYPESAFE_API_KEY), 'ledger_consistent': consistent,
                'network_requests': 0, **report()}, indent=2))
        elif action in ('status', 'report'):
            self.stdout.write(json.dumps(report(), indent=2))
        elif action == 'demo':
            self.demo(o['seed_only'])
        elif action == 'seed-synthetic':
            from classification.fixtures import seed_calibration
            evaluations = seed_calibration(o['provider'])
            self.stdout.write(json.dumps({'provenance': 'synthetic-fixture', 'network_requests': 0,
                'evaluations': [str(e.pk) for e in evaluations]}))
        elif action == 'import-html':
            from classification.evidence import capture_page
            stamp = parse_datetime(o['retrieved_at'])
            if not stamp or timezone.is_naive(stamp) or stamp > timezone.now():
                raise ValueError('Supply an actual, nonfuture retrieval timestamp with timezone.')
            path = Path(o['file'])
            if path.stat().st_size > 6 * 1024 * 1024:
                raise ValueError('HTML file exceeds 6 MiB.')
            body = path.read_bytes()
            evaluations = capture_page(Source.objects.get(pk=o['source']), o['url'], hashlib.sha256(body).hexdigest(),
                body.decode('utf-8', errors='replace'), provider=o['provider'], retrieved_at=stamp)
            self.stdout.write(json.dumps({'evaluations': [str(e.pk) for e in evaluations], 'network_requests': 0}))
        elif action == 'run':
            if not 1 <= o['limit'] <= 100:
                raise ValueError('Limit must be 1–100.')
            if o['live'] and o['mock_only']:
                raise ValueError('Choose live or mock-only.')
            if not o['mock_only'] and not o['live']:
                raise ValueError('Choose --mock-only or --live explicitly.')
            if o['live'] and accounting.readiness():
                raise ValueError('; '.join(accounting.readiness()))
            token = self.lease()
            try:
                for _ in range(o['limit']):
                    heartbeat(token)
                    query = Evaluation.objects.filter(provider='live' if o['live'] else 'mock',
                        state__in=['queued', 'retry_wait', 'waiting'], available_at__lte=timezone.now())
                    if o['evaluation']:
                        query = query.filter(pk=o['evaluation'])
                    evaluation = query.order_by('created_at').first()
                    if not evaluation:
                        break
                    services.process(evaluation, token, mock_only=o['mock_only'])
                self.stdout.write(json.dumps(report(), indent=2))
            finally:
                release_lease(token)
        elif action == 'verify-domain':
            from classification.routing import verify_domain
            verify_domain(o['domain_id'], 'local CLI', o['reason'])
            self.stdout.write('Relationship verified. Fetching still requires source approval and scope.')
        elif action == 'pilot':
            from discovery.models import Campaign
            campaign = Campaign.objects.get(pk=o['campaign'])
            if not 1 <= o['hours'] <= 168:
                raise ValueError('Pilot lifetime must be 1–168 hours.')
            pilot = Pilot.objects.create(name=o['name'], campaign=campaign, active=o['activate'],
                expires_at=timezone.now() + timedelta(hours=o['hours']))
            ControlEvent.objects.create(actor='local CLI', action='create_pilot', reason=o['name'], data={'pilot': pilot.pk})
            self.stdout.write(f'Pilot {pilot.pk}: active={pilot.active}. Campaign must also be active.')
        elif action == 'pilot-state':
            pilot = Pilot.objects.get(pk=o['pilot_id'])
            pilot.active = o['state'] == 'resume'
            pilot.save(update_fields=['active'])
            ControlEvent.objects.create(actor='local CLI', action='pilot_' + o['state'], reason=o['reason'], data={'pilot': pilot.pk})
        elif action == 'job-resume':
            from django.db import transaction
            from classification.routing import check_work
            with transaction.atomic():
                job = CompanyJob.objects.select_for_update().get(pk=o['job_id'])
                if job.state != 'paused':
                    raise ValueError('Only paused company jobs can be resumed.')
                job.state = 'discovering' if job.run_id else 'queued'
                check_work(job)
                job.reason = o['reason'][:1000]
                job.save(update_fields=['state', 'reason'])
                ControlEvent.objects.create(actor='local CLI', action='resume_company_job', reason=o['reason'], data={'job': job.pk})
        elif action in ('pause', 'resume'):
            accounting.set_paused(action == 'pause', 'local CLI', o['reason'])
        elif action == 'recover':
            accounting.recover_attempt(o['attempt_id'], 'local CLI', o['reason'], o['worker_stopped'], o['billed_nusd'])
        elif action == 'retry-persistence':
            accounting.retry_settled_persistence(o['evaluation_id'], 'local CLI', o['reason'])
        elif action == 'purge':
            from classification.evidence import purge_expired
            purge_expired()

    def lease(self):
        token = acquire_lease()
        if not token:
            raise CommandError('A worker already owns this database. Stop it before using this bounded command.')
        return token

    def demo(self, seed_only):
        from classification.fixtures import seed_demo, DEMO_ORIGIN
        from classification.routing import advance, eligible, queue_company
        from discovery.models import DiscoveryJob
        from discovery.services import process, finish
        with override_settings(JEV_MODE='mock', JEV_CAPTURE_ENABLED=True, JEV_ROUTING_ENABLED=True):
            pilot, _ = seed_demo()
            if seed_only:
                self.stdout.write(f'Offline pilot {pilot.pk} seeded; no requests made.')
                return
            token = self.lease()
            try:
                # Deliberately select ONLY this fixture's work, even in a populated DB.
                for _ in range(100):
                    heartbeat(token)
                    evaluation = Evaluation.objects.filter(provider='mock', document__source__url=DEMO_ORIGIN + '/',
                        state='queued').order_by('created_at').first()
                    if evaluation:
                        services.process(evaluation, token, mock_only=True)
                        continue
                    for trigger in Evaluation.objects.filter(provider='mock', person__isnull=True, company_job__isnull=True,
                            document__source__url=DEMO_ORIGIN + '/', state='succeeded'):
                        if eligible(trigger):
                            queue_company(pilot.pk, trigger.pk)
                    for job in pilot.jobs.exclude(state__in=['completed', 'insufficient_evidence', 'paused', 'coalesced']):
                        advance(job.pk)
                    page = DiscoveryJob.objects.filter(company_job__pilot=pilot, status='queued',
                        source__collector='demo', source__url=DEMO_ORIGIN + '/').select_related('run__campaign', 'source', 'candidate').first()
                    if not page:
                        break
                    process(page)
                    finish(page.run)
                self.stdout.write(json.dumps({'demo': 'synthetic; no provider accuracy claim', **report()}, indent=2))
            finally:
                release_lease(token)
