"""Offline discovery evaluation: export a label template or score labels. No network requests."""
import json

from django.core.management.base import BaseCommand, CommandError

from discovery.evaluation import evaluate, export_template, load_labels
from discovery.models import Campaign


class Command(BaseCommand):
    help = ("Export a label template from a campaign's recent candidates (--export), or evaluate the "
            "current ranker, eligibility gate and site recall against labels (--labels).")

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, required=True)
        parser.add_argument("--labels", help="Path to a version 1 label file.")
        parser.add_argument("--export", help="Write a label template to this path.")
        parser.add_argument("--limit", type=int, default=150, help="Candidates to export.")
        parser.add_argument("--json", action="store_true", help="Print the report as JSON.")

    def handle(self, *args, **options):
        campaign = Campaign.objects.filter(pk=options["campaign"]).first()
        if not campaign:
            raise CommandError("Campaign not found.")
        if bool(options["labels"]) == bool(options["export"]):
            raise CommandError("Use exactly one of --labels or --export.")
        if options["export"]:
            template = export_template(campaign, max(1, min(options["limit"], 5000)))
            with open(options["export"], "w", encoding="utf-8") as handle:
                json.dump(template, handle, indent=2)
            self.stdout.write(f"Wrote {len(template['pages'])} unlabeled page(s) to {options['export']}. "
                              "Set each label to reps, team_no_reps or irrelevant; add expected site origins.")
            return
        try:
            pages, sites = load_labels(options["labels"])
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        if not pages and not sites:
            raise CommandError("No labeled pages or sites found.")
        report = evaluate(campaign, pages, sites)
        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2))
            return
        self.stdout.write(f"Labeled pages: {report['pages']} ({report['positives']} with reps)")
        self.stdout.write(f"Ranker average precision: {report['average_precision']}")
        for k, value in report["precision_at"].items():
            self.stdout.write(f"Ranker precision@{k}: {value}")
        gate = report["gate"]
        self.stdout.write(f"Eligibility gate: {gate['passed']} passed; precision {gate['precision']}, "
                          f"recall {gate['recall']}")
        for title, key in (("Rep pages the gate rejects", "missed_reps"), ("Non-rep pages the gate passes", "false_passes")):
            if report[key]:
                self.stdout.write(title + ":")
                for url in report[key]:
                    self.stdout.write(f"  {url}")
        if "sites" in report:
            sites = report["sites"]
            self.stdout.write(f"Site recall: {sites['found']}/{sites['expected']} ({sites['recall']}); "
                              f"by method {sites['by_method']}")
            for origin in sites["missing"]:
                self.stdout.write(f"  missing {origin}")
