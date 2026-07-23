import json
import grp
import os
import pwd
import socket
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

import bk.admin as admin_module
from bk.admin import (
    AdminIdentity,
    AdminInitPlan,
    INSTALL_MANIFEST_MODE,
    _detected_gpu_count,
    _python_uninstall_hint,
    _validate_plan,
    apply_admin_command_link_install,
    apply_admin_gpu_policy,
    apply_admin_init,
    apply_admin_system_services_install,
    apply_admin_system_services_uninstall,
    apply_admin_uninstall,
    inspect_admin_init,
    inspect_admin_gpu_policy,
    inspect_admin_gpu_policy_recovery,
    inspect_admin_system_services,
    inspect_admin_uninstall,
    recover_admin_gpu_policy,
    run_admin_cli,
)
from bk.cli import main as bk_main
from bk.config import (
    BROKER_ALL_SOCKET_MODE,
    BROKER_DIR_MODE,
    BROKER_FILE_MODE,
    BROKER_GROUP_SOCKET_MODE,
    Config,
    load_config,
)
from bk.gpu import GpuSnapshot
from bk.login_hook import apply_login_hook_install
from bk.models import BookingError


def non_root_identity() -> AdminIdentity:
    current = pwd.getpwuid(os.getuid())
    if current.pw_uid != 0:
        return AdminIdentity(current.pw_uid, current.pw_name, current.pw_gid)
    for record in pwd.getpwall():
        if record.pw_uid > 0:
            return AdminIdentity(record.pw_uid, record.pw_name, record.pw_gid)
    raise unittest.SkipTest("no non-root account is available")


class TtyInput(StringIO):
    def isatty(self):
        return True


