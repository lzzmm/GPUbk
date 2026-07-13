import fcntl
import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bk.config import Config
from bk.joblogs import (
    MIB,
    JobLogPump,
    WorkerBusyError,
    acquire_job_worker_lease,
    cleanup_job_logs,
    job_log_path,
    job_log_root,
    read_job_log_tail,
    rotated_job_log_path,
    worker_instance_id,
    worker_instance_lease_path,
)
from bk.models import Actor


class JobLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "jobs"
        self.root.mkdir(mode=0o700)
        self.actor = Actor(os.getuid(), "current")
        self.config = Config(
            data_dir=Path(self.tmp.name) / "data",
            gpu_count=1,
            job_log_dir=self.root,
            job_log_retention_days=30,
            job_log_max_mb=1,
            job_log_total_max_mb=1,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def write_log(self, reservation_id: str, size: int, *, rotated: bool = False) -> Path:
        path = job_log_path(self.config, reservation_id)
        if rotated:
            path = rotated_job_log_path(path)
        path.write_bytes(b"x" * size)
        path.chmod(0o600)
        return path

    def test_tail_reads_rotated_then_current_segments(self):
        reservation_id = str(uuid.uuid4())
        self.write_log(reservation_id, 1, rotated=True).write_text("older-", encoding="utf-8")
        self.write_log(reservation_id, 1).write_text("newest", encoding="utf-8")

        self.assertEqual(read_job_log_tail(self.config, reservation_id, 13), "older-newest")

    def test_library_default_job_log_root_uses_the_same_xdg_state_policy(self):
        config = replace(self.config, job_log_dir=None)
        home = Path(self.tmp.name) / "home"
        with mock.patch.dict(
            "os.environ",
            {"HOME": str(home), "XDG_STATE_HOME": "relative-state"},
            clear=True,
        ):
            root = job_log_root(config)

        self.assertEqual(root, home / ".local" / "state" / "bk" / "jobs")

    def test_worker_lease_is_exclusive_and_released_without_deleting_metadata(self):
        first = acquire_job_worker_lease(self.config, self.actor, "worker-1", "host-a")
        try:
            with self.assertRaisesRegex(WorkerBusyError, "another worker"):
                acquire_job_worker_lease(self.config, self.actor, "worker-2", "host-a")
            self.assertEqual((self.root / "worker.lock").stat().st_mode & 0o777, 0o600)
            self.assertEqual(first.instance_path.stat().st_mode & 0o777, 0o600)
        finally:
            first.release()

        second = acquire_job_worker_lease(self.config, self.actor, "worker-3", "host-a")
        second.release()
        payload = (self.root / "worker.lock").read_text(encoding="utf-8")
        self.assertIn('"worker_id": "worker-3"', payload)
        self.assertIn(f'"instance_id": "{worker_instance_id(self.config)}"', payload)

    def test_worker_instance_id_is_path_normalized_and_data_directory_specific(self):
        equivalent = replace(
            self.config,
            data_dir=self.config.data_dir.parent / "other" / ".." / "data",
        )
        different = replace(self.config, data_dir=self.config.data_dir.parent / "data-b")

        self.assertEqual(worker_instance_id(self.config), worker_instance_id(equivalent))
        self.assertNotEqual(worker_instance_id(self.config), worker_instance_id(different))
        self.assertEqual(len(worker_instance_id(self.config)), 64)

    def test_worker_lease_rejects_symbolic_link_redirection(self):
        target = self.root.parent / "outside-worker-lock"
        target.write_text("untouched", encoding="utf-8")
        (self.root / "worker.lock").symlink_to(target)

        with self.assertRaises(OSError):
            acquire_job_worker_lease(self.config, self.actor, "worker-1", "host-a")

        self.assertEqual(target.read_text(encoding="utf-8"), "untouched")

    def test_worker_lease_retries_a_transient_status_probe(self):
        real_flock = fcntl.flock
        attempts = 0

        def transient_probe(fd, operation):
            nonlocal attempts
            if operation == (fcntl.LOCK_EX | fcntl.LOCK_NB):
                attempts += 1
                if attempts == 1:
                    raise BlockingIOError("transient status probe")
            return real_flock(fd, operation)

        with mock.patch("bk.joblogs.fcntl.flock", side_effect=transient_probe):
            lease = acquire_job_worker_lease(
                self.config,
                self.actor,
                "worker-after-probe",
                "host-a",
            )
            lease.release()

        path = self.root / "worker.lock"
        self.assertEqual(attempts, 3)
        self.assertIn(
            '"worker_id": "worker-after-probe"',
            path.read_text(encoding="utf-8"),
        )

    def test_occupied_instance_lease_releases_global_lease_after_failure(self):
        instance_path = worker_instance_lease_path(self.config)
        instance_path.touch(mode=0o600)
        instance_path.chmod(0o600)
        instance_fd = os.open(instance_path, os.O_RDWR)
        fcntl.flock(instance_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaisesRegex(WorkerBusyError, "another worker"):
                acquire_job_worker_lease(self.config, self.actor, "blocked", "host-a")
        finally:
            fcntl.flock(instance_fd, fcntl.LOCK_UN)
            os.close(instance_fd)

        recovered = acquire_job_worker_lease(self.config, self.actor, "recovered", "host-a")
        recovered.release()

    def test_worker_lease_short_write_releases_both_kernel_locks(self):
        with mock.patch("bk.joblogs.os.write", return_value=0):
            with self.assertRaisesRegex(OSError, "short write"):
                acquire_job_worker_lease(self.config, self.actor, "failed", "host-a")

        recovered = acquire_job_worker_lease(self.config, self.actor, "recovered", "host-a")
        recovered.release()

    def test_instance_close_failure_still_releases_global_worker_lock(self):
        lease = acquire_job_worker_lease(self.config, self.actor, "first", "host-a")
        instance_fd = lease.instance_fd
        real_close = os.close

        def close_then_fail(fd):
            real_close(fd)
            if fd == instance_fd:
                raise OSError("reported instance close failure")

        with mock.patch("bk.joblogs.os.close", side_effect=close_then_fail):
            lease.release()

        recovered = acquire_job_worker_lease(self.config, self.actor, "recovered", "host-a")
        recovered.release()

    def test_new_job_log_propagates_directory_fsync_failure_and_closes_file(self):
        path = job_log_path(self.config, str(uuid.uuid4()))
        real_close = os.close
        with (
            mock.patch(
                "bk.joblogs.fsync_directory",
                side_effect=OSError("job log directory sync failed"),
            ),
            mock.patch("bk.joblogs.os.close", wraps=real_close) as close,
        ):
            with self.assertRaisesRegex(OSError, "job log directory sync failed"):
                JobLogPump(path, self.actor, MIB, {"event": "start"})

        close.assert_called_once()

    def test_retention_removes_old_terminal_log(self):
        reservation_id = str(uuid.uuid4())
        path = self.write_log(reservation_id, 128)
        old = datetime.now(timezone.utc) - timedelta(days=31)
        os.utime(path, (old.timestamp(), old.timestamp()))
        ledger = {
            "reservations": [
                {
                    "id": reservation_id,
                    "uid": self.actor.uid,
                    "status": "expired",
                    "end_at": old.isoformat(),
                    "job": {"status": "succeeded"},
                }
            ]
        }

        result = cleanup_job_logs(self.config, ledger, self.actor)

        self.assertEqual(result.removed, 1)
        self.assertEqual(result.bytes_removed, 128)
        self.assertFalse(path.exists())

    def test_retryable_active_job_log_is_retained_even_when_old(self):
        reservation_id = str(uuid.uuid4())
        path = self.write_log(reservation_id, 128)
        old = datetime.now(timezone.utc) - timedelta(days=31)
        os.utime(path, (old.timestamp(), old.timestamp()))
        ledger = {
            "reservations": [
                {
                    "id": reservation_id,
                    "uid": self.actor.uid,
                    "status": "active",
                    "end_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    "job": {"status": "failed"},
                }
            ]
        }

        result = cleanup_job_logs(self.config, ledger, self.actor)

        self.assertEqual(result.removed, 0)
        self.assertEqual(result.retained, 1)
        self.assertTrue(path.exists())

    def test_quota_removes_oldest_terminal_log_first(self):
        older_id = str(uuid.uuid4())
        newer_id = str(uuid.uuid4())
        older = self.write_log(older_id, 700 * 1024)
        newer = self.write_log(newer_id, 700 * 1024)
        now = datetime.now(timezone.utc)
        os.utime(older, ((now - timedelta(hours=2)).timestamp(),) * 2)
        os.utime(newer, ((now - timedelta(hours=1)).timestamp(),) * 2)
        config = replace(self.config, job_log_retention_days=0)

        result = cleanup_job_logs(config, {"reservations": []}, self.actor, now=now)

        self.assertEqual(result.removed, 1)
        self.assertFalse(older.exists())
        self.assertTrue(newer.exists())
        self.assertLessEqual(result.bytes_retained, MIB)

    def test_unsafe_log_is_reported_and_target_is_untouched(self):
        reservation_id = str(uuid.uuid4())
        target = Path(self.tmp.name) / "outside.log"
        target.write_text("do not remove", encoding="utf-8")
        job_log_path(self.config, reservation_id).symlink_to(target)

        result = cleanup_job_logs(self.config, {"reservations": []}, self.actor)

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.retained, 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "do not remove")

    def test_active_log_can_exceed_quota_but_is_never_deleted(self):
        reservation_id = str(uuid.uuid4())
        path = self.write_log(reservation_id, 2 * MIB)
        ledger = {
            "reservations": [
                {
                    "id": reservation_id,
                    "uid": self.actor.uid,
                    "status": "active",
                    "end_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    "job": {"status": "running"},
                }
            ]
        }

        result = cleanup_job_logs(self.config, ledger, self.actor)

        self.assertEqual(result.removed, 0)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.quota_excess_bytes, MIB)
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
