from django.core.management.base import BaseCommand
from leads.models import Source
from leads.services.worker import enqueue

class Command(BaseCommand):
    help = "Add an explicitly fictional, offline source to exercise the app."
    def handle(self, *args, **kwargs):
        source, created = Source.objects.get_or_create(url="https://demo.clearpay.invalid/team/", defaults={
            "name": "Demo • fictional sales team", "company": "Example Payments (fictional)",
            "collector": "demo", "approved": True, "approval_notes": "Packaged synthetic fixture; no network requests.",
            "max_pages": 1, "max_depth": 0, "follow_links": False, "active": False,
        })
        enqueue(source)
        self.stdout.write("Offline demo queued. Start the worker to populate fictional records.")
