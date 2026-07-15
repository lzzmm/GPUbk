import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bk import __version__
from bk.advisor import build_gpu_advice
from bk.collector_status import collector_document
from bk.config import Config
from bk.gpu import GpuProcessSnapshot, GpuSnapshot
from bk.joblogs import acquire_job_worker_lease
from bk.models import MODE_EXCLUSIVE, MODE_SHARED, Actor, BookingError, BookingRequest
from bk.scheduler import add_booking
from bk.scheduler import find_applied_create as scheduler_find_applied_create
from bk.service import (
    AGENT_SCHEMA_VERSION,
    booking_result_payload,
    build_agent_context,
    recommend_booking,
    scheduled_job_worker_warning,
    submit_booking,
    submit_cancellation,
    submit_edit,
)
from bk.storage import LedgerStore
from bk.usage_store import UsageAuditStore
from bk.worker import job_spec_path, job_submission_identity


class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.start = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.scheduler_now_patch = mock.patch(
            "bk.scheduler.utc_now",
            return_value=self.start - timedelta(days=1),
        )
        self.scheduler_now_patch.start()
        self.config = Config(data_dir=self.data_dir, gpu_count=2, max_shared_users=2)
        self.store = LedgerStore(self.data_dir)
        self.actor = Actor(1002 if os.getuid() == 1001 else 1001, "alice")
        self.snapshots = [
            GpuSnapshot(
                0,
                "busy",
                memory_used_mb=16000,
                memory_total_mb=24000,
                utilization_percent=80,
                processes=(
                    GpuProcessSnapshot(
                        55, 1002, "bob", "python train.py --token secret"
                    ),
                ),
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
        self.advice = build_gpu_advice(
            self.config, snapshots=self.snapshots, history={}, at=self.start
        )

    def tearDown(self):
        self.scheduler_now_patch.stop()
        self.tmp.cleanup()

    def test_running_worker_warns_when_logout_persistence_is_disabled(self):
        warning = scheduled_job_worker_warning(
            {
                "running": True,
                "state": "running",
                "persistence": {
                    "state": "disabled",
                    "logout_safe": False,
                    "admin_argv": ["sudo", "loginctl", "enable-linger", "alice"],
                },
            }
        )

        self.assertIn("may stop after logout", warning)
        self.assertIn("tmux", warning)
        self.assertIn("bk info", warning)

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
        self.assertEqual(context["software"], {"name": "gpubk", "version": __version__})
        self.assertEqual(
            context["administrator"]["schema_version"],
            "gpubk.administrator.v1",
        )
        self.assertEqual(context["administrator"]["account"]["uid"], os.getuid())
        self.assertEqual(context["policy"]["granularity_minutes"], 5)
        self.assertEqual(context["policy"]["access_mode"], "private")
        self.assertIsNone(context["policy"]["storage_gid"])
        self.assertEqual(context["policy"]["enabled_gpus"], [0, 1])
        self.assertEqual(context["policy"]["disabled_gpus"], [])
        self.assertEqual(context["policy"]["gpu_priority"], {})
        self.assertTrue(context["policy"]["worker_live_guard"])
        self.assertEqual(context["gpu_advice"]["order"], [1, 0])
        self.assertEqual(context["gpu_advice"]["gpus"][1]["name"], "idle")
        self.assertEqual(context["gpu_advice"]["gpus"][1]["temperature_c"], 47)
        self.assertFalse(
            context["gpu_advice"]["gpus"][1]["capabilities"]["stable_device_identifier"]
        )
        self.assertTrue(context["capabilities"]["idempotent_edit"])
        self.assertTrue(context["capabilities"]["idempotent_cancel"])
        self.assertTrue(context["capabilities"]["operation_status"])
        self.assertTrue(context["capabilities"]["preflight_idempotent_replay"])
        self.assertEqual(context["capabilities"]["idempotent_edit_history_limit"], 256)
        self.assertTrue(context["capabilities"]["structured_cancel"])
        self.assertTrue(context["capabilities"]["scheduled_job_live_guard"])
        self.assertTrue(context["capabilities"]["single_worker_lease"])
        self.assertTrue(context["capabilities"]["worker_liveness"])
        self.assertTrue(context["capabilities"]["worker_instance_binding"])
        self.assertTrue(context["capabilities"]["daemon_policy_guard"])
        self.assertTrue(context["capabilities"]["scheduled_job_crash_recovery"])
        self.assertTrue(context["capabilities"]["scheduled_job_path_snapshot"])
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
        self.assertEqual(context["policy"]["worker_max_parallel"], 64)
        self.assertEqual(context["policy"]["worker_effective_max_parallel"], 4)
        self.assertEqual(context["policy"]["worker_termination_grace_seconds"], 5.0)
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
        self.assertEqual(context["policy"]["daemon_policy_exit_code"], 78)
        self.assertTrue(context["capabilities"]["configurable_monitor_cadence"])
        self.assertTrue(context["capabilities"]["collector_liveness"])
        self.assertTrue(context["capabilities"]["request_gpu_exclusions"])
        self.assertTrue(
            context["capabilities"]["administrator_gpu_eligibility_policy"]
        )
        self.assertEqual(context["worker"]["state"], "unavailable")
        self.assertNotIn("secret", str(context))

    def test_agent_context_exposes_administrator_gpu_policy(self):
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            disabled_gpus=(1,),
            gpu_priority=((0, 8),),
        )
        advice = build_gpu_advice(
            config,
            snapshots=self.snapshots,
            history={},
            at=self.start,
        )

        context = build_agent_context(
            config,
            self.store,
            self.actor,
            at=self.start,
            advice=advice,
        )

        self.assertEqual(context["policy"]["enabled_gpus"], [0])
        self.assertEqual(context["policy"]["disabled_gpus"], [1])
        self.assertEqual(context["policy"]["gpu_priority"], {"0": 8})

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
                        "stable_device_identifier": True,
                        "process_telemetry": True,
                        "process_utilization": True,
                    }
                    for gpu in range(2)
                ],
                stable_device_identifier_gap=[],
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
        self.assertTrue(collector["process_identity_capability_known"])
        self.assertEqual(collector["process_identity_gap"], [])

    def test_context_and_implicit_submission_use_configured_granularity(self):
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            slot_minutes=10,
        )
        now = self.start + timedelta(minutes=47, seconds=23)
        advice = build_gpu_advice(config, snapshots=self.snapshots, history={}, at=now)

        context = build_agent_context(
            config, self.store, self.actor, at=now, advice=advice
        )
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
        self.assertEqual(
            submission.result.reservation["start_at"], "2030-01-01T12:40:00Z"
        )

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
        self.assertGreater(
            recommendation["recommendation"]["gpu_details"][0]["memory_free_now_mb"],
            20000,
        )
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

        self.assertEqual(
            recommendation["recommendation"]["start_at"], "2030-01-01T12:40:00Z"
        )
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

        self.assertEqual(
            submission.result.reservation["start_at"], "2030-01-01T12:40:00Z"
        )
        self.assertFalse(submission.result.queued)

    def test_recommendation_and_create_ignore_the_same_expired_legacy_record(self):
        active_slice = self.start + timedelta(minutes=40)
        now = active_slice + timedelta(minutes=1, seconds=23)
        legacy = add_booking(
            self.store,
            self.config,
            BookingRequest(
                actor=Actor(1002, "legacy"),
                count=1,
                duration_seconds=60 * 60,
                start_at=active_slice - timedelta(hours=1),
                mode=MODE_EXCLUSIVE,
                preferred_gpus=[0],
            ),
        )

        def make_sub_slot_legacy(ledger):
            reservation = next(
                item
                for item in ledger["reservations"]
                if item["id"] == legacy.reservation["id"]
            )
            reservation["end_at"] = (active_slice + timedelta(seconds=30)).isoformat()
            return ledger, None, [], True

        self.store.transaction(make_sub_slot_legacy)
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
                mode=MODE_EXCLUSIVE,
                preferred_gpus=[0],
                allow_queue=True,
                advice=self.advice,
            )
            submission = submit_booking(
                self.config,
                self.store,
                self.actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=now,
                mode=MODE_EXCLUSIVE,
                preferred_gpus=[0],
                allow_queue=True,
                advice=self.advice,
            )

        self.assertEqual(
            recommendation["recommendation"]["start_at"], "2030-01-01T12:40:00Z"
        )
        self.assertEqual(
            submission.result.reservation["start_at"], "2030-01-01T12:40:00Z"
        )
        stored_legacy = next(
            item
            for item in self.store.load()["reservations"]
            if item["id"] == legacy.reservation["id"]
        )
        self.assertEqual(stored_legacy["status"], "expired")

    def test_exact_recommendation_and_create_reject_before_the_current_slot(self):
        now = self.start + timedelta(minutes=41, seconds=23)
        current_start = self.start + timedelta(minutes=40)
        past_start = self.start + timedelta(minutes=35)
        before = self.store.load()

        with (
            mock.patch("bk.service.utc_now", return_value=now),
            mock.patch("bk.scheduler.utc_now", return_value=now),
        ):
            with self.assertRaisesRegex(BookingError, "current booking slice"):
                recommend_booking(
                    self.config,
                    self.store,
                    self.actor,
                    count=1,
                    duration_seconds=30 * 60,
                    start_at=past_start,
                    mode=MODE_SHARED,
                    allow_queue=False,
                    advice=self.advice,
                )
            with self.assertRaisesRegex(BookingError, "current booking slice"):
                submit_booking(
                    self.config,
                    self.store,
                    self.actor,
                    count=1,
                    duration_seconds=30 * 60,
                    start_at=past_start,
                    mode=MODE_SHARED,
                    allow_queue=False,
                    advice=self.advice,
                )
            current = recommend_booking(
                self.config,
                self.store,
                self.actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=current_start,
                mode=MODE_SHARED,
                preferred_gpus=[1],
                allow_queue=False,
                advice=self.advice,
            )

        self.assertEqual(self.store.load(), before)
        self.assertTrue(current["available"])
        self.assertEqual(current["recommendation"]["start_at"], "2030-01-01T12:40:00Z")

    def test_weighted_share_is_exposed_in_recommendation_and_public_result(self):
        config = Config(data_dir=self.data_dir, gpu_count=2, max_shared_users=4)
        advice = build_gpu_advice(
            config, snapshots=self.snapshots, history={}, at=self.start
        )

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
        self.assertEqual(recommendation["request"]["share_capacity_units_per_gpu"], 4)
        self.assertNotIn("share_fraction_per_gpu", recommendation["request"])
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
        self.assertTrue(
            any("start `bk w start`" in warning for warning in payload["warnings"])
        )
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
            with mock.patch(
                "bk.worker_status.inspect_worker_persistence",
                return_value={
                    "kind": "systemd-linger",
                    "state": "enabled",
                    "logout_safe": True,
                },
            ):
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
        self.assertFalse(
            any(
                "scheduled command worker" in warning for warning in payload["warnings"]
            )
        )

    def test_scheduled_job_interrupt_before_commit_removes_unused_private_spec(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=self.data_dir / "private-jobs",
        )

        with mock.patch("bk.service.add_booking", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                submit_booking(
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

        self.assertEqual(self.store.load().get("reservations", []), [])
        self.assertEqual(list((config.job_log_dir / "specs").glob("*.json")), [])

    def test_scheduled_job_interrupt_after_commit_retains_referenced_private_spec(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=self.data_dir / "private-jobs",
        )

        def commit_then_interrupt(store, runtime_config, request):
            add_booking(store, runtime_config, request)
            raise KeyboardInterrupt

        with mock.patch("bk.service.add_booking", side_effect=commit_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                submit_booking(
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

        reservation = self.store.load()["reservations"][0]
        self.assertTrue(job_spec_path(config, reservation["job"]["spec_id"]).exists())

    def test_scheduled_job_operation_retry_has_no_new_external_or_file_side_effects(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=self.data_dir / "private-jobs",
        )
        kwargs = {
            "count": 1,
            "duration_seconds": 30 * 60,
            "start_at": self.start,
            "command_argv": [sys.executable, "-c", "print('private')"],
            "working_directory": self.tmp.name,
            "allow_queue": False,
            "operation_id": "scheduled-retry-1",
        }

        first = submit_booking(config, self.store, actor, advice=self.advice, **kwargs)
        with (
            mock.patch("bk.service._allocation_decision") as allocator,
            mock.patch("bk.service.prepare_job_spec") as prepare,
            mock.patch("bk.service.delete_job_spec") as delete,
            mock.patch("bk.advisor.snapshot") as live_probe,
            mock.patch("bk.advisor.UsageAuditStore") as history_store,
        ):
            second = submit_booking(config, self.store, actor, **kwargs)

        self.assertTrue(first.result.created)
        self.assertFalse(second.result.created)
        self.assertEqual(
            first.result.reservation["id"], second.result.reservation["id"]
        )
        self.assertEqual(second.allocator.source, "idempotent-replay")
        self.assertEqual(second.advice.live_states[0].status, "unknown")
        allocator.assert_not_called()
        prepare.assert_not_called()
        delete.assert_not_called()
        live_probe.assert_not_called()
        history_store.assert_not_called()
        self.assertEqual(len(list((config.job_log_dir / "specs").glob("*.json"))), 1)

    def test_explicit_duplicate_accepts_a_legacy_v1_command_digest(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=self.data_dir / "private-jobs",
        )
        command = [sys.executable, "-c", "print('legacy duplicate')"]
        identity = job_submission_identity(
            actor,
            command,
            self.tmp.name,
            execution_environment={"PATH": "/new-worker-path"},
        )
        first = add_booking(
            self.store,
            config,
            BookingRequest(
                actor=actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=self.start,
                preferred_gpus=[0],
                job_spec_id="00000000-0000-0000-0000-000000000001",
                job_digest=identity.legacy_digests[0],
                job_summary=identity.summary,
            ),
        )

        with mock.patch.dict(os.environ, {"PATH": "/different-current-path"}):
            duplicate = submit_booking(
                config,
                self.store,
                actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=self.start,
                preferred_gpus=[0],
                command_argv=command,
                working_directory=self.tmp.name,
                allow_queue=False,
                advice=self.advice,
            )

        self.assertFalse(duplicate.result.created)
        self.assertEqual(duplicate.result.reservation["id"], first.reservation["id"])
        self.assertEqual(len(self.store.load()["reservations"]), 1)
        self.assertEqual(list((config.job_log_dir / "specs").glob("*.json")), [])

    def test_scheduled_job_operation_id_rejects_a_different_private_command(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=self.data_dir / "private-jobs",
        )
        common = {
            "count": 1,
            "duration_seconds": 30 * 60,
            "start_at": self.start,
            "working_directory": self.tmp.name,
            "allow_queue": False,
            "operation_id": "scheduled-command-mismatch",
            "advice": self.advice,
        }
        submit_booking(
            config,
            self.store,
            actor,
            command_argv=[sys.executable, "-c", "print('first')"],
            **common,
        )

        with (
            mock.patch("bk.service._allocation_decision") as allocator,
            mock.patch("bk.service.prepare_job_spec") as prepare,
        ):
            with self.assertRaisesRegex(BookingError, "different write"):
                submit_booking(
                    config,
                    self.store,
                    actor,
                    command_argv=[sys.executable, "-c", "print('second')"],
                    **common,
                )

        allocator.assert_not_called()
        prepare.assert_not_called()
        self.assertEqual(len(self.store.load()["reservations"]), 1)
        self.assertEqual(len(list((config.job_log_dir / "specs").glob("*.json"))), 1)

    def test_scheduled_job_operation_id_rejects_a_different_submission_path(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=self.data_dir / "private-jobs",
        )
        common = {
            "count": 1,
            "duration_seconds": 30 * 60,
            "start_at": self.start,
            "command_argv": [sys.executable, "-c", "print('same command')"],
            "working_directory": self.tmp.name,
            "allow_queue": False,
            "operation_id": "scheduled-path-mismatch",
            "advice": self.advice,
        }
        with mock.patch.dict(os.environ, {"PATH": "/environment/one"}):
            submit_booking(config, self.store, actor, **common)

        with (
            mock.patch.dict(os.environ, {"PATH": "/environment/two"}),
            mock.patch("bk.service._allocation_decision") as allocator,
            mock.patch("bk.service.prepare_job_spec") as prepare,
        ):
            with self.assertRaisesRegex(BookingError, "different write"):
                submit_booking(config, self.store, actor, **common)

        allocator.assert_not_called()
        prepare.assert_not_called()
        self.assertEqual(len(self.store.load()["reservations"]), 1)

    def test_scheduled_job_retry_does_not_attempt_unused_spec_cleanup(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=self.data_dir / "private-jobs",
        )
        kwargs = {
            "count": 1,
            "duration_seconds": 30 * 60,
            "start_at": self.start,
            "command_argv": [sys.executable, "-c", "print('private')"],
            "working_directory": self.tmp.name,
            "allow_queue": False,
            "operation_id": "scheduled-retry-cleanup-warning",
            "advice": self.advice,
        }
        submit_booking(config, self.store, actor, **kwargs)

        with mock.patch("bk.service.delete_job_spec") as delete:
            retried = submit_booking(config, self.store, actor, **kwargs)

        payload = booking_result_payload(
            "existing", retried, actor, self.store.last_warning
        )
        self.assertFalse(retried.result.created)
        self.assertEqual(retried.allocator.source, "idempotent-replay")
        self.assertIsNone(self.store.last_warning)
        self.assertFalse(any("cleanup" in warning for warning in payload["warnings"]))
        delete.assert_not_called()

    def test_scheduled_job_retry_accepts_a_legacy_v1_command_digest(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=self.data_dir / "private-jobs",
        )
        command = [sys.executable, "-c", "print('legacy')"]
        identity = job_submission_identity(
            actor,
            command,
            self.tmp.name,
            execution_environment={"PATH": "/new-worker-path"},
        )
        legacy_digest = identity.legacy_digests[0]
        operation_id = "legacy-v1-command-replay"
        first = add_booking(
            self.store,
            config,
            BookingRequest(
                actor=actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=self.start,
                preferred_gpus=[0],
                op_id=operation_id,
                job_spec_id="00000000-0000-0000-0000-000000000001",
                job_digest=legacy_digest,
                job_summary=identity.summary,
            ),
        )

        with (
            mock.patch("bk.service._allocation_decision") as allocator,
            mock.patch("bk.service.prepare_job_spec") as prepare,
            mock.patch.dict(os.environ, {"PATH": "/different-current-path"}),
        ):
            replay = submit_booking(
                config,
                self.store,
                actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=self.start,
                preferred_gpus=[0],
                command_argv=command,
                working_directory=self.tmp.name,
                allow_queue=False,
                operation_id=operation_id,
            )

        self.assertFalse(replay.result.created)
        self.assertEqual(replay.result.reservation["id"], first.reservation["id"])
        self.assertEqual(replay.allocator.source, "idempotent-replay")
        allocator.assert_not_called()
        prepare.assert_not_called()

    def test_scheduled_job_retry_survives_a_removed_working_directory(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=self.data_dir / "private-jobs",
        )
        work_dir = self.data_dir / "temporary-work"
        work_dir.mkdir()
        kwargs = {
            "count": 1,
            "duration_seconds": 30 * 60,
            "start_at": self.start,
            "command_argv": [sys.executable, "-c", "print('private')"],
            "working_directory": str(work_dir),
            "allow_queue": False,
            "operation_id": "scheduled-retry-missing-cwd",
        }
        first = submit_booking(config, self.store, actor, advice=self.advice, **kwargs)
        work_dir.rmdir()

        retried = submit_booking(config, self.store, actor, **kwargs)

        self.assertTrue(first.result.created)
        self.assertFalse(retried.result.created)
        self.assertEqual(first.result.reservation["id"], retried.result.reservation["id"])
        self.assertEqual(retried.allocator.source, "idempotent-replay")

    def test_concurrent_scheduled_retries_fall_back_to_atomic_create(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=self.data_dir / "private-jobs",
        )
        barrier = threading.Barrier(2)

        def synchronized_find(ledger, runtime_config, request):
            result = scheduler_find_applied_create(ledger, runtime_config, request)
            if result is None:
                barrier.wait(timeout=2)
            return result

        def submit():
            return submit_booking(
                config,
                self.store,
                actor,
                count=1,
                duration_seconds=30 * 60,
                start_at=self.start,
                command_argv=[sys.executable, "-c", "print('private')"],
                working_directory=self.tmp.name,
                allow_queue=False,
                operation_id="concurrent-scheduled-retry",
                advice=self.advice,
            )

        with mock.patch("bk.service.find_applied_create", side_effect=synchronized_find):
            with ThreadPoolExecutor(max_workers=2) as pool:
                submissions = list(pool.map(lambda _index: submit(), range(2)))

        self.assertEqual(sum(item.result.created for item in submissions), 1)
        self.assertEqual(len(self.store.load()["reservations"]), 1)
        self.assertEqual(len(list((config.job_log_dir / "specs").glob("*.json"))), 1)

    def test_new_scheduled_job_rejects_missing_cwd_before_allocation(self):
        actor = Actor(os.getuid(), "current")
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=self.data_dir / "private-jobs",
        )
        missing = self.data_dir / "missing-work"

        with (
            mock.patch("bk.service._allocation_decision") as allocator,
            mock.patch("bk.service.prepare_job_spec") as prepare,
        ):
            with self.assertRaisesRegex(BookingError, "working directory does not exist"):
                submit_booking(
                    config,
                    self.store,
                    actor,
                    count=1,
                    duration_seconds=30 * 60,
                    start_at=self.start,
                    command_argv=[sys.executable, "-c", "print('private')"],
                    working_directory=str(missing),
                    allow_queue=False,
                    operation_id="missing-cwd-new-command",
                    advice=self.advice,
                )

        allocator.assert_not_called()
        prepare.assert_not_called()

    def test_scheduled_job_submission_rejects_worker_readiness_from_another_instance(
        self,
    ):
        actor = Actor(os.getuid(), "current")
        job_dir = self.data_dir / "private-jobs"
        config = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=job_dir,
        )
        other = Config(
            data_dir=self.data_dir / "other-ledger",
            gpu_count=2,
            max_shared_users=2,
            job_log_dir=job_dir,
        )
        lease = acquire_job_worker_lease(other, actor, "other-worker", "gpu-host")
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

        self.assertEqual(submission.worker_status["state"], "other-instance")
        self.assertFalse(submission.worker_status["running"])
        self.assertTrue(
            any("another data directory" in warning for warning in payload["warnings"])
        )

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
        self.assertEqual(reservation["share_capacity_units_per_gpu"], 2)
        self.assertNotIn("share_fraction_per_gpu", reservation)

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
        self.assertEqual(
            recommendation["nearest_available"]["start_at"], "2030-01-01T12:30:00Z"
        )
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
        with (
            mock.patch("bk.service._allocation_decision") as allocator,
            mock.patch("bk.advisor.snapshot") as live_probe,
        ):
            retried = submit_edit(
                self.config,
                self.store,
                self.actor,
                created.reservation["id"],
                duration_seconds=45 * 60,
                operation_id="service-edit-1",
            )

        self.assertTrue(first.result.created)
        self.assertFalse(retried.result.created)
        self.assertEqual(first.result.reservation["end_at"], "2030-01-01T12:45:00Z")
        self.assertEqual(first.allocator.source, "fixed-gpu")
        self.assertEqual(retried.allocator.source, "idempotent-replay")
        allocator.assert_not_called()
        live_probe.assert_not_called()
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

        for field, message in (
            ("count", "GPU count"),
            ("duration_seconds", "duration"),
        ):
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

    def test_structured_edit_rejects_policy_mismatch_before_allocation(self):
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
        mismatch = Config(
            data_dir=self.data_dir,
            gpu_count=2,
            max_shared_users=3,
            allocator_command=(
                sys.executable,
                "-c",
                "raise SystemExit('must not run')",
            ),
        )
        before = self.store.ledger_path.read_bytes()

        with mock.patch("bk.service._allocation_decision") as allocator:
            with self.assertRaisesRegex(
                BookingError, "max_shared_reservations_per_gpu"
            ):
                submit_edit(
                    mismatch,
                    self.store,
                    self.actor,
                    created.reservation["id"],
                    count=1,
                    duration_seconds=45 * 60,
                    advice=self.advice,
                )

        allocator.assert_not_called()
        self.assertEqual(self.store.ledger_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
