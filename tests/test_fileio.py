import os
import tempfile
import unittest
from pathlib import Path

from bk.fileio import ensure_directory, open_existing_regular, open_or_create_regular


class SecureFileIoTests(unittest.TestCase):
    def test_existing_read_rejects_symbolic_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            link = root / "link"
            target.write_text("secret", encoding="utf-8")
            link.symlink_to(target)

            with self.assertRaises(OSError):
                open_existing_regular(link)

    def test_existing_write_rejects_symbolic_link_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            link = root / "link"
            target.write_text("keep", encoding="utf-8")
            link.symlink_to(target)

            with self.assertRaises(OSError):
                open_or_create_regular(link, os.O_WRONLY | os.O_APPEND, 0o600)

            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_fifo_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "fifo"
            os.mkfifo(fifo)

            with self.assertRaisesRegex(OSError, "non-regular"):
                open_existing_regular(fifo)

    def test_directory_helper_rejects_symbolic_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            link = root / "link"
            target.mkdir()
            link.symlink_to(target, target_is_directory=True)

            with self.assertRaises(NotADirectoryError):
                ensure_directory(link, 0o700)


if __name__ == "__main__":
    unittest.main()
