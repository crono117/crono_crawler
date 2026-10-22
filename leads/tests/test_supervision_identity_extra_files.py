from unittest.mock import patch
from django.test import SimpleTestCase
from leads.services import supervision


class VerifyIdentityExtraFilesTests(SimpleTestCase):
    def test_previous_not_dict_refuses(self):
        with self.assertRaises(supervision.RecoveryRequired):
            supervision.verify_identity("not-a-dict")

    def test_extra_files_wrong_types_refuse(self):
        # Prepare a minimal valid state and identity, then inject bad extra_files
        state = {'unit': 'fixture.service'}
        base_identity = {
            'checkouts': {},
            'file_hashes': {},
            'working_directory': '/x',
            'entry_paths': [],
            'unit_files': [],
            'environment_files': [],
            'effective_config_hash': 'h',
            'extra_files': []
        }
        # Snapshot/identity are controlled so that equality check only compares structures
        with patch.object(supervision, 'snapshot', return_value={'unit': 'fixture.service',
                                                                 'entrypoints_inspected': True,
                                                                 'definition_reloaded': True,
                                                                 'working_directory': '/x',
                                                                 'unit_files': [],
                                                                 'environment_files': [],
                                                                 'entry_paths': [],
                                                                 'effective_config_hash': 'h'}), \
             patch.object(supervision, 'capture_identity', return_value=base_identity):
            for bad in (5, None, [5], [{}]):
                with self.subTest(bad=bad):
                    record = {**state, 'identity': {**base_identity, 'extra_files': bad}}
                    with self.assertRaises(supervision.RecoveryRequired):
                        supervision.verify_identity(record)

    def test_valid_identity_round_tripped_through_json_still_passes(self):
        import json
        # Minimal identity with a valid extra_files list of strings
        identity = {
            'checkouts': {},
            'file_hashes': {},
            'working_directory': '/x',
            'entry_paths': [],
            'unit_files': [],
            'environment_files': [],
            'effective_config_hash': 'h',
            'extra_files': ['/abs/file1', '/abs/file2']
        }
        state = {'unit': 'fixture.service', 'identity': identity}
        with patch.object(supervision, 'snapshot', return_value={'unit': 'fixture.service',
                                                                 'entrypoints_inspected': True,
                                                                 'definition_reloaded': True,
                                                                 'working_directory': '/x',
                                                                 'unit_files': [],
                                                                 'environment_files': [],
                                                                 'entry_paths': [],
                                                                 'effective_config_hash': 'h'}), \
             patch.object(supervision, 'capture_identity', return_value=identity):
            round_tripped = json.loads(json.dumps(state))
            supervision.verify_identity(round_tripped)

