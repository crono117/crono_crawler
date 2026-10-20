from unittest.mock import patch
from django.test import SimpleTestCase
from leads.services import supervision
from leads.tests.test_guard_review import FakeUnit


class GuardIdentityMissingTests(SimpleTestCase):
    def _run_guard_with_identity(self, identity):
        fake = FakeUnit()
        # Snapshot/ctl mocked to operate on FakeUnit; capture_identity returns a minimal structure
        with patch.object(supervision, "systemctl", side_effect=fake.ctl), \
             patch.object(supervision, "snapshot", side_effect=fake.snapshot), \
             patch.object(supervision, "capture_identity", create=True, return_value={"checkouts": {}, "file_hashes": {}, "working_directory": "/x", "entry_paths": [], "unit_files": [], "environment_files": [], "effective_config_hash": "h"}), \
             patch.object(supervision, "cgroup_empty", create=True, return_value=True):
            def change():
                # Force rollback path
                raise ValueError("synthetic change failure")
            # Record callback injects a corrupt/missing-keys identity into the saved state
            def record(state):
                state["identity"] = identity
            with self.assertRaisesRegex(RuntimeError, "identity"):
                supervision.guarded_update("fixture.service", change, lambda: fake.events.append("rollback"), record)
        # Unit must remain stopped and never restarted
        self.assertEqual(fake.active, "inactive")
        self.assertNotIn("start", fake.events)

    def test_missing_each_required_fingerprint_key_refuses_restart(self):
        required = {
            "checkouts": {},
            "file_hashes": {},
            "working_directory": "/x",
            "entry_paths": [],
            "unit_files": [],
            "environment_files": [],
            "effective_config_hash": "h",
        }
        for key in list(required.keys()):
            bad = dict(required)
            del bad[key]
            with self.subTest(missing=key):
                self._run_guard_with_identity(bad)

    def test_empty_identity_dict_refuses_restart(self):
        self._run_guard_with_identity({})

    def test_wrong_types_refuse_restart(self):
        for identity in ("not-a-dict", 123, ["array"], None):
            with self.subTest(type=type(identity).__name__):
                # verify_identity path will error; guard must propagate refusal and never restart
                self._run_guard_with_identity(identity)

