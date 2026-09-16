from django.core.management.base import BaseCommand
from discovery.models import Campaign
from discovery.services import DEMO_ORIGIN, start
from leads.models import Source


class Command(BaseCommand):
    help = "Queue a fixed fictional discovery campaign; no network requests or real contacts."

    def handle(self, *args, **options):
        source, _ = Source.objects.get_or_create(url=DEMO_ORIGIN + "/", defaults={
            "name": "Fictional discovery directory", "approved": True, "approval_notes": "Fixed offline fixture.",
            "collector": "demo", "company": "Example Payments", "follow_links": False})
        campaign, _ = Campaign.objects.get_or_create(name="Offline discovery demo", defaults={"max_pages": 10})
        campaign.sources.add(source)
        run = start(campaign)
        self.stdout.write(self.style.SUCCESS(f"Discovery demo run #{run.pk} queued. Start the worker and open Discovery. Expected: 3 fictional people and a new vendor URL awaiting review."))
