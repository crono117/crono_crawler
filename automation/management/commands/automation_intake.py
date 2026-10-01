"""Read-only preview of ranked automatic-setup admission. Never creates a job, source or setting.

Shows the pending pool by intake tier, today's slots, and which sites the ranked allocator would
admit right now (with every rank adjustment spelled out), optionally under a proposed
``--explore-share``. Use it to size the share from live data before running
``automation_policy --explore-share N --apply``. Makes no network requests.
"""
import json

from django.core.management.base import BaseCommand, CommandError

from automation.intake import plan
from automation.learning import admission_funnel
from automation.models import SitePolicy


class Command(BaseCommand):
    help = "Preview which pending sites ranked intake would admit, without changing anything."

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, required=True)
        parser.add_argument("--explore-share", type=int, help="Preview this share instead of the saved one (0-100).")
        parser.add_argument("--limit", type=int, default=30, help="Rows to list per section.")

    def handle(self, *args, **options):
        policy = SitePolicy.objects.select_related("campaign").filter(campaign_id=options["campaign"]).first()
        if not policy:
            raise CommandError("This campaign has no automation policy.")
        share = options["explore_share"]
        if share is not None and not 0 <= share <= 100:
            raise CommandError("--explore-share must be between 0 and 100.")
        result = {"policy_enabled": policy.enabled, "saved_explore_share": policy.explore_share,
                  "require_page_intent": policy.require_page_intent, "preview": plan(policy, explore_share=share, limit=options["limit"]),
                  "admission_funnel": admission_funnel(policy.campaign)}
        self.stdout.write(json.dumps(result, indent=2))
