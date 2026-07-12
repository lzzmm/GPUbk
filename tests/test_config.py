import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bk.config import load_config


class ConfigTests(unittest.TestCase):
    def test_generic_default_uses_xdg_data_home_not_lab_specific_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                "os.environ",
                {"XDG_DATA_HOME": tmp},
                clear=True,
            ):
                config = load_config()

            self.assertEqual(config.data_dir, Path(tmp) / "bk")
            self.assertEqual(config.file_mode, 0o600)
            self.assertEqual(config.dir_mode, 0o700)
            self.assertTrue(config.worker_live_guard)
            self.assertEqual(config.job_log_retention_days, 30)
            self.assertEqual(config.job_log_max_mb, 64)
            self.assertEqual(config.job_log_total_max_mb, 4096)

    def test_gpu_count_is_auto_detected_when_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"BK_DATA_DIR": tmp}, clear=True):
                with mock.patch("bk.config._auto_gpu_count", return_value=8) as detect:
                    config = load_config()

            self.assertEqual(config.gpu_count, 8)
            detect.assert_called_once_with()

    def test_explicit_gpu_count_skips_auto_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                "os.environ",
                {"BK_DATA_DIR": tmp, "BK_GPU_COUNT": "3"},
                clear=True,
            ):
                with mock.patch("bk.config._auto_gpu_count") as detect:
                    config = load_config()

            self.assertEqual(config.gpu_count, 3)
            detect.assert_not_called()

    def test_shared_modes_and_memory_policy_parse_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            config_path = data_dir / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "file_mode": "0660",
                        "dir_mode": "2770",
                        "require_shared_memory": True,
                        "ledger_retention_days": 45,
                        "usage_minute_retention_days": 30,
                        "usage_five_minute_retention_days": 365,
                        "usage_ten_minute_retention_days": 1095,
                        "usage_hourly_retention_days": 1500,
                        "usage_daily_retention_days": 0,
                        "usage_event_retention_days": 365,
                        "worker_live_guard": False,
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            with mock.patch.dict("os.environ", {"BK_DATA_DIR": str(data_dir)}, clear=True):
                config = load_config()

            self.assertEqual(config.file_mode, 0o660)
            self.assertEqual(config.dir_mode, 0o2770)
            self.assertTrue(config.require_shared_memory)
            self.assertEqual(config.ledger_retention_days, 45)
            self.assertEqual(config.usage_minute_retention_days, 30)
            self.assertEqual(config.usage_five_minute_retention_days, 365)
            self.assertEqual(config.usage_ten_minute_retention_days, 1095)
            self.assertEqual(config.usage_hourly_retention_days, 1500)
            self.assertEqual(config.usage_daily_retention_days, 0)
            self.assertEqual(config.usage_event_retention_days, 365)
            self.assertFalse(config.worker_live_guard)

    def test_config_file_must_not_be_group_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            path = data_dir / "config.json"
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o664)

            with mock.patch.dict("os.environ", {"BK_DATA_DIR": str(data_dir)}, clear=True):
                with self.assertRaisesRegex(PermissionError, "must not be writable"):
                    load_config()

    def test_config_file_symbolic_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            target = root / "config-target.json"
            target.write_text("{}", encoding="utf-8")
            (data_dir / "config.json").symlink_to(target)

            with mock.patch.dict("os.environ", {"BK_DATA_DIR": str(data_dir)}, clear=True):
                with self.assertRaises(OSError):
                    load_config()

    def test_zero_retention_disables_hot_ledger_pruning(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                "os.environ",
                {"BK_DATA_DIR": tmp, "BK_LEDGER_RETENTION_DAYS": "0"},
                clear=True,
            ):
                config = load_config()

            self.assertEqual(config.ledger_retention_days, 0)

    def test_zero_daily_retention_keeps_daily_usage_forever(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                "os.environ",
                {"BK_DATA_DIR": tmp, "BK_USAGE_DAILY_RETENTION_DAYS": "0"},
                clear=True,
            ):
                config = load_config()

            self.assertEqual(config.usage_daily_retention_days, 0)

    def test_job_log_limits_can_be_overridden_or_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                "os.environ",
                {
                    "BK_DATA_DIR": tmp,
                    "BK_JOB_LOG_RETENTION_DAYS": "0",
                    "BK_JOB_LOG_MAX_MB": "8",
                    "BK_JOB_LOG_TOTAL_MAX_MB": "512",
                },
                clear=True,
            ):
                config = load_config()

            self.assertEqual(config.job_log_retention_days, 0)
            self.assertEqual(config.job_log_max_mb, 8)
            self.assertEqual(config.job_log_total_max_mb, 512)

    def test_file_mode_rejects_executable_bits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                "os.environ",
                {"BK_DATA_DIR": tmp, "BK_FILE_MODE": "0770"},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "executable bits"):
                    load_config()

    def test_allocator_command_is_opt_in_and_shell_split_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                "os.environ",
                {
                    "BK_DATA_DIR": tmp,
                    "BK_ALLOCATOR_COMMAND": f"{sys.executable} -m example_allocator",
                    "BK_ALLOCATOR_WEIGHT": "7.5",
                },
                clear=True,
            ):
                config = load_config()

            self.assertEqual(config.allocator_command, (sys.executable, "-m", "example_allocator"))
            self.assertEqual(config.allocator_weight, 7.5)


if __name__ == "__main__":
    unittest.main()
