import json
import os
import tempfile
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from discovery.evaluation import average_precision, evaluate, load_labels
from discovery.models import Campaign, DiscoveredURL
from leads.models import Source


class EvaluationTests(TestCase):
    def setUp(self):
        self.campaign = Campaign.objects.create(name="Eval")
        self.tmp = tempfile.mkdtemp()

    def write(self, data, name="labels.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def labels(self):
        return {"version": 1, "pages": [
            {"url": "https://a.example/our-team/", "label": "reps", "anchor": "Merchant services sales team"},
            {"url": "https://a.example/blog/news/", "label": "irrelevant", "anchor": "News"},
            {"url": "https://a.example/pricing/", "label": "irrelevant", "anchor": "Pricing"},
            {"url": "https://a.example/careers/", "label": ""}],
            "sites": ["https://a.example", "https://missing.example"]}

    def test_average_precision(self):
        self.assertEqual(average_precision([True, False, True]), (1 + 2 / 3) / 2)
        self.assertEqual(average_precision([False, False]), 0.0)

    def test_load_skips_blank_and_rejects_unknown_labels(self):
        pages, sites = load_labels(self.write(self.labels()))
        self.assertEqual(len(pages), 3)
        self.assertEqual(len(sites), 2)
        bad = self.labels()
        bad["pages"][0]["label"] = "maybe"
        with self.assertRaises(ValueError):
            load_labels(self.write(bad, "bad.json"))
        with self.assertRaises(ValueError):
            load_labels(self.write({"version": 2}, "v2.json"))

    def test_evaluate_ranks_gate_and_site_recall(self):
        DiscoveredURL.objects.create(campaign=self.campaign, url="https://a.example/our-team/",
                                     origin="https://a.example", method="commoncrawl")
        pages, sites = load_labels(self.write(self.labels()))
        report = evaluate(self.campaign, pages, sites)
        self.assertEqual(report["positives"], 1)
        self.assertEqual(report["precision_at"][10], round(1 / 3, 3))
        self.assertEqual(report["average_precision"], 1.0)  # the rep page ranks first
        self.assertEqual(report["gate"]["recall"], 1.0)
        self.assertEqual(report["sites"]["found"], 1)
        self.assertEqual(report["sites"]["by_method"], {"commoncrawl": 1})
        self.assertEqual(report["sites"]["missing"], ["https://missing.example"])

    def test_export_then_evaluate_round_trip(self):
        source = Source.objects.create(name="S", url="https://a.example/", approved=True)
        DiscoveredURL.objects.create(campaign=self.campaign, url="https://a.example/our-team/",
                                     origin="https://a.example", source=source, label="Our team")
        path = os.path.join(self.tmp, "template.json")
        out = StringIO()
        call_command("discovery_eval", campaign=self.campaign.pk, export=path, stdout=out)
        self.assertIn("Wrote 1 unlabeled page(s)", out.getvalue())
        with open(path, encoding="utf-8") as handle:
            template = json.load(handle)
        self.assertEqual(template["pages"][0]["label"], "")
        with self.assertRaises(CommandError):
            call_command("discovery_eval", campaign=self.campaign.pk, labels=path)  # nothing labeled yet
        template["pages"][0]["label"] = "reps"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(template, handle)
        out = StringIO()
        call_command("discovery_eval", campaign=self.campaign.pk, labels=path, stdout=out)
        self.assertIn("Labeled pages: 1 (1 with reps)", out.getvalue())
        self.assertIn("Eligibility gate:", out.getvalue())

    def test_example_label_file_is_valid(self):
        pages, sites = load_labels("docs/eval/discovery_labels.example.json")
        self.assertEqual(len(pages), 3)
        self.assertEqual(sites, ["https://expected-agent.example"])

    def test_command_needs_exactly_one_mode(self):
        with self.assertRaises(CommandError):
            call_command("discovery_eval", campaign=self.campaign.pk)
