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
    mode: str = "shared",
) -> dict:
    return {
        "id": reservation_id,
        "uid": uid,
        "username": f"user-{uid}",
        "mode": mode,
        "gpus": list(gpus),
        "start_at": iso(start),
        "end_at": iso(end),
        "status": status,
    }


class LoginNoticeTests(unittest.TestCase):
    def test_only_critical_global_announcements_appear_at_login(self):
        now = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
        base = {
            "created_at": iso(now - timedelta(minutes=1)),
            "starts_at": iso(now - timedelta(minutes=1)),
            "expires_at": iso(now + timedelta(hours=1)),
            "actor_uid": 0,
            "actor_username": "root",
        }
        ledger = {
            "reservations": [],
            "announcements": [
                {**base, "id": "info", "level": "info", "message": "FYI"},
                {
                    **base,
                    "id": "critical",
                    "level": "critical",
                    "message": "Power maintenance",
                },
            ],
        }

        summary = build_login_summary(
            ledger, 1001, now=now, within_seconds=24 * 60 * 60
        )
        rendered = render_login_summary(summary, color=False)

        self.assertEqual([item["id"] for item in summary["announcements"]], ["critical"])
        self.assertIn("CRITICAL Power maintenance", rendered)
        self.assertNotIn("FYI", rendered)

    def test_multiline_announcement_wraps_to_eighty_display_columns(self):
        now = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
        summary = {
            "generated_at": iso(now),
            "active": [],
            "upcoming": [],
            "overdue": [],
            "notifications": [],
            "exclusive_blocks": [],
            "worker": None,
            "announcements": [
                {
                    "message": "English heading\n" + "预约制度正式启用。" * 12,
                }
            ],
        }

        rendered = render_login_summary(summary, color=False)

        self.assertIn("CRITICAL English heading\n", rendered)
        for line in rendered.splitlines():
            display_width = sum(
                0
                if __import__("unicodedata").combining(char)
                else 2
                if __import__("unicodedata").east_asian_width(char) in {"W", "F"}
                else 1
                for char in line
            )
            self.assertLessEqual(display_width, 80)

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

    def test_login_warns_about_another_users_active_exclusive_gpu(self):
        now = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
        summary = build_login_summary(
            {
                "reservations": [
                    reservation(
                        "exclusive-booking",
                        1002,
                        now - timedelta(minutes=5),
                        now + timedelta(minutes=55),
                        gpus=(2, 3),
                        mode="exclusive",
                    )
                ]
            },
            1001,
            now=now,
            within_seconds=86400,
        )

        rendered = render_login_summary(summary)
        self.assertEqual(len(summary["exclusive_blocks"]), 1)
        self.assertIn("2 GPUs exclusive", rendered)
        self.assertIn("AVOID GPU 2,3", rendered)
        self.assertIn("exclusive to user-1002", rendered)
        self.assertIn("use `bk g`", rendered)

    def test_login_shows_only_the_nearest_future_exclusive_detail(self):
        now = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
        ledger = {
            "reservations": [
                reservation(
                    "first",
                    1002,
                    now + timedelta(hours=1),
                    now + timedelta(hours=2),
                    gpus=(4,),
                    mode="exclusive",
                ),
                reservation(
                    "second",
                    1003,
                    now + timedelta(hours=3),
                    now + timedelta(hours=4),
                    gpus=(5,),
                    mode="exclusive",
                ),
            ]
        }

        rendered = render_login_summary(
            build_login_summary(ledger, 1001, now=now, within_seconds=86400)
        )

        self.assertIn("2 upcoming exclusives", rendered)
        self.assertIn("SOON  GPU 4", rendered)
        self.assertIn("(+1 later;", rendered)
        self.assertIn("run `bk tl`)", rendered)
        self.assertNotIn("SOON  GPU 5", rendered)

    def test_scheduled_job_login_notice_explains_worker_and_logout_risk(self):
        now = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
        summary = build_login_summary(
            {"reservations": [reservation("job", 1001, now, now + timedelta(hours=1))]},
            1001,
            now=now,
            within_seconds=86400,
            worker={
                "state": "stopped",
                "running": False,
                "persistence": {"state": "disabled", "logout_safe": False},
            },
        )

        rendered = render_login_summary(summary)
        self.assertIn("AUTO-RUN worker is not running", rendered)
        self.assertIn("tmux", rendered)
        self.assertIn("may stop after logout", rendered)
        self.assertIn("bk info", rendered)

    def test_recent_administrator_cancellation_is_visible_at_login(self):
        now = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
        cancelled = reservation(
            "cancelled-booking",
            1001,
            now + timedelta(hours=1),
            now + timedelta(hours=2),
            status="cancelled",
        )
        cancelled["notifications"] = [
            {
                "id": "notice-id",
                "type": "reservation-admin-cancelled",
                "created_at": iso(now - timedelta(minutes=1)),
                "actor_uid": 0,
                "actor_username": "root",
                "reason": "maintenance",
                "message": "Reservation cancelled by administrator: maintenance",
            }
        ]

        summary = build_login_summary(
            {"reservations": [cancelled]},
            1001,
            now=now,
            within_seconds=86400,
        )
        rendered = render_login_summary(summary)

        self.assertEqual(len(summary["notifications"]), 1)
        self.assertIn("1 notice", rendered)
        self.assertIn("maintenance", rendered)

    def test_expired_reservation_with_reliable_unreserved_process_warns(self):
        now = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
        ledger = {
            "reservations": [
                reservation(
                    "expired-booking",
                    1001,
                    now - timedelta(hours=1),
                    now - timedelta(minutes=5),
                    status="expired",
                    gpus=(2,),
                )
            ]
        }
        summary = build_login_summary(
            ledger,
            1001,
            now=now,
            within_seconds=86400,
            process_state={
                "g2:p123": {
                    "gpu": 2,
                    "pid": 123,
                    "uid": 1001,
                    "status": "unreserved",
                }
            },
            reliable_gpus=(2,),
        )

        self.assertEqual(summary["overdue"][0]["pids"], [123])
        plain = render_login_summary(summary)
        colored = render_login_summary(summary, color=True)
        self.assertIn("ALERT GPU 2", plain)
        self.assertIn("reservation expire", plain)
        self.assertIn("\x1b[1;31m", colored)

    def test_never_reserved_process_warns_its_owner_at_login(self):
        now = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
        summary = build_login_summary(
            {"reservations": []},
            1001,
            now=now,
            within_seconds=86400,
            process_state={
                "g2:p123": {
                    "gpu": 2,
                    "pid": 123,
                    "uid": 1001,
                    "status": "unreserved",
                },
                "g2:p456": {
                    "gpu": 2,
                    "pid": 456,
                    "uid": 1002,
                    "status": "unreserved",
                },
            },
            reliable_gpus=(2,),
        )

        self.assertEqual(summary["unreserved"], [{"gpu": 2, "pids": [123]}])
        rendered = render_login_summary(summary)
        self.assertIn("1 unreserved GPU", rendered)
        self.assertIn("WARNING GPU 2 has your unreserved PID 123", rendered)
        self.assertIn("`bk a`", rendered)

    def test_unreserved_login_warning_requires_reliable_attribution(self):
        now = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
        summary = build_login_summary(
            {"reservations": []},
            1001,
            now=now,
            within_seconds=86400,
            process_state={
                "g2:p123": {
                    "gpu": 2,
                    "pid": 123,
                    "uid": 1001,
                    "status": "unreserved",
                }
            },
            reliable_gpus=(),
        )

        self.assertEqual(summary["unreserved"], [])
        self.assertEqual(render_login_summary(summary), "")

    def test_overdue_warning_requires_reliable_gpu_and_no_current_booking(self):
        now = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
        expired = reservation(
            "expired-booking",
            1001,
            now - timedelta(hours=1),
            now - timedelta(minutes=5),
            status="expired",
            gpus=(0,),
        )
        process_state = {
            "g0:p123": {
                "gpu": 0,
                "pid": 123,
                "uid": 1001,
                "status": "unreserved",
            }
        }
        unreliable = build_login_summary(
            {"reservations": [expired]},
            1001,
            now=now,
            within_seconds=86400,
            process_state=process_state,
            reliable_gpus=(),
        )
        active = build_login_summary(
            {
                "reservations": [
                    expired,
                    reservation(
                        "current-booking",
                        1001,
                        now - timedelta(minutes=1),
                        now + timedelta(minutes=30),
                        gpus=(0,),
                    ),
                ]
            },
            1001,
            now=now,
            within_seconds=86400,
            process_state=process_state,
            reliable_gpus=(0,),
        )

        self.assertEqual(unreliable["overdue"], [])
        self.assertEqual(active["overdue"], [])


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
