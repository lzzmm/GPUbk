import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bk.advisor import build_gpu_advice
from bk.config import Config
from bk.gpu import GpuProcessSnapshot, GpuSnapshot
from bk.models import MODE_EXCLUSIVE, MODE_SHARED, Actor, BookingRequest
from bk.scheduler import add_booking
from bk.service import AGENT_SCHEMA_VERSION, build_agent_context, recommend_booking
from bk.storage import LedgerStore


class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.config = Config(data_dir=self.data_dir, gpu_count=2, max_shared_users=2)
        self.store = LedgerStore(self.data_dir)
        self.actor = Actor(1001, "alice")
        self.start = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.snapshots = [
            GpuSnapshot(
                0,
                "busy",
                memory_used_mb=16000,
                memory_total_mb=24000,
                utilization_percent=80,
                processes=(GpuProcessSnapshot(55, 1002, "bob", "python train.py --token secret"),),
                source="simulation",
            ),
            GpuSnapshot(
                1,
                "idle",
                memory_used_mb=1000,
                memory_total_mb=24000,
                utilization_percent=2,
                source="simulation",
            ),
        ]
        self.advice = build_gpu_advice(self.config, snapshots=self.snapshots, history={}, at=self.start)

    def tearDown(self):
        self.tmp.cleanup()

    def test_context_has_stable_schema_and_no_process_arguments(self):
        context = build_agent_context(
            self.config,
            self.store,
            self.actor,
            at=self.start,
            advice=self.advice,
        )

        self.assertEqual(context["schema_version"], AGENT_SCHEMA_VERSION)
        self.assertEqual(context["policy"]["granularity_minutes"], 5)
        self.assertEqual(context["gpu_advice"]["order"], [1, 0])
        self.assertNotIn("secret", str(context))

    def test_recommendation_is_read_only_and_prefers_live_idle_gpu(self):
        recommendation = recommend_booking(
            self.config,
            self.store,
            self.actor,
            count=1,
            duration_seconds=30 * 60,
            start_at=self.start,
            mode=MODE_SHARED,
            expected_memory_mb=4096,
            allow_queue=False,
            advice=self.advice,
        )

        self.assertTrue(recommendation["available"])
        self.assertEqual(recommendation["recommendation"]["gpus"], [1])
        self.assertEqual(recommendation["recommendation"]["confidence"], "medium")
        self.assertGreater(recommendation["recommendation"]["gpu_details"][0]["memory_free_now_mb"], 20000)
        self.assertEqual(self.store.load()["reservations"], [])

    def test_exact_conflict_returns_nearest_without_writing(self):
        add_booking(
            self.store,
            self.config,
            BookingRequest(
                actor=Actor(1002, "bob"),
                count=2,
                duration_seconds=30 * 60,
                start_at=self.start,
                mode=MODE_EXCLUSIVE,
            ),
        )

        recommendation = recommend_booking(
            self.config,
            self.store,
            self.actor,
            count=1,
            duration_seconds=30 * 60,
            start_at=self.start,
            mode=MODE_SHARED,
            allow_queue=False,
            advice=self.advice,
        )

        self.assertFalse(recommendation["available"])
        self.assertEqual(recommendation["nearest_available"]["start_at"], "2030-01-01T12:30:00Z")
        self.assertEqual(len(self.store.load()["reservations"]), 1)


if __name__ == "__main__":
    unittest.main()
