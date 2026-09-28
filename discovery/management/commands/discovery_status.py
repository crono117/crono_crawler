import json

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from discovery.acquisition import search_status
from discovery.models import Campaign


class Command(BaseCommand):
    help = "Read-only source acquisition status, quotas and latest outcomes. No credentials or network requests."

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, required=True)

    def handle(self, *args, **options):
        try:
            campaign = Campaign.objects.get(pk=options["campaign"])
        except Campaign.DoesNotExist as exc:
            raise CommandError("Campaign does not exist.") from exc
        self.stdout.write(json.dumps(search_status(campaign), cls=DjangoJSONEncoder, indent=2))
