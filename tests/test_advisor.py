import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bk.advisor import build_gpu_advice
from bk.config import Config
from bk.gpu import GpuProcessSnapshot, GpuSnapshot
from bk.timeparse import to_iso


class GpuAdvisorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.config = Config(data_dir=Path("/tmp/bk-advisor-test"), gpu_count=3)

    def test_live_busy_gpu_is_ranked_after_idle_gpu(self):
        snapshots = [
            GpuSnapshot(
                0,
                "sim",
                memory_used_mb=12000,
                memory_total_mb=24000,
                utilization_percent=80,
                processes=(GpuProcessSnapshot(42, 1001, "alice", "python train.py"),),
                source="simulation",
            ),
            GpuSnapshot(1, "sim", memory_total_mb=24000, utilization_percent=2, source="simulation"),
            GpuSnapshot(2, "sim", memory_total_mb=24000, utilization_percent=4, source="simulation"),
        ]

        advice = build_gpu_advice(self.config, snapshots=snapshots, history={}, at=self.now)

        self.assertEqual(advice.order[0], 1)
        self.assertEqual(advice.order[-1], 0)
        self.assertEqual(advice.live_states[0].status, "busy")
        self.assertIn("alice", advice.live_states[0].reason)

    def test_recent_history_breaks_tie_between_currently_idle_gpus(self):
        snapshots = [
            GpuSnapshot(0, "sim", memory_total_mb=24000, utilization_percent=0, source="simulation"),
            GpuSnapshot(1, "sim", memory_total_mb=24000, utilization_percent=0, source="simulation"),
            GpuSnapshot(2, "sim", memory_total_mb=24000, utilization_percent=0, source="simulation"),
        ]
        history = {
            "version": 1,
            "gpus": {
                "0": [
                    {
                        "window_end": to_iso(self.now - timedelta(minutes=1)),
                        "known_samples": 30,
                        "avg_utilization_percent": 90,
                        "avg_memory_percent": 80,
                        "busy_fraction": 1,
                    }
                ],
                "1": [
                    {
                        "window_end": to_iso(self.now - timedelta(minutes=1)),
                        "known_samples": 30,
                        "avg_utilization_percent": 5,
                        "avg_memory_percent": 4,
                        "busy_fraction": 0,
                    }
                ],
            },
        }

        advice = build_gpu_advice(self.config, snapshots=snapshots, history=history, at=self.now)

        self.assertLess(advice.scores[1], advice.scores[0])
        self.assertEqual(advice.order[:2], [2, 1])
        self.assertGreater(advice.historical_loads[0].predicted_percent, 80)


if __name__ == "__main__":
    unittest.main()
