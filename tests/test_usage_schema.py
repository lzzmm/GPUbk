import unittest
from datetime import datetime, timezone
import json
from pathlib import Path

from bk.usage_schema import (
    aggregate_rollups,
    decode_event,
    decode_rollup,
    encode_event,
    encode_rollup,
    unknown_storage_fields,
)


class UsageSchemaTests(unittest.TestCase):
    def test_frozen_v1_fixtures_remain_readable(self):
        fixture_dir = Path(__file__).parent / "fixtures" / "usage" / "v1"
        event = decode_event(json.loads((fixture_dir / "event.json").read_text(encoding="utf-8")))
        rollup = decode_rollup(json.loads((fixture_dir / "rollup.json").read_text(encoding="utf-8")))

        self.assertEqual(event["event_id"], "fixture-event")
        self.assertEqual(event["status"], "ok")
        self.assertEqual(rollup["avg_sm_percent"], 45)
        self.assertEqual(rollup["workload_observed_seconds"], {"7": 60.0})

    def test_event_round_trip_uses_object_fields_and_preserves_extensions(self):
        event = {
            "event": "process-start",
            "timestamp": "2030-01-01T12:00:00Z",
            "event_id": "event-1",
            "key": "g0:p12:sabc",
            "gpu": 0,
            "pid": 12,
            "uid": 1001,
            "username": "alice",
            "workload_id": 7,
            "kind": "C",
            "status": "ok",
            "reservation_ids": ["booking"],
            "extensions": {"org.example.metric": {"v": 1, "value": 3}},
        }

        encoded = encode_event(event)
        decoded = decode_event(encoded, lambda uid: "alice" if uid == 1001 else "?")

        self.assertIsInstance(encoded, dict)
        self.assertEqual(decoded["event"], "process-start")
        self.assertEqual(decoded["workload_id"], 7)
        self.assertEqual(decoded["extensions"], event["extensions"])

    def test_unknown_enum_is_retained_as_unknown_code(self):
        decoded = decode_event({"v": 1, "e": 91, "t": 1893499200, "s": 77})

        self.assertEqual(decoded["event"], "unknown(91)")
        self.assertEqual(decoded["status"], "unknown(77)")

    def test_rollup_round_trip_keeps_missing_distinct_from_measured_zero(self):
        base = {
            "window_start": "2030-01-01T12:00:00Z",
            "window_end": "2030-01-01T12:01:00Z",
            "partial": False,
            "gpu": 0,
            "uid": 1001,
            "username": "alice",
            "status": "ok",
            "reservation_ids": ["booking"],
            "sample_count": 30,
            "observed_seconds": 60,
            "active_sample_count": 0,
            "active_observed_seconds": 0,
            "avg_process_count": 0,
            "max_process_count": 0,
            "sm_sample_count": 30,
            "avg_sm_percent": 0,
            "max_sm_percent": 0,
            "avg_gpu_memory_mb": 0,
            "max_gpu_memory_mb": 0,
            "device_util_sample_count": 0,
            "avg_device_util_percent": None,
            "max_device_util_percent": None,
            "workload_ids": [],
            "workload_observed_seconds": {},
        }

        decoded = decode_rollup(encode_rollup(base), lambda _uid: "alice")

        self.assertEqual(decoded["avg_sm_percent"], 0)
        self.assertIsNone(decoded["avg_device_util_percent"])
        self.assertEqual(decoded["active_observed_seconds"], 0)

    def test_rollup_aggregation_is_weighted_and_preserves_workload_presence(self):
        first = self._record("2030-01-01T12:00:00Z", 10, 100, 2)
        second = self._record("2030-01-01T12:01:00Z", 30, 300, 3)

        result = aggregate_rollups([first, second], 300)[0]

        self.assertEqual(result["sample_count"], 4)
        self.assertEqual(result["avg_sm_percent"], 20)
        self.assertEqual(result["avg_gpu_memory_mb"], 200)
        self.assertEqual(result["max_gpu_memory_mb"], 300)
        self.assertEqual(result["workload_ids"], [2, 3])
        self.assertEqual(result["workload_observed_seconds"], {"2": 60.0, "3": 60.0})

    def test_compactor_detects_future_storage_fields(self):
        self.assertEqual(unknown_storage_fields({"v": 1, "t": 1, "future": 9}, "rollups"), {"future"})

    @staticmethod
    def _record(start: str, sm: int, memory: int, workload_id: int) -> dict:
        timestamp = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end = datetime.fromtimestamp(timestamp.timestamp() + 60, timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "window_start": start,
            "window_end": end,
            "partial": False,
            "gpu": 0,
            "uid": 1001,
            "username": "alice",
            "status": "ok",
            "reservation_ids": ["booking"],
            "sample_count": 2,
            "observed_seconds": 60,
            "active_sample_count": 2,
            "active_observed_seconds": 60,
            "avg_process_count": 1,
            "max_process_count": 1,
            "sm_sample_count": 2,
            "avg_sm_percent": sm,
            "max_sm_percent": sm,
            "avg_gpu_memory_mb": memory,
            "max_gpu_memory_mb": memory,
            "device_util_sample_count": 2,
            "avg_device_util_percent": sm,
            "max_device_util_percent": sm,
            "workload_ids": [workload_id],
            "workload_observed_seconds": {str(workload_id): 60},
        }


if __name__ == "__main__":
    unittest.main()
