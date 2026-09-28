from django.core.management.base import BaseCommand
from django.db import transaction

from discovery.models import DiscoveredURL
from discovery.ranking import clean_url, noise_reason
from leads.models import SourceCandidate


class Command(BaseCommand):
    help = ("Apply the current link filters to already-queued discovery items: page chrome is dismissed "
            "with its reason kept, and invalid URLs (e.g. single-label hosts) lose any policy approval. "
            "Dry run unless --apply. Makes no network requests.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        apply = options["apply"]
        dismissed = candidates = invalid = 0
        with transaction.atomic():
            for item in DiscoveredURL.objects.filter(decision="pending", source__isnull=True):
                reason = noise_reason(item.url)
                if reason:
                    dismissed += 1
                    self.stdout.write(f"  dismiss URL #{item.pk} ({reason}): {item.url[:100]}")
                    if apply:
                        item.decision = "dismissed"
                        item.reasons = list(item.reasons) + [f"Filtered automatically: {reason}"]
                        item.save(update_fields=["decision", "reasons"])
            for item in SourceCandidate.objects.filter(status="new"):
                reason = noise_reason(item.url)
                if reason:
                    candidates += 1
                    self.stdout.write(f"  dismiss collector link #{item.pk} ({reason}): {item.url[:100]}")
                    if apply:
                        item.status = "dismissed"
                        item.save(update_fields=["status"])
            for item in DiscoveredURL.objects.exclude(decision="dismissed").select_related("source"):
                try:
                    clean_url(item.url)
                    continue
                except (ValueError, UnicodeError) as exc:
                    reason = str(exc)
                invalid += 1
                self.stdout.write(f"  invalid URL #{item.pk} ({reason}): {item.url[:100]}")
                if apply:
                    item.decision = "dismissed"
                    item.reasons = list(item.reasons) + [f"Filtered automatically: {reason}"]
                    item.save(update_fields=["decision", "reasons"])
                    self._withdraw_policy_source(item, reason)
        verb = "Dismissed" if apply else "Would dismiss"
        self.stdout.write(f"{verb} {dismissed} discovered URL(s), {candidates} collector link(s) and "
                          f"{invalid} invalid URL(s).{'' if apply else ' Re-run with --apply.'}")

    def _withdraw_policy_source(self, item, reason):
        """A policy approval of an invalid URL is withdrawn; operator-approved sources are left alone."""
        source = item.source
        if not source or source.approval_kind != "policy":
            return
        from automation.models import SiteAutomationJob
        from automation.services import pause_job
        source.approved = source.active = False
        source.last_error = f"Policy approval withdrawn: {reason}"
        source.save(update_fields=["approved", "active", "last_error"])
        job = SiteAutomationJob.objects.filter(source=source).first()
        if job and job.state not in ("failed",):
            pause_job(job, f"Policy approval withdrawn: {reason}", failed=True)
        self.stdout.write(f"    withdrew policy approval of source #{source.pk}")
