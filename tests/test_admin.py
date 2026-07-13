import json
import grp
import os
import pwd
import socket
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import bk.admin as admin_module
from bk.admin import (
    AdminIdentity,
    AdminInitPlan,
    INSTALL_MANIFEST_MODE,
    _detected_gpu_count,
    _validate_plan,
    apply_admin_init,
    apply_admin_uninstall,
    inspect_admin_init,
    inspect_admin_uninstall,
    run_admin_cli,
)
from bk.cli import main as bk_main
from bk.config import (
    BROKER_ALL_SOCKET_MODE,
    BROKER_DIR_MODE,
    BROKER_FILE_MODE,
    BROKER_GROUP_SOCKET_MODE,
)
from bk.gpu import GpuSnapshot
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


class AdminInitTests(unittest.TestCase):
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
                "7\n"
                "5\n"
                "0\n"
                "4\n"
                "maybe\n"
                "yes\n"
                "\n"
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
            self.assertIn("sharing:    4", text)
            self.assertFalse((root / "shared").exists())

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

            preview = inspect_admin_uninstall(
                plan.config_file,
                purge_data=True,
                expected_owner=os.geteuid(),
            )
            self.assertEqual(preview["status"], "ready")
            self.assertTrue(plan.config_file.exists())

            result = apply_admin_uninstall(
                plan.config_file,
                purge_data=True,
                require_root=False,
            )

            self.assertTrue(result["manifest_removed"])
            self.assertFalse(plan.config_file.parent.exists())
            self.assertFalse(plan.data_dir.exists())
            self.assertFalse(plan.broker_socket.parent.exists())
            self.assertTrue((root / "etc").is_dir())
            self.assertTrue((root / "var" / "lib").is_dir())
            self.assertTrue((root / "run").is_dir())

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
