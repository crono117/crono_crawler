import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from discovery.importing import MAX_BATCH, MAX_BYTES, import_metadata, parse_metadata
from discovery.models import Campaign


class Command(BaseCommand):
    help = 'Preview/import up to 10 URL metadata rows for manual review; no DNS, fetching or approvals.'

    def add_arguments(self, parser):
        parser.add_argument('--campaign', type=int, required=True)
        parser.add_argument('--file', required=True, help='UTF-8 JSON array: url, optional label/context.')
        parser.add_argument('--limit', type=int, default=MAX_BATCH)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--dry-run', action='store_true', help='Preview only (default).')
        mode.add_argument('--apply', action='store_true', help='Save pending metadata requiring manual review.')

    def handle(self, *args, **options):
        try:
            with Path(options['file']).open('rb') as stream:
                rows = parse_metadata(stream.read(MAX_BYTES + 1), options['limit'])
            result = import_metadata(options['campaign'], rows, apply=options['apply'])
        except Campaign.DoesNotExist:
            raise CommandError('Campaign does not exist; select an explicit existing campaign.') from None
        except OSError:
            raise CommandError('Cannot read the local metadata file.') from None
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps(result, indent=2))
