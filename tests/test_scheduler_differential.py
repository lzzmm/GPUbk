import random
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bk.config import Config
from bk.models import MODE_EXCLUSIVE, MODE_SHARED
from bk.scheduler import find_earliest_slot
from bk.timeparse import parse_iso


NOW = datetime(2032, 3, 7, 10, 1, 17, tzinfo=timezone.utc)
BASE = datetime(2032, 3, 7, 10, 0, tzinfo=timezone.utc)


def _aligned(value, seconds, *, floor=False):
    timestamp = int(value.timestamp())
    remainder = timestamp % seconds
    if floor or not remainder:
        timestamp -= remainder
    else:
        timestamp += seconds - remainder
    return datetime.fromtimestamp(timestamp, timezone.utc)


def _overlaps(start_a, end_a, start_b, end_b):
    return start_a < end_b and start_b < end_a


def _gpu_available(records, gpu, start, end, mode, capacity, requested_units):
    overlapping = [
        record
        for record in records
        if gpu in record["gpus"]
        and _overlaps(start, end, record["_start"], record["_end"])
    ]
    if mode == MODE_EXCLUSIVE:
        return not overlapping
    if any(record["mode"] == MODE_EXCLUSIVE for record in overlapping):
        return False

    points = {start, end}
    for record in overlapping:
        points.add(max(start, record["_start"]))
        points.add(min(end, record["_end"]))
    ordered = sorted(points)
    for left, right in zip(ordered, ordered[1:]):
        used = sum(
            int(record.get("share_units", 1))
            for record in overlapping
            if record["mode"] == MODE_SHARED
            and _overlaps(left, right, record["_start"], record["_end"])
        )
        if used + requested_units > capacity:
            return False
    return True


def _oracle_start(records, config, count, earliest, duration, mode, preferred, units):
    records = [record for record in records if record["_end"] > NOW]
    step = config.slot_seconds
    candidate = (
        _aligned(NOW, step, floor=True)
        if earliest <= NOW
        else _aligned(earliest, step)
    )
    search_until = candidate + timedelta(hours=config.queue_search_hours)
    while candidate <= search_until:
        end = candidate + duration
        candidates = list(preferred) if preferred is not None else list(range(config.gpu_count))
        available = [
            gpu
            for gpu in candidates
            if _gpu_available(
                records,
                gpu,
                candidate,
                end,
                mode,
                config.max_shared_users,
                units,
            )
        ]
        if preferred is not None:
            if len(available) == len(preferred):
                return candidate
        elif len(available) >= count:
            return candidate
        candidate += timedelta(seconds=step)
    return None


class SchedulerDifferentialTests(unittest.TestCase):
    def test_optimized_candidates_match_independent_slice_scan(self):
        rng = random.Random(0xB00C)
        slot_options = (1, 2, 3, 5, 10, 15)

        for case in range(2500):
            gpu_count = rng.randint(1, 4)
            capacity = rng.randint(1, 4)
            config = Config(
                data_dir=Path("/tmp/gpubk-scheduler-differential"),
                gpu_count=gpu_count,
                max_shared_users=capacity,
                queue_search_hours=2,
                slot_minutes=rng.choice(slot_options),
            )
            records = []
            ledger_records = []
            for index in range(rng.randint(0, 12)):
                start = BASE + timedelta(
                    minutes=rng.randint(-20, 70),
                    seconds=rng.choice((0, 0, 17, 30, 59)),
                )
                end = start + timedelta(minutes=rng.randint(1, 20) * rng.choice((2, 3, 5)))
                record_mode = MODE_EXCLUSIVE if rng.random() < 0.2 else MODE_SHARED
                record = {
                    "id": f"case-{case}-record-{index}",
                    "uid": rng.randint(1000, 1005),
                    "username": "user",
                    "gpus": rng.sample(
                        range(gpu_count),
                        rng.randint(1, min(2, gpu_count)),
                    ),
                    "mode": record_mode,
                    "start_at": start.isoformat(),
                    "end_at": end.isoformat(),
                    "status": "active",
                    "share_units": (
                        capacity
                        if record_mode == MODE_EXCLUSIVE
                        else rng.randint(1, capacity)
                    ),
                    "_start": start,
                    "_end": end,
                }
                records.append(record)
                ledger_records.append(
                    {key: value for key, value in record.items() if not key.startswith("_")}
                )

            count = rng.randint(1, gpu_count)
            mode = MODE_EXCLUSIVE if rng.random() < 0.3 else MODE_SHARED
            units = capacity if mode == MODE_EXCLUSIVE else rng.randint(1, capacity)
            earliest = BASE + timedelta(
                minutes=rng.randint(-2, 35),
                seconds=rng.choice((0, 17, 59)),
            )
            duration = timedelta(minutes=config.slot_minutes * rng.randint(1, 12))
            preferred = (
                sorted(rng.sample(range(gpu_count), count))
                if rng.random() < 0.3
                else None
            )
            active_records = [record for record in records if record["_end"] > NOW]
            expected = _oracle_start(
                active_records,
                config,
                count,
                earliest,
                duration,
                mode,
                preferred,
                units,
            )

            with mock.patch("bk.scheduler.utc_now", return_value=NOW):
                actual = find_earliest_slot(
                    {"version": 1, "reservations": ledger_records},
                    config,
                    count,
                    earliest,
                    duration,
                    mode,
                    2000,
                    preferred,
                    True,
                    share_units=units,
                )

            message = (
                f"case={case} slot={config.slot_minutes} GPUs={gpu_count} "
                f"capacity={capacity} count={count} mode={mode} units={units} "
                f"earliest={earliest.isoformat()} preferred={preferred}"
            )
            self.assertEqual(actual[0] if actual else None, expected, message)
            if actual is not None:
                self.assertTrue(
                    all(
                        _gpu_available(
                            active_records,
                            gpu,
                            actual[0],
                            actual[0] + duration,
                            mode,
                            capacity,
                            units,
                        )
                        for gpu in actual[1]
                    ),
                    message,
                )
                self.assertEqual(
                    [parse_iso(record["start_at"]) for record in ledger_records],
                    [record["_start"] for record in records],
                    "the optimized scheduler must not mutate input records",
                )


if __name__ == "__main__":
    unittest.main()
