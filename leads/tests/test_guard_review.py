"""Regressions from independent review. All service operations are mocked."""
from unittest.mock import patch
from django.test import SimpleTestCase
from leads.services import supervision


class FakeUnit:
    def __init__(self):
        self.active, self.enabled, self.code = 'active', 'enabled', 'old'
        self.events = []
        self.refuse_stop = False
        self.fail_after_start = False
        self.main_pid_after_stop = 0

    def snapshot(self, unit):
        return {'unit': unit, 'active_state': self.active, 'enabled_state': self.enabled,
                'main_pid': 42 if self.active == 'active' else self.main_pid_after_stop,
                'control_pid': 0, 'control_group': '', 'kill_mode': 'control-group'}

    def ctl(self, action, unit):
        self.events.append(action)
        if action == 'stop' and not self.refuse_stop:
            self.active = 'inactive'
        elif action == 'start':
            self.active = 'active'
            if self.fail_after_start:
                self.fail_after_start = False
                self.enabled = 'disabled'  # make post-start readback fail
        elif action in ('enable', 'disable'):
            self.enabled = 'enabled' if action == 'enable' else 'disabled'


class GuardIndependentReviewTests(SimpleTestCase):
    def run_guard(self, fake, change, rollback):
        with patch.object(supervision, 'systemctl', side_effect=fake.ctl), \
             patch.object(supervision, 'snapshot', side_effect=fake.snapshot), \
             patch.object(supervision, 'capture_identity', create=True, side_effect=lambda *a, **k: {'code': fake.code}), \
             patch.object(supervision, 'cgroup_empty', create=True, return_value=True):
            supervision.guarded_update('fixture.service', change, rollback, lambda s: fake.events.append('record'))

    def test_failed_post_start_readback_stops_before_rollback(self):
        fake = FakeUnit()
        fake.fail_after_start = True
        def change():
            fake.events.append('change')
            fake.code = 'new'
        def rollback():
            fake.events.append('rollback')
            self.assertEqual(fake.active, 'inactive', 'rollback ran beneath a live service')
            fake.code = 'old'
        with self.assertRaises(RuntimeError):
            self.run_guard(fake, change, rollback)
        rollback_at = fake.events.index('rollback')
        first_start = fake.events.index('start')
        self.assertIn('stop', fake.events[first_start + 1:rollback_at])

    def test_unproven_stop_never_runs_change_or_rollback(self):
        fake = FakeUnit()
        fake.refuse_stop = True
        with self.assertRaises(RuntimeError):
            self.run_guard(fake, lambda: fake.events.append('change'), lambda: fake.events.append('rollback'))
        self.assertNotIn('change', fake.events)
        self.assertNotIn('rollback', fake.events)
        self.assertNotIn('start', fake.events)

    def test_inactive_with_live_pid_is_not_quiescent(self):
        fake = FakeUnit()
        fake.main_pid_after_stop = 77
        with self.assertRaises(RuntimeError):
            self.run_guard(fake, lambda: fake.events.append('change'), lambda: fake.events.append('rollback'))
        self.assertNotIn('change', fake.events)
        self.assertNotIn('rollback', fake.events)

    def test_noop_rollback_keeps_service_stopped(self):
        fake = FakeUnit()
        def change():
            fake.code = 'new'
            raise ValueError('synthetic change failure')
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            self.run_guard(fake, change, lambda: fake.events.append('rollback'))
        self.assertEqual(fake.active, 'inactive')
        self.assertNotIn('start', fake.events)
