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

from bk.cli import _run_with_booking_deadline, main as bk_main
from bk.collector_status import collector_document
from bk.fileio import ensure_directory
from bk.usage_store import UsageAuditStore


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
        env["BK_BOOKING_HORIZON_DAYS"] = "3650"
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

    def run_bk_with_input(self, args, data_dir, text, extra_env=None):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["BK_DATA_DIR"] = str(data_dir)
        env["BK_GPU_COUNT"] = "2"
        env["BK_MAX_SHARED_USERS"] = "2"
        env["BK_BOOKING_HORIZON_DAYS"] = "3650"
        env["BK_GPU_SIM_FILE"] = str(Path(data_dir) / "missing-gpu-simulation.json")
        if extra_env:
            env.update(extra_env)
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
            cluster_env = {
                "BK_CLUSTER_CONFIG": str(data_dir / "missing-cluster.json")
            }

            version = self.run_bk(["--version"], data_dir)
            help_result = self.run_bk(["--help"], data_dir)
            skill_show = self.run_bk(["skill", "show"], data_dir)
            skill_install = self.run_bk(
                ["skill", "install", "--target", str(skill_dir)],
                data_dir,
            )
            cluster_help = self.run_bk(["c", "-h"], data_dir, cluster_env)
            admin_cluster_help = self.run_bk(
                ["admin", "cluster", "-h"], data_dir, cluster_env
            )
            cluster_status = self.run_bk(["c"], data_dir, cluster_env)

            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertRegex(version.stdout, r"^bk \d+\.\d+\.\d+")
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("GPUBK", help_result.stdout)
            self.assertEqual(skill_show.returncode, 0, skill_show.stderr)
            self.assertIn("name: gpubk", skill_show.stdout)
            self.assertEqual(skill_install.returncode, 0, skill_install.stderr)
            self.assertTrue((skill_dir / "SKILL.md").is_file())
            self.assertEqual(cluster_help.returncode, 0, cluster_help.stderr)
            self.assertIn("GPUBK cluster federation", cluster_help.stdout)
            self.assertEqual(
                admin_cluster_help.returncode, 0, admin_cluster_help.stderr
            )
            self.assertIn("remote-only catalog", admin_cluster_help.stdout)
            self.assertEqual(cluster_status.returncode, 2)
            self.assertIn("cluster mode is not configured", cluster_status.stderr)
            self.assertNotIn("JSONDecodeError", cluster_status.stderr)

            ordinary = self.run_bk(["doctor", "--json"], data_dir)
            self.assertEqual(ordinary.returncode, 2)
            self.assertIn("JSONDecodeError", ordinary.stdout)

    def test_default_command_starts_plain_interactive_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk_with_input([], Path(tmp), "status\nquit\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("GPUBK booking", result.stdout)
            self.assertIn("administrator:", result.stdout)
            self.assertIn("details: bk info", result.stdout)
            self.assertIn("bk> ", result.stdout)
            self.assertIn("GPU status", result.stdout)
            self.assertIn("0    unknown", result.stdout)
            self.assertIn("1    unknown", result.stdout)

    def test_login_notice_is_quiet_for_hook_and_explanatory_when_manual(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            hook = self.run_bk(["login", "--hook"], data_dir)
            manual = self.run_bk(["login"], data_dir)
            json_result = self.run_bk(["login", "--json"], data_dir)

            self.assertEqual(hook.returncode, 0, hook.stderr)
            self.assertEqual(hook.stdout, "")
            self.assertEqual(manual.returncode, 0, manual.stderr)
            self.assertIn("No active or upcoming reservations", manual.stdout)
            self.assertEqual(json_result.returncode, 0, json_result.stderr)
            payload = json.loads(json_result.stdout)
            self.assertEqual(payload["kind"], "login-notice")
            self.assertEqual(payload["active"], [])
            self.assertEqual(payload["upcoming"], [])

    def test_login_notice_reports_a_current_booking(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            booking = self.run_bk(["1", "5m"], data_dir)
            notice = self.run_bk(["login", "--hook"], data_dir)

            self.assertEqual(booking.returncode, 0, booking.stderr)
            self.assertEqual(notice.returncode, 0, notice.stderr)
            self.assertIn("GPUBK: 1 active", notice.stdout)
            self.assertIn("NOW", notice.stdout)
            self.assertIn("GPU 0", notice.stdout)

    def test_info_exposes_the_local_administrator_in_text_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            text_result = self.run_bk(["info"], data_dir)
            json_result = self.run_bk(["contact", "--compact"], data_dir)

            self.assertEqual(text_result.returncode, 0, text_result.stderr)
            self.assertIn("GPUBK server", text_result.stdout)
            self.assertIn("Administrator:", text_result.stdout)
            self.assertIn("Linux UID:", text_result.stdout)
            self.assertEqual(json_result.returncode, 0, json_result.stderr)
            document = json.loads(json_result.stdout)
            self.assertEqual(document["schema_version"], "gpubk.administrator.v1")
            self.assertEqual(document["kind"], "administrator")
            self.assertEqual(document["account"]["uid"], os.getuid())

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
                    "BK_GPU_COUNT": "8",
                    "BK_MAX_SHARED_USERS": "4",
                    "BK_WORKER_MAX_PARALLEL": "20",
                    "BK_WORKER_TERMINATION_GRACE_SECONDS": "7.5",
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
            self.assertEqual(payload["effective"]["access_mode"], "private")
            self.assertEqual(payload["effective"]["worker_max_parallel"], 20)
            self.assertEqual(payload["effective"]["worker_effective_max_parallel"], 20)
            self.assertEqual(
                payload["effective"]["worker_termination_grace_seconds"],
                7.5,
            )
            self.assertTrue(payload["effective"]["allocator_command_configured"])
            self.assertIsNone(payload["effective"]["storage_gid"])
            self.assertNotIn("secret-value", result.stdout)
            self.assertEqual(payload["ledger_policy"]["status"], "unbound")
            self.assertIn("BK_SLOT_MINUTES", payload["environment_overrides"])
            self.assertIn("BK_MONITOR_INTERVAL_SECONDS", payload["environment_overrides"])
            self.assertIn("BK_TUI_REFRESH_SECONDS", payload["environment_overrides"])
            self.assertIn("BK_WORKER_MAX_PARALLEL", payload["environment_overrides"])
            self.assertIn(
                "BK_WORKER_TERMINATION_GRACE_SECONDS",
                payload["environment_overrides"],
            )

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

    def test_daemons_return_config_exit_code_without_creating_runtime_state_on_policy_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            created = self.run_bk(["1", "30m", "--quiet"], data_dir)
            mismatch_env = {"BK_MAX_SHARED_USERS": "3"}

            worker = self.run_bk(["worker", "--once"], data_dir, mismatch_env)
            monitor = self.run_bk(["monitor", "--once"], data_dir, mismatch_env)

            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(worker.returncode, 78, worker.stderr)
            self.assertEqual(monitor.returncode, 78, monitor.stderr)
            self.assertIn("worker configuration does not match", worker.stderr)
            self.assertIn("monitor configuration does not match", monitor.stderr)
            self.assertEqual(list(data_dir.rglob("worker.lock")), [])
            self.assertFalse((data_dir / "usage.lock").exists())
            self.assertFalse((data_dir / "usage").exists())

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
            self.assertEqual(payload["config_file"]["owner_uid"], os.getuid())
            self.assertEqual(payload["effective"]["config_file"], str(config_path.resolve()))
            self.assertIsNone(payload["effective"]["monitor_uid"])
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

    def test_positional_memory_is_shorthand_for_per_gpu_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            result = self.run_bk(["1", "30m", "5g"], data_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["reservations"][0]["expected_memory_mb"], 5 * 1024)

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
                    "many",   # invalid GPU count
                    "1",
                    "7m",     # invalid duration
                    "30m",
                    "hwo",    # invalid start
                    "now",
                    "",       # automatic GPUs
                    "",       # default shared slot request
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

    def test_guided_add_defaults_to_one_gpu_and_thirty_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            user_input = "\n".join(["", "", "", "", "", "", "", "", "", ""])

            result = self.run_bk_with_input(["add"], data_dir, user_input)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("GPU count [1-2 enabled] (1)", result.stdout)
            self.assertIn("duration=30m", result.stdout)
            reservation = json.loads(
                (data_dir / "ledger.json").read_text(encoding="utf-8")
            )["reservations"][0]
            self.assertEqual(len(reservation["gpus"]), 1)
            start = datetime.fromisoformat(reservation["start_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(reservation["end_at"].replace("Z", "+00:00"))
            self.assertEqual(end - start, timedelta(minutes=30))

    def test_guided_add_accepts_auto_memory_as_a_share_weighted_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            simulation = data_dir / "gpus.json"
            simulation.write_text(
                json.dumps(
                    {
                        "gpus": [
                            {"index": 0, "name": "sim", "memory_total_mb": 40960},
                            {"index": 1, "name": "sim", "memory_total_mb": 40960},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            user_input = "\n".join(
                [
                    "",      # shared
                    "1",
                    "30m",
                    "now",
                    "",      # automatic GPUs
                    "",      # one shared slot
                    "",      # automatic share-weighted VRAM
                    "",      # no command
                    "",      # confirm
                    "",
                ]
            )

            result = self.run_bk_with_input(
                ["add"],
                data_dir,
                user_input,
                {
                    "BK_REQUIRE_SHARED_MEMORY": "1",
                    "BK_GPU_SIM_FILE": str(simulation),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Invalid input", result.stdout)
            self.assertIn("expected VRAM/GPU=auto (~", result.stdout)
            self.assertEqual(result.stdout.count("Review"), 1)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertGreater(ledger["reservations"][0]["expected_memory_mb"], 0)

    def test_strict_auto_memory_fails_closed_without_capacity_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            automatic = self.run_bk(
                ["1", "30m"], data_dir, {"BK_REQUIRE_SHARED_MEMORY": "1"}
            )
            explicit = self.run_bk(
                ["1", "30m", "5g"],
                data_dir,
                {"BK_REQUIRE_SHARED_MEMORY": "1"},
            )

            self.assertEqual(automatic.returncode, 2)
            self.assertIn("requires GPU memory telemetry", automatic.stderr)
            self.assertEqual(explicit.returncode, 0, explicit.stderr)

    def test_guided_add_can_go_back_and_cancel_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_input = "\n".join(["", "", "1", "back", "2", "cancel", ""])

            result = self.run_bk_with_input(["add"], Path(tmp), user_input)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("cancelled", result.stdout)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse((Path(tmp) / "ledger.json").exists())

    def test_guided_add_eof_cancels_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk_with_input(["add"], Path(tmp), "")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("cancelled", result.stdout)

    def test_guided_add_reports_max_used_and_requested_shared_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            start = iso(ceil_5m(datetime.now(timezone.utc) + timedelta(days=1)))
            existing = self.run_bk(
                ["1", "30m", "--gpu", "0", "--start", start],
                data_dir,
            )
            user_input = "\n".join(
                [
                    "",       # shared
                    "1",      # one GPU
                    "30m",
                    start,
                    "0",      # exact GPU
                    "cancel", # stop at the slot prompt
                    "",
                ]
            )

            guided = self.run_bk_with_input(["add"], data_dir, user_input)

            self.assertEqual(existing.returncode, 0, existing.stderr)
            self.assertEqual(guided.returncode, 0, guided.stderr)
            self.assertIn("max 2; used G0=1; request 1-2", guided.stdout)
            self.assertIn("cancelled", guided.stdout)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(len(ledger["reservations"]), 1)

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
            self.assertIn("used=3 max=2", result.stdout)
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
            job_dir = Path(tmp) / "private-jobs"

            result = self.run_bk(
                ["doctor", "--json"],
                data_dir,
                {"BK_JOB_LOG_DIR": str(job_dir)},
            )
            required = self.run_bk(
                ["doctor", "--require-monitor", "--json", "--strict"],
                data_dir,
                {"BK_JOB_LOG_DIR": str(job_dir)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema_version"], "gpubk.doctor.v1")
            self.assertEqual(payload["probes"], [])
            self.assertTrue(payload["healthy"])
            self.assertIsNone(payload["ready"])
            self.assertFalse(payload["monitor_required"])
            self.assertFalse(payload["worker_required"])
            self.assertEqual(payload["worker"]["state"], "not-seen")
            self.assertEqual(required.returncode, 2, required.stderr)
            required_payload = json.loads(required.stdout)
            self.assertTrue(required_payload["monitor_required"])
            self.assertEqual(required_payload["collector"]["state"], "not-seen")
            self.assertEqual(
                required_payload["policy_issues"],
                [
                    {
                        "type": "monitor-health",
                        "message": (
                            "collector heartbeat has not been recorded; start the monitor and retry"
                        ),
                    }
                ],
            )
            self.assertFalse(data_dir.exists())
            self.assertFalse(job_dir.exists())

    def test_doctor_require_worker_is_read_only_and_rejects_an_absent_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "not-created"
            job_dir = Path(tmp) / "private-jobs"

            result = self.run_bk(
                ["doctor", "--require-worker", "--json", "--strict"],
                data_dir,
                {"BK_JOB_LOG_DIR": str(job_dir)},
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["worker_required"])
            self.assertEqual(payload["worker"]["state"], "not-seen")
            self.assertFalse(payload["healthy"])
            issue = next(
                item for item in payload["policy_issues"] if item["type"] == "worker-health"
            )
            self.assertIn("state=not-seen", issue["message"])
            self.assertFalse(data_dir.exists())
            self.assertFalse(job_dir.exists())

    def test_doctor_require_worker_accepts_only_the_current_data_instance(self):
        from bk.config import Config
        from bk.identity import current_actor
        from bk.joblogs import acquire_job_worker_lease

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            job_dir = root / "private-jobs"
            environment = {"BK_JOB_LOG_DIR": str(job_dir)}
            config = Config(data_dir=data_dir, gpu_count=1, job_log_dir=job_dir)
            lease = acquire_job_worker_lease(
                config,
                current_actor(),
                "doctor-worker",
                "test-host",
            )
            try:
                result = self.run_bk(
                    ["doctor", "--require-worker", "--json", "--strict"],
                    data_dir,
                    environment,
                )
                human = self.run_bk(
                    ["doctor", "--require-worker", "--strict"],
                    data_dir,
                    environment,
                )
            finally:
                lease.release()

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["healthy"])
            self.assertTrue(payload["worker_required"])
            self.assertEqual(payload["worker"]["state"], "running")
            self.assertTrue(payload["worker"]["running"])
            self.assertEqual(human.returncode, 0, human.stderr)
            self.assertIn("worker is running for this data directory", human.stdout)

            stopped = self.run_bk(
                ["doctor", "--require-worker", "--json", "--strict"],
                data_dir,
                environment,
            )
            self.assertEqual(stopped.returncode, 2, stopped.stderr)
            stopped_payload = json.loads(stopped.stdout)
            self.assertEqual(stopped_payload["worker"]["state"], "stopped")
            self.assertTrue(
                any(
                    item["type"] == "worker-health"
                    for item in stopped_payload["policy_issues"]
                )
            )

            other_config = Config(
                data_dir=root / "other-data",
                gpu_count=1,
                job_log_dir=job_dir,
            )
            other_lease = acquire_job_worker_lease(
                other_config,
                current_actor(),
                "other-doctor-worker",
                "test-host",
            )
            try:
                other = self.run_bk(
                    ["doctor", "--require-worker", "--json", "--strict"],
                    data_dir,
                    environment,
                )
            finally:
                other_lease.release()

            self.assertEqual(other.returncode, 2, other.stderr)
            other_payload = json.loads(other.stdout)
            self.assertEqual(other_payload["worker"]["state"], "other-instance")
            issue = next(
                item
                for item in other_payload["policy_issues"]
                if item["type"] == "worker-health"
            )
            self.assertIn("state=other-instance", issue["message"])

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

    def test_doctor_probe_rejects_configured_gpu_topology_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "probe-data"
            simulation = Path(tmp) / "gpu-sim.json"
            simulation.write_text(
                json.dumps({"gpus": [{"index": 0, "name": "only-one"}]}),
                encoding="utf-8",
            )

            result = self.run_bk(
                ["doctor", "--probe", "--json", "--strict"],
                data_dir,
                {"BK_GPU_SIM_FILE": str(simulation), "BK_GPU_COUNT": "2"},
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            gpu = next(item for item in payload["probes"] if item["name"] == "gpu-telemetry")
            self.assertEqual(gpu["status"], "fail")
            self.assertIn("topology", gpu["message"])
            self.assertEqual(gpu["configured_device_count"], 2)
            self.assertEqual(gpu["indices"], [0])
            self.assertFalse(payload["ready"])

    def test_doctor_reports_a_stale_collector_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            now = datetime.now(timezone.utc).replace(microsecond=0)
            sampled_at = now - timedelta(minutes=10)
            UsageAuditStore(data_dir).save_collector_status(
                collector_document(
                    monitor_id="monitor-doctor",
                    status="running",
                    uid=os.getuid(),
                    pid=4321,
                    hostname="gpu-host",
                    heartbeat_interval_seconds=60.0,
                    sample_interval_seconds=2.0,
                    rollup_seconds=60,
                    started_at=sampled_at - timedelta(minutes=5),
                    sampled_at=sampled_at,
                    written_at=sampled_at,
                    devices=[
                        {
                            "gpu": 0,
                            "source": "nvml",
                            "device_telemetry": True,
                            "stable_device_identifier": True,
                            "process_telemetry": True,
                            "process_utilization": True,
                        }
                    ],
                    stable_device_identifier_gap=[],
                    process_telemetry_gap=[],
                    process_utilization_gap=[],
                )
            )

            result = self.run_bk(["doctor", "--json", "--strict"], data_dir)

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["collector"]["state"], "stale")
            issue = next(
                item for item in payload["policy_issues"] if item["type"] == "monitor-health"
            )
            self.assertIn("heartbeat is stale", issue["message"])
            self.assertFalse(payload["healthy"])

    def test_doctor_post_start_verification_accepts_a_fresh_complete_monitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            now = datetime.now(timezone.utc).replace(microsecond=0)
            UsageAuditStore(data_dir).save_collector_status(
                collector_document(
                    monitor_id="monitor-running",
                    status="running",
                    uid=os.getuid(),
                    pid=4321,
                    hostname="gpu-host",
                    heartbeat_interval_seconds=60.0,
                    sample_interval_seconds=2.0,
                    rollup_seconds=60,
                    started_at=now - timedelta(minutes=1),
                    sampled_at=now,
                    written_at=now,
                    devices=[
                        {
                            "gpu": 0,
                            "source": "nvml",
                            "device_telemetry": True,
                            "stable_device_identifier": True,
                            "process_telemetry": True,
                            "process_utilization": True,
                        }
                    ],
                    stable_device_identifier_gap=[],
                    process_telemetry_gap=[],
                    process_utilization_gap=[],
                )
            )

            result = self.run_bk(
                ["doctor", "--require-monitor", "--json", "--strict"],
                data_dir,
            )
            human = self.run_bk(
                ["doctor", "--require-monitor", "--strict"],
                data_dir,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["healthy"])
            self.assertTrue(payload["monitor_required"])
            self.assertEqual(payload["collector"]["state"], "running")
            self.assertEqual(human.returncode, 0, human.stderr)
            self.assertIn("fresh, complete telemetry", human.stdout)

    def test_doctor_post_start_rejects_missing_stable_device_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            now = datetime.now(timezone.utc).replace(microsecond=0)
            UsageAuditStore(data_dir).save_collector_status(
                collector_document(
                    monitor_id="monitor-no-stable-id",
                    status="degraded",
                    uid=os.getuid(),
                    pid=4321,
                    hostname="gpu-host",
                    heartbeat_interval_seconds=60.0,
                    sample_interval_seconds=2.0,
                    rollup_seconds=60,
                    started_at=now - timedelta(minutes=1),
                    sampled_at=now,
                    written_at=now,
                    devices=[
                        {
                            "gpu": 0,
                            "source": "nvml",
                            "device_telemetry": True,
                            "stable_device_identifier": False,
                            "process_telemetry": True,
                            "process_utilization": True,
                        }
                    ],
                    stable_device_identifier_gap=[0],
                    process_telemetry_gap=[],
                    process_utilization_gap=[],
                )
            )

            result = self.run_bk(
                ["doctor", "--require-monitor", "--json", "--strict"],
                data_dir,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["collector"]["state"], "degraded")
            self.assertEqual(
                payload["collector"]["stable_device_identifier_gap"],
                [0],
            )
            issue = next(
                item
                for item in payload["policy_issues"]
                if item["type"] == "monitor-health"
            )
            self.assertIn("stable identifier gaps=[0]", issue["message"])

    def test_doctor_post_start_rejects_unattributed_gpu_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            now = datetime.now(timezone.utc).replace(microsecond=0)
            UsageAuditStore(data_dir).save_collector_status(
                collector_document(
                    monitor_id="monitor-no-process-identity",
                    status="degraded",
                    uid=os.getuid(),
                    pid=4321,
                    hostname="gpu-host",
                    heartbeat_interval_seconds=60.0,
                    sample_interval_seconds=2.0,
                    rollup_seconds=60,
                    started_at=now - timedelta(minutes=1),
                    sampled_at=now,
                    written_at=now,
                    devices=[
                        {
                            "gpu": 0,
                            "source": "nvml",
                            "device_telemetry": True,
                            "stable_device_identifier": True,
                            "process_telemetry": True,
                            "process_utilization": True,
                        }
                    ],
                    stable_device_identifier_gap=[],
                    process_telemetry_gap=[],
                    process_identity_gap=[0],
                    process_utilization_gap=[],
                )
            )

            result = self.run_bk(
                ["doctor", "--require-monitor", "--json", "--strict"],
                data_dir,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["collector"]["state"], "degraded")
            self.assertEqual(payload["collector"]["process_identity_gap"], [0])
            usage = self.run_bk(["usage"], data_dir)
            self.assertEqual(usage.returncode, 0, usage.stderr)
            self.assertIn("collector: degraded", usage.stdout)
            self.assertIn("gaps=identity:0", usage.stdout)
            self.assertIn("sampled past only; future reservations excluded", usage.stdout)
            self.assertIn("Last 7 days", usage.stdout)
            self.assertIn("Last 4 weeks", usage.stdout)
            issue = next(
                item
                for item in payload["policy_issues"]
                if item["type"] == "monitor-health"
            )
            self.assertIn("process identity gaps=[0]", issue["message"])

    def test_doctor_rejects_a_fresh_collector_for_the_wrong_gpu_topology(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            now = datetime.now(timezone.utc).replace(microsecond=0)
            UsageAuditStore(data_dir).save_collector_status(
                collector_document(
                    monitor_id="monitor-old-config",
                    status="running",
                    uid=os.getuid(),
                    pid=4321,
                    hostname="gpu-host",
                    heartbeat_interval_seconds=60.0,
                    sample_interval_seconds=2.0,
                    rollup_seconds=60,
                    started_at=now - timedelta(minutes=1),
                    sampled_at=now,
                    written_at=now,
                    devices=[
                        {
                            "gpu": 0,
                            "source": "nvml",
                            "device_telemetry": True,
                            "stable_device_identifier": True,
                            "process_telemetry": True,
                            "process_utilization": True,
                        }
                    ],
                    stable_device_identifier_gap=[],
                    process_telemetry_gap=[],
                    process_utilization_gap=[],
                )
            )

            result = self.run_bk(
                ["doctor", "--json", "--strict"],
                data_dir,
                {"BK_GPU_COUNT": "2"},
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["collector"]["state"], "topology-mismatch")
            self.assertFalse(payload["collector"]["fresh"])
            issue = next(
                item for item in payload["policy_issues"] if item["type"] == "monitor-health"
            )
            self.assertIn("reports 1 GPU(s)", issue["message"])
            self.assertIn("expects 2", issue["message"])

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

    def test_doctor_reports_configured_storage_gid_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "shared"
            data_dir.mkdir(mode=0o700)
            data_dir.chmod(0o2770)
            actual_gid = data_dir.stat().st_gid
            config_dir = root / "trusted"
            config_dir.mkdir(mode=0o700)
            config_path = config_dir / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "data_dir": str(data_dir),
                        "gpu_count": 1,
                        "file_mode": "0660",
                        "dir_mode": "2770",
                        "storage_gid": actual_gid + 1,
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)

            result = self.run_bk(
                ["doctor", "--json", "--strict"],
                data_dir,
                {"BK_CONFIG_FILE": str(config_path)},
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["storage_gid"], actual_gid + 1)
            root_issues = [
                item
                for item in payload["storage_issues"]
                if item.get("path") == str(data_dir)
                and item["type"] in {"directory-gid", "usage-directory-gid"}
            ]
            self.assertEqual(len(root_issues), 1)
            issue = root_issues[0]
            self.assertEqual(issue["type"], "directory-gid")
            self.assertEqual(issue["expected_gid"], actual_gid + 1)
            self.assertEqual(issue["actual_gid"], actual_gid)

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
            issue = next(
                item
                for item in payload["storage_issues"]
                if item["type"] == "ledger-read"
            )
            self.assertIn("reservations[0].uid", issue["message"])
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
            issue = next(
                item
                for item in payload["storage_issues"]
                if item["type"] == "ledger-read"
            )
            self.assertIn("reservations[0] must be an object", issue["message"])
            self.assertFalse(payload["healthy"])

    def test_semantic_ledger_corruption_fails_cleanly_for_humans_and_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            ledger = data_dir / "ledger.json"
            ledger.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "reservations": [{"id": "incomplete"}],
                    }
                ),
                encoding="utf-8",
            )
            ledger.chmod(0o600)

            status = self.run_bk(["status"], data_dir)
            agent = self.run_bk(["agent", "context", "--compact"], data_dir)

            self.assertEqual(status.returncode, 2)
            self.assertIn("reservations[0].uid", status.stderr)
            self.assertNotIn("Traceback", status.stderr)
            self.assertEqual(agent.returncode, 2)
            payload = json.loads(agent.stdout)
            self.assertEqual(payload["kind"], "error")
            self.assertEqual(payload["error"]["type"], "LedgerCorruptionError")
            self.assertIn("reservations[0].uid", payload["error"]["message"])

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

    def test_direct_edit_rejects_zero_gpu_count_without_entering_guided_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            create = self.run_bk(
                ["1", "30m", "--start", "2030-01-01T00:00:00Z"],
                data_dir,
            )
            before = (data_dir / "ledger.json").read_bytes()

            edit = self.run_bk(["edit", "1", "--count", "0"], data_dir)

            self.assertEqual(create.returncode, 0, create.stderr)
            self.assertEqual(edit.returncode, 2)
            self.assertIn("GPU count must be >= 1", edit.stderr)
            self.assertNotIn("Guided edit", edit.stdout)
            self.assertEqual((data_dir / "ledger.json").read_bytes(), before)

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
                    "7m",    # invalid duration
                    "45m",
                    "hwo",   # invalid start
                    "",      # keep start
                    "",      # keep GPUs
                    "",      # keep count
                    "",      # keep shared slot request
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

    def test_integer_shared_slots_use_one_capacity_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            env = {"BK_MAX_SHARED_USERS": "4"}

            weighted = self.run_bk(
                ["1", "30m", "--gpu", "0", "--share", "3", "-q"],
                data_dir,
                env,
            )
            remaining = self.run_bk(["1", "30m", "--gpu", "0", "-q"], data_dir, env)
            queued = self.run_bk(["1", "30m", "--gpu", "0", "-q"], data_dir, env)

            self.assertEqual(weighted.returncode, 0, weighted.stderr)
            self.assertEqual(remaining.returncode, 0, remaining.stderr)
            self.assertEqual(queued.returncode, 0, queued.stderr)
            self.assertIn("share=3 slots (max 4)", weighted.stdout)
            self.assertIn("share=1 slot (max 4)", remaining.stdout)
            self.assertIn("queued:", queued.stdout)
            ledger = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual([item["share_units"] for item in ledger["reservations"]], [3, 1, 1])
            self.assertLess(
                ledger["reservations"][1]["start_at"],
                ledger["reservations"][0]["end_at"],
            )
            self.assertEqual(ledger["reservations"][0]["end_at"], ledger["reservations"][2]["start_at"])

            payload_result = self.run_bk(
                ["1", "30m", "--share", "3", "--json"],
                Path(tmp) / "json",
                env,
            )
            self.assertEqual(payload_result.returncode, 0, payload_result.stderr)
            payload = json.loads(payload_result.stdout)
            self.assertEqual(payload["reservation"]["share_units_per_gpu"], 3)
            self.assertEqual(payload["reservation"]["share_capacity_units_per_gpu"], 4)
            self.assertNotIn("share_fraction_per_gpu", payload["reservation"])

    def test_direct_edit_can_change_shared_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            env = {"BK_MAX_SHARED_USERS": "4"}
            created = self.run_bk(
                ["1", "30m", "--start", "2030-01-01T00:00:00Z"],
                data_dir,
                env,
            )
            edited = self.run_bk(["e", "1", "--share", "3"], data_dir, env)

            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(edited.returncode, 0, edited.stderr)
            self.assertIn("share=3 slots (max 4)", edited.stdout)
            reservation = json.loads((data_dir / "ledger.json").read_text(encoding="utf-8"))["reservations"][0]
            self.assertEqual(reservation["share_units"], 3)

    def test_slots_preserves_share_in_copyable_booking_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(
                ["slots", "1", "30m", "--share", "3", "--limit", "1"],
                Path(tmp),
                {"BK_MAX_SHARED_USERS": "4"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("share 3 slots (max 4)", result.stdout)
            self.assertIn("--share 3", result.stdout)

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
                ["1", "30m", "--start", "2030-01-01T00:00:00Z", "--share", "3"],
                data_dir,
                env,
            )
            second = self.run_bk(
                ["1", "30m", "--start", "2030-01-01T00:00:00Z", "--share", "1"],
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

    def test_timeline_keeps_retained_expired_history_but_hides_cancellations(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            created = self.run_bk(
                ["1", "30m", "--start", "2030-01-01T00:00:00Z"],
                data_dir,
            )
            self.assertEqual(created.returncode, 0, created.stderr)

            ledger_path = data_dir / "ledger.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            reservation = ledger["reservations"][0]
            reservation.update(
                {
                    "start_at": "2020-01-01T00:00:00Z",
                    "end_at": "2020-01-01T00:30:00Z",
                    "status": "expired",
                }
            )
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

            historical = self.run_bk(
                [
                    "tl",
                    "--from",
                    "2020-01-01T00:00:00Z",
                    "--window",
                    "30m",
                    "--step",
                    "5m",
                ],
                data_dir,
            )
            self.assertEqual(historical.returncode, 0, historical.stderr)
            historical_gpu = next(
                line for line in historical.stdout.splitlines() if line.startswith("G0")
            )
            self.assertIn("M1", historical_gpu)

            reservation["status"] = "cancelled"
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            cancelled = self.run_bk(
                [
                    "tl",
                    "--from",
                    "2020-01-01T00:00:00Z",
                    "--window",
                    "30m",
                    "--step",
                    "5m",
                ],
                data_dir,
            )
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            cancelled_gpu = next(
                line for line in cancelled.stdout.splitlines() if line.startswith("G0")
            )
            self.assertNotIn("M1", cancelled_gpu)

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
            self.assertIn("START HERE\n", result.stdout)
            self.assertIn("bk tutorial", result.stdout)
            self.assertIn("BOOK\n", result.stdout)
            self.assertIn("VIEW\n", result.stdout)
            self.assertIn("bk 2 1h", result.stdout)
            self.assertIn("bk book 2 1h", result.stdout)
            self.assertIn("bk e ID --at 20:00", result.stdout)
            self.assertTrue(all(len(line) <= 72 for line in result.stdout.splitlines()), result.stdout)

    def test_help_hides_administrator_commands_from_ordinary_users(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            config = data_dir / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "data_dir": str(data_dir / "data"),
                        "gpu_count": 1,
                        "monitor_uid": os.getuid() + 1,
                        "file_mode": "0600",
                        "dir_mode": "0700",
                    }
                ),
                encoding="utf-8",
            )
            config.chmod(0o600)
            result = self.run_bk(
                ["--help"],
                data_dir / "unused",
                {"BK_CONFIG_FILE": str(config)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("AUTOMATION", result.stdout)
            self.assertIn("bk agent context", result.stdout)
            self.assertIn("bk doctor", result.stdout)
            self.assertNotIn("ADMINISTRATION", result.stdout)
            self.assertNotIn("bk admin init", result.stdout)

    def test_run_uses_gpus_from_the_current_users_active_booking(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            created = self.run_bk(["1", "30m", "--gpu", "0"], data_dir)
            result = self.run_bk(
                [
                    "run",
                    "--",
                    sys.executable,
                    "-c",
                    "import os; print(os.environ['CUDA_VISIBLE_DEVICES'])",
                ],
                data_dir,
            )

            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("run: GPU=0", result.stdout)
            self.assertEqual(result.stdout.splitlines()[-1], "0")

    def test_run_without_active_booking_is_read_only_and_recommends_booking(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            result = self.run_bk(["run"], data_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no booking is active now", result.stdout)
            self.assertIn("bk run 1 30m -- python train.py", result.stdout)
            self.assertFalse((data_dir / "ledger.json").exists())

    def test_gpu_shortcut_is_read_only_and_suggests_one_gpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            result = self.run_bk(["g"], data_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("GPU 0 | suggested", result.stdout)
            self.assertIn("book: bk 1 30m --gpu 0", result.stdout)
            self.assertFalse((data_dir / "ledger.json").exists())

    def test_gpu_shortcut_prefers_active_booking_and_shows_live_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            simulation = data_dir / "gpu.json"
            simulation.write_text(
                json.dumps(
                    {
                        "gpus": [
                            {
                                "index": 0,
                                "name": "sim",
                                "memory_used_mb": 4096,
                                "memory_total_mb": 32768,
                                "utilization_percent": 42,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            env = {"BK_GPU_SIM_FILE": str(simulation)}
            created = self.run_bk(["1", "30m", "--gpu", "0"], data_dir, env)
            result = self.run_bk(["gpu"], data_dir, env)

            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("GPU 0 | yours now", result.stdout)
            self.assertIn("42% util", result.stdout)
            self.assertIn("28.0GiB free", result.stdout)
            self.assertIn("run: bk run -- COMMAND", result.stdout)

    def test_common_booking_flags_have_short_forms(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            result = self.run_bk(
                ["1", "30m", "-g", "0", "-m", "4g", "-s", "1"],
                data_dir,
            )
            help_result = self.run_bk(["1", "30m", "-h"], data_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("gpu=0", result.stdout)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("-g GPU", help_result.stdout)
            self.assertIn("-m MEM", help_result.stdout)
            self.assertIn("-s SLOTS", help_result.stdout)

    def test_immediate_run_stops_at_the_booking_deadline(self):
        deadline = datetime.now(timezone.utc) + timedelta(seconds=0.1)

        with redirect_stderr(StringIO()) as stderr:
            result = _run_with_booking_deadline(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                os.environ.copy(),
                deadline,
            )

        self.assertEqual(result, 124)
        self.assertIn("reservation ended", stderr.getvalue())

    def test_booking_run_queues_a_private_command_for_the_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            blocker = self.run_bk(["1", "30m", "--share", "2"], data_dir)
            result = self.run_bk(
                ["run", "1", "30m", "--", sys.executable, "-c", "print('later')"],
                data_dir,
            )

            self.assertEqual(blocker.returncode, 0, blocker.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("queued:", result.stdout)
            self.assertIn("your worker will launch", result.stdout)
            reservations = json.loads(
                (data_dir / "ledger.json").read_text(encoding="utf-8")
            )["reservations"]
            queued = reservations[-1]
            self.assertEqual(queued["job"]["status"], "pending")
            self.assertGreater(queued["start_at"], reservations[0]["start_at"])

    def test_contextual_help_never_starts_interactive_or_protocol_entrypoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "absent"
            cases = (
                (["add", "--help"], "usage: bk add", "mode [s shared"),
                (["tui", "--help"], "usage: bk tui", "GPUBK TUI fallback"),
                (["tutorial", "--help"], "usage: bk tutorial", "GPUBK tutorial 1/"),
                (["mcp", "--help"], "usage: bk mcp", "MCP server requires"),
                (["usage", "--help"], "usage: bk usage", "usage: bk usage me"),
                (["book", "--help"], "usage: bk book", "Unknown command"),
                (["c", "--help"], "GPUBK cluster federation", "Unknown command"),
                (
                    ["c", "probe", "--help"],
                    "usage: bk cluster probe",
                    "Unknown command",
                ),
                (["help", "add"], "usage: bk add", "mode [s shared"),
                (["help", "usage"], "usage: bk usage", "usage: bk usage me"),
            )
            for args, expected, forbidden in cases:
                with self.subTest(args=args):
                    result = self.run_bk(args, data_dir)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(expected, result.stdout)
                    self.assertNotIn(forbidden, result.stdout + result.stderr)
            self.assertFalse(data_dir.exists())

    def test_tutorial_is_replayable_and_does_not_create_a_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            result = self.run_bk(
                ["tutorial"],
                data_dir,
                {"XDG_STATE_HOME": str(root / "state")},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("GPUBK tutorial 1/", result.stdout)
            self.assertIn("bk 1 30m", result.stdout)
            self.assertIn("bk tutorial --tui", result.stdout)
            self.assertNotIn("\x1b[", result.stdout)
            self.assertFalse((data_dir / "ledger.json").exists())

    def test_launch_only_commands_reject_unexpected_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "absent"
            for command in ("add", "tui", "mcp"):
                with self.subTest(command=command):
                    result = self.run_bk([command, "unexpected"], data_dir)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("unrecognized arguments: unexpected", result.stderr)
            self.assertFalse(data_dir.exists())

    def test_explicit_book_alias_creates_the_same_default_shared_booking(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["book", "1", "30m", "--quiet"], Path(tmp))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith("created:"), result.stdout)
            self.assertIn("mode=shared", result.stdout)

    def test_exact_create_and_agent_recommend_reject_a_historical_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            past = floor_5m(datetime.now(timezone.utc)) - timedelta(minutes=5)
            start = iso(past)

            create = self.run_bk(["1", "30m", "--start", start], data_dir)
            recommend = self.run_bk(
                [
                    "agent",
                    "recommend",
                    "1",
                    "30m",
                    "--start",
                    start,
                    "--compact",
                ],
                data_dir,
            )

            self.assertEqual(create.returncode, 2)
            self.assertIn("current booking slice", create.stderr)
            self.assertEqual(recommend.returncode, 2)
            payload = json.loads(recommend.stdout)
            self.assertEqual(payload["kind"], "error")
            self.assertIn("current booking slice", payload["error"]["message"])
            self.assertFalse((data_dir / "ledger.json").exists())

    def test_unknown_command_uses_the_default_english_interface(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bk(["not-a-command"], Path(tmp))

            self.assertEqual(result.returncode, 2)
            self.assertIn("Unknown command: not-a-command", result.stderr)
            self.assertNotIn("\u672a\u77e5\u547d\u4ee4", result.stderr)

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
            self.assertIn("collector: stopped at=", events.stdout)
            self.assertIn("unreserved", events.stdout)
            self.assertIn("train.py", events.stdout)
            self.assertEqual(rollups.returncode, 0, rollups.stderr)
            self.assertIn('"partial": true', rollups.stdout)
            self.assertIn('"schema_version": "gpubk.usage.v1"', rollups.stdout)
            self.assertEqual(json.loads(rollups.stdout)["collector"]["state"], "stopped")

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

    def test_worker_status_is_read_only_and_strictly_verifies_the_kernel_lease(self):
        from bk.config import Config
        from bk.identity import current_actor
        from bk.joblogs import acquire_job_worker_lease

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            job_dir = root / "jobs"
            env = {"BK_JOB_LOG_DIR": str(job_dir)}

            unseen = self.run_bk(["worker", "--status", "--json"], data_dir, env)
            shorthand = self.run_bk(["w"], data_dir, env)
            required = self.run_bk(["w", "--require-running", "--json"], data_dir, env)

            self.assertEqual(unseen.returncode, 0, unseen.stderr)
            self.assertEqual(json.loads(unseen.stdout)["state"], "not-seen")
            self.assertEqual(shorthand.returncode, 0, shorthand.stderr)
            self.assertIn("bk w start", shorthand.stdout)
            self.assertEqual(required.returncode, 2, required.stderr)
            self.assertFalse(job_dir.exists())

            config = Config(data_dir=data_dir, gpu_count=1, job_log_dir=job_dir)
            lease = acquire_job_worker_lease(config, current_actor(), "holder", "test-host")
            try:
                running = self.run_bk(
                    ["worker", "--status", "--require-running", "--json"],
                    data_dir,
                    env,
                )
                jobs = self.run_bk(["jobs", "--json"], data_dir, env)
            finally:
                lease.release()
            stopped = self.run_bk(["worker", "--status"], data_dir, env)

            self.assertEqual(running.returncode, 0, running.stderr)
            running_payload = json.loads(running.stdout)
            self.assertEqual(running_payload["state"], "running")
            self.assertTrue(running_payload["running"])
            self.assertEqual(running_payload["lease"]["worker_id"], "holder")
            self.assertEqual(jobs.returncode, 0, jobs.stderr)
            self.assertEqual(json.loads(jobs.stdout)["worker"]["state"], "running")
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            self.assertIn("worker: stopped", stopped.stdout)

            other_config = Config(
                data_dir=root / "other-data",
                gpu_count=1,
                job_log_dir=job_dir,
            )
            other_lease = acquire_job_worker_lease(
                other_config,
                current_actor(),
                "other-holder",
                "test-host",
            )
            try:
                other_status = self.run_bk(
                    ["worker", "--status", "--require-running", "--json"],
                    data_dir,
                    env,
                )
                other_human = self.run_bk(["worker", "--status"], data_dir, env)
            finally:
                other_lease.release()

            self.assertEqual(other_status.returncode, 2, other_status.stderr)
            other_payload = json.loads(other_status.stdout)
            self.assertEqual(other_payload["state"], "other-instance")
            self.assertFalse(other_payload["running"])
            self.assertTrue(other_payload["lease_held"])
            self.assertFalse(other_payload["instance_match"])
            self.assertEqual(other_human.returncode, 0, other_human.stderr)
            self.assertIn("worker: other instance", other_human.stdout)

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
            self.assertIn("bk doctor --require-worker --strict", installed.stdout)
            self.assertIn("sudo loginctl enable-linger", installed.stdout)

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
            self.assertIn("bk doctor --require-monitor --strict", installed.stdout)
            self.assertIn("sudo loginctl enable-linger", installed.stdout)

    def test_shared_monitor_policy_fails_before_runtime_or_service_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "absent-data"
            units = root / "absent-units"
            environment = {"BK_FILE_MODE": "0660", "BK_DIR_MODE": "2770"}

            monitor = self.run_bk(["monitor", "--once"], data_dir, environment)
            service = self.run_bk(
                ["service", "install", "monitor", "--target-dir", str(units)],
                data_dir,
                environment,
            )
            maintenance_dry_run = self.run_bk(
                ["usage", "maintain", "--json"],
                data_dir,
                environment,
            )
            maintenance = self.run_bk(
                ["usage", "maintain", "--yes"],
                data_dir,
                environment,
            )
            migration = self.run_bk(
                ["usage", "migrate", "--yes"],
                data_dir,
                environment,
            )
            doctor = self.run_bk(
                ["doctor", "--json", "--strict"],
                data_dir,
                environment,
            )

            self.assertEqual(monitor.returncode, 77)
            self.assertIn("trusted external or system", monitor.stderr)
            self.assertEqual(service.returncode, 77)
            self.assertIn("trusted external or system", service.stderr)
            self.assertEqual(maintenance_dry_run.returncode, 0, maintenance_dry_run.stderr)
            self.assertEqual(maintenance.returncode, 77)
            self.assertEqual(migration.returncode, 77)
            self.assertFalse(data_dir.exists())
            self.assertFalse(units.exists())
            self.assertEqual(doctor.returncode, 2)
            doctor_payload = json.loads(doctor.stdout)
            self.assertIsNone(doctor_payload["monitor_uid"])
            policy_issues = doctor_payload["policy_issues"]
            self.assertEqual(policy_issues[0]["type"], "monitor-policy")

    def test_assigned_shared_monitor_can_install_its_user_service(self):
        from bk.cli import _service_command
        from bk.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            units = root / "units"
            config = Config(
                data_dir=root / "shared",
                file_mode=0o660,
                dir_mode=0o2770,
                config_file=Path("/etc/gpubk/config.json"),
                config_owner_uid=0,
                monitor_uid=os.getuid(),
            )
            output = StringIO()

            with redirect_stdout(output):
                result = _service_command(
                    ["install", "monitor", "--target-dir", str(units)],
                    config,
                )

            self.assertEqual(result, 0)
            self.assertTrue((units / "bk-monitor.service").is_file())
            self.assertIn("captured config file: /etc/gpubk/config.json", output.getvalue())

    def test_worker_service_install_preserves_explicit_config_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            units = root / "units"
            result = self.run_bk(
                ["service", "install", "worker", "--target-dir", str(units)],
                data_dir,
                {
                    "BK_GPU_COUNT": "8",
                    "BK_MAX_SHARED_USERS": "4",
                    "BK_WORKER_MAX_PARALLEL": "20",
                    "BK_WORKER_TERMINATION_GRACE_SECONDS": "7.5",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            text = (units / "bk-worker.service").read_text(encoding="utf-8")
            self.assertIn('Environment="BK_GPU_COUNT=8"', text)
            self.assertIn('Environment="BK_MAX_SHARED_USERS=4"', text)
            self.assertIn('Environment="BK_WORKER_MAX_PARALLEL=20"', text)
            self.assertIn(
                'Environment="BK_WORKER_TERMINATION_GRACE_SECONDS=7.5"',
                text,
            )
            self.assertIn(
                "captured config overrides: BK_BOOKING_HORIZON_DAYS, BK_GPU_COUNT, "
                "BK_MAX_SHARED_USERS, "
                "BK_WORKER_MAX_PARALLEL, BK_WORKER_TERMINATION_GRACE_SECONDS",
                result.stdout,
            )

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
            env = {
                "BK_JOB_LOG_DIR": str(log_dir),
                "BK_WORKER_LIVE_GUARD": "0",
            }
            create = self.run_bk(
                [
                    "1",
                    "10m",
                    "--",
                    sys.executable,
                    "-c",
                    "import os; print('CUDA=' + os.environ['CUDA_VISIBLE_DEVICES'])",
                ],
                data_dir,
                env,
            )
            status_before = self.run_bk(["status"], data_dir, env)
            worker = self.run_bk(["w", "--once", "--quiet", "--poll", "0.1"], data_dir, env)
            status_after = self.run_bk(["status"], data_dir, env)
            jobs = self.run_bk(["j"], data_dir, env)
            log = self.run_bk(["jl", "1"], data_dir, env)
            cleanup = self.run_bk(["jobs", "--cleanup", "--json"], data_dir, env)

            self.assertEqual(create.returncode, 0, create.stderr)
            self.assertIn("job: pending", create.stdout)
            self.assertIn("worker: not seen", create.stdout)
            self.assertIn("scheduled command worker is not-seen", create.stderr)
            self.assertEqual(status_before.returncode, 0, status_before.stderr)
            self.assertIn("worker: not seen", status_before.stdout)
            self.assertIn("scheduled command worker is not-seen", status_before.stderr)
            self.assertEqual(worker.returncode, 0, worker.stderr)
            self.assertEqual(status_after.returncode, 0, status_after.stderr)
            self.assertNotIn("worker:", status_after.stdout)
            self.assertNotIn("scheduled command worker", status_after.stderr)
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

    def test_json_scheduled_booking_reports_worker_liveness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            log_dir = root / "job-logs"
            start = iso(ceil_5m(datetime.now(timezone.utc) + timedelta(hours=1)))
            result = self.run_bk(
                [
                    "1",
                    "10m",
                    "--start",
                    start,
                    "--json",
                    "--",
                    sys.executable,
                    "-c",
                    "print('scheduled')",
                ],
                data_dir,
                {"BK_JOB_LOG_DIR": str(log_dir)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["worker"]["state"], "not-seen")
            self.assertTrue(
                any("start `bk w start`" in warning for warning in payload["warnings"])
            )

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
            self.assertIn("committed operation replayed", second.stdout)
            self.assertNotIn("memory telemetry unavailable", second.stdout)
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
            self.assertTrue(
                recommendation_payload["capabilities"]["idempotent_booking"]
            )
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
            cancelled = self.run_bk(
                [
                    "agent",
                    "cancel",
                    short_id,
                    "--op-id",
                    "cli-agent-cancel-1",
                    "--compact",
                ],
                data_dir,
            )
            cancelled_retry = self.run_bk(
                [
                    "agent",
                    "cancel",
                    short_id,
                    "--op-id",
                    "cli-agent-cancel-1",
                    "--compact",
                ],
                data_dir,
            )
            cancellation_status = self.run_bk(
                ["agent", "operation", "cli-agent-cancel-1", "--compact"],
                data_dir,
            )
            retry_after_cancel = self.run_bk(edit_args, data_dir)

            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(edited.returncode, 0, edited.stderr)
            self.assertEqual(retried.returncode, 0, retried.stderr)
            self.assertEqual(missing_operation_id.returncode, 2, missing_operation_id.stderr)
            self.assertEqual(mismatched.returncode, 2, mismatched.stderr)
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            self.assertEqual(cancelled_retry.returncode, 0, cancelled_retry.stderr)
            self.assertEqual(
                cancellation_status.returncode,
                0,
                cancellation_status.stderr,
            )
            self.assertEqual(retry_after_cancel.returncode, 0, retry_after_cancel.stderr)
            edited_payload = json.loads(edited.stdout)
            retried_payload = json.loads(retried.stdout)
            missing_operation_payload = json.loads(missing_operation_id.stdout)
            mismatch_payload = json.loads(mismatched.stdout)
            cancelled_payload = json.loads(cancelled.stdout)
            cancelled_retry_payload = json.loads(cancelled_retry.stdout)
            cancellation_status_payload = json.loads(cancellation_status.stdout)
            retry_after_cancel_payload = json.loads(retry_after_cancel.stdout)
            self.assertEqual(edited_payload["status"], "updated")
            self.assertEqual(retried_payload["status"], "exists")
            self.assertEqual(retried_payload["allocator"]["source"], "idempotent-replay")
            self.assertIn("operation ID is required", missing_operation_payload["error"]["message"])
            self.assertEqual(edited_payload["reservation"]["expected_memory_mb_per_gpu"], 8192)
            self.assertIn("different write", mismatch_payload["error"]["message"])
            self.assertEqual(cancelled_payload["kind"], "cancellation_result")
            self.assertEqual(cancelled_payload["reservation"]["status"], "cancelled")
            self.assertEqual(
                cancelled_retry_payload["reservation"]["id"],
                cancelled_payload["reservation"]["id"],
            )
            self.assertTrue(cancellation_status_payload["found"])
            self.assertEqual(cancellation_status_payload["action"], "cancel")
            self.assertTrue(
                cancellation_status_payload["capabilities"]["operation_status"]
            )
            self.assertEqual(retry_after_cancel_payload["status"], "exists")
            self.assertEqual(
                retry_after_cancel_payload["allocator"]["source"],
                "idempotent-replay",
            )
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

    def test_direct_and_guided_booking_can_exclude_gpus_without_extra_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            direct_dir = Path(tmp) / "direct"
            direct = self.run_bk(
                ["1", "30m", "--exclude", "0,1", "--json"],
                direct_dir,
                {"BK_GPU_COUNT": "3"},
            )

            self.assertEqual(direct.returncode, 0, direct.stderr)
            self.assertEqual(json.loads(direct.stdout)["reservation"]["gpus"], [2])

            guided_dir = Path(tmp) / "guided"
            guided_input = "\n".join(
                [
                    "",             # shared
                    "1",
                    "30m",
                    "now",
                    "except 0,1",   # one concise placement choice
                    "",             # one shared slot
                    "",             # automatic VRAM estimate
                    "",             # no command
                    "",             # confirm
                    "",
                ]
            )
            guided = self.run_bk_with_input(
                ["add"],
                guided_dir,
                guided_input,
                {"BK_GPU_COUNT": "3"},
            )

            self.assertEqual(guided.returncode, 0, guided.stderr)
            self.assertIn("excluded GPUs=0,1", guided.stdout)
            ledger = json.loads((guided_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["reservations"][0]["gpus"], [2])

    def test_cli_reports_administrator_gpu_policy_and_rejects_disabled_fixed_gpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            environment = {
                "BK_GPU_COUNT": "3",
                "BK_DISABLED_GPUS": "1",
                "BK_GPU_PRIORITY": "2=9",
            }

            report = self.run_bk(["config", "--json"], data_dir, environment)
            rejected = self.run_bk(
                ["1", "30m", "--gpu", "1"],
                data_dir,
                environment,
            )

            self.assertEqual(report.returncode, 0, report.stderr)
            effective = json.loads(report.stdout)["effective"]
            self.assertEqual(effective["enabled_gpus"], [0, 2])
            self.assertEqual(effective["disabled_gpus"], [1])
            self.assertEqual(effective["gpu_priority"], {"2": 9})
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("disabled by the administrator", rejected.stderr)

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

    def test_reset_is_disabled_for_shared_data_without_modifying_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "shared"
            environment = {"BK_FILE_MODE": "0660", "BK_DIR_MODE": "2770"}
            create = self.run_bk(["1", "30m"], data_dir, environment)
            self.assertEqual(create.returncode, 0, create.stderr)
            before = {
                str(path.relative_to(data_dir)): path.read_bytes()
                for path in data_dir.rglob("*")
                if path.is_file()
            }

            reset = self.run_bk(["reset", "--yes"], data_dir, environment)

            self.assertEqual(reset.returncode, 2)
            self.assertIn("disabled for shared data directories", reset.stderr)
            after = {
                str(path.relative_to(data_dir)): path.read_bytes()
                for path in data_dir.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_preset_save_list_and_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            environment = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "BK_GPU_COUNT": "4",
                "BK_MAX_SHARED_USERS": "4",
            }

            saved = self.run_bk(
                ["preset", "save", "train", "2", "1h", "12g", "-s", "2"],
                data_dir,
                environment,
            )
            listed = self.run_bk(["p"], data_dir, environment)
            booked = self.run_bk(["p", "train", "--json"], data_dir, environment)

            self.assertEqual(saved.returncode, 0, saved.stderr)
            self.assertIn("saved preset: train", saved.stdout)
            self.assertIn("train", listed.stdout)
            payload = json.loads(booked.stdout)
            self.assertEqual(payload["reservation"]["gpus"], [0, 1])
            self.assertEqual(payload["reservation"]["share_units_per_gpu"], 2)
            self.assertEqual(
                payload["reservation"]["expected_memory_mb_per_gpu"], 12 * 1024
            )

    def test_preset_short_and_long_help_are_not_treated_as_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            short = self.run_bk(["p", "-h"], data_dir)
            long = self.run_bk(["preset", "--help"], data_dir)

            self.assertEqual(short.returncode, 0, short.stderr)
            self.assertEqual(long.returncode, 0, long.stderr)
            self.assertIn("save NAME N DUR", short.stdout)
            self.assertIn("book with a saved preset", long.stdout)

    def test_default_booking_horizon_rejects_far_future_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            far_start = iso(ceil_5m(datetime.now(timezone.utc) + timedelta(days=31)))
            result = self.run_bk(
                ["1", "30m", "--start", far_start],
                data_dir,
                {"BK_BOOKING_HORIZON_DAYS": "30"},
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("30-day booking horizon", result.stderr)

    def test_list_history_keeps_cancelled_reservation_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            created = self.run_bk(["1", "30m"], data_dir)
            reservation_id = created.stdout.split()[1]
            edited = self.run_bk(["e", reservation_id, "-d", "35m"], data_dir)
            cancelled = self.run_bk(["d", reservation_id], data_dir)
            history = self.run_bk(["l", "--history"], data_dir)
            lifecycle = self.run_bk(["history", reservation_id], data_dir)

            self.assertEqual(edited.returncode, 0, edited.stderr)
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            self.assertIn("status=cancelled", history.stdout)
            self.assertIn("cancelled by reservation owner", history.stdout)
            self.assertIn("Edits: 1", lifecycle.stdout)
            self.assertIn("Cancellation: owner", lifecycle.stdout)


if __name__ == "__main__":
    unittest.main()
