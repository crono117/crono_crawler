import secrets
from django.core.management.base import BaseCommand, CommandError
from discovery.models import Campaign
from automation.coordination import token_hash
from automation.models import CoordinatorClient


class Command(BaseCommand):
    help = "Create a campaign-scoped coordinator credential. Prints its token once; store it privately."

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, required=True)
        parser.add_argument("--name", required=True)

    def handle(self, *args, **options):
        if not Campaign.objects.filter(pk=options["campaign"]).exists():
            raise CommandError("Campaign not found.")
        token = secrets.token_urlsafe(48)
        client = CoordinatorClient.objects.create(name=options["name"][:120], campaign_id=options["campaign"], token_hash=token_hash(token))
        self.stdout.write(f"Client {client.pk}; bearer token (shown once): {token}")
