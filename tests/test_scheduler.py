import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from bk.config import Config
from bk.models import MODE_EXCLUSIVE, MODE_SHARED, Actor, BookingError, BookingRequest, EditRequest
from bk.scheduler import (
    MAX_EDIT_OPERATIONS_PER_RESERVATION,
    add_booking,
    cancel_booking,
    edit_booking,
    find_available_gpus,
    find_earliest_slot,
)
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
        share_units=None,
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
            share_units=share_units,
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

    def test_operation_id_signature_includes_share_units(self):
        config = replace(self.config, max_shared_users=4)
        add_booking(
            self.store,
            config,
            self.request(
                1001,
                MODE_SHARED,
                op_id="agent-share-create",
                share_units=1,
            ),
        )

        with self.assertRaisesRegex(BookingError, "different write"):
            add_booking(
                self.store,
                config,
                self.request(
                    1001,
                    MODE_SHARED,
                    op_id="agent-share-create",
                    share_units=2,
                ),
            )

    def test_implicit_now_operation_id_remains_idempotent_across_a_slot_boundary(self):
        request_time = self.start + timedelta(minutes=1)
        request = self.request(
            1001,
            MODE_SHARED,
            start=request_time,
            allow_queue=True,
            op_id="agent-now-across-boundary",
        )

        with mock.patch("bk.scheduler.utc_now", return_value=request_time):
            first = add_booking(self.store, self.config, request)
        with mock.patch("bk.scheduler.utc_now", return_value=request_time + timedelta(minutes=5)):
            retried = add_booking(self.store, self.config, request)

        self.assertTrue(first.created)
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

    def test_weighted_share_leaves_only_the_remaining_capacity(self):
        config = replace(self.config, max_shared_users=4)
        first = add_booking(
            self.store,
            config,
            self.request(1001, MODE_SHARED, allow_queue=True, share_units=3),
        )
        second = add_booking(
            self.store,
            config,
            self.request(1002, MODE_SHARED, allow_queue=True),
        )
        third = add_booking(
            self.store,
            config,
            self.request(1003, MODE_SHARED, allow_queue=True),
        )

        self.assertEqual(first.reservation["share_units"], 3)
        self.assertFalse(second.queued)
        self.assertEqual(second.reservation["share_units"], 1)
        self.assertTrue(third.queued)
        self.assertEqual(parse_iso(third.reservation["start_at"]), self.start + timedelta(hours=1))

    def test_legacy_record_without_share_units_counts_as_one(self):
        config = replace(self.config, max_shared_users=4)
        legacy = add_booking(self.store, config, self.request(1001, MODE_SHARED)).reservation

        def remove_new_field(ledger):
            ledger["reservations"][0].pop("share_units")
            return ledger, None, [], True

        self.store.transaction(remove_new_field)
        weighted = add_booking(
            self.store,
            config,
            self.request(1002, MODE_SHARED, share_units=3),
        )

        self.assertTrue(weighted.created)
        self.assertNotIn("share_units", next(item for item in self.store.load()["reservations"] if item["id"] == legacy["id"]))
        with self.assertRaisesRegex(BookingError, "shared capacity full"):
            add_booking(self.store, config, self.request(1003, MODE_SHARED))

    def test_editing_share_units_rechecks_overlapping_capacity(self):
        config = replace(self.config, max_shared_users=4)
        mine = add_booking(self.store, config, self.request(1001, MODE_SHARED)).reservation
        add_booking(
            self.store,
            config,
            self.request(1002, MODE_SHARED, share_units=2),
        )

        with self.assertRaisesRegex(BookingError, "shared capacity full"):
            edit_booking(
                self.store,
                config,
                EditRequest(
                    actor=Actor(1001, "user1001"),
                    reservation_id=mine["id"],
                    share_units=3,
                    update_share_units=True,
                ),
            )

        stored = next(item for item in self.store.load()["reservations"] if item["id"] == mine["id"])
        self.assertEqual(stored["share_units"], 1)

    def test_explicit_memory_is_not_multiplied_by_share_units(self):
        config = replace(self.config, max_shared_users=4)
        capacities = {0: 24 * 1024}
        add_booking(
            self.store,
            config,
            replace(
                self.request(1001, MODE_SHARED, share_units=3),
                expected_memory_mb=4 * 1024,
                gpu_memory_capacity_mb=capacities,
            ),
        )
        second = add_booking(
            self.store,
            config,
            replace(
                self.request(1002, MODE_SHARED),
                expected_memory_mb=4 * 1024,
                gpu_memory_capacity_mb=capacities,
            ),
        )

        self.assertTrue(second.created)

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

    def test_implicit_now_start_uses_the_current_five_minute_slot(self):
        now = self.start + timedelta(minutes=1, seconds=17)

        with mock.patch("bk.scheduler.utc_now", return_value=now):
            result = add_booking(
                self.store,
                self.config,
                self.request(1001, MODE_SHARED, start=now, allow_queue=True),
            )

        self.assertEqual(parse_iso(result.reservation["start_at"]), self.start)
        self.assertEqual(parse_iso(result.reservation["end_at"]), self.start + timedelta(hours=1))

    def test_future_unaligned_queue_start_is_still_rounded_up(self):
        now = self.start - timedelta(minutes=30)
        requested = self.start + timedelta(minutes=1)

        with mock.patch("bk.scheduler.utc_now", return_value=now):
            result = add_booking(
                self.store,
                self.config,
                self.request(1001, MODE_SHARED, start=requested, allow_queue=True),
            )

        self.assertEqual(parse_iso(result.reservation["start_at"]), self.start + timedelta(minutes=5))

    def test_explicit_start_and_duration_must_match_five_minute_grid(self):
        with self.assertRaisesRegex(BookingError, "5-minute boundary"):
            add_booking(self.store, self.config, self.request(1001, MODE_SHARED, start=self.start + timedelta(minutes=1)))

        with self.assertRaisesRegex(BookingError, "multiple of 5 minutes"):
            add_booking(self.store, self.config, self.request(1001, MODE_SHARED, duration_seconds=60))

    def test_configured_ten_minute_grid_controls_create_and_queue_candidates(self):
        config = Config(
            data_dir=Path(self.tmp.name),
            gpu_count=1,
            max_shared_users=2,
            slot_minutes=10,
        )
        now = datetime(2030, 1, 1, 12, 47, 23, tzinfo=self.start.tzinfo)

        with mock.patch("bk.scheduler.utc_now", return_value=now):
            created = add_booking(
                self.store,
                config,
                self.request(
                    1001,
                    MODE_SHARED,
                    start=now,
                    duration_seconds=20 * 60,
                    allow_queue=True,
                ),
            )

        self.assertEqual(parse_iso(created.reservation["start_at"]), now.replace(minute=40, second=0))
        with self.assertRaisesRegex(BookingError, "multiple of 10 minutes"):
            add_booking(
                self.store,
                config,
                self.request(1002, MODE_SHARED, duration_seconds=5 * 60),
            )
        with self.assertRaisesRegex(BookingError, "10-minute boundary"):
            add_booking(
                self.store,
                config,
                self.request(
                    1002,
                    MODE_SHARED,
                    start=datetime(2030, 1, 2, 12, 45, tzinfo=self.start.tzinfo),
                    duration_seconds=20 * 60,
                ),
            )

        legacy_end = datetime(2030, 1, 2, 12, 45, tzinfo=self.start.tzinfo)
        ledger = {
            "version": 1,
            "reservations": [
                {
                    "id": "legacy-five-minute-end",
                    "uid": 1009,
                    "username": "legacy",
                    "gpus": [0],
                    "mode": MODE_EXCLUSIVE,
                    "start_at": (legacy_end - timedelta(minutes=5)).isoformat(),
                    "end_at": legacy_end.isoformat(),
                    "status": "active",
                }
            ],
        }
        with mock.patch("bk.scheduler.utc_now", return_value=legacy_end - timedelta(hours=1)):
            slot = find_earliest_slot(
                ledger,
                config,
                1,
                legacy_end - timedelta(minutes=5),
                timedelta(minutes=20),
                MODE_EXCLUSIVE,
                1001,
                allow_queue=True,
            )

        self.assertIsNotNone(slot)
        self.assertEqual(slot[0], datetime(2030, 1, 2, 12, 50, tzinfo=self.start.tzinfo))

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
        self.store.ledger_path.chmod(0o600)
        self.store.log_path.chmod(0o600)
        config = replace(self.config, ledger_retention_days=30)

        created = add_booking(self.store, config, self.request(2000, MODE_SHARED))

        ids = {item["id"] for item in self.store.load()["reservations"]}
        self.assertEqual(ids, {"recent-cancelled", created.reservation["id"]})
        self.assertIn("historic-audit", self.store.log_path.read_text(encoding="utf-8"))

    def test_maintenance_marks_expired_running_job_uncertain(self):
        now = utc_now()
        record = {
            "id": "expired-running",
            "uid": 1001,
            "username": "user1001",
            "gpus": [0],
            "mode": MODE_SHARED,
            "start_at": (now - timedelta(minutes=10)).isoformat(),
            "end_at": (now - timedelta(minutes=5)).isoformat(),
            "status": "active",
            "created_at": (now - timedelta(minutes=10)).isoformat(),
            "updated_at": (now - timedelta(minutes=10)).isoformat(),
            "job": {"status": "running", "summary": "python train.py"},
        }
        self.store.ensure()
        self.store.ledger_path.write_text(
            json.dumps({"version": 1, "reservations": [record]}),
            encoding="utf-8",
        )
        self.store.ledger_path.chmod(0o600)

        add_booking(self.store, self.config, self.request(2000, MODE_SHARED))

        stored = next(
            item for item in self.store.load()["reservations"] if item["id"] == "expired-running"
        )
        self.assertEqual(stored["status"], "expired")
        self.assertEqual(stored["job"]["status"], "uncertain")
        self.assertEqual(stored["job"]["recovery_state"], "expired-unverified")

    def test_cancellation_preserves_claim_for_crash_recovery(self):
        actor = Actor(1001, "user1001")
        created = add_booking(
            self.store,
            self.config,
            BookingRequest(
                actor=actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=self.start,
                job_spec_id="00000000-0000-0000-0000-000000000001",
                job_digest="0" * 64,
                job_summary="python train.py",
            ),
        ).reservation

        def mark_claimed(ledger):
            item = next(value for value in ledger["reservations"] if value["id"] == created["id"])
            item["job"]["status"] = "claimed"
            item["job"]["claim_token"] = "claim-token"
            item["job"]["worker_lease_id"] = "old-worker"
            return ledger, None, [], True

        self.store.transaction(mark_claimed)

        cancelled = cancel_booking(self.store, created["id"], actor)

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["job"]["status"], "claimed")
        self.assertIsNotNone(cancelled["job"]["cancel_requested_at"])


if __name__ == "__main__":
    unittest.main()
