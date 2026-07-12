import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from bk.cli import main as bk_main
from bk.fileio import ensure_directory


ROOT = Path(__file__).resolve().parents[1]


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ceil_5m(dt):
    timestamp = int(dt.timestamp())
    remainder = timestamp % 300
    if remainder:
        timestamp += 300 - remainder
    return datetime.fromtimestamp(timestamp, timezone.utc)


def floor_5m(dt):
    timestamp = int(dt.timestamp())
    return datetime.fromtimestamp(timestamp - (timestamp % 300), timezone.utc)


class CliTests(unittest.TestCase):
    def run_bk(self, args, data_dir, extra_env=None):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["BK_DATA_DIR"] = str(data_dir)
        env["BK_GPU_COUNT"] = "1"
        env["BK_MAX_SHARED_USERS"] = "2"
        env["BK_GPU_SIM_FILE"] = str(Path(data_dir) / "missing-gpu-simulation.json")
        if extra_env:
            env.update(extra_env)
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
        env["BK_GPU_SIM_FILE"] = str(Path(data_dir) / "missing-gpu-simulation.json")
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

    def test_diagnostic_entrypoints_do_not_require_a_valid_shared_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            config_path = data_dir / "config.json"
            config_path.write_text("{broken", encoding="utf-8")
            config_path.chmod(0o600)
            skill_dir = data_dir / "installed-skill"

            version = self.run_bk(["--version"], data_dir)
            help_result = self.run_bk(["--help"], data_dir)
            skill_show = self.run_bk(["skill", "show"], data_dir)
            skill_install = self.run_bk(
                ["skill", "install", "--target", str(skill_dir)],
                data_dir,
            )

            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertRegex(version.stdout, r"^bk \d+\.\d+\.\d+")
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("GPUbk", help_result.stdout)
            self.assertEqual(skill_show.returncode, 0, skill_show.stderr)
            self.assertIn("name: gpubk", skill_show.stdout)
            self.assertEqual(skill_install.returncode, 0, skill_install.stderr)
            self.assertTrue((skill_dir / "SKILL.md").is_file())

            ordinary = self.run_bk(["doctor", "--json"], data_dir)
            self.assertEqual(ordinary.returncode, 2)
            self.assertIn("JSONDecodeError", ordinary.stdout)

    def test_default_command_starts_plain_interactive_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk_with_input([], Path(tmp), "status\nquit\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("GPUbk booking", result.stdout)
            self.assertIn("bk> ", result.stdout)
            self.assertIn("GPU status", result.stdout)
            self.assertIn("0    unknown", result.stdout)
            self.assertIn("1    unknown", result.stdout)

    def test_config_report_is_read_only_and_redacts_allocator_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "absent"
            result = self.run_bk(
                ["config", "--compact"],
                data_dir,
                {
                    "BK_SLOT_MINUTES": "10",
                    "BK_TIMELINE_HOURS": "4",
                    "BK_MONITOR_INTERVAL_SECONDS": "5",
                    "BK_MONITOR_ROLLUP_SECONDS": "300",
                    "BK_TUI_REFRESH_SECONDS": "2.5",
                    "BK_ALLOCATOR_COMMAND": "allocator --token secret-value",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(data_dir.exists())
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema_version"], "gpubk.config.v1")
            self.assertEqual(payload["effective"]["slot_minutes"], 10)
            self.assertEqual(payload["effective"]["timeline_hours"], 4)
            self.assertEqual(payload["effective"]["monitor_interval_seconds"], 5.0)
            self.assertEqual(payload["effective"]["monitor_rollup_seconds"], 300)
            self.assertEqual(payload["effective"]["tui_refresh_seconds"], 2.5)
            self.assertTrue(payload["effective"]["allocator_command_configured"])
            self.assertNotIn("secret-value", result.stdout)
            self.assertEqual(payload["ledger_policy"]["status"], "unbound")
            self.assertIn("BK_SLOT_MINUTES", payload["environment_overrides"])
            self.assertIn("BK_MONITOR_INTERVAL_SECONDS", payload["environment_overrides"])
            self.assertIn("BK_TUI_REFRESH_SECONDS", payload["environment_overrides"])

    def test_config_report_detects_bound_policy_match_and_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            created = self.run_bk(["1", "30m", "--quiet"], data_dir)
            matching = self.run_bk(["cfg", "--json"], data_dir)
            mismatch = self.run_bk(
                ["config", "--json"],
                data_dir,
                {"BK_SLOT_MINUTES": "10"},
            )

            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(matching.returncode, 0, matching.stderr)
            self.assertEqual(json.loads(matching.stdout)["ledger_policy"]["status"], "match")
            self.assertEqual(mismatch.returncode, 2, mismatch.stderr)
            mismatch_payload = json.loads(mismatch.stdout)
            self.assertEqual(mismatch_payload["ledger_policy"]["status"], "mismatch")
            self.assertIn("granularity", mismatch_payload["ledger_policy"]["message"])

    def test_config_report_uses_external_trusted_config_without_writing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "absent-data"
            config_dir = root / "trusted"
            config_dir.mkdir(mode=0o700)
            config_path = config_dir / "config.json"
            config_path.write_text(
                json.dumps({"config_version": 1, "gpu_count": 2, "slot_minutes": 10}),
                encoding="utf-8",
            )
            config_path.chmod(0o600)

            result = self.run_bk(
                ["config", "--compact"],
                data_dir,
                {"BK_CONFIG_FILE": str(config_path)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(data_dir.exists())
            payload = json.loads(result.stdout)
            self.assertEqual(payload["config_file"]["path"], str(config_path.resolve()))
            self.assertEqual(payload["effective"]["config_file"], str(config_path.resolve()))
            self.assertIn("BK_CONFIG_FILE", payload["environment_overrides"])

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

    def test_quiet_booking_still_surfaces_deferred_transaction_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            output = StringIO()
            errors = StringIO()
            environment = {
                "BK_DATA_DIR": str(data_dir),
                "BK_GPU_COUNT": "1",
                "BK_MAX_SHARED_USERS": "2",
                "BK_GPU_SIM_FILE": str(data_dir / "missing-simulation.json"),
            }

            with (
                mock.patch.dict(os.environ, environment),
                mock.patch(
                    "bk.storage.fsync_directory",
                    side_effect=OSError("journal directory sync failed"),
                ),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                result = bk_main(["1", "5m", "--op-id", "warning-test", "--quiet"])

            self.assertEqual(result, 0)
            self.assertIn("created:", output.getvalue())
            self.assertIn("warning: transaction accepted", errors.getvalue())
            self.assertIn("deferred recovery", errors.getvalue())
            self.assertTrue((data_dir / "transaction.json").exists())
            self.assertFalse((data_dir / "ledger.json").exists())

    def test_compound_duration_memory_and_live_idle_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            simulation = data_dir / "gpu-sim.json"
            simulation.write_text(
                json.dumps(
                    {
                        "gpus": [
                            {
                                "index": 0,
                                "name": "busy",
                                "memory_used_mb": 16000,
                                "memory_total_mb": 24000,
                                "utilization_percent": 80,
                                "processes": [
                                    {
                                        "pid": 55,
                                        "uid": os.getuid() + 1,
                                        "username": "other",
                                        "command": "python train.py",
                                    }
                                ],
                            },
                            {
                                "index": 1,
                                "name": "idle",
                                "memory_used_mb": 100,
                                "memory_total_mb": 24000,
                                "utilization_percent": 0,
                                "processes": [],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_bk(
                ["1", "1h30m", "--mem", "4g"],
                data_dir,
                {"BK_GPU_COUNT": "2", "BK_GPU_SIM_FILE": str(simulation)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            booking = ledger["reservations"][0]
            self.assertEqual(booking["gpus"], [1])
            self.assertEqual(booking["expected_memory_mb"], 4096)
            self.assertEqual(
                datetime.fromisoformat(booking["end_at"].replace("Z", "+00:00"))
                - datetime.fromisoformat(booking["start_at"].replace("Z", "+00:00")),
                timedelta(minutes=90),
            )
            self.assertIn("gpu=1", result.stdout)
            self.assertNotIn("selection: GPU 1", result.stdout)
            self.assertIn("avoided currently busy GPU 0", result.stdout)
            self.assertIn("physical-free-now=", result.stdout)
            self.assertIn("reservation-budget-after=", result.stdout)

    def test_auto_alias_defaults_to_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["auto", "1", "30m"], Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            ledger = json.loads((Path(tmp) / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["reservations"][0]["mode"], "shared")

    def test_implicit_now_starts_in_the_current_five_minute_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["1", "30m"], Path(tmp))

            self.assertEqual(result.returncode, 0, result.stderr)
            reservation = json.loads((Path(tmp) / "ledger.json").read_text(encoding="utf-8"))["reservations"][0]
            created_at = datetime.fromisoformat(reservation["created_at"].replace("Z", "+00:00"))
            self.assertEqual(reservation["start_at"], iso(floor_5m(created_at)))

    def test_configured_booking_slice_applies_to_cli_and_agent_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            environment = {"BK_SLOT_MINUTES": "10"}

            rejected = self.run_bk(["1", "5m"], data_dir, environment)
            created = self.run_bk(["1", "20m", "--json"], data_dir, environment)
            context = self.run_bk(
                ["agent", "context", "--compact"],
                data_dir,
                environment,
            )
            timeline = self.run_bk(
                ["timeline", "2h", "--step", "10m"],
                data_dir,
                environment,
            )
            doctor = self.run_bk(["doctor", "--json"], data_dir, environment)

            self.assertEqual(rejected.returncode, 2)
            self.assertIn("multiple of 10 minutes", rejected.stderr)
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(timeline.returncode, 0, timeline.stderr)
            reservation = json.loads(created.stdout)["reservation"]
            start = datetime.fromisoformat(reservation["start_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(reservation["end_at"].replace("Z", "+00:00"))
            self.assertEqual(int(start.timestamp()) % 600, 0)
            self.assertEqual(end - start, timedelta(minutes=20))
            self.assertEqual(json.loads(context.stdout)["policy"]["granularity_minutes"], 10)
            self.assertIn("10m/cell", timeline.stdout)
            self.assertIn("--step 30m", timeline.stdout)
            self.assertEqual(json.loads(doctor.stdout)["booking_slot_minutes"], 10)

    def test_doctor_reports_a_local_granularity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            created = self.run_bk(["1", "30m"], data_dir)
            report = self.run_bk(
                ["doctor", "--json"],
                data_dir,
                {"BK_SLOT_MINUTES": "10"},
            )

            self.assertEqual(created.returncode, 0, created.stderr)
            payload = json.loads(report.stdout)
            self.assertFalse(payload["healthy"])
            self.assertEqual(payload["policy_issues"][0]["type"], "ledger-policy-mismatch")
            self.assertIn("granularity_seconds", payload["policy_issues"][0]["message"])

    def test_human_at_option_accepts_relative_time_without_queueing(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = datetime.now(timezone.utc)
            result = self.run_bk(["1", "30m", "--at", "+30m"], Path(tmp))

            self.assertEqual(result.returncode, 0, result.stderr)
            reservation = json.loads((Path(tmp) / "ledger.json").read_text(encoding="utf-8"))["reservations"][0]
            start = datetime.fromisoformat(reservation["start_at"].replace("Z", "+00:00"))
            self.assertGreaterEqual(start, before + timedelta(minutes=30))
            self.assertEqual(int(start.timestamp()) % 300, 0)

    def test_guided_add_recovers_each_invalid_field_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_input = "\n".join(
                [
                    "",       # shared
                    "",       # default shared capacity
                    "many",   # invalid GPU count
                    "1",
                    "7m",     # invalid duration
                    "30m",
                    "hwo",    # invalid start
                    "now",
                    "",       # automatic GPUs
                    "",       # automatic VRAM estimate
                    "",       # no command
                    "",       # confirm
                    "",
                ]
            )

            result = self.run_bk_with_input(["add"], Path(tmp), user_input)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreaterEqual(result.stdout.count("Invalid input:"), 3)
            self.assertIn("tomorrow 09:00", result.stdout)
            self.assertIn("Review", result.stdout)
            self.assertIn("created:", result.stdout)
            ledger = json.loads((Path(tmp) / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(len(ledger["reservations"]), 1)

    def test_guided_add_can_go_back_and_cancel_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_input = "\n".join(["", "", "1", "back", "2", "cancel", ""])

            result = self.run_bk_with_input(["add"], Path(tmp), user_input)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("cancelled", result.stdout)
            self.assertFalse((Path(tmp) / "ledger.json").exists())

    def test_guided_add_eof_cancels_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk_with_input(["add"], Path(tmp), "")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("cancelled", result.stdout)
            self.assertNotIn("Traceback", result.stderr)

    def test_exclusive_command_uses_exclusive_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["exclusive", "1", "30m"], Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            ledger = json.loads((Path(tmp) / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["reservations"][0]["mode"], "exclusive")

    def test_single_letter_shared_and_exclusive_aliases(self):
        with tempfile.TemporaryDirectory() as shared_tmp, tempfile.TemporaryDirectory() as exclusive_tmp:
            shared = self.run_bk(["s", "1", "30m"], Path(shared_tmp))
            exclusive = self.run_bk(["x", "1", "30m"], Path(exclusive_tmp))

            self.assertEqual(shared.returncode, 0, shared.stderr)
            self.assertEqual(exclusive.returncode, 0, exclusive.stderr)
            shared_ledger = json.loads((Path(shared_tmp) / "ledger.json").read_text(encoding="utf-8"))
            exclusive_ledger = json.loads((Path(exclusive_tmp) / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(shared_ledger["reservations"][0]["mode"], "shared")
            self.assertEqual(exclusive_ledger["reservations"][0]["mode"], "exclusive")

    def test_implicit_now_shared_request_overlaps_until_record_capacity_then_queues(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            now = ceil_5m(datetime.now(timezone.utc).replace(microsecond=0))
            existing_end = now + timedelta(hours=1)
            data_dir.mkdir(parents=True, exist_ok=True)
            ledger_path = data_dir / "ledger.json"
            ledger_path.write_text(
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
            ledger_path.chmod(0o600)

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
            self.assertIn("units=3/2", result.stdout)
            self.assertIn("left", result.stdout)
            self.assertIn("right", result.stdout)
            self.assertIn("third", result.stdout)

    def test_log_tail_is_uid_filtered_bounded_and_machine_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            log_path = data_dir / "ops.log"
            events = [
                {
                    "ts": "2030-01-01T00:00:00Z",
                    "uid": os.getuid(),
                    "action": "add",
                    "result": "created",
                    "reservation_id": "first-event",
                },
                {
                    "ts": "2030-01-01T00:01:00Z",
                    "uid": os.getuid() + 1,
                    "action": "add",
                    "result": "created",
                    "reservation_id": "other-event",
                },
                {
                    "ts": "2030-01-01T00:02:00Z",
                    "uid": os.getuid(),
                    "action": "edit",
                    "result": "updated",
                    "reservation_id": "latest-event",
                    "message": "x" * 5000,
                    "unknown_nested": [[0] * 1000],
                },
            ]
            log_path.write_bytes(
                b"".join(json.dumps(item).encode() + b"\n" for item in events)
                + b"not-json\n"
            )
            log_path.chmod(0o600)

            result = self.run_bk(["log", "--limit", "1", "--json"], data_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema_version"], "gpubk.audit.v1")
            self.assertEqual(payload["uid"], os.getuid())
            self.assertEqual(len(payload["events"]), 1)
            self.assertEqual(payload["events"][0]["reservation_id"], "latest-event")
            self.assertEqual(len(payload["events"][0]["message"]), 4096)
            self.assertNotIn("unknown_nested", payload["events"][0])
            self.assertIn("malformed audit record", payload["warning"])

    def test_log_plain_output_survives_invalid_optional_fields_and_escapes_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            log_path = data_dir / "ops.log"
            log_path.write_text(
                json.dumps(
                    {
                        "ts": "bad-time",
                        "uid": os.getuid(),
                        "action": "add\u001b[31m",
                        "result": None,
                        "gpus": "bad",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            log_path.chmod(0o600)

            result = self.run_bk(["lg"], data_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("invalid-time", result.stdout)
            self.assertIn("add?[31m", result.stdout)
            self.assertNotIn("\x1b", result.stdout)
            self.assertIn("gpu=-", result.stdout)

    def test_doctor_reports_interrupted_audit_tail_without_repairing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            log_path = data_dir / "ops.log"
            log_path.write_bytes(
                json.dumps({"uid": os.getuid(), "action": "add"}).encode() + b"\n{"
            )
            log_path.chmod(0o600)
            original = log_path.read_bytes()

            result = self.run_bk(["doctor", "--json", "--strict"], data_dir)

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            issue = next(
                item for item in payload["storage_issues"] if item["type"] == "audit-log-tail"
            )
            self.assertIn("malformed audit record", issue["message"])
            self.assertIn("not newline-terminated", issue["message"])
            self.assertEqual(log_path.read_bytes(), original)

    def test_doctor_json_is_read_only_without_explicit_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "not-created"

            result = self.run_bk(["doctor", "--json"], data_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema_version"], "gpubk.doctor.v1")
            self.assertEqual(payload["probes"], [])
            self.assertTrue(payload["healthy"])
            self.assertIsNone(payload["ready"])
            self.assertFalse(data_dir.exists())

    def test_doctor_probe_is_machine_readable_and_strict_rejects_simulation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "probe-data"
            simulation = Path(tmp) / "gpu-sim.json"
            simulation.write_text(
                json.dumps({"gpus": [{"index": 0, "name": "sim", "processes": []}]}),
                encoding="utf-8",
            )
            env = {"BK_GPU_SIM_FILE": str(simulation)}

            report = self.run_bk(["doctor", "--probe", "--json"], data_dir, env)
            strict = self.run_bk(["doctor", "--probe", "--json", "--strict"], data_dir, env)

            self.assertEqual(report.returncode, 0, report.stderr)
            self.assertEqual(strict.returncode, 2, strict.stderr)
            payload = json.loads(report.stdout)
            by_name = {item["name"]: item for item in payload["probes"]}
            self.assertEqual(by_name["atomic-replace"]["status"], "pass")
            self.assertEqual(by_name["process-lock"]["status"], "pass")
            self.assertEqual(by_name["gpu-telemetry"]["status"], "warn")
            self.assertFalse(payload["ready"])
            self.assertEqual(list(data_dir.glob(".gpubk-probe-*")), [])

    def test_doctor_reports_ledger_symlink_as_json_without_following_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir(mode=0o700)
            target = Path(tmp) / "outside-ledger.json"
            target.write_text(json.dumps({"version": 1, "reservations": []}), encoding="utf-8")
            original = target.read_bytes()
            (data_dir / "ledger.json").symlink_to(target)

            result = self.run_bk(["doctor", "--json", "--strict"], data_dir)

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            issue_types = {item["type"] for item in payload["storage_issues"]}
            self.assertIn("file-type", issue_types)
            self.assertIn("ledger-read", issue_types)
            self.assertFalse(payload["healthy"])
            self.assertEqual(target.read_bytes(), original)

    def test_doctor_reports_hard_linked_ledger_without_reading_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            ledger = data_dir / "ledger.json"
            ledger.write_text(
                json.dumps({"version": 1, "reservations": []}),
                encoding="utf-8",
            )
            ledger.chmod(0o600)
            alias = data_dir / "ledger-alias"
            os.link(ledger, alias)
            original = alias.read_bytes()

            result = self.run_bk(["doctor", "--json", "--strict"], data_dir)

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            issue_types = {item["type"] for item in payload["storage_issues"]}
            self.assertIn("file-links", issue_types)
            self.assertIn("ledger-read", issue_types)
            self.assertFalse(payload["healthy"])
            self.assertEqual(alias.read_bytes(), original)

    def test_doctor_reports_usage_directory_symlink_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir(mode=0o700)
            outside = Path(tmp) / "outside-usage"
            outside.mkdir()
            (data_dir / "usage").symlink_to(outside, target_is_directory=True)

            result = self.run_bk(["doctor", "--json", "--strict"], data_dir)

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            issue = next(
                item
                for item in payload["storage_issues"]
                if item["type"] == "usage-directory-type"
            )
            self.assertEqual(issue["actual"], "symbolic-link")
            self.assertFalse(payload["healthy"])
            self.assertEqual(list(outside.iterdir()), [])

    def test_doctor_never_reads_through_a_symlink_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside-data"
            outside.mkdir()
            ledger = outside / "ledger.json"
            ledger.write_text("private outside content", encoding="utf-8")
            usage = outside / "usage"
            usage.mkdir()
            usage_meta = usage / "store.json"
            usage_meta.write_text("private usage content", encoding="utf-8")
            original = ledger.read_bytes()
            original_usage = usage_meta.read_bytes()
            data_dir = Path(tmp) / "linked-data"
            data_dir.symlink_to(outside, target_is_directory=True)

            result = self.run_bk(["doctor", "--json", "--strict"], data_dir)

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            issue_types = {item["type"] for item in payload["storage_issues"]}
            self.assertIn("directory-type", issue_types)
            ledger_read = next(
                item for item in payload["storage_issues"] if item["type"] == "ledger-read"
            )
            usage_health = next(
                item for item in payload["storage_issues"] if item["type"] == "usage-health"
            )
            self.assertIn("skipped", ledger_read["message"])
            self.assertIn("skipped", usage_health["message"])
            self.assertNotIn("usage-format", issue_types)
            self.assertEqual(ledger.read_bytes(), original)
            self.assertEqual(usage_meta.read_bytes(), original_usage)

    def test_doctor_does_not_recover_a_pending_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            transaction = {
                "version": 1,
                "transaction_id": "doctor-read-only",
                "created_at": "2030-01-01T00:00:00Z",
                "ledger": {
                    "version": 1,
                    "last_transaction_id": "doctor-read-only",
                    "reservations": [{"id": "pending"}],
                },
                "logs": [],
            }
            journal = data_dir / "transaction.json"
            journal.write_text(json.dumps(transaction), encoding="utf-8")
            journal.chmod(0o600)
            original = journal.read_bytes()

            result = self.run_bk(["doctor", "--json", "--strict"], data_dir)

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn(
                "pending-journal",
                {item["type"] for item in payload["storage_issues"]},
            )
            self.assertEqual(journal.read_bytes(), original)
            self.assertFalse((data_dir / "ledger.json").exists())
            self.assertFalse((data_dir / "ops.log").exists())

    def test_doctor_reports_malformed_reservation_records_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            ledger = data_dir / "ledger.json"
            ledger.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "reservations": [{"id": "broken", "status": "active"}],
                    }
                ),
                encoding="utf-8",
            )
            ledger.chmod(0o600)

            result = self.run_bk(["doctor", "--json", "--strict"], data_dir)

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["policy_issues"][0]["type"], "invalid-ledger-record")
            self.assertIn("end_at", payload["policy_issues"][0]["message"])
            self.assertFalse(payload["healthy"])

    def test_doctor_reports_non_object_reservation_records_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            ledger = data_dir / "ledger.json"
            ledger.write_text(
                json.dumps({"version": 1, "reservations": ["broken"]}),
                encoding="utf-8",
            )
            ledger.chmod(0o600)

            result = self.run_bk(["doctor", "--json", "--strict"], data_dir)

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["policy_issues"][0]["type"], "invalid-ledger-record")
            self.assertFalse(payload["healthy"])

    def test_doctor_reports_read_only_backup_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            backup_dir = data_dir / "backups"
            backup_dir.mkdir(mode=0o700)
            ledger = data_dir / "ledger.json"
            ledger.write_text("broken", encoding="utf-8")
            ledger.chmod(0o600)
            backup = backup_dir / "ledger-20300101T000000000000Z.json"
            backup.write_text(
                json.dumps({"version": 1, "reservations": []}),
                encoding="utf-8",
            )
            backup.chmod(0o600)

            result = self.run_bk(["doctor", "--json", "--strict"], data_dir)

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            fallback = next(
                item for item in payload["storage_issues"] if item["type"] == "ledger-fallback"
            )
            self.assertIn("latest valid backup", fallback["message"])
            self.assertEqual(ledger.read_text(encoding="utf-8"), "broken")

    def test_booking_output_uses_local_time_not_utc_z_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["1", "30m"], Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} [+-]\d{4}")
            self.assertNotIn("T", result.stdout)
            self.assertNotIn("Z", result.stdout)

    def test_list_expands_long_duration_without_changing_total_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            created = self.run_bk(["1", "5d4h20m", "--quiet"], data_dir)
            listed = self.run_bk(["l"], data_dir)

            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertIn("dur=124h20m (5d4h20m)", listed.stdout)

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

    def test_short_management_aliases_list_edit_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            start = iso(ceil_5m(datetime.now(timezone.utc) + timedelta(minutes=10)))
            create = self.run_bk(["1", "30m", "--start", start], data_dir)
            listed = self.run_bk(["l"], data_dir)
            edited = self.run_bk(["e", "1", "--duration", "1h"], data_dir)
            deleted = self.run_bk(["d", "1"], data_dir)

            self.assertEqual(create.returncode, 0, create.stderr)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertIn("shared", listed.stdout)
            self.assertEqual(edited.returncode, 0, edited.stderr)
            self.assertIn("updated:", edited.stdout)
            self.assertEqual(deleted.returncode, 0, deleted.stderr)
            self.assertIn("cancelled:", deleted.stdout)

    def test_short_status_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["st"], Path(tmp))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("GPU status", result.stdout)

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

    def test_edit_accepts_short_mode_and_can_clear_memory_declaration(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            create = self.run_bk(
                ["1", "30m", "--mem", "4g", "--start", "2030-01-01T00:00:00Z"],
                data_dir,
            )
            edit = self.run_bk(["e", "1", "--mode", "x", "--mem", "-"], data_dir)

            self.assertEqual(create.returncode, 0, create.stderr)
            self.assertEqual(edit.returncode, 0, edit.stderr)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["reservations"][0]["mode"], "exclusive")
            self.assertNotIn("expected_memory_mb", ledger["reservations"][0])

    def test_direct_edit_accepts_friendly_local_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            start = iso(ceil_5m(datetime.now(timezone.utc) + timedelta(days=1)))
            create = self.run_bk(["1", "30m", "--start", start], data_dir)
            edit = self.run_bk(["e", "1", "--at", "+2d"], data_dir)

            self.assertEqual(create.returncode, 0, create.stderr)
            self.assertEqual(edit.returncode, 0, edit.stderr)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            edited_start = datetime.fromisoformat(ledger["reservations"][0]["start_at"].replace("Z", "+00:00"))
            self.assertGreater(edited_start, datetime.now(timezone.utc) + timedelta(days=1, hours=23))
            self.assertEqual(int(edited_start.timestamp()) % 300, 0)

    def test_guided_edit_recovers_invalid_fields_and_confirms_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            start = iso(ceil_5m(datetime.now(timezone.utc) + timedelta(days=1)))
            create = self.run_bk(["1", "30m", "--start", start], data_dir, {"BK_GPU_COUNT": "2"})
            user_input = "\n".join(
                [
                    "",      # keep mode
                    "",      # keep share
                    "7m",    # invalid duration
                    "45m",
                    "hwo",   # invalid start
                    "",      # keep start
                    "",      # keep GPUs
                    "",      # keep count
                    "",      # keep memory
                    "",      # do not queue
                    "",      # confirm
                    "",
                ]
            )
            edit = self.run_bk_with_input(["e", "1"], data_dir, user_input)

            self.assertEqual(create.returncode, 0, create.stderr)
            self.assertEqual(edit.returncode, 0, edit.stderr)
            self.assertGreaterEqual(edit.stdout.count("Invalid input:"), 2)
            self.assertIn("Review", edit.stdout)
            self.assertIn("duration=45m", edit.stdout)
            self.assertIn("updated:", edit.stdout)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            reservation = ledger["reservations"][0]
            duration = datetime.fromisoformat(reservation["end_at"].replace("Z", "+00:00")) - datetime.fromisoformat(
                reservation["start_at"].replace("Z", "+00:00")
            )
            self.assertEqual(duration, timedelta(minutes=45))

    def test_weighted_share_and_share_with_use_the_same_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            env = {"BK_MAX_SHARED_USERS": "4"}

            weighted = self.run_bk(
                ["1", "30m", "--gpu", "0", "--share", "3/4", "-q"],
                data_dir,
                env,
            )
            remaining = self.run_bk(["1", "30m", "--gpu", "0", "-q"], data_dir, env)
            queued = self.run_bk(["1", "30m", "--gpu", "0", "-q"], data_dir, env)

            self.assertEqual(weighted.returncode, 0, weighted.stderr)
            self.assertEqual(remaining.returncode, 0, remaining.stderr)
            self.assertEqual(queued.returncode, 0, queued.stderr)
            self.assertIn("share=3/4", weighted.stdout)
            self.assertIn("share=1/4", remaining.stdout)
            self.assertIn("queued:", queued.stdout)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual([item["share_units"] for item in ledger["reservations"]], [3, 1, 1])
            self.assertLess(
                ledger["reservations"][1]["start_at"],
                ledger["reservations"][0]["end_at"],
            )
            self.assertEqual(ledger["reservations"][0]["end_at"], ledger["reservations"][2]["start_at"])

        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(
                ["1", "30m", "--share-with", "1", "--json"],
                Path(tmp),
                {"BK_MAX_SHARED_USERS": "4"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["reservation"]["share_units_per_gpu"], 3)
            self.assertEqual(payload["reservation"]["share_fraction_per_gpu"], "3/4")

    def test_direct_edit_can_change_shared_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            env = {"BK_MAX_SHARED_USERS": "4"}
            created = self.run_bk(
                ["1", "30m", "--start", "2030-01-01T00:00:00Z"],
                data_dir,
                env,
            )
            edited = self.run_bk(["e", "1", "--share", "3/4"], data_dir, env)

            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(edited.returncode, 0, edited.stderr)
            self.assertIn("share=3/4", edited.stdout)
            reservation = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))["reservations"][0]
            self.assertEqual(reservation["share_units"], 3)

    def test_slots_preserves_share_in_copyable_booking_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(
                ["slots", "1", "30m", "--share-with", "1", "--limit", "1"],
                Path(tmp),
                {"BK_MAX_SHARED_USERS": "4"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("share 3/4", result.stdout)
            self.assertIn("--share 3/4", result.stdout)

    def test_status_is_compact_and_timeline_is_a_separate_aligned_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self.run_bk(["status"], Path(tmp))
            timeline = self.run_bk(["tl", "30m", "--step", "5m"], Path(tmp))

            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("GPU status", status.stdout)
            self.assertIn("VRAM free/total", status.stdout)
            self.assertNotIn("Timeline |", status.stdout)
            self.assertEqual(timeline.returncode, 0, timeline.stderr)
            self.assertIn("5m/cell | 6 cells", timeline.stdout)
            self.assertIn("M1-M9 total units, includes mine", timeline.stdout)
            minute_line = next(line for line in timeline.stdout.splitlines() if line.startswith("Min"))
            gpu_line = next(line for line in timeline.stdout.splitlines() if line.startswith("G0"))
            self.assertEqual(len(minute_line), len(gpu_line))
            self.assertEqual((len(minute_line[6:]) + 1) // 3, 6)
            self.assertEqual((len(gpu_line[6:]) + 1) // 3, 6)

    def test_timeline_reports_total_shared_units_when_a_slice_includes_mine(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            env = {"BK_MAX_SHARED_USERS": "4"}
            first = self.run_bk(
                ["1", "30m", "--start", "2030-01-01T00:00:00Z", "--share", "3/4"],
                data_dir,
                env,
            )
            second = self.run_bk(
                ["1", "30m", "--start", "2030-01-01T00:00:00Z", "--share", "1/4"],
                data_dir,
                env,
            )
            timeline = self.run_bk(
                [
                    "tl",
                    "--from",
                    "2030-01-01T00:00:00Z",
                    "--window",
                    "30m",
                    "--step",
                    "5m",
                ],
                data_dir,
                env,
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(timeline.returncode, 0, timeline.stderr)
            gpu_line = next(line for line in timeline.stdout.splitlines() if line.startswith("G0"))
            self.assertIn("M4", gpu_line)
            self.assertNotIn("M3", gpu_line)

    def test_status_ignores_system_gpu_contexts_when_deciding_busy_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            simulation = data_dir / "gpu-sim.json"
            simulation.write_text(
                json.dumps(
                    {
                        "gpus": [
                            {
                                "index": 0,
                                "name": "RTX",
                                "memory_used_mb": 512,
                                "memory_total_mb": 32607,
                                "utilization_percent": 0,
                                "processes": [
                                    {
                                        "pid": 123,
                                        "uid": 0,
                                        "username": "root",
                                        "command": "Xorg",
                                        "gpu_memory_mb": 4,
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            status = self.run_bk(["st"], data_dir, {"BK_GPU_SIM_FILE": str(simulation)})
            verbose = self.run_bk(["st", "-v"], data_dir, {"BK_GPU_SIM_FILE": str(simulation)})

            self.assertEqual(status.returncode, 0, status.stderr)
            gpu_row = next(line for line in status.stdout.splitlines() if line.startswith("0"))
            self.assertIn("    0 idle", gpu_row)
            self.assertNotIn("busy", gpu_row)
            self.assertIn("state=system", verbose.stdout)

    def test_status_uses_live_utilization_when_no_process_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            simulation = data_dir / "gpu-sim.json"
            simulation.write_text(
                json.dumps(
                    {
                        "gpus": [
                            {
                                "index": 0,
                                "name": "RTX",
                                "memory_used_mb": 512,
                                "memory_total_mb": 32607,
                                "utilization_percent": 80,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_bk(["st"], data_dir, {"BK_GPU_SIM_FILE": str(simulation)})

            self.assertEqual(result.returncode, 0, result.stderr)
            gpu_row = next(line for line in result.stdout.splitlines() if line.startswith("0"))
            self.assertIn("busy", gpu_row)

    def test_timeline_controls_window_step_start_and_gpu_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(
                ["timeline", "--from", "+30m", "--window", "2h", "--step", "15m", "--gpu", "0"],
                Path(tmp),
                {"BK_GPU_COUNT": "2"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("15m/cell | 8 cells", result.stdout)
            self.assertIn("G0", result.stdout)
            self.assertNotIn("G1", result.stdout)

    def test_status_and_default_timeline_fit_a_72_column_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"BK_GPU_COUNT": "8", "COLUMNS": "72"}
            status = self.run_bk(["st"], Path(tmp), env)
            timeline = self.run_bk(["tl"], Path(tmp), env)

            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(timeline.returncode, 0, timeline.stderr)
            self.assertTrue(all(len(line) <= 72 for line in status.stdout.splitlines()), status.stdout)
            self.assertTrue(all(len(line) <= 72 for line in timeline.stdout.splitlines()), timeline.stdout)
            self.assertEqual(timeline.stdout.count("Min   "), 2)

    def test_narrow_status_with_a_reservation_remains_compact(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            env = {"BK_GPU_COUNT": "8", "COLUMNS": "72"}
            created = self.run_bk(["2", "1h30m", "--quiet"], data_dir, env)
            status = self.run_bk(["st"], data_dir, env)

            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertTrue(all(len(line) <= 72 for line in status.stdout.splitlines()), status.stdout)
            self.assertIn("G=0,1", status.stdout)

    def test_timeline_auto_step_scales_a_long_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(
                ["tl", "1d", "--step", "auto"],
                Path(tmp),
                {"BK_GPU_COUNT": "2", "COLUMNS": "72"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("2h/cell | 12 cells", result.stdout)
            self.assertTrue(all(len(line) <= 72 for line in result.stdout.splitlines()), result.stdout)

    def test_configured_timeline_hours_controls_default_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(
                ["tl"],
                Path(tmp),
                {"BK_GPU_COUNT": "2", "BK_TIMELINE_HOURS": "4", "COLUMNS": "100"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("5m/cell | 48 cells", result.stdout)

    def test_verbose_booking_restores_load_score_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            simulation = data_dir / "gpu-sim.json"
            simulation.write_text(
                json.dumps(
                    {
                        "gpus": [
                            {
                                "index": 0,
                                "name": "idle",
                                "memory_used_mb": 0,
                                "memory_total_mb": 24000,
                                "utilization_percent": 0,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = self.run_bk(
                ["1", "30m", "--verbose"],
                data_dir,
                {"BK_GPU_SIM_FILE": str(simulation)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("selection: GPU 0", result.stdout)

    def test_quiet_booking_prints_one_result_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["1", "30m", "--quiet"], Path(tmp))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.splitlines()), 1)
            self.assertTrue(result.stdout.startswith("created:"), result.stdout)

    def test_slots_lists_multiple_read_only_placement_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            result = self.run_bk(
                ["slots", "1", "30m", "--limit", "2"],
                data_dir,
                {"BK_GPU_COUNT": "2"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Earliest shared options", result.stdout)
            self.assertIn("read-only", result.stdout)
            self.assertIn(" 1 0", result.stdout)
            self.assertIn(" 2 1", result.stdout)
            self.assertIn("Book option 1: bk 1 30m --gpu 0 --at", result.stdout)
            self.assertFalse((data_dir / "ledger.json").exists())

    def test_help_prioritizes_common_cli_workflows(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["--help"], Path(tmp), {"COLUMNS": "72"})

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("BOOK\n", result.stdout)
            self.assertIn("VIEW\n", result.stdout)
            self.assertIn("bk 2 1h", result.stdout)
            self.assertIn("bk e ID --at 20:00", result.stdout)
            self.assertTrue(all(len(line) <= 72 for line in result.stdout.splitlines()), result.stdout)

    def test_monitor_once_writes_usage_events_and_rollups(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            simulation = data_dir / "gpu-sim.json"
            simulation.write_text(
                json.dumps(
                    {
                        "gpus": [
                            {
                                "index": 0,
                                "name": "sim",
                                "utilization_percent": 50,
                                "processes": [
                                    {
                                        "pid": 1234,
                                        "uid": os.getuid(),
                                        "username": "current",
                                        "command": "python train.py",
                                        "gpu_memory_mb": 1024,
                                        "sm_utilization_percent": 40,
                                        "host_start_id": "start-1234",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            today = datetime.now(timezone.utc).date()
            partition = data_dir / f"usage/minute/{today:%Y}/{today:%m}/{today.isoformat()}.v1.jsonl"
            ensure_directory(partition.parent, 0o700, require_mode=True)
            partition.write_bytes(b'{"interrupted"')
            partition.chmod(0o600)

            monitor = self.run_bk(
                ["monitor", "--once"],
                data_dir,
                {
                    "BK_GPU_SIM_FILE": str(simulation),
                    "BK_MONITOR_INTERVAL_SECONDS": "5",
                    "BK_MONITOR_ROLLUP_SECONDS": "300",
                },
            )
            events = self.run_bk(["usage", "events", "--all", "--since", "1h"], data_dir)
            rollups = self.run_bk(
                ["usage", "samples", "--all", "--since", "1h", "--resolution", "1m", "--json", "--compact"],
                data_dir,
            )

            self.assertEqual(monitor.returncode, 0, monitor.stderr)
            self.assertIn("monitor started: interval=5s rollup=300s", monitor.stdout)
            self.assertIn("monitor started", monitor.stdout)
            self.assertIn("process-start", monitor.stdout)
            self.assertIn("discarded an incomplete trailing usage record", monitor.stdout)
            self.assertEqual(events.returncode, 0, events.stderr)
            self.assertIn("unreserved", events.stdout)
            self.assertIn("train.py", events.stdout)
            self.assertEqual(rollups.returncode, 0, rollups.stderr)
            self.assertIn('"partial": true', rollups.stdout)
            self.assertIn('"schema_version": "gpubk.usage.v1"', rollups.stdout)

    def test_monitor_flags_override_configured_cadence(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(
                ["monitor", "--once", "--interval", "1", "--rollup", "30"],
                Path(tmp),
                {
                    "BK_MONITOR_INTERVAL_SECONDS": "5",
                    "BK_MONITOR_ROLLUP_SECONDS": "300",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("monitor started: interval=1s rollup=30s", result.stdout)

    def test_monitor_rejects_inexact_cadence_before_creating_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "absent"
            result = self.run_bk(
                ["monitor", "--once", "--interval", "7", "--rollup", "60"],
                data_dir,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("integer multiple", result.stderr)
            self.assertFalse(data_dir.exists())

    def test_monitor_fails_fast_when_another_writer_holds_the_usage_lock(self):
        from bk.usage_store import UsageAuditStore

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = UsageAuditStore(data_dir, lock_timeout_seconds=0.05)

            with store.lock():
                result = self.run_bk(
                    ["monitor", "--once"],
                    data_dir,
                    {"BK_LOCK_TIMEOUT_SECONDS": "0.05"},
                )

            self.assertEqual(result.returncode, 75)
            self.assertIn("another monitor or telemetry maintenance writer is active", result.stderr)

    def test_worker_fails_fast_when_another_worker_holds_the_private_lease(self):
        from bk.config import Config
        from bk.identity import current_actor
        from bk.joblogs import acquire_job_worker_lease

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            job_dir = root / "jobs"
            config = Config(data_dir=data_dir, gpu_count=1, job_log_dir=job_dir)
            lease = acquire_job_worker_lease(config, current_actor(), "holder", "test-host")
            try:
                result = self.run_bk(
                    ["worker", "--once"],
                    data_dir,
                    {"BK_JOB_LOG_DIR": str(job_dir)},
                )
            finally:
                lease.release()

            self.assertEqual(result.returncode, 75)
            self.assertIn("another worker is active", result.stderr)

    def test_service_unit_captures_effective_data_and_job_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "shared data"
            jobs = root / "private jobs"
            units = root / "units"

            shown = self.run_bk(
                ["service", "show", "worker"],
                data_dir,
                {"BK_JOB_LOG_DIR": str(jobs)},
            )
            installed = self.run_bk(
                ["service", "install", "worker", "--target-dir", str(units)],
                data_dir,
                {"BK_JOB_LOG_DIR": str(jobs)},
            )

            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            unit = (units / "bk-worker.service").read_text(encoding="utf-8")
            self.assertEqual(shown.stdout, unit)
            self.assertIn(f'Environment="BK_DATA_DIR={data_dir}"', unit)
            self.assertIn(f'Environment="BK_JOB_LOG_DIR={jobs}"', unit)
            self.assertNotIn("BK_CONFIG_FILE", unit)
            self.assertNotIn("EnvironmentFile=", unit)
            self.assertIn(f"captured data directory: {data_dir}", installed.stdout)
            self.assertNotIn("captured config file:", installed.stdout)

    def test_service_unit_captures_an_external_trusted_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "shared"
            config_dir = root / "trusted"
            config_dir.mkdir(mode=0o700)
            config_path = config_dir / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            config_path.chmod(0o600)
            units = root / "units"

            installed = self.run_bk(
                ["service", "install", "monitor", "--target-dir", str(units)],
                data_dir,
                {"BK_CONFIG_FILE": str(config_path)},
            )

            self.assertEqual(installed.returncode, 0, installed.stderr)
            unit = (units / "bk-monitor.service").read_text(encoding="utf-8")
            self.assertIn(f'Environment="BK_CONFIG_FILE={config_path.resolve()}"', unit)
            self.assertNotIn("--interval", unit)
            self.assertNotIn("--rollup", unit)
            self.assertIn(f"captured config file: {config_path.resolve()}", installed.stdout)

    def test_usage_cli_exposes_stable_storage_and_dry_run_maintenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            storage = self.run_bk(["usage", "storage", "--json"], data_dir)
            maintenance = self.run_bk(["usage", "maintain", "--json"], data_dir)

            self.assertEqual(storage.returncode, 0, storage.stderr)
            self.assertIn('"kind": "usage-storage"', storage.stdout)
            self.assertEqual(maintenance.returncode, 0, maintenance.stderr)
            self.assertIn('"dry_run": true', maintenance.stdout)
            self.assertFalse((data_dir / "usage").exists())
            self.assertFalse((data_dir / "usage.lock").exists())

    def test_usage_cli_rejects_bad_historical_time_with_actionable_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["usage", "events", "--from", "hwo"], Path(tmp))

            self.assertEqual(result.returncode, 2)
            self.assertIn("YYYY-MM-DD HH:MM", result.stderr)

    def test_cli_schedules_and_runs_command_with_user_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            log_dir = Path(tmp) / "job-logs"
            start = iso(ceil_5m(datetime.now(timezone.utc)) - timedelta(minutes=5))
            env = {
                "BK_JOB_LOG_DIR": str(log_dir),
                "BK_WORKER_LIVE_GUARD": "0",
            }
            create = self.run_bk(
                [
                    "1",
                    "10m",
                    "--start",
                    start,
                    "--",
                    sys.executable,
                    "-c",
                    "import os; print('CUDA=' + os.environ['CUDA_VISIBLE_DEVICES'])",
                ],
                data_dir,
                env,
            )
            worker = self.run_bk(["w", "--once", "--quiet", "--poll", "0.1"], data_dir, env)
            jobs = self.run_bk(["j"], data_dir, env)
            log = self.run_bk(["jl", "1"], data_dir, env)
            cleanup = self.run_bk(["jobs", "--cleanup", "--json"], data_dir, env)

            self.assertEqual(create.returncode, 0, create.stderr)
            self.assertIn("job: pending", create.stdout)
            self.assertEqual(worker.returncode, 0, worker.stderr)
            self.assertEqual(jobs.returncode, 0, jobs.stderr)
            self.assertIn("succeeded", jobs.stdout)
            self.assertIn("CUDA=0", log.stdout)
            self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
            self.assertEqual(
                json.loads(cleanup.stdout)["private_job_cleanup"]["failed"],
                0,
            )
            self.assertEqual(
                json.loads(cleanup.stdout)["private_job_log_cleanup"]["failed"],
                0,
            )
            self.assertEqual(list((log_dir / "specs").glob("*.json")), [])

    def test_cli_cancellation_removes_a_pending_private_job_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            log_dir = root / "job-logs"
            start = iso(ceil_5m(datetime.now(timezone.utc) + timedelta(days=1)))
            env = {"BK_JOB_LOG_DIR": str(log_dir)}
            created = self.run_bk(
                [
                    "1",
                    "30m",
                    "--start",
                    start,
                    "--",
                    sys.executable,
                    "-c",
                    "print('private')",
                ],
                data_dir,
                env,
            )

            cancelled = self.run_bk(["d", "1"], data_dir, env)

            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            self.assertIn("cancelled:", cancelled.stdout)
            self.assertEqual(list((log_dir / "specs").glob("*.json")), [])

    def test_jobs_cleanup_keeps_json_contract_for_an_unsafe_private_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            log_dir = root / "job-logs"
            outside = root / "outside-specs"
            log_dir.mkdir(mode=0o700)
            outside.mkdir()
            (log_dir / "specs").symlink_to(outside, target_is_directory=True)

            result = self.run_bk(
                ["jobs", "--cleanup", "--json"],
                data_dir,
                {"BK_JOB_LOG_DIR": str(log_dir)},
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["private_job_cleanup"]["failed"], 1)
            self.assertIn("not a directory", payload["private_job_cleanup"]["warnings"][0])
            self.assertEqual(list(outside.iterdir()), [])

    def test_worker_once_returns_waiting_status_when_live_guard_blocks_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            log_dir = root / "job-logs"
            marker = root / "must-not-launch"
            simulation = root / "gpu-sim.json"
            simulation.write_text(
                json.dumps(
                    {
                        "gpus": [
                            {
                                "index": 0,
                                "name": "busy",
                                "memory_used_mb": 4096,
                                "memory_total_mb": 24000,
                                "utilization_percent": 80,
                                "processes": [
                                    {
                                        "pid": 9911,
                                        "uid": os.getuid() + 1,
                                        "username": "other",
                                        "command": "python rogue.py",
                                        "gpu_memory_mb": 4096,
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            start = iso(floor_5m(datetime.now(timezone.utc)))
            env = {
                "BK_JOB_LOG_DIR": str(log_dir),
                "BK_GPU_SIM_FILE": str(simulation),
            }
            create = self.run_bk(
                [
                    "1",
                    "10m",
                    "--gpu",
                    "0",
                    "--start",
                    start,
                    "--mem",
                    "1g",
                    "--",
                    sys.executable,
                    "-c",
                    f"open({str(marker)!r}, 'w').write('bad')",
                ],
                data_dir,
                env,
            )
            worker = self.run_bk(
                ["worker", "--once", "--quiet", "--poll", "0.1"],
                data_dir,
                env,
            )
            jobs = self.run_bk(["jobs"], data_dir, env)

            self.assertEqual(create.returncode, 0, create.stderr)
            self.assertEqual(worker.returncode, 3, worker.stderr)
            self.assertFalse(marker.exists())
            self.assertIn("pending", jobs.stdout)
            self.assertIn("note: GPU 0 has unreserved process", jobs.stdout)

    def test_operation_id_is_idempotent_for_agent_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            first = self.run_bk(["1", "30m", "--op-id", "agent-request-42"], data_dir)
            second = self.run_bk(["1", "30m", "--op-id", "agent-request-42"], data_dir)
            mismatch = self.run_bk(
                ["1", "45m", "--op-id", "agent-request-42", "--json"],
                data_dir,
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(mismatch.returncode, 2, mismatch.stderr)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(len(ledger["reservations"]), 1)
            self.assertIn("exists:", second.stdout)
            self.assertIn("different write", json.loads(mismatch.stdout)["error"]["message"])

    def test_agent_context_and_recommendation_are_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "not-created"
            context = self.run_bk(["agent", "context", "--compact"], data_dir)
            recommendation = self.run_bk(["agent", "recommend", "1", "30m", "--compact"], data_dir)

            self.assertEqual(context.returncode, 0, context.stderr)
            context_payload = json.loads(context.stdout)
            self.assertEqual(context_payload["schema_version"], "bk.agent.v1")
            self.assertEqual(context_payload["kind"], "context")
            self.assertEqual(recommendation.returncode, 0, recommendation.stderr)
            recommendation_payload = json.loads(recommendation.stdout)
            self.assertTrue(recommendation_payload["available"])
            self.assertEqual(recommendation_payload["recommendation"]["gpus"], [0])
            self.assertFalse(data_dir.exists())

    def test_agent_edit_and_cancel_are_structured_and_retry_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            start = iso(ceil_5m(datetime.now(timezone.utc) + timedelta(days=1)))
            created = self.run_bk(["1", "30m", "--start", start, "--json"], data_dir)
            created_payload = json.loads(created.stdout)
            short_id = created_payload["reservation"]["short_id"]

            edit_args = [
                "agent",
                "edit",
                short_id,
                "--duration",
                "45m",
                "--mem",
                "8g",
                "--op-id",
                "cli-agent-edit-1",
                "--compact",
            ]
            edited = self.run_bk(edit_args, data_dir)
            retried = self.run_bk(edit_args, data_dir)
            missing_operation_id = self.run_bk(
                ["agent", "edit", short_id, "--duration", "50m", "--compact"],
                data_dir,
            )
            mismatched = self.run_bk(
                [
                    "agent",
                    "edit",
                    short_id,
                    "--duration",
                    "50m",
                    "--op-id",
                    "cli-agent-edit-1",
                    "--compact",
                ],
                data_dir,
            )
            cancelled = self.run_bk(["agent", "cancel", short_id, "--compact"], data_dir)
            retry_after_cancel = self.run_bk(edit_args, data_dir)

            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(edited.returncode, 0, edited.stderr)
            self.assertEqual(retried.returncode, 0, retried.stderr)
            self.assertEqual(missing_operation_id.returncode, 2, missing_operation_id.stderr)
            self.assertEqual(mismatched.returncode, 2, mismatched.stderr)
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            self.assertEqual(retry_after_cancel.returncode, 0, retry_after_cancel.stderr)
            edited_payload = json.loads(edited.stdout)
            retried_payload = json.loads(retried.stdout)
            missing_operation_payload = json.loads(missing_operation_id.stdout)
            mismatch_payload = json.loads(mismatched.stdout)
            cancelled_payload = json.loads(cancelled.stdout)
            retry_after_cancel_payload = json.loads(retry_after_cancel.stdout)
            self.assertEqual(edited_payload["status"], "updated")
            self.assertEqual(retried_payload["status"], "exists")
            self.assertIn("operation ID is required", missing_operation_payload["error"]["message"])
            self.assertEqual(edited_payload["reservation"]["expected_memory_mb_per_gpu"], 8192)
            self.assertIn("different write", mismatch_payload["error"]["message"])
            self.assertEqual(cancelled_payload["kind"], "cancellation_result")
            self.assertEqual(cancelled_payload["reservation"]["status"], "cancelled")
            self.assertEqual(retry_after_cancel_payload["status"], "exists")
            self.assertEqual(retry_after_cancel_payload["reservation"]["status"], "cancelled")

    def test_booking_and_list_json_outputs_need_no_text_scraping(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            created = self.run_bk(["1", "30m", "--json"], data_dir)
            listed = self.run_bk(["l", "--json"], data_dir)

            self.assertEqual(created.returncode, 0, created.stderr)
            created_payload = json.loads(created.stdout)
            self.assertEqual(created_payload["kind"], "booking_result")
            self.assertEqual(created_payload["status"], "created")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            listed_payload = json.loads(listed.stdout)
            self.assertEqual(len(listed_payload["reservations"]), 1)
            self.assertEqual(
                listed_payload["reservations"][0]["id"],
                created_payload["reservation"]["id"],
            )

    def test_booking_json_error_is_structured(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            start = "2030-01-01T00:00:00Z"
            first = self.run_bk(["x", "1", "30m", "--start", start], data_dir)
            conflict = self.run_bk(["1", "30m", "--start", start, "--json"], data_dir)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(conflict.returncode, 2)
            payload = json.loads(conflict.stdout)
            self.assertEqual(payload["kind"], "error")
            self.assertIn("conflict", payload["error"]["message"])
            self.assertEqual(conflict.stderr, "")

    def test_reset_clears_ledger_logs_and_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            create = self.run_bk(["1", "30m"], data_dir)
            self.assertEqual(create.returncode, 0, create.stderr)
            self.assertTrue((data_dir / "ops.log").exists())
            (data_dir / "usage-events.jsonl").write_text("{}\n", encoding="utf-8")
            (data_dir / "usage-rollups.jsonl").write_text("{}\n", encoding="utf-8")
            (data_dir / "usage-state.json").write_text('{"version": 1, "processes": {}}\n', encoding="utf-8")

            reset = self.run_bk(["reset", "--yes"], data_dir)
            self.assertEqual(reset.returncode, 0, reset.stderr)
            self.assertIn("reset: removed", reset.stdout)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["reservations"], [])
            self.assertFalse((data_dir / "ops.log").exists())
            self.assertFalse((data_dir / "usage-events.jsonl").exists())
            self.assertFalse((data_dir / "usage-rollups.jsonl").exists())
            self.assertFalse((data_dir / "usage-state.json").exists())


if __name__ == "__main__":
    unittest.main()
