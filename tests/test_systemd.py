import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bk.config import Config
from bk.models import BookingError
from bk.systemd import (
    MANAGED_UNIT_MARKER,
    SYSTEM_MANAGED_UNIT_MARKER,
    default_user_unit_dir,
    install_user_unit,
    service_environment,
    system_unit_names,
    system_unit_text,
    uninstall_user_unit,
    unit_text,
)


class BundledSystemdTests(unittest.TestCase):
    def test_system_units_run_as_configured_non_root_owner_and_are_hardened(self):
        common = {
            "service_uid": 1001,
            "service_gid": 1002,
            "config_file": Path("/etc/gpubk/config.json"),
            "data_dir": Path("/var/lib/gpubk"),
            "socket_directory": Path("/run/gpubk"),
            "gpu_count": 8,
            "python_executable": Path("/opt/gpubk/bin/python"),
        }

        broker = system_unit_text("broker", **common)
        monitor = system_unit_text("monitor", **common)

        self.assertEqual(
            system_unit_names(),
            ("gpubk-broker.service", "gpubk-monitor.service"),
        )
        for rendered in (broker, monitor):
            self.assertTrue(rendered.startswith(SYSTEM_MANAGED_UNIT_MARKER))
            self.assertIn("User=1001", rendered)
            self.assertIn("Group=1002", rendered)
            self.assertIn('Environment="BK_CONFIG_FILE=/etc/gpubk/config.json"', rendered)
            self.assertIn("WorkingDirectory=/var/lib/gpubk", rendered)
            self.assertIn("ReadWritePaths=/var/lib/gpubk /run/gpubk", rendered)
            self.assertIn("NoNewPrivileges=true", rendered)
            self.assertIn("ProtectSystem=strict", rendered)
            self.assertIn("WantedBy=multi-user.target", rendered)
            self.assertNotIn("User=root", rendered)
            self.assertNotIn("@SERVICE_UID@", rendered)
        self.assertIn(
            'ExecStartPre="/opt/gpubk/bin/python" -m bk broker --check', broker
        )
        self.assertIn('ExecStart="/opt/gpubk/bin/python" -m bk broker', broker)
        self.assertIn("RuntimeDirectory=gpubk", broker)
        self.assertIn('ExecStart="/opt/gpubk/bin/python" -m bk monitor', monitor)
        self.assertIn("DevicePolicy=closed", monitor)
        self.assertIn("DeviceAllow=/dev/nvidiactl rw", monitor)
        self.assertIn("DeviceAllow=/dev/nvidia0 rw", monitor)
        self.assertIn("DeviceAllow=/dev/nvidia7 rw", monitor)
        self.assertNotIn("DeviceAllow=/dev/nvidia8 rw", monitor)
        self.assertNotIn("DeviceAllow=/dev/nvidia", broker)
        self.assertNotIn("RuntimeDirectory=", monitor)

    def test_system_broker_creates_nested_run_directory_but_not_persistent_path(self):
        common = {
            "service_uid": 1001,
            "service_gid": 1001,
            "config_file": Path("/etc/gpubk/config.json"),
            "data_dir": Path("/srv/gpubk"),
            "gpu_count": 8,
            "python_executable": Path("/opt/gpubk/bin/python"),
        }

        volatile = system_unit_text(
            "broker", socket_directory=Path("/run/lab/gpubk"), **common
        )
        persistent = system_unit_text(
            "broker", socket_directory=Path("/srv/gpubk/run"), **common
        )

        self.assertIn("RuntimeDirectory=lab/gpubk", volatile)
        self.assertNotIn("RuntimeDirectory=", persistent)

    def test_system_unit_escapes_paths_without_quoting_path_directives(self):
        rendered = system_unit_text(
            "monitor",
            service_uid=1001,
            service_gid=1001,
            config_file=Path("/etc/gpubk/config.json"),
            data_dir=Path('/srv/GPU lab/percent%/quote"'),
            socket_directory=Path("/run/GPU lab"),
            gpu_count=8,
            python_executable=Path("/opt/gpubk/bin/python"),
        )

        self.assertIn(
            "WorkingDirectory=/srv/GPU\\x20lab/percent%%/quote\\x22", rendered
        )
        self.assertIn(
            "ReadWritePaths=/srv/GPU\\x20lab/percent%%/quote\\x22 /run/GPU\\x20lab",
            rendered,
        )

    def test_system_unit_rejects_root_identity_and_relative_paths(self):
        common = {
            "service_uid": 1001,
            "service_gid": 1001,
            "config_file": Path("/etc/gpubk/config.json"),
            "data_dir": Path("/var/lib/gpubk"),
            "socket_directory": Path("/run/gpubk"),
            "gpu_count": 8,
            "python_executable": Path("/opt/gpubk/bin/python"),
        }
        with self.assertRaisesRegex(BookingError, "positive integer"):
            system_unit_text("broker", **{**common, "service_uid": 0})
        with self.assertRaisesRegex(BookingError, "GPU count"):
            system_unit_text("monitor", **{**common, "gpu_count": 0})
        with self.assertRaisesRegex(BookingError, "must be absolute"):
            system_unit_text(
                "broker", **{**common, "python_executable": Path("python3")}
            )

    def test_units_are_bundled_and_remain_user_scoped(self):
        python = Path("/opt/bk venv/bin/python")
        environment = {"BK_DATA_DIR": "/data2/shared/bk", "PYTHONUNBUFFERED": "1"}
        worker = unit_text("worker", python, environment=environment)
        monitor = unit_text("monitor", python, environment=environment)

        self.assertTrue(worker.startswith(MANAGED_UNIT_MARKER))
        self.assertIn('ExecStart="/opt/bk venv/bin/python" -m bk worker', worker)
        self.assertIn('ExecStart="/opt/bk venv/bin/python" -m bk monitor', monitor)
        self.assertNotIn("--interval", monitor)
        self.assertNotIn("--rollup", monitor)
        self.assertIn('Environment="BK_DATA_DIR=/data2/shared/bk"', worker)
        self.assertIn("RestartPreventExitStatus=75 77 78", monitor)
        self.assertIn("StartLimitIntervalSec=60", monitor)
        self.assertIn("StartLimitBurst=3", monitor)
        self.assertIn("RestartPreventExitStatus=75 78", worker)
        self.assertIn("TimeoutStopSec=75", worker)
        self.assertIn("StartLimitIntervalSec=60", worker)
        self.assertIn("StartLimitBurst=3", worker)
        self.assertNotIn("EnvironmentFile=", worker)
        self.assertNotIn("@PYTHON_EXECUTABLE@", worker)
        self.assertNotIn("@SERVICE_ENVIRONMENT@", worker)
        self.assertNotIn("User=root", worker)

    def test_unit_escapes_systemd_specifiers_and_environment_markers(self):
        worker = unit_text(
            "worker",
            Path("/opt/percent%/$name/python"),
            environment={"BK_DATA_DIR": '/data/percent%/$name/quote"/back\\slash'},
        )

        self.assertIn('ExecStart="/opt/percent%%/$$name/python" -m bk worker', worker)
        self.assertIn(
            'Environment="BK_DATA_DIR=/data/percent%%/$name/quote\\"/back\\\\slash"',
            worker,
        )

    def test_unit_rejects_relative_interpreter_path(self):
        with self.assertRaisesRegex(BookingError, "absolute path"):
            unit_text("worker", Path("python3"))

    def test_install_never_enables_service_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            environment = {"BK_DATA_DIR": "/data2/shared/bk"}

            path = install_user_unit("worker", target, environment=environment)

            self.assertEqual(path, target / "bk-worker.service")
            self.assertTrue(path.is_file())
            with self.assertRaisesRegex(BookingError, "already exists"):
                install_user_unit("worker", target, environment=environment)
            install_user_unit("worker", target, environment=environment, force=True)

    def test_uninstall_removes_only_a_managed_regular_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            installed = install_user_unit(
                "monitor",
                target,
                environment={"BK_DATA_DIR": "/data2/shared/bk"},
            )

            removed = uninstall_user_unit("monitor", target)

            self.assertEqual(removed, installed)
            self.assertFalse(installed.exists())

            unrecognized = target / "bk-monitor.service"
            unrecognized.write_text("[Unit]\nDescription=other\n", encoding="utf-8")
            unrecognized.chmod(0o644)
            with self.assertRaisesRegex(BookingError, "unrecognized"):
                uninstall_user_unit("monitor", target)
            self.assertTrue(unrecognized.exists())

    def test_uninstall_refuses_a_unit_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            real = target / "real.service"
            real.write_text(MANAGED_UNIT_MARKER, encoding="utf-8")
            real.chmod(0o644)
            destination = target / "bk-monitor.service"
            destination.symlink_to(real)

            with self.assertRaises(OSError):
                uninstall_user_unit("monitor", target)

            self.assertTrue(destination.is_symlink())
            self.assertTrue(real.exists())

    def test_default_install_ignores_relative_xdg_config_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with mock.patch.dict(
                "os.environ",
                {"HOME": str(home), "XDG_CONFIG_HOME": "relative-config"},
                clear=True,
            ):
                expected = home / ".config" / "systemd" / "user"
                self.assertEqual(default_user_unit_dir(), expected)
                installed = install_user_unit(
                    "worker",
                    environment={"BK_DATA_DIR": "/data2/shared/bk"},
                )

            self.assertEqual(installed, expected / "bk-worker.service")
            self.assertTrue(installed.is_file())

    def test_default_install_uses_absolute_xdg_config_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_home = Path(tmp) / "xdg-config"
            with mock.patch.dict(
                "os.environ",
                {"HOME": "/home/ignored", "XDG_CONFIG_HOME": str(config_home)},
                clear=True,
            ):
                self.assertEqual(
                    default_user_unit_dir(),
                    config_home / "systemd" / "user",
                )

    def test_install_refuses_dangling_unit_symlink_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            destination = target / "bk-worker.service"
            destination.symlink_to(target / "missing-unit")

            with self.assertRaisesRegex(BookingError, "already exists"):
                install_user_unit(
                    "worker",
                    target,
                    environment={"BK_DATA_DIR": "/data2/shared/bk"},
                )

            self.assertTrue(destination.is_symlink())

    def test_install_surfaces_directory_fsync_failure_without_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            with mock.patch(
                "bk.systemd.fsync_directory",
                side_effect=OSError("unit directory sync failed"),
            ):
                with self.assertRaisesRegex(OSError, "unit directory sync failed"):
                    install_user_unit(
                        "worker",
                        target,
                        environment={"BK_DATA_DIR": "/data2/shared/bk"},
                    )

            destination = target / "bk-worker.service"
            self.assertTrue(destination.is_file())
            self.assertIn("-m bk worker", destination.read_text(encoding="utf-8"))
            self.assertEqual(list(target.glob(".bk-worker.service.*.tmp")), [])

    def test_service_environment_captures_absolute_runtime_paths(self):
        config = Config(
            data_dir=Path("relative-data"),
            job_log_dir=Path("relative-logs"),
            config_file=Path("relative-config/config.json"),
        )

        worker = service_environment(config, "worker", process_environment={})
        monitor = service_environment(config, "monitor", process_environment={})

        self.assertEqual(worker["BK_DATA_DIR"], str(Path("relative-data").absolute()))
        self.assertEqual(
            worker["BK_CONFIG_FILE"],
            str(Path("relative-config/config.json").absolute()),
        )
        self.assertEqual(worker["BK_JOB_LOG_DIR"], str(Path("relative-logs").absolute()))
        self.assertEqual(monitor["BK_DATA_DIR"], str(Path("relative-data").absolute()))
        self.assertEqual(
            monitor["BK_CONFIG_FILE"],
            str(Path("relative-config/config.json").absolute()),
        )
        self.assertNotIn("BK_JOB_LOG_DIR", monitor)

        defaults = service_environment(
            Config(data_dir=Path("relative-data")),
            "monitor",
            process_environment={},
        )
        self.assertNotIn("BK_CONFIG_FILE", defaults)

    def test_service_environment_captures_only_explicit_nonsecret_config_overrides(self):
        config = Config(
            data_dir=Path("relative-data"),
            gpu_count=8,
            max_shared_users=4,
            worker_max_parallel=20,
            worker_poll_seconds=1.0,
            worker_live_guard=False,
            worker_termination_grace_seconds=7.5,
            file_mode=0o660,
        )
        environment = service_environment(
            config,
            "worker",
            process_environment={
                "BK_GPU_COUNT": "08",
                "BK_MAX_SHARED_USERS": "4",
                "BK_WORKER_MAX_PARALLEL": "20",
                "BK_WORKER_POLL_SECONDS": "1.000",
                "BK_WORKER_LIVE_GUARD": "no",
                "BK_WORKER_TERMINATION_GRACE_SECONDS": "7.500",
                "BK_FILE_MODE": "0o660",
                "BK_ALLOCATOR_COMMAND": "allocator --token secret-value",
                "BK_MONITOR_UID": "9999",
            },
        )

        self.assertEqual(environment["BK_GPU_COUNT"], "8")
        self.assertEqual(environment["BK_MAX_SHARED_USERS"], "4")
        self.assertEqual(environment["BK_WORKER_MAX_PARALLEL"], "20")
        self.assertEqual(environment["BK_WORKER_POLL_SECONDS"], "1")
        self.assertEqual(environment["BK_WORKER_LIVE_GUARD"], "false")
        self.assertEqual(environment["BK_WORKER_TERMINATION_GRACE_SECONDS"], "7.5")
        self.assertEqual(environment["BK_FILE_MODE"], "0660")
        self.assertNotIn("BK_ALLOCATOR_COMMAND", environment)
        self.assertNotIn("BK_MONITOR_UID", environment)
        self.assertNotIn("secret-value", unit_text("worker", environment=environment))

    def test_unit_rejects_control_characters_in_environment(self):
        with self.assertRaisesRegex(BookingError, "control character"):
            unit_text("worker", Path("/usr/bin/python3"), environment={"BK_DATA_DIR": "/tmp/bad\npath"})


if __name__ == "__main__":
    unittest.main()
