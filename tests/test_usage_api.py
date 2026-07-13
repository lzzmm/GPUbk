import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bk.collector_status import COLLECTOR_STATUS_SCHEMA_VERSION, collector_document
from bk.config import Config
from bk.usage_api import UsageQueryService, auto_resolution
from bk.usage_store import UsageAuditStore
from bk.telemetry import (
    CollectorStatusSink,
    TELEMETRY_INGEST_SCHEMA_VERSION,
    open_usage_query,
    open_usage_store,
)
from bk.workload import describe_workload


class UsageApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.config = Config(data_dir=self.data_dir, gpu_count=2)
        self.store = UsageAuditStore(self.data_dir)
        self.api = UsageQueryService(self.config, self.store)
        self.start = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def test_user_summary_is_uid_filterable_and_uses_process_metrics(self):
        alice_workload = self.store.register_workload(1001, describe_workload("torchrun train.py"))
        bob_workload = self.store.register_workload(1002, describe_workload("python eval.py"))
        self.store.append_rollups(
            [
                self._record(1001, "alice", 0, alice_workload, status="ok", active=60),
                self._record(1002, "bob", 1, bob_workload, status="unreserved", active=60),
            ]
        )

        payload = self.api.users(
            start=self.start - timedelta(minutes=1),
            end=self.start + timedelta(minutes=2),
            resolution="1m",
            uid=1001,
        )

        self.assertEqual(payload["schema_version"], "gpubk.usage.v1")
        self.assertEqual(len(payload["users"]), 1)
        alice = payload["users"][0]
        self.assertEqual(alice["uid"], 1001)
        self.assertEqual(alice["active_gpu_seconds"], 60)
        self.assertEqual(alice["reserved_gpu_seconds"], 60)
        self.assertEqual(alice["workloads"][0]["kind"], "training")
        self.assertIn("not divided", payload["notes"][1])

    def test_requested_coarser_resolution_is_filled_from_current_minute_data(self):
        workload_id = self.store.register_workload(1001, describe_workload("python train.py"))
        self.store.append_rollups([self._record(1001, "alice", 0, workload_id, status="ok", active=60)])

        payload = self.api.samples(
            start=self.start,
            end=self.start + timedelta(minutes=5),
            resolution="5m",
            uid=1001,
        )

        self.assertEqual(payload["query"]["resolution_seconds"], 300)
        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(payload["records"][0]["resolution_seconds"], 300)
        self.assertEqual(payload["records"][0]["workloads"][0]["label"], "train.py")

    def test_event_api_expands_workload_without_exposing_command_arguments(self):
        workload_id = self.store.register_workload(
            1001,
            describe_workload("python train.py --api-key super-secret"),
        )
        self.store.append_events(
            [
                {
                    "event": "process-start",
                    "timestamp": self.start.isoformat().replace("+00:00", "Z"),
                    "event_id": "event-1",
                    "key": "g0:p1:s1",
                    "gpu": 0,
                    "pid": 1,
                    "uid": 1001,
                    "username": "alice",
                    "workload_id": workload_id,
                    "kind": "C",
                    "status": "ok",
                    "reservation_ids": [],
                }
            ]
        )

        payload = self.api.events(
            start=self.start - timedelta(seconds=1),
            end=self.start + timedelta(seconds=1),
            uid=1001,
        )

        text = str(payload)
        self.assertEqual(payload["records"][0]["workload"]["label"], "train.py")
        self.assertNotIn("super-secret", text)
        self.assertNotIn("api-key", text)

    def test_capabilities_expose_stable_interfaces_and_retention(self):
        payload = self.api.capabilities()

        self.assertEqual(payload["storage_format"], "gpubk.usage/1")
        self.assertEqual(payload["interfaces"]["writer_protocol"], "bk.telemetry.TelemetrySink")
        self.assertEqual(
            payload["interfaces"]["collector_status_protocol"],
            "bk.telemetry.CollectorStatusSink",
        )
        self.assertEqual(payload["collector"]["state"], "not-seen")
        self.assertNotIn("collector", payload["storage"])
        self.assertEqual(payload["retention"]["minute_days"], 30)
        self.assertEqual(payload["retention"]["daily_days"], 0)
        self.assertEqual(
            payload["writer_policy"],
            {
                "configured_uid": None,
                "role_required": False,
                "root_owned_config_required": False,
            },
        )
        self.assertTrue(payload["durability"]["append_batch_rollback"])
        self.assertTrue(payload["durability"]["interrupted_tail_repair"])

    def test_every_public_query_reports_collector_freshness(self):
        self.store.save_collector_status(
            collector_document(
                monitor_id="monitor-api",
                status="running",
                uid=1001,
                pid=4321,
                hostname="gpu-host",
                heartbeat_interval_seconds=60.0,
                sample_interval_seconds=2.0,
                rollup_seconds=60,
                started_at=self.start - timedelta(minutes=1),
                sampled_at=self.start,
                written_at=self.start,
                devices=[
                    {
                        "gpu": 0,
                        "source": "nvml",
                        "device_telemetry": True,
                        "stable_device_identifier": True,
                        "process_telemetry": True,
                        "process_utilization": True,
                    },
                    {
                        "gpu": 1,
                        "source": "nvml",
                        "device_telemetry": True,
                        "stable_device_identifier": True,
                        "process_telemetry": True,
                        "process_utilization": True,
                    },
                ],
                stable_device_identifier_gap=[],
                process_telemetry_gap=[],
                process_utilization_gap=[],
            )
        )

        with mock.patch("bk.usage_api.utc_now", return_value=self.start):
            payloads = [
                self.api.capabilities(),
                self.api.samples(start=self.start - timedelta(minutes=1), end=self.start),
                self.api.events(start=self.start - timedelta(minutes=1), end=self.start),
                self.api.users(start=self.start - timedelta(minutes=1), end=self.start),
            ]

        self.assertTrue(all(payload["collector"]["state"] == "running" for payload in payloads))
        self.assertTrue(all(payload["collector"]["fresh"] for payload in payloads))
        self.assertTrue(all(payload["collector"]["topology_match"] for payload in payloads))
        self.assertTrue(
            all(
                payload["collector"]["stable_device_identifier_gap"] == []
                for payload in payloads
            )
        )
        self.assertTrue(
            all(payload["collector"]["process_identity_gap"] == [] for payload in payloads)
        )

    def test_public_telemetry_facade_is_ui_independent(self):
        store = open_usage_store(self.config)
        api = open_usage_query(self.config)

        self.assertIsInstance(store, UsageAuditStore)
        self.assertIsInstance(api, UsageQueryService)
        self.assertEqual(TELEMETRY_INGEST_SCHEMA_VERSION, "gpubk.telemetry.v1")
        self.assertEqual(COLLECTOR_STATUS_SCHEMA_VERSION, "gpubk.collector.v1")
        self.assertTrue(hasattr(CollectorStatusSink, "save_collector_status"))

    def test_auto_resolution_scales_with_query_window(self):
        self.assertEqual(auto_resolution(self.start, self.start + timedelta(hours=2)), 60)
        self.assertEqual(auto_resolution(self.start, self.start + timedelta(days=30)), 300)
        self.assertEqual(auto_resolution(self.start, self.start + timedelta(days=300)), 600)
        self.assertEqual(auto_resolution(self.start, self.start + timedelta(days=1000)), 3600)
        self.assertEqual(auto_resolution(self.start, self.start + timedelta(days=2000)), 86400)

    def _record(self, uid, username, gpu, workload_id, *, status, active):
        return {
            "window_start": self.start.isoformat().replace("+00:00", "Z"),
            "window_end": (self.start + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "partial": False,
            "gpu": gpu,
            "uid": uid,
            "username": username,
            "status": status,
            "reservation_ids": [f"booking-{uid}"] if status == "ok" else [],
            "sample_count": 30,
            "observed_seconds": 60,
            "active_sample_count": 30 if active else 0,
            "active_observed_seconds": active,
            "avg_process_count": 1 if active else 0,
            "max_process_count": 1 if active else 0,
            "sm_sample_count": 30,
            "avg_sm_percent": 50,
            "max_sm_percent": 70,
            "avg_gpu_memory_mb": 4096,
            "max_gpu_memory_mb": 6144,
            "device_util_sample_count": 30,
            "avg_device_util_percent": 60,
            "max_device_util_percent": 80,
            "workload_ids": [workload_id],
            "workload_observed_seconds": {str(workload_id): active},
        }


if __name__ == "__main__":
    unittest.main()
