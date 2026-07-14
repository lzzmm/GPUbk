import contextlib
import io
import unittest
from unittest import mock

import bk.live_usage_demo as demo
from bk.usage_cli import run_usage_cli


class LiveUsageDemoTests(unittest.TestCase):
    def test_reservation_covers_current_slot_and_workload(self):
        self.assertEqual(demo.reservation_minutes(5, 65), 10)
        self.assertEqual(demo.reservation_minutes(1, 65), 3)

    def test_selected_idle_gpu_requires_one_idle_gpu(self):
        payload = {
            "allocation": {
                "selected": [{"gpu": 6, "live_status": "idle", "live_reason": ""}]
            }
        }
        self.assertEqual(demo.selected_idle_gpu(payload), 6)

        payload["allocation"]["selected"][0]["live_status"] = "busy"
        with self.assertRaisesRegex(demo.DemoError, "not idle"):
            demo.selected_idle_gpu(payload)

    def test_usage_demo_is_routed_without_opening_the_usage_store(self):
        with mock.patch("bk.live_usage_demo.main", return_value=7) as run:
            self.assertEqual(
                run_usage_cli(
                    ["demo", "--seconds", "20"], mock.sentinel.unused_config
                ),
                7,
            )
        run.assert_called_once_with(["--seconds", "20"])

    def test_main_reports_demo_errors_without_a_traceback(self):
        stderr = io.StringIO()
        with (
            mock.patch("bk.live_usage_demo._run", side_effect=demo.DemoError("not ready")),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(demo.main([]), 2)
        self.assertEqual(stderr.getvalue(), "usage demo: not ready\n")

    def test_cancel_failure_is_visible_and_returns_false(self):
        stderr = io.StringIO()
        with (
            mock.patch("bk.live_usage_demo.subprocess.run") as run,
            contextlib.redirect_stderr(stderr),
        ):
            run.return_value.returncode = 1
            self.assertFalse(demo.cancel("bk", "12345678-abcd"))
        self.assertIn("bk del 12345678", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
