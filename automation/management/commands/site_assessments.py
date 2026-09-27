from collections import Counter

from django.core.management.base import BaseCommand

from automation.assessments import record, record_failure
from automation.models import SiteAssessment, SiteAutomationJob
from automation.private_pages import read_html
from automation.recipes import evaluate, proposals


class Command(BaseCommand):
    help = ("Report why probed sites did or didn't yield contacts. --backfill first records "
            "assessments for existing jobs (no network requests).")

    def add_arguments(self, parser):
        parser.add_argument("--backfill", action="store_true")
        parser.add_argument("--refresh", action="store_true",
                            help="With --backfill, recompute assessments that already exist for the current generation.")
        parser.add_argument("--campaign", type=int)
        parser.add_argument("--limit", type=int, default=25)

    def handle(self, *args, **options):
        jobs = SiteAutomationJob.objects.select_related("source")
        if options["campaign"]:
            jobs = jobs.filter(campaign_id=options["campaign"])
        if options["backfill"]:
            created = sum(bool(self.backfill(job, options["refresh"])) for job in jobs)
            self.stdout.write(f"Backfilled {created} assessment(s).")
        assessments = SiteAssessment.objects.all()
        if options["campaign"]:
            assessments = assessments.filter(job__campaign_id=options["campaign"])
        if not assessments.exists():
            self.stdout.write("No site assessments recorded yet.")
            return
        outcomes, reasons, future = Counter(), Counter(), Counter()
        for item in assessments:
            outcomes[item.outcome] += 1
            reasons.update(item.reasons)
            future.update(item.future_options)
        for title, counter in (("Outcomes", outcomes), ("Reasons", reasons), ("Future options", future)):
            self.stdout.write(f"\n{title}:")
            for key, count in counter.most_common():
                self.stdout.write(f"  {count:4d}  {key}")
        self.stdout.write(f"\nMost recent {options['limit']}:")
        for item in assessments[:options["limit"]]:
            self.stdout.write(f"  [{item.outcome}] {item.origin} (gen {item.generation})\n      {item.summary}")

    def backfill(self, job, refresh=False):
        if job.assessments.filter(generation=job.generation).exists():
            if not refresh:
                return None
            job.assessments.filter(generation=job.generation).delete()
        pages = [page for page in job.pages.filter(generation=job.generation, phase="probe", status="done")
                 if page.metadata.get("url")]
        metadata = {page.metadata["url"]: page.metadata for page in pages}
        versions = list(job.recipes.filter(generation=job.generation))
        assessment = None
        if metadata or versions:
            samples = []
            for page in pages:
                try:
                    samples.append((page.metadata["url"], read_html(page)))
                except ValueError:
                    pass
            if samples:
                # Private HTML still exists: evaluate every variant, exactly as live setup does.
                recipes = [v.recipe for v in versions] + proposals([html for _, html in samples])
                stats = [{url: {**evaluate(html, job.source, recipe)[2], "row": recipe.get("row", recipe.get("engine", ""))}
                          for url, html in samples} for recipe in recipes[:65]]
            else:
                stats = [{url: {**page_stats, "row": v.recipe.get("row", v.recipe.get("engine", ""))}
                          for url, page_stats in v.validation.get("pages", {}).items()} for v in versions]
            assessment = record(job, stats, metadata)
        if job.state in ("paused", "failed") and job.message:
            assessment = record_failure(job, job.message) or assessment
        return assessment
