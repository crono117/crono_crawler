"""Real HTTP sockets and the collection/database pipeline, without third-party I/O.

Only the fixture hostname's DNS answer and socket destination are substituted.
HTTP parsing, redirects, robots, extraction, scheduling and storage run normally.
The production public-address checks and source controls are not changed.
"""
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from leads.models import DomainState, Lead, Observation, PageJob, SourceCandidate
from leads.services.network import FetchError, fetch, in_scope
from leads.services.worker import acquire_lease, enqueue, release_lease, tick
from .test_collection import demo_source


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.requests.append({"path": self.path, "host": self.headers.get("Host"),
                                     "agent": self.headers.get("User-Agent")})
        if self.path == "/robots.txt":
            status, content_type, body = 200, "text/plain", b"User-agent: *\nAllow: /\n"
        elif self.path == "/team/":
            status, content_type, body = 200, "text/html; charset=utf-8", self.server.team_html
        elif self.path == "/team/page-2":
            status, content_type, body = 200, "text/html; charset=utf-8", self.server.second_html
        elif self.path == "/blocked/":
            status, content_type, body = 403, "text/plain", b"Forbidden"
        elif self.path == "/redirect/":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1/private")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            status, content_type, body = 404, "text/plain", b"Not found"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class HttpSocketIntegrationTests(TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.server.requests = []
        html = (Path(settings.BASE_DIR) / "examples/demo-team.html").read_text()
        self.server.team_html = (html + '<a href="/team/page-2">More</a>'
                                 '<a href="https://vendor.example.org/team/">Example vendor</a>').encode()
        self.server.second_html = (
            '<article class="team-member"><h3>Alex Example</h3>'
            '<p class="role">Merchant services sales consultant</p>'
            '<a href="mailto:alex@example.com">Contact Alex</a></article>'
        ).encode()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        original_dns = socket.getaddrinfo
        original_connect = socket.create_connection

        def fixture_dns(host, port, *args, **kwargs):
            if host == "fixture.example.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]
            return original_dns(host, port, *args, **kwargs)

        def fixture_connect(address, *args, **kwargs):
            # The only permitted test connection is to the fixture's pinned IP.
            self.assertEqual(address, ("8.8.8.8", 80))
            return original_connect(self.server.server_address, *args, **kwargs)

        self.dns_patch = patch("leads.services.network.socket.getaddrinfo", side_effect=fixture_dns)
        self.connect_patch = patch("leads.services.network.socket.create_connection", side_effect=fixture_connect)
        self.dns_patch.start()
        self.connect_patch.start()
        self.source = demo_source(url="http://fixture.example.test/team/", collector="http",
                                  follow_links=True, discover_external=True, max_pages=2, max_depth=1)
        self.token = acquire_lease()

    def tearDown(self):
        release_lease(self.token)
        self.connect_patch.stop()
        self.dns_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def make_deferred_jobs_due(self):
        # Advance only this test database's deadlines instead of waiting in real time.
        DomainState.objects.update(next_allowed_at=timezone.now())
        PageJob.objects.filter(status="queued").update(available_at=timezone.now())

    def finish(self, run):
        for _ in range(8):
            self.make_deferred_jobs_due()
            tick(self.token)
            run.refresh_from_db()
            if run.status in ("completed", "failed", "paused"):
                return
        self.fail("The bounded fixture crawl did not finish.")

    def test_real_http_collection_recrawl_and_suppression(self):
        run = enqueue(self.source)
        tick(self.token)
        self.assertEqual([r["path"] for r in self.server.requests], ["/robots.txt"])
        # A second immediate tick cannot fetch the page before its persisted delay.
        tick(self.token)
        self.assertEqual(len(self.server.requests), 1)
        self.finish(run)
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.pages_done, 2)
        self.assertEqual(Lead.objects.count(), 3)
        self.assertEqual(Observation.objects.count(), 4)
        self.assertEqual(SourceCandidate.objects.get().url, "https://vendor.example.org/")
        self.assertEqual([r["path"] for r in self.server.requests], ["/robots.txt", "/team/", "/team/page-2"])
        self.assertTrue(all(r["host"] == "fixture.example.test" for r in self.server.requests))
        self.assertTrue(all(r["agent"] == settings.BOT_USER_AGENT for r in self.server.requests))
        Lead.objects.filter(name="Alex Example").update(status="suppressed", notes="Fixture review")
        second = enqueue(self.source)
        with patch("leads.services.worker.extract", side_effect=AssertionError("Unchanged pages should skip extraction")):
            self.finish(second)
        self.assertEqual(second.status, "completed")
        self.assertEqual(len(self.server.requests), 5)
        self.assertEqual(Lead.objects.count(), 3)
        self.assertEqual(Lead.objects.get(name="Alex Example").status, "suppressed")

    def test_real_http_forbidden_response_pauses_the_source(self):
        self.source.url = "http://fixture.example.test/blocked/"
        self.source.save()
        run = enqueue(self.source)
        self.finish(run)
        self.source.refresh_from_db()
        self.assertEqual(run.status, "paused")
        self.assertFalse(self.source.active)
        self.make_deferred_jobs_due()
        self.assertFalse(tick(self.token))
        self.assertEqual([r["path"] for r in self.server.requests], ["/robots.txt", "/blocked/"])

    def test_real_http_redirect_is_stopped_before_private_destination(self):
        with self.assertRaises(FetchError):
            fetch("http://fixture.example.test/redirect/", settings.BOT_USER_AGENT,
                  guard=lambda url: in_scope(self.source, url))
        self.assertEqual([r["path"] for r in self.server.requests], ["/redirect/"])
