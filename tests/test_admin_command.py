import os
import tempfile
import unittest
from pathlib import Path

from bk.admin_command import (
    PHASE_INSTALLED,
    apply_command_link_install,
    apply_command_link_uninstall,
    inspect_command_link,
    plan_command_link_install,
)
from bk.models import BookingError


class AdminCommandLinkTests(unittest.TestCase):
    def paths(self, root: Path) -> tuple[Path, Path]:
        binary = root / "bin"
        target_dir = root / "opt" / "gpubk" / "bin"
        binary.mkdir(parents=True, mode=0o755)
        target_dir.mkdir(parents=True, mode=0o755)
        target = target_dir / "bk"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o755)
        return binary / "bk", target

    def test_created_link_is_resumable_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination, target = self.paths(Path(tmp))
            plan = plan_command_link_install(
                existing=None,
                destination=destination,
                target=target,
                expected_owner=os.geteuid(),
            )
            self.assertTrue(plan.document["owned"])
            self.assertEqual(plan.status, "absent")

            installed = apply_command_link_install(
                plan.document,
                expected_owner=os.geteuid(),
            )
            self.assertEqual(installed["phase"], PHASE_INSTALLED)
            self.assertEqual(os.readlink(destination), str(target))

            resumed = plan_command_link_install(
                existing=plan.document,
                destination=destination,
                target=target,
                expected_owner=os.geteuid(),
            )
            finalized = apply_command_link_install(
                resumed.document,
                expected_owner=os.geteuid(),
            )
            self.assertTrue(finalized["owned"])
            self.assertTrue(
                apply_command_link_uninstall(
                    finalized,
                    expected_owner=os.geteuid(),
                )
            )
            self.assertFalse(os.path.lexists(destination))

    def test_preexisting_exact_link_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination, target = self.paths(Path(tmp))
            destination.symlink_to(target)
            plan = plan_command_link_install(
                existing=None,
                destination=destination,
                target=target,
                expected_owner=os.geteuid(),
            )
            self.assertFalse(plan.document["owned"])
            installed = apply_command_link_install(
                plan.document,
                expected_owner=os.geteuid(),
            )
            self.assertFalse(
                apply_command_link_uninstall(
                    installed,
                    expected_owner=os.geteuid(),
                )
            )
            self.assertEqual(os.readlink(destination), str(target))

    def test_unknown_path_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination, target = self.paths(Path(tmp))
            destination.write_text("keep\n", encoding="utf-8")
            plan = plan_command_link_install(
                existing=None,
                destination=destination,
                target=target,
                expected_owner=os.geteuid(),
            )
            self.assertTrue(plan.blockers)
            with self.assertRaisesRegex(BookingError, "non-symlink"):
                apply_command_link_install(
                    plan.document,
                    expected_owner=os.geteuid(),
                )
            self.assertEqual(destination.read_text(encoding="utf-8"), "keep\n")

    def test_drift_and_missing_preexisting_link_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination, target = self.paths(Path(tmp))
            destination.symlink_to(target)
            installed = apply_command_link_install(
                plan_command_link_install(
                    existing=None,
                    destination=destination,
                    target=target,
                    expected_owner=os.geteuid(),
                ).document,
                expected_owner=os.geteuid(),
            )
            destination.unlink()
            inspection = inspect_command_link(
                installed,
                expected_owner=os.geteuid(),
            )
            self.assertIn("pre-existing command link is missing", inspection["blockers"][0])

    def test_target_must_be_root_equivalent_owned_and_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination, target = self.paths(Path(tmp))
            target.chmod(0o644)
            with self.assertRaisesRegex(BookingError, "not executable"):
                plan_command_link_install(
                    existing=None,
                    destination=destination,
                    target=target,
                    expected_owner=os.geteuid(),
                )

    def test_uninstall_removes_managed_dangling_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination, target = self.paths(Path(tmp))
            installed = apply_command_link_install(
                plan_command_link_install(
                    existing=None,
                    destination=destination,
                    target=target,
                    expected_owner=os.geteuid(),
                ).document,
                expected_owner=os.geteuid(),
            )
            target.unlink()

            self.assertTrue(
                apply_command_link_uninstall(
                    installed,
                    expected_owner=os.geteuid(),
                )
            )
            self.assertFalse(os.path.lexists(destination))


if __name__ == "__main__":
    unittest.main()
