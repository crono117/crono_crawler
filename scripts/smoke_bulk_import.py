"""Real HTTP smoke for JSON preview/confirm; synthetic isolated data only.

Run using the documented isolated runner, never with production DATA_DIR/.env.
No worker, crawl, external network, approval or paid provider is started.
"""
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
assert not (ROOT / '.env').exists(), 'Run only in an isolated checkout without .env'
assert os.environ.get('JEV_MODE') == 'off'
assert os.environ.get('BRAVE_SEARCH_ENABLED') == '0'
assert os.environ.get('DATA_DIR') and 'bulk-import-' in os.environ['DATA_DIR']
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from leads.models import Source, Run, PageJob, SourceCandidate
from discovery.models import DiscoveryJob, DiscoveredURL
from classification.models import Attempt

call_command('migrate', interactive=False, verbosity=0)
assert Source.objects.count() == 0
user = get_user_model().objects.create_superuser(username='bulk-http-fixture')
client = Client()
client.force_login(user)
jar = http.cookiejar.CookieJar()
jar.set_cookie(http.cookiejar.Cookie(0, settings.SESSION_COOKIE_NAME, client.cookies[settings.SESSION_COOKIE_NAME].value,
                                    None, False, '127.0.0.1', False, False, '/', True, False, None, True, None, None, {}))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))
with socket.socket() as reservation:
    reservation.bind(('127.0.0.1', 0))
    port = reservation.getsockname()[1]
base = f'http://127.0.0.1:{port}'
log_path = Path(os.environ['TMPDIR']) / 'bulk-http-server.log'

def request(path, data=None, headers=None):
    req = urllib.request.Request(base + path, data=data, headers=headers or {})
    try:
        with opener.open(req, timeout=8) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers

def soup(body):
    return BeautifulSoup(body, 'html.parser')

def csrf(body):
    return soup(body).select_one('input[name=csrfmiddlewaretoken]')['value']

def preview(document, token):
    boundary = 'BulkSmoke' + secrets.token_hex(16)
    payload = json.dumps(document).encode()
    chunks = []
    for key, value in {'action': 'preview', 'csrfmiddlewaretoken': token}.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sources.json"\r\nContent-Type: application/json\r\n\r\n'.encode())
    chunks.extend([payload, f'\r\n--{boundary}--\r\n'.encode()])
    return request('/sources/import/', b''.join(chunks), {'Content-Type': f'multipart/form-data; boundary={boundary}'})

with log_path.open('w') as log:
    server = subprocess.Popen([sys.executable, 'manage.py', 'runserver', f'127.0.0.1:{port}', '--noreload'],
                              cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(80):
            if server.poll() is not None:
                raise RuntimeError('Local server exited: ' + log_path.read_text()[-3000:])
            try:
                status, body, _ = request('/sources/import/')
                break
            except urllib.error.URLError:
                time.sleep(.1)
        else:
            raise RuntimeError('Local server did not start')
        assert status == 200, (status, body[:200])
        csrf_token = csrf(body)
        document = {'schema_version': 1, 'sources': [
            {'name': 'HTTP Fixture Team', 'url': 'https://bulk-fixture.example.org/team/'},
            {'name': 'HTTP Fixture Contact', 'url': 'https://bulk-fixture.example.org/contact/', 'category': 'pos'},
        ]}
        status, body, _ = preview(document, csrf_token)
        assert status == 200, (status, body[:200])
        assert Source.objects.count() == 0, 'Preview persisted sources'
        preview_token = soup(body).select_one('input[name=preview_token]')['value']
        data = urllib.parse.urlencode({'action': 'import', 'csrfmiddlewaretoken': csrf(body), 'preview_token': preview_token}).encode()
        status, body, _ = request('/sources/import/', data, {'Content-Type': 'application/x-www-form-urlencoded'})
        assert status == 200, (status, body[:200])
        assert Source.objects.count() == 2
        assert not Source.objects.filter(active=True).exists()
        assert not Source.objects.filter(approved=True).exists()
        before = list(Source.objects.order_by('pk').values())
        # Browser retry of the same confirmation cannot overwrite or duplicate rows.
        status, _, _ = request('/sources/import/', data, {'Content-Type': 'application/x-www-form-urlencoded'})
        assert status == 200 and list(Source.objects.order_by('pk').values()) == before
        bad = {'schema_version': 1, 'sources': [
            {'name': 'Must not be created', 'url': 'https://bulk-fixture.example.org/new/'},
            {'name': 'Forbidden approval', 'url': 'https://bulk-fixture.example.org/private/', 'approved': True},
        ]}
        status, _, _ = preview(bad, csrf_token)
        assert status == 400 and Source.objects.count() == 2
        status, _, _ = request('/sources/import/', urllib.parse.urlencode({'action': 'import', 'preview_token': preview_token}).encode(),
                               {'Content-Type': 'application/x-www-form-urlencoded'})
        assert status == 403, 'Confirmation accepted without CSRF token'
        for path, kind in (('example/', 'application/json'), ('schema/', 'application/json'), ('guide/', 'text/markdown')):
            status, content, headers = request('/sources/import/' + path)
            assert status == 200 and kind in headers['Content-Type'] and 'attachment' in headers['Content-Disposition']
            if kind == 'application/json':
                json.loads(content)
        for model in (Run, PageJob, SourceCandidate, DiscoveryJob, DiscoveredURL, Attempt):
            assert model.objects.count() == 0, model.__name__
        print('PASS: real authenticated HTTP upload → preview → confirm created only paused/unapproved sources.')
        print('PASS: preview no writes; repeat confirmation unchanged; invalid batch all-or-nothing; CSRF enforced.')
        print('PASS: example/schema/guide downloads; no collection/discovery jobs, candidates or paid attempts.')
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill(); server.wait(timeout=5)
        print('PASS: isolated web process stopped; runner owns temporary database cleanup.')
