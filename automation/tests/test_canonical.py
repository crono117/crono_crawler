"""Canonical-origin onboarding for policy-created sources. Offline: local HTTP server, fixture DNS."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase
from django.utils import timezone

from automation.canonical import MAX_REBINDS, classify, rebind, redirect_target, site_origins
from automation.models import ProbePage, RecipeVersion, SiteAssessment, SiteAutomationJob
from automation.policy import consider
from discovery.models import DiscoveredURL
from leads.models import DomainState, Lead, Source
from leads.services.worker import tick
from .test_pipeline import HTML, AutomationCase

PATHS = "/team\n/our-team\n/about"


class ClassifyTests(SimpleTestCase):
    def check(self, source, target, expected, paths=PATHS, homepage=True):
        self.assertEqual(classify(source, target, allowed_paths=paths, allow_homepage=homepage), expected,
                         f"{source} -> {target}")

    def test_same_site_canonical_forms_are_rebindable(self):
        self.check("https://example.com/team/", "https://www.example.com/team/", "canonical")
        self.check("https://www.example.com/team/", "https://example.com/team/", "canonical")
        self.check("http://example.com/team/", "https://example.com/team/", "canonical")
        self.check("http://example.com/team/", "https://www.example.com/team", "canonical")
        self.check("https://example.com/", "https://www.example.com/", "canonical")

    def test_same_site_path_must_stay_inside_policy_scope(self):
        self.check("https://example.com/team/", "https://www.example.com/blog/", "canonical_path_out_of_scope")
        self.check("https://example.com/", "https://www.example.com/", "canonical_path_out_of_scope", homepage=False)
        self.check("https://example.com/team/", "https://www.example.com/team/../admin", "canonical_path_out_of_scope")

    def test_hostile_and_unrelated_destinations_are_rejected(self):
        cases = {
            "https://www.example.com/team/?x": "canonical",  # control: query alone is still same site
            "http://www.example.com/team/": "downgrade",
            "http://example.com/team/": "downgrade",
            "https://shop.example.com/team/": "other_subdomain",
            "https://www.www.example.com/team/": "other_subdomain",
            "https://example.com.evil.net/team/": "cross_domain",
            "https://wwwexample.com/team/": "cross_domain",
            "https://examp1e.com/team/": "cross_domain",
            "https://xn--exmple-cua.com/team/": "cross_domain",
            "https://www.example.org/team/": "cross_domain",
            "https://user:pass@www.example.com/team/": "invalid",
            "https://www.example.com:8443/team/": "invalid",
            "ftp://www.example.com/team/": "invalid",
            "": "unparsed",
            "https://example.com/other/": "same_origin",
        }
        for target, expected in cases.items():
            with self.subTest(target=target):
                self.check("https://example.com/team/", target, expected)

    def test_parent_to_subdomain_is_not_same_site(self):
        self.check("https://sales.example.com/team/", "https://example.com/team/", "other_subdomain")
        self.check("https://sales.example.com/team/", "https://www.sales.example.com/team/", "canonical")

    def test_helpers(self):
        self.assertEqual(sorted(site_origins("https://www.Example.com/x")),
                         ["http://example.com", "http://www.example.com", "https://example.com", "https://www.example.com"])
        self.assertEqual(redirect_target("Page unavailable: URL or redirect falls outside the approved scope: "
                                         "https://www.example.com/team/"), "https://www.example.com/team/")
        self.assertEqual(redirect_target("HTTP 404."), "")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        host = self.headers["Host"].split(":")[0]
        self.server.requests.append((host, self.path))
        status, content_type, body, location = self.server.pages.get(
            (host, self.path), (404, "text/plain", b"missing", None))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


ROBOTS = (200, "text/plain", b"User-agent: *\nAllow: /", None)
TEAM = (200, "text/html", HTML.encode(), None)


def moved(location):
    return (301, "text/plain", b"", location)


class CanonicalHTTPTests(AutomationCase):
    HOSTS = {"vendor.fixture.test": "8.8.8.8", "www.vendor.fixture.test": "8.8.8.8",
             "other.fixture.test": "8.8.8.8", "shop.vendor.fixture.test": "8.8.8.8"}

    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = []
        self.server.pages = {
            ("vendor.fixture.test", "/robots.txt"): ROBOTS,
            ("www.vendor.fixture.test", "/robots.txt"): ROBOTS,
            ("vendor.fixture.test", "/team/"): moved("http://www.vendor.fixture.test/team/"),
            ("www.vendor.fixture.test", "/team/"): TEAM,
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        connect, original_dns, hosts = socket.create_connection, socket.getaddrinfo, dict(self.HOSTS)
        self.hosts = hosts

        def fixture_dns(host, port, *args, **kwargs):
            if (host, port) == self.server.server_address:
                return original_dns(host, port, *args, **kwargs)
            if host not in hosts:
                raise socket.gaierror("unknown fixture host")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (hosts[host], port))]

        def fixture_connect(address, *args, **kwargs):
            self.assertEqual(address, ("8.8.8.8", 80))
            return connect(self.server.server_address, *args, **kwargs)
        self.dns = patch("leads.services.network.socket.getaddrinfo", side_effect=fixture_dns)
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

    def finish(self, job):
        for _ in range(80):
            DomainState.objects.update(next_allowed_at=timezone.now())
            ProbePage.objects.update(available_at=timezone.now())
            SiteAutomationJob.objects.update(available_at=timezone.now())
            tick(self.token, prefer_discovery=True)
            job.refresh_from_db()
            if job.state in ("active", "paused", "failed"):
                return job
        self.fail(job.message)

    def start_job(self, url="http://vendor.fixture.test/team/"):
        return self.candidate(url).source.automation_job

    def test_www_redirect_rebinds_once_and_reaches_active_with_leads(self):
        job = self.finish(self.start_job())
        self.assertEqual(job.state, "active", job.message)
        source = Source.objects.get(pk=job.source_id)
        self.assertEqual(source.url, "http://www.vendor.fixture.test/team/")
        self.assertEqual(source.approval_kind, "policy")
        self.assertEqual((job.original_url, job.canonical_rebinds), ("http://vendor.fixture.test/team/", 1))
        self.assertEqual(Lead.objects.count(), 2)
        # Robots are checked again for the canonical origin before any page there is fetched.
        self.assertEqual(self.server.requests[:4], [
            ("vendor.fixture.test", "/robots.txt"), ("vendor.fixture.test", "/team/"),
            ("www.vendor.fixture.test", "/robots.txt"), ("www.vendor.fixture.test", "/team/")])
        self.assertIn("Setup rebound http://vendor.fixture.test -> http://www.vendor.fixture.test", source.approval_notes)
        # Scope stays exact: the old origin is no longer in scope for the rebound source.
        from leads.services.network import in_scope
        self.assertFalse(in_scope(source, "http://vendor.fixture.test/team/"))

    def assert_failed_closed(self, job, destination_host):
        self.assertEqual(job.state, "failed", job.message)
        self.assertEqual(job.canonical_rebinds, 0)
        self.assertEqual(Source.objects.get(pk=job.source_id).url, "http://vendor.fixture.test/team/")
        self.assertNotIn(destination_host, {host for host, _ in self.server.requests})
        self.assertEqual(SiteAssessment.objects.get(job=job).outcome, "redirect_out_of_scope")
        self.assertFalse(Lead.objects.exists())

    def test_cross_domain_redirect_still_fails_without_touching_destination(self):
        self.server.pages[("vendor.fixture.test", "/team/")] = moved("http://other.fixture.test/team/")
        self.assert_failed_closed(self.finish(self.start_job()), "other.fixture.test")

    def test_other_subdomain_redirect_still_fails(self):
        self.server.pages[("vendor.fixture.test", "/team/")] = moved("http://shop.vendor.fixture.test/team/")
        self.assert_failed_closed(self.finish(self.start_job()), "shop.vendor.fixture.test")

    def test_same_site_redirect_to_a_path_outside_policy_scope_fails(self):
        self.server.pages[("vendor.fixture.test", "/team/")] = moved("http://www.vendor.fixture.test/blog/")
        self.assert_failed_closed(self.finish(self.start_job()), "www.vendor.fixture.test")

    def test_redirect_loop_back_to_the_authorized_origin_stops_after_one_rebind(self):
        self.server.pages[("www.vendor.fixture.test", "/team/")] = moved("http://vendor.fixture.test/team/")
        job = self.finish(self.start_job())
        self.assertEqual(job.state, "failed")
        # Judged against the originally authorized URL, the bounce back is not a new canonical form.
        self.assertEqual(job.canonical_rebinds, 1)
        self.assertEqual(len([r for r in self.server.requests if r[1] == "/team/"]), 2)
        self.assertFalse(Lead.objects.exists())

    def test_existing_source_for_the_canonical_origin_pauses_for_review(self):
        job = self.start_job()
        Source.objects.create(name="Operator copy", url="http://www.vendor.fixture.test/team/", approved=True)
        job = self.finish(job)
        self.assertEqual(job.state, "paused")
        self.assertEqual(job.canonical_rebinds, 0)
        self.assertEqual(SiteAssessment.objects.get(job=job).outcome, "canonical_conflict")
        self.assertNotIn("www.vendor.fixture.test", {host for host, _ in self.server.requests})

    def test_dismissed_canonical_origin_is_never_adopted(self):
        job = self.start_job()
        DiscoveredURL.objects.create(campaign=self.campaign, url="http://www.vendor.fixture.test/",
                                     origin="http://www.vendor.fixture.test", decision="dismissed",
                                     dismissal_scope="origin")
        job = self.finish(job)
        self.assertNotEqual(job.state, "active")
        self.assertEqual(job.canonical_rebinds, 0)
        self.assertNotIn("www.vendor.fixture.test", {host for host, _ in self.server.requests})

    def test_canonical_host_resolving_to_a_private_address_is_still_refused(self):
        self.hosts["www.vendor.fixture.test"] = "10.0.0.8"
        job = self.finish(self.start_job())
        self.assertEqual(job.canonical_rebinds, 1)
        self.assertIn(job.state, ("paused", "failed"))
        self.assertIn("not a public Internet address", job.message)
        self.assertNotIn("www.vendor.fixture.test", {host for host, _ in self.server.requests})
        self.assertFalse(Lead.objects.exists())

    def test_stale_or_reauthorized_jobs_cannot_rebind(self):
        job = self.start_job()
        page = job.pages.get()
        job.state = "probing"
        job.save(update_fields=["state"])
        stale = ProbePage(job=job, generation=job.generation + 1, phase="probe", url=page.url)
        self.assertIsNone(rebind(job, stale, "http://www.vendor.fixture.test/team/"))
        canary = ProbePage(job=job, generation=job.generation, phase="canary", url=page.url)
        self.assertIsNone(rebind(job, canary, "http://www.vendor.fixture.test/team/"))
        SiteAutomationJob.objects.filter(pk=job.pk).update(canonical_rebinds=MAX_REBINDS)
        job.refresh_from_db()
        self.assertIsNone(rebind(job, page, "http://www.vendor.fixture.test/team/"))  # cap reached
        SiteAutomationJob.objects.filter(pk=job.pk).update(canonical_rebinds=0)
        job.refresh_from_db()
        self.policy.probe_pages = 3
        self.policy.save()  # snapshot no longer matches the job's authorization
        self.assertIsNone(rebind(job, page, "http://www.vendor.fixture.test/team/"))
        job.refresh_from_db()
        self.assertEqual((job.canonical_rebinds, job.source.url), (0, "http://vendor.fixture.test/team/"))

    def test_requeue_recovers_a_historical_canonical_failure_only_when_applied(self):
        with patch("automation.canonical.rebind", return_value=None):  # behavior before this change
            job = self.finish(self.start_job())
        self.assertEqual(job.state, "failed")
        self.assertEqual(SiteAssessment.objects.get(job=job).outcome, "redirect_out_of_scope")
        other = self.candidate("http://other.fixture.test/team/").source.automation_job
        SiteAutomationJob.objects.filter(pk=other.pk).update(
            state="failed", message="Page unavailable: URL or redirect falls outside the approved scope: "
                                    "https://unrelated.example.net/team/")
        out = StringIO()
        call_command("automation_requeue_canonical", campaign=self.campaign.pk, stdout=out)
        dry = json.loads(out.getvalue())
        self.assertEqual([row["job_id"] for row in dry["eligible"]], [job.pk])
        job.refresh_from_db()
        self.assertEqual(job.state, "failed")  # dry run changed nothing
        call_command("automation_requeue_canonical", campaign=self.campaign.pk, apply=True, stdout=StringIO())
        job = self.finish(job)
        self.assertEqual(job.state, "active", job.message)
        self.assertEqual(job.canonical_rebinds, 1)
        other.refresh_from_db()
        self.assertEqual(other.state, "failed")

    def test_operator_sources_are_never_rebound(self):
        job = self.start_job()
        Source.objects.filter(pk=job.source_id).update(approval_kind="operator")
        from automation.policy import scope_hash
        job.refresh_from_db()
        SiteAutomationJob.objects.filter(pk=job.pk).update(state="probing", scope_hash=scope_hash(job.source))
        job.refresh_from_db()
        self.assertIsNone(rebind(job, job.pages.get(), "http://www.vendor.fixture.test/team/"))


class SameSiteAdmissionTests(AutomationCase):
    def test_existing_same_site_source_blocks_a_duplicate_job(self):
        Source.objects.create(name="Existing", url="https://www.vendor.example.org/team/", approved=True)
        candidate = self.candidate("https://vendor.example.org/team/")
        self.assertIsNone(getattr(candidate.source, "automation_job", None) if candidate.source else None)
        self.assertEqual(SiteAutomationJob.objects.count(), 0)

    def test_same_site_origin_dismissal_is_honored(self):
        DiscoveredURL.objects.create(campaign=self.campaign, url="https://www.vendor.example.org/",
                                     origin="https://www.vendor.example.org", decision="dismissed",
                                     dismissal_scope="origin")
        self.candidate("https://vendor.example.org/team/")
        self.assertEqual(SiteAutomationJob.objects.count(), 0)

    def test_rebound_source_blocks_a_second_job_for_its_old_origin(self):
        first = self.candidate("https://vendor.example.org/team/").source
        Source.objects.filter(pk=first.pk).update(url="https://www.vendor.example.org/team/")
        pending = DiscoveredURL.objects.create(campaign=self.campaign, url="https://vendor.example.org/our-team/",
                                               origin="https://vendor.example.org", decision="pending", score=90,
                                               label="Our sales team")
        self.assertIsNone(consider(pending))
        self.assertEqual(SiteAutomationJob.objects.count(), 1)


class AuditCommandTests(AutomationCase):
    def make(self, url, state, message="", outcome="", reasons=(), recipe_score=None, rebinds=0, original=""):
        job = self.candidate(url).source.automation_job
        SiteAutomationJob.objects.filter(pk=job.pk).update(state=state, message=message, canonical_rebinds=rebinds,
                                                           original_url=original)
        if outcome:
            SiteAssessment.objects.create(job=job, source=job.source, origin=url.split("/team")[0],
                                          outcome=outcome, reasons=list(reasons or [outcome]))
        if recipe_score is not None:
            version = RecipeVersion.objects.create(source=job.source, job=job, version=1, generation=1,
                                                   score=recipe_score, scope_hash=job.scope_hash,
                                                   status="known_good")
            SiteAutomationJob.objects.filter(pk=job.pk).update(current_recipe=version, last_good_recipe=version)
        return job

    def test_audit_groups_jobs_classifies_redirects_and_replays_policy(self):
        scope = "Page unavailable: URL or redirect falls outside the approved scope: "
        self.policy.max_sites_per_day = 25
        self.policy.save()
        self.make("https://a.example.org/team/", "failed", scope + "https://www.a.example.org/team/",
                  "redirect_out_of_scope")
        self.make("https://b.example.org/team/", "failed", scope + "https://elsewhere.example.net/team/",
                  "redirect_out_of_scope")
        self.make("https://c.example.org/team/", "paused", "robots.txt returned HTML; review source access.")
        self.make("https://d.example.org/team/", "paused",
                  "Recipe review needed: no_matching_cards. No contacts were stored.")
        self.make("https://e.example.org/team/", "paused", "", "people_without_personal_contacts")
        self.make("https://f.example.org/team/", "active", "Canary passed.", recipe_score=72)
        out = StringIO()
        call_command("automation_audit", campaign=self.campaign.pk, stdout=out)
        text = out.getvalue()
        result = json.loads(text)
        summary = result["summary"]
        self.assertEqual(summary["jobs"], 6)
        self.assertEqual(summary["by_category"], {"redirect_scope": 2, "access": 1, "recipe_segmentation": 1,
                                                  "cards_without_sales_records": 1, "active": 1})
        self.assertEqual(summary["redirects"]["safely_canonical_recoverable"], 1)
        self.assertEqual(summary["redirects"]["rejected_cross_domain"], 1)
        self.assertEqual(summary["by_detail"]["access:robots_returned_html"], 1)
        self.assertEqual(summary["recipe_floor_replay"]["accepted_below_model_floor"], 1)
        self.assertIn("require_page_intent_replay", summary)
        self.assertNotIn("@", text)  # no contact values
        self.assertNotIn("<", text)  # no HTML

    def test_audit_is_read_only(self):
        self.make("https://a.example.org/team/", "failed", "x", "redirect_out_of_scope")
        before = (SiteAutomationJob.objects.count(), Source.objects.count(), DiscoveredURL.objects.count(),
                  list(SiteAutomationJob.objects.values_list("state", "generation")))
        call_command("automation_audit", campaign=self.campaign.pk, since="2000-01-01", no_jobs=True, stdout=StringIO())
        self.assertEqual(before, (SiteAutomationJob.objects.count(), Source.objects.count(),
                                  DiscoveredURL.objects.count(),
                                  list(SiteAutomationJob.objects.values_list("state", "generation"))))
