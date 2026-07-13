import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bk.models import BookingError
from bk.skill import SKILL_NAME, default_skill_path, install_skill, skill_text


class BundledSkillTests(unittest.TestCase):
    def test_default_path_uses_only_an_absolute_codex_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            explicit = root / "codex-home"

            with mock.patch.dict(
                os.environ,
                {"HOME": str(home), "CODEX_HOME": str(explicit)},
                clear=True,
            ):
                self.assertEqual(default_skill_path(), explicit / "skills" / SKILL_NAME)
            for value in ("", "relative/codex-home"):
                with mock.patch.dict(
                    os.environ,
                    {"HOME": str(home), "CODEX_HOME": value},
                    clear=True,
                ):
                    self.assertEqual(
                        default_skill_path(),
                        home / ".codex" / "skills" / SKILL_NAME,
                    )

    def test_bundled_skill_has_expected_trigger_metadata(self):
        text = skill_text()

        self.assertEqual(SKILL_NAME, "gpubk")
        self.assertIn(f"name: {SKILL_NAME}", text)
        self.assertIn("expected VRAM", text)
        self.assertIn("operation ID", text)
        self.assertIn("edit_my_gpu_booking", text)
        self.assertIn("bk agent edit", text)
        self.assertIn("collector.fresh", text)
        self.assertIn("--require-monitor", text)

    def test_install_is_complete_and_refuses_accidental_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / SKILL_NAME

            installed = install_skill(destination)

            self.assertEqual(installed, destination)
            self.assertTrue((destination / "SKILL.md").is_file())
            self.assertTrue((destination / "agents" / "openai.yaml").is_file())
            self.assertTrue((destination / "references" / "protocol.md").is_file())
            with self.assertRaisesRegex(BookingError, "already exists"):
                install_skill(destination)

    def test_force_only_replaces_a_recognized_gpubk_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrong = Path(tmp) / "unrelated"
            wrong.mkdir()
            (wrong / "SKILL.md").write_text("name: unrelated\n", encoding="utf-8")

            with self.assertRaisesRegex(BookingError, "unrecognized directory"):
                install_skill(wrong, force=True)

            destination = Path(tmp) / SKILL_NAME
            install_skill(destination)
            (destination / "stale.txt").write_text("stale", encoding="utf-8")
            install_skill(destination, force=True)
            self.assertFalse((destination / "stale.txt").exists())

    def test_force_refuses_the_active_working_tree_without_removing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / SKILL_NAME
            install_skill(destination)
            sentinel = destination / "user-file.txt"
            sentinel.write_text("keep", encoding="utf-8")
            original = Path.cwd()
            os.chdir(destination / "references")
            try:
                with self.assertRaisesRegex(BookingError, "current working directory"):
                    install_skill(destination, force=True)
            finally:
                os.chdir(original)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_force_rejects_a_symbolic_link_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            install_skill(target)
            sentinel = target / "user-file.txt"
            sentinel.write_text("keep", encoding="utf-8")
            destination = root / SKILL_NAME
            destination.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(BookingError, "unrecognized directory"):
                install_skill(destination, force=True)

            self.assertTrue(destination.is_symlink())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_failed_force_install_restores_the_previous_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / SKILL_NAME
            install_skill(destination)
            sentinel = destination / "user-file.txt"
            sentinel.write_text("keep", encoding="utf-8")
            real_replace = os.replace
            calls = 0

            def fail_new_skill(source, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated replacement failure")
                return real_replace(source, target)

            with mock.patch("bk.skill.os.replace", side_effect=fail_new_skill):
                with self.assertRaisesRegex(OSError, "simulated replacement failure"):
                    install_skill(destination, force=True)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertEqual(list(destination.parent.glob(".gpubk.*")), [])


if __name__ == "__main__":
    unittest.main()
