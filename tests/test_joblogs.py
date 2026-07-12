import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bk.config import Config
from bk.joblogs import (
    MIB,
    cleanup_job_logs,
    job_log_path,
    read_job_log_tail,
    rotated_job_log_path,
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
