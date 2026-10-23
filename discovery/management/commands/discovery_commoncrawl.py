"""Find contact pages on approved sources via the Common Crawl index (index only)."""
from django.core.management.base import BaseCommand, CommandError

from discovery.models import Campaign
from discovery.providers import common_crawl_collection, common_crawl_ready
from discovery.services import common_crawl_specs, register_common_crawl
from leads.services.network import FetchError


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
        started_with_specs = False
        if options["start_if_idle"] and campaign.active and not campaign.runs.filter(status__in=("queued", "running")).exists():
            from django.db import transaction
            from discovery.services import start
            with transaction.atomic():
                before = campaign.urls.count()
                run = start(campaign, priority_specs=specs)
                started_with_specs = True
                started_added = campaign.urls.count() - before
            self.stdout.write(f"Started run #{run.pk} so the index URLs can join it.")
        try:
            added = started_added if started_with_specs else register_common_crawl(campaign, specs)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Processed {len(specs)} index URL(s); {added} newly registered to the campaign. "
                          "Data: Common Crawl (CC BY 4.0).")
