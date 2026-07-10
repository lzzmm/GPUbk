import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from bk.config import Config
from bk.models import MODE_EXCLUSIVE, MODE_SHARED, Actor, BookingError, BookingRequest
from bk.scheduler import add_booking
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

    def request(self, uid, mode, *, start=None, count=1, duration_seconds=3600, preferred_gpus=None, allow_queue=False):
        return BookingRequest(
            actor=Actor(uid=uid, username=f"user{uid}"),
            count=count,
            duration_seconds=duration_seconds,
            start_at=start or self.start,
            mode=mode,
            preferred_gpus=preferred_gpus,
            allow_queue=allow_queue,
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


if __name__ == "__main__":
    unittest.main()
