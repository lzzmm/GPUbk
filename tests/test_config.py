import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bk.config import Config, load_config


class ConfigTests(unittest.TestCase):
    def test_slot_field_preserves_existing_positional_constructor_order(self):
        config = Config(Path("/tmp/bk-config-order"), 8, 4)

        self.assertEqual(config.gpu_count, 8)
        self.assertEqual(config.max_shared_users, 4)
        self.assertEqual(config.slot_minutes, 5)

    def test_generic_default_uses_xdg_data_home_not_lab_specific_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                "os.environ",
                {"XDG_DATA_HOME": tmp},
                clear=True,
            ), mock.patch(
                "bk.config.SYSTEM_CONFIG_FILE",
                Path(tmp) / "missing-system-config.json",
            ):
                config = load_config()

            self.assertEqual(config.data_dir, Path(tmp) / "bk")
            self.assertEqual(config.file_mode, 0o600)
            self.assertEqual(config.dir_mode, 0o700)
            self.assertEqual(config.slot_minutes, 5)
            self.assertEqual(config.slot_seconds, 300)
            self.assertIsNone(config.config_file)
            self.assertEqual(config.config_path, Path(tmp) / "bk" / "config.json")
            self.assertEqual(config.timeline_hours, 2)
            self.assertTrue(config.worker_live_guard)
            self.assertEqual(config.worker_max_parallel, 64)
            self.assertEqual(config.effective_worker_max_parallel, 2)
            self.assertEqual(config.worker_termination_grace_seconds, 5.0)
            self.assertEqual(config.job_log_retention_days, 30)
            self.assertEqual(config.job_log_max_mb, 64)
            self.assertEqual(config.job_log_total_max_mb, 4096)
            self.assertEqual(config.worker_recovery_grace_seconds, 5.0)
            self.assertEqual(config.monitor_interval_seconds, 2.0)
            self.assertEqual(config.monitor_rollup_seconds, 60)
            self.assertEqual(config.tui_refresh_seconds, 1.0)
            self.assertIsNone(config.monitor_uid)
            self.assertIsNone(config.storage_gid)
            self.assertIsNone(config.config_owner_uid)

    def test_trusted_system_config_supplies_shared_data_dir_without_shell_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "etc" / "gpubk"
            config_dir.mkdir(parents=True, mode=0o700)
            config_path = config_dir / "config.json"
            data_dir = root / "shared-data"
            config_path.write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "data_dir": str(data_dir),
                        "gpu_count": 8,
                        "slot_minutes": 10,
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)

            with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
                "bk.config.SYSTEM_CONFIG_FILE",
                config_path,
            ):
                config = load_config()

            self.assertEqual(config.data_dir, data_dir)
            self.assertEqual(config.config_file, config_path.resolve())
            self.assertEqual(config.config_owner_uid, os.getuid())
            self.assertEqual(config.gpu_count, 8)
            self.assertEqual(config.slot_minutes, 10)

    def test_explicit_data_dir_bypasses_system_config_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "private-data"
            system_config = root / "broken-system-config.json"
            system_config.write_text("not json", encoding="utf-8")
            system_config.chmod(0o600)

            with mock.patch.dict(
                "os.environ",
                {"BK_DATA_DIR": str(data_dir), "BK_GPU_COUNT": "2"},
                clear=True,
            ), mock.patch("bk.config.SYSTEM_CONFIG_FILE", system_config):
                config = load_config()

            self.assertEqual(config.data_dir, data_dir)
            self.assertIsNone(config.config_file)
            self.assertEqual(config.gpu_count, 2)

    def test_system_config_requires_an_absolute_data_dir(self):
        cases = (
            ({"config_version": 1, "gpu_count": 2}, "must define data_dir"),
            (
                {"config_version": 1, "data_dir": "relative/data", "gpu_count": 2},
                "data_dir must be an absolute filesystem path",
            ),
        )
        for document, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "config.json"
                config_path.write_text(json.dumps(document), encoding="utf-8")
                config_path.chmod(0o600)
                with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
                    "bk.config.SYSTEM_CONFIG_FILE",
                    config_path,
                ):
                    with self.assertRaisesRegex(ValueError, message):
                        load_config()

    def test_external_config_data_dir_is_overridden_only_by_explicit_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            configured_data = root / "configured-data"
            environment_data = root / "environment-data"
            config_path.write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "data_dir": str(configured_data),
                        "gpu_count": 2,
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)

            with mock.patch.dict(
                "os.environ",
                {"BK_CONFIG_FILE": str(config_path)},
                clear=True,
            ):
                configured = load_config()
            config_path.write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "data_dir": "superseded/relative/path",
                        "gpu_count": 2,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {
                    "BK_CONFIG_FILE": str(config_path),
                    "BK_DATA_DIR": str(environment_data),
                },
                clear=True,
            ):
                overridden = load_config()

            self.assertEqual(configured.data_dir, configured_data)
            self.assertEqual(overridden.data_dir, environment_data)

    def test_external_config_without_data_dir_requires_an_environment_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            data_dir = root / "environment-data"
            config_path.write_text(
                json.dumps({"config_version": 1, "gpu_count": 2}),
                encoding="utf-8",
            )
            config_path.chmod(0o600)

            with mock.patch.dict(
                "os.environ",
                {"BK_CONFIG_FILE": str(config_path)},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "must define data_dir"):
                    load_config()
            with mock.patch.dict(
                "os.environ",
                {
                    "BK_CONFIG_FILE": str(config_path),
                    "BK_DATA_DIR": str(data_dir),
                },
                clear=True,
            ):
                configured = load_config()

            self.assertEqual(configured.data_dir, data_dir)

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
                        "gpu_count": 8,
                        "file_mode": "0660",
                        "dir_mode": "2770",
                        "slot_minutes": 10,
                        "require_shared_memory": True,
                        "ledger_retention_days": 45,
                        "usage_minute_retention_days": 30,
                        "usage_five_minute_retention_days": 365,
                        "usage_ten_minute_retention_days": 1095,
                        "usage_hourly_retention_days": 1500,
                        "usage_daily_retention_days": 0,
                        "usage_event_retention_days": 365,
                        "worker_max_parallel": 12,
                        "worker_termination_grace_seconds": 7.5,
                        "worker_live_guard": False,
                        "monitor_interval_seconds": 5,
                        "monitor_rollup_seconds": 300,
                        "tui_refresh_seconds": 2.5,
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            with mock.patch.dict("os.environ", {"BK_DATA_DIR": str(data_dir)}, clear=True):
                config = load_config()

            self.assertEqual(config.file_mode, 0o660)
            self.assertEqual(config.dir_mode, 0o2770)
            self.assertEqual(config.slot_minutes, 10)
            self.assertEqual(config.slot_seconds, 600)
            self.assertTrue(config.require_shared_memory)
            self.assertEqual(config.ledger_retention_days, 45)
            self.assertEqual(config.usage_minute_retention_days, 30)
            self.assertEqual(config.usage_five_minute_retention_days, 365)
            self.assertEqual(config.usage_ten_minute_retention_days, 1095)
            self.assertEqual(config.usage_hourly_retention_days, 1500)
            self.assertEqual(config.usage_daily_retention_days, 0)
            self.assertEqual(config.usage_event_retention_days, 365)
            self.assertEqual(config.worker_max_parallel, 12)
            self.assertEqual(config.effective_worker_max_parallel, 12)
            self.assertEqual(config.worker_termination_grace_seconds, 7.5)
            self.assertFalse(config.worker_live_guard)
            self.assertEqual(config.monitor_interval_seconds, 5.0)
            self.assertEqual(config.monitor_rollup_seconds, 300)
            self.assertEqual(config.tui_refresh_seconds, 2.5)

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

    def test_data_local_config_cannot_redirect_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "selected-data"
            data_dir.mkdir()
            config_path = data_dir / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "data_dir": str(root / "redirected-data"),
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)

            with mock.patch.dict(
                "os.environ",
                {"BK_DATA_DIR": str(data_dir)},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "data_dir is only allowed"):
                    load_config()

    def test_external_config_file_is_separate_from_shared_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "shared"
            data_dir.mkdir(mode=0o770)
            data_dir.chmod(0o2770)
            config_dir = root / "trusted"
            config_dir.mkdir(mode=0o700)
            config_path = config_dir / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "gpu_count": 3,
                        "slot_minutes": 10,
                        "monitor_uid": 1234,
                        "storage_gid": data_dir.stat().st_gid,
                        "file_mode": "0660",
                        "dir_mode": "2770",
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)

            with mock.patch.dict(
                "os.environ",
                {
                    "BK_DATA_DIR": str(data_dir),
                    "BK_CONFIG_FILE": str(config_path),
                    "BK_MONITOR_UID": "9999",
                    "BK_STORAGE_GID": "9999",
                },
                clear=True,
            ):
                config = load_config()

            self.assertEqual(config.data_dir, data_dir)
            self.assertEqual(config.config_file, config_path.resolve())
            self.assertEqual(config.config_path, config_path.resolve())
            self.assertEqual(config.gpu_count, 3)
            self.assertEqual(config.slot_minutes, 10)
            self.assertEqual(config.monitor_uid, 1234)
            self.assertEqual(config.storage_gid, data_dir.stat().st_gid)
            self.assertEqual(config.config_owner_uid, os.getuid())

    def test_explicit_config_file_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {
                    "BK_DATA_DIR": str(root / "data"),
                    "BK_CONFIG_FILE": str(root / "missing.json"),
                },
                clear=True,
            ):
                with self.assertRaisesRegex(FileNotFoundError, "BK_CONFIG_FILE does not exist"):
                    load_config()

    def test_config_directory_must_not_be_group_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "shared"
            data_dir.mkdir()
            data_dir.chmod(0o2770)
            path = data_dir / "config.json"
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o600)

            with mock.patch.dict("os.environ", {"BK_DATA_DIR": str(data_dir)}, clear=True):
                with self.assertRaisesRegex(PermissionError, "outside the shared data directory"):
                    load_config()

    def test_external_config_directory_is_canonicalized_but_leaf_links_stay_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "trusted"
            target.mkdir(mode=0o700)
            config_path = target / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            config_path.chmod(0o600)
            linked_parent = root / "linked"
            linked_parent.symlink_to(target, target_is_directory=True)

            with mock.patch.dict(
                "os.environ",
                {
                    "BK_DATA_DIR": str(root / "data"),
                    "BK_CONFIG_FILE": str(linked_parent / "config.json"),
                },
                clear=True,
            ):
                config = load_config()

            self.assertEqual(config.config_file, config_path.resolve())

            config_path.unlink()
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            config_path.symlink_to(outside)
            with mock.patch.dict(
                "os.environ",
                {
                    "BK_DATA_DIR": str(root / "data"),
                    "BK_CONFIG_FILE": str(linked_parent / "config.json"),
                },
                clear=True,
            ):
                with self.assertRaises(OSError):
                    load_config()

    def test_secure_leaf_under_group_writable_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared"
            shared.mkdir()
            shared.chmod(0o2770)
            trusted = shared / "trusted"
            trusted.mkdir(mode=0o700)
            config_path = trusted / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            config_path.chmod(0o600)

            with mock.patch.dict(
                "os.environ",
                {
                    "BK_DATA_DIR": str(root / "data"),
                    "BK_CONFIG_FILE": str(config_path),
                },
                clear=True,
            ):
                with self.assertRaisesRegex(PermissionError, "outside the shared data directory"):
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

    def test_slot_minutes_can_be_overridden_by_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                "os.environ",
                {
                    "BK_DATA_DIR": tmp,
                    "BK_GPU_COUNT": "1",
                    "BK_SLOT_MINUTES": "15",
                },
                clear=True,
            ):
                config = load_config()

            self.assertEqual(config.slot_minutes, 15)
            self.assertEqual(config.slot_seconds, 900)

    def test_slot_minutes_must_divide_one_hour(self):
        for value in (0, 7, 61, "bad", True):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                with mock.patch.dict(
                    "os.environ",
                    {
                        "BK_DATA_DIR": tmp,
                        "BK_GPU_COUNT": "1",
                        "BK_SLOT_MINUTES": str(value),
                    },
                    clear=True,
                ):
                    with self.assertRaisesRegex(ValueError, "slot_minutes"):
                        load_config()

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
                    "BK_WORKER_MAX_PARALLEL": "3",
                    "BK_WORKER_TERMINATION_GRACE_SECONDS": "2.5",
                    "BK_WORKER_RECOVERY_GRACE_SECONDS": "0.25",
                },
                clear=True,
            ):
                config = load_config()

            self.assertEqual(config.job_log_retention_days, 0)
            self.assertEqual(config.job_log_max_mb, 8)
            self.assertEqual(config.job_log_total_max_mb, 512)
            self.assertEqual(config.worker_max_parallel, 3)
            self.assertEqual(config.effective_worker_max_parallel, 2)
            self.assertEqual(config.worker_termination_grace_seconds, 2.5)
            self.assertEqual(config.worker_recovery_grace_seconds, 0.25)

    def test_worker_parallel_limit_is_bounded_by_config_and_shared_topology(self):
        topology_limited = Config(
            Path("/tmp/bk-worker-topology-limit"),
            gpu_count=2,
            max_shared_users=4,
            worker_max_parallel=64,
        )
        configured_limited = Config(
            Path("/tmp/bk-worker-config-limit"),
            gpu_count=8,
            max_shared_users=4,
            worker_max_parallel=3,
        )

        self.assertEqual(topology_limited.effective_worker_max_parallel, 8)
        self.assertEqual(configured_limited.effective_worker_max_parallel, 3)

        for invalid in (False, 0, 4097):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError,
                "worker_max_parallel",
            ):
                Config(Path("/tmp/bk-worker-invalid-limit"), worker_max_parallel=invalid)

        for invalid in (False, 0.01, 61, float("nan")):
            with self.subTest(invalid_grace=invalid), self.assertRaisesRegex(
                ValueError,
                "worker_termination_grace_seconds",
            ):
                Config(
                    Path("/tmp/bk-worker-invalid-grace"),
                    worker_termination_grace_seconds=invalid,
                )

    def test_monitor_cadence_can_be_overridden_by_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                "os.environ",
                {
                    "BK_DATA_DIR": tmp,
                    "BK_MONITOR_INTERVAL_SECONDS": "2.5",
                    "BK_MONITOR_ROLLUP_SECONDS": "30",
                    "BK_TUI_REFRESH_SECONDS": "1.5",
                },
                clear=True,
            ):
                config = load_config()

            self.assertEqual(config.monitor_interval_seconds, 2.5)
            self.assertEqual(config.monitor_rollup_seconds, 30)
            self.assertEqual(config.tui_refresh_seconds, 1.5)

    def test_monitor_cadence_rejects_unsafe_or_inexact_windows(self):
        cases = (
            ("0.1", "60", ">= 0.2"),
            ("2", "1", ">= monitor_interval_seconds"),
            ("7", "60", "integer multiple"),
            ("2", "0", ">= 1"),
        )
        for interval, rollup, message in cases:
            with (
                self.subTest(interval=interval, rollup=rollup),
                tempfile.TemporaryDirectory() as tmp,
            ):
                with mock.patch.dict(
                    "os.environ",
                    {
                        "BK_DATA_DIR": tmp,
                        "BK_MONITOR_INTERVAL_SECONDS": interval,
                        "BK_MONITOR_ROLLUP_SECONDS": rollup,
                    },
                    clear=True,
                ):
                    with self.assertRaisesRegex(ValueError, message):
                        load_config()

    def test_tui_refresh_rejects_unbounded_or_nonfinite_values(self):
        cases = (("0.01", ">= 0.1"), ("61", "<= 60"), ("nan", "finite"))
        for value, message in cases:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                with mock.patch.dict(
                    "os.environ",
                    {"BK_DATA_DIR": tmp, "BK_TUI_REFRESH_SECONDS": value},
                    clear=True,
                ):
                    with self.assertRaisesRegex(ValueError, message):
                        load_config()

    def test_monitor_uid_rejects_boolean_negative_and_oversized_values(self):
        for value in (True, -1, 2**32 - 1):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                path.write_text(json.dumps({"monitor_uid": value}), encoding="utf-8")
                path.chmod(0o600)
                with mock.patch.dict("os.environ", {"BK_DATA_DIR": tmp}, clear=True):
                    with self.assertRaisesRegex(ValueError, "monitor_uid"):
                        load_config()

    def test_storage_gid_is_file_only_and_requires_setgid_directory_mode(self):
        for value in (True, -1, 2**32 - 1):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                path.write_text(json.dumps({"storage_gid": value}), encoding="utf-8")
                path.chmod(0o600)
                with mock.patch.dict("os.environ", {"BK_DATA_DIR": tmp}, clear=True):
                    with self.assertRaisesRegex(ValueError, "storage_gid"):
                        load_config()

        with self.assertRaisesRegex(ValueError, "setgid dir_mode"):
            Config(Path("/tmp/gpubk-storage-gid"), storage_gid=os.getgid())

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

    def test_unknown_config_key_is_rejected_with_typo_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"max_share_users": 4}), encoding="utf-8")
            path.chmod(0o600)

            with mock.patch.dict("os.environ", {"BK_DATA_DIR": tmp}, clear=True):
                with self.assertRaisesRegex(
                    ValueError,
                    "unknown config key 'max_share_users'.*max_shared_users",
                ):
                    load_config()

    def test_config_version_is_optional_but_newer_versions_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"config_version": 1, "gpu_count": 3}), encoding="utf-8")
            path.chmod(0o600)
            with mock.patch.dict("os.environ", {"BK_DATA_DIR": tmp}, clear=True):
                self.assertEqual(load_config().gpu_count, 3)

            path.write_text(json.dumps({"config_version": 2}), encoding="utf-8")
            with mock.patch.dict("os.environ", {"BK_DATA_DIR": tmp}, clear=True):
                with self.assertRaisesRegex(ValueError, "unsupported config_version 2"):
                    load_config()

    def test_integer_fields_reject_booleans_and_fractional_numbers(self):
        for key, value in (("gpu_count", True), ("slot_minutes", 5.0)):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                path.write_text(json.dumps({key: value}), encoding="utf-8")
                path.chmod(0o600)
                with mock.patch.dict("os.environ", {"BK_DATA_DIR": tmp}, clear=True):
                    with self.assertRaisesRegex(ValueError, "integer"):
                        load_config()

    def test_float_fields_reject_nonfinite_and_excessive_values(self):
        cases = (
            ({"BK_LOCK_TIMEOUT_SECONDS": "nan"}, "finite"),
            ({"BK_ALLOCATOR_TIMEOUT_SECONDS": "inf"}, "finite"),
            ({"BK_GPU_COUNT": "1025"}, "<= 1024"),
            ({"BK_QUEUE_SEARCH_HOURS": str(10 * 365 * 24 + 1)}, "<= 87600"),
            ({"BK_WORKER_MAX_PARALLEL": "0"}, ">= 1"),
            ({"BK_WORKER_MAX_PARALLEL": "4097"}, "<= 4096"),
            ({"BK_WORKER_TERMINATION_GRACE_SECONDS": "0.01"}, ">= 0.1"),
            ({"BK_WORKER_TERMINATION_GRACE_SECONDS": "61"}, "<= 60"),
        )
        for environment, message in cases:
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as tmp:
                values = {"BK_DATA_DIR": tmp, **environment}
                with mock.patch.dict("os.environ", values, clear=True):
                    with self.assertRaisesRegex(ValueError, message):
                        load_config()

    def test_paths_and_allocator_arguments_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"job_log_dir": "bad\x00path"}), encoding="utf-8")
            path.chmod(0o600)
            with mock.patch.dict("os.environ", {"BK_DATA_DIR": tmp}, clear=True):
                with self.assertRaisesRegex(ValueError, "filesystem path|non-empty path"):
                    load_config()

            path.write_text(json.dumps({"job_log_dir": False}), encoding="utf-8")
            with mock.patch.dict("os.environ", {"BK_DATA_DIR": tmp}, clear=True):
                with self.assertRaisesRegex(ValueError, "filesystem path"):
                    load_config()

            path.unlink()

            with mock.patch.dict(
                "os.environ",
                {"BK_DATA_DIR": tmp, "BK_ALLOCATOR_COMMAND": "x" * 4097},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "4096 bytes"):
                    load_config()


if __name__ == "__main__":
    unittest.main()
