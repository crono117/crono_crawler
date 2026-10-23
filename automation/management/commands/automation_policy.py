"""Show or change a campaign's automation *admission* settings. Dry run unless --apply.

Only require_page_intent, min_url_score, max_sites_per_day and explore_share can be changed here. They decide
which new sites enter setup; they do not bound an admitted setup (see policy.SETUP_FIELDS), so
changing them leaves existing jobs, recipes and active sources alone. Setup-bounding fields such
as min_recipe_score stay a deliberate change in the policy form, which pauses jobs for review.
Values are validated against the model's bounds; nothing else in the policy is saved.
"""
import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from automation.models import SitePolicy
from automation.policy import ADMISSION_FIELDS, SETUP_FIELDS


class Command(BaseCommand):
    help = "Show or change automation admission settings without pausing admitted setups."

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, required=True)
        parser.add_argument("--require-page-intent", choices=("on", "off"))
        parser.add_argument("--min-url-score", type=int)
        parser.add_argument("--max-sites-per-day", type=int)
        parser.add_argument("--explore-share", type=int,
                            help="Percent of the daily setup allowance open to pages without direct team/person "
                                 "intent (0-100). 100 = first eligible wins. Preview with automation_intake.")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        policy = SitePolicy.objects.filter(campaign_id=options["campaign"]).first()
        if not policy:
            raise CommandError("This campaign has no automation policy.")
        before = {name: getattr(policy, name) for name in ADMISSION_FIELDS + SETUP_FIELDS}
        changes = {}
        if options["require_page_intent"]:
            changes["require_page_intent"] = options["require_page_intent"] == "on"
        if options["min_url_score"] is not None:
            changes["min_url_score"] = options["min_url_score"]
        if options["max_sites_per_day"] is not None:
            changes["max_sites_per_day"] = options["max_sites_per_day"]
        if options["explore_share"] is not None:
            changes["explore_share"] = options["explore_share"]
        for name, value in changes.items():
            setattr(policy, name, value)
        try:
            for name in changes:
                field = SitePolicy._meta.get_field(name)
                field.clean(getattr(policy, name), policy)
        except ValidationError as exc:
            raise CommandError(f"{name}: {'; '.join(exc.messages)}") from exc
        result = {"campaign": options["campaign"], "before": {k: before[k] for k in ADMISSION_FIELDS},
                  "requested": changes, "applied": False,
                  "setup_fields_unchanged": {k: before[k] for k in SETUP_FIELDS}}
        if changes and options["apply"]:
            SitePolicy.objects.filter(pk=policy.pk).update(**changes, updated_at=timezone.now())
            policy.refresh_from_db()
            result["applied"] = True
            result["after"] = {k: getattr(policy, k) for k in ADMISSION_FIELDS}
        self.stdout.write(json.dumps(result, indent=2))
