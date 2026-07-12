import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import bk.worker as worker_module
from bk.config import Config
from bk.gpu import GpuProcessSnapshot, GpuSnapshot
from bk.joblogs import MIB, job_log_paths, read_job_log_tail
from bk.models import Actor, BookingError, BookingRequest
from bk.scheduler import add_booking, cancel_booking
from bk.storage import LedgerStore
from bk.timeparse import utc_now
from bk.worker import (
    cleanup_job_specs,
    claim_due_jobs,
    job_log_path,
    job_spec_path,
    prepare_job_spec,
    retry_job,
    run_worker,
)


def floor_5m(value):
    timestamp = int(value.timestamp())
    return datetime.fromtimestamp(timestamp - timestamp % 300, timezone.utc)


class ScheduledJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        self.work_dir = Path(self.tmp.name) / "work"
        self.log_dir = Path(self.tmp.name) / "logs"
        self.work_dir.mkdir()
        self.config = Config(
            data_dir=self.data_dir,
            gpu_count=1,
            max_shared_users=2,
            job_log_dir=self.log_dir,
            worker_poll_seconds=0.1,
            worker_claim_timeout_seconds=1,
            worker_live_guard=False,
        )
        self.store = LedgerStore(self.data_dir)
        self.actor = Actor(os.getuid(), "current")
        self.start = floor_5m(utc_now())

    def tearDown(self):
        self.tmp.cleanup()

    def booking(self, actor=None, command=None):
        actor = actor or self.actor
        command = command or [sys.executable, "-c", "print('ok')"]
        if actor.uid == self.actor.uid:
            spec = prepare_job_spec(self.config, actor, command, str(self.work_dir))
            spec_id, digest, summary = spec.spec_id, spec.digest, spec.summary
        else:
            spec_id, digest, summary = "00000000-0000-0000-0000-000000000001", "0" * 64, "private job"
        return add_booking(
            self.store,
            self.config,
            BookingRequest(
                actor=actor,
                count=1,
                duration_seconds=10 * 60,
                start_at=self.start,
                preferred_gpus=[0],
                job_spec_id=spec_id,
                job_digest=digest,
                job_summary=summary,
            ),
        ).reservation

    def test_worker_executes_only_current_uid_and_injects_gpu_environment(self):
        command = [
            sys.executable,
            "-c",
            "import json,os; print(json.dumps({'cuda': os.environ['CUDA_VISIBLE_DEVICES'], "
            "'rid': os.environ['BK_RESERVATION_ID']}))",
        ]
        mine = self.booking(command=command)
        other = self.booking(actor=Actor(self.actor.uid + 1, "other"))

        summary = run_worker(self.config, self.store, self.actor, once=True, poll_seconds=0.1, quiet=True)

        ledger = self.store.load()
        by_id = {item["id"]: item for item in ledger["reservations"]}
        self.assertEqual(summary.started, 1)
        self.assertEqual(summary.succeeded, 1)
        self.assertEqual(by_id[mine["id"]]["job"]["status"], "succeeded")
        self.assertNotIn("log_path", by_id[mine["id"]]["job"])
        self.assertEqual(by_id[other["id"]]["job"]["status"], "pending")
        log = job_log_path(self.config, mine["id"]).read_text(encoding="utf-8")
        output = json.loads(log.splitlines()[-1])
        self.assertEqual(output["cuda"], "0")
        self.assertEqual(output["rid"], mine["id"])
        self.assertFalse(job_spec_path(self.config, mine["job"]["spec_id"]).exists())

    def test_worker_rolls_high_volume_output_without_blocking_the_job(self):
        config = replace(self.config, job_log_max_mb=1)
        reservation = self.booking(
            command=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x' * 1500000); print('TAIL-MARKER')",
            ]
        )

        summary = run_worker(config, self.store, self.actor, once=True, poll_seconds=0.1, quiet=True)

        paths = job_log_paths(config, reservation["id"])
        self.assertEqual(summary.succeeded, 1)
        self.assertEqual(len(paths), 2)
        self.assertLessEqual(sum(path.stat().st_size for path in paths), MIB)
        self.assertTrue(read_job_log_tail(config, reservation["id"], 64).endswith("TAIL-MARKER\n"))

    def test_worker_tracks_same_process_group_children_after_the_leader_exits(self):
        marker = self.work_dir / "child-finished"
        child_code = f"import time; time.sleep(0.4); open({str(marker)!r}, 'w').write('done')"
        self.booking(
            command=[
                sys.executable,
                "-c",
                f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child_code!r}])",
            ]
        )

        started = time.monotonic()
        summary = run_worker(self.config, self.store, self.actor, once=True, poll_seconds=0.1, quiet=True)

        self.assertEqual(summary.succeeded, 1)
        self.assertTrue(marker.exists())
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_worker_rejects_a_symbolic_link_job_log_without_touching_its_target(self):
        marker = self.work_dir / "must-not-run"
        reservation = self.booking(
            command=[sys.executable, "-c", f"open({str(marker)!r}, 'w').write('bad')"]
        )
        target = self.work_dir / "outside-log"
        target.write_text("safe", encoding="utf-8")
        job_log_path(self.config, reservation["id"]).symlink_to(target)

        summary = run_worker(self.config, self.store, self.actor, once=True, poll_seconds=0.1, quiet=True)

        stored = next(item for item in self.store.load()["reservations"] if item["id"] == reservation["id"])
        self.assertEqual(summary.failed, 1)
        self.assertEqual(stored["job"]["status"], "failed")
        self.assertFalse(marker.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "safe")

    @unittest.skipUnless(Path("/proc").is_dir(), "Linux /proc is required for crash recovery")
    def test_new_worker_terminates_process_group_left_by_crashed_worker(self):
        reservation = self.booking(
            command=[sys.executable, "-c", "import time; time.sleep(30)"]
        )
        project_root = Path(__file__).resolve().parents[1]
        env = {
            **os.environ,
            "PYTHONPATH": str(project_root / "src"),
            "BK_DATA_DIR": str(self.data_dir),
            "BK_JOB_LOG_DIR": str(self.log_dir),
            "BK_GPU_COUNT": "1",
            "BK_WORKER_LIVE_GUARD": "0",
            "BK_WORKER_RECOVERY_GRACE_SECONDS": "0.2",
        }
        worker = subprocess.Popen(
            [sys.executable, "-m", "bk", "worker", "--quiet", "--poll", "0.1"],
            cwd=project_root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child_pid = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                stored = next(
                    item
                    for item in self.store.load()["reservations"]
                    if item["id"] == reservation["id"]
                )
                if stored["job"]["status"] == "running":
                    child_pid = int(stored["job"]["runner_pid"])
                    break
                time.sleep(0.05)
            self.assertIsNotNone(child_pid, "first worker did not start the command")
            os.kill(worker.pid, signal.SIGKILL)
            worker.wait(timeout=2)
            os.kill(child_pid, 0)

            recovered = subprocess.run(
                [sys.executable, "-m", "bk", "worker", "--once", "--quiet", "--poll", "0.1"],
                cwd=project_root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )

            stored = next(
                item
                for item in self.store.load()["reservations"]
                if item["id"] == reservation["id"]
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(stored["job"]["status"], "uncertain")
            self.assertEqual(stored["job"]["recovery_state"], "terminated")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    raw_stat = (Path("/proc") / str(child_pid) / "stat").read_text(
                        encoding="utf-8"
                    )
                except OSError:
                    break
                if raw_stat[raw_stat.rfind(")") + 1 :].split()[0] == "Z":
                    break
                time.sleep(0.05)
            else:
                self.fail("orphaned child process is still running after recovery")
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=2)
            if child_pid is not None:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_live_guard_waits_without_log_spam_then_launches_when_gpu_is_safe(self):
        marker = self.work_dir / "guard-launched"
        reservation = self.booking(
            command=[sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ok')"]
        )
        guarded = replace(self.config, worker_live_guard=True)
        busy = [
            GpuSnapshot(
                0,
                "gpu0",
                memory_used_mb=4096,
                memory_total_mb=24000,
                utilization_percent=80,
                processes=(
                    GpuProcessSnapshot(
                        4402,
                        self.actor.uid + 1,
                        "other",
                        "python rogue.py",
                        4096,
                        75,
                    ),
                ),
                source="simulation",
            )
        ]
        idle = [
            GpuSnapshot(
                0,
                "gpu0",
                memory_total_mb=24000,
                utilization_percent=0,
                source="simulation",
            )
        ]

        first = run_worker(
            guarded,
            self.store,
            self.actor,
            once=True,
            poll_seconds=0.1,
            quiet=True,
            snapshot_provider=lambda _config: busy,
        )
        second = run_worker(
            guarded,
            self.store,
            self.actor,
            once=True,
            poll_seconds=0.1,
            quiet=True,
            snapshot_provider=lambda _config: busy,
        )

        waiting = next(
            item for item in self.store.load()["reservations"] if item["id"] == reservation["id"]
        )
        self.assertEqual(first.waiting, 1)
        self.assertEqual(second.waiting, 1)
        self.assertEqual(waiting["job"]["status"], "pending")
        self.assertEqual(waiting["job"]["launch_guard_state"], "waiting")
        self.assertIn("unreserved process", waiting["job"]["message"])
        self.assertFalse(marker.exists())
        audit = self.store.log_path.read_text(encoding="utf-8")
        self.assertEqual(audit.count('"action": "job-waiting"'), 1)

        launched = run_worker(
            guarded,
            self.store,
            self.actor,
            once=True,
            poll_seconds=0.1,
            quiet=True,
            snapshot_provider=lambda _config: idle,
        )

        stored = next(
            item for item in self.store.load()["reservations"] if item["id"] == reservation["id"]
        )
        self.assertEqual(launched.succeeded, 1)
        self.assertEqual(stored["job"]["status"], "succeeded")
        self.assertNotIn("launch_guard_state", stored["job"])
        self.assertTrue(marker.exists())

    def test_launch_failure_is_persisted_without_shell_fallback(self):
        marker = self.work_dir / "must-not-exist"
        reservation = self.booking(command=[f"missing-command;touch {marker}"])

        summary = run_worker(self.config, self.store, self.actor, once=True, poll_seconds=0.1, quiet=True)

        stored = next(item for item in self.store.load()["reservations"] if item["id"] == reservation["id"])
        self.assertEqual(summary.failed, 1)
        self.assertEqual(stored["job"]["status"], "failed")
        self.assertFalse(marker.exists())
        self.assertTrue(job_spec_path(self.config, reservation["job"]["spec_id"]).exists())

    def test_launch_failure_keeps_private_paths_out_of_the_shared_ledger(self):
        secret = "private-launch-path-token"
        missing = self.work_dir / secret / "missing-command"
        reservation = self.booking(command=[str(missing)])

        run_worker(self.config, self.store, self.actor, once=True, poll_seconds=0.1, quiet=True)

        ledger_text = self.store.ledger_path.read_text(encoding="utf-8")
        stored = next(item for item in self.store.load()["reservations"] if item["id"] == reservation["id"])
        private_log = job_log_path(self.config, reservation["id"]).read_text(encoding="utf-8")
        self.assertNotIn(secret, ledger_text)
        self.assertEqual(
            stored["job"]["message"],
            "scheduled executable or working directory was not found",
        )
        self.assertIn(secret, private_log)

    def test_cancelled_pending_job_is_never_executed(self):
        marker = self.work_dir / "not-run"
        reservation = self.booking(command=[sys.executable, "-c", f"open({str(marker)!r}, 'w').write('bad')"])
        cancel_booking(self.store, reservation["id"], self.actor)

        summary = run_worker(self.config, self.store, self.actor, once=True, poll_seconds=0.1, quiet=True)

        self.assertEqual(summary.started, 0)
        self.assertFalse(marker.exists())
        self.assertFalse(job_spec_path(self.config, reservation["job"]["spec_id"]).exists())

    def test_cancellation_after_claim_prevents_popen_and_is_not_counted_as_failure(self):
        marker = self.work_dir / "claim-race-must-not-run"
        reservation = self.booking(
            command=[sys.executable, "-c", f"open({str(marker)!r}, 'w').write('bad')"]
        )

        def cancel_before_launch(store, actor, reservation_id, _claim_token):
            cancel_booking(store, reservation_id, actor)
            return False

        with mock.patch("bk.worker._claim_is_launchable", side_effect=cancel_before_launch):
            summary = run_worker(
                self.config,
                self.store,
                self.actor,
                once=True,
                poll_seconds=0.1,
                quiet=True,
            )

        stored = next(item for item in self.store.load()["reservations"] if item["id"] == reservation["id"])
        self.assertFalse(marker.exists())
        self.assertEqual(summary.cancelled, 1)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(stored["job"]["status"], "cancelled")

    def test_running_job_is_terminated_at_reservation_deadline(self):
        reservation = self.booking(command=[sys.executable, "-c", "import time; time.sleep(30)"])
        clock = {"now": utc_now()}
        deadline = datetime.fromisoformat(reservation["end_at"].replace("Z", "+00:00"))
        mark_running = worker_module._mark_running

        def mark_running_then_cross_deadline(*args, **kwargs):
            marked = mark_running(*args, **kwargs)
            if marked:
                clock["now"] = deadline + timedelta(seconds=1)
            return marked

        with (
            mock.patch("bk.worker.utc_now", side_effect=lambda: clock["now"]),
            mock.patch("bk.worker._mark_running", side_effect=mark_running_then_cross_deadline),
        ):
            summary = run_worker(
                self.config,
                self.store,
                self.actor,
                once=True,
                poll_seconds=0.1,
                quiet=True,
            )

        stored = next(item for item in self.store.load()["reservations"] if item["id"] == reservation["id"])
        self.assertEqual(summary.failed, 1)
        self.assertEqual(stored["job"]["status"], "timed-out")
        self.assertFalse(job_spec_path(self.config, reservation["job"]["spec_id"]).exists())

    def test_stale_claim_becomes_uncertain_instead_of_running_twice(self):
        reservation = self.booking()
        first = claim_due_jobs(
            self.store,
            self.actor,
            utc_now(),
            worker_id="dead-worker",
            worker_lease_id="dead-worker",
            runner_host="host",
            runner_pid=999999,
            claim_timeout_seconds=1,
            limit=1,
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["job"]["worker_lease_id"], "dead-worker")

        second = claim_due_jobs(
            self.store,
            self.actor,
            utc_now() + timedelta(seconds=2),
            worker_id="new-worker",
            worker_lease_id="new-worker",
            runner_host="host",
            runner_pid=os.getpid(),
            claim_timeout_seconds=1,
            limit=1,
        )

        stored = next(item for item in self.store.load()["reservations"] if item["id"] == reservation["id"])
        self.assertEqual(second, [])
        self.assertEqual(stored["job"]["status"], "uncertain")

        with self.assertRaisesRegex(BookingError, "may already be running"):
            retry_job(self.store, self.actor, reservation["id"])
        retried = retry_job(
            self.store,
            self.actor,
            reservation["id"],
            accept_duplicate_risk=True,
        )
        self.assertEqual(retried["job"]["status"], "pending")
        self.assertTrue(job_spec_path(self.config, reservation["job"]["spec_id"]).exists())

    def test_new_worker_waits_for_active_prelease_job_instead_of_claiming_more(self):
        reservation = self.booking()

        def mark_legacy_running(ledger):
            item = next(value for value in ledger["reservations"] if value["id"] == reservation["id"])
            item["job"]["status"] = "running"
            item["job"].pop("worker_lease_id", None)
            return ledger, None, [], True

        self.store.transaction(mark_legacy_running)

        summary = run_worker(self.config, self.store, self.actor, once=True, quiet=True)

        stored = next(item for item in self.store.load()["reservations"] if item["id"] == reservation["id"])
        self.assertEqual(summary.started, 0)
        self.assertEqual(summary.waiting, 1)
        self.assertEqual(stored["job"]["status"], "running")

    def test_cleanup_defers_fresh_orphans_then_removes_them_after_grace(self):
        spec = prepare_job_spec(
            self.config,
            self.actor,
            [sys.executable, "-c", "print('orphan')"],
            str(self.work_dir),
        )
        path = job_spec_path(self.config, spec.spec_id)

        deferred = cleanup_job_specs(self.config, self.store, self.actor)
        removed = cleanup_job_specs(
            self.config,
            self.store,
            self.actor,
            orphan_grace_seconds=0,
        )

        self.assertEqual(deferred.deferred_orphans, 1)
        self.assertEqual(deferred.removed, 0)
        self.assertEqual(removed.removed, 1)
        self.assertFalse(path.exists())

    def test_cleanup_reports_missing_active_spec_without_touching_the_ledger(self):
        reservation = self.booking()
        path = job_spec_path(self.config, reservation["job"]["spec_id"])
        path.unlink()

        result = cleanup_job_specs(self.config, self.store, self.actor)

        self.assertEqual(result.failed, 1)
        self.assertIn("missing", result.warnings[0])
        stored = next(
            item for item in self.store.load()["reservations"] if item["id"] == reservation["id"]
        )
        self.assertEqual(stored["job"]["status"], "pending")

    def test_cleanup_retains_a_spec_referenced_by_a_malformed_uid(self):
        spec = prepare_job_spec(
            self.config,
            self.actor,
            [sys.executable, "-c", "print('retain')"],
            str(self.work_dir),
        )
        path = job_spec_path(self.config, spec.spec_id)

        def add_malformed_reference(ledger):
            ledger["reservations"].append(
                {
                    "id": "malformed-uid",
                    "uid": "not-an-integer",
                    "job": {"spec_id": spec.spec_id, "status": "succeeded"},
                }
            )
            return ledger, None, [], True

        self.store.transaction(add_malformed_reference)

        result = cleanup_job_specs(
            self.config,
            self.store,
            self.actor,
            orphan_grace_seconds=0,
        )

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.retained, 1)
        self.assertIn("invalid UID", result.warnings[0])
        self.assertTrue(path.exists())

    def test_cleanup_rejects_a_symlink_spec_directory(self):
        self.log_dir.mkdir(mode=0o700)
        outside = Path(self.tmp.name) / "outside-specs"
        outside.mkdir()
        (self.log_dir / "specs").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(BookingError, "not a directory"):
            cleanup_job_specs(self.config, self.store, self.actor)

        self.assertEqual(list(outside.iterdir()), [])

    def test_job_command_requires_absolute_working_directory(self):
        with self.assertRaisesRegex(BookingError, "must be absolute"):
            prepare_job_spec(
                self.config,
                self.actor,
                ["python", "train.py"],
                "relative/path",
            )

    def test_job_spec_rejects_symbolic_link_log_directory(self):
        target = Path(self.tmp.name) / "log-target"
        target.mkdir()
        self.log_dir.symlink_to(target, target_is_directory=True)

        with self.assertRaises(NotADirectoryError):
            prepare_job_spec(
                self.config,
                self.actor,
                [sys.executable, "-c", "print('safe')"],
                str(self.work_dir),
            )

        self.assertEqual(list(target.iterdir()), [])

    def test_shared_ledger_contains_no_command_arguments_and_private_spec_is_locked_down(self):
        secret = "api-token-should-stay-private"
        reservation = self.booking(command=[sys.executable, "-c", f"print({secret!r})"])

        ledger_text = self.store.ledger_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, ledger_text)
        self.assertNotIn('"argv"', ledger_text)
        spec_path = job_spec_path(self.config, reservation["job"]["spec_id"])
        self.assertEqual(stat.S_IMODE(spec_path.stat().st_mode), 0o600)
        self.assertIn(secret, spec_path.read_text(encoding="utf-8"))

    def test_tampered_private_spec_is_rejected_before_execution(self):
        marker = self.work_dir / "tampered"
        reservation = self.booking(command=[sys.executable, "-c", "print('safe')"])
        spec_path = job_spec_path(self.config, reservation["job"]["spec_id"])
        payload = json.loads(spec_path.read_text(encoding="utf-8"))
        payload["argv"] = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('bad')"]
        spec_path.write_text(json.dumps(payload), encoding="utf-8")
        spec_path.chmod(0o600)

        summary = run_worker(self.config, self.store, self.actor, once=True, poll_seconds=0.1, quiet=True)

        stored = next(item for item in self.store.load()["reservations"] if item["id"] == reservation["id"])
        self.assertEqual(summary.failed, 1)
        self.assertEqual(stored["job"]["status"], "failed")
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
