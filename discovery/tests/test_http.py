"""Exercise discovery using real HTTP sockets and deterministic synthetic sites."""
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from discovery.models import Campaign, DailyUsage, DiscoveryJob
from discovery.services import approve, start
from leads.models import DomainState, Lead, Source
from leads.services.worker import acquire_lease, release_lease, tick


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.headers["Host"], self.path))
        status, content_type, body = self.server.pages.get(self.path, (404, "text/plain", b"Not found"))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if self.path == "/redirect/":
            self.send_header("Location", "http://127.0.0.1/private")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class DiscoveryHTTPTests(TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = []
        self.server.pages = {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\nSitemap: http://fixture.example.test/map-index\n"),
            "/": (200, "text/html", b'<a href="/team/">Merchant services sales team</a><a href="http://vendor.fixture.test/reps/alex">POS sales representatives</a><a href="/blog/">News</a>'),
            "/map-index": (200, "application/xml", b'<sitemapindex><sitemap><loc>http://fixture.example.test/nested.xml</loc></sitemap></sitemapindex>'),
            "/nested.xml": (200, "application/xml", b'<urlset><url><loc>http://fixture.example.test/team/hidden</loc></url><url><loc>http://127.0.0.1/private</loc></url><url><loc>http://[bad</loc></url></urlset>'),
            "/team/": (200, "text/html", b'<article class="team-member"><h3>Alex Example</h3><p class="role">Merchant services sales consultant</p><a href="mailto:alex@example.com">Email</a></article>'),
            "/team/hidden": (200, "text/html", b'<article class="team-member"><h3>Casey Fixture</h3><p class="role">Payroll sales consultant</p><a href="mailto:casey@example.com">Email</a></article>'),
            "/reps/alex": (200, "text/html", b'<article class="team-member"><h3>Taylor Fixture</h3><p class="role">POS sales consultant</p><a href="mailto:taylor@example.com">Email</a></article>'),
            "/redirect/": (302, "text/plain", b""),
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        original_connect = socket.create_connection
        original_dns = socket.getaddrinfo

        def dns(host, port, *args, **kwargs):
            if (host, port) == self.server.server_address:
                return original_dns(host, port, *args, **kwargs)
            self.assertIn(host, ("fixture.example.test", "vendor.fixture.test"))
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

        def connect(address, *args, **kwargs):
            self.assertEqual(address, ("8.8.8.8", 80))
            return original_connect(self.server.server_address, *args, **kwargs)

        self.dns = patch("leads.services.network.socket.getaddrinfo", side_effect=dns)
        self.connection = patch("leads.services.network.socket.create_connection", side_effect=connect)
        self.dns.start()
        self.connection.start()
        self.source = Source.objects.create(name="Synthetic merchant services directory", url="http://fixture.example.test/", approved=True)
        self.campaign = Campaign.objects.create(name="HTTP campaign", max_pages=20)
        self.campaign.sources.add(self.source)
        self.token = acquire_lease()

    def tearDown(self):
        release_lease(self.token)
        self.connection.stop()
        self.dns.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def finish(self, run):
        for _ in range(40):
            DomainState.objects.update(next_allowed_at=timezone.now())
            DiscoveryJob.objects.filter(status="queued").update(available_at=timezone.now())
            tick(self.token, prefer_discovery=True)
            run.refresh_from_db()
            if run.status in ("completed", "failed", "paused"):
                return
        self.fail("HTTP discovery did not finish")

    def test_real_robots_nested_sitemaps_review_and_collection(self):
        run = start(self.campaign)
        tick(self.token, prefer_discovery=True)
        self.assertEqual(self.server.requests, [("fixture.example.test", "/robots.txt")])
        tick(self.token, prefer_discovery=True)
        self.assertEqual(len(self.server.requests), 1)
        self.finish(run)
        self.assertEqual(run.status, "completed")
        self.assertEqual(Lead.objects.count(), 2)
        self.assertTrue(all(host == "fixture.example.test" for host, _ in self.server.requests))
        self.assertFalse(any(path in ("/private", "/blog/") for _, path in self.server.requests))
        self.assertTrue(any(path == "/team/hidden" for _, path in self.server.requests))
        self.assertEqual(DailyUsage.objects.get().requests, len(self.server.requests))
        candidate = self.campaign.urls.get(url="http://vendor.fixture.test/reps/alex")
        self.assertEqual(candidate.decision, "pending")
        vendor = Source.objects.create(name="Reviewed vendor", url=candidate.url, approved=True, allowed_paths="/reps")
        approve(candidate, vendor)
        next_run = start(self.campaign)
        self.finish(next_run)
        self.assertEqual(next_run.status, "completed")
        self.assertEqual(Lead.objects.count(), 3)
        self.assertIn(("vendor.fixture.test", "/reps/alex"), self.server.requests)
        self.assertEqual(next_run.jobs.get(url="http://fixture.example.test/map-index").kind, "sitemap")

    def test_sitemap_pages_below_the_campaign_minimum_are_not_saved(self):
        self.server.pages["/nested.xml"] = (200, "application/xml",
            b'<urlset><url><loc>http://fixture.example.test/team/hidden</loc></url>'
            b'<url><loc>http://fixture.example.test/blog/fictional-post</loc></url>'
            b'<url><loc>http://fixture.example.test/tag/fictional</loc></url></urlset>')
        self.campaign.min_score = 35
        self.campaign.save()
        run = start(self.campaign)
        self.finish(run)
        saved = set(self.campaign.urls.values_list("url", flat=True))
        self.assertIn("http://fixture.example.test/team/hidden", saved)
        self.assertNotIn("http://fixture.example.test/blog/fictional-post", saved)
        self.assertNotIn("http://fixture.example.test/tag/fictional", saved)
        nested = run.jobs.get(url="http://fixture.example.test/nested.xml")
        self.assertIn("Read 3 sitemap entries; kept 1", nested.message)

    def test_sitemap_keeps_a_penalized_direct_page_and_drops_a_negative_score(self):
        # The sitemap gate is the same eligibility check as queueing. A direct page whose
        # pre-yield rank already met the minimum stays saved when a yield penalty drops the
        # stored score; the penalty remains on the row as priority. A negative score still
        # cannot be saved, because that page can never be queued.
        from discovery import ranking, services
        real = ranking.current_candidate_score

        def yield_penalised(campaign, url, *args, **kwargs):
            score, reasons = real(campaign, url, *args, **kwargs)
            if url.endswith("/team/hidden"):
                return score - 30, reasons + ["Synthetic zero-yield penalty"]
            if url.endswith("/team/rejected"):
                return -100, ["Synthetic exclusion"]
            return score, reasons

        self.server.pages["/nested.xml"] = (
            200, "application/xml",
            b"<urlset>"
            b"<url><loc>http://fixture.example.test/team/hidden</loc></url>"
            b"<url><loc>http://fixture.example.test/team/rejected</loc></url>"
            b"</urlset>")
        self.campaign.min_score = 35
        self.campaign.save()
        with (patch.object(ranking, "current_candidate_score", side_effect=yield_penalised),
              patch.object(services, "current_candidate_score", side_effect=yield_penalised)):
            run = start(self.campaign)
            self.finish(run)
        hidden = self.campaign.urls.get(url="http://fixture.example.test/team/hidden")
        self.assertEqual(hidden.score, 5)  # +35 direct, then -30; not raised back to the minimum
        self.assertLess(hidden.score, self.campaign.min_score)
        self.assertTrue(run.jobs.filter(url=hidden.url, kind="page").exists())
        self.assertFalse(self.campaign.urls.filter(url="http://fixture.example.test/team/rejected").exists())
        nested = run.jobs.get(url="http://fixture.example.test/nested.xml")
        self.assertIn("Read 2 sitemap entries; kept 1", nested.message)

    def test_403_blocks_only_the_source_records_it_and_is_not_retried(self):
        self.server.pages["/"] = (403, "text/plain", b"Forbidden")
        self.campaign.use_sitemaps = False
        self.campaign.save()
        run = start(self.campaign)
        self.finish(run)
        self.campaign.refresh_from_db()
        self.source.refresh_from_db()
        # The campaign keeps running for other sources; the blocked one is stopped and recorded.
        self.assertTrue(self.campaign.active)
        self.assertEqual(run.status, "failed")
        self.assertFalse(self.source.active)
        self.assertIsNotNone(self.source.blocked_at)
        self.assertIn("403", self.source.blocked_reason)
        self.assertFalse(tick(self.token, prefer_discovery=True))
        requests = len(self.server.requests)
        # A later run does not seed the blocked source again.
        rerun = start(self.campaign)
        self.finish(rerun)
        self.assertEqual(len(self.server.requests), requests)
        self.assertFalse(rerun.jobs.filter(source=self.source).exclude(status="skipped").exists())

    def test_redirect_to_private_address_is_blocked(self):
        self.source.url = "http://fixture.example.test/redirect/"
        self.source.save()
        self.campaign.use_sitemaps = False
        self.campaign.save()
        run = start(self.campaign)
        self.finish(run)
        self.assertEqual(run.status, "failed")
        self.assertEqual([path for _, path in self.server.requests], ["/robots.txt", "/redirect/"])

    def test_restrictive_robots_prevents_page_fetch(self):
        self.server.pages["/robots.txt"] = (200, "text/plain", b"User-agent: *\nDisallow: /\n")
        run = start(self.campaign)
        self.finish(run)
        self.source.refresh_from_db()
        self.assertIsNotNone(self.source.blocked_at)
        self.assertIn("robots.txt", self.source.blocked_reason)
        self.assertEqual([path for _, path in self.server.requests], ["/robots.txt"])
