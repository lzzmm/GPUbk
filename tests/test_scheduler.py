import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from bk.config import Config
from bk.models import MODE_EXCLUSIVE, MODE_SHARED, Actor, BookingError, BookingRequest, EditRequest
from bk.scheduler import MAX_EDIT_OPERATIONS_PER_RESERVATION, add_booking, edit_booking, find_available_gpus
from bk.storage import LedgerStore
from bk.timeparse import parse_iso, utc_now


def ceil_5m(value):
    timestamp = int(value.timestamp())
    remainder = timestamp % 300
    if remainder:
        timestamp += 300 - remainder
    return datetime.fromtimestamp(timestamp, value.tzinfo)


class SchedulerModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.tmp.name), gpu_count=1, max_shared_users=2)
        self.store = LedgerStore(self.config.data_dir)
        self.start = ceil_5m(utc_now() + timedelta(days=1))

    def tearDown(self):
        self.tmp.cleanup()

    def request(
        self,
        uid,
        mode,
        *,
        start=None,
        count=1,
        duration_seconds=3600,
        preferred_gpus=None,
        allow_queue=False,
        op_id=None,
    ):
        return BookingRequest(
            actor=Actor(uid=uid, username=f"user{uid}"),
            count=count,
            duration_seconds=duration_seconds,
            start_at=start or self.start,
            mode=mode,
            preferred_gpus=preferred_gpus,
            allow_queue=allow_queue,
            op_id=op_id,
        )

    def test_shared_allows_configured_number_of_users(self):
        first = add_booking(self.store, self.config, self.request(1001, MODE_SHARED))
        second = add_booking(self.store, self.config, self.request(1002, MODE_SHARED))

        self.assertTrue(first.created)
        self.assertTrue(second.created)
        self.assertEqual(first.reservation["gpus"], [0])
        self.assertEqual(second.reservation["gpus"], [0])

        with self.assertRaises(BookingError):
            add_booking(self.store, self.config, self.request(1003, MODE_SHARED))

    def test_exclusive_blocks_shared_overlap(self):
        add_booking(self.store, self.config, self.request(1001, MODE_EXCLUSIVE))

        with self.assertRaises(BookingError):
            add_booking(self.store, self.config, self.request(1002, MODE_SHARED))

    def test_shared_blocks_later_exclusive_overlap(self):
        add_booking(self.store, self.config, self.request(1001, MODE_SHARED))

        with self.assertRaises(BookingError):
            add_booking(self.store, self.config, self.request(1002, MODE_EXCLUSIVE))

    def test_exact_duplicate_is_idempotent(self):
        first = add_booking(self.store, self.config, self.request(1001, MODE_SHARED))
        second = add_booking(self.store, self.config, self.request(1001, MODE_SHARED))

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.reservation["id"], second.reservation["id"])

    def test_exclusive_duplicate_is_idempotent(self):
        first = add_booking(self.store, self.config, self.request(1001, MODE_EXCLUSIVE))
        second = add_booking(self.store, self.config, self.request(1001, MODE_EXCLUSIVE))

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.reservation["id"], second.reservation["id"])

    def test_create_operation_id_rejects_a_different_request(self):
        first = add_booking(
            self.store,
            self.config,
            self.request(1001, MODE_SHARED, op_id="agent-create-1"),
        )

        retried = add_booking(
            self.store,
            self.config,
            self.request(1001, MODE_SHARED, op_id="agent-create-1"),
        )
        with self.assertRaisesRegex(BookingError, "different write"):
            add_booking(
                self.store,
                self.config,
                self.request(
                    1001,
                    MODE_SHARED,
                    duration_seconds=30 * 60,
                    op_id="agent-create-1",
                ),
            )

        self.assertFalse(retried.created)
        self.assertEqual(retried.reservation["id"], first.reservation["id"])
        self.assertEqual(len(self.store.load()["reservations"]), 1)

    def test_edit_operation_id_is_idempotent_and_cannot_be_reused(self):
        actor = Actor(uid=1001, username="user1001")
        created = add_booking(self.store, self.config, self.request(actor.uid, MODE_SHARED))
        request = EditRequest(
            actor=actor,
            reservation_id=created.reservation["id"],
            op_id="agent-edit-1",
            duration_seconds=30 * 60,
        )

        first = edit_booking(self.store, self.config, request)
        retried = edit_booking(self.store, self.config, request)
        with self.assertRaisesRegex(BookingError, "different write"):
            edit_booking(
                self.store,
                self.config,
                EditRequest(
                    actor=actor,
                    reservation_id=created.reservation["id"],
                    op_id="agent-edit-1",
                    duration_seconds=45 * 60,
                ),
            )
        with self.assertRaisesRegex(BookingError, "different write"):
            add_booking(
                self.store,
                self.config,
                self.request(actor.uid, MODE_SHARED, op_id="agent-edit-1"),
            )

        self.assertTrue(first.created)
        self.assertFalse(retried.created)
        self.assertEqual(first.reservation["end_at"], retried.reservation["end_at"])
        self.assertEqual(len(first.reservation["edit_operations"]), 1)
        self.assertEqual(len(self.store.log_path.read_text(encoding="utf-8").splitlines()), 2)

    def test_idempotent_edit_history_is_bounded(self):
        actor = Actor(uid=1001, username="user1001")
        created = add_booking(self.store, self.config, self.request(actor.uid, MODE_SHARED))

        def fill_history(ledger):
            ledger["reservations"][0]["edit_operations"] = [
                {"op_id": f"old-{index}", "signature": "0" * 64}
                for index in range(MAX_EDIT_OPERATIONS_PER_RESERVATION)
            ]
            return ledger, None, [], True

        self.store.transaction(fill_history)

        with self.assertRaisesRegex(BookingError, "idempotent edit limit"):
            edit_booking(
                self.store,
                self.config,
                EditRequest(
                    actor=actor,
                    reservation_id=created.reservation["id"],
                    op_id="one-too-many",
                    duration_seconds=30 * 60,
                ),
            )

    def test_same_uid_shared_records_overlap_until_capacity_is_full(self):
        first = add_booking(self.store, self.config, self.request(1001, MODE_SHARED, allow_queue=True))
        second = add_booking(
            self.store,
            self.config,
            self.request(1001, MODE_SHARED, allow_queue=True),
        )
        third = add_booking(
            self.store,
            self.config,
            self.request(1001, MODE_SHARED, allow_queue=True),
        )

        self.assertTrue(first.created)
        self.assertTrue(second.created)
        self.assertFalse(second.queued)
        self.assertEqual(parse_iso(second.reservation["start_at"]), parse_iso(first.reservation["start_at"]))
        self.assertTrue(third.queued)
        self.assertEqual(parse_iso(third.reservation["start_at"]), parse_iso(first.reservation["end_at"]))

    def test_shared_capacity_counts_records_not_distinct_uids(self):
        add_booking(self.store, self.config, self.request(1001, MODE_SHARED, allow_queue=True))
        add_booking(self.store, self.config, self.request(1001, MODE_SHARED, allow_queue=True))
        third = add_booking(self.store, self.config, self.request(1002, MODE_SHARED, allow_queue=True))

        self.assertTrue(third.queued)
        self.assertEqual(parse_iso(third.reservation["start_at"]), self.start + timedelta(hours=1))

    def test_third_shared_user_queues_after_shared_capacity_frees(self):
        add_booking(self.store, self.config, self.request(1001, MODE_SHARED, allow_queue=True))
        add_booking(self.store, self.config, self.request(1002, MODE_SHARED, allow_queue=True))
        third = add_booking(self.store, self.config, self.request(1003, MODE_SHARED, allow_queue=True))

        self.assertTrue(third.queued)
        self.assertEqual(parse_iso(third.reservation["start_at"]), self.start + timedelta(hours=1))

    def test_exclusive_request_queues_until_all_requested_gpus_are_free(self):
        config = Config(data_dir=Path(self.tmp.name), gpu_count=2, max_shared_users=2)
        store = LedgerStore(config.data_dir)
        add_booking(store, config, self.request(1001, MODE_SHARED, preferred_gpus=[0]))
        add_booking(store, config, self.request(1002, MODE_SHARED, preferred_gpus=[1]))

        result = add_booking(
            store,
            config,
            self.request(
                1003,
                MODE_EXCLUSIVE,
                start=self.start + timedelta(seconds=10),
                count=2,
                allow_queue=True,
            ),
        )

        self.assertTrue(result.queued)
        self.assertEqual(result.reservation["gpus"], [0, 1])
        self.assertEqual(parse_iso(result.reservation["start_at"]), self.start + timedelta(hours=1))

    def test_explicit_start_conflict_fails_with_nearest_available_hint(self):
        add_booking(self.store, self.config, self.request(1001, MODE_SHARED))

        with self.assertRaisesRegex(BookingError, "nearest available"):
            add_booking(self.store, self.config, self.request(1002, MODE_EXCLUSIVE))

        ledger = self.store.load()
        self.assertEqual(len(ledger["reservations"]), 1)

    def test_edit_rejects_a_reservation_that_has_already_started(self):
        actor = Actor(uid=1001, username="user1001")
        created = add_booking(self.store, self.config, self.request(actor.uid, MODE_SHARED))
        before = self.store.load()

        with mock.patch("bk.scheduler.utc_now", return_value=self.start + timedelta(minutes=5)):
            with self.assertRaisesRegex(BookingError, "after it has started"):
                edit_booking(
                    self.store,
                    self.config,
                    EditRequest(actor=actor, reservation_id=created.reservation["id"], duration_seconds=30 * 60),
                )

        self.assertEqual(self.store.load(), before)

    def test_exact_edit_rejects_a_new_start_in_the_past(self):
        actor = Actor(uid=1001, username="user1001")
        created = add_booking(self.store, self.config, self.request(actor.uid, MODE_SHARED))
        now = self.start - timedelta(minutes=30)
        before = self.store.load()

        with mock.patch("bk.scheduler.utc_now", return_value=now):
            with self.assertRaisesRegex(BookingError, "must not be in the past"):
                edit_booking(
                    self.store,
                    self.config,
                    EditRequest(
                        actor=actor,
                        reservation_id=created.reservation["id"],
                        start_at=now - timedelta(minutes=5),
                    ),
                )

        self.assertEqual(self.store.load(), before)

    def test_preferred_gpu_queues_on_that_gpu_while_auto_can_choose_another(self):
        config = Config(data_dir=Path(self.tmp.name), gpu_count=2, max_shared_users=2)
        store = LedgerStore(config.data_dir)
        first = add_booking(store, config, self.request(1001, MODE_EXCLUSIVE, preferred_gpus=[0]))

        auto = add_booking(
            store,
            config,
            self.request(1002, MODE_EXCLUSIVE, start=self.start + timedelta(seconds=5), allow_queue=True),
        )
        preferred = add_booking(
            store,
            config,
            self.request(
                1003,
                MODE_EXCLUSIVE,
                start=self.start + timedelta(seconds=10),
                preferred_gpus=[0],
                allow_queue=True,
            ),
        )

        self.assertFalse(auto.queued)
        self.assertEqual(auto.reservation["gpus"], [1])
        self.assertTrue(preferred.queued)
        self.assertEqual(preferred.reservation["gpus"], [0])
        self.assertEqual(parse_iso(preferred.reservation["start_at"]), parse_iso(first.reservation["end_at"]))

    def test_auto_shared_prefers_gpu_without_existing_shared_load(self):
        config = Config(data_dir=Path(self.tmp.name), gpu_count=2, max_shared_users=2)
        store = LedgerStore(config.data_dir)
        add_booking(store, config, self.request(1001, MODE_SHARED, preferred_gpus=[0]))

        result = add_booking(store, config, self.request(1002, MODE_SHARED))

        self.assertEqual(result.reservation["gpus"], [1])

    def test_public_availability_api_preserves_optional_argument_order(self):
        config = Config(data_dir=Path(self.tmp.name), gpu_count=2)

        available = find_available_gpus(
            {"version": 1, "reservations": []},
            config,
            1,
            self.start,
            self.start + timedelta(hours=1),
            MODE_SHARED,
            1001,
            [1, 0],
            {0: 10.0, 1: 0.0},
        )

        self.assertEqual(available, [1])

    def test_shared_memory_budget_blocks_oversubscription(self):
        capacities = {0: 24 * 1024}
        add_booking(
            self.store,
            self.config,
            replace(
                self.request(1001, MODE_SHARED),
                expected_memory_mb=16 * 1024,
                gpu_memory_capacity_mb=capacities,
            ),
        )

        with self.assertRaisesRegex(BookingError, "shared memory full"):
            add_booking(
                self.store,
                self.config,
                replace(
                    self.request(1002, MODE_SHARED),
                    expected_memory_mb=12 * 1024,
                    gpu_memory_capacity_mb=capacities,
                ),
            )

    def test_undeclared_shared_memory_uses_equal_share_assumption(self):
        capacities = {0: 24 * 1024}
        first_request = self.request(1001, MODE_SHARED)
        add_booking(
            self.store,
            self.config,
            replace(first_request, gpu_memory_capacity_mb=capacities),
        )

        with self.assertRaisesRegex(BookingError, "shared memory full"):
            second_request = self.request(1002, MODE_SHARED)
            add_booking(
                self.store,
                self.config,
                replace(
                    second_request,
                    expected_memory_mb=16 * 1024,
                    gpu_memory_capacity_mb=capacities,
                ),
            )

    def test_implicit_now_start_is_rounded_up_to_five_minute_grid(self):
        unaligned = self.start + timedelta(minutes=1)
        result = add_booking(
            self.store,
            self.config,
            self.request(1001, MODE_SHARED, start=unaligned, allow_queue=True),
        )

        self.assertEqual(int(parse_iso(result.reservation["start_at"]).timestamp()) % 300, 0)

    def test_explicit_start_and_duration_must_match_five_minute_grid(self):
        with self.assertRaisesRegex(BookingError, "5-minute boundary"):
            add_booking(self.store, self.config, self.request(1001, MODE_SHARED, start=self.start + timedelta(minutes=1)))

        with self.assertRaisesRegex(BookingError, "multiple of 5 minutes"):
            add_booking(self.store, self.config, self.request(1001, MODE_SHARED, duration_seconds=60))

    def test_new_booking_prunes_old_terminal_records_but_keeps_audit_log(self):
        now = utc_now()
        old = now - timedelta(days=120)
        recent = now - timedelta(days=5)
        old_record = {
            "id": "old-active",
            "op_id": "old-active-op",
            "uid": 1001,
            "username": "user1001",
            "gpus": [0],
            "mode": MODE_SHARED,
            "start_at": old.isoformat(),
            "end_at": (old + timedelta(hours=1)).isoformat(),
            "status": "active",
            "created_at": old.isoformat(),
            "updated_at": old.isoformat(),
        }
        old_cancelled = {**old_record, "id": "old-cancelled", "status": "cancelled"}
        recent_cancelled = {
            **old_record,
            "id": "recent-cancelled",
            "status": "cancelled",
            "updated_at": recent.isoformat(),
        }
        self.store.ensure()
        self.store.ledger_path.write_text(
            json.dumps({"version": 1, "reservations": [old_record, old_cancelled, recent_cancelled]}),
            encoding="utf-8",
        )
        self.store.log_path.write_text('{"event_id":"historic-audit"}\n', encoding="utf-8")
        config = replace(self.config, ledger_retention_days=30)

        created = add_booking(self.store, config, self.request(2000, MODE_SHARED))

        ids = {item["id"] for item in self.store.load()["reservations"]}
        self.assertEqual(ids, {"recent-cancelled", created.reservation["id"]})
        self.assertIn("historic-audit", self.store.log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
