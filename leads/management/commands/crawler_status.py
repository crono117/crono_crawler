"""Read-only diagnostics. No provider/target fetches; optional explicit loopback HTTP probe."""
from http.client import HTTPConnection, HTTPException
import json
from urllib.parse import urlsplit

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from discovery.diagnostics import summarize
from discovery.models import DiscoveryRun
from leads.models import WorkerLease
from leads.services import supervision
from leads.services.worker import LEASE_SECONDS


class Command(BaseCommand):
    help = 'Read-only lease, queue, run and optional user-systemd/loopback HTTP diagnostics.'

    def add_arguments(self, parser):
        parser.add_argument('--campaign', type=int)
        parser.add_argument('--unit', action='append', default=[])
        parser.add_argument('--expected-revision', default='')
        parser.add_argument('--http', default='', help='Optional http://127.0.0.1:PORT/path readiness probe.')

    def handle(self, *args, **options):
        lease = WorkerLease.objects.filter(key='collector').first()
        age = (timezone.now() - lease.heartbeat_at).total_seconds() if lease else None
        lease_state = 'missing' if not lease else 'empty' if not lease.token else 'stale' if age > LEASE_SECONDS else 'fresh'
        runs = DiscoveryRun.objects.all()
        if options['campaign']:
            runs = runs.filter(campaign_id=options['campaign'])
        units = []
        for unit in options['unit']:
            try:
                units.append(supervision.status(unit, options['expected_revision']))
            except (ValueError, OSError, supervision.subprocess.SubprocessError):
                units.append({'unit': unit, 'error': 'Unable to inspect unit; check user-systemd availability.'})
        http = {'state': 'not_checked'}
        if options['http']:
            try:
                url = urlsplit(options['http'])
                port = url.port  # validates numeric/range before opening any socket
                valid = (url.scheme == 'http' and url.hostname == '127.0.0.1' and not url.username
                         and not url.password and not url.fragment and (port is None or 1 <= port <= 65535))
            except ValueError:
                valid = False
            if not valid:
                raise CommandError('HTTP probe requires a valid numeric-loopback HTTP URL and port 1-65535, without credentials.')
            conn = HTTPConnection('127.0.0.1', port if port is not None else 80, timeout=3)
            try:
                conn.request('GET', url.path or '/')
                response = conn.getresponse()
                http = {'state': 'healthy' if response.status in (200, 302) else 'unhealthy_http', 'status': response.status}
            except (OSError, HTTPException):
                http = {'state': 'unhealthy_http'}
            finally:
                conn.close()
        rows = []
        for run in runs.order_by('-id')[:20]:
            rows.append({'id': run.pk, 'status': run.status,
                'terminal_reason': run.terminal_reason or ('in_progress' if run.status in ('queued', 'running') else 'legacy_unknown'),
                'message': run.message or 'No historical run message; see aggregate counts.',
                'queue': {state: run.jobs.filter(status=state).count() for state in ('queued', 'processing', 'done', 'failed', 'skipped')},
                'diagnostics': run.diagnostics or summarize(run)})
        self.stdout.write(json.dumps({'lease': {'state': lease_state, 'heartbeat_age_seconds': age},
            'units': units, 'http': http, 'runs': rows}, default=str, indent=2))
