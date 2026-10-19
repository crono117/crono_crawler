from unittest.mock import patch
from django.test import SimpleTestCase
from leads.services import supervision


class SupervisionTests(SimpleTestCase):
    def test_failed_deployment_records_and_restores_all_enabled_active_combinations(self):
        for enabled in ('enabled', 'disabled'):
            for active in ('active', 'inactive'):
                with self.subTest(enabled=enabled, active=active):
                    state = {'unit': 'fixture.service', 'enabled_state': enabled, 'active_state': active}
                    events = []
                    def change():
                        events.append('change')
                        raise RuntimeError('synthetic deployment failure')
                    with patch.object(supervision, 'snapshot', return_value=state), \
                         patch.object(supervision, 'systemctl', side_effect=lambda *args: events.append(args)):
                        with self.assertRaisesRegex(RuntimeError, 'deployment failure'):
                            supervision.guarded_update('fixture.service', change, lambda: events.append('rollback'),
                                lambda s: events.append(('record', dict(s))))
                    self.assertEqual(events, [('record', state), ('stop', 'fixture.service'), 'change', 'rollback',
                        ('enable' if enabled == 'enabled' else 'disable', 'fixture.service'),
                        ('start' if active == 'active' else 'stop', 'fixture.service')])

    def test_failed_preflight_cannot_stop_service(self):
        state = {'unit': 'fixture.service', 'enabled_state': 'enabled', 'active_state': 'active'}
        with patch.object(supervision, 'snapshot', return_value=state), patch.object(supervision, 'systemctl') as ctl:
            with self.assertRaises(OSError):
                supervision.guarded_update('fixture.service', lambda: None, lambda: None,
                    lambda _: (_ for _ in ()).throw(OSError('disk full')))
        ctl.assert_not_called()

    def test_rollback_failure_still_restores_state_and_is_reported(self):
        state = {'unit': 'fixture.service', 'enabled_state': 'enabled', 'active_state': 'active'}
        with patch.object(supervision, 'snapshot', return_value=state), patch.object(supervision, 'systemctl') as ctl:
            with self.assertRaisesRegex(RuntimeError, 'rollback failed'):
                supervision.guarded_update('fixture.service', lambda: (_ for _ in ()).throw(ValueError()),
                    lambda: (_ for _ in ()).throw(RuntimeError('rollback failed')), lambda _: None)
        self.assertIn(('enable', 'fixture.service'), [c.args for c in ctl.call_args_list])
        self.assertIn(('start', 'fixture.service'), [c.args for c in ctl.call_args_list])

    def test_mismatched_readback_fails_loudly(self):
        expected = {'unit': 'fixture.service', 'enabled_state': 'enabled', 'active_state': 'active'}
        with patch.object(supervision, 'snapshot', return_value={**expected, 'enabled_state': 'disabled'}), \
             patch.object(supervision, 'systemctl'):
            with self.assertRaisesRegex(RuntimeError, 'restoration failed'):
                supervision.restore(expected)

    def test_status_distinguishes_stopped_enabled_and_stale_checkout(self):
        with patch.object(supervision, 'snapshot', return_value={'unit': 'fixture.service',
                'active_state': 'inactive', 'enabled_state': 'enabled', 'working_directory': '/fixture'}), \
             patch.object(supervision, 'revision', return_value='old'):
            status = supervision.status('fixture.service', 'new')
        self.assertTrue(status['stopped'])
        self.assertTrue(status['enabled'])
        self.assertFalse(status['active'])
        self.assertEqual(status['checkout_status'], 'stale_checkout')
