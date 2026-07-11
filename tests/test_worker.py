import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bk.config import Config
from bk.models import Actor, BookingError, BookingRequest
from bk.scheduler import add_booking, cancel_booking
from bk.storage import LedgerStore
from bk.timeparse import to_iso, utc_now
from bk.worker import (
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
        self.assertEqual(by_id[other["id"]]["job"]["status"], "pending")
        log = job_log_path(self.config, mine["id"]).read_text(encoding="utf-8")
        output = json.loads(log.splitlines()[-1])
        self.assertEqual(output["cuda"], "0")
        self.assertEqual(output["rid"], mine["id"])

    def test_launch_failure_is_persisted_without_shell_fallback(self):
        marker = self.work_dir / "must-not-exist"
        reservation = self.booking(command=[f"missing-command;touch {marker}"])

        summary = run_worker(self.config, self.store, self.actor, once=True, poll_seconds=0.1, quiet=True)

        stored = next(item for item in self.store.load()["reservations"] if item["id"] == reservation["id"])
        self.assertEqual(summary.failed, 1)
        self.assertEqual(stored["job"]["status"], "failed")
        self.assertFalse(marker.exists())

    def test_cancelled_pending_job_is_never_executed(self):
        marker = self.work_dir / "not-run"
        reservation = self.booking(command=[sys.executable, "-c", f"open({str(marker)!r}, 'w').write('bad')"])
        cancel_booking(self.store, reservation["id"], self.actor)

        summary = run_worker(self.config, self.store, self.actor, once=True, poll_seconds=0.1, quiet=True)

        self.assertEqual(summary.started, 0)
        self.assertFalse(marker.exists())

    def test_running_job_is_terminated_at_reservation_deadline(self):
        reservation = self.booking(command=[sys.executable, "-c", "import time; time.sleep(30)"])

        def shorten(ledger):
            item = next(value for value in ledger["reservations"] if value["id"] == reservation["id"])
            item["end_at"] = to_iso(utc_now() + timedelta(seconds=1))
            return ledger, None, [], True

        self.store.transaction(shorten)
        summary = run_worker(self.config, self.store, self.actor, once=True, poll_seconds=0.1, quiet=True)

        stored = next(item for item in self.store.load()["reservations"] if item["id"] == reservation["id"])
        self.assertEqual(summary.failed, 1)
        self.assertEqual(stored["job"]["status"], "timed-out")

    def test_stale_claim_becomes_uncertain_instead_of_running_twice(self):
        reservation = self.booking()
        first = claim_due_jobs(
            self.store,
            self.actor,
            utc_now(),
            worker_id="dead-worker",
            runner_host="host",
            runner_pid=999999,
            claim_timeout_seconds=1,
            limit=1,
        )
        self.assertEqual(len(first), 1)

        second = claim_due_jobs(
            self.store,
            self.actor,
            utc_now() + timedelta(seconds=2),
            worker_id="new-worker",
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
