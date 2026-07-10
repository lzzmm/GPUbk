import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ceil_5m(dt):
    timestamp = int(dt.timestamp())
    remainder = timestamp % 300
    if remainder:
        timestamp += 300 - remainder
    return datetime.fromtimestamp(timestamp, timezone.utc)


class CliTests(unittest.TestCase):
    def run_bk(self, args, data_dir):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["BK_DATA_DIR"] = str(data_dir)
        env["BK_GPU_COUNT"] = "1"
        env["BK_MAX_SHARED_USERS"] = "2"
        return subprocess.run(
            [sys.executable, "-m", "bk"] + args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(ROOT),
            check=False,
        )

    def run_bk_with_input(self, args, data_dir, text):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["BK_DATA_DIR"] = str(data_dir)
        env["BK_GPU_COUNT"] = "2"
        env["BK_MAX_SHARED_USERS"] = "2"
        return subprocess.run(
            [sys.executable, "-m", "bk"] + args,
            input=text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(ROOT),
            check=False,
        )

    def test_default_command_starts_plain_interactive_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk_with_input([], Path(tmp), "status\nquit\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("bk GPU booking", result.stdout)
            self.assertIn("bk> ", result.stdout)
            self.assertIn("GPU 0: unknown", result.stdout)
            self.assertIn("GPU 1: unknown", result.stdout)

    def test_plain_interactive_shell_can_create_booking(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk_with_input([], Path(tmp), "1 30m --gpu 0\nlist\nquit\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("created:", result.stdout)
            ledger = json.loads((Path(tmp) / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["reservations"][0]["mode"], "shared")

    def test_bare_count_duration_defaults_to_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["1", "30m"], Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            ledger = json.loads((Path(tmp) / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["reservations"][0]["mode"], "shared")

    def test_auto_alias_defaults_to_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["auto", "1", "30m"], Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            ledger = json.loads((Path(tmp) / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["reservations"][0]["mode"], "shared")

    def test_exclusive_command_uses_exclusive_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["exclusive", "1", "30m"], Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            ledger = json.loads((Path(tmp) / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["reservations"][0]["mode"], "exclusive")

    def test_implicit_now_shared_request_overlaps_until_record_capacity_then_queues(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            now = ceil_5m(datetime.now(timezone.utc).replace(microsecond=0))
            existing_end = now + timedelta(hours=1)
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "ledger.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "reservations": [
                            {
                                "id": "existing",
                                "op_id": "existing-op",
                                "uid": os.getuid(),
                                "username": "current",
                                "gpus": [0],
                                "mode": "shared",
                                "start_at": iso(now - timedelta(minutes=1)),
                                "end_at": iso(existing_end),
                                "status": "active",
                                "created_at": iso(now - timedelta(minutes=1)),
                                "updated_at": iso(now - timedelta(minutes=1)),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            first = self.run_bk(["1", "30m", "--gpu", "0"], data_dir)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("created:", first.stdout)
            second = self.run_bk(["1", "30m", "--gpu", "0"], data_dir)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("queued:", second.stdout)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(len(ledger["reservations"]), 3)
            self.assertEqual(ledger["reservations"][2]["start_at"], ledger["reservations"][1]["end_at"])

    def test_explicit_start_conflict_fails_and_keeps_ledger_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            start = "2030-01-01T00:00:00Z"
            first = self.run_bk(["1", "30m", "--gpu", "0", "--start", start], data_dir)
            self.assertEqual(first.returncode, 0, first.stderr)

            second = self.run_bk(["exclusive", "1", "30m", "--gpu", "0", "--start", start], data_dir)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("nearest available", second.stderr)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(len(ledger["reservations"]), 1)

    def test_doctor_reports_shared_capacity_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            now = ceil_5m(datetime.now(timezone.utc).replace(microsecond=0))
            data_dir.mkdir(parents=True, exist_ok=True)
            base = {
                "op_id": "op",
                "uid": os.getuid(),
                "username": "current",
                "gpus": [0],
                "mode": "shared",
                "status": "active",
                "created_at": iso(now),
                "updated_at": iso(now),
            }
            left = {
                **base,
                "id": "left",
                "start_at": iso(now + timedelta(hours=1)),
                "end_at": iso(now + timedelta(hours=3)),
            }
            right = {
                **base,
                "id": "right",
                "op_id": "op-right",
                "start_at": iso(now + timedelta(hours=2)),
                "end_at": iso(now + timedelta(hours=4)),
            }
            third = {
                **base,
                "id": "third",
                "op_id": "op-third",
                "uid": os.getuid() + 1,
                "username": "other",
                "start_at": iso(now + timedelta(hours=2)),
                "end_at": iso(now + timedelta(hours=4)),
            }
            (data_dir / "ledger.json").write_text(json.dumps({"version": 1, "reservations": [left, right, third]}), encoding="utf-8")

            result = self.run_bk(["doctor"], data_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("shared-capacity", result.stdout)
            self.assertIn("count=3", result.stdout)
            self.assertIn("limit=2", result.stdout)
            self.assertIn("left", result.stdout)
            self.assertIn("right", result.stdout)
            self.assertIn("third", result.stdout)

    def test_booking_output_uses_local_time_not_utc_z_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["1", "30m"], Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} [+-]\d{4}")
            self.assertNotIn("T", result.stdout)
            self.assertNotIn("Z", result.stdout)

    def test_short_id_and_index_can_delete_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            create = self.run_bk(["1", "30m"], data_dir)
            self.assertEqual(create.returncode, 0, create.stderr)

            delete = self.run_bk(["del", "1"], data_dir)
            self.assertEqual(delete.returncode, 0, delete.stderr)
            self.assertIn("cancelled:", delete.stdout)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["reservations"][0]["status"], "cancelled")

    def test_edit_by_short_id_changes_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            create = self.run_bk(["1", "30m", "--start", "2030-01-01T00:00:00Z"], data_dir)
            self.assertEqual(create.returncode, 0, create.stderr)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            short_id = ledger["reservations"][0]["id"][:8]

            edit = self.run_bk(["edit", short_id, "--duration", "1h"], data_dir)
            self.assertEqual(edit.returncode, 0, edit.stderr)
            self.assertIn("updated:", edit.stdout)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["reservations"][0]["start_at"], "2030-01-01T00:00:00Z")
            self.assertEqual(ledger["reservations"][0]["end_at"], "2030-01-01T01:00:00Z")

    def test_status_shows_ascii_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["status"], Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Timeline", result.stdout)
            self.assertIn("Legend: . free, M mine", result.stdout)
            self.assertIn("shared record count", result.stdout)

    def test_reset_clears_ledger_logs_and_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            create = self.run_bk(["1", "30m"], data_dir)
            self.assertEqual(create.returncode, 0, create.stderr)
            self.assertTrue((data_dir / "ops.log").exists())

            reset = self.run_bk(["reset", "--yes"], data_dir)
            self.assertEqual(reset.returncode, 0, reset.stderr)
            self.assertIn("reset: removed", reset.stdout)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["reservations"], [])
            self.assertFalse((data_dir / "ops.log").exists())


if __name__ == "__main__":
    unittest.main()
