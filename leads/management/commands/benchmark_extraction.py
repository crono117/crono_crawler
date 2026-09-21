import json
import subprocess
from django.core.management.base import BaseCommand, CommandError
from automation.extraction_benchmark import run


class Command(BaseCommand):
    help = 'Compare extraction on saved synthetic train/held-out pages. No requests or database writes.'

    def add_arguments(self, parser):
        parser.add_argument('--autoscraper-python', help='Python in a separate environment with requirements-benchmark.txt.')
        parser.add_argument('--assert-fixtures', action='store_true', help='Fail if the new recipes miss a synthetic expected pair.')

    def handle(self, *args, **options):
        try:
            report = run(options['autoscraper_python'])
        except (OSError, subprocess.SubprocessError) as exc:
            raise CommandError('AutoScraper subprocess failed; install requirements-benchmark.txt in its separate environment.') from exc
        self.stdout.write(json.dumps(report, indent=2))
        if options['assert_fixtures'] and any(
                r['extraction_packs']['missed'] or r['extraction_packs']['false_positive'] for r in report['results']):
            raise CommandError('Synthetic held-out extraction differed from expected person/contact pairs.')
        if options['assert_fixtures'] and options['autoscraper_python']:
            for result in report['results']:
                auto = result['autoscraper']
                if auto['training_supported'] and any(auto[key]['missed'] or auto[key]['false_positive']
                        for key in ('training_replay', 'unchanged_layout_control')):
                    raise CommandError('AutoScraper training/stable-layout control failed; check its isolated dependencies.')
