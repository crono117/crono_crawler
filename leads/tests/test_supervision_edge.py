from unittest.mock import patch
from django.test import SimpleTestCase
from leads.services import supervision
from leads.tests.test_guard_review import FakeUnit


class GuardEdgeCaseTests(SimpleTestCase):
    def test_timer_restarted_mid_update_fails_closed(self):
        """
        Simulate a unit that is stopped, then restarted by an external timer before restore().
        The guard must detect the unexpected active state, re-quiesce, and fail closed.
        """
        fake = FakeUnit()
        # Track snapshot calls to flip to active exactly at the restore-time quiescence check.
        call_count = {"n": 0}

        def snapshot(unit):
            call_count["n"] += 1
            # 1st call: initial snapshot (active), 2nd: after stop (inactive),
            # 3rd: restore() requires quiescent again -> emulate timer restart (active)
            if call_count["n"] >= 3:
                fake.active = "active"
            return fake.snapshot(unit)

        with patch.object(supervision, "systemctl", side_effect=fake.ctl), \
             patch.object(supervision, "snapshot", side_effect=snapshot), \
             patch.object(supervision, "capture_identity", create=True, return_value={"code": "old"}), \
             patch.object(supervision, "cgroup_empty", create=True, return_value=True):
            with self.assertRaisesRegex(RuntimeError, "quiescence"):
                supervision.guarded_update(
                    "fixture.service",
                    lambda: None,                  # change()
                    lambda: fake.events.append("rollback"),  # rollback() should not run
                    lambda s: fake.events.append("record"),
                )
        # Guard failed closed, never claimed success; no restart success recorded.
        self.assertNotIn("rollback", fake.events)
        # We do not assert precise event order beyond not succeeding.

    def test_partial_or_corrupt_state_identity_refuses_restart(self):
        """
        Record a state with a deliberately truncated identity fingerprint and
        force a change failure so rollback verifies identity: restart is refused.
        """
        fake = FakeUnit()

        def record_and_corrupt(state):
            # Simulate a corrupt/partial fingerprint persisted in the state file.
            ident = state.get("identity", {})
            if "file_hashes" in ident:
                # Drop one arbitrary hash entry to make the identity partial.
                try:
                    key = next(iter(ident["file_hashes"].keys()))
                    del ident["file_hashes"][key]
                except StopIteration:
                    pass
            fake.events.append("record")

        with patch.object(supervision, "systemctl", side_effect=fake.ctl), \
             patch.object(supervision, "snapshot", side_effect=fake.snapshot), \
             patch.object(supervision, "capture_identity", create=True, side_effect=lambda *a, **k: {"code": fake.code, "file_hashes": {"x": "y"}}), \
             patch.object(supervision, "cgroup_empty", create=True, return_value=True):
            def change():
                fake.code = "new"
                raise ValueError("synthetic change failure")
            with self.assertRaisesRegex(RuntimeError, "identity"):
                supervision.guarded_update("fixture.service", change, lambda: fake.events.append("rollback"), record_and_corrupt)
        # Service remains stopped and no restart recorded.
        self.assertEqual(fake.active, "inactive")
        self.assertNotIn("start", fake.events)

