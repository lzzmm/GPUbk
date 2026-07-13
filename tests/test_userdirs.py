import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bk.userdirs import xdg_user_directory


class UserDirectoryTests(unittest.TestCase):
    def test_absolute_xdg_directory_is_used_directly(self):
        result = xdg_user_directory(
            "XDG_DATA_HOME",
            ".local/share",
            environment={
                "HOME": "/home/alice",
                "XDG_DATA_HOME": "/srv/alice/data",
            },
        )

        self.assertEqual(result, Path("/srv/alice/data"))

    def test_empty_or_relative_xdg_directory_falls_back_to_absolute_home(self):
        for value in (
            "",
            "relative/data",
            "~/not-expanded",
            "x" * 5000,
        ):
            with self.subTest(value=value):
                result = xdg_user_directory(
                    "XDG_DATA_HOME",
                    ".local/share",
                    environment={"HOME": "/home/alice", "XDG_DATA_HOME": value},
                )

                self.assertEqual(result, Path("/home/alice/.local/share"))

    def test_relative_home_falls_back_to_numeric_accounts_absolute_home(self):
        account = SimpleNamespace(pw_dir="/accounts/1001")
        with mock.patch("bk.userdirs.pwd.getpwuid", return_value=account):
            result = xdg_user_directory(
                "XDG_STATE_HOME",
                ".local/state",
                environment={"HOME": "relative-home", "XDG_STATE_HOME": "relative-state"},
            )

        self.assertEqual(result, Path("/accounts/1001/.local/state"))

    def test_invalid_environment_paths_fail_without_using_the_working_directory(self):
        for value, message in (
            ("/tmp/bad\x00path", "non-empty filesystem path"),
            ("/tmp/\udcff", "valid UTF-8"),
            ("/" + "x" * 4097, "must not exceed"),
        ):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(ValueError, message):
                    xdg_user_directory(
                        "XDG_CONFIG_HOME",
                        ".config",
                        environment={"HOME": "/home/alice", "XDG_CONFIG_HOME": value},
                    )


if __name__ == "__main__":
    unittest.main()
