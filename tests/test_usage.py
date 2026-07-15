import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bk.gpu import GpuProcessSnapshot, GpuSnapshot
from bk.usage import (
    CONTAINER_IDENTITY_AMBIGUOUS,
    CONTAINER_IDENTITY_INFERRED,
    USAGE_AUTHORIZED,
    USAGE_UNKNOWN,
    USAGE_UNRESERVED,
    USAGE_WRONG_GPU,
    USAGE_SYSTEM,
    classify_process_usage,
    summarize_process_command,
    _uid_can_access_docker_socket,
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

    def test_known_display_service_is_system_usage_not_violation(self):
        process = GpuProcessSnapshot(47520, 0, "root", "/usr/lib/xorg/Xorg vt1")
        snapshots = [GpuSnapshot(0, "sim", processes=(process,))]

        row = classify_process_usage(snapshots, [], self.now)[0][0]

        self.assertEqual(row.status, USAGE_SYSTEM)
        self.assertFalse(row.violation)

    def test_arbitrary_root_compute_process_is_not_implicitly_exempt(self):
        process = GpuProcessSnapshot(99, 0, "root", "python root-training.py")
        snapshots = [GpuSnapshot(0, "sim", processes=(process,))]

        row = classify_process_usage(snapshots, [], self.now)[0][0]

        self.assertEqual(row.status, USAGE_UNRESERVED)
        self.assertTrue(row.violation)

    def test_root_docker_process_is_inferred_for_one_eligible_reserved_uid(self):
        process = GpuProcessSnapshot(
            99,
            0,
            "root",
            "python train.py",
            container_runtime="docker",
            container_id="a" * 64,
            host_uid=0,
        )
        snapshots = [GpuSnapshot(0, "sim", processes=(process,))]
        current = reservation(
            "alice-container",
            1001,
            0,
            self.now - timedelta(minutes=5),
            self.now + timedelta(minutes=5),
        )

        row = classify_process_usage(
            snapshots,
            [current],
            self.now,
            container_uid_allowed=lambda runtime, uid: runtime == "docker" and uid == 1001,
        )[0][0]

        self.assertEqual(row.status, USAGE_AUTHORIZED)
        self.assertEqual(row.process.uid, 1001)
        self.assertEqual(row.process.host_uid, 0)
        self.assertEqual(row.process.identity_source, CONTAINER_IDENTITY_INFERRED)
        self.assertEqual(row.reservation_ids, ("alice-container",))

    def test_root_docker_process_stays_unknown_for_multiple_eligible_users(self):
        process = GpuProcessSnapshot(
            99,
            0,
            "root",
            "python train.py",
            container_runtime="docker",
            container_id="b" * 64,
            host_uid=0,
        )
        snapshots = [GpuSnapshot(0, "sim", processes=(process,))]
        reservations = [
            reservation("alice", 1001, 0, self.now - timedelta(minutes=5), self.now + timedelta(minutes=5)),
            reservation("bob", 1002, 0, self.now - timedelta(minutes=5), self.now + timedelta(minutes=5)),
        ]

        row = classify_process_usage(
            snapshots,
            reservations,
            self.now,
            container_uid_allowed=lambda runtime, uid: True,
        )[0][0]

        self.assertEqual(row.status, USAGE_UNKNOWN)
        self.assertFalse(row.violation)
        self.assertEqual(row.process.uid, 0)
        self.assertEqual(row.process.identity_source, CONTAINER_IDENTITY_AMBIGUOUS)

    def test_root_docker_process_is_not_inferred_for_ineligible_user(self):
        process = GpuProcessSnapshot(
            99,
            0,
            "root",
            "python train.py",
            container_runtime="docker",
            container_id="c" * 64,
        )
        snapshots = [GpuSnapshot(0, "sim", processes=(process,))]
        current = reservation(
            "alice",
            1001,
            0,
            self.now - timedelta(minutes=5),
            self.now + timedelta(minutes=5),
        )

        row = classify_process_usage(
            snapshots,
            [current],
            self.now,
            container_uid_allowed=lambda runtime, uid: False,
        )[0][0]

        self.assertEqual(row.status, USAGE_UNRESERVED)
        self.assertEqual(row.process.uid, 0)

    def test_docker_socket_group_membership_controls_inference_eligibility(self):
        socket_path = Mock()
        socket_path.stat.return_value = SimpleNamespace(st_mode=0o140660, st_gid=138)
        account = SimpleNamespace(pw_gid=1001, pw_name="alice")
        docker_group = SimpleNamespace(gr_mem=["alice"])

        with patch("bk.usage.pwd.getpwuid", return_value=account), patch(
            "bk.usage.grp.getgrgid",
            return_value=docker_group,
        ):
            self.assertTrue(_uid_can_access_docker_socket(1001, socket_path))

        docker_group.gr_mem = []
        with patch("bk.usage.pwd.getpwuid", return_value=account), patch(
            "bk.usage.grp.getgrgid",
            return_value=docker_group,
        ):
            self.assertFalse(_uid_can_access_docker_socket(1001, socket_path))

    def test_world_writable_docker_socket_allows_any_uid_candidate(self):
        socket_path = Mock()
        socket_path.stat.return_value = SimpleNamespace(st_mode=0o140666, st_gid=138)

        self.assertTrue(_uid_can_access_docker_socket(1001, socket_path))

    def test_process_command_summary_does_not_expose_arbitrary_arguments(self):
        secret = "super-secret-token"

        summary = summarize_process_command(f"/usr/bin/python3 /home/alice/train.py --token {secret}")

        self.assertEqual(summary, "python3 train.py")
        self.assertNotIn(secret, summary)


if __name__ == "__main__":
    unittest.main()
