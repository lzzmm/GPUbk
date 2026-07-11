import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bk.config import Config
from bk.mcp_server import BkMcpBackend, _read_tail
from bk.models import Actor, BookingError, BookingRequest
from bk.scheduler import add_booking
from bk.storage import LedgerStore


class McpBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        self.config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=Path(self.tmp.name) / "private-jobs",
        )
        self.store = LedgerStore(self.data_dir)
        self.backend = BkMcpBackend(self.config, self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_context_and_recommendation_use_stable_agent_schema(self):
        context = self.backend.context()
        recommendation = self.backend.recommend(1, "30m")

        self.assertEqual(context["schema_version"], "bk.agent.v1")
        self.assertEqual(context["actor"]["uid"], os.getuid())
        self.assertTrue(recommendation["available"])

    def test_booking_requires_operation_id_and_retries_are_idempotent(self):
        with self.assertRaisesRegex(BookingError, "operation_id is required"):
            self.backend.book(1, "30m", "")

        first = self.backend.book(1, "30m", "mcp-request-1")
        second = self.backend.book(1, "30m", "mcp-request-1")

        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "exists")
        self.assertEqual(first["reservation"]["id"], second["reservation"]["id"])
        self.assertEqual(len(self.store.load()["reservations"]), 1)

    def test_command_arguments_remain_private_when_submitted_through_mcp(self):
        secret = "mcp-secret-token"

        result = self.backend.book(
            1,
            "30m",
            "mcp-job-1",
            command=["python", "-c", f"print({secret!r})"],
            working_directory=self.tmp.name,
        )

        self.assertEqual(result["status"], "created")
        self.assertNotIn(secret, self.store.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(result["reservation"]["job"]["summary"], "python -c (+1 args)")

    def test_cancel_tool_cannot_target_another_uid(self):
        other = add_booking(
            self.store,
            self.config,
            BookingRequest(
                actor=Actor(os.getuid() + 1, "other"),
                count=1,
                duration_seconds=1800,
                start_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            ),
        ).reservation

        with self.assertRaisesRegex(BookingError, "not found for current UID"):
            self.backend.cancel(other["id"])

    def test_job_log_tail_is_unicode_safe_and_bounded(self):
        path = Path(self.tmp.name) / "unicode.log"
        path.write_text("prefix-" + "测" * 100 + "-end", encoding="utf-8")

        result = _read_tail(path, 12)

        self.assertEqual(result, "测" * 8 + "-end")
        self.assertEqual(len(result), 12)

    def test_job_log_tail_rejects_symbolic_link(self):
        target = Path(self.tmp.name) / "target.log"
        link = Path(self.tmp.name) / "link.log"
        target.write_text("private", encoding="utf-8")
        link.symlink_to(target)

        with self.assertRaises(OSError):
            _read_tail(link, 32)


if __name__ == "__main__":
    unittest.main()
