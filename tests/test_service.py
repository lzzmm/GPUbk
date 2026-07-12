import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bk.advisor import build_gpu_advice
from bk.collector_status import collector_document
from bk.config import Config
from bk.gpu import GpuProcessSnapshot, GpuSnapshot
from bk.joblogs import acquire_job_worker_lease
from bk.models import MODE_EXCLUSIVE, MODE_SHARED, Actor, BookingError, BookingRequest
from bk.scheduler import add_booking
from bk.service import (
    AGENT_SCHEMA_VERSION,
    booking_result_payload,
    build_agent_context,
    recommend_booking,
    submit_booking,
    submit_cancellation,
    submit_edit,
)
from bk.storage import LedgerStore
from bk.usage_store import UsageAuditStore
from bk.worker import job_spec_path


class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.config = Config(data_dir=self.data_dir, gpu_count=2, max_shared_users=2)
        self.store = LedgerStore(self.data_dir)
        self.actor = Actor(1002 if os.getuid() == 1001 else 1001, "alice")
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
                temperature_c=47,
                source="simulation",
            ),
        ]
        self.advice = build_gpu_advice(self.config, snapshots=self.snapshots, history={}, at=self.start)

    def tearDown(self):
        self.tmp.cleanup()

    def test_context_has_stable_schema_and_no_process_arguments(self):
        self.assertNotEqual(self.actor.uid, os.getuid())
        context = build_agent_context(
            self.config,
            self.store,
            self.actor,
            at=self.start,
            advice=self.advice,
        )

        self.assertEqual(context["schema_version"], AGENT_SCHEMA_VERSION)
        self.assertEqual(context["policy"]["granularity_minutes"], 5)
        self.assertTrue(context["policy"]["worker_live_guard"])
        self.assertEqual(context["gpu_advice"]["order"], [1, 0])
        self.assertEqual(context["gpu_advice"]["gpus"][1]["name"], "idle")
        self.assertEqual(context["gpu_advice"]["gpus"][1]["temperature_c"], 47)
        self.assertFalse(
            context["gpu_advice"]["gpus"][1]["capabilities"][
                "stable_device_identifier"
            ]
        )
        self.assertTrue(context["capabilities"]["idempotent_edit"])
        self.assertEqual(context["capabilities"]["idempotent_edit_history_limit"], 256)
        self.assertTrue(context["capabilities"]["structured_cancel"])
        self.assertTrue(context["capabilities"]["scheduled_job_live_guard"])
        self.assertTrue(context["capabilities"]["single_worker_lease"])
        self.assertTrue(context["capabilities"]["worker_liveness"])
        self.assertTrue(context["capabilities"]["scheduled_job_crash_recovery"])
        self.assertTrue(context["capabilities"]["weighted_shared_capacity"])
        self.assertTrue(context["capabilities"]["private_job_spec_cleanup"])
        self.assertTrue(context["capabilities"]["bounded_private_job_logs"])
        self.assertTrue(context["capabilities"]["private_job_log_cleanup"])
        self.assertTrue(context["capabilities"]["bounded_personal_audit_log"])
        self.assertEqual(context["capabilities"]["audit_api_schema"], "gpubk.audit.v1")
        self.assertEqual(
            context["capabilities"]["private_job_spec_orphan_grace_seconds"],
            24 * 60 * 60,
        )
        self.assertEqual(context["policy"]["shared_capacity_units_per_gpu"], 2)
        self.assertEqual(context["policy"]["job_log_retention_days"], 30)
        self.assertEqual(context["policy"]["job_log_max_mb"], 64)
        self.assertEqual(context["policy"]["job_log_total_max_mb"], 4096)
        self.assertEqual(context["policy"]["worker_recovery_grace_seconds"], 5.0)
        self.assertEqual(
            context["policy"]["monitoring"],
            {
                "sample_interval_seconds": 2.0,
                "rollup_seconds": 60,
                "writer_uid": None,
                "collector": {
                    "schema_version": "gpubk.collector.v1",
                    "state": "not-seen",
                    "reported_status": None,
                    "fresh": None,
                    "age_seconds": None,
                    "stale_after_seconds": None,
                },
            },
        )
        self.assertEqual(context["policy"]["worker_busy_exit_code"], 75)
        self.assertEqual(context["policy"]["worker_waiting_exit_code"], 3)
        self.assertTrue(context["capabilities"]["configurable_monitor_cadence"])
        self.assertTrue(context["capabilities"]["collector_liveness"])
        self.assertEqual(context["worker"]["state"], "unavailable")
        self.assertNotIn("secret", str(context))

    def test_agent_context_exposes_current_uid_worker_liveness(self):
        actor = Actor(os.getuid(), "current")
        job_dir = self.data_dir / "private-jobs"
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=job_dir,
        )
        lease = acquire_job_worker_lease(config, actor, "agent-worker", "gpu-host")
        try:
            context = build_agent_context(
                config,
                self.store,
                actor,
                at=self.start,
                advice=self.advice,
            )
        finally:
            lease.release()

        self.assertEqual(context["worker"]["state"], "running")
        self.assertTrue(context["worker"]["running"])
        self.assertEqual(context["worker"]["lease"]["worker_id"], "agent-worker")

    def test_agent_context_exposes_a_stale_collector_without_writing(self):
        usage_store = UsageAuditStore(self.data_dir)
        usage_store.save_collector_status(
            collector_document(
                monitor_id="monitor-agent",
                status="running",
                uid=1001,
                pid=4321,
                hostname="gpu-host",
                heartbeat_interval_seconds=60.0,
                sample_interval_seconds=2.0,
                rollup_seconds=60,
                started_at=self.start - timedelta(minutes=5),
                sampled_at=self.start,
                written_at=self.start,
                devices=[
                    {
                        "gpu": gpu,
                        "source": "nvml",
                        "device_telemetry": True,
                        "process_telemetry": True,
                        "process_utilization": True,
                    }
                    for gpu in range(2)
                ],
                process_telemetry_gap=[],
                process_utilization_gap=[],
            )
        )

        context = build_agent_context(
            self.config,
            self.store,
            self.actor,
            at=self.start + timedelta(seconds=181),
            advice=self.advice,
        )

        collector = context["policy"]["monitoring"]["collector"]
        self.assertEqual(collector["state"], "stale")
        self.assertFalse(collector["fresh"])
        self.assertTrue(collector["topology_match"])

    def test_context_and_implicit_submission_use_configured_granularity(self):
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            slot_minutes=10,
        )
        now = self.start + timedelta(minutes=47, seconds=23)
        advice = build_gpu_advice(config, snapshots=self.snapshots, history={}, at=now)

        context = build_agent_context(config, self.store, self.actor, at=now, advice=advice)
        with (
            mock.patch("bk.service.utc_now", return_value=now),
            mock.patch("bk.scheduler.utc_now", return_value=now),
        ):
            submission = submit_booking(
                config,
                self.store,
                self.actor,
                count=1,
                duration_seconds=20 * 60,
                start_at=now,
                allow_queue=True,
                advice=advice,
            )

        self.assertEqual(context["policy"]["granularity_minutes"], 10)
        self.assertTrue(context["capabilities"]["configurable_booking_granularity"])
        self.assertEqual(submission.result.reservation["start_at"], "2030-01-01T12:40:00Z")

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

    def test_implicit_recommendation_uses_the_current_five_minute_slot(self):
        now = self.start + timedelta(minutes=41, seconds=23)

        with (
            mock.patch("bk.service.utc_now", return_value=now),
            mock.patch("bk.scheduler.utc_now", return_value=now),
        ):
            recommendation = recommend_booking(
                self.config,
                self.store,
                self.actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=now,
                mode=MODE_SHARED,
                allow_queue=True,
                advice=self.advice,
            )

        self.assertEqual(recommendation["recommendation"]["start_at"], "2030-01-01T12:40:00Z")
        self.assertFalse(recommendation["recommendation"]["queued"])

    def test_implicit_submission_uses_the_current_five_minute_slot(self):
        now = self.start + timedelta(minutes=41, seconds=23)

        with (
            mock.patch("bk.service.utc_now", return_value=now),
            mock.patch("bk.scheduler.utc_now", return_value=now),
        ):
            submission = submit_booking(
                self.config,
                self.store,
                self.actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=now,
                mode=MODE_SHARED,
                allow_queue=True,
                advice=self.advice,
            )

        self.assertEqual(submission.result.reservation["start_at"], "2030-01-01T12:40:00Z")
        self.assertFalse(submission.result.queued)

    def test_weighted_share_is_exposed_in_recommendation_and_public_result(self):
        config = Config(data_dir=self.data_dir, gpu_count=2, max_shared_users=4)
        advice = build_gpu_advice(config, snapshots=self.snapshots, history={}, at=self.start)

        recommendation = recommend_booking(
            config,
            self.store,
            self.actor,
            count=1,
            duration_seconds=30 * 60,
            start_at=self.start,
            mode=MODE_SHARED,
            share_units=3,
            allow_queue=False,
            advice=advice,
        )
        submission = submit_booking(
            config,
            self.store,
            self.actor,
            count=1,
            duration_seconds=30 * 60,
            start_at=self.start,
            mode=MODE_SHARED,
            share_units=3,
            allow_queue=False,
            advice=advice,
        )

        self.assertEqual(recommendation["request"]["share_units_per_gpu"], 3)
        self.assertEqual(recommendation["request"]["share_fraction_per_gpu"], "3/4")
        self.assertEqual(submission.result.reservation["share_units"], 3)

    def test_non_job_submission_does_not_probe_a_private_worker(self):
        with mock.patch("bk.service.inspect_worker_status") as inspect:
            submission = submit_booking(
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

        payload = booking_result_payload("created", submission, self.actor)

        inspect.assert_not_called()
        self.assertIsNone(submission.worker_status)
        self.assertIsNone(payload["worker"])
        self.assertFalse(any("worker" in warning for warning in payload["warnings"]))

    def test_scheduled_job_submission_and_edit_report_missing_worker(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=self.data_dir / "private-jobs",
        )
        submission = submit_booking(
            config,
            self.store,
            actor,
            count=1,
            duration_seconds=30 * 60,
            start_at=self.start,
            command_argv=[sys.executable, "-c", "print('private')"],
            working_directory=self.tmp.name,
            allow_queue=False,
            advice=self.advice,
        )

        payload = booking_result_payload("created", submission, actor)
        edited = submit_edit(
            config,
            self.store,
            actor,
            submission.result.reservation["id"],
            duration_seconds=45 * 60,
            advice=self.advice,
        )

        self.assertEqual(submission.worker_status["state"], "not-seen")
        self.assertEqual(payload["worker"]["state"], "not-seen")
        self.assertTrue(any("start `bk w`" in warning for warning in payload["warnings"]))
        self.assertEqual(edited.worker_status["state"], "not-seen")

    def test_scheduled_job_submission_accepts_a_proven_running_worker(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=self.data_dir / "private-jobs",
        )
        lease = acquire_job_worker_lease(config, actor, "service-worker", "gpu-host")
        try:
            submission = submit_booking(
                config,
                self.store,
                actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=self.start,
                command_argv=[sys.executable, "-c", "print('private')"],
                working_directory=self.tmp.name,
                allow_queue=False,
                advice=self.advice,
            )
        finally:
            lease.release()

        payload = booking_result_payload("created", submission, actor)

        self.assertEqual(submission.worker_status["state"], "running")
        self.assertTrue(submission.worker_status["running"])
        self.assertFalse(any("scheduled command worker" in warning for warning in payload["warnings"]))

    def test_cancellation_removes_the_current_uids_pending_private_job_spec(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=Path(self.tmp.name) / "private-jobs",
        )
        submission = submit_booking(
            config,
            self.store,
            actor,
            count=1,
            duration_seconds=30 * 60,
            start_at=self.start,
            mode=MODE_SHARED,
            command_argv=[sys.executable, "-c", "print('private')"],
            working_directory=self.tmp.name,
            allow_queue=False,
            advice=self.advice,
        )
        reservation = submission.result.reservation
        path = job_spec_path(config, reservation["job"]["spec_id"])

        cancelled = submit_cancellation(config, self.store, actor, reservation["id"])

        self.assertEqual(cancelled.reservation["status"], "cancelled")
        self.assertEqual(cancelled.cleanup.removed, 1)
        self.assertEqual(cancelled.cleanup.failed, 0)
        self.assertFalse(path.exists())

    def test_cancellation_stays_successful_when_private_cleanup_is_unsafe(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=Path(self.tmp.name) / "private-jobs",
        )
        submission = submit_booking(
            config,
            self.store,
            actor,
            count=1,
            duration_seconds=30 * 60,
            start_at=self.start,
            command_argv=[sys.executable, "-c", "print('private')"],
            working_directory=self.tmp.name,
            allow_queue=False,
            advice=self.advice,
        )
        reservation = submission.result.reservation
        spec_dir = config.job_log_dir / "specs"
        outside = Path(self.tmp.name) / "outside-specs"
        spec_dir.rename(outside)
        spec_dir.symlink_to(outside, target_is_directory=True)

        cancelled = submit_cancellation(config, self.store, actor, reservation["id"])

        self.assertEqual(cancelled.reservation["status"], "cancelled")
        self.assertEqual(cancelled.cleanup.failed, 1)
        self.assertIn("not a directory", cancelled.cleanup.warnings[0])
        self.assertIn("cleanup issue", self.store.last_warning)
        self.assertEqual(len(list(outside.glob("*.json"))), 1)

    def test_context_fails_closed_for_malformed_legacy_share_units(self):
        add_booking(
            self.store,
            self.config,
            BookingRequest(
                actor=self.actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=self.start,
                mode=MODE_SHARED,
            ),
        )

        def damage_share_units(ledger):
            ledger["reservations"][0]["share_units"] = "broken"
            return ledger, None, [], True

        self.store.transaction(damage_share_units)
        context = build_agent_context(
            self.config,
            self.store,
            self.actor,
            at=self.start,
            advice=self.advice,
        )

        reservation = context["reservations"][0]
        self.assertEqual(reservation["share_units_per_gpu"], 2)
        self.assertEqual(reservation["share_fraction_per_gpu"], "2/2")

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

    def test_structured_edit_uses_advice_and_is_idempotent(self):
        created = add_booking(
            self.store,
            self.config,
            BookingRequest(
                actor=self.actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=self.start,
                mode=MODE_SHARED,
            ),
        )

        first = submit_edit(
            self.config,
            self.store,
            self.actor,
            created.reservation["id"],
            duration_seconds=45 * 60,
            operation_id="service-edit-1",
            advice=self.advice,
        )
        retried = submit_edit(
            self.config,
            self.store,
            self.actor,
            created.reservation["id"],
            duration_seconds=45 * 60,
            operation_id="service-edit-1",
            advice=self.advice,
        )

        self.assertTrue(first.result.created)
        self.assertFalse(retried.result.created)
        self.assertEqual(first.result.reservation["end_at"], "2030-01-01T12:45:00Z")
        self.assertEqual(first.allocator.source, "fixed-gpu")
        self.assertEqual(len(self.store.load()["reservations"]), 1)

    def test_structured_edit_rejects_explicit_zero_before_allocation(self):
        created = add_booking(
            self.store,
            self.config,
            BookingRequest(
                actor=self.actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=self.start,
                mode=MODE_SHARED,
            ),
        )

        for field, message in (("count", "GPU count"), ("duration_seconds", "duration")):
            with self.subTest(field=field):
                with mock.patch("bk.service._allocation_decision") as allocator:
                    with self.assertRaisesRegex(BookingError, message):
                        submit_edit(
                            self.config,
                            self.store,
                            self.actor,
                            created.reservation["id"],
                            advice=self.advice,
                            **{field: 0},
                        )
                allocator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
