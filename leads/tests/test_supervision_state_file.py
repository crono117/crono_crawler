"""Exercise the CLI's durable state path; all service/script execution is mocked."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase

from leads.services import supervision
from leads.tests.test_guard_review import FakeUnit
from scripts import guard_service_update


class GuardStateFileTests(SimpleTestCase):
    def run_failed_update(self, corrupt=None, *, remove=False):
        fake = FakeUnit()
        output = io.StringIO()
        error = None
        with tempfile.TemporaryDirectory(prefix='guard-state-') as temp:
            root = Path(temp)
            state_file = root / 'state.json'
            change, rollback = root / 'change', root / 'rollback'
            for script in (change, rollback):
                script.write_text('#!/bin/sh\nexit 0\n')
                script.chmod(0o700)

            def run_script(argv, **kwargs):
                self.assertEqual(fake.active, 'inactive')
                if argv == [str(change)]:
                    # The real write_state has persisted the pre-update fingerprint.
                    self.assertEqual(json.loads(state_file.read_text())['identity'], {'code': 'old'})
                    if remove:
                        state_file.unlink()
                    elif corrupt is not None:
                        state_file.write_bytes(corrupt)
                    raise subprocess.CalledProcessError(1, argv)
                self.assertEqual(argv, [str(rollback)])
                fake.events.append('rollback')
                return subprocess.CompletedProcess(argv, 0)

            argv = ['guard_service_update.py', '--unit', 'fixture.service',
                    '--state-file', str(state_file), '--change-script', str(change),
                    '--rollback-script', str(rollback)]
            with patch('sys.argv', argv), redirect_stdout(output), \
                 patch.object(supervision, 'snapshot', side_effect=fake.snapshot), \
                 patch.object(supervision, 'systemctl', side_effect=fake.ctl), \
                 patch.object(supervision, 'capture_identity', return_value={'code': 'old'}), \
                 patch.object(supervision, 'cgroup_empty', return_value=True), \
                 patch.object(guard_service_update.subprocess, 'run', side_effect=run_script):
                try:
                    guard_service_update.main()
                except Exception as exc:
                    error = exc
        return fake, error, output.getvalue()

    def assert_refused(self, payload=None, *, remove=False):
        fake, error, output = self.run_failed_update(payload, remove=remove)
        self.assertEqual(fake.active, 'inactive', 'unreadable rollback record allowed restart')
        self.assertNotIn('start', fake.events)
        self.assertEqual(fake.enabled, 'enabled')
        self.assertIsInstance(error, supervision.RecoveryRequired)
        self.assertEqual(output, '')

    def test_truncated_json_refuses_restart(self):
        self.assert_refused(b'{"unit": "fixture.service", "identity":')

    def test_non_json_bytes_refuse_restart(self):
        self.assert_refused(b'\xff\xfe\x00not json')

    def test_empty_file_refuses_restart(self):
        self.assert_refused(b'')

    def test_missing_file_refuses_restart(self):
        self.assert_refused(remove=True)

    def test_parseable_but_changed_record_refuses_restart(self):
        for payload in (b'null', b'[]', b'{}', b'{"unit": "other.service"}'):
            with self.subTest(payload=payload):
                self.assert_refused(payload)

    def test_intact_record_restores_service_and_propagates_deployment_failure(self):
        fake, error, output = self.run_failed_update()
        self.assertIsInstance(error, subprocess.CalledProcessError)
        self.assertEqual((fake.active, fake.enabled), ('active', 'enabled'))
        self.assertEqual(fake.events.count('start'), 1)
        self.assertIn('rollback', fake.events)
        self.assertEqual(output, '')
