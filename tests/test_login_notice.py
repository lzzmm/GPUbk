import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bk.login_hook import (
    LOGIN_HOOK_MARKER,
    apply_login_hook_install,
    apply_login_hook_uninstall,
    inspect_login_hook,
    render_login_hook,
)
from bk.login_notice import build_login_summary, render_login_summary
from bk.models import BookingError


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def reservation(
    reservation_id: str,
    uid: int,
    start: datetime,
    end: datetime,
    *,
    status: str = "active",
    gpus=(0,),
) -> dict:
    return {
        "id": reservation_id,
        "uid": uid,
        "username": f"user-{uid}",
        "mode": "shared",
        "gpus": list(gpus),
        "start_at": iso(start),
        "end_at": iso(end),
        "status": status,
    }


class LoginNoticeTests(unittest.TestCase):
    def test_summary_contains_only_this_uids_active_and_near_term_bookings(self):
        now = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
        ledger = {
            "reservations": [
                reservation("active-booking", 1001, now - timedelta(minutes=5), now + timedelta(minutes=55), gpus=(0, 1)),
                reservation("next-booking", 1001, now + timedelta(hours=2), now + timedelta(hours=3), gpus=(2,)),
                reservation("far-booking", 1001, now + timedelta(days=2), now + timedelta(days=2, hours=1)),
                reservation("other-user", 1002, now - timedelta(minutes=5), now + timedelta(hours=1)),
                reservation("cancelled", 1001, now, now + timedelta(hours=1), status="cancelled"),
            ]
        }

        summary = build_login_summary(
            ledger,
            1001,
            now=now,
            within_seconds=24 * 60 * 60,
        )

        self.assertEqual([item["id"] for item in summary["active"]], ["active-booking"])
        self.assertEqual([item["id"] for item in summary["upcoming"]], ["next-booking"])
        rendered = render_login_summary(summary)
        self.assertIn("GPUBK: 1 active, 1 upcoming", rendered)
        self.assertIn("NOW  active", rendered)
        self.assertIn("GPU 0,1", rendered)
        self.assertIn("NEXT next-b", rendered)
        self.assertNotIn("other-user", rendered)

    def test_empty_summary_renders_no_login_noise(self):
        summary = build_login_summary(
            {"reservations": []},
            1001,
            now=datetime(2030, 1, 1, tzinfo=timezone.utc),
            within_seconds=86400,
        )

        self.assertEqual(render_login_summary(summary), "")


class LoginHookTests(unittest.TestCase):
    def test_hook_is_bounded_interactive_and_reversible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "gpubk.sh"
            executable = Path("/opt/gpubk/bin/bk")

            installed = apply_login_hook_install(
                path,
                executable=executable,
                require_root=False,
            )
            text = path.read_text(encoding="utf-8")

            self.assertTrue(installed["changed"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertTrue(text.startswith(LOGIN_HOOK_MARKER))
            self.assertIn("[ -t 1 ]", text)
            self.assertIn("timeout -k 0.2s 1s", text)
            self.assertIn("login --hook 2>/dev/null || :", text)
            self.assertEqual(
                inspect_login_hook(
                    path,
                    executable=executable,
                    expected_owner=os.geteuid(),
                )["status"],
                "installed",
            )

            removed = apply_login_hook_uninstall(path, require_root=False)

            self.assertTrue(removed["changed"])
            self.assertFalse(path.exists())

    def test_install_refuses_to_replace_unknown_or_linked_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unknown = root / "unknown.sh"
            unknown.write_text("echo keep-me\n", encoding="utf-8")
            unknown.chmod(0o644)

            with self.assertRaisesRegex(BookingError, "unmanaged"):
                apply_login_hook_install(unknown, require_root=False)
            self.assertEqual(unknown.read_text(encoding="utf-8"), "echo keep-me\n")

            target = root / "target.sh"
            target.write_bytes(render_login_hook())
            target.chmod(0o644)
            linked = root / "linked.sh"
            linked.symlink_to(target)
            with self.assertRaisesRegex(BookingError, "regular file"):
                apply_login_hook_install(linked, require_root=False)
