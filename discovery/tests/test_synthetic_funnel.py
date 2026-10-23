"""Synthetic campaign: index URLs move from discovery to stored leads without a network.

Every person, address and phone number below is fictional. Nothing here is a measured
production yield.
"""
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from discovery.models import Campaign, DiscoveredURL, DiscoveryJob, DiscoveryRun
from discovery.services import finish, process, register_batch
from leads.models import DomainState, Lead, Observation, Source
from leads.services.network import Response

ORIGIN = "https://reps.example.test"

TEAM = """
<article class="team-member"><h3>Dana Discovery</h3>
<p class="role">Merchant services sales representative</p>
<a href="mailto:dana-discovery@example.com">Email</a>
<a href="tel:+1-555-0100">555-0100</a></article>
""".encode()

DIRECTORY = """
<article class="team-member"><h3>Riley Example</h3>
<p class="role">Merchant services sales representative</p>
<a href="mailto:riley-example@example.com">Email</a>
<a href="tel:+1-555-0101">555-0101</a></article>
<article class="team-member"><h3>Casey Sample</h3>
<p class="role">Merchant services sales representative</p>
<a href="mailto:casey-sample@example.com">Email</a>
<a href="tel:+1-555-0102">555-0102</a></article>
""".encode()

EMPTY = b"<h1>Our team</h1><p>No public contacts are listed on this synthetic page.</p>"
ENGINEER = """
<article class="team-member"><h3>Robin Sample</h3><p class="role">Engineer</p>
<a href="mailto:robin-sample@example.com">Email</a></article>
""".encode()
HOME = b'<h1>Fictional directory</h1><a href="/our-team/">Our team</a>'
FORM = b"<form><p>Send a message</p><input type='email'><button>Send</button></form>"
ARTICLE = b"<article><p>Alex Salesperson was mentioned in this synthetic post.</p></article>"
JOB = b"<h1>Sales representative vacancy</h1><p>Apply with a resume. No employee is listed.</p>"

PAGES = {
    ORIGIN + "/": HOME,
    ORIGIN + "/our-team/": TEAM,
    ORIGIN + "/team/": DIRECTORY,
    ORIGIN + "/our-team/empty/": EMPTY,
    ORIGIN + "/our-team/engineer/": ENGINEER,
    ORIGIN + "/contact/": FORM,
    ORIGIN + "/blog/meet-the-sales-team/": ARTICLE,
    ORIGIN + "/jobs/sales-representative/": JOB,
}


