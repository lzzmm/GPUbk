import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from bk.timeparse import to_iso
from bk.usage_cli import _print_payload


class UsageCliTests(unittest.TestCase):
    def test_new_monitor_explains_first_rollup_delay(self):
        now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        payload = {
            "kind": "usage-users",
            "collector": {
                "state": "running",
                "started_at": to_iso(now - timedelta(seconds=12)),
                "age_seconds": 1,
                "rollup_seconds": 60,
            },
            "query": {"start_at": to_iso(now - timedelta(hours=24)), "end_at": to_iso(now)},
            "coverage": {
                "record_count": 0,
                "first_sample_at": None,
                "last_sample_at": None,
                "continuous": False,
            },
            "users": [],
            "warnings": [],
            "truncated": False,
        }

        output = io.StringIO()
        with mock.patch("bk.usage_cli.utc_now", return_value=now), redirect_stdout(output):
            _print_payload(payload, personal=True)

        text = output.getvalue()
        self.assertIn("monitor: healthy | up 12s | sampled 1s ago", text)
        self.assertIn("No finalized usage yet.", text)
        self.assertIn("first summary should appear in about 48s", text)

    def test_human_output_surfaces_truncation_and_storage_warnings(self):
        payload = {
            "kind": "usage-samples",
            "collector": {"state": "not-seen"},
            "records": [],
            "warnings": ["skipped malformed usage record"],
            "truncated": True,
        }

        output = io.StringIO()
        with redirect_stdout(output):
            _print_payload(payload)

        text = output.getvalue()
        self.assertIn("result limit reached", text)
        self.assertIn("warning: skipped malformed usage record", text)

    def test_empty_summary_explains_numeric_uid_mismatch(self):
        payload = {
            "kind": "usage-users",
            "collector": {"state": "running", "rollup_seconds": 60},
            "coverage": {
                "record_count": 0,
                "first_sample_at": None,
                "last_sample_at": None,
                "continuous": False,
                "store_has_samples": True,
                "matching_process_event": False,
            },
            "query": {},
            "users": [],
        }

        output = io.StringIO()
        with mock.patch("bk.usage_cli.os.getuid", return_value=1003), redirect_stdout(output):
            _print_payload(payload, personal=True)

        text = output.getvalue()
        self.assertIn("samples exist, but none match your current numeric UID", text)
        self.assertIn("Current UID: 1003", text)


if __name__ == "__main__":
    unittest.main()
