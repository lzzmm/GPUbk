import unittest
from datetime import datetime, timedelta, timezone

from bk.gpu import GpuProcessSnapshot, GpuSnapshot
from bk.usage import (
    USAGE_AUTHORIZED,
    USAGE_UNKNOWN,
    USAGE_UNRESERVED,
    USAGE_WRONG_GPU,
    classify_process_usage,
)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def reservation(rid, uid, gpu, start, end):
    return {
        "id": rid,
        "uid": uid,
        "username": f"user{uid}",
        "gpus": [gpu],
        "mode": "shared",
        "start_at": iso(start),
        "end_at": iso(end),
        "status": "active",
    }


class UsageClassificationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)

    def test_processes_are_mapped_to_current_reservations_by_uid_and_gpu(self):
        processes = (
            GpuProcessSnapshot(1, 1001, "alice", "python a.py"),
            GpuProcessSnapshot(2, 1002, "bob", "python b.py"),
            GpuProcessSnapshot(3, 1003, "carol", "python c.py"),
            GpuProcessSnapshot(4, None, "?", "python hidden.py"),
        )
        snapshots = [GpuSnapshot(0, "sim", processes=processes, source="simulation")]
        reservations = [
            reservation("alice-ok", 1001, 0, self.now - timedelta(minutes=5), self.now + timedelta(minutes=5)),
            reservation("bob-other", 1002, 1, self.now - timedelta(minutes=5), self.now + timedelta(minutes=5)),
        ]

        rows = classify_process_usage(snapshots, reservations, self.now)[0]
        by_pid = {row.process.pid: row for row in rows}

        self.assertEqual(by_pid[1].status, USAGE_AUTHORIZED)
        self.assertEqual(by_pid[1].reservation_ids, ("alice-ok",))
        self.assertEqual(by_pid[2].status, USAGE_WRONG_GPU)
        self.assertEqual(by_pid[3].status, USAGE_UNRESERVED)
        self.assertEqual(by_pid[4].status, USAGE_UNKNOWN)
        self.assertTrue(by_pid[2].violation)
        self.assertFalse(by_pid[4].violation)

    def test_future_reservation_does_not_authorize_current_process(self):
        process = GpuProcessSnapshot(1, 1001, "alice", "python a.py")
        snapshots = [GpuSnapshot(0, "sim", processes=(process,))]
        future = reservation("future", 1001, 0, self.now + timedelta(minutes=5), self.now + timedelta(minutes=10))

        row = classify_process_usage(snapshots, [future], self.now)[0][0]

        self.assertEqual(row.status, USAGE_UNRESERVED)


if __name__ == "__main__":
    unittest.main()
