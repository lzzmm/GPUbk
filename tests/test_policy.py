import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bk.config import Config
from bk.models import Actor, BookingError, BookingRequest
from bk.policy import (
    DaemonPolicyError,
    PolicyGuardedLedgerStore,
    bind_ledger_policy,
    policy_for_config,
    validate_ledger_policy,
)
from bk.scheduler import add_booking
from bk.service import build_agent_context
from bk.storage import LedgerStore


class LedgerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            booking_horizon_days=3650,
        )
        self.store = LedgerStore(self.data_dir)
        self.actor = Actor(1001, "user1001")
        self.request = BookingRequest(
            actor=self.actor,
            count=1,
            duration_seconds=30 * 60,
            start_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            preferred_gpus=[0],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_booking_binds_scheduler_and_storage_policy(self):
        add_booking(self.store, self.config, self.request)

        ledger = self.store.load()

        self.assertEqual(ledger["policy"], policy_for_config(self.config))

    def test_storage_gid_is_omitted_until_an_operator_configures_it(self):
        self.assertNotIn("storage_gid", policy_for_config(self.config))

    def test_configured_storage_gid_is_bound_into_new_policy(self):
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            dir_mode=0o2770,
            storage_gid=os.getgid(),
        )

        self.assertEqual(policy_for_config(config)["storage_gid"], os.getgid())

    def test_configured_storage_gid_upgrades_legacy_policy_on_next_write(self):
        legacy = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            dir_mode=0o2770,
        )
        ledger = {"policy": policy_for_config(legacy)}
        configured = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            booking_horizon_days=3650,
            dir_mode=0o2770,
            storage_gid=os.getgid(),
        )

        validate_ledger_policy(ledger, configured)
        self.assertTrue(bind_ledger_policy(ledger, configured))
        self.assertEqual(ledger["policy"]["storage_gid"], os.getgid())

    def test_bound_storage_gid_cannot_be_omitted_or_changed(self):
        configured = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            dir_mode=0o2770,
            storage_gid=os.getgid(),
        )
        ledger = {"policy": policy_for_config(configured)}
        omitted = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            booking_horizon_days=3650,
            dir_mode=0o2770,
        )
        changed = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            dir_mode=0o2770,
            storage_gid=os.getgid() + 1,
        )

        with self.assertRaisesRegex(BookingError, "storage_gid"):
            validate_ledger_policy(ledger, omitted)
        with self.assertRaisesRegex(BookingError, "storage_gid"):
            validate_ledger_policy(ledger, changed)

    def test_bound_storage_gid_cannot_be_bypassed_during_a_write(self):
        os.chmod(self.data_dir, 0o2770)
        configured = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            booking_horizon_days=3650,
            dir_mode=0o2770,
            storage_gid=os.getgid(),
        )
        trusted_store = LedgerStore(
            self.data_dir,
            dir_mode=0o2770,
            storage_gid=os.getgid(),
        )
        add_booking(trusted_store, configured, self.request)
        original = trusted_store.ledger_path.read_bytes()
        omitted = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            booking_horizon_days=3650,
            dir_mode=0o2770,
        )
        bypass_store = LedgerStore(self.data_dir, dir_mode=0o2770)

        with self.assertRaisesRegex(BookingError, "storage_gid"):
            add_booking(bypass_store, omitted, self.request)

        self.assertEqual(trusted_store.ledger_path.read_bytes(), original)

    def test_capacity_override_cannot_write_or_generate_agent_context(self):
        add_booking(self.store, self.config, self.request)
        original = self.store.ledger_path.read_bytes()
        override = Config(data_dir=self.data_dir, gpu_count=2, max_shared_users=99)

        with self.assertRaisesRegex(BookingError, "max_shared_reservations_per_gpu"):
            add_booking(self.store, override, self.request)
        with self.assertRaisesRegex(BookingError, "max_shared_reservations_per_gpu"):
            build_agent_context(override, self.store, self.actor)

        self.assertEqual(self.store.ledger_path.read_bytes(), original)

    def test_daemon_guard_revalidates_latest_policy_inside_transaction_lock(self):
        add_booking(self.store, self.config, self.request)
        guarded = PolicyGuardedLedgerStore(self.store, self.config, "worker")
        guarded.load()

        def replace_policy(ledger):
            ledger["policy"]["max_shared_reservations_per_gpu"] = 99
            return ledger, None, [], True

        self.store.transaction(replace_policy)
        after_drift = self.store.ledger_path.read_bytes()
        called = False

        def forbidden_mutation(ledger):
            nonlocal called
            called = True
            return ledger, None, [], True

        with self.assertRaisesRegex(DaemonPolicyError, "worker configuration"):
            guarded.transaction(forbidden_mutation)

        self.assertFalse(called)
        self.assertEqual(self.store.ledger_path.read_bytes(), after_drift)

    def test_daemon_guard_allows_legacy_unbound_start_but_detects_policy_removal(self):
        guarded = PolicyGuardedLedgerStore(self.store, self.config, "monitor")
        self.assertNotIn("policy", guarded.load())
        add_booking(self.store, self.config, self.request)
        self.assertIn("policy", guarded.load())

        def remove_policy(ledger):
            ledger.pop("policy", None)
            return ledger, None, [], True

        self.store.transaction(remove_policy)

        with self.assertRaisesRegex(DaemonPolicyError, "ledger policy was removed"):
            guarded.load()

    def test_granularity_override_cannot_mutate_a_bound_ledger(self):
        add_booking(self.store, self.config, self.request)
        original = self.store.ledger_path.read_bytes()
        override = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            slot_minutes=10,
        )

        with self.assertRaisesRegex(BookingError, "granularity_seconds"):
            add_booking(self.store, override, self.request)
        with self.assertRaisesRegex(BookingError, "granularity_seconds"):
            build_agent_context(override, self.store, self.actor)

        self.assertEqual(self.store.ledger_path.read_bytes(), original)

    def test_nondefault_granularity_is_bound_in_seconds(self):
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            booking_horizon_days=3650,
            slot_minutes=10,
        )
        request = BookingRequest(
            actor=self.actor,
            count=1,
            duration_seconds=20 * 60,
            start_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            preferred_gpus=[0],
        )

        add_booking(self.store, config, request)

        self.assertEqual(self.store.load()["policy"]["granularity_seconds"], 600)

    def test_storage_mode_override_is_rejected_before_mutation(self):
        add_booking(self.store, self.config, self.request)
        mismatched = LedgerStore(self.data_dir, file_mode=0o660, dir_mode=0o700)
        mutator_called = False

        def mutate(ledger):
            nonlocal mutator_called
            mutator_called = True
            return ledger, None, [], False

        with self.assertRaisesRegex(PermissionError, "storage modes do not match"):
            mismatched.transaction(mutate)

        self.assertFalse(mutator_called)

    def test_legacy_policy_free_ledger_is_readable_and_binds_on_next_booking(self):
        self.store.ensure()
        self.store._atomic_write_ledger({"version": 1, "reservations": []})

        self.assertNotIn("policy", self.store.load())
        add_booking(self.store, self.config, self.request)

        self.assertEqual(self.store.load()["policy"], policy_for_config(self.config))


if __name__ == "__main__":
    unittest.main()
