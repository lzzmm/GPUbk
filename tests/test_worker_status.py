import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path

from bk.config import Config
from bk.joblogs import acquire_job_worker_lease, worker_instance_id
from bk.models import (
    JOB_CANCELLED,
    JOB_CLAIMED,
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_MISSED,
    JOB_PENDING,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    JOB_TIMED_OUT,
    JOB_UNCERTAIN,
    Actor,
)
from bk.worker_status import (
    MAX_WORKER_LEASE_BYTES,
    inspect_worker_status,
    reservations_need_worker,
)


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

    def release_instance_lease(self, lease) -> None:
        fd = lease.instance_fd
        lease.instance_fd = -1
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def test_absent_lease_is_not_seen_and_does_not_create_storage(self):
        status = inspect_worker_status(self.config, self.actor)

        self.assertEqual(status["schema_version"], "gpubk.worker.v1")
        self.assertEqual(status["state"], "not-seen")
        self.assertFalse(status["running"])
        self.assertFalse(status["lease_present"])
        self.assertFalse(status["lease_held"])
        self.assertIsNone(status["instance_match"])
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
        self.assertTrue(running["lease_held"])
        self.assertTrue(running["instance_lease_held"])
        self.assertTrue(running["instance_match"])
        self.assertEqual(running["evidence"], "kernel-flock")
        self.assertTrue(running["metadata_valid"])
        self.assertEqual(running["lease"]["worker_id"], "worker-1")
        self.assertEqual(running["lease"]["hostname"], "host-a")
        self.assertEqual(stopped["state"], "stopped")
        self.assertFalse(stopped["running"])
        self.assertFalse(stopped["lease_held"])
        self.assertIsNone(stopped["instance_lease_held"])
        self.assertIsNone(stopped["instance_match"])
        self.assertEqual(stopped["lease"], running["lease"])

    def test_running_worker_for_another_data_directory_is_not_ready(self):
        lease = acquire_job_worker_lease(self.config, self.actor, "worker-1", "host-a")
        other = Config(
            data_dir=self.base / "other-data",
            gpu_count=1,
            job_log_dir=self.root,
        )
        try:
            status = inspect_worker_status(other, self.actor)
        finally:
            lease.release()

        self.assertEqual(status["state"], "other-instance")
        self.assertFalse(status["running"])
        self.assertTrue(status["lease_held"])
        self.assertFalse(status["instance_lease_held"])
        self.assertTrue(status["metadata_valid"])
        self.assertFalse(status["instance_match"])
        self.assertIn("another GPUBK data directory", status["warning"])

    def test_matching_instance_lock_is_authoritative_over_stale_valid_metadata(self):
        lease = acquire_job_worker_lease(self.config, self.actor, "worker-1", "host-a")
        other = Config(
            data_dir=self.base / "other-data",
            gpu_count=1,
            job_log_dir=self.root,
        )
        path = self.root / "worker.lock"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["instance_id"] = worker_instance_id(other)
        raw = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            os.ftruncate(lease.fd, 0)
            os.lseek(lease.fd, 0, os.SEEK_SET)
            os.write(lease.fd, raw)
            status = inspect_worker_status(self.config, self.actor)
        finally:
            lease.release()

        self.assertEqual(status["state"], "running")
        self.assertTrue(status["running"])
        self.assertTrue(status["lease_held"])
        self.assertTrue(status["instance_lease_held"])
        self.assertTrue(status["instance_match"])
        self.assertTrue(status["metadata_valid"])
        self.assertIsNone(status["warning"])

    def test_running_legacy_worker_is_unverified_until_restarted(self):
        lease = acquire_job_worker_lease(self.config, self.actor, "worker-1", "host-a")
        payload = json.loads((self.root / "worker.lock").read_text(encoding="utf-8"))
        payload.pop("instance_id")
        raw = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            self.release_instance_lease(lease)
            os.ftruncate(lease.fd, 0)
            os.lseek(lease.fd, 0, os.SEEK_SET)
            os.write(lease.fd, raw)
            status = inspect_worker_status(self.config, self.actor)
        finally:
            lease.release()

        self.assertEqual(status["state"], "unverified")
        self.assertIsNone(status["running"])
        self.assertTrue(status["lease_held"])
        self.assertTrue(status["metadata_valid"])
        self.assertIsNone(status["instance_match"])
        self.assertIn("restart the worker", status["warning"])

    def test_malformed_instance_binding_cannot_prove_worker_readiness(self):
        lease = acquire_job_worker_lease(self.config, self.actor, "worker-1", "host-a")
        payload = json.loads((self.root / "worker.lock").read_text(encoding="utf-8"))
        payload["instance_id"] = "not-a-digest"
        raw = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            self.release_instance_lease(lease)
            os.ftruncate(lease.fd, 0)
            os.lseek(lease.fd, 0, os.SEEK_SET)
            os.write(lease.fd, raw)
            status = inspect_worker_status(self.config, self.actor)
        finally:
            lease.release()

        self.assertEqual(status["state"], "unverified")
        self.assertIsNone(status["running"])
        self.assertTrue(status["lease_held"])
        self.assertFalse(status["metadata_valid"])
        self.assertIsNone(status["instance_match"])
        self.assertIn("lowercase SHA-256", status["warning"])

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

    def test_stopped_legacy_metadata_does_not_request_a_worker_restart(self):
        lease = acquire_job_worker_lease(self.config, self.actor, "worker-1", "host-a")
        lease.release()
        path = self.root / "worker.lock"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("instance_id")
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        status = inspect_worker_status(self.config, self.actor)

        self.assertEqual(status["state"], "stopped")
        self.assertFalse(status["running"])
        self.assertTrue(status["metadata_valid"])
        self.assertIsNone(status["warning"])

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
        self.assertTrue(running["lease_held"])
        self.assertTrue(running["instance_lease_held"])
        self.assertFalse(running["metadata_valid"])
        self.assertTrue(running["instance_match"])
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

    def test_redirected_instance_lease_cannot_prove_readiness(self):
        lease = acquire_job_worker_lease(self.config, self.actor, "worker-1", "host-a")
        target = self.base / "outside-instance"
        target.write_text("private", encoding="utf-8")
        lease.instance_path.unlink()
        lease.instance_path.symlink_to(target)
        try:
            status = inspect_worker_status(self.config, self.actor)
        finally:
            lease.release()

        self.assertEqual(status["state"], "unverified")
        self.assertIsNone(status["running"])
        self.assertTrue(status["lease_held"])
        self.assertIsNone(status["instance_lease_held"])
        self.assertIsNone(status["instance_match"])
        self.assertIn("cannot safely inspect worker instance lease", status["warning"])
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

    def test_only_nonterminal_current_uid_jobs_require_a_worker(self):
        def item(status, *, uid=self.actor.uid):
            return {"uid": uid, "job": {"status": status}}

        for status in (JOB_PENDING, JOB_CLAIMED, JOB_RUNNING, "future-state", None):
            with self.subTest(status=status):
                self.assertTrue(reservations_need_worker([item(status)], self.actor.uid))

        for status in (
            JOB_SUCCEEDED,
            JOB_FAILED,
            JOB_CANCELLED,
            JOB_MISSED,
            JOB_TIMED_OUT,
            JOB_INTERRUPTED,
            JOB_UNCERTAIN,
        ):
            with self.subTest(status=status):
                self.assertFalse(reservations_need_worker([item(status)], self.actor.uid))

        self.assertFalse(reservations_need_worker([{"uid": self.actor.uid}], self.actor.uid))
        self.assertFalse(
            reservations_need_worker([item(JOB_PENDING, uid=self.actor.uid + 1)], self.actor.uid)
        )


if __name__ == "__main__":
    unittest.main()
