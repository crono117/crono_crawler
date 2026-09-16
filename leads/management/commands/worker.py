import logging
import signal
import threading
from django.core.management.base import BaseCommand
from leads.services.worker import acquire_lease, release_lease, tick

class Command(BaseCommand):
    help = "Run the single persistent scheduler/collector. Restarts resume unfinished pages."
    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Perform a single queue tick and exit.")
    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        token = None
        try:
            while not token and not stop.is_set():
                token = acquire_lease()
                if not token:
                    self.stdout.write("Another worker holds the lease; waiting for it to stop or expire.")
                    if options["once"]:
                        return
                    stop.wait(15)
            if not token:
                return
            self.stdout.write(self.style.SUCCESS("Collector running. Ctrl+C stops it safely."))
            prefer_discovery = False
            while not stop.is_set():
                worked = tick(token, prefer_discovery=prefer_discovery)
                prefer_discovery = not prefer_discovery
                if options["once"]:
                    break
                if not worked:
                    stop.wait(2)
        finally:
            if token:
                release_lease(token)
