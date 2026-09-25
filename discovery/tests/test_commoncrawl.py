import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from discovery.models import Campaign, DiscoveryRun
from discovery.providers import common_crawl_collection, common_crawl_urls
from discovery.services import common_crawl_specs, register_common_crawl
from leads.models import Source
from leads.services.network import FetchError, Response

ENABLED = dict(COMMON_CRAWL_ENABLED=True, COMMON_CRAWL_COLLECTION="")


def ndjson(*urls):
    return "\n".join(json.dumps({"url": url}) for url in urls).encode()


@override_settings(**ENABLED)
class CommonCrawlProviderTests(TestCase):
    @patch("discovery.providers.fetch")
    def test_parses_index_lines_and_queries_exact_host(self, fetch):
        fetch.return_value = Response("https://index.commoncrawl.org/x", 200, {}, ndjson(
            "https://a.example/team/", "https://a.example/blog/") + b"\nnot json\n")
        self.assertEqual(common_crawl_urls("a.example", "CC-MAIN-2026-35"),
                         ["https://a.example/team/", "https://a.example/blog/"])
        url = fetch.call_args[0][0]
        self.assertTrue(url.startswith("https://index.commoncrawl.org/CC-MAIN-2026-35-index?"))
        self.assertIn("url=a.example%2F%2A", url)
        self.assertIn("filter=status%3A200", url)
        guard = fetch.call_args.kwargs["guard"]
        self.assertFalse(guard("https://evil.example/?x"))

    @patch("discovery.providers.fetch")
    def test_no_captures_is_empty(self, fetch):
        fetch.return_value = Response("https://index.commoncrawl.org/x", 404, {}, b"No Captures found")
        self.assertEqual(common_crawl_urls("a.example", "CC-MAIN-2026-35"), [])

    def test_rejects_wildcards_and_paths(self):
        for host in ("*.example", "a.example/x", ""):
            with self.assertRaises(ValueError):
                common_crawl_urls(host, "CC-MAIN-2026-35")

    @override_settings(COMMON_CRAWL_ENABLED=False)
    def test_disabled_by_default(self):
        with self.assertRaises(ValueError):
            common_crawl_urls("a.example", "CC-MAIN-2026-35")

    @patch("discovery.providers.fetch")
    def test_newest_collection_is_validated(self, fetch):
        fetch.return_value = Response("u", 200, {}, json.dumps([{"id": "CC-MAIN-2026-35"}]).encode())
        self.assertEqual(common_crawl_collection(), "CC-MAIN-2026-35")
        fetch.return_value = Response("u", 200, {}, json.dumps([{"id": "../../evil"}]).encode())
        with self.assertRaises(FetchError):
            common_crawl_collection()


@override_settings(**ENABLED)
class CommonCrawlDiscoveryTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="Agent site", url="https://agents.example/", approved=True)
        self.campaign = Campaign.objects.create(name="CC", keywords="", sales_terms="", active=True)
        self.campaign.sources.add(self.source)

    def lookup(self, host, collection):
        return ["https://agents.example/our-team/", "http://agents.example/meet-the-team/",
                "https://agents.example/our-team/", "https://agents.example/pricing/",
                "https://other.example/our-team/", "https://agents.example/our-team/?utm_source=x"]

    def test_specs_keep_only_in_scope_contact_pages(self):
        specs, counts = common_crawl_specs(self.campaign, "CC-MAIN-2026-35", lookup=self.lookup, pause=0)
        self.assertEqual([spec["url"] for spec in specs],
                         ["https://agents.example/our-team/", "https://agents.example/meet-the-team/"])
        self.assertEqual(counts, {"Agent site": 2})
        self.assertTrue(all(spec["method"] == "commoncrawl" for spec in specs))

    def test_registration_uses_ordinary_gates_and_needs_open_run(self):
        specs, _ = common_crawl_specs(self.campaign, "CC-MAIN-2026-35", lookup=self.lookup, pause=0)
        with self.assertRaises(ValueError):
            register_common_crawl(self.campaign, specs)
        self.campaign.exclusions = "path:meet-the-team"
        self.campaign.save()
        run = DiscoveryRun.objects.create(campaign=self.campaign)
        self.assertEqual(register_common_crawl(self.campaign, specs), 2)
        team = self.campaign.urls.get(url="https://agents.example/our-team/")
        self.assertEqual((team.method, team.decision), ("commoncrawl", "approved"))
        self.assertTrue(team.jobs.filter(run=run).exists())
        excluded = self.campaign.urls.get(url="https://agents.example/meet-the-team/")
        self.assertEqual(excluded.decision, "dismissed")
        self.assertFalse(excluded.jobs.exists())

    def test_command_dry_run_registers_nothing(self):
        spec = {"url": "https://agents.example/our-team/", "label": "", "context": "", "found_on": "",
                "method": "commoncrawl", "depth": 1, "kind": "page"}
        with patch("discovery.management.commands.discovery_commoncrawl.common_crawl_specs",
                   return_value=([spec], {"Agent site": 1})):
            out = StringIO()
            call_command("discovery_commoncrawl", campaign=self.campaign.pk, collection="CC-MAIN-2026-35",
                         dry_run=True, stdout=out)
        self.assertIn("Dry run: 1 URL(s) not registered.", out.getvalue())
        self.assertIn("https://agents.example/our-team/", out.getvalue())
        self.assertFalse(self.campaign.urls.exists())

    @override_settings(COMMON_CRAWL_ENABLED=False)
    def test_command_refuses_when_disabled(self):
        with self.assertRaises(CommandError):
            call_command("discovery_commoncrawl", campaign=self.campaign.pk)
