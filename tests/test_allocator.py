import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bk.advisor import build_gpu_advice
from bk.allocator import ALLOCATOR_SCHEMA_VERSION, apply_external_allocator
from bk.config import Config
from bk.gpu import GpuSnapshot
from bk.models import MODE_EXCLUSIVE, MODE_SHARED, Actor, BookingRequest
from bk.scheduler import add_booking, find_earliest_slot
from bk.storage import LedgerStore


def allocator_command(response):
    source = f"import json; print(json.dumps({response!r}))"
    return (sys.executable, "-c", source)


class ExternalAllocatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.store = LedgerStore(self.data_dir)
        self.actor = Actor(1001, "alice")
        self.start = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.snapshots = [
            GpuSnapshot(0, "gpu0", memory_total_mb=24000, source="simulation"),
            GpuSnapshot(1, "gpu1", memory_total_mb=24000, source="simulation"),
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_external_order_blends_into_local_scores(self):
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            allocator_command=allocator_command(
                {
                    "schema_version": ALLOCATOR_SCHEMA_VERSION,
                    "gpu_order": [1, 0],
                    "reason": "spread thermal load",
                }
            ),
            allocator_weight=10,
        )
        advice = build_gpu_advice(config, snapshots=self.snapshots, history={}, at=self.start)

        decision = apply_external_allocator(
            config,
            self.store,
            self.actor,
            advice,
            count=1,
            duration_seconds=1800,
            start_at=self.start,
            mode=MODE_SHARED,
            expected_memory_mb=4096,
        )

        self.assertEqual(decision.source, "external")
        self.assertEqual(decision.order, [1, 0])
        self.assertLess(decision.scores[1], decision.scores[0])
        self.assertEqual(decision.reason, "spread thermal load")

    def test_invalid_external_output_falls_back_without_crashing(self):
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            allocator_command=allocator_command(
                {"schema_version": ALLOCATOR_SCHEMA_VERSION, "gpu_order": [0, 0]}
            ),
        )
        advice = build_gpu_advice(config, snapshots=self.snapshots, history={}, at=self.start)

        decision = apply_external_allocator(
            config,
            self.store,
            self.actor,
            advice,
            count=1,
            duration_seconds=1800,
            start_at=self.start,
            mode=MODE_SHARED,
            expected_memory_mb=None,
        )

        self.assertEqual(decision.source, "builtin-fallback")
        self.assertEqual(decision.order, advice.order)
        self.assertIn("permutation", decision.warning)

    def test_slow_external_allocator_times_out_and_falls_back(self):
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            allocator_command=(sys.executable, "-c", "import time; time.sleep(1)"),
            allocator_timeout_seconds=0.05,
        )
        advice = build_gpu_advice(config, snapshots=self.snapshots, history={}, at=self.start)

        decision = apply_external_allocator(
            config,
            self.store,
            self.actor,
            advice,
            count=1,
            duration_seconds=1800,
            start_at=self.start,
            mode=MODE_SHARED,
            expected_memory_mb=None,
        )

        self.assertEqual(decision.source, "builtin-fallback")
        self.assertIn("timed out", decision.warning)

    def test_oversized_external_output_is_bounded_and_falls_back(self):
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            allocator_command=(sys.executable, "-c", "print('x' * 70000)"),
        )
        advice = build_gpu_advice(config, snapshots=self.snapshots, history={}, at=self.start)

        decision = apply_external_allocator(
            config,
            self.store,
            self.actor,
            advice,
            count=1,
            duration_seconds=1800,
            start_at=self.start,
            mode=MODE_SHARED,
            expected_memory_mb=None,
        )

        self.assertEqual(decision.source, "builtin-fallback")
        self.assertIn("exceeded 64 KiB", decision.warning)

    def test_timeout_kills_allocator_descendants(self):
        marker = self.data_dir / "escaped-child"
        child = f"import time; time.sleep(.4); open({str(marker)!r}, 'w').write('bad')"
        parent = (
            "import subprocess,sys; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}])"
        )
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            allocator_command=(sys.executable, "-c", parent),
            allocator_timeout_seconds=0.05,
        )
        advice = build_gpu_advice(config, snapshots=self.snapshots, history={}, at=self.start)

        decision = apply_external_allocator(
            config,
            self.store,
            self.actor,
            advice,
            count=1,
            duration_seconds=1800,
            start_at=self.start,
            mode=MODE_SHARED,
            expected_memory_mb=None,
        )
        time.sleep(0.5)

        self.assertEqual(decision.source, "builtin-fallback")
        self.assertIn("timed out", decision.warning)
        self.assertFalse(marker.exists())

    def test_successful_allocator_cleans_up_background_descendants(self):
        marker = self.data_dir / "background-child"
        child = f"import time; time.sleep(.4); open({str(marker)!r}, 'w').write('bad')"
        response = {
            "schema_version": ALLOCATOR_SCHEMA_VERSION,
            "gpu_order": [0, 1],
        }
        parent = (
            "import json,subprocess,sys; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            f"print(json.dumps({response!r}))"
        )
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            allocator_command=(sys.executable, "-c", parent),
        )
        advice = build_gpu_advice(config, snapshots=self.snapshots, history={}, at=self.start)

        decision = apply_external_allocator(
            config,
            self.store,
            self.actor,
            advice,
            count=1,
            duration_seconds=1800,
            start_at=self.start,
            mode=MODE_SHARED,
            expected_memory_mb=None,
        )
        time.sleep(0.5)

        self.assertEqual(decision.source, "external")
        self.assertFalse(marker.exists())

    def test_external_preference_cannot_bypass_hard_conflict(self):
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=1,
            allocator_command=allocator_command(
                {"schema_version": ALLOCATOR_SCHEMA_VERSION, "gpu_order": [0, 1]}
            ),
            allocator_weight=100,
        )
        add_booking(
            self.store,
            config,
            BookingRequest(
                actor=Actor(2002, "bob"),
                count=1,
                duration_seconds=1800,
                start_at=self.start,
                mode=MODE_EXCLUSIVE,
                preferred_gpus=[0],
            ),
        )
        advice = build_gpu_advice(config, snapshots=self.snapshots, history={}, at=self.start)
        decision = apply_external_allocator(
            config,
            self.store,
            self.actor,
            advice,
            count=1,
            duration_seconds=1800,
            start_at=self.start,
            mode=MODE_EXCLUSIVE,
            expected_memory_mb=None,
        )

        slot = find_earliest_slot(
            self.store.load(),
            config,
            1,
            self.start,
            timedelta(minutes=30),
            MODE_EXCLUSIVE,
            self.actor.uid,
            gpu_order=decision.order,
            gpu_scores=decision.scores,
        )

        self.assertEqual(slot[1], [1])


if __name__ == "__main__":
    unittest.main()
