"""Read-only discovery harvest-rate report. Makes no network requests and changes nothing."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Q
from django.utils import timezone

from discovery.yields import OriginYield, page_jobs, window_days


class Command(BaseCommand):
    help = ("Report pages fetched vs. pages that stored validated contacts, grouped by discovery method, "
            "search query and origin. Method/query reflect each URL's latest discovery context.")

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, help="Limit to one campaign ID.")
        parser.add_argument("--days", type=int, help="Window in days (default DISCOVERY_YIELD_WINDOW_DAYS).")
        parser.add_argument("--limit", type=int, default=15, help="Rows per origin section.")

    def handle(self, *args, **options):
        days = max(1, options["days"] or window_days())
        jobs = page_jobs(since=timezone.now() - timedelta(days=days))
        if options["campaign"]:
            jobs = jobs.filter(run__campaign_id=options["campaign"])
        total = self.grouped(jobs, None)
        if not total or not total[0][1].fetched:
            self.stdout.write(f"No completed page jobs in the last {days} day(s).")
            return
        stats = total[0][1]
        self.stdout.write(f"Window: last {days} day(s)")
        self.stdout.write(f"Harvest rate: {self.ratio(stats)}")
        self.section("By discovery method", self.grouped(jobs, "candidate__method"))
        searches = self.grouped(jobs.filter(candidate__method="search"), "candidate__search_query")
        if searches:
            self.section("By search query", searches)
        origins = self.grouped(jobs, "candidate__origin")
        limit = max(1, options["limit"])
        self.section("Top origins", sorted(origins, key=lambda row: (-row[1].productive, -row[1].fetched))[:limit])
        empty = [row for row in origins if row[1].productive == 0]
        if empty:
            self.section("Origins with no validated contacts",
                         sorted(empty, key=lambda row: -row[1].fetched)[:limit])

    @staticmethod
    def grouped(jobs, field):
        aggregates = dict(fetched=Count("url", distinct=True),
                          productive=Count("url", distinct=True, filter=Q(contacts_seen__gt=0)))
        if field is None:
            row = jobs.aggregate(**aggregates)
            return [("all", OriginYield(row["fetched"] or 0, row["productive"] or 0))]
        rows = jobs.values(field).annotate(**aggregates).order_by(field)
        return [(row[field] or "(none)", OriginYield(row["fetched"], row["productive"])) for row in rows]

    @staticmethod
    def ratio(stats):
        percent = round(100 * stats.productive / stats.fetched) if stats.fetched else 0
        return f"{stats.productive}/{stats.fetched} pages ({percent}%)"

    def section(self, title, rows):
        self.stdout.write("")
        self.stdout.write(title)
        for name, stats in rows:
            self.stdout.write(f"  {self.ratio(stats):>22}  {name}")
