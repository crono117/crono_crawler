from django.core.management.base import BaseCommand
from django.db import transaction
from automation.fixtures import DEMO_ORIGIN
from automation.models import SitePolicy
from automation.services import start_setup
from discovery.models import Campaign
from leads.models import Source


class Command(BaseCommand):
    help = "Queue a fixed offline site-automation demo with two fictional people and no network access."

    @transaction.atomic
    def handle(self, *args, **options):
        campaign, _ = Campaign.objects.get_or_create(name="Offline automation demo", defaults={"active": True, "use_sitemaps": False})
        SitePolicy.objects.get_or_create(campaign=campaign, defaults={"enabled": True, "probe_pages": 1, "canary_pages": 1})
        source, _ = Source.objects.get_or_create(url=DEMO_ORIGIN + "/team/", defaults={"name": "Fictional automatic setup",
            "approved": True, "approval_kind": "policy", "approval_notes": "Fixed offline demo; no real site authorization.",
            "setup_mode": "automatic", "collector": "demo", "allowed_paths": "/team/"})
        campaign.sources.add(source)
        campaign.active = True
        campaign.save(update_fields=["active"])
        job = start_setup(source, campaign)
        self.stdout.write(f"Offline setup #{job.pk} queued. One existing worker will produce Robin Autonomy and Morgan Pipeline after validation and a canary.")
