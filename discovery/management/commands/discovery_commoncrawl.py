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
        try:
            added = register_common_crawl(campaign, specs)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Registered {len(specs)} URL(s); {added} new to this campaign. "
                          "Data: Common Crawl (CC BY 4.0).")
