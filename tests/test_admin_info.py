import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bk.admin_info import administrator_display_lines, administrator_info
from bk.config import Config


class AdministratorInfoTests(unittest.TestCase):
    def test_reads_adduser_gecos_fields_for_the_service_account(self):
        config = Config(
            data_dir=Path("/tmp/gpubk-admin-info"),
            monitor_uid=1003,
        )
        account = SimpleNamespace(
            pw_uid=1003,
            pw_name="chenyuhan",
            pw_gecos="Chen Yuhan,Room 5090,+86 123,020-123,cortex@example.com",
        )

        with mock.patch("bk.admin_info.pwd.getpwuid", return_value=account) as lookup:
            info = administrator_info(config)

        lookup.assert_called_once_with(1003)
        self.assertEqual(info.username, "chenyuhan")
        self.assertEqual(info.full_name, "Chen Yuhan")
        self.assertEqual(info.room, "Room 5090")
        self.assertEqual(info.work_phone, "+86 123")
        self.assertEqual(info.home_phone, "020-123")
        self.assertEqual(info.other, "cortex@example.com")
        self.assertEqual(info.as_dict()["source"], "linux-account-gecos")
        self.assertIn("work +86 123", administrator_display_lines(info)[-1])

    def test_broker_owner_wins_and_terminal_controls_are_removed(self):
        config = Config(
            data_dir=Path("/tmp/gpubk-admin-info"),
            file_mode=0o644,
            dir_mode=0o755,
            broker_socket=Path("/run/gpubk/broker.sock"),
            broker_uid=2001,
            broker_socket_mode=0o666,
            monitor_uid=2002,
        )
        account = SimpleNamespace(
            pw_name="admin\x1b[31m",
            pw_gecos="Admin\nName,,,,contact\x07here",
        )

        with mock.patch("bk.admin_info.pwd.getpwuid", return_value=account):
            info = administrator_info(config)

        self.assertEqual(info.uid, 2001)
        self.assertNotIn("\x1b", info.username)
        self.assertEqual(info.full_name, "Admin Name")
        self.assertEqual(info.other, "contact here")

    def test_unresolved_account_returns_a_stable_numeric_identity(self):
        config = Config(data_dir=Path("/tmp/gpubk-admin-info"))
        with (
            mock.patch("bk.admin_info.os.getuid", return_value=3456),
            mock.patch("bk.admin_info.pwd.getpwuid", side_effect=KeyError),
        ):
            info = administrator_info(config)

        self.assertEqual(info.username, "3456")
        self.assertFalse(info.account_resolved)
        self.assertIn("lookup is currently unavailable", administrator_display_lines(info)[-1])


if __name__ == "__main__":
    unittest.main()
