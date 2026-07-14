import os
import tempfile
import unittest
from pathlib import Path

from bk.admin_services import (
    PHASE_INSTALLED,
    PHASE_INSTALLING,
    PHASE_REMOVING,
    apply_installed_system_services,
    apply_system_services_install,
    apply_system_services_uninstall,
    enabled_unit_links,
    inspect_system_service_files,
    plan_system_services_install,
    plan_system_services_uninstall,
    retarget_system_services_document,
)
from bk.models import BookingError
from bk.systemd import system_unit_names


class AdminSystemServicesTests(unittest.TestCase):
    def plan(self, root: Path, *, existing=None, force=False, uid=1001, gid=1001):
        units = root / "systemd"
        units.mkdir(mode=0o755, exist_ok=True)
        return plan_system_services_install(
            existing=existing,
            config_file=root / "etc" / "gpubk" / "config.json",
            data_dir=root / "var" / "lib" / "gpubk",
            socket_directory=root / "run" / "gpubk",
            service_uid=uid,
            service_gid=gid,
            unit_directory=units,
            python_executable=Path(os.sys.executable),
            expected_owner=os.geteuid(),
            force=force,
        )

    def test_install_is_resumable_and_uninstall_restores_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pending = self.plan(root)

            self.assertEqual(pending.document["phase"], PHASE_INSTALLING)
            self.assertEqual(set(pending.statuses.values()), {"original"})
            installed = apply_system_services_install(
                pending.document, expected_owner=os.geteuid()
            )

            self.assertEqual(installed["phase"], PHASE_INSTALLED)
            statuses, blockers = inspect_system_service_files(installed)
            self.assertEqual(set(statuses.values()), {"managed"})
            self.assertEqual(blockers, [])
            for name in system_unit_names():
                text = (root / "systemd" / name).read_text(encoding="utf-8")
                self.assertIn("User=1001", text)
                self.assertIn("Group=1001", text)

            removing = plan_system_services_uninstall(installed)
            self.assertEqual(removing.document["phase"], PHASE_REMOVING)
            apply_system_services_uninstall(
                removing.document, expected_owner=os.geteuid()
            )
            for name in system_unit_names():
                self.assertFalse((root / "systemd" / name).exists())

    def test_update_accepts_previous_managed_content_and_converges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed = apply_system_services_install(
                self.plan(root).document,
                expected_owner=os.geteuid(),
            )

            pending = self.plan(root, existing=installed, uid=2001, gid=2002)

            self.assertEqual(
                set(pending.statuses.values()), {"previous-managed"}
            )
            updated = apply_system_services_install(
                pending.document,
                expected_owner=os.geteuid(),
            )
            self.assertEqual(updated["service_uid"], 2001)
            self.assertEqual(updated["service_gid"], 2002)
            for name in system_unit_names():
                text = (root / "systemd" / name).read_text(encoding="utf-8")
                self.assertIn("User=2001", text)
                self.assertIn("Group=2002", text)

    def test_transfer_retarget_can_apply_and_roll_back_managed_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = apply_system_services_install(
                self.plan(root).document,
                expected_owner=os.geteuid(),
            )
            transferred = retarget_system_services_document(
                original,
                service_uid=3001,
                service_gid=3002,
                expected_owner=os.geteuid(),
            )

            apply_installed_system_services(
                transferred,
                allowed_current=[original],
                expected_owner=os.geteuid(),
            )
            self.assertIn(
                "User=3001",
                (root / "systemd" / "gpubk-broker.service").read_text(
                    encoding="utf-8"
                ),
            )

            apply_installed_system_services(
                original,
                allowed_current=[transferred],
                expected_owner=os.geteuid(),
            )
            self.assertIn(
                "User=1001",
                (root / "systemd" / "gpubk-broker.service").read_text(
                    encoding="utf-8"
                ),
            )

    def test_force_replacement_restores_reviewed_existing_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            units = root / "systemd"
            units.mkdir(mode=0o755)
            originals = {}
            for name in system_unit_names():
                path = units / name
                content = f"# pre-existing {name}\n"
                path.write_text(content, encoding="utf-8")
                path.chmod(0o644)
                originals[name] = content

            with self.assertRaisesRegex(BookingError, "untracked"):
                self.plan(root)
            installed = apply_system_services_install(
                self.plan(root, force=True).document,
                expected_owner=os.geteuid(),
            )
            removing = plan_system_services_uninstall(installed)
            apply_system_services_uninstall(
                removing.document, expected_owner=os.geteuid()
            )

            for name, content in originals.items():
                self.assertEqual(
                    (units / name).read_text(encoding="utf-8"), content
                )

    def test_drift_and_enabled_links_are_reported_without_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed = apply_system_services_install(
                self.plan(root).document,
                expected_owner=os.geteuid(),
            )
            unit = root / "systemd" / "gpubk-monitor.service"
            unit.write_text("# externally changed\n", encoding="utf-8")
            unit.chmod(0o644)

            statuses, blockers = inspect_system_service_files(installed)
            self.assertEqual(statuses["gpubk-monitor.service"], "drifted")
            self.assertTrue(blockers)
            with self.assertRaisesRegex(BookingError, "drifted"):
                apply_system_services_uninstall(
                    plan_system_services_uninstall(installed).document,
                    expected_owner=os.geteuid(),
                )
            self.assertTrue(unit.exists())

            wants = root / "systemd" / "multi-user.target.wants"
            wants.mkdir()
            (wants / "gpubk-broker.service").symlink_to(
                root / "systemd" / "gpubk-broker.service"
            )
            self.assertEqual(
                enabled_unit_links(installed),
                (wants / "gpubk-broker.service",),
            )


if __name__ == "__main__":
    unittest.main()
