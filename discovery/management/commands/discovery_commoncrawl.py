"""Find contact pages on approved sources via the Common Crawl index (index only)."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from discovery.models import Campaign
from discovery.providers import common_crawl_collection, common_crawl_ready
from discovery.ranking import clean_url
from discovery.services import QUEUE_FULL_RESULT, common_crawl_specs, register_common_crawl, start
from leads.services.network import FetchError


def _spec_urls(specs):
    urls = []
    for item in specs:
        try:
            urls.append(clean_url(item["url"]))
        except (KeyError, TypeError, ValueError, UnicodeError):
            continue
    return urls


class Command(BaseCommand):
    help = ("Query the public Common Crawl index for archived contact/team pages on a campaign's approved "
            "sources and register in-scope matches into its open run through the ordinary gates. "
            "Target sites are not contacted by this command. Requires COMMON_CRAWL_ENABLED=1.")

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, required=True)
        parser.add_argument("--collection", default="", help="Crawl ID such as CC-MAIN-2026-35 (default: newest).")
        parser.add_argument("--dry-run", action="store_true", help="List matches without registering them.")
        parser.add_argument("--start-if-idle", action="store_true",
                            help="If the active campaign has no open run, start its ordinary run first (for schedules).")

    def handle(self, *args, **options):
        if not common_crawl_ready():
            raise CommandError("Common Crawl lookup is disabled. Set COMMON_CRAWL_ENABLED=1.")
        campaign = Campaign.objects.filter(pk=options["campaign"]).first()
        if not campaign:
            raise CommandError("Campaign not found.")
        try:
            collection = options["collection"] or common_crawl_collection()
            specs, counts = common_crawl_specs(campaign, collection)
        except (FetchError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Collection: {collection}")
        for name, kept in counts.items():
            self.stdout.write(f"  {kept:>4} in-scope contact-page URL(s)  {name}")
        if options["dry_run"]:
            for spec in specs:
                self.stdout.write(f"  {spec['url']}")
            self.stdout.write(f"Dry run: {len(specs)} URL(s) not registered.")
            return
        # One campaign lock covers the idle check, run creation, and registration.
        # The collector's own start() takes the same lock, so the two cannot interleave
        # a queue fill. This command does not take the collector lease and does not fetch.
        spec_urls = _spec_urls(specs)
        try:
            with transaction.atomic():
                campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
                before = (set(campaign.urls.filter(url__in=spec_urls).values_list("url", flat=True))
                          if spec_urls else set())
                idle = (options["start_if_idle"] and campaign.active and not
                        campaign.runs.filter(status__in=("queued", "running")).exists())
                if idle:
                    run = start(campaign, priority_specs=specs)
                    self.stdout.write(f"Started run #{run.pk} so the index URLs can join it.")
                else:
                    register_common_crawl(campaign, specs)
                    run = campaign.runs.filter(status__in=("queued", "running")).order_by("-id").first()
                present = set(campaign.urls.filter(url__in=spec_urls).values_list("url", flat=True))
                added = len(present - before)
                approved = campaign.urls.filter(url__in=spec_urls, decision="approved").count()
                queued = run.jobs.filter(kind="page", url__in=spec_urls).count() if run else 0
                deferred = campaign.urls.filter(url__in=spec_urls, last_result=QUEUE_FULL_RESULT).count()
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"Processed {len(specs)} index URL(s); {added} newly registered; "
            f"{approved} approved; {queued} queued; {deferred} deferred. "
            "Data: Common Crawl (CC BY 4.0).")
