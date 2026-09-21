#!/usr/bin/env python3
"""Run only the offline third-party comparison in its own dependency environment."""
import json
from pathlib import Path
from unittest.mock import patch
from autoscraper import AutoScraper

ROOT = Path(__file__).resolve().parents[1] / 'examples' / 'extraction'


def measure(values, expected):
    actual = {str(value).casefold() for value in values}
    return {'correct': len(actual & expected), 'false_positive': len(actual - expected),
            'missed': len(expected - actual), 'expected': len(expected)}


def main():
    results = {}
    with patch('socket.socket.connect', side_effect=AssertionError('Offline benchmark attempted network I/O')), \
         patch('requests.sessions.Session.request', side_effect=AssertionError('Offline benchmark attempted HTTP')):
        for case in json.loads((ROOT / 'manifest.json').read_text()):
            model = AutoScraper()
            train = (ROOT / case['train']).read_text()
            wanted = case['training_names'] + case['training_emails']
            model.build(html=train, wanted_list=wanted)
            expected = {row[key].casefold() for row in case['expected'] for key in ('name', 'email')}
            result = measure(model.get_result_similar(html=(ROOT / case['heldout']).read_text()), expected)
            result.update(unit='unpaired field values', person_contact_association_validated=False,
                training_supported=case['name'] != 'jsonld',
                training_replay=measure(model.get_result_similar(html=train), {value.casefold() for value in wanted}),
                unchanged_layout_control=measure(model.get_result_similar(html=(ROOT / case['stable']).read_text()), expected))
            results[case['name']] = result
    print(json.dumps(results))


if __name__ == '__main__':
    main()
