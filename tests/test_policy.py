import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bk.config import Config
from bk.models import Actor, BookingError, BookingRequest
from bk.policy import policy_for_config
from bk.scheduler import add_booking
from bk.service import build_agent_context
from bk.storage import LedgerStore


class LedgerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.config = Config(data_dir=self.data_dir, gpu_count=2, max_shared_users=2)
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

    def test_capacity_override_cannot_write_or_generate_agent_context(self):
        add_booking(self.store, self.config, self.request)
        original = self.store.ledger_path.read_bytes()
        override = Config(data_dir=self.data_dir, gpu_count=2, max_shared_users=99)

        with self.assertRaisesRegex(BookingError, "max_shared_reservations_per_gpu"):
            add_booking(self.store, override, self.request)
        with self.assertRaisesRegex(BookingError, "max_shared_reservations_per_gpu"):
            build_agent_context(override, self.store, self.actor)

        self.assertEqual(self.store.ledger_path.read_bytes(), original)

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