class AdminMaintenanceTests(unittest.TestCase):
    def test_broker_restart_waits_until_ping_succeeds(self):
        config = Config(
            data_dir=Path("/tmp/gpubk-test"),
            broker_socket=Path("/run/gpubk/broker.sock"),
            broker_uid=1001,
            file_mode=0o644,
            dir_mode=0o755,
            broker_socket_mode=0o666,
        )
        client = mock.Mock()
        client.call.side_effect = [
            BookingError("socket absent"),
            BookingError("connection refused"),
            {"status": "ok"},
        ]
        with (
            mock.patch("bk.broker.BrokerClient", return_value=client),
            mock.patch("bk.admin.time.sleep") as sleep,
        ):
            admin_module._wait_for_broker_ready(
                config, attempts=3, delay_seconds=0.01
            )

        self.assertEqual(client.call.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_guided_announcement_uses_safe_defaults(self):
        config = Config(data_dir=Path("/tmp/gpubk-test"), gpu_count=2)
        answers = ["", "", "", "", "y"]
        with (
            mock.patch("bk.admin.os.geteuid", return_value=0),
            mock.patch.object(admin_module.sys.stdin, "isatty", return_value=True),
            mock.patch("bk.admin.load_config", return_value=config),
            mock.patch("builtins.input", side_effect=answers),
            mock.patch("bk.admin._run_admin_notice") as run_notice,
        ):
            run_notice.side_effect = lambda args: 0 if args.notice_action == "publish" else 1
            status = admin_module._run_admin_notice_guide(
                mock.Mock(config_file=Path("/etc/gpubk/config.json")), config
            )

        self.assertEqual(status, 0)
        publish_args = run_notice.call_args.args[0]
        self.assertEqual(publish_args.message, "Server announcement")
        self.assertEqual(publish_args.level, "warning")
        self.assertEqual(publish_args.starts, "now")
        self.assertEqual(publish_args.expires, "24h")

    def test_blackout_can_publish_a_matching_announcement(self):
        config = Config(data_dir=Path("/tmp/gpubk-test"), gpu_count=2)
        plan = mock.Mock()
        with (
            mock.patch("bk.admin.os.geteuid", return_value=0),
            mock.patch("bk.admin.load_config", return_value=config),
            mock.patch("bk.admin.shutil.which", return_value="/usr/bin/systemctl"),
            mock.patch("bk.admin._run_systemctl"),
            mock.patch("bk.admin.inspect_admin_gpu_policy", return_value=plan),
            mock.patch("bk.admin.apply_admin_gpu_policy"),
            mock.patch("bk.admin._run_admin_notice", return_value=0) as notice,
        ):
            status = run_admin_cli(
                [
                    "blackout",
                    "add",
                    "2030-01-01T20:00:00+08:00",
                    "2030-01-01T22:00:00+08:00",
                    "Cooling maintenance",
                    "--announce",
                    "--yes",
                ]
            )

        self.assertEqual(status, 0)
        notice_args = notice.call_args.args[0]
        self.assertEqual(notice_args.level, "warning")
        self.assertEqual(notice_args.message, "Cooling maintenance")
        self.assertEqual(notice_args.starts, "2030-01-01T12:00:00Z")
        self.assertEqual(notice_args.until, "2030-01-01T14:00:00Z")

    def test_guided_maintenance_defaults_block_and_warn_for_two_hours(self):
        config = Config(data_dir=Path("/tmp/gpubk-test"), gpu_count=2)
        answers = ["", "", "", "", "", "y"]
        with (
            mock.patch("bk.admin.os.geteuid", return_value=0),
            mock.patch.object(admin_module.sys.stdin, "isatty", return_value=True),
            mock.patch("bk.admin.load_config", return_value=config),
            mock.patch("builtins.input", side_effect=answers),
            mock.patch("bk.admin._run_admin_blackout", return_value=0) as blackout,
            mock.patch("bk.admin._run_admin_notice", return_value=0) as notice,
        ):
            status = run_admin_cli(["maintain"])

        self.assertEqual(status, 0)
        blackout_args = blackout.call_args.args[0]
        notice_args = notice.call_args.args[0]
        self.assertEqual(blackout_args.reason, "Server maintenance")
        self.assertEqual(notice_args.message, "Server maintenance")
        self.assertEqual(notice_args.level, "warning")
        self.assertEqual(notice_args.expires, "2h")


class AdminInitTests(unittest.TestCase):
    def test_admin_can_enable_worker_logout_persistence_for_one_user(self):
        account = mock.Mock(pw_uid=1003, pw_name="alice")
        states = [
            {"kind": "systemd-linger", "state": "disabled", "logout_safe": False},
            {"kind": "systemd-linger", "state": "enabled", "logout_safe": True},
        ]
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("bk.admin.os.geteuid", return_value=0), \
             mock.patch("bk.admin.pwd.getpwnam", return_value=account), \
             mock.patch("bk.admin.inspect_worker_persistence", side_effect=states), \
             mock.patch("bk.admin.shutil.which", return_value="/usr/bin/loginctl"), \
             mock.patch("bk.admin.subprocess.run", return_value=completed) as run:
            result = run_admin_cli(
                ["worker-persistence", "enable", "alice", "--yes"]
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/loginctl", "enable-linger", "alice"],
        )

    def test_python_uninstall_hint_for_isolated_environment(self):
        with mock.patch.object(admin_module.sys, "prefix", "/opt/gpubk"), mock.patch.object(
            admin_module.sys, "base_prefix", "/usr"
        ), mock.patch.object(
            admin_module.sys, "executable", "/opt/gpubk/bin/python"
        ):
            hint = _python_uninstall_hint()

        self.assertIn("sudo rm -rf /opt/gpubk", hint)
        self.assertNotIn("pip uninstall", hint)

    def test_python_uninstall_hint_for_system_interpreter(self):
        executable = "/usr/bin/python3"
        with mock.patch.object(admin_module.sys, "prefix", "/usr"), mock.patch.object(
            admin_module.sys, "base_prefix", "/usr"
        ), mock.patch.object(admin_module.sys, "executable", executable):
            hint = _python_uninstall_hint()

        resolved = Path(executable).resolve()
        self.assertIn(f"{resolved} -m pip uninstall gpubk", hint)

    def plan(self, root: Path, **changes) -> AdminInitPlan:
        identity = non_root_identity()
        values = {
            "config_file": root / "etc" / "gpubk" / "config.json",
            "data_dir": root / "var" / "lib" / "gpubk",
            "access": "all",
            "gpu_count": 8,
            "slot_minutes": 5,
            "max_shared_users": 2,
            "require_shared_memory": True,
            "service": identity,
            "group_name": None,
            "broker_gid": None,
            "broker_socket": root / "run" / "gpubk" / "broker.sock",
            "broker_socket_mode": BROKER_ALL_SOCKET_MODE,
            "file_mode": BROKER_FILE_MODE,
            "dir_mode": BROKER_DIR_MODE,
        }
        values.update(changes)
        return AdminInitPlan(**values)

    def prepare_parents(self, root: Path) -> None:
        (root / "etc").mkdir(mode=0o755)
        (root / "var").mkdir(mode=0o755)
        (root / "var" / "lib").mkdir(mode=0o755)
        (root / "run").mkdir(mode=0o755)

    def test_cluster_init_builds_local_catalog_without_manual_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cluster.json"
            output = StringIO()
            with (
                mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
                mock.patch("bk.admin_cluster.write_cluster_config") as write_config,
                redirect_stdout(output),
            ):
                status = run_admin_cli(
                    [
                        "cluster",
                        "init",
                        "gpu-a",
                        "--cluster-file",
                        str(path),
                        "--yes",
                    ]
                )
            self.assertEqual(status, 0)
            configured = write_config.call_args.args[0]
            self.assertEqual(configured.nodes[0].name, "gpu-a")
            self.assertEqual(configured.nodes[0].transport, "local")
            self.assertIn("cluster catalog updated", output.getvalue())

    def test_cluster_history_root_and_export_are_explicit_and_reviewable(self):
        from bk.cluster import ClusterConfig, ClusterNode

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "cluster.json"
            node = ClusterNode(
                "gpu-a",
                "a" * 20,
                "local",
                None,
                "/usr/local/bin/bk",
                0,
                8,
            )
            current = ClusterConfig(catalog, (node,))
            archive = root / "archive"
            output = StringIO()
            with (
                mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
                mock.patch("bk.admin_cluster.load_cluster_config", return_value=current),
                mock.patch("bk.admin_cluster.write_cluster_config") as write_config,
                redirect_stdout(output),
            ):
                status = run_admin_cli(
                    [
                        "cluster",
                        "history-root",
                        str(archive),
                        "--cluster-file",
                        str(catalog),
                        "--yes",
                    ]
                )

            self.assertEqual(status, 0)
            configured = write_config.call_args.args[0]
            self.assertEqual(configured.history_root, archive)

            start = datetime(2030, 1, 1, tzinfo=timezone.utc)
            configured = ClusterConfig(catalog, (node,), history_root=archive)
            fake_runtime = Config(data_dir=root / "data", gpu_count=1, monitor_uid=1003)
            output = StringIO()
            with (
                mock.patch("bk.admin_cluster.os.geteuid", return_value=0),
                mock.patch("bk.admin_cluster.load_cluster_config", return_value=configured),
                mock.patch("bk.admin_cluster.load_config", return_value=fake_runtime),
                mock.patch(
                    "bk.cluster_history.resolve_history_window",
                    return_value=(start, start + timedelta(days=1)),
                ),
                mock.patch(
                    "bk.cluster_history.export_cluster_history",
                    return_value={
                        "status": "exported",
                        "root": str(archive),
                        "node_id": node.node_id,
                        "generation": "generation",
                        "files": 2,
                        "bytes": 100,
                    },
                ) as export_history,
                redirect_stdout(output),
            ):
                status = run_admin_cli(
                    [
                        "cluster",
                        "export-history",
                        "--cluster-file",
                        str(catalog),
                        "--yes",
                    ]
                )

            self.assertEqual(status, 0)
            export_history.assert_called_once()
            self.assertIn("cluster history exported", output.getvalue())

    def test_all_user_initialization_is_atomic_idempotent_and_service_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)

            first = apply_admin_init(plan, require_root=False)
            second = apply_admin_init(plan, require_root=False)

            self.assertTrue(first["config_changed"])
            self.assertTrue(first["data_created"])
            self.assertTrue(first["socket_directory_created"])
            self.assertFalse(second["config_changed"])
            self.assertFalse(second["data_created"])
            self.assertFalse(second["socket_directory_created"])
            self.assertEqual(stat.S_IMODE(plan.config_file.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(plan.data_dir.stat().st_mode), 0o755)
            self.assertEqual(plan.data_dir.stat().st_uid, plan.service.uid)
            self.assertEqual(
                stat.S_IMODE(plan.broker_socket.parent.stat().st_mode), 0o755
            )
            document = json.loads(plan.config_file.read_text(encoding="utf-8"))
            self.assertEqual(document["file_mode"], "0644")
            self.assertEqual(document["dir_mode"], "0755")
            self.assertEqual(document["broker_socket_mode"], "0666")
            self.assertEqual(document["broker_uid"], plan.service.uid)
            self.assertNotIn("storage_gid", document)
            manifest = plan.config_file.parent / "install.json"
            self.assertTrue(manifest.is_file())
            self.assertEqual(
                stat.S_IMODE(manifest.stat().st_mode), INSTALL_MANIFEST_MODE
            )

    def test_inspection_previews_clean_initialization_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)

            inspection = inspect_admin_init(
                plan,
                expected_owner=os.geteuid(),
            )

            self.assertEqual(inspection.config_action, "create")
            self.assertEqual(inspection.data_action, "create")
            self.assertEqual(inspection.socket_directory_action, "create")
            self.assertFalse(inspection.data_exists)
            self.assertFalse(inspection.data_nonempty)
            self.assertFalse(plan.config_file.exists())
            self.assertFalse(plan.data_dir.exists())

    def test_unconfigured_nonempty_data_is_never_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            plan.data_dir.mkdir()
            plan.data_dir.chmod(plan.dir_mode)
            marker = plan.data_dir / "existing-data"
            marker.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(BookingError, "unconfigured non-empty"):
                inspect_admin_init(plan, expected_owner=os.geteuid())

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertFalse(plan.config_file.exists())

    def test_group_mode_sets_group_policy_only_when_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            identity = non_root_identity()
            group_record = grp.getgrgid(identity.primary_gid)
            plan = self.plan(
                root,
                access="group",
                service=identity,
                group_name=group_record.gr_name,
                broker_gid=identity.primary_gid,
                broker_socket_mode=BROKER_GROUP_SOCKET_MODE,
            )

            apply_admin_init(plan, require_root=False)

            document = json.loads(plan.config_file.read_text(encoding="utf-8"))
            self.assertEqual(document["file_mode"], "0644")
            self.assertEqual(document["dir_mode"], "0755")
            self.assertEqual(document["broker_gid"], identity.primary_gid)
            self.assertEqual(document["broker_socket_mode"], "0660")
            self.assertEqual(stat.S_IMODE(plan.data_dir.stat().st_mode), 0o755)

    def test_different_config_requires_force_and_nonempty_data_refuses_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            original = self.plan(root)
            apply_admin_init(original, require_root=False)
            changed = self.plan(root, max_shared_users=4)

            with self.assertRaisesRegex(BookingError, "configuration already exists"):
                apply_admin_init(changed, require_root=False)

            marker = original.data_dir / "existing-data"
            marker.write_text("keep", encoding="utf-8")
            marker.chmod(0o666)
            with self.assertRaisesRegex(BookingError, "non-empty data directory"):
                apply_admin_init(changed, force=True, require_root=False)

    def test_force_replaces_config_only_for_empty_data_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            original = self.plan(root)
            apply_admin_init(original, require_root=False)
            changed = self.plan(root, max_shared_users=4)

            result = apply_admin_init(changed, force=True, require_root=False)

            self.assertTrue(result["config_changed"])
            self.assertTrue(Path(result["config_backup"]).is_file())
            backup = json.loads(Path(result["config_backup"]).read_text(encoding="utf-8"))
            current = json.loads(changed.config_file.read_text(encoding="utf-8"))
            self.assertEqual(backup["max_shared_users"], 2)
            self.assertEqual(current["max_shared_users"], 4)

    def test_repeated_force_keeps_the_first_tracked_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            original = self.plan(root)
            apply_admin_init(original, require_root=False)
            first_change = self.plan(root, max_shared_users=4)
            first = apply_admin_init(first_change, force=True, require_root=False)
            backup_path = Path(first["config_backup"])
            second_change = self.plan(root, max_shared_users=3)

            second = apply_admin_init(second_change, force=True, require_root=False)

            self.assertEqual(Path(second["config_backup"]), backup_path)
            backup = json.loads(backup_path.read_text(encoding="utf-8"))
            current = json.loads(second_change.config_file.read_text(encoding="utf-8"))
            self.assertEqual(backup["max_shared_users"], 2)
            self.assertEqual(current["max_shared_users"], 3)

    def test_admin_dry_run_precedes_normal_config_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run").mkdir(mode=0o755)
            broken_data = root / "broken"
            broken_data.mkdir()
            (broken_data / "config.json").write_text("{broken", encoding="utf-8")
            identity = non_root_identity()
            output = StringIO()
            errors = StringIO()
            argv = [
                "admin",
                "init",
                "--dry-run",
                "--json",
                "--gpu-count",
                "8",
                "--disabled-gpus",
                "7",
                "--gpu-priority",
                "6=10,7=20",
                "--service-user",
                str(identity.uid),
                "--broker-socket",
                str(root / "run" / "gpubk" / "broker.sock"),
                "--data-dir",
                str(root / "shared"),
                "--config-file",
                str(root / "config" / "config.json"),
            ]
            with mock.patch.dict(os.environ, {"BK_DATA_DIR": str(broken_data)}, clear=False):
                with redirect_stdout(output), redirect_stderr(errors):
                    status = bk_main(argv)

            self.assertEqual(status, 0, errors.getvalue())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "dry-run")
            self.assertEqual(payload["access"]["mode"], "all")
            self.assertEqual(payload["disabled_gpus"], [7])
            self.assertEqual(payload["gpu_priority"], {"6": 10, "7": 20})
            self.assertFalse(payload["require_shared_memory"])
            self.assertEqual(payload["access"]["file_mode"], "0644")
            self.assertEqual(payload["access"]["dir_mode"], "0755")
            self.assertEqual(payload["access"]["socket_mode"], "0666")
            self.assertEqual(
                payload["access"]["write_boundary"], "service-account-only"
            )
            self.assertEqual(payload["inspection"]["config_action"], "create")
            self.assertEqual(payload["inspection"]["data_action"], "create")
            self.assertFalse((root / "shared").exists())
            self.assertFalse((root / "config").exists())

    def test_json_mode_never_emits_interactive_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run").mkdir(mode=0o755)
            identity = non_root_identity()
            output = StringIO()
            argv = [
                "init",
                "--dry-run",
                "--json",
                "--gpu-count",
                "8",
                "--service-user",
                str(identity.uid),
                "--broker-socket",
                str(root / "run" / "gpubk" / "broker.sock"),
                "--data-dir",
                str(root / "shared"),
                "--config-file",
                str(root / "config" / "config.json"),
            ]

            with mock.patch("sys.stdin", TtyInput("unexpected input\n")):
                with redirect_stdout(output):
                    status = run_admin_cli(argv)

            self.assertEqual(status, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "dry-run")
            self.assertEqual(payload["access"]["mode"], "all")

    def test_apply_requires_root_but_dry_run_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            with mock.patch("bk.admin.os.geteuid", return_value=1234):
                with self.assertRaisesRegex(BookingError, "must run as root"):
                    apply_admin_init(plan)

    def test_json_apply_emits_one_final_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run").mkdir(mode=0o755)
            identity = non_root_identity()
            output = StringIO()
            argv = [
                "init",
                "--yes",
                "--json",
                "--gpu-count",
                "8",
                "--service-user",
                str(identity.uid),
                "--broker-socket",
                str(root / "run" / "gpubk" / "broker.sock"),
                "--data-dir",
                str(root / "shared"),
                "--config-file",
                str(root / "config" / "config.json"),
            ]
            result = {
                "config_changed": True,
                "config_backup": None,
                "data_created": True,
                "socket_directory_created": True,
            }
            with mock.patch("bk.admin.apply_admin_init", return_value=result):
                with redirect_stdout(output):
                    status = run_admin_cli(argv)

            self.assertEqual(status, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "initialized")
            self.assertEqual(payload["result"], result)

    def test_system_services_json_apply_emits_one_final_document(self):
        inspection = {
            "operation": "install",
            "status": "ready",
            "blockers": [],
            "units": {
                "gpubk-broker.service": "original",
                "gpubk-monitor.service": "original",
            },
        }
        result = {
            "status": "installed",
            "units": ["gpubk-broker.service", "gpubk-monitor.service"],
        }
        output = StringIO()
        with (
            mock.patch(
                "bk.admin.inspect_admin_system_services",
                return_value=(mock.sentinel.service_plan, inspection),
            ),
            mock.patch(
                "bk.admin.apply_admin_system_services_install",
                return_value=result,
            ) as apply_services,
            redirect_stdout(output),
        ):
            status = run_admin_cli(
                [
                    "services",
                    "install",
                    "--config-file",
                    "/etc/gpubk/config.json",
                    "--yes",
                    "--json",
                ]
            )

        self.assertEqual(status, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "installed")
        self.assertEqual(payload["inspection"], inspection)
        self.assertEqual(payload["result"], result)
        apply_services.assert_called_once_with(
            Path("/etc/gpubk/config.json"),
            service_plan=mock.sentinel.service_plan,
        )

    def test_system_services_noninteractive_apply_requires_yes(self):
        inspection = {
            "operation": "uninstall",
            "status": "ready",
            "blockers": [],
            "units": {},
        }
        output = StringIO()
        errors = StringIO()
        with (
            mock.patch(
                "bk.admin.inspect_admin_system_services",
                return_value=(mock.sentinel.service_plan, inspection),
            ),
            mock.patch(
                "bk.admin.apply_admin_system_services_uninstall"
            ) as apply_services,
            mock.patch("sys.stdin", StringIO()),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            status = run_admin_cli(
                ["services", "uninstall", "--config-file", "/etc/gpubk/config.json", "--json"]
            )

        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), inspection)
        self.assertIn("pass --yes", errors.getvalue())
        apply_services.assert_not_called()

    def test_system_services_status_returns_nonzero_for_drift(self):
        inspection = {
            "operation": "status",
            "status": "blocked",
            "blockers": ["managed systemd unit drifted"],
            "units": {"gpubk-broker.service": "drifted"},
        }
        output = StringIO()
        with (
            mock.patch(
                "bk.admin.inspect_admin_system_services",
                return_value=(None, inspection),
            ),
            redirect_stdout(output),
        ):
            status = run_admin_cli(
                ["services", "status", "--config-file", "/etc/gpubk/config.json", "--json"]
            )

        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), inspection)

    def test_gpu_policy_json_apply_emits_one_final_document(self):
        config_file = Path("/etc/gpubk/config.json")
        policy = admin_module.AdminGpuPolicyPlan(
            config_file=config_file,
            data_dir=Path("/var/lib/gpubk"),
            broker_socket=Path("/run/gpubk/broker.sock"),
            service=AdminIdentity(1003, "admin", 1003),
            current_disabled_gpus=(),
            desired_disabled_gpus=(7,),
            current_gpu_priority=(),
            desired_gpu_priority=((6, 10),),
            current_require_shared_memory=True,
            desired_require_shared_memory=False,
            require_shared_memory_update=False,
            current_document={"gpu_count": 8},
            desired_document={
                "gpu_count": 8,
                "disabled_gpus": [7],
                "gpu_priority": {"6": 10},
            },
            blockers=(),
        )
        result = {
            "schema_version": admin_module.ADMIN_SCHEMA_VERSION,
            "kind": "admin-gpu-policy",
            "status": "updated",
            "config_file": str(config_file),
            "disabled_gpus": [7],
            "gpu_priority": {"6": 10},
        }
        output = StringIO()
        with (
            mock.patch("bk.admin.inspect_admin_gpu_policy", return_value=policy),
            mock.patch(
                "bk.admin.apply_admin_gpu_policy",
                return_value=result,
            ) as apply_policy,
            redirect_stdout(output),
        ):
            status = run_admin_cli(
                [
                    "gpu-policy",
                    "--config-file",
                    str(config_file),
                    "--disabled-gpus",
                    "7",
                    "--gpu-priority",
                    "6=10",
                    "--allow-implicit-shared-memory",
                    "--yes",
                    "--json",
                ]
            )

        self.assertEqual(status, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "updated")
        self.assertEqual(payload["inspection"]["status"], "planned")
        self.assertFalse(
            payload["inspection"]["desired"]["require_shared_memory"]
        )
        apply_policy.assert_called_once_with(policy)

    def test_gpu_policy_accepts_horizon_and_repeated_blackouts(self):
        config_file = Path("/etc/gpubk/config.json")
        blackouts = (
            ("2030-01-01T00:00:00Z", "2030-01-01T02:00:00Z", "maintenance"),
        )
        policy = admin_module.AdminGpuPolicyPlan(
            config_file=config_file,
            data_dir=Path("/var/lib/gpubk"),
            broker_socket=Path("/run/gpubk/broker.sock"),
            service=AdminIdentity(1003, "admin", 1003),
            current_disabled_gpus=(),
            desired_disabled_gpus=(),
            current_gpu_priority=(),
            desired_gpu_priority=(),
            current_require_shared_memory=False,
            desired_require_shared_memory=False,
            require_shared_memory_update=None,
            current_document={"gpu_count": 8},
            desired_document={
                "gpu_count": 8,
                "booking_horizon_days": 14,
                "max_booking_duration_hours": 72,
            },
            blockers=(),
            current_booking_horizon_days=30,
            desired_booking_horizon_days=14,
            booking_horizon_days_update=14,
            current_max_booking_duration_hours=720,
            desired_max_booking_duration_hours=72,
            max_booking_duration_hours_update=72,
            current_booking_blackouts=(),
            desired_booking_blackouts=blackouts,
            booking_blackouts_update=blackouts,
        )
        output = StringIO()
        with mock.patch(
            "bk.admin.inspect_admin_gpu_policy", return_value=policy
        ) as inspect_policy, redirect_stdout(output):
            status = run_admin_cli(
                [
                    "gpu-policy",
                    "--config-file",
                    str(config_file),
                    "--booking-horizon-days",
                    "14",
                    "--max-booking-hours",
                    "72",
                    "--blackout",
                    "2030-01-01T00:00:00Z",
                    "2030-01-01T02:00:00Z",
                    "maintenance",
                    "--dry-run",
                    "--json",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["desired"]["booking_horizon_days"], 14)
        self.assertEqual(
            json.loads(output.getvalue())["desired"]["max_booking_duration_hours"],
            72,
        )
        self.assertEqual(
            inspect_policy.call_args.kwargs["booking_blackouts"],
            [[
                "2030-01-01T00:00:00Z",
                "2030-01-01T02:00:00Z",
                "maintenance",
            ]],
        )

    def test_gpu_policy_json_recovery_emits_one_final_document(self):
        config_file = Path("/etc/gpubk/config.json")
        inspection = {
            "schema_version": admin_module.ADMIN_SCHEMA_VERSION,
            "kind": "admin-gpu-policy-recovery",
            "status": "ready",
            "config_file": str(config_file),
            "journal": "/etc/gpubk/config-update.json",
            "blockers": [],
        }
        result = {
            "schema_version": admin_module.ADMIN_SCHEMA_VERSION,
            "kind": "admin-gpu-policy-recovery",
            "status": "recovered",
            "config_file": str(config_file),
        }
        output = StringIO()
        with (
            mock.patch(
                "bk.admin.inspect_admin_gpu_policy_recovery",
                return_value=inspection,
            ),
            mock.patch(
                "bk.admin.recover_admin_gpu_policy",
                return_value=result,
            ) as recover_policy,
            redirect_stdout(output),
        ):
            status = run_admin_cli(
                [
                    "gpu-policy",
                    "--config-file",
                    str(config_file),
                    "--recover",
                    "--yes",
                    "--json",
                ]
            )

        self.assertEqual(status, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "recovered")
        self.assertEqual(payload["inspection"], inspection)
        recover_policy.assert_called_once_with(config_file)

    def test_noninteractive_apply_requires_yes_and_emits_a_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run").mkdir(mode=0o755)
            identity = non_root_identity()
            output = StringIO()
            errors = StringIO()
            argv = [
                "init",
                "--json",
                "--gpu-count",
                "8",
                "--service-user",
                str(identity.uid),
                "--broker-socket",
                str(root / "run" / "gpubk" / "broker.sock"),
                "--data-dir",
                str(root / "shared"),
                "--config-file",
                str(root / "config" / "config.json"),
            ]
            with mock.patch("sys.stdin", StringIO()):
                with redirect_stdout(output), redirect_stderr(errors):
                    status = run_admin_cli(argv)

            self.assertEqual(status, 1)
            self.assertEqual(json.loads(output.getvalue())["status"], "planned")
            self.assertIn("pass --yes", errors.getvalue())
            self.assertFalse((root / "shared").exists())

    def test_interactive_init_recovers_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run").mkdir(mode=0o755)
            identity = non_root_identity()
            answers = TtyInput(
                "relative\n"
                f"{root / 'shared'}\n"
                "invalid\n"
                "all\n"
                "many\n"
                "0\n"
                "8\n"
                "9\n"
                "\n"
                "7\n"
                "6=10\n"
                "7\n"
                "5\n"
                "0\n"
                "4\n"
                "maybe\n"
                "yes\n"
            )
            output = StringIO()
            argv = [
                "init",
                "--dry-run",
                "--service-user",
                str(identity.uid),
                "--broker-socket",
                str(root / "run" / "gpubk" / "broker.sock"),
                "--config-file",
                str(root / "config" / "config.json"),
            ]
            with (
                mock.patch("sys.stdin", answers),
                mock.patch("bk.admin._detected_gpu_count", return_value=8),
            ):
                with redirect_stdout(output):
                    status = run_admin_cli(argv)

            text = output.getvalue()
            self.assertEqual(status, 0)
            self.assertIn("Invalid path", text)
            self.assertIn("Please choose one of", text)
            self.assertIn("Please enter a whole number", text)
            self.assertIn("Invalid slice", text)
            self.assertIn("Please answer y or n", text)
            self.assertIn("sharing:    max 4 slots per GPU", text)
            self.assertFalse((root / "shared").exists())

    def test_guided_install_dry_run_previews_services_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run").mkdir(mode=0o755)
            identity = non_root_identity()
            output = StringIO()
            argv = [
                "install",
                "--dry-run",
                "--yes",
                "--no-start",
                "--gpu-count",
                "8",
                "--service-user",
                str(identity.uid),
                "--broker-socket",
                str(root / "run" / "gpubk" / "broker.sock"),
                "--data-dir",
                str(root / "shared"),
                "--config-file",
                str(root / "config" / "config.json"),
            ]
            with redirect_stdout(output):
                status = run_admin_cli(argv)

            self.assertEqual(status, 0)
            self.assertIn("services: install only", output.getvalue())
            self.assertIn("Dry run: no files or services changed", output.getvalue())
            self.assertFalse((root / "config").exists())

    def test_one_command_install_applies_tracked_state_and_starts_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.plan(Path(tmp))
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with (
                mock.patch("bk.admin.os.geteuid", return_value=0),
                mock.patch("bk.admin._detected_gpu_count", return_value=8),
                mock.patch("bk.admin._default_service_identity", return_value=plan.service),
                mock.patch("bk.admin._build_plan", return_value=plan),
                mock.patch("bk.admin._validate_plan"),
                mock.patch("bk.admin.inspect_admin_init", return_value=mock.Mock()),
                mock.patch("bk.admin._print_plan"),
                mock.patch(
                    "bk.admin.plan_command_link_install",
                    return_value=mock.Mock(status="absent", blockers=()),
                ),
                mock.patch("bk.admin.apply_admin_init") as apply_init,
                mock.patch(
                    "bk.admin.apply_admin_command_link_install"
                ) as apply_command_link,
                mock.patch(
                    "bk.admin.inspect_admin_system_services",
                    return_value=(mock.Mock(), {"blockers": []}),
                ),
                mock.patch("bk.admin.apply_admin_system_services_install") as apply_services,
                mock.patch("bk.admin.shutil.which", return_value="/usr/bin/systemctl"),
                mock.patch("bk.admin.subprocess.run", return_value=completed) as run,
                redirect_stdout(StringIO()),
            ):
                status = run_admin_cli(["install", "--yes", "--gpu-count", "8"])

            self.assertEqual(status, 0)
            apply_init.assert_called_once()
            apply_command_link.assert_called_once()
            apply_services.assert_called_once()
            self.assertEqual(run.call_count, 2)
            self.assertEqual(
                run.call_args_list[1].args[0],
                [
                    "/usr/bin/systemctl",
                    "enable",
                    "--now",
                    "gpubk-broker.service",
                    "gpubk-monitor.service",
                ],
            )

    def test_repeated_install_preserves_configuration_and_restarts_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file = root / "etc" / "gpubk" / "config.json"
            config_file.parent.mkdir(parents=True)
            (config_file.parent / "install.json").write_text("{}\n", encoding="utf-8")
            command_path = root / "bin" / "bk"
            command_target = root / "opt" / "gpubk" / "bin" / "bk"
            manifest = {
                "data_dir": str(root / "data"),
                "service_uid": 1003,
                "system_services": {"phase": "installed"},
                "command_link": {
                    "destination": str(command_path),
                    "target": str(command_target),
                },
            }
            document = {
                "data_dir": str(root / "data"),
                "broker_socket": str(root / "run" / "broker.sock"),
                "gpu_count": 8,
                "slot_minutes": 10,
                "max_shared_users": 4,
                "require_shared_memory": False,
                "disabled_gpus": [7],
                "gpu_priority": {"6": 10},
            }
            command_plan = mock.Mock(status="managed", blockers=())
            service_plan = mock.Mock()
            ready = {"blockers": []}
            output = StringIO()
            with (
                mock.patch("bk.admin.os.geteuid", return_value=0),
                mock.patch(
                    "bk.admin._load_admin_services_manifest",
                    return_value=(manifest, document),
                ),
                mock.patch(
                    "bk.admin.plan_command_link_install",
                    return_value=command_plan,
                ),
                mock.patch(
                    "bk.admin.inspect_admin_system_services",
                    side_effect=[(service_plan, ready), (service_plan, ready)],
                ) as inspect_services,
                mock.patch("bk.admin.apply_admin_command_link_install") as apply_link,
                mock.patch("bk.admin.apply_admin_system_services_install") as apply_services,
                mock.patch("bk.admin._detected_gpu_count") as detect_gpus,
                mock.patch("bk.admin.shutil.which", return_value="/usr/bin/systemctl"),
                mock.patch("bk.admin._run_systemctl") as systemctl,
                redirect_stdout(output),
            ):
                status = run_admin_cli(
                    [
                        "install",
                        "--yes",
                        "--config-file",
                        str(config_file),
                        "--gpu-count",
                        "8",
                        "--slot-minutes",
                        "10",
                        "--max-shared-users",
                        "4",
                        "--disabled-gpus",
                        "7",
                        "--gpu-priority",
                        "6=10",
                        "--allow-implicit-shared-memory",
                    ]
                )

            self.assertEqual(status, 0)
            detect_gpus.assert_not_called()
            self.assertTrue(inspect_services.call_args_list[0].kwargs["allow_running"])
            self.assertNotIn("allow_running", inspect_services.call_args_list[1].kwargs)
            apply_link.assert_called_once()
            apply_services.assert_called_once_with(
                config_file,
                service_plan=service_plan,
            )
            self.assertEqual(
                [call.args[1] for call in systemctl.call_args_list],
                ["stop", "daemon-reload", "enable"],
            )
            self.assertIn("policy:   preserve", output.getvalue())
            self.assertIn("reconciled: configuration and data were preserved", output.getvalue())

    def test_repeated_install_rejects_configuration_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file = root / "etc" / "gpubk" / "config.json"
            config_file.parent.mkdir(parents=True)
            (config_file.parent / "install.json").write_text("{}\n", encoding="utf-8")
            manifest = {
                "data_dir": str(root / "data"),
                "service_uid": 1003,
            }
            document = {
                "data_dir": str(root / "data"),
                "broker_socket": str(root / "run" / "broker.sock"),
                "gpu_count": 8,
                "slot_minutes": 5,
                "max_shared_users": 2,
                "require_shared_memory": False,
            }
            with (
                mock.patch("bk.admin.os.geteuid", return_value=0),
                mock.patch(
                    "bk.admin._load_admin_services_manifest",
                    return_value=(manifest, document),
                ),
                mock.patch("bk.admin.inspect_admin_system_services") as inspect_services,
            ):
                with self.assertRaisesRegex(
                    BookingError,
                    "will not rewrite.*--max-shared-users",
                ):
                    run_admin_cli(
                        [
                            "install",
                            "--yes",
                            "--config-file",
                            str(config_file),
                            "--max-shared-users",
                            "4",
                        ]
                    )

            inspect_services.assert_not_called()

    def test_repeated_install_recovers_services_after_a_post_stop_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file = root / "etc" / "gpubk" / "config.json"
            config_file.parent.mkdir(parents=True)
            (config_file.parent / "install.json").write_text("{}\n", encoding="utf-8")
            manifest = {
                "data_dir": str(root / "data"),
                "service_uid": 1003,
                "system_services": {"phase": "installed"},
            }
            document = {
                "data_dir": str(root / "data"),
                "broker_socket": str(root / "run" / "broker.sock"),
                "gpu_count": 8,
                "slot_minutes": 5,
                "max_shared_users": 2,
                "require_shared_memory": False,
            }
            service_plan = mock.Mock()
            with (
                mock.patch("bk.admin.os.geteuid", return_value=0),
                mock.patch(
                    "bk.admin._load_admin_services_manifest",
                    return_value=(manifest, document),
                ),
                mock.patch(
                    "bk.admin.inspect_admin_system_services",
                    side_effect=[
                        (service_plan, {"blockers": []}),
                        (service_plan, {"blockers": ["injected post-stop failure"]}),
                    ],
                ),
                mock.patch("bk.admin.shutil.which", return_value="/usr/bin/systemctl"),
                mock.patch("bk.admin._run_systemctl") as systemctl,
                redirect_stdout(StringIO()),
            ):
                with self.assertRaisesRegex(BookingError, "injected post-stop failure"):
                    run_admin_cli(
                        [
                            "install",
                            "--yes",
                            "--no-command-link",
                            "--config-file",
                            str(config_file),
                        ]
                    )

            self.assertEqual(
                [call.args[1] for call in systemctl.call_args_list],
                ["stop", "daemon-reload", "start"],
            )

    def test_command_link_is_tracked_by_install_manifest_and_full_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            command_dir = root / "usr" / "local" / "bin"
            command_dir.mkdir(parents=True, mode=0o755)
            target_dir = root / "opt" / "gpubk" / "bin"
            target_dir.mkdir(parents=True, mode=0o755)
            target = target_dir / "bk"
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            target.chmod(0o755)
            destination = command_dir / "bk"

            result = apply_admin_command_link_install(
                plan.config_file,
                destination=destination,
                target=target,
                require_root=False,
            )
            manifest = json.loads(
                (plan.config_file.parent / "install.json").read_text(encoding="utf-8")
            )
            self.assertTrue(result["owned"])
            self.assertEqual(manifest["command_link"]["phase"], "installed")
            self.assertEqual(os.readlink(destination), str(target))

            preview = inspect_admin_uninstall(
                plan.config_file,
                purge_data=True,
                expected_owner=os.geteuid(),
            )
            self.assertEqual(preview["status"], "ready")
            self.assertTrue(preview["command_link"]["owned"])
            removed = apply_admin_uninstall(
                plan.config_file,
                purge_data=True,
                require_root=False,
            )
            self.assertTrue(removed["command_link_removed"])
            self.assertFalse(os.path.lexists(destination))

    def test_full_uninstall_preserves_a_preexisting_exact_command_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            command_dir = root / "usr" / "local" / "bin"
            command_dir.mkdir(parents=True, mode=0o755)
            target_dir = root / "opt" / "gpubk" / "bin"
            target_dir.mkdir(parents=True, mode=0o755)
            target = target_dir / "bk"
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            target.chmod(0o755)
            destination = command_dir / "bk"
            destination.symlink_to(target)

            result = apply_admin_command_link_install(
                plan.config_file,
                destination=destination,
                target=target,
                require_root=False,
            )
            self.assertFalse(result["owned"])
            removed = apply_admin_uninstall(
                plan.config_file,
                purge_data=True,
                require_root=False,
            )

            self.assertFalse(removed["command_link_removed"])
            self.assertEqual(os.readlink(destination), str(target))

    def test_gpu_detection_requires_real_telemetry_unless_count_is_explicit(self):
        self.assertEqual(_detected_gpu_count(8), 8)
        with self.assertRaisesRegex(BookingError, "between 1"):
            _detected_gpu_count(0)
        with mock.patch("bk.admin.detect_gpu_count", return_value=2), mock.patch(
            "bk.admin.snapshot",
            return_value=[
                GpuSnapshot(0, "unknown", source="unknown"),
                GpuSnapshot(1, "unknown", source="unknown"),
            ],
        ):
            with self.assertRaisesRegex(BookingError, "could not be verified"):
                _detected_gpu_count(None)
        with mock.patch("bk.admin.detect_gpu_count", return_value=2), mock.patch(
            "bk.admin.snapshot",
            return_value=[
                GpuSnapshot(0, "GPU 0", source="nvml"),
                GpuSnapshot(1, "GPU 1", source="nvml"),
            ],
        ):
            self.assertEqual(_detected_gpu_count(None), 2)

    def test_config_must_stay_outside_shared_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self.plan(
                root,
                config_file=root / "var" / "lib" / "gpubk" / "config.json",
            )
            with self.assertRaisesRegex(BookingError, "outside the shared data"):
                _validate_plan(plan)

    def test_config_filename_cannot_collide_with_admin_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("install.json", "transfer.json"):
                plan = self.plan(
                    root,
                    config_file=root / "etc" / "gpubk" / name,
                )
                with self.assertRaisesRegex(BookingError, "administrator metadata"):
                    _validate_plan(plan)

    def test_init_refuses_an_untracked_transfer_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            plan.config_file.parent.mkdir(mode=0o755)
            journal = plan.config_file.parent / "transfer.json"
            journal.write_text("{}\n", encoding="utf-8")
            journal.chmod(INSTALL_MANIFEST_MODE)

            with self.assertRaisesRegex(BookingError, "journal already exists"):
                inspect_admin_init(plan, expected_owner=os.geteuid())

    def test_existing_permission_drift_and_unsafe_data_paths_are_not_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            plan.data_dir.mkdir(mode=0o700)
            marker = plan.data_dir / "keep"
            marker.write_text("data", encoding="utf-8")
            with self.assertRaisesRegex(
                BookingError, "refusing to change owner or mode"
            ):
                apply_admin_init(plan, require_root=False)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            plan.data_dir.write_text("not a directory", encoding="utf-8")
            with self.assertRaisesRegex(BookingError, "not a real directory"):
                apply_admin_init(plan, require_root=False)

    def test_existing_config_permission_or_shape_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            plan.config_file.chmod(0o600)
            with self.assertRaisesRegex(BookingError, "mode must be 0644"):
                apply_admin_init(plan, require_root=False)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            plan.config_file.parent.mkdir(mode=0o755)
            plan.config_file.write_text("[]", encoding="utf-8")
            plan.config_file.chmod(0o644)
            with self.assertRaisesRegex(BookingError, "JSON object"):
                apply_admin_init(plan, require_root=False)

    def test_uninstall_purge_removes_every_created_server_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            (plan.data_dir / "ledger.json").write_text("{}\n", encoding="utf-8")
            (plan.data_dir / "ledger.json").chmod(BROKER_FILE_MODE)
            profile_dir = root / "etc" / "profile.d"
            profile_dir.mkdir(mode=0o755)
            login_hook = profile_dir / "gpubk.sh"
            apply_login_hook_install(login_hook, require_root=False)

            preview = inspect_admin_uninstall(
                plan.config_file,
                purge_data=True,
                expected_owner=os.geteuid(),
                login_hook_path=login_hook,
            )
            self.assertEqual(preview["status"], "ready")
            self.assertTrue(preview["login_hook_managed"])
            self.assertTrue(plan.config_file.exists())

            result = apply_admin_uninstall(
                plan.config_file,
                purge_data=True,
                require_root=False,
                login_hook_path=login_hook,
            )

            self.assertTrue(result["manifest_removed"])
            self.assertTrue(result["login_hook_removed"])
            self.assertFalse(login_hook.exists())
            self.assertFalse(plan.config_file.parent.exists())
            self.assertFalse(plan.data_dir.exists())
            self.assertFalse(plan.broker_socket.parent.exists())
            self.assertTrue((root / "etc").is_dir())
            self.assertTrue((root / "var" / "lib").is_dir())
            self.assertTrue((root / "run").is_dir())

    def test_uninstall_removes_valid_cluster_catalog_from_created_config_directory(self):
        from bk.cluster import ClusterConfig, ClusterNode, write_cluster_config
        from bk.node_identity import stable_node_identity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            catalog = plan.config_file.parent / "cluster.json"
            identity = stable_node_identity()
            write_cluster_config(
                ClusterConfig(
                    catalog,
                    (
                        ClusterNode(
                            "gpu-a",
                            identity["id"],
                            "local",
                            None,
                            "/usr/local/bin/bk",
                            0,
                            8,
                        ),
                        ClusterNode(
                            "legacy",
                            "b" * 20,
                            "ssh",
                            "legacy@gpu-b",
                            "/usr/local/bin/bk",
                            0,
                            8,
                        ),
                    ),
                ),
                require_root=False,
            )

            preview = inspect_admin_uninstall(
                plan.config_file,
                purge_data=True,
                expected_owner=os.geteuid(),
            )
            self.assertEqual(preview["status"], "ready")
            self.assertEqual(preview["cluster_catalog"], str(catalog))

            result = apply_admin_uninstall(
                plan.config_file,
                purge_data=True,
                require_root=False,
            )

            self.assertTrue(result["cluster_catalog_removed"])
            self.assertFalse(plan.config_file.parent.exists())

    def test_uninstall_preserves_cluster_catalog_in_preexisting_config_directory(self):
        from bk.cluster import ClusterConfig, ClusterNode, write_cluster_config
        from bk.node_identity import stable_node_identity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            plan.config_file.parent.mkdir(mode=0o755)
            apply_admin_init(plan, require_root=False)
            catalog = plan.config_file.parent / "cluster.json"
            identity = stable_node_identity()
            write_cluster_config(
                ClusterConfig(
                    catalog,
                    (
                        ClusterNode(
                            "gpu-a",
                            identity["id"],
                            "local",
                            None,
                            "/usr/local/bin/bk",
                            0,
                            8,
                        ),
                    ),
                ),
                require_root=False,
            )

            result = apply_admin_uninstall(
                plan.config_file,
                purge_data=True,
                require_root=False,
            )

            self.assertFalse(result["cluster_catalog_removed"])
            self.assertTrue(catalog.is_file())

    def test_uninstall_refuses_invalid_named_cluster_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            catalog = plan.config_file.parent / "cluster.json"
            catalog.write_text("{}\n", encoding="utf-8")
            catalog.chmod(0o644)

            with self.assertRaisesRegex(
                BookingError, "unsupported or invalid cluster catalog"
            ):
                inspect_admin_uninstall(
                    plan.config_file,
                    purge_data=True,
                    expected_owner=os.geteuid(),
                )
            self.assertTrue(catalog.is_file())

    def test_uninstall_requires_explicit_purge_for_nonempty_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            (plan.data_dir / "ledger.json").write_text("{}\n", encoding="utf-8")
            (plan.data_dir / "ledger.json").chmod(BROKER_FILE_MODE)

            preview = inspect_admin_uninstall(
                plan.config_file,
                purge_data=False,
                expected_owner=os.geteuid(),
            )
            self.assertEqual(preview["status"], "blocked")
            self.assertIn("--purge-data", preview["blockers"][0])
            with self.assertRaisesRegex(BookingError, "--purge-data"):
                apply_admin_uninstall(
                    plan.config_file,
                    purge_data=False,
                    require_root=False,
                )
            self.assertTrue(plan.config_file.exists())

    def test_uninstall_refuses_modified_config_and_unknown_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            plan.config_file.write_text("{}\n", encoding="utf-8")
            plan.config_file.chmod(0o644)
            with self.assertRaisesRegex(BookingError, "changed after initialization"):
                inspect_admin_uninstall(
                    plan.config_file,
                    purge_data=True,
                    expected_owner=os.geteuid(),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            (plan.data_dir / "unrelated.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(BookingError, "unknown entries"):
                inspect_admin_uninstall(
                    plan.config_file,
                    purge_data=True,
                    expected_owner=os.geteuid(),
                )
            self.assertEqual(
                (plan.data_dir / "unrelated.txt").read_text(encoding="utf-8"),
                "keep",
            )

    def test_uninstall_refuses_a_running_broker_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(plan.broker_socket))
            listener.listen(8)
            try:
                preview = inspect_admin_uninstall(
                    plan.config_file,
                    purge_data=True,
                    expected_owner=os.geteuid(),
                )
                self.assertEqual(preview["socket_state"], "active")
                self.assertEqual(preview["status"], "blocked")
                with self.assertRaisesRegex(BookingError, "broker is running"):
                    apply_admin_uninstall(
                        plan.config_file,
                        purge_data=True,
                        require_root=False,
                    )
            finally:
                listener.close()

    def test_uninstall_refuses_a_running_monitor_or_ledger_transaction(self):
        for lock_name, message in (
            ("usage.lock", "monitor is running"),
            ("ledger.lock", "ledger transaction is active"),
        ):
            with self.subTest(lock_name=lock_name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.prepare_parents(root)
                    plan = self.plan(root)
                    apply_admin_init(plan, require_root=False)
                    lock_path = plan.data_dir / lock_name
                    lock_path.write_bytes(b"")
                    lock_path.chmod(BROKER_FILE_MODE)
                    fd = os.open(lock_path, os.O_RDWR)
                    admin_module.fcntl.flock(fd, admin_module.fcntl.LOCK_EX)
                    try:
                        preview = inspect_admin_uninstall(
                            plan.config_file,
                            purge_data=True,
                            expected_owner=os.geteuid(),
                        )
                        self.assertEqual(preview["status"], "blocked")
                        self.assertTrue(
                            any(message in item for item in preview["blockers"])
                        )
                    finally:
                        admin_module.fcntl.flock(fd, admin_module.fcntl.LOCK_UN)
                        os.close(fd)

    def test_uninstall_restores_preexisting_empty_directories_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            plan.config_file.parent.mkdir(mode=0o755)
            plan.data_dir.mkdir(mode=0o700)
            plan.broker_socket.parent.mkdir(mode=0o700)
            previous = b'{"previous": true}\n'
            plan.config_file.write_bytes(previous)
            plan.config_file.chmod(0o644)

            apply_admin_init(plan, force=True, require_root=False)
            apply_admin_uninstall(
                plan.config_file,
                purge_data=True,
                require_root=False,
            )

            self.assertEqual(plan.config_file.read_bytes(), previous)
            self.assertFalse((plan.config_file.parent / "install.json").exists())
            self.assertFalse((plan.config_file.parent / "config.json.bak").exists())
            self.assertEqual(stat.S_IMODE(plan.data_dir.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE(plan.broker_socket.parent.stat().st_mode), 0o700
            )

    def test_uninstall_recovers_an_interrupted_config_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            plan.config_file.parent.mkdir(mode=0o755)
            previous = b'{"previous": true}\n'
            plan.config_file.write_bytes(previous)
            plan.config_file.chmod(0o644)
            write_file = admin_module._write_new_file

            def fail_new_config(path, payload, mode, *, replace):
                if path == plan.config_file:
                    raise OSError("injected config write failure")
                return write_file(path, payload, mode, replace=replace)

            with mock.patch("bk.admin._write_new_file", side_effect=fail_new_config):
                with self.assertRaisesRegex(OSError, "injected config write failure"):
                    apply_admin_init(plan, force=True, require_root=False)

            backup = plan.config_file.with_name("config.json.bak")
            self.assertTrue(backup.exists())
            preview = inspect_admin_uninstall(
                plan.config_file,
                purge_data=True,
                expected_owner=os.geteuid(),
            )
            self.assertEqual(preview["status"], "ready")

            apply_admin_uninstall(
                plan.config_file,
                purge_data=True,
                require_root=False,
            )

            self.assertEqual(plan.config_file.read_bytes(), previous)
            self.assertFalse(backup.exists())
            self.assertFalse(plan.data_dir.exists())
            self.assertFalse(plan.broker_socket.parent.exists())

    def test_uninstall_recovers_an_interrupted_upgrade_of_a_tracked_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            original = self.plan(root)
            apply_admin_init(original, require_root=False)
            changed = self.plan(root, max_shared_users=4)
            write_file = admin_module._write_new_file

            def fail_new_config(path, payload, mode, *, replace):
                if path == changed.config_file:
                    raise OSError("injected upgrade failure")
                return write_file(path, payload, mode, replace=replace)

            with mock.patch("bk.admin._write_new_file", side_effect=fail_new_config):
                with self.assertRaisesRegex(OSError, "injected upgrade failure"):
                    apply_admin_init(changed, force=True, require_root=False)

            preview = inspect_admin_uninstall(
                changed.config_file,
                purge_data=True,
                expected_owner=os.geteuid(),
            )
            self.assertEqual(preview["status"], "ready")
            apply_admin_uninstall(
                changed.config_file,
                purge_data=True,
                require_root=False,
            )
            self.assertFalse(changed.config_file.parent.exists())
            self.assertFalse(changed.data_dir.exists())
            self.assertFalse(changed.broker_socket.parent.exists())

    def test_uninstall_apply_requires_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            with mock.patch("bk.admin.os.geteuid", return_value=1234):
                with self.assertRaisesRegex(BookingError, "must run as root"):
                    apply_admin_uninstall(plan.config_file, purge_data=True)

    def test_tracked_system_services_complete_install_and_uninstall_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            unit_directory = root / "systemd"
            unit_directory.mkdir(mode=0o755)

            service_plan, inspection = inspect_admin_system_services(
                plan.config_file,
                operation="install",
                unit_directory=unit_directory,
                python_executable=Path(os.sys.executable),
                expected_owner=os.geteuid(),
            )
            self.assertEqual(inspection["status"], "ready")
            result = apply_admin_system_services_install(
                plan.config_file,
                service_plan=service_plan,
                require_root=False,
            )

            self.assertEqual(result["status"], "installed")
            manifest_path = plan.config_file.parent / "install.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["system_services"]["phase"], "installed")
            status_plan, status = inspect_admin_system_services(
                plan.config_file,
                operation="status",
                expected_owner=os.geteuid(),
            )
            self.assertIsNone(status_plan)
            self.assertEqual(status["status"], "installed")
            self.assertEqual(set(status["units"].values()), {"managed"})

            uninstall_preview = inspect_admin_uninstall(
                plan.config_file,
                purge_data=True,
                expected_owner=os.geteuid(),
            )
            self.assertTrue(uninstall_preview["system_services_present"])
            self.assertTrue(
                any("system services" in item for item in uninstall_preview["blockers"])
            )

            removal_plan, removal = inspect_admin_system_services(
                plan.config_file,
                operation="uninstall",
                expected_owner=os.geteuid(),
            )
            self.assertEqual(removal["status"], "ready")
            removed = apply_admin_system_services_uninstall(
                plan.config_file,
                service_plan=removal_plan,
                require_root=False,
            )
            self.assertEqual(removed["status"], "uninstalled")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertNotIn("system_services", manifest)
            for name in ("gpubk-broker.service", "gpubk-monitor.service"):
                self.assertFalse((unit_directory / name).exists())

            uninstall_preview = inspect_admin_uninstall(
                plan.config_file,
                purge_data=True,
                expected_owner=os.geteuid(),
            )
            self.assertFalse(uninstall_preview["system_services_present"])
            self.assertEqual(uninstall_preview["status"], "ready")

    def test_service_reconcile_can_plan_a_controlled_restart_while_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            unit_directory = root / "systemd"
            unit_directory.mkdir(mode=0o755)
            service_plan, _inspection = inspect_admin_system_services(
                plan.config_file,
                operation="install",
                unit_directory=unit_directory,
                expected_owner=os.geteuid(),
            )
            apply_admin_system_services_install(
                plan.config_file,
                service_plan=service_plan,
                require_root=False,
            )

            with (
                mock.patch("bk.admin._broker_socket_state", return_value="active"),
                mock.patch("bk.admin._probe_admin_lock", return_value="active"),
            ):
                _strict_plan, strict = inspect_admin_system_services(
                    plan.config_file,
                    operation="install",
                    unit_directory=unit_directory,
                    expected_owner=os.geteuid(),
                )
                _restart_plan, restart = inspect_admin_system_services(
                    plan.config_file,
                    operation="install",
                    unit_directory=unit_directory,
                    expected_owner=os.geteuid(),
                    allow_running=True,
                )

            self.assertEqual(strict["status"], "blocked")
            self.assertTrue(any("broker is running" in item for item in strict["blockers"]))
            self.assertTrue(any("monitor is running" in item for item in strict["blockers"]))
            self.assertEqual(restart["status"], "ready")
            self.assertEqual(restart["blockers"], [])

    def test_system_service_uninstall_requires_disable_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            unit_directory = root / "systemd"
            unit_directory.mkdir(mode=0o755)
            service_plan, _ = inspect_admin_system_services(
                plan.config_file,
                operation="install",
                unit_directory=unit_directory,
                expected_owner=os.geteuid(),
            )
            apply_admin_system_services_install(
                plan.config_file,
                service_plan=service_plan,
                require_root=False,
            )
            wants = unit_directory / "multi-user.target.wants"
            wants.mkdir()
            (wants / "gpubk-broker.service").symlink_to(
                unit_directory / "gpubk-broker.service"
            )

            _, inspection = inspect_admin_system_services(
                plan.config_file,
                operation="uninstall",
                expected_owner=os.geteuid(),
            )

            self.assertEqual(inspection["status"], "blocked")
            self.assertTrue(any("disable --now" in item for item in inspection["blockers"]))

    def test_gpu_policy_update_is_atomic_and_preserves_other_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(
                root,
                disabled_gpus=(7,),
                gpu_priority=((6, 10),),
            )
            apply_admin_init(plan, require_root=False)
            before = json.loads(plan.config_file.read_text(encoding="utf-8"))

            policy = inspect_admin_gpu_policy(
                plan.config_file,
                disabled_gpus="6,7",
                gpu_priority="5=20,6=10",
                require_shared_memory=False,
                expected_owner=os.geteuid(),
            )
            result = apply_admin_gpu_policy(policy, require_root=False)

            after = json.loads(plan.config_file.read_text(encoding="utf-8"))
            manifest = json.loads(
                (plan.config_file.parent / "install.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["status"], "updated")
            self.assertEqual(after["disabled_gpus"], [6, 7])
            self.assertEqual(after["gpu_priority"], {"5": 20, "6": 10})
            self.assertFalse(after["require_shared_memory"])
            self.assertFalse(result["require_shared_memory"])
            self.assertEqual(after["data_dir"], before["data_dir"])
            self.assertEqual(
                manifest["config_sha256"],
                admin_module._sha256(plan.config_file.read_bytes()),
            )
            self.assertEqual(len(manifest["config_updates"]), 1)
            self.assertIn(
                "require_shared_memory",
                manifest["config_updates"][0]["fields"],
            )
            self.assertFalse(
                (plan.config_file.parent / "config-update.json").exists()
            )

    def test_gpu_policy_update_requires_stopped_broker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            with mock.patch("bk.admin._transfer_socket_state", return_value="active"):
                policy = inspect_admin_gpu_policy(
                    plan.config_file,
                    disabled_gpus="7",
                    expected_owner=os.geteuid(),
                )
                self.assertTrue(any("broker is running" in item for item in policy.blockers))
                with self.assertRaisesRegex(BookingError, "broker is running"):
                    apply_admin_gpu_policy(policy, require_root=False)

    def test_gpu_policy_write_failure_rolls_back_both_trusted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            manifest_path = plan.config_file.parent / "install.json"
            config_before = plan.config_file.read_bytes()
            manifest_before = manifest_path.read_bytes()
            policy = inspect_admin_gpu_policy(
                plan.config_file,
                disabled_gpus="7",
                expected_owner=os.geteuid(),
            )

            with mock.patch(
                "bk.admin._write_manifest",
                side_effect=OSError("injected manifest failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected manifest failure"):
                    apply_admin_gpu_policy(policy, require_root=False)

            self.assertEqual(plan.config_file.read_bytes(), config_before)
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertFalse(
                (plan.config_file.parent / "config-update.json").exists()
            )

    def test_incomplete_gpu_policy_rollback_blocks_startup_until_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_parents(root)
            plan = self.plan(root)
            apply_admin_init(plan, require_root=False)
            config_before = plan.config_file.read_bytes()
            manifest_path = plan.config_file.parent / "install.json"
            manifest_before = manifest_path.read_bytes()
            policy = inspect_admin_gpu_policy(
                plan.config_file,
                disabled_gpus="7",
                expected_owner=os.geteuid(),
            )

            with (
                mock.patch(
                    "bk.admin._write_manifest",
                    side_effect=OSError("injected manifest failure"),
                ),
                mock.patch(
                    "bk.admin._restore_config_update_snapshots",
                    return_value=["injected rollback failure"],
                ),
            ):
                with self.assertRaisesRegex(BookingError, "rollback was incomplete"):
                    apply_admin_gpu_policy(policy, require_root=False)

            journal = plan.config_file.parent / "config-update.json"
            self.assertTrue(journal.is_file())
            with mock.patch.dict(
                os.environ,
                {"BK_CONFIG_FILE": str(plan.config_file)},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "must be recovered"):
                    load_config()

            inspection = inspect_admin_gpu_policy_recovery(
                plan.config_file,
                expected_owner=os.geteuid(),
            )
            self.assertEqual(inspection["status"], "ready")
            result = recover_admin_gpu_policy(
                plan.config_file,
                require_root=False,
            )

            self.assertEqual(result["status"], "recovered")
            self.assertEqual(plan.config_file.read_bytes(), config_before)
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertFalse(journal.exists())

    def test_gpu_policy_recovery_accepts_admin_owned_crash_socket(self):
        journal = {
            "service_uid": 1003,
            "service_gid": 1003,
            "data_dir": "/var/lib/gpubk",
            "broker_socket": "/run/gpubk/broker.sock",
        }
        blockers = mock.Mock(return_value=[])
        with (
            mock.patch(
                "bk.admin._read_config_update_journal",
                return_value=journal,
            ),
            mock.patch("bk.admin._validate_transfer_directory"),
            mock.patch("bk.admin._admin_service_blockers", blockers),
        ):
            inspection = inspect_admin_gpu_policy_recovery(
                Path("/etc/gpubk/config.json"),
                expected_owner=0,
            )

        self.assertEqual(inspection["status"], "ready")
        self.assertEqual(
            blockers.call_args.kwargs["socket_owner_uids"],
            {0, 1003},
        )


class AdminTransferTests(unittest.TestCase):
    def prepare(self, root: Path) -> AdminInitPlan:
        (root / "etc").mkdir(mode=0o755)
        (root / "var").mkdir(mode=0o755)
        (root / "var" / "lib").mkdir(mode=0o755)
        (root / "run").mkdir(mode=0o755)
        identity = non_root_identity()
        plan = AdminInitPlan(
            config_file=root / "etc" / "gpubk" / "config.json",
            data_dir=root / "var" / "lib" / "gpubk",
            access="all",
            gpu_count=8,
            slot_minutes=5,
            max_shared_users=2,
            require_shared_memory=True,
            service=identity,
            group_name=None,
            broker_gid=None,
            broker_socket=root / "run" / "gpubk" / "broker.sock",
            broker_socket_mode=BROKER_ALL_SOCKET_MODE,
            file_mode=BROKER_FILE_MODE,
            dir_mode=BROKER_DIR_MODE,
        )
        apply_admin_init(plan, require_root=False)
        return plan

    @staticmethod
    def target(plan: AdminInitPlan) -> AdminIdentity:
        return AdminIdentity(
            plan.service.uid + 100_000,
            "next-admin",
            plan.service.primary_gid + 100_000,
        )

    def test_default_service_account_is_the_user_who_invoked_sudo(self):
        identity = non_root_identity()
        with mock.patch.dict(
            os.environ,
            {"SUDO_UID": str(identity.uid), "SUDO_USER": identity.username},
            clear=True,
        ):
            selected = admin_module._default_service_identity(None)

        self.assertEqual(selected, identity)

    def test_default_service_account_is_current_user_without_sudo(self):
        identity = non_root_identity()
        if identity.uid != os.getuid():
            self.skipTest("current test process is root")
        with mock.patch.dict(os.environ, {}, clear=True):
            selected = admin_module._default_service_identity(None)

        self.assertEqual(selected, identity)

    def test_transfer_dry_run_does_not_create_guards_or_modify_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self.prepare(root)
            before = {
                str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            _, inspection = admin_module.inspect_admin_transfer(
                plan.config_file,
                self.target(plan),
                expected_owner=os.geteuid(),
            )

            after = {
                str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(inspection["status"], "ready")
            self.assertEqual(after, before)
            self.assertFalse((plan.data_dir / "usage.lock").exists())
            self.assertFalse((plan.data_dir / "ledger.lock").exists())

    def test_running_broker_and_monitor_each_block_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self.prepare(root)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(plan.broker_socket))
            listener.listen(1)
            try:
                _, inspection = admin_module.inspect_admin_transfer(
                    plan.config_file,
                    self.target(plan),
                    expected_owner=os.geteuid(),
                )
                self.assertEqual(inspection["status"], "blocked")
                self.assertIn("broker is running", inspection["blockers"][0])
            finally:
                listener.close()
                plan.broker_socket.unlink()

            lock_path = plan.data_dir / "usage.lock"
            lock_path.write_bytes(b"")
            lock_path.chmod(BROKER_FILE_MODE)
            fd = os.open(lock_path, os.O_RDWR)
            admin_module.fcntl.flock(fd, admin_module.fcntl.LOCK_EX)
            try:
                _, inspection = admin_module.inspect_admin_transfer(
                    plan.config_file,
                    self.target(plan),
                    expected_owner=os.geteuid(),
                )
                self.assertEqual(inspection["status"], "blocked")
                self.assertTrue(
                    any("monitor is running" in item for item in inspection["blockers"])
                )
            finally:
                admin_module.fcntl.flock(fd, admin_module.fcntl.LOCK_UN)
                os.close(fd)

    def test_same_account_transfer_is_an_unchanged_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self.prepare(root)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(plan.broker_socket))
            listener.listen(1)
            try:
                result = admin_module.apply_admin_transfer(
                    plan.config_file,
                    plan.service,
                    require_root=False,
                )
            finally:
                listener.close()
                plan.broker_socket.unlink()

            self.assertEqual(result["status"], "unchanged")

    def test_transfer_preserves_ledger_and_changes_only_service_identity_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self.prepare(root)
            target = self.target(plan)
            ledger = plan.data_dir / "ledger.json"
            ledger_payload = b'{"reservations":[{"uid":1234}]}\n'
            ledger.write_bytes(ledger_payload)
            ledger.chmod(BROKER_FILE_MODE)
            config_before = json.loads(plan.config_file.read_text(encoding="utf-8"))
            config_before["future_extension"] = {"policy": "preserve-me"}
            config_payload = admin_module._config_payload(config_before)
            admin_module._write_new_file(
                plan.config_file,
                config_payload,
                admin_module.CONFIG_FILE_MODE,
                replace=True,
            )
            manifest_path = plan.config_file.parent / "install.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["config_sha256"] = admin_module._sha256(config_payload)
            admin_module._write_manifest(manifest_path, manifest, replace=True)

            with (
                mock.patch("bk.admin._retarget_managed_tree") as retarget_tree,
                mock.patch("bk.admin._retarget_transfer_path") as retarget_path,
            ):
                result = admin_module.apply_admin_transfer(
                    plan.config_file,
                    target,
                    require_root=False,
                )

            config_after = json.loads(plan.config_file.read_text(encoding="utf-8"))
            unchanged_before = dict(config_before)
            unchanged_after = dict(config_after)
            self.assertEqual(unchanged_before.pop("broker_uid"), plan.service.uid)
            self.assertEqual(unchanged_before.pop("monitor_uid"), plan.service.uid)
            self.assertEqual(unchanged_after.pop("broker_uid"), target.uid)
            self.assertEqual(unchanged_after.pop("monitor_uid"), target.uid)
            self.assertEqual(unchanged_after, unchanged_before)
            self.assertEqual(
                config_after["future_extension"],
                {"policy": "preserve-me"},
            )
            self.assertEqual(ledger.read_bytes(), ledger_payload)
            self.assertEqual(result["status"], "transferred")
            self.assertFalse(result["reservations_rewritten"])
            self.assertEqual(retarget_tree.call_count, 1)
            self.assertEqual(retarget_path.call_count, 1)
            manifest = json.loads(
                (plan.config_file.parent / "install.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["service_uid"], target.uid)
            self.assertEqual(manifest["service_gid"], target.primary_gid)
            self.assertEqual(len(manifest["service_transfers"]), 1)
            self.assertFalse((plan.config_file.parent / "transfer.json").exists())

    def test_transfer_updates_tracked_system_units_and_failure_rolls_them_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self.prepare(root)
            target = self.target(plan)
            unit_directory = root / "systemd"
            unit_directory.mkdir(mode=0o755)
            service_plan, _ = inspect_admin_system_services(
                plan.config_file,
                operation="install",
                unit_directory=unit_directory,
                expected_owner=os.geteuid(),
            )
            apply_admin_system_services_install(
                plan.config_file,
                service_plan=service_plan,
                require_root=False,
            )
            broker_unit = unit_directory / "gpubk-broker.service"
            original_unit = broker_unit.read_bytes()

            with (
                mock.patch("bk.admin._retarget_managed_tree"),
                mock.patch("bk.admin._retarget_transfer_path"),
                mock.patch(
                    "bk.admin._write_manifest",
                    side_effect=OSError("injected manifest failure"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "injected manifest failure"):
                    admin_module.apply_admin_transfer(
                        plan.config_file,
                        target,
                        require_root=False,
                    )

            self.assertEqual(broker_unit.read_bytes(), original_unit)
            self.assertFalse((plan.config_file.parent / "transfer.json").exists())

            with (
                mock.patch("bk.admin._retarget_managed_tree"),
                mock.patch("bk.admin._retarget_transfer_path"),
            ):
                result = admin_module.apply_admin_transfer(
                    plan.config_file,
                    target,
                    require_root=False,
                )

            self.assertTrue(result["system_services_updated"])
            rendered = broker_unit.read_text(encoding="utf-8")
            self.assertIn(f"User={target.uid}", rendered)
            self.assertIn(f"Group={target.primary_gid}", rendered)
            manifest = json.loads(
                (plan.config_file.parent / "install.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["system_services"]["service_uid"], target.uid
            )

    def test_failed_transfer_rolls_back_config_manifest_and_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self.prepare(root)
            target = self.target(plan)
            manifest_path = plan.config_file.parent / "install.json"
            config_before = plan.config_file.read_bytes()
            manifest_before = manifest_path.read_bytes()

            with (
                mock.patch("bk.admin._retarget_managed_tree"),
                mock.patch("bk.admin._retarget_transfer_path"),
                mock.patch(
                    "bk.admin._write_manifest",
                    side_effect=OSError("injected manifest failure"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "injected manifest failure"):
                    admin_module.apply_admin_transfer(
                        plan.config_file,
                        target,
                        require_root=False,
                    )

            self.assertEqual(plan.config_file.read_bytes(), config_before)
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertFalse((plan.config_file.parent / "transfer.json").exists())

    def test_interrupted_transfer_is_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self.prepare(root)
            target = self.target(plan)
            config_before = plan.config_file.read_bytes()
            with (
                mock.patch("bk.admin._retarget_managed_tree"),
                mock.patch("bk.admin._retarget_transfer_path"),
                mock.patch(
                    "bk.admin._write_manifest",
                    side_effect=OSError("injected manifest failure"),
                ),
                mock.patch(
                    "bk.admin._rollback_admin_transfer",
                    return_value=["injected rollback failure"],
                ),
            ):
                with self.assertRaisesRegex(BookingError, "rollback was incomplete"):
                    admin_module.apply_admin_transfer(
                        plan.config_file,
                        target,
                        require_root=False,
                    )

            journal = plan.config_file.parent / "transfer.json"
            self.assertTrue(journal.exists())
            self.assertNotEqual(plan.config_file.read_bytes(), config_before)

            result = admin_module.recover_admin_transfer(
                plan.config_file,
                require_root=False,
            )

            self.assertEqual(result["status"], "recovered")
            self.assertEqual(plan.config_file.read_bytes(), config_before)
            self.assertFalse(journal.exists())

    def test_transfer_rejects_links_and_uninstall_refuses_an_open_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self.prepare(root)
            usage = plan.data_dir / "usage"
            usage.mkdir(mode=BROKER_DIR_MODE)
            (usage / "unsafe").symlink_to(plan.config_file)
            with self.assertRaisesRegex(BookingError, "symbolic"):
                admin_module.inspect_admin_transfer(
                    plan.config_file,
                    self.target(plan),
                    expected_owner=os.geteuid(),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self.prepare(root)
            journal = plan.config_file.parent / "transfer.json"
            journal.write_text("{}\n", encoding="utf-8")
            journal.chmod(INSTALL_MANIFEST_MODE)
            with self.assertRaisesRegex(BookingError, "must be recovered"):
                inspect_admin_uninstall(
                    plan.config_file,
                    purge_data=True,
                    expected_owner=os.geteuid(),
                )

    def test_transfer_rejects_hard_linked_managed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self.prepare(root)
            backups = plan.data_dir / "backups"
            backups.mkdir(mode=BROKER_DIR_MODE)
            first = backups / "one.json"
            first.write_text("{}\n", encoding="utf-8")
            first.chmod(BROKER_FILE_MODE)
            os.link(first, backups / "two.json")

            with self.assertRaisesRegex(BookingError, "hard-linked"):
                admin_module.inspect_admin_transfer(
                    plan.config_file,
                    self.target(plan),
                    expected_owner=os.geteuid(),
                )

    def test_transfer_json_apply_emits_one_document(self):
        target = AdminIdentity(1234, "next-admin", 1234)
        inspection = {
            "status": "ready",
            "blockers": [],
            "from": {"uid": 1000, "gid": 1000, "username": "old-admin"},
            "to": {"uid": 1234, "gid": 1234, "username": "next-admin"},
        }
        result = {
            "status": "transferred",
            "service_uid": 1234,
            "service_gid": 1234,
            "service_username": "next-admin",
        }
        output = StringIO()
        with (
            mock.patch("bk.admin._resolve_identity", return_value=target),
            mock.patch(
                "bk.admin.inspect_admin_transfer",
                return_value=(mock.sentinel.plan, inspection),
            ),
            mock.patch("bk.admin.apply_admin_transfer", return_value=result),
            redirect_stdout(output),
        ):
            status = run_admin_cli(
                [
                    "transfer",
                    "next-admin",
                    "--config-file",
                    "/etc/gpubk/config.json",
                    "--yes",
                    "--json",
                ]
            )

        self.assertEqual(status, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "transferred")
        self.assertEqual(payload["inspection"], inspection)
        self.assertEqual(payload["result"], result)

    def test_transfer_apply_requires_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self.prepare(root)
            with mock.patch("bk.admin.os.geteuid", return_value=1234):
                with self.assertRaisesRegex(BookingError, "must run as root"):
                    admin_module.apply_admin_transfer(
                        plan.config_file,
                        self.target(plan),
                    )


@unittest.skipUnless(os.geteuid() == 0, "requires root for real UID ownership transfer")
class AdminRootLifecycleTests(unittest.TestCase):
    def test_real_init_transfer_and_uninstall_lifecycle(self):
        accounts = [
            AdminIdentity(record.pw_uid, record.pw_name, record.pw_gid)
            for record in pwd.getpwall()
            if record.pw_uid > 0
        ]
        unique_accounts = []
        for account in accounts:
            if all(existing.uid != account.uid for existing in unique_accounts):
                unique_accounts.append(account)
            if len(unique_accounts) == 2:
                break
        if len(unique_accounts) < 2:
            self.skipTest("two non-root local accounts are required")
        service, target = unique_accounts

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "etc").mkdir(mode=0o755)
            (root / "var").mkdir(mode=0o755)
            (root / "var" / "lib").mkdir(mode=0o755)
            (root / "run").mkdir(mode=0o755)
            plan = AdminInitPlan(
                config_file=root / "etc" / "gpubk" / "config.json",
                data_dir=root / "var" / "lib" / "gpubk",
                access="all",
                gpu_count=8,
                slot_minutes=5,
                max_shared_users=2,
                require_shared_memory=True,
                service=service,
                group_name=None,
                broker_gid=None,
                broker_socket=root / "run" / "gpubk" / "broker.sock",
                broker_socket_mode=BROKER_ALL_SOCKET_MODE,
                file_mode=BROKER_FILE_MODE,
                dir_mode=BROKER_DIR_MODE,
            )
            apply_admin_init(plan)
            ledger = plan.data_dir / "ledger.json"
            ledger_payload = b'{"reservations":[{"uid":4242}]}\n'
            ledger.write_bytes(ledger_payload)
            ledger.chmod(BROKER_FILE_MODE)
            os.chown(ledger, service.uid, service.primary_gid)
            unit_directory = root / "systemd"
            unit_directory.mkdir(mode=0o755)
            service_plan, inspection = inspect_admin_system_services(
                plan.config_file,
                operation="install",
                unit_directory=unit_directory,
                python_executable=Path(os.sys.executable),
            )
            self.assertEqual(inspection["status"], "ready")
            apply_admin_system_services_install(
                plan.config_file,
                service_plan=service_plan,
            )

            result = admin_module.apply_admin_transfer(plan.config_file, target)

            self.assertEqual(result["status"], "transferred")
            self.assertEqual(ledger.read_bytes(), ledger_payload)
            self.assertEqual(
                (ledger.stat().st_uid, ledger.stat().st_gid),
                (target.uid, target.primary_gid),
            )
            self.assertEqual(
                (plan.data_dir.stat().st_uid, plan.data_dir.stat().st_gid),
                (target.uid, target.primary_gid),
            )
            config = json.loads(plan.config_file.read_text(encoding="utf-8"))
            self.assertEqual(config["broker_uid"], target.uid)
            self.assertEqual(config["monitor_uid"], target.uid)
            self.assertFalse((plan.config_file.parent / "transfer.json").exists())
            broker_unit = (unit_directory / "gpubk-broker.service").read_text(
                encoding="utf-8"
            )
            self.assertIn(f"User={target.uid}", broker_unit)
            self.assertIn(f"Group={target.primary_gid}", broker_unit)

            removal_plan, inspection = inspect_admin_system_services(
                plan.config_file,
                operation="uninstall",
            )
            self.assertEqual(inspection["status"], "ready")
            apply_admin_system_services_uninstall(
                plan.config_file,
                service_plan=removal_plan,
            )

            uninstall = apply_admin_uninstall(
                plan.config_file,
                purge_data=True,
            )

            self.assertTrue(uninstall["manifest_removed"])
            self.assertFalse(plan.config_file.parent.exists())
            self.assertFalse(plan.data_dir.exists())
            self.assertFalse(plan.broker_socket.parent.exists())


if __name__ == "__main__":
    unittest.main()
