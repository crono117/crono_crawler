"""Small synthetic held-out comparison, deliberately offline and not a yield estimate."""
import json
from pathlib import Path
import subprocess
from unittest.mock import patch
from django.conf import settings
from django.test import override_settings
from leads.models import Source
from .recipes import evaluate, proposals, readiness


def pairs(records):
    return {(row['name'].casefold(), row['email'].casefold()) for row in records}


def measure(actual, expected):
    return {'correct': len(actual & expected), 'false_positive': len(actual - expected),
            'missed': len(expected - actual), 'expected': len(expected)}


def run(autoscraper_python=None):
    root = Path(settings.BASE_DIR) / 'examples' / 'extraction'
    source = Source(company='Example Payments')
    results = []
    # Any accidental library fetch must fail, even with credentials in the environment.
    with patch('socket.socket.connect', side_effect=AssertionError('Offline benchmark attempted network I/O')), \
         patch('requests.sessions.Session.request', side_effect=AssertionError('Offline benchmark attempted HTTP')):
        for case in json.loads((root / 'manifest.json').read_text()):
            train, heldout = [(root / case[key]).read_text() for key in ('train', 'heldout')]
            expected = pairs(case['expected'])
            result = {'case': case['name']}
            for enabled, name in ((False, 'baseline'), (True, 'extraction_packs')):
                with override_settings(EXTRACTION_PACKS_ENABLED=enabled):
                    # Choose using training HTML only. Never select a recipe on the held-out answers.
                    options = [(recipe, evaluate(train, source, recipe)[2]) for recipe in proposals([train])]
                    recipe, stats = max(options, key=lambda item: (readiness([item[1]]), item[1]['accepted_records']))
                    records = evaluate(heldout, source, recipe)[0] if readiness([stats]) >= 85 else []
                    result[name] = measure(pairs(records), expected) | {'recipe': recipe}
            results.append(result)
    if autoscraper_python:
        # Preserve the venv interpreter path: resolving its symlink loses the venv.
        completed = subprocess.run([str(Path(autoscraper_python).absolute()),
            str(Path(settings.BASE_DIR) / 'scripts' / 'benchmark_autoscraper.py')],
            check=True, capture_output=True, text=True, timeout=30)
        comparison = json.loads(completed.stdout)
        for result in results:
            result['autoscraper'] = comparison[result['case']]
    return {'dataset': 'Five synthetic train/held-out layout pairs; not representative web coverage.',
            'network': 'Socket connections and requests HTTP calls blocked during comparison.',
            'results': results}
