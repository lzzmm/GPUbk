import tempfile
import unittest
from pathlib import Path

from bk.models import BookingError
from bk.skill import SKILL_NAME, install_skill, skill_text


class BundledSkillTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
