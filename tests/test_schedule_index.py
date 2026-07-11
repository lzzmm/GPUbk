import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bk.config import Config
from bk.models import MODE_EXCLUSIVE
from bk.schedule_index import ReservationIndex
from bk.scheduler import find_earliest_slot, list_active
from bk.timeparse import parse_iso, to_iso


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def record(rid, gpu, start, end, status="active"):
    return {
        "id": rid,
        "uid": 1000,
        "username": "user",
        "gpus": [gpu],
        "mode": MODE_EXCLUSIVE,
        "start_at": to_iso(start),
        "end_at": to_iso(end),
        "status": status,
    }


class ReservationIndexTests(unittest.TestCase):
    def test_active_records_are_sorted_and_overlap_respects_touching_boundaries(self):
        first = record("first", 0, NOW + timedelta(hours=1), NOW + timedelta(hours=2))
        second = record("second", 0, NOW + timedelta(hours=2), NOW + timedelta(hours=3))
        expired = record("expired", 0, NOW - timedelta(hours=2), NOW)
        cancelled = record("cancelled", 0, NOW, NOW + timedelta(hours=4), "cancelled")
        malformed = record("malformed", 0, NOW + timedelta(hours=3), NOW + timedelta(hours=4))
        malformed["gpus"] = None
        ledger = {"version": 1, "reservations": [second, cancelled, malformed, expired, first]}

        index = ReservationIndex.from_ledger(ledger, NOW)

        self.assertEqual([item["id"] for item in index.records()], ["first", "second", "malformed"])
        self.assertEqual(
            [item.record["id"] for item in index.overlapping(0, NOW + timedelta(hours=2), NOW + timedelta(hours=2, minutes=30))],
            ["second"],
        )
        self.assertEqual([item["id"] for item in list_active(ledger, NOW)], ["first", "second", "malformed"])

    def test_repeated_overlap_queries_do_not_reparse_timestamps(self):
        records = [
            record(str(index), index % 8, NOW + timedelta(minutes=index), NOW + timedelta(minutes=index + 30))
            for index in range(200)
        ]
        ledger = {"version": 1, "reservations": records}

        with mock.patch("bk.schedule_index.parse_iso", wraps=parse_iso) as parser:
            index = ReservationIndex.from_ledger(ledger, NOW)
            for minute in range(100):
                start = NOW + timedelta(minutes=minute)
                index.overlapping(minute % 8, start, start + timedelta(minutes=30))

        self.assertEqual(parser.call_count, len(records) * 2)

    def test_history_index_can_include_expired_records_without_changing_default(self):
        expired = record("expired", 0, NOW - timedelta(hours=2), NOW - timedelta(hours=1), "expired")
        active = record("active", 0, NOW + timedelta(hours=1), NOW + timedelta(hours=2))
        ledger = {"version": 1, "reservations": [expired, active]}

        default = ReservationIndex.from_ledger(ledger, NOW - timedelta(hours=3))
        history = ReservationIndex.from_ledger(
            ledger,
            NOW - timedelta(hours=3),
            statuses=("active", "expired"),
        )
        empty = ReservationIndex.from_ledger(ledger, NOW - timedelta(hours=3), statuses=())

        self.assertEqual([item["id"] for item in default.records()], ["active"])
        self.assertEqual([item["id"] for item in history.records()], ["expired", "active"])
        self.assertEqual(empty.records(), [])

    def test_week_queue_search_parses_each_active_record_only_once(self):
        reservations = []
        for slot in range(48):
            start = NOW + timedelta(minutes=30 * slot)
            for gpu in range(8):
                reservations.append(record(f"{slot}-{gpu}", gpu, start, start + timedelta(minutes=30)))
        ledger = {"version": 1, "reservations": reservations}
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=8, queue_search_hours=24)
            with mock.patch("bk.schedule_index.parse_iso", wraps=parse_iso) as parser:
                result = find_earliest_slot(
                    ledger,
                    config,
                    8,
                    NOW,
                    timedelta(minutes=30),
                    MODE_EXCLUSIVE,
                    1000,
                    allow_queue=True,
                )

        self.assertEqual(result, (NOW + timedelta(days=1), list(range(8))))
        self.assertEqual(parser.call_count, len(reservations) * 2)


if __name__ == "__main__":
    unittest.main()
