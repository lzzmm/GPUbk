import tempfile
import unittest
from pathlib import Path

from bk.models import BookingError
from bk.systemd import install_user_unit, unit_text


class BundledSystemdTests(unittest.TestCase):
    def test_units_are_bundled_and_remain_user_scoped(self):
        python = Path("/opt/bk venv/bin/python")
        worker = unit_text("worker", python)
        monitor = unit_text("monitor", python)

        self.assertIn('ExecStart="/opt/bk venv/bin/python" -m bk worker', worker)
        self.assertIn('ExecStart="/opt/bk venv/bin/python" -m bk monitor', monitor)
        self.assertNotIn("@PYTHON_EXECUTABLE@", worker)
        self.assertNotIn("User=root", worker)

    def test_unit_escapes_systemd_specifiers_and_environment_markers(self):
        worker = unit_text("worker", Path("/opt/percent%/$name/python"))

        self.assertIn('ExecStart="/opt/percent%%/$$name/python" -m bk worker', worker)

    def test_unit_rejects_relative_interpreter_path(self):
        with self.assertRaisesRegex(BookingError, "absolute path"):
            unit_text("worker", Path("python3"))

    def test_install_never_enables_service_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            path = install_user_unit("worker", target)

            self.assertEqual(path, target / "bk-worker.service")
            self.assertTrue(path.is_file())
            with self.assertRaisesRegex(BookingError, "already exists"):
                install_user_unit("worker", target)
            install_user_unit("worker", target, force=True)


if __name__ == "__main__":
    unittest.main()
