import os
import tempfile
import unittest
from pathlib import Path

from bk.config import Config
from bk.joblogs import acquire_job_worker_lease
from bk.models import Actor
from bk.worker_status import MAX_WORKER_LEASE_BYTES, inspect_worker_status


class WorkerStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "jobs"
        self.actor = Actor(os.getuid(), "current")
        self.config = Config(
            data_dir=self.base / "data",
            gpu_count=1,
            job_log_dir=self.root,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_absent_lease_is_not_seen_and_does_not_create_storage(self):
        status = inspect_worker_status(self.config, self.actor)

        self.assertEqual(status["schema_version"], "gpubk.worker.v1")
        self.assertEqual(status["state"], "not-seen")
        self.assertFalse(status["running"])
        self.assertFalse(status["lease_present"])
        self.assertFalse(self.root.exists())

    def test_kernel_lock_reports_running_then_stopped_with_diagnostic_metadata(self):
        lease = acquire_job_worker_lease(self.config, self.actor, "worker-1", "host-a")
        try:
            running = inspect_worker_status(self.config, self.actor)
        finally:
            lease.release()
        stopped = inspect_worker_status(self.config, self.actor)

        self.assertEqual(running["state"], "running")
        self.assertTrue(running["running"])
        self.assertEqual(running["evidence"], "kernel-flock")
        self.assertTrue(running["metadata_valid"])
        self.assertEqual(running["lease"]["worker_id"], "worker-1")
        self.assertEqual(running["lease"]["hostname"], "host-a")
        self.assertEqual(stopped["state"], "stopped")
        self.assertFalse(stopped["running"])
        self.assertEqual(stopped["lease"], running["lease"])

    def test_stopped_probe_does_not_modify_lease_bytes_or_timestamps(self):
        lease = acquire_job_worker_lease(self.config, self.actor, "worker-1", "host-a")
        lease.release()
        path = self.root / "worker.lock"
        before_bytes = path.read_bytes()
        before = path.stat()

        status = inspect_worker_status(self.config, self.actor)

        after = path.stat()
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(after.st_size, before.st_size)

    def test_malformed_metadata_does_not_override_kernel_liveness(self):
        lease = acquire_job_worker_lease(self.config, self.actor, "worker-1", "host-a")
        path = self.root / "worker.lock"
        try:
            os.ftruncate(lease.fd, 0)
            os.lseek(lease.fd, 0, os.SEEK_SET)
            os.write(lease.fd, b"{broken")
            running = inspect_worker_status(self.config, self.actor)
        finally:
            lease.release()

        self.assertEqual(running["state"], "running")
        self.assertTrue(running["running"])
        self.assertFalse(running["metadata_valid"])
        self.assertIsNone(running["lease"])
        self.assertIn("invalid worker lease metadata", running["warning"])
        self.assertEqual(path.read_bytes(), b"{broken")

    def test_oversized_metadata_is_bounded_and_reported(self):
        self.root.mkdir(mode=0o700)
        path = self.root / "worker.lock"
        path.write_bytes(b"x" * (MAX_WORKER_LEASE_BYTES + 1))
        path.chmod(0o600)

        status = inspect_worker_status(self.config, self.actor)

        self.assertEqual(status["state"], "stopped")
        self.assertFalse(status["metadata_valid"])
        self.assertIn("exceeds", status["warning"])

    def test_pathologically_nested_metadata_is_reported_without_crashing(self):
        self.root.mkdir(mode=0o700)
        path = self.root / "worker.lock"
        path.write_bytes(b"[" * 1100 + b"]" * 1100)
        path.chmod(0o600)

        status = inspect_worker_status(self.config, self.actor)

        self.assertEqual(status["state"], "stopped")
        self.assertFalse(status["metadata_valid"])
        self.assertIn("invalid worker lease metadata", status["warning"])

    def test_symbolic_link_is_invalid_and_target_is_untouched(self):
        self.root.mkdir(mode=0o700)
        target = self.base / "outside"
        target.write_text("private", encoding="utf-8")
        (self.root / "worker.lock").symlink_to(target)

        status = inspect_worker_status(self.config, self.actor)

        self.assertEqual(status["state"], "invalid")
        self.assertIsNone(status["running"])
        self.assertEqual(target.read_text(encoding="utf-8"), "private")

    def test_permission_drift_and_hard_links_are_invalid(self):
        lease = acquire_job_worker_lease(self.config, self.actor, "worker-1", "host-a")
        lease.release()
        path = self.root / "worker.lock"
        path.chmod(0o644)
        mode_status = inspect_worker_status(self.config, self.actor)
        path.chmod(0o600)
        os.link(path, self.base / "worker-link")
        link_status = inspect_worker_status(self.config, self.actor)

        self.assertEqual(mode_status["state"], "invalid")
        self.assertIn("expected 0600", mode_status["warning"])
        self.assertEqual(link_status["state"], "invalid")
        self.assertIn("hard links", link_status["warning"])

    def test_other_uid_is_unavailable_without_touching_private_storage(self):
        status = inspect_worker_status(
            self.config,
            Actor(self.actor.uid + 1, "other"),
        )

        self.assertEqual(status["state"], "unavailable")
        self.assertIsNone(status["running"])
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
