import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bk.announcements import (
    active_announcements,
    archive_announcement,
    edit_announcement,
    publish_announcement,
    remove_announcement,
)
from bk.config import Config
from bk.models import Actor, BookingError
from bk.storage import LedgerStore


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.tmp.name))
        self.store = LedgerStore(self.config.data_dir)
        self.admin = Actor(uid=0, username="root")

    def tearDown(self):
        self.tmp.cleanup()

    def test_publish_list_expire_and_archive(self):
        item = publish_announcement(
            self.store,
            self.config,
            self.admin,
            "Cooling maintenance tonight",
            "warning",
            3600,
        )
        ledger = self.store.load()
        self.assertEqual(active_announcements(ledger)[0]["id"], item["id"])
        after_expiry = datetime.now(timezone.utc) + timedelta(hours=2)
        self.assertEqual(active_announcements(ledger, now=after_expiry), [])

        edited = edit_announcement(
            self.store,
            self.config,
            self.admin,
            item["id"][:8],
            message="Updated cooling maintenance",
            level="critical",
            expires_in_seconds=7200,
        )
        self.assertEqual(edited["message"], "Updated cooling maintenance")
        self.assertEqual(edited["level"], "critical")

        archived = archive_announcement(
            self.store, self.config, self.admin, item["id"][:8]
        )
        self.assertEqual(archived["id"], item["id"])
        self.assertEqual(archived["archived_by_uid"], 0)
        retained = self.store.load().get("announcements")
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["message"], "Updated cooling maintenance")
        self.assertEqual(active_announcements({"announcements": retained}), [])

        repeated = archive_announcement(
            self.store, self.config, self.admin, item["id"][:8]
        )
        self.assertEqual(repeated["archived_at"], archived["archived_at"])
        with self.assertRaisesRegex(BookingError, "cannot be edited"):
            edit_announcement(
                self.store,
                self.config,
                self.admin,
                item["id"][:8],
                message="Must not change archived history",
            )

    def test_ordinary_user_cannot_publish_or_remove(self):
        user = Actor(uid=1001, username="alice")
        with self.assertRaisesRegex(BookingError, "administrator"):
            publish_announcement(
                self.store, self.config, user, "fake maintenance", "critical", 3600
            )
        with self.assertRaisesRegex(BookingError, "administrator"):
            remove_announcement(self.store, self.config, user, "missing")

    def test_service_owner_is_not_implicitly_an_announcement_administrator(self):
        config = Config(
            data_dir=Path(self.tmp.name),
            broker_uid=1001,
            broker_socket=Path(self.tmp.name) / "broker.sock",
            file_mode=0o644,
            dir_mode=0o755,
            broker_socket_mode=0o666,
        )
        with self.assertRaisesRegex(BookingError, "requires sudo"):
            publish_announcement(
                self.store,
                config,
                Actor(uid=1001, username="service-owner"),
                "unauthorized notice",
                "warning",
                3600,
            )

    def test_invalid_level_and_expiry_are_rejected(self):
        with self.assertRaisesRegex(BookingError, "level"):
            publish_announcement(
                self.store, self.config, self.admin, "message", "red", 3600
            )
        with self.assertRaisesRegex(BookingError, "expiry"):
            publish_announcement(
                self.store, self.config, self.admin, "message", "info", 10
            )

    def test_scheduled_announcement_activates_only_inside_its_window(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        starts = now + timedelta(hours=2)
        item = publish_announcement(
            self.store,
            self.config,
            self.admin,
            "Planned maintenance",
            "warning",
            3600,
            starts_at=starts,
        )
        ledger = self.store.load()

        self.assertEqual(active_announcements(ledger, now=now), [])
        self.assertEqual(
            active_announcements(ledger, now=starts + timedelta(minutes=1))[0]["id"],
            item["id"],
        )
        self.assertEqual(
            active_announcements(ledger, now=starts + timedelta(hours=1)), []
        )

        moved_start = starts + timedelta(hours=1)
        moved_end = moved_start + timedelta(hours=2)
        edited = edit_announcement(
            self.store,
            self.config,
            self.admin,
            item["id"][:8],
            starts_at=moved_start,
            expires_at=moved_end,
        )
        self.assertEqual(
            edited["starts_at"], moved_start.isoformat().replace("+00:00", "Z")
        )
        self.assertEqual(
            edited["expires_at"], moved_end.isoformat().replace("+00:00", "Z")
        )

    def test_edit_rejects_deadline_before_scheduled_start(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        item = publish_announcement(
            self.store,
            self.config,
            self.admin,
            "Planned maintenance",
            "warning",
            3600,
            starts_at=now + timedelta(hours=2),
        )
        with self.assertRaisesRegex(BookingError, "after its start"):
            edit_announcement(
                self.store,
                self.config,
                self.admin,
                item["id"][:8],
                expires_at=now + timedelta(hours=1),
            )
