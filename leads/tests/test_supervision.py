import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.test import SimpleTestCase
from leads.services import supervision
from .test_guard_review import FakeUnit


class SupervisionTests(SimpleTestCase):
    def guard(self, fake, change, rollback, record=lambda s: None):
        with patch.object(supervision, 'snapshot', side_effect=fake.snapshot), \
             patch.object(supervision, 'systemctl', side_effect=fake.ctl), \
             patch.object(supervision, 'capture_identity', side_effect=lambda *a, **k: {'code': fake.code}), \
             patch.object(supervision, 'cgroup_empty', return_value=True):
            supervision.guarded_update('fixture.service', change, rollback, record)

    def test_failed_deployment_records_and_restores_all_enabled_active_combinations(self):
        for enabled in ('enabled', 'disabled'):
            for active in ('active', 'inactive'):
                with self.subTest(enabled=enabled, active=active):
                    fake = FakeUnit()
                    fake.enabled, fake.active = enabled, active
                    def change():
                        fake.code = 'new'
                        raise ValueError('deployment failed')
                    def rollback():
                        self.assertEqual(fake.active, 'inactive')
                        fake.code = 'old'
                    with self.assertRaisesRegex(ValueError, 'deployment failed'):
                        self.guard(fake, change, rollback, lambda s: fake.events.append('record'))
                    self.assertEqual(fake.events[0], 'record')
                    self.assertEqual((fake.enabled, fake.active, fake.code), (enabled, active, 'old'))

    def test_failed_preflight_cannot_stop_service(self):
        fake = FakeUnit()
        with self.assertRaises(OSError):
            self.guard(fake, lambda: None, lambda: None,
                       lambda _: (_ for _ in ()).throw(OSError('disk full')))
        self.assertEqual(fake.events, [])

    def test_rollback_failure_restores_enablement_but_never_restarts_unverified_code(self):
        fake = FakeUnit()
        with self.assertRaisesRegex(RuntimeError, 'rollback failed'):
            self.guard(fake, lambda: (_ for _ in ()).throw(ValueError()),
                       lambda: (_ for _ in ()).throw(RuntimeError('rollback failed')))
        self.assertEqual((fake.enabled, fake.active), ('enabled', 'inactive'))
        self.assertNotIn('start', fake.events)

    def test_mismatched_readback_fails_loudly(self):
        fake = FakeUnit()
        state = fake.snapshot('fixture.service')
        fake.active = 'inactive'
        fake.fail_after_start = True
        with patch.object(supervision, 'snapshot', side_effect=fake.snapshot), \
             patch.object(supervision, 'systemctl', side_effect=fake.ctl), \
             patch.object(supervision, 'cgroup_empty', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'restoration failed'):
                supervision.restore(state)

    def test_success_preserves_existing_disabled_stopped_state(self):
        fake = FakeUnit()
        fake.active, fake.enabled = 'inactive', 'disabled'
        self.guard(fake, lambda: None, lambda: self.fail('unexpected rollback'))
        self.assertEqual((fake.enabled, fake.active), ('disabled', 'inactive'))
        self.assertNotIn('start', fake.events)


class IdentityInspectionTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='guard-identity-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'checkout'
        self.repo.mkdir()
        self.script = self.repo / 'run-local.py'
        self.script.write_text('print("fixture")\n')
        (self.repo / '.gitignore').write_text('.venv/\n.env\n')
        for args in [('init',), ('add', '.'), ('-c','user.name=Fixture','-c','user.email=fixture@example.test', 'commit','-m','fixture')]:
            subprocess.run(['git', *args], cwd=self.repo, check=True, capture_output=True)
        self.python = self.repo / '.venv/bin/python'
        self.python.parent.mkdir(parents=True)
        self.python.write_text('interpreter-fixture')
        self.config = self.repo / '.env'
        self.config.write_text('SYNTHETIC_KEY=private-fixture-value')
        self.unit = self.root / 'fixture.service'
        self.unit.write_text('[Service]\nKillMode=control-group\n')
        self.state = {'unit': 'fixture.service', 'working_directory': str(self.repo),
            'active_state': 'inactive', 'enabled_state': 'enabled',
            'entry_checkouts': [str(self.repo)], 'entry_paths': [str(self.python), str(self.script)],
            'unit_files': [str(self.unit)], 'environment_files': [str(self.config)],
            'entrypoints_inspected': True, 'definition_reloaded': True, 'effective_config_hash': 'fixture-config-hash'}

    def recorded(self):
        return {**self.state, 'identity': supervision.capture_identity(self.state)}

    def test_identity_records_revisions_and_hashes_without_private_values(self):
        record = self.recorded()
        self.assertEqual(len(record['identity']['checkouts'][str(self.repo)]['revision']), 40)
        self.assertNotIn('private-fixture-value', json.dumps(record))
        self.assertIn(str(self.unit), record['identity']['file_hashes'])
        with patch.object(supervision, 'snapshot', return_value=self.state):
            supervision.verify_identity(record)

    def test_unit_config_and_interpreter_changes_prevent_rollback_restart(self):
        for path in (self.unit, self.config, self.python):
            with self.subTest(path=path.name):
                old = path.read_bytes()
                record = self.recorded()
                path.write_bytes(old + b'\nchanged')
                with patch.object(supervision, 'snapshot', return_value=self.state):
                    with self.assertRaisesRegex(RuntimeError, 'identity'):
                        supervision.verify_identity(record)
                path.write_bytes(old)

    def test_changed_revision_and_dirty_checkout_fail_identity(self):
        record = self.recorded()
        self.script.write_text('print("new fixture")\n')
        with patch.object(supervision, 'snapshot', return_value=self.state):
            self.assertEqual(supervision.status('fixture.service', supervision.revision(str(self.repo)))['checkout_status'], 'dirty_checkout')
            with self.assertRaises(RuntimeError):
                supervision.verify_identity(record)
        subprocess.run(['git','add','.'], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','-m','new'], cwd=self.repo, check=True, capture_output=True)
        with patch.object(supervision, 'snapshot', return_value=self.state):
            with self.assertRaisesRegex(RuntimeError, 'identity'):
                supervision.verify_identity(record)
            self.assertEqual(supervision.status('fixture.service', record['identity']['checkouts'][str(self.repo)]['revision'])['checkout_status'], 'stale_checkout')

    def test_wrapper_and_unloaded_definition_never_pass_identity(self):
        for state in ({**self.state, 'entrypoints_inspected': False}, {**self.state, 'definition_reloaded': False}):
            with self.assertRaises(RuntimeError):
                supervision.capture_identity(state)
        with patch.object(supervision, 'snapshot', return_value={**self.state, 'entrypoints_inspected': False}):
            self.assertEqual(supervision.status('fixture.service', 'expected')['checkout_status'], 'uninspected_checkout')

    def test_execstart_mismatch_and_wrapper_are_not_current(self):
        for executable, script in [('/bin/sh', '/some-wrapper'), (str(self.python), '/old/run-local.py')]:
            output = f'''LoadState=loaded
ActiveState=inactive
UnitFileState=enabled
WorkingDirectory={self.repo}
MainPID=0
ControlPID=0
ExecStart={{ path={executable} ; argv[]={executable} {script} ; ignore_errors=no ; }}
FragmentPath={self.unit}
NeedDaemonReload=no
'''
            with patch.object(supervision, 'systemctl', return_value=output):
                self.assertFalse(supervision.snapshot('fixture.service')['entrypoints_inspected'])

    def test_direct_entry_and_effective_unit_config_are_inspected_without_leaking_argv(self):
        output = f'''LoadState=loaded
ActiveState=inactive
UnitFileState=enabled
WorkingDirectory={self.repo}
MainPID=0
ControlPID=0
KillMode=control-group
ControlGroup=
ExecStart={{ path={self.python} ; argv[]={self.python} {self.script} --token private-fixture-value ; ignore_errors=no ; pid=0 ; }}
Environment=SECRET=private-fixture-value
FragmentPath={self.unit}
DropInPaths=
EnvironmentFiles={self.config} (ignore_errors=no)
NeedDaemonReload=no
'''
        with patch.object(supervision, 'systemctl', return_value=output):
            state = supervision.snapshot('fixture.service')
            self.assertTrue(state['entrypoints_inspected'])
            identity = supervision.capture_identity(state)
        self.assertNotIn('private-fixture-value', json.dumps(state))
        self.assertNotIn('private-fixture-value', json.dumps(identity))
        changed = output.replace('SECRET=private-fixture-value', 'SECRET=changed-fixture')
        with patch.object(supervision, 'systemctl', return_value=changed):
            with self.assertRaisesRegex(RuntimeError, 'identity'):
                supervision.verify_identity({**state, 'identity': identity})


class StateFileDurabilityTests(SimpleTestCase):
    def test_file_and_directory_are_fsynced_before_record_returns(self):
        import os
        import stat
        from scripts.guard_service_update import write_state
        calls = []
        real = os.fsync
        def sync(fd):
            calls.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
            return real(fd)
        with tempfile.TemporaryDirectory() as temp, patch('scripts.guard_service_update.os.fsync', side_effect=sync):
            path = Path(temp) / 'state.json'
            write_state(path, {'unit': 'fixture.service'})
            self.assertEqual(json.loads(path.read_text()), {'unit': 'fixture.service'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(calls, ['file', 'directory'])

    def test_existing_state_file_cannot_be_overwritten(self):
        from scripts.guard_service_update import write_state
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.json'
            write_state(path, {'old': True})
            with self.assertRaises(FileExistsError):
                write_state(path, {'new': True})
            self.assertEqual(json.loads(path.read_text()), {'old': True})

    def test_directory_fsync_failure_prevents_any_service_stop(self):
        import os
        import stat
        from scripts.guard_service_update import write_state
        real = os.fsync
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('synthetic directory sync failure')
            return real(fd)
        fake = FakeUnit()
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(supervision, 'snapshot', side_effect=fake.snapshot), \
             patch.object(supervision, 'capture_identity', return_value={'code': 'old'}), \
             patch.object(supervision, 'systemctl', side_effect=fake.ctl), \
             patch('scripts.guard_service_update.os.fsync', side_effect=sync):
            with self.assertRaises(OSError):
                supervision.guarded_update('fixture.service', lambda: None, lambda: None,
                    lambda state: write_state(Path(temp)/'state.json', state))
        self.assertEqual(fake.events, [])