class SyntheticFunnelTests(TestCase):
    def test_suitable_pages_become_leads_and_unsuitable_pages_do_not(self):
        source = Source.objects.create(
            name="Synthetic reps", url=ORIGIN + "/", approved=True, allow_homepage=True,
            collector="http", delay_seconds=0, follow_links=False, require_sales_role=True)
        campaign = Campaign.objects.create(
            name="Synthetic funnel", keywords="", sales_terms="sales representative", exclusions=(
                "path:blog\npath:article\npath:jobs"),
            min_score=35, max_pages=20, use_sitemaps=False, active=True)
        campaign.sources.add(source)
        history = DiscoveryRun.objects.create(campaign=campaign, status="completed")
        for index in range(3):
            url = f"{ORIGIN}/staff/history-{index}/"
            candidate = DiscoveredURL.objects.create(
                campaign=campaign, url=url, origin=ORIGIN, source=source, decision="approved", label="Staff")
            DiscoveryJob.objects.create(
                run=history, candidate=candidate, source=source, url=url, status="done", contacts_seen=0)
        DomainState.objects.create(
            origin=ORIGIN, robots_checked_at=timezone.now(), robots_text="User-agent: *\nAllow: /",
            next_allowed_at=timezone.now())

        specs = [
            {"url": ORIGIN + "/", "label": "Home", "method": "seed", "seed": True, "kind": "page",
             "source_hint": source.pk},
            {"url": ORIGIN + "/our-team/", "label": "", "method": "commoncrawl", "kind": "page"},
            {"url": ORIGIN + "/team/", "label": "Team directory", "method": "commoncrawl", "kind": "page"},
            {"url": ORIGIN + "/our-team/empty/", "label": "", "method": "commoncrawl", "kind": "page"},
            {"url": ORIGIN + "/our-team/engineer/", "label": "", "method": "commoncrawl", "kind": "page"},
            {"url": ORIGIN + "/contact/", "label": "Contact form", "method": "commoncrawl", "kind": "page"},
            {"url": ORIGIN + "/blog/meet-the-sales-team/", "label": "sales representative",
             "method": "commoncrawl", "kind": "page"},
            {"url": ORIGIN + "/jobs/sales-representative/", "label": "sales representative",
             "method": "commoncrawl", "kind": "page"},
        ]
        run = DiscoveryRun.objects.create(campaign=campaign, status="queued")
        register_batch(run, specs, campaign=campaign, order=True)
        rows = {row.url: row for row in campaign.urls.exclude(url__contains="/staff/history-")}
        queued_urls = set(run.jobs.values_list("url", flat=True))
        fetched = []

        def fake_fetch(url, *args, **kwargs):
            self.assertIn(url, PAGES)
            fetched.append(url)
            return Response(url, 200, {"content-type": "text/html"}, PAGES[url])

        for _ in range(len(PAGES)):
            job = run.jobs.filter(status="queued").order_by("-priority", "id").first()
            if job is None:
                break
            DiscoveryJob.objects.filter(pk=job.pk).update(status="processing")
            job.refresh_from_db()
            with patch("discovery.services.fetch", side_effect=fake_fetch):
                process(job)
        finish(run)
        run.refresh_from_db()

        leads = list(Lead.objects.order_by("name").values_list("name", flat=True))
        team_job = run.jobs.get(url=ORIGIN + "/our-team/")
        metrics = {
            "synthetic": True,
            "discovered": len(specs),
            "registered": len(rows),
            "approved": sum(row.decision == "approved" for row in rows.values()),
            "dismissed": sum(row.decision == "dismissed" for row in rows.values()),
            "queued": len(queued_urls),
            "fetched": len(fetched),
            "lead_producing_jobs": run.jobs.filter(contacts_seen__gt=0).count(),
            "leads": leads,
            "observations": Observation.objects.count(),
            "penalized_team_priority": team_job.priority,
            "dismissed_reasons": {
                url: row.reasons for url, row in rows.items() if row.decision == "dismissed"},
            "unqueued": sorted(set(PAGES) - queued_urls),
        }
        artifact = Path("/opt/cursor/artifacts")
        if artifact.is_dir():
            lines = [f"{key}: {value}" for key, value in metrics.items()]
            (artifact / "funnel_metrics.txt").write_text("\n".join(lines) + "\n")

        self.assertEqual(metrics["registered"], 8)
        self.assertEqual(metrics["dismissed"], 2)
        self.assertEqual(rows[ORIGIN + "/blog/meet-the-sales-team/"].decision, "dismissed")
        self.assertEqual(rows[ORIGIN + "/jobs/sales-representative/"].decision, "dismissed")
        self.assertIn("Excluded", " ".join(rows[ORIGIN + "/blog/meet-the-sales-team/"].reasons))
        self.assertNotIn(ORIGIN + "/contact/", queued_urls)
        self.assertNotIn(ORIGIN + "/blog/meet-the-sales-team/", fetched)
        self.assertNotIn(ORIGIN + "/jobs/sales-representative/", fetched)
        self.assertEqual(team_job.priority, 20)
        self.assertGreater(team_job.contacts_seen, 0)
        self.assertEqual(leads, ["Casey Sample", "Dana Discovery", "Riley Example"])
        self.assertEqual(Observation.objects.count(), 3)
        self.assertEqual(run.jobs.get(url=ORIGIN + "/our-team/engineer/").contacts_seen, 0)
        self.assertEqual(run.jobs.get(url=ORIGIN + "/our-team/empty/").contacts_seen, 0)
        self.assertEqual(run.jobs.get(url=ORIGIN + "/").contacts_seen, 0)
        self.assertEqual(run.status, "completed")
        self.assertGreater(run.pages_done, 0)
