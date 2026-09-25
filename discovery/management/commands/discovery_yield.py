"""Read-only discovery harvest-rate report. Makes no network requests and changes nothing."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Q, Sum
from django.utils import timezone

from discovery.yields import OriginYield, canary_pages, page_jobs, safe_origin, window_days


class Command(BaseCommand):
    help = ("Report pages fetched vs. pages that stored validated contacts, grouped by discovery method, "
            "search query and origin. Method/query reflect each URL's latest discovery context.")

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, help="Limit to one campaign ID.")
        parser.add_argument("--days", type=int, help="Window in days (default DISCOVERY_YIELD_WINDOW_DAYS).")
        parser.add_argument("--limit", type=int, default=15, help="Rows per origin section.")

    def handle(self, *args, **options):
        days = max(1, options["days"] or window_days())
        jobs = page_jobs(since=timezone.now() - timedelta(days=days))  # discovery page jobs only
        if options["campaign"]:
            jobs = jobs.filter(run__campaign_id=options["campaign"])
        since = timezone.now() - timedelta(days=days)
        total = self.grouped(jobs, None)
        if not total or not total[0][1].fetched:
            self.stdout.write(f"No completed page jobs in the last {days} day(s).")
            self.canary_section(since, options["campaign"])
            return
        _, stats, new = total[0]
        self.stdout.write(f"Window: last {days} day(s)")
        self.stdout.write(f"Harvest rate: {self.ratio(stats)}; {new} new contact(s)")
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
        self.canary_section(since, options["campaign"])

    @staticmethod
    def grouped(jobs, field):
        aggregates = dict(fetched=Count("url", distinct=True),
                          productive=Count("url", distinct=True, filter=Q(contacts_seen__gt=0)),
                          new=Sum("new_contacts"))
        if field is None:
            row = jobs.aggregate(**aggregates)
            return [("all", OriginYield(row["fetched"] or 0, row["productive"] or 0), row["new"] or 0)]
        rows = jobs.values(field).annotate(**aggregates).order_by(field)
        return [(row[field] or "(none)", OriginYield(row["fetched"], row["productive"]), row["new"] or 0)
                for row in rows]

    @staticmethod
    def ratio(stats, new=None):
        percent = round(100 * stats.productive / stats.fetched) if stats.fetched else 0
        text = f"{stats.productive}/{stats.fetched} pages ({percent}%)"
        return text if new is None else f"{text}, {new} new"

    def section(self, title, rows):
        self.stdout.write("")
        self.stdout.write(title)
        for name, stats, new in rows:
            self.stdout.write(f"  {self.ratio(stats, new):>30}  {name}")

    def canary_section(self, since, campaign):
        pages = canary_pages(since)
        if campaign:
            pages = pages.filter(job__campaign_id=campaign)
        by_origin = {}
        for source_url, url, accepted in pages.values_list("job__source__url", "url",
                                                           "metadata__validation__accepted_records"):
            seen = by_origin.setdefault(safe_origin(source_url) or "(unknown)", {})
            seen[url] = seen.get(url, False) or bool(accepted)
        if by_origin:
            self.stdout.write("")
            self.stdout.write("Setup canaries (released; recipe-validated records, not stored-contact counts)")
            for name, seen in sorted(by_origin.items()):
                stats = OriginYield(len(seen), sum(seen.values()))
                self.stdout.write(f"  {self.ratio(stats):>30}  {name}")
