import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from discovery.models import Campaign, DiscoveredURL, DiscoveryJob, DiscoveryRun
from discovery.turnover import protection_breakdown
from leads.models import Source

BIG = "https://big.example"


class ProtectionBreakdownTests(TestCase):
    def setUp(self):
        self.campaign = Campaign.objects.create(name="Protection", active=True, use_sitemaps=False)
        self.run = DiscoveryRun.objects.create(campaign=self.campaign)

    def row(self, path, **extra):
        return DiscoveredURL.objects.create(campaign=self.campaign, url=BIG + path, origin=BIG, decision="approved", kind="page", **extra)

    def test_explains_why_a_large_origin_cannot_be_parked(self):
        source = Source.objects.create(name="big", url=BIG + "/directory/", approved=True)
        self.row("/seed", method="seed")
        self.row("/imported", method="operator_import")
        self.row("/manual", manual_review_required=True)
        DiscoveredURL.objects.create(campaign=self.campaign, url=BIG + "/sitemap.xml", origin=BIG, kind="sitemap")
        productive = self.row("/productive/1")
        DiscoveryJob.objects.create(run=self.run, candidate=productive, url=productive.url, status="done", contacts_seen=2)
        working = self.row("/queued")
        DiscoveryJob.objects.create(run=self.run, candidate=working, url=working.url, status="queued")
        self.row("/directory/", source=source)
        self.row("/location/a", source=source)
        self.row("/location/b", source=source)
        archived = self.row("/archived")
        DiscoveredURL.objects.filter(pk=archived.pk).update(archived_at=archived.first_seen)
        result = protection_breakdown(self.campaign, [BIG, "https://other.example"])
        self.assertEqual(result[BIG], {"seed": 1, "operator_import": 1, "manual_review": 1, "sitemap": 1,
                                       "produced_contacts": 1, "active_job": 1, "starting_url": 1, "parkable": 2})
        self.assertEqual(result["https://other.example"], {})
        self.assertEqual(protection_breakdown(self.campaign, []), {})

    def test_inventory_report_includes_the_breakdown_for_top_origins(self):
        self.row("/location/a")
        out = StringIO()
        call_command("discovery_inventory", "--campaign", str(self.campaign.pk), stdout=out)
        report = json.loads(out.getvalue())
        self.assertEqual(report["before"]["top_origin_protection"], {BIG: {"parkable": 1}})
