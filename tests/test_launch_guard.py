import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bk.config import Config
from bk.gpu import GpuProcessSnapshot, GpuSnapshot
from bk.launch_guard import assess_job_launch
from bk.models import MODE_EXCLUSIVE, MODE_SHARED
from bk.timeparse import to_iso


class JobLaunchGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.config = Config(
            data_dir=Path("unused"),
            gpu_count=1,
            max_shared_users=2,
            shared_memory_reserve_mb=512,
        )

    def reservation(
        self,
        uid=1001,
        mode=MODE_SHARED,
        expected_memory_mb=None,
        share_units=None,
    ):
        item = {
            "id": f"booking-{uid}",
            "uid": uid,
            "username": f"user{uid}",
            "gpus": [0],
            "mode": mode,
            "status": "active",
            "start_at": to_iso(self.now - timedelta(minutes=5)),
            "end_at": to_iso(self.now + timedelta(minutes=30)),
        }
        if expected_memory_mb is not None:
            item["expected_memory_mb"] = expected_memory_mb
        if share_units is not None:
            item["share_units"] = share_units
        return item

    def gpu(
        self,
        *,
        used=0,
        processes=(),
        source="nvml",
        utilization=0,
        device_uuid="GPU-00000000-0000-0000-0000-000000000000",
    ):
        return GpuSnapshot(
            0,
            "gpu0",
            memory_used_mb=used,
            memory_total_mb=24000,
            utilization_percent=utilization,
            processes=tuple(processes),
            source=source,
            device_uuid=device_uuid,
        )

    def test_unreserved_process_blocks_shared_job(self):
        target = self.reservation()
        process = GpuProcessSnapshot(41, 2002, "other", "python rogue.py", 4096, 80)

        decision = assess_job_launch(
            self.config,
            target,
            [self.gpu(used=4096, processes=(process,))],
            [target],
            at=self.now,
        )

        self.assertFalse(decision.ready)
        self.assertIn("unreserved process", decision.reason)

    def test_authorized_shared_process_is_allowed_with_memory_headroom(self):
        target = self.reservation(expected_memory_mb=4096)
        other = self.reservation(uid=2002, expected_memory_mb=4096)
        process = GpuProcessSnapshot(42, 2002, "other", "python train.py", 4096, 80)

        decision = assess_job_launch(
            self.config,
            target,
            [self.gpu(used=4096, processes=(process,))],
            [target, other],
            at=self.now,
        )

        self.assertTrue(decision.ready, decision.reason)

    def test_exclusive_job_waits_for_any_non_system_process(self):
        target = self.reservation(mode=MODE_EXCLUSIVE)
        process = GpuProcessSnapshot(43, 1001, "user1001", "python manual.py", 2048, 50)

        decision = assess_job_launch(
            self.config,
            target,
            [self.gpu(used=2048, processes=(process,))],
            [target],
            at=self.now,
        )

        self.assertFalse(decision.ready)
        self.assertIn("exclusive GPU 0 still has process", decision.reason)

    def test_expected_memory_is_checked_against_physical_headroom(self):
        target = self.reservation(expected_memory_mb=8192)

        decision = assess_job_launch(
            self.config,
            target,
            [self.gpu(used=18000)],
            [target],
            at=self.now,
        )

        self.assertFalse(decision.ready)
        self.assertIn("8192MiB is required", decision.reason)

    def test_missing_live_telemetry_fails_closed(self):
        target = self.reservation(expected_memory_mb=4096)

        decision = assess_job_launch(
            self.config,
            target,
            [self.gpu(source="none")],
            [target],
            at=self.now,
        )

        self.assertFalse(decision.ready)
        self.assertIn("telemetry is unavailable", decision.reason)

    def test_device_metrics_without_process_telemetry_fail_closed(self):
        target = self.reservation(expected_memory_mb=4096)

        decision = assess_job_launch(
            self.config,
            target,
            [self.gpu(source="nvidia-smi")],
            [target],
            at=self.now,
        )

        self.assertFalse(decision.ready)
        self.assertIn("process telemetry is unavailable", decision.reason)

    def test_nvml_without_stable_identifier_fails_closed(self):
        target = self.reservation(expected_memory_mb=4096)

        decision = assess_job_launch(
            self.config,
            target,
            [self.gpu(device_uuid="")],
            [target],
            at=self.now,
        )

        self.assertFalse(decision.ready)
        self.assertIn("stable identifier is unavailable", decision.reason)

    def test_omitted_memory_uses_weighted_share_at_launch_time(self):
        config = Config(
            data_dir=Path("unused"),
            gpu_count=1,
            max_shared_users=4,
            shared_memory_reserve_mb=0,
        )
        target = self.reservation(share_units=3)

        decision = assess_job_launch(
            config,
            target,
            [self.gpu(used=7000)],
            [target],
            at=self.now,
        )

        self.assertFalse(decision.ready)
        self.assertIn("18000MiB is required", decision.reason)

    def test_ready_decision_carries_the_exact_checked_gpu_identifier(self):
        target = self.reservation(expected_memory_mb=4096)

        decision = assess_job_launch(
            self.config,
            target,
            [
                self.gpu(
                    device_uuid="GPU-00000000-0000-0000-0000-000000000123"
                )
            ],
            [target],
            at=self.now,
        )

        self.assertTrue(decision.ready, decision.reason)
        self.assertEqual(
            decision.cuda_visible_devices,
            ("GPU-00000000-0000-0000-0000-000000000123",),
        )


if __name__ == "__main__":
    unittest.main()
