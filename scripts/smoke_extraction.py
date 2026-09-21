#!/usr/bin/env python3
"""Real worker and SQLite restart with five fictional platform layouts; no API key."""
import io
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix='extraction-smoke-') as directory:
        env = dict(os.environ, DATA_DIR=directory, DATABASE_URL='', DJANGO_SETTINGS_MODULE='config.settings',
            DJANGO_SECRET_KEY=secrets.token_urlsafe(48), EXTRACTION_PACKS_ENABLED='1',
            JEV_MODE='mock', JEV_CAPTURE_ENABLED='1', JEV_ROUTING_ENABLED='0', TYPESAFE_API_KEY='',
            BRAVE_SEARCH_ENABLED='0', OLLAMA_MODEL='')
        os.environ.update(env)
        import django
        django.setup()
        from django.core.management import call_command
        from django.db import connections
        from automation.models import SiteAutomationJob
        from classification.models import Attempt, EvidenceSpan, Judgment, Person
        from classification.evidence import valid_span
        from leads.models import Lead, Observation
        call_command('migrate', interactive=False, verbosity=0)
        call_command('init_extraction_demo', stdout=io.StringIO())
        output = tempfile.TemporaryFile(mode='w+')
        worker = None

        def start():
            return subprocess.Popen([sys.executable, 'manage.py', 'worker'], env=env,
                                    stdout=output, stderr=subprocess.STDOUT)

        def stop():
            if worker and worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    worker.kill(); worker.wait()

        def wait_for(predicate):
            deadline = time.monotonic() + 50
            while time.monotonic() < deadline:
                if predicate():
                    return
                if worker.poll() is not None:
                    raise AssertionError('Worker exited unexpectedly.')
                failed = SiteAutomationJob.objects.filter(state__in=('failed', 'paused')).first()
                if failed:
                    raise AssertionError(failed.message)
                time.sleep(.1)
            raise AssertionError('Extraction worker did not finish in time.')

        try:
            worker = start()
            wait_for(lambda: SiteAutomationJob.objects.filter(state='active').count() == 5)
            wait_for(lambda: Judgment.objects.filter(evaluation__person__isnull=False).exists())
            stop()
            assert Observation.objects.count() == 10, Observation.objects.count()
            assert set(Lead.objects.values_list('name', flat=True)) == {'Robin Demo', 'Morgan Sample'}
            assert Person.objects.filter(name='Robin Demo').exists()
            assert all(valid_span(span) for span in EvidenceSpan.objects.select_related('document'))
            assert not Attempt.objects.exists()
            structured = SiteAutomationJob.objects.filter(source__url__contains='/jsonld/').get()
            assert structured.current_recipe.recipe == {'engine': 'structured-v1'}
            lead = Lead.objects.first()
            lead.status, lead.notes = 'suppressed', 'Keep this synthetic review across restart.'
            lead.save()
            before = (Lead.objects.count(), Observation.objects.count())
            call_command('init_extraction_demo', stdout=io.StringIO())
            worker = start()
            wait_for(lambda: SiteAutomationJob.objects.filter(state='active').count() == 5)
            stop()
            lead.refresh_from_db()
            assert before == (Lead.objects.count(), Observation.objects.count())
            assert lead.status == 'suppressed' and lead.notes.startswith('Keep this')
            assert not Attempt.objects.exists()
            print('PASS: five platform layouts completed probe, validation and canary in a real worker.')
            print('PASS: 10 source observations, structured Jev evidence and mock judgments; no paid attempts.')
            print('PASS: restart and re-probe preserve contact deduplication and human suppression.')
        except Exception:
            output.seek(0); print(output.read(), file=sys.stderr)
            raise
        finally:
            stop(); connections.close_all(); output.close()


if __name__ == '__main__':
    main()
