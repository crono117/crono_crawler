#!/usr/bin/env python3
"""Real web/worker restart and two-process SQLite admission checks, entirely offline.

Uses a temporary database and fictional fixture. Admission subprocesses exercise
only the ledger; they never dispatch a request or need a real key.
"""
import hashlib
import io
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def admission_probe(evaluation_id, token, barrier):
    import django
    django.setup()
    from classification.accounting import Deferred, reserve
    deadline = time.monotonic() + 15
    while not Path(barrier).exists():
        if time.monotonic() > deadline:
            raise RuntimeError('Admission barrier timed out.')
        time.sleep(.02)
    try:
        reserve(evaluation_id, token)
        print('ADMITTED')
    except Deferred:
        print('DEFERRED')


def import_probe(campaign_id, url, barrier):
    import django
    django.setup()
    from discovery.importing import import_metadata
    deadline = time.monotonic() + 15
    while not Path(barrier).exists():
        if time.monotonic() > deadline:
            raise RuntimeError('Import barrier timed out.')
        time.sleep(.02)
    result = import_metadata(campaign_id, [{'url': url}], apply=True)
    print(result['results'][0]['outcome'])


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    if len(sys.argv) > 1 and sys.argv[1] == '--admit':
        admission_probe(*sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == '--import':
        import_probe(*sys.argv[2:])
        return
    with tempfile.TemporaryDirectory(prefix='jev-smoke-') as directory:
        env = dict(os.environ, DATA_DIR=directory, DATABASE_URL='', DJANGO_DEBUG='1',
            DJANGO_SECRET_KEY=secrets.token_urlsafe(48), DJANGO_ALLOWED_HOSTS='127.0.0.1,localhost',
            DJANGO_SECURE_COOKIES='0', DJANGO_SETTINGS_MODULE='config.settings', JEV_MODE='mock',
            JEV_CAPTURE_ENABLED='1', JEV_ROUTING_ENABLED='1', TYPESAFE_API_KEY='', JEV_PRICE_CONFIRMED='0',
            JEV_TOKEN_COUNTER='', JEV_ALLOW_ESTIMATED_TOKENS='0', BRAVE_SEARCH_ENABLED='0', OLLAMA_MODEL='', EXTRACTION_PACKS_ENABLED='0')
        os.environ.update(env)
        import django
        django.setup()
        import httpx
        from bs4 import BeautifulSoup
        from django.contrib.auth import get_user_model
        from django.core.management import call_command
        from django.db import connections
        from classification import accounting
        from classification.evidence import capture_page
        from classification.fixtures import seed_demo, demo_response
        from classification.models import Attempt, CompanyJob, Evaluation, JevControl, Judgment, Person
        from discovery.models import DiscoveredURL, DiscoveryJob
        from leads.services.worker import acquire_lease, release_lease

        call_command('migrate', interactive=False, verbosity=0)
        pilot, _ = seed_demo()
        source = pilot.campaign.sources.get()
        password = secrets.token_urlsafe(24)
        user = get_user_model().objects.create_user('jev-smoke', password=password, is_staff=True)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        output = tempfile.TemporaryFile(mode='w+')
        processes = []

        def start():
            for command in ([sys.executable, 'manage.py', 'runserver', f'127.0.0.1:{port}', '--noreload'],
                            [sys.executable, 'manage.py', 'worker']):
                processes.append(subprocess.Popen(command, env=env, stdout=output, stderr=subprocess.STDOUT))

        def stop():
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            processes.clear()

        def wait_for(predicate, description):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if predicate():
                    return
                if any(p.poll() is not None for p in processes):
                    raise AssertionError('App process exited unexpectedly.')
                time.sleep(.1)
            raise AssertionError(description)

        def csrf(response):
            return BeautifulSoup(response.text, 'html.parser').select_one('input[name=csrfmiddlewaretoken]')['value']

        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', follow_redirects=True, trust_env=False, timeout=5) as client:
                def ready():
                    try:
                        return client.get('/login/').status_code == 200
                    except httpx.TransportError:
                        return False
                start()
                wait_for(ready, 'Web server did not start.')
                wait_for(lambda: CompanyJob.objects.filter(state='completed').exists(), 'Worker did not complete company demo.')
                assert Person.objects.filter(name='Jordan Example').exists()
                assert Judgment.objects.filter(question_id='person_sales_role', label='direct_sales').exists()
                assert not Attempt.objects.exists()
                assert DiscoveredURL.objects.filter(manual_review_required=True, decision='pending').exists()
                login = client.get('/login/')
                response = client.post('/login/', data={'username': user.username, 'password': password,
                    'csrfmiddlewaretoken': csrf(login)})
                assert response.status_code == 200
                dashboard = client.get('/classification/')
                assert 'Jev evidence review' in dashboard.text and 'completed' in dashboard.text
                evaluation = Evaluation.objects.filter(person__isnull=False).first()
                assert 'Exact saved evidence' in client.get(f'/classification/evaluations/{evaluation.pk}/').text
                for action in ('pause', 'resume'):
                    response = client.post('/classification/control/', data={'action': action, 'reason': 'Offline smoke control',
                        'csrfmiddlewaretoken': csrf(dashboard)})
                    assert response.status_code == 200
                    assert JevControl.objects.get().paused == (action == 'pause')
                counts = (Evaluation.objects.count(), DiscoveryJob.objects.count(), Judgment.objects.count())
                stop()
                start()
                wait_for(ready, 'Web server did not restart.')
                wait_for(lambda: client.get('/classification/').status_code == 200, 'Session did not survive restart.')
                assert counts == (Evaluation.objects.count(), DiscoveryJob.objects.count(), Judgment.objects.count())
                print('PASS: real web/worker demo, evidence UI, CSRF controls and restart persistence.')
                stop()

            # Two independent Python processes race on one file-backed SQLite DB.
            body = demo_response(source.url)
            evaluations = capture_page(source, source.url, hashlib.sha256(body.body).hexdigest(), body.text, provider='live')
            token = acquire_lease()
            assert token
            barrier = str(Path(directory) / 'start-admission')
            live_env = dict(env, JEV_MODE='live', TYPESAFE_API_KEY='synthetic-never-sent',
                            JEV_PRICE_CONFIRMED='1', JEV_ALLOW_ESTIMATED_TOKENS='1')
            children = [subprocess.Popen([sys.executable, __file__, '--admit', str(e.pk), token, barrier],
                env=live_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for e in evaluations[:2]]
            Path(barrier).touch()
            outcomes = []
            for child in children:
                stdout, stderr = child.communicate(timeout=30)
                assert child.returncode == 0, stderr
                outcomes.append(stdout.strip())
            assert sorted(outcomes) == ['ADMITTED', 'DEFERRED'], outcomes
            assert Attempt.objects.count() == 1
            wallet = JevControl.objects.get()
            accounting.verify_ledger(wallet)
            assert wallet.active_attempt and wallet.reserved_nusd == accounting.RESERVATION_NUSD
            release_lease(token)
            new_token = acquire_lease()
            assert new_token and new_token != token
            assert JevControl.objects.get().active_attempt == wallet.active_attempt
            # The admitted process ended without dispatch. Recovery deliberately
            # retains the unknown reservation until an operator verifies billing.
            accounting.recover_attempt(wallet.active_attempt, 'smoke', 'Admission subprocess exited; no HTTP was called.', True)
            assert JevControl.objects.get().reserved_nusd == accounting.RESERVATION_NUSD
            accounting.verify_ledger(JevControl.objects.get())
            release_lease(new_token)
            print('PASS: simultaneous process admission serialized; restart and crash recovery retain reservations.')
            from discovery.models import Campaign
            campaign = Campaign.objects.create(name='Offline concurrent metadata import', max_candidates=1)
            barrier = str(Path(directory) / 'start-import')
            children = []
            try:
                for name in ('first', 'second'):
                    children.append(subprocess.Popen([sys.executable, __file__, '--import', str(campaign.pk),
                        f'https://{name}.example.org/team/', barrier], env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                Path(barrier).touch()
                outcomes = []
                for child in children:
                    stdout, stderr = child.communicate(timeout=30)
                    assert child.returncode == 0, stderr
                    outcomes.append(stdout.strip())
                assert sorted(outcomes) == ['created', 'inventory_full'], outcomes
                assert campaign.urls.count() == 1
                assert campaign.urls.get().manual_review_required
                assert not campaign.sources.exists() and not campaign.runs.exists()
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=10)
            print('PASS: simultaneous metadata imports respect inventory cap without approval or jobs.')
            print('PASS: no paid API requests or external website requests were made.')
        except Exception:
            output.seek(0)
            print(output.read(), file=sys.stderr)
            raise
        finally:
            stop()
            connections.close_all()
            output.close()


if __name__ == '__main__':
    main()
