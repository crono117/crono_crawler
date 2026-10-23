"""Re-queue failed setup jobs whose only problem was a same-site canonical redirect.

Dry run unless --apply. Selects jobs in state "failed" whose recorded redirect destination
classifies as "canonical" (automation.canonical.classify) and that have not been rebound yet.
Applying calls start_setup(), exactly like the operator's resume button: a new generation under the
current policy snapshot, starting from the originally authorized URL. The worker then meets the
redirect again and rebinds through the same guarded path. No source, approval or policy is
edited here, and no daily new-site allowance is consumed (no new job is created).
"""
import json

from django.core.management.base import BaseCommand, CommandError

from automation.canonical import classify, redirect_target
from automation.models import SiteAutomationJob, SitePolicy
from automation.services import start_setup
from discovery.models import Campaign


class Command(BaseCommand):
    help = "Re-queue failed automatic setups blocked only by a same-site canonical redirect."

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, required=True)
        parser.add_argument("--limit", type=int, default=25)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            campaign = Campaign.objects.get(pk=options["campaign"])
        except Campaign.DoesNotExist as exc:
            raise CommandError("Campaign does not exist.") from exc
        if not SitePolicy.objects.filter(campaign=campaign, enabled=True).exists() or not campaign.active:
            raise CommandError("The campaign must be active with an enabled automation policy.")
        eligible = []
        for job in (SiteAutomationJob.objects.filter(campaign=campaign, state="failed", canonical_rebinds=0)
                    .select_related("source").prefetch_related("pages").order_by("id")):
            source = job.source
            if source.approval_kind != "policy" or not source.approved:
                continue
            messages = [job.message] + [page.message for page in job.pages.all() if page.generation == job.generation]
            target = next((t for t in map(redirect_target, messages) if t), "")
            if classify(source.url, target, allowed_paths=source.allowed_paths,
                        allow_homepage=source.allow_homepage) == "canonical":
                eligible.append((job, target))
        eligible = eligible[:max(0, options["limit"])]
        requeued = []
        if options["apply"]:
            for job, _ in eligible:
                job = start_setup(job.source, campaign)
                requeued.append(job.pk)
        self.stdout.write(json.dumps({
            "campaign": campaign.pk, "eligible": [{"job_id": job.pk, "source_id": job.source_id,
                                                   "authorized_url": job.source.url, "redirect_to": target}
                                                  for job, target in eligible],
            "applied": bool(options["apply"]), "requeued": requeued}, indent=2))
