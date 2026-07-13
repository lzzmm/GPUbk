import os
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bk.config import Config
from bk.job_recovery import (
    ManagedProcessGroup,
    ProcessDiscovery,
    active_legacy_job_count,
    discover_managed_process_groups,
    recover_abandoned_jobs,
)
from bk.models import Actor, BookingRequest
from bk.scheduler import add_booking
from bk.storage import LedgerStore
from bk.timeparse import to_iso


def floor_5m(value):
    return datetime.fromtimestamp(int(value.timestamp()) // 300 * 300, timezone.utc)


class JobRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = Config(data_dir=self.root / "data", gpu_count=2)
        self.store = LedgerStore(self.config.data_dir)
        self.actor = Actor(os.getuid(), "current")
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def abandoned_job(
        self,
        *,
        status="running",
        lease_id="old-worker",
        runner_host=None,
        end_at=None,
    ):
        reservation = add_booking(
            self.store,
            self.config,
            BookingRequest(
                actor=self.actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=floor_5m(self.now),
                job_spec_id=str(uuid.uuid4()),
                job_digest="0" * 64,
                job_summary="python train.py",
            ),
        ).reservation

        def mutate(ledger):
            item = next(value for value in ledger["reservations"] if value["id"] == reservation["id"])
            effective_end = (end_at or (self.now + timedelta(minutes=30))).replace(
                microsecond=0
            )
            if effective_end <= floor_5m(self.now):
                item["start_at"] = to_iso(effective_end - timedelta(minutes=30))
            item["end_at"] = to_iso(effective_end)
            item["job"].update(
                {
                    "status": status,
                    "worker_id": "old-worker",
                    "runner_host": runner_host or socket.gethostname(),
                    "runner_pid": 999999,
                }
            )
            if lease_id is None:
                item["job"].pop("worker_lease_id", None)
            else:
                item["job"]["worker_lease_id"] = lease_id
            return ledger, None, [], True

        self.store.transaction(mutate)
        return reservation

    def stored(self, reservation_id):
        return next(
            item for item in self.store.load()["reservations"] if item["id"] == reservation_id
        )

    def test_missing_abandoned_process_becomes_uncertain_without_retry(self):
        reservation = self.abandoned_job()

        summary = recover_abandoned_jobs(
            self.store,
            self.actor,
            hostname=socket.gethostname(),
            worker_id="new-worker",
            now=self.now,
            grace_seconds=0.1,
            discoverer=lambda _reservation_id, _uid: ProcessDiscovery(True),
        )

        job = self.stored(reservation["id"])["job"]
        self.assertEqual(summary.uncertain_jobs, 1)
        self.assertEqual(summary.terminated_groups, 0)
        self.assertEqual(job["status"], "uncertain")
        self.assertEqual(job["recovery_state"], "not-found")
        self.assertIn("not retried automatically", job["message"])

    def test_verified_process_group_is_terminated_and_stays_uncertain(self):
        reservation = self.abandoned_job()
        env = {**os.environ, "BK_RESERVATION_ID": reservation["id"]}
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=env,
            start_new_session=True,
        )

        def discover(_reservation_id, _uid):
            if process.poll() is None:
                return ProcessDiscovery(
                    True,
                    (ManagedProcessGroup(process.pid, process.pid, "test-start"),),
                )
            return ProcessDiscovery(True)

        try:
            summary = recover_abandoned_jobs(
                self.store,
                self.actor,
                hostname=socket.gethostname(),
                worker_id="new-worker",
                now=self.now,
                grace_seconds=0.2,
                discoverer=discover,
            )
            process.wait(timeout=2)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)

        job = self.stored(reservation["id"])["job"]
        self.assertEqual(summary.terminated_groups, 1)
        self.assertEqual(job["status"], "uncertain")
        self.assertEqual(job["recovery_state"], "terminated")
        self.assertIn("prior completion remains uncertain", job["message"])

    def test_remote_abandoned_job_is_never_signalled_locally(self):
        reservation = self.abandoned_job(runner_host="another-host")

        def must_not_discover(_reservation_id, _uid):
            raise AssertionError("remote recovery must not inspect or signal local processes")

        summary = recover_abandoned_jobs(
            self.store,
            self.actor,
            hostname=socket.gethostname(),
            worker_id="new-worker",
            now=self.now,
            grace_seconds=0.1,
            discoverer=must_not_discover,
        )

        job = self.stored(reservation["id"])["job"]
        self.assertEqual(summary.remote_jobs, 1)
        self.assertEqual(job["status"], "uncertain")
        self.assertEqual(job["recovery_state"], "remote-unverified")

        recover_abandoned_jobs(
            self.store,
            self.actor,
            hostname="another-host",
            worker_id="worker-on-original-host",
            now=self.now,
            grace_seconds=0.1,
            discoverer=lambda _reservation_id, _uid: ProcessDiscovery(True),
        )
        self.assertEqual(
            self.stored(reservation["id"])["job"]["recovery_state"],
            "not-found",
        )

    def test_process_identity_is_rechecked_before_any_signal(self):
        reservation = self.abandoned_job()
        calls = 0

        def disappears(_reservation_id, _uid):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ProcessDiscovery(
                    True,
                    (ManagedProcessGroup(424242, 424242, "old-start"),),
                )
            return ProcessDiscovery(True)

        with mock.patch("bk.job_recovery.os.killpg") as killpg:
            summary = recover_abandoned_jobs(
                self.store,
                self.actor,
                hostname=socket.gethostname(),
                worker_id="new-worker",
                now=self.now,
                grace_seconds=0.1,
                discoverer=disappears,
            )

        job = self.stored(reservation["id"])["job"]
        killpg.assert_not_called()
        self.assertEqual(summary.terminated_groups, 0)
        self.assertEqual(job["recovery_state"], "disappeared")

    def test_active_legacy_job_is_left_for_the_prelease_worker(self):
        reservation = self.abandoned_job(lease_id=None)

        summary = recover_abandoned_jobs(
            self.store,
            self.actor,
            hostname=socket.gethostname(),
            worker_id="new-worker",
            now=self.now,
            grace_seconds=0.1,
            discoverer=lambda _reservation_id, _uid: ProcessDiscovery(True),
        )

        self.assertEqual(summary.legacy_jobs, 1)
        self.assertEqual(summary.uncertain_jobs, 0)
        self.assertEqual(self.stored(reservation["id"])["job"]["status"], "running")
        self.assertEqual(active_legacy_job_count(self.store.load(), self.actor, self.now), 1)

    def test_expired_legacy_job_no_longer_stays_running_forever(self):
        self.now = floor_5m(self.now) + timedelta(minutes=1, microseconds=500_000)
        reservation = self.abandoned_job(
            lease_id=None,
            end_at=self.now - timedelta(minutes=1),
        )

        summary = recover_abandoned_jobs(
            self.store,
            self.actor,
            hostname=socket.gethostname(),
            worker_id="new-worker",
            now=self.now,
            grace_seconds=0.1,
            discoverer=lambda _reservation_id, _uid: ProcessDiscovery(True),
        )

        stored = self.stored(reservation["id"])
        self.assertEqual(summary.legacy_jobs, 1)
        self.assertEqual(stored["status"], "expired")
        self.assertEqual(stored["job"]["status"], "uncertain")
        self.assertEqual(stored["job"]["recovery_state"], "legacy-not-found")

    def test_expired_legacy_job_can_terminate_a_verified_process_group(self):
        reservation = self.abandoned_job(
            lease_id=None,
            end_at=self.now - timedelta(minutes=1),
        )
        calls = 0

        def discover(_reservation_id, _uid):
            nonlocal calls
            calls += 1
            if calls <= 2:
                return ProcessDiscovery(
                    True,
                    (ManagedProcessGroup(515151, 515151, "legacy-start"),),
                )
            return ProcessDiscovery(True)

        with mock.patch("bk.job_recovery.os.killpg") as killpg:
            summary = recover_abandoned_jobs(
                self.store,
                self.actor,
                hostname=socket.gethostname(),
                worker_id="new-worker",
                now=self.now,
                grace_seconds=0.1,
                discoverer=discover,
            )

        killpg.assert_called_once_with(515151, signal.SIGTERM)
        self.assertEqual(summary.terminated_groups, 1)
        self.assertEqual(
            self.stored(reservation["id"])["job"]["recovery_state"],
            "terminated",
        )

    def test_proc_discovery_matches_exact_uid_owned_reservation_marker(self):
        proc_root = self.root / "proc"
        process_dir = proc_root / "321"
        process_dir.mkdir(parents=True)
        pgid = os.getpgrp() + 1000
        (process_dir / "stat").write_text(
            f"321 (python train.py) S 1 {pgid} 0 0 0\n",
            encoding="utf-8",
        )
        (process_dir / "environ").write_bytes(
            b"OTHER=value\0BK_RESERVATION_ID=reservation-123\0"
        )

        found = discover_managed_process_groups(
            "reservation-123",
            self.actor.uid,
            proc_root=proc_root,
        )
        missing = discover_managed_process_groups(
            "reservation-12",
            self.actor.uid,
            proc_root=proc_root,
        )

        self.assertTrue(found.available)
        self.assertEqual([item.pgid for item in found.groups], [pgid])
        self.assertEqual(missing.groups, ())


if __name__ == "__main__":
    unittest.main()
