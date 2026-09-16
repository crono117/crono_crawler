import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from django.utils import timezone
from automation.models import ProbePage, SiteAutomationJob
from discovery.models import DailyUsage
from leads.models import DomainState, Lead
from leads.services.worker import tick
from .test_pipeline import AutomationCase, HTML


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.headers["Host"], self.path))
        status, content_type, body = self.server.pages.get(self.path, (404, "text/plain", b"missing"))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if self.path == "/team/redirect/":
            self.send_header("Location", "http://127.0.0.1/private/")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class AutomationHTTPTests(AutomationCase):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = []
        self.server.pages = {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /"),
            "/team/": (200, "text/html", HTML.encode()),
            "/team/redirect/": (302, "text/plain", b""),
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        connect = socket.create_connection
        original_dns = socket.getaddrinfo
        def fixture_dns(host, port, *args, **kwargs):
            if (host, port) == self.server.server_address:
                return original_dns(host, port, *args, **kwargs)
            self.assertEqual(host, "vendor.fixture.test")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]
        self.dns = patch("leads.services.network.socket.getaddrinfo", side_effect=fixture_dns)
        def fixture_connect(address, *args, **kwargs):
            self.assertEqual(address, ("8.8.8.8", 80))
            return connect(self.server.server_address, *args, **kwargs)
        self.socket = patch("leads.services.network.socket.create_connection", side_effect=fixture_connect)
        self.dns.start()
        self.socket.start()

    def tearDown(self):
        self.socket.stop()
        self.dns.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def finish_actual(self, job):
        for _ in range(60):
            DomainState.objects.update(next_allowed_at=timezone.now())
            ProbePage.objects.update(available_at=timezone.now())
            SiteAutomationJob.objects.update(available_at=timezone.now())
            tick(self.token, prefer_discovery=True)
            job.refresh_from_db()
            if job.state in ("active", "paused", "failed"):
                return
        self.fail(job.message)

    def test_real_http_worker_runs_policy_probe_recipe_and_canary(self):
        job = self.candidate("http://vendor.fixture.test/team/").source.automation_job
        self.finish_actual(job)
        self.assertEqual(job.state, "active", job.message)
        self.assertEqual(self.server.requests, [("vendor.fixture.test", "/robots.txt"), ("vendor.fixture.test", "/team/"), ("vendor.fixture.test", "/team/")])
        self.assertEqual(Lead.objects.count(), 2)
        self.assertEqual(DailyUsage.objects.get().requests, 3)

    def test_robots_disallow_stops_before_html_request(self):
        self.server.pages["/robots.txt"] = (200, "text/plain", b"User-agent: *\nDisallow: /")
        job = self.candidate("http://vendor.fixture.test/team/").source.automation_job
        self.finish_actual(job)
        self.assertEqual(job.state, "paused")
        self.assertEqual(self.server.requests, [("vendor.fixture.test", "/robots.txt")])
        self.assertFalse(Lead.objects.exists())

    def test_private_redirect_is_rejected_without_a_request_to_destination(self):
        job = self.candidate("http://vendor.fixture.test/team/redirect/").source.automation_job
        self.finish_actual(job)
        self.assertEqual(job.state, "failed")
        self.assertEqual([path for _, path in self.server.requests], ["/robots.txt", "/team/redirect/"])
        self.assertFalse(Lead.objects.exists())
