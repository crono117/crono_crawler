from datetime import timedelta
from io import StringIO

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from automation.views import lead_totals, recent_leads
from discovery.models import Campaign, DiscoveredURL, DiscoveryJob, DiscoveryRun
from leads.models import Lead, Observation, Source
from leads.services.reporting import production_leads, production_totals
from leads.services.storage import save_records


class ProductionReportingTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.since = self.now - timedelta(days=7)
        self.real = Source.objects.create(name="Fictional Payments", url="https://payments.example.org/")
        self.demo = Source.objects.create(name="Demo", url="https://demo.example.org/", collector="demo")
        self.campaign = Campaign.objects.create(name="Real source campaign")
        self.campaign.sources.add(self.real)

    def lead(self, identity, source, *, first_seen=None, scope="shared", status="new"):
        lead = Lead.objects.create(identity=identity, name="Fictional Representative", contact_scope=scope,
                                   status=status, first_seen=first_seen or self.now)
        Observation.objects.create(lead=lead, source=source, page_url=source.url, evidence="Fixture evidence")
        return lead

    def test_totals_count_real_identities_once_across_sources_pages_and_demo_overlap(self):
        lead = self.lead("real", self.real)
        Observation.objects.create(lead=lead, source=self.real, page_url=self.real.url + "team", evidence="x")
        Observation.objects.create(lead=lead, source=self.demo, page_url=self.demo.url, evidence="x")
        self.lead("demo-only", self.demo)
        self.lead("older", self.real, first_seen=self.now - timedelta(days=8), scope="unknown")
        self.lead("suppressed", self.real, scope="unknown", status="suppressed")
        Lead.objects.create(identity="no-evidence", name="No Evidence")
        self.assertEqual(production_totals(self.since), {
            "total": 3, "new": 2, "shared": 1, "new_shared": 1, "available": 2, "needs_review": 2})
        self.assertEqual(lead_totals(Lead, self.since), {"total": 3, "today": 2, "shared": 1})
        self.assertEqual(production_leads().count(), 3)

    def test_campaign_report_is_distinct_and_uses_first_seen_not_observation_refresh(self):
        old = self.lead("old", self.real, first_seen=self.now - timedelta(days=8))
        self.lead("new", self.real)
        elsewhere = Source.objects.create(name="Elsewhere", url="https://elsewhere.example.org/")
        self.lead("other", elsewhere)
        self.lead("demo-only", self.demo)
        Observation.objects.create(lead=old, source=self.real, page_url=self.real.url + "team", evidence="new page")
        other_campaign = Campaign.objects.create(name="Another campaign")
        other_campaign.sources.add(self.real)
        self.assertEqual(production_totals(self.since, campaign_id=self.campaign.pk)["new"], 1)
        out = StringIO()
        call_command("discovery_yield", campaign=self.campaign.pk, days=7, stdout=out)
        self.assertIn("New production lead records: 1 (1 with shared company contact)", out.getvalue())
        self.assertIn("No completed page jobs", out.getvalue())
        self.assertIn("totals across campaigns may overlap", out.getvalue())

    def test_production_creations_survive_refreshes_with_zero_discovery_new_contacts(self):
        record = {"name": "Alex Fictional", "title": "Sales Representative", "company": "Fictional Payments",
                  "email": "info@payments.example.org", "phone": "", "contact_scope": "shared",
                  "person_tags": [], "evidence": "Alex Fictional Sales Representative info@payments.example.org"}
        save_records(self.real, self.real.url, "fixture", [record], [])
        save_records(self.real, self.real.url, "fixture", [record], [])
        run = DiscoveryRun.objects.create(campaign=self.campaign, status="completed")
        candidate = DiscoveredURL.objects.create(campaign=self.campaign, source=self.real,
            url=self.real.url, origin=self.real.url.rstrip("/"), decision="approved")
        DiscoveryJob.objects.create(run=run, source=self.real, candidate=candidate, url=self.real.url,
                                    status="done", contacts_seen=1, new_contacts=0)
        out = StringIO()
        call_command("discovery_yield", days=7, stdout=out)
        self.assertIn("New production lead records: 1 (1 with shared company contact)", out.getvalue())
        self.assertIn("0 new contact(s) recorded by discovery jobs", out.getvalue())
        self.assertEqual(Lead.objects.count(), 1)

    def test_demo_jobs_are_excluded_from_discovery_harvest(self):
        run = DiscoveryRun.objects.create(campaign=self.campaign, status="completed")
        for source in (self.real, self.demo):
            candidate = DiscoveredURL.objects.create(campaign=self.campaign, source=source,
                url=source.url, origin=source.url.rstrip("/"), decision="approved")
            DiscoveryJob.objects.create(run=run, source=source, candidate=candidate, url=source.url,
                                        status="done", contacts_seen=1, new_contacts=1)
        out = StringIO()
        call_command("discovery_yield", days=7, stdout=out)
        self.assertIn("Harvest rate: 1/1 pages (100%); 1 new contact(s)", out.getvalue())
        self.assertNotIn(self.demo.url.rstrip("/"), out.getvalue())

    def test_dashboard_shows_production_creations_and_excludes_demo_only_records(self):
        self.lead("real", self.real)
        self.lead("demo-only", self.demo)
        self.client.force_login(get_user_model().objects.create_user("report-operator", is_staff=True))
        response = self.client.get("/")
        self.assertEqual(response.context["lead_count"], 1)
        self.assertContains(response, "1 new lead in the last 24 hours")
        self.assertContains(response, "1 with shared company contact")


class PublishedSourceTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="Fictional", url="https://company.example.org/")
        self.lead = Lead.objects.create(identity="source-link", name="Alex Fictional", contact_scope="unknown")
        self.current = Observation.objects.create(lead=self.lead, source=self.source,
            page_url=self.source.url + "team/alex", evidence="Current source", last_seen=timezone.now() - timedelta(days=1))
        self.missing = Observation.objects.create(lead=self.lead, source=self.source,
            page_url=self.source.url + "old-profile", evidence="Old source", present=False)

    def test_list_links_directly_to_present_evidence_and_lab_preserves_unconfirmed_attribution(self):
        self.client.force_login(get_user_model().objects.create_user("source-operator", is_staff=True))
        html = BeautifulSoup(self.client.get("/leads/").content, "html.parser")
        link = html.find("a", string="View source ↗")
        self.assertEqual(link["href"], self.current.page_url)
        self.assertEqual(link["target"], "_blank")
        self.assertIn("noopener", link["rel"])
        with self.assertNumQueries(3):
            row = recent_leads(Lead)[0]
        self.assertEqual(row["source_url"], self.current.page_url)
        self.assertTrue(row["source_present"])
        self.assertEqual((row["scope_key"], row["scope"], row["shared"]), ("unknown", "Unconfirmed", False))

    def test_missing_only_evidence_is_labelled_and_unsafe_urls_are_not_linked(self):
        Observation.objects.filter(pk=self.current.pk).update(present=False)
        self.assertEqual(recent_leads(Lead)[0]["source_url"], self.missing.page_url)
        self.assertFalse(recent_leads(Lead)[0]["source_present"])
        self.client.force_login(get_user_model().objects.create_user("past-source-operator", is_staff=True))
        self.assertContains(self.client.get("/leads/"), "View previous source ↗")
        Observation.objects.filter(pk=self.current.pk).update(page_url="javascript:alert(1)")
        Observation.objects.filter(pk=self.missing.pk).update(page_url="data:text/plain,fixture")
        self.assertEqual(recent_leads(Lead)[0]["source_url"], "")
        self.assertNotContains(self.client.get("/leads/"), "javascript:")
