import unittest
from datetime import datetime, timedelta, timezone

from bk.collector_status import (
    COLLECTOR_STATUS_SCHEMA_VERSION,
    CollectorStatusError,
    classify_collector_document,
    collector_document,
    safe_hostname,
    validate_collector_document,
)


class CollectorStatusTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.devices = [
            {
                "gpu": 0,
                "source": "nvml",
                "device_telemetry": True,
                "stable_device_identifier": True,
                "process_telemetry": True,
                "process_utilization": True,
            }
        ]

    def document(self, **overrides):
        values = {
            "monitor_id": "monitor-1",
            "status": "running",
            "uid": 1001,
            "pid": 4321,
            "hostname": "gpu-host",
            "heartbeat_interval_seconds": 60.0,
            "sample_interval_seconds": 2.0,
            "rollup_seconds": 60,
            "started_at": self.now - timedelta(minutes=5),
            "sampled_at": self.now,
            "written_at": self.now,
            "devices": self.devices,
            "stable_device_identifier_gap": [],
            "process_telemetry_gap": [],
            "process_identity_gap": [],
            "process_utilization_gap": [],
        }
        values.update(overrides)
        return collector_document(**values)

    def test_fresh_running_status_becomes_stale_after_three_heartbeats(self):
        payload = self.document()
        payload["future_extension"] = {"kept": True}

        fresh = classify_collector_document(payload, now=self.now + timedelta(seconds=179))
        stale = classify_collector_document(payload, now=self.now + timedelta(seconds=181))

        self.assertIs(validate_collector_document(payload), payload)
        self.assertEqual(fresh["state"], "running")
        self.assertTrue(fresh["fresh"])
        self.assertTrue(fresh["stable_device_identifier_capability_known"])
        self.assertEqual(fresh["stable_device_identifier_gap"], [])
        self.assertTrue(fresh["process_identity_capability_known"])
        self.assertEqual(fresh["process_identity_gap"], [])
        self.assertEqual(stale["state"], "stale")
        self.assertFalse(stale["fresh"])
        self.assertEqual(stale["reported_status"], "running")

    def test_degraded_and_stopped_states_remain_explicit(self):
        degraded_device = dict(self.devices[0], process_utilization=False)
        degraded = self.document(
            status="degraded",
            devices=[degraded_device],
            process_utilization_gap=[0],
        )
        stopped_at = self.now + timedelta(seconds=5)
        stopped = self.document(
            status="stopped",
            written_at=stopped_at,
            stopped_at=stopped_at,
        )

        self.assertEqual(classify_collector_document(degraded, now=self.now)["state"], "degraded")
        stopped_status = classify_collector_document(stopped, now=stopped_at)
        self.assertEqual(stopped_status["state"], "stopped")
        self.assertFalse(stopped_status["fresh"])

    def test_legacy_status_without_stable_identifier_capability_is_degraded(self):
        payload = self.document()
        payload.pop("stable_device_identifier_gap")
        for device in payload["devices"]:
            device.pop("stable_device_identifier")

        self.assertIs(validate_collector_document(payload), payload)
        status = classify_collector_document(payload, now=self.now)

        self.assertEqual(status["reported_status"], "running")
        self.assertEqual(status["state"], "degraded")
        self.assertTrue(status["fresh"])
        self.assertFalse(status["stable_device_identifier_capability_known"])
        self.assertEqual(status["stable_device_identifier_gap"], [0])
        self.assertFalse(status["devices"][0]["stable_device_identifier"])

    def test_legacy_status_without_process_identity_capability_is_degraded(self):
        payload = self.document()
        payload.pop("process_identity_gap")

        self.assertIs(validate_collector_document(payload), payload)
        status = classify_collector_document(payload, now=self.now)

        self.assertEqual(status["reported_status"], "running")
        self.assertEqual(status["state"], "degraded")
        self.assertTrue(status["fresh"])
        self.assertFalse(status["process_identity_capability_known"])
        self.assertEqual(status["process_identity_gap"], [0])

    def test_large_future_timestamp_is_reported_as_clock_skew(self):
        payload = self.document()

        status = classify_collector_document(payload, now=self.now - timedelta(minutes=10))

        self.assertEqual(status["state"], "clock-skew")
        self.assertEqual(status["clock_skew_seconds"], 600.0)

    def test_fresh_status_with_the_wrong_configured_topology_is_not_current(self):
        status = classify_collector_document(
            self.document(),
            now=self.now,
            expected_gpu_count=2,
        )

        self.assertEqual(status["state"], "topology-mismatch")
        self.assertFalse(status["fresh"])
        self.assertFalse(status["topology_match"])
        self.assertEqual(status["expected_gpu_count"], 2)

    def test_semantic_mismatches_fail_closed(self):
        cases = []
        wrong_schema = self.document()
        wrong_schema["schema_version"] = "gpubk.collector.v2"
        cases.append((wrong_schema, "unsupported collector status schema"))
        duplicate_gpu = self.document()
        duplicate_gpu["devices"] = [self.devices[0], self.devices[0]]
        cases.append((duplicate_gpu, "unique and contiguous"))
        false_running = self.document()
        false_running["devices"] = [
            dict(
                self.devices[0],
                process_telemetry=False,
                process_utilization=False,
            )
        ]
        false_running["process_telemetry_gap"] = [0]
        false_running["process_identity_gap"] = [0]
        cases.append((false_running, "running collector status contains degraded"))
        mismatched_gap = self.document()
        mismatched_gap["process_telemetry_gap"] = [0]
        cases.append((mismatched_gap, "must match per-device process telemetry"))
        missing_identity_dependency = self.document()
        missing_identity_dependency["status"] = "degraded"
        missing_identity_dependency["devices"] = [
            dict(
                self.devices[0],
                process_telemetry=False,
                process_utilization=False,
            )
        ]
        missing_identity_dependency["process_telemetry_gap"] = [0]
        cases.append(
            (
                missing_identity_dependency,
                "process_identity_gap must include every process telemetry gap",
            )
        )
        mismatched_identifier_gap = self.document()
        mismatched_identifier_gap["stable_device_identifier_gap"] = [0]
        cases.append(
            (
                mismatched_identifier_gap,
                "must match per-device stable identifier capabilities",
            )
        )
        invalid_identifier_capability = self.document()
        invalid_identifier_capability["devices"] = [
            dict(self.devices[0], stable_device_identifier=None)
        ]
        cases.append((invalid_identifier_capability, "must be boolean"))
        impossible_capability = self.document()
        impossible_capability["devices"] = [
            dict(self.devices[0], device_telemetry=False, process_telemetry=True)
        ]
        cases.append((impossible_capability, "requires device telemetry"))
        impossible_identifier = self.document()
        impossible_identifier["devices"] = [
            dict(
                self.devices[0],
                device_telemetry=False,
                stable_device_identifier=True,
                process_telemetry=False,
                process_utilization=False,
            )
        ]
        impossible_identifier["process_telemetry_gap"] = [0]
        impossible_identifier["process_identity_gap"] = [0]
        cases.append((impossible_identifier, "stable_device_identifier requires"))
        bad_order = self.document()
        bad_order["sampled_at"] = "2029-12-31T11:00:00Z"
        cases.append((bad_order, "sampled_at must not precede"))

        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(CollectorStatusError, message):
                    validate_collector_document(payload)

    def test_builder_rejects_degraded_capabilities_marked_running(self):
        degraded_device = dict(
            self.devices[0],
            process_telemetry=False,
            process_utilization=False,
        )

        with self.assertRaisesRegex(CollectorStatusError, "running collector status"):
            self.document(
                devices=[degraded_device],
                process_telemetry_gap=[0],
                process_identity_gap=[0],
            )

    def test_builder_infers_identity_gap_from_missing_process_telemetry(self):
        degraded_device = dict(
            self.devices[0],
            process_telemetry=False,
            process_utilization=False,
        )

        payload = self.document(
            status="degraded",
            devices=[degraded_device],
            process_telemetry_gap=[0],
            process_identity_gap=None,
        )

        self.assertEqual(payload["process_identity_gap"], [0])

    def test_builder_rejects_unattributed_processes_marked_running(self):
        with self.assertRaisesRegex(CollectorStatusError, "running collector status"):
            self.document(process_identity_gap=[0])

    def test_builder_rejects_missing_stable_identifier_marked_running(self):
        degraded_device = dict(
            self.devices[0],
            stable_device_identifier=False,
        )

        with self.assertRaisesRegex(CollectorStatusError, "running collector status"):
            self.document(
                devices=[degraded_device],
                stable_device_identifier_gap=[0],
            )

    def test_hostname_is_ascii_bounded_and_nonempty(self):
        self.assertEqual(safe_hostname("gpu\n主机"), "gpu???")
        self.assertEqual(safe_hostname(""), "unknown")
        self.assertLessEqual(len(safe_hostname("x" * 1000)), 255)
        self.assertEqual(COLLECTOR_STATUS_SCHEMA_VERSION, "gpubk.collector.v1")


if __name__ == "__main__":
    unittest.main()
