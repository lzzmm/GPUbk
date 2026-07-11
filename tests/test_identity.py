import os
import unittest
from types import SimpleNamespace
from unittest import mock

from bk.identity import current_actor


class CurrentActorTests(unittest.TestCase):
    def test_username_comes_from_numeric_uid_not_environment(self):
        with (
            mock.patch("bk.identity.os.getuid", return_value=1234),
            mock.patch("bk.identity.pwd.getpwuid", return_value=SimpleNamespace(pw_name="trusted-name")),
            mock.patch.dict(os.environ, {"USER": "spoofed", "LOGNAME": "spoofed"}),
        ):
            actor = current_actor()

        self.assertEqual(actor.uid, 1234)
        self.assertEqual(actor.username, "trusted-name")

    def test_unknown_uid_uses_stable_numeric_display_name(self):
        with (
            mock.patch("bk.identity.os.getuid", return_value=9876),
            mock.patch("bk.identity.pwd.getpwuid", side_effect=KeyError),
        ):
            actor = current_actor()

        self.assertEqual(actor.username, "9876")


if __name__ == "__main__":
    unittest.main()
