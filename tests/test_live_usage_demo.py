import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "tools" / "live_usage_demo.py"
SPEC = importlib.util.spec_from_file_location("live_usage_demo", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


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


if __name__ == "__main__":
    unittest.main()
