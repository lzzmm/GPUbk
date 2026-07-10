import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bk.config import Config
from bk.gpu import GpuProcessSnapshot, GpuSnapshot
from bk.monitor import UsageAuditStore, UsageMonitor
from bk.storage import LedgerStore


def iso(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reservation(rid, uid, gpu, start, end):
    return {
        "id": rid,
        "op_id": f"{rid}-op",
        "uid": uid,
        "username": f"user{uid}",
        "gpus": [gpu],
        "mode": "shared",
        "start_at": iso(start),
        "end_at": iso(end),
        "status": "active",
        "created_at": iso(start),
        "updated_at": iso(start),
    }


class UsageMonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2030, 1, 1, 12, 0, 1, tzinfo=timezone.utc)

    @staticmethod
    def write_ledger(path, reservations):
        path.mkdir(parents=True, exist_ok=True)
        (path / "ledger.json").write_text(
            json.dumps({"version": 1, "reservations": reservations}),
            encoding="utf-8",
        )

    def test_monitor_deduplicates_process_events_and_records_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self.write_ledger(
                data_dir,
                [reservation("alice-booking", 1001, 0, self.now - timedelta(minutes=5), self.now + timedelta(minutes=5))],
            )
            config = Config(data_dir=data_dir, gpu_count=1)
            ledger_store = LedgerStore(data_dir)
            audit_store = UsageAuditStore(data_dir)
            current = [
                GpuSnapshot(
                    0,
                    "sim",
                    processes=(
                        GpuProcessSnapshot(10, 1001, "alice", "python a.py", 1024, 40, "C", "start-a"),
                        GpuProcessSnapshot(20, 2002, "bob", "python b.py", 2048, 30, "C", "start-b"),
                    ),
                    source="simulation",
                )
            ]
            monitor = UsageMonitor(config, ledger_store, audit_store, snapshot_provider=lambda _config: current)

            first = monitor.collect(self.now)
            second = monitor.collect(self.now + timedelta(seconds=2))
            current[0] = GpuSnapshot(0, "sim", processes=(), source="simulation")
            third = monitor.collect(self.now + timedelta(seconds=4))
            monitor.close(self.now + timedelta(seconds=5))

            self.assertEqual(len(first.events), 2)
            self.assertEqual(second.events, ())
            self.assertEqual(len(third.events), 2)
            self.assertEqual({event["event"] for event in first.events}, {"process-start"})
            self.assertEqual({event["event"] for event in third.events}, {"process-stop"})
            self.assertEqual(first.violation_count, 1)
            self.assertEqual(third.process_count, 0)
            self.assertEqual(len(audit_store.recent_events(10)), 4)

    def test_monitor_emits_authorization_change_when_booking_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self.write_ledger(data_dir, [])
            config = Config(data_dir=data_dir, gpu_count=1)
            ledger_store = LedgerStore(data_dir)
            audit_store = UsageAuditStore(data_dir)
            devices = [
                GpuSnapshot(
                    0,
                    "sim",
                    processes=(GpuProcessSnapshot(10, 1001, "alice", "python a.py", host_start_id="start-a"),),
                    source="simulation",
                )
            ]
            monitor = UsageMonitor(config, ledger_store, audit_store, snapshot_provider=lambda _config: devices)

            first = monitor.collect(self.now)
            self.write_ledger(
                data_dir,
                [reservation("alice-booking", 1001, 0, self.now - timedelta(minutes=1), self.now + timedelta(minutes=5))],
            )
            second = monitor.collect(self.now + timedelta(seconds=2))
            monitor.close(self.now + timedelta(seconds=3))

            self.assertEqual(first.events[0]["status"], "unreserved")
            self.assertEqual(second.events[0]["event"], "authorization-change")
            self.assertEqual(second.events[0]["old_status"], "unreserved")
            self.assertEqual(second.events[0]["status"], "ok")
            self.assertEqual(second.events[0]["reservation_ids"], ["alice-booking"])

    def test_monitor_state_survives_restart_without_duplicate_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self.write_ledger(data_dir, [])
            config = Config(data_dir=data_dir, gpu_count=1)
            ledger_store = LedgerStore(data_dir)
            audit_store = UsageAuditStore(data_dir)
            devices = [
                GpuSnapshot(
                    0,
                    "sim",
                    processes=(GpuProcessSnapshot(10, 1001, "alice", "python a.py", host_start_id="start-a"),),
                    source="simulation",
                )
            ]

            first_monitor = UsageMonitor(config, ledger_store, audit_store, snapshot_provider=lambda _config: devices)
            first = first_monitor.collect(self.now)
            first_monitor.close(self.now + timedelta(seconds=1))
            second_monitor = UsageMonitor(config, ledger_store, audit_store, snapshot_provider=lambda _config: devices)
            second = second_monitor.collect(self.now + timedelta(seconds=2))
            second_monitor.close(self.now + timedelta(seconds=3))

            self.assertEqual(len(first.events), 1)
            self.assertEqual(second.events, ())
            self.assertEqual(len(audit_store.recent_events(10)), 1)

    def test_rollup_aggregates_process_metrics_and_observed_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self.write_ledger(
                data_dir,
                [reservation("alice-booking", 1001, 0, self.now - timedelta(minutes=1), self.now + timedelta(minutes=5))],
            )
            config = Config(data_dir=data_dir, gpu_count=1)
            ledger_store = LedgerStore(data_dir)
            audit_store = UsageAuditStore(data_dir)
            devices = [
                GpuSnapshot(
                    0,
                    "sim",
                    utilization_percent=70,
                    processes=(
                        GpuProcessSnapshot(10, 1001, "alice", "python a.py", 1024, 40, "C", "start-a"),
                    ),
                    source="simulation",
                )
            ]
            monitor = UsageMonitor(
                config,
                ledger_store,
                audit_store,
                interval_seconds=2,
                rollup_seconds=60,
                snapshot_provider=lambda _config: devices,
            )

            monitor.collect(self.now)
            monitor.collect(self.now + timedelta(seconds=2))
            monitor.close(self.now + timedelta(seconds=3))

            rollups = audit_store.recent_rollups(10)
            self.assertEqual(len(rollups), 1)
            rollup = rollups[0]
            self.assertEqual(rollup["status"], "ok")
            self.assertEqual(rollup["sample_count"], 2)
            self.assertEqual(rollup["observed_seconds"], 4)
            self.assertEqual(rollup["avg_process_count"], 1)
            self.assertEqual(rollup["avg_sm_percent"], 40)
            self.assertEqual(rollup["avg_gpu_memory_mb"], 1024)
            self.assertEqual(rollup["avg_device_util_percent"], 70)
            self.assertTrue(rollup["partial"])

    def test_reserved_but_idle_user_is_present_in_rollup(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self.write_ledger(
                data_dir,
                [reservation("idle-booking", 1001, 0, self.now - timedelta(minutes=1), self.now + timedelta(minutes=5))],
            )
            config = Config(data_dir=data_dir, gpu_count=1)
            ledger_store = LedgerStore(data_dir)
            audit_store = UsageAuditStore(data_dir)
            devices = [GpuSnapshot(0, "sim", utilization_percent=0, processes=(), source="simulation")]
            monitor = UsageMonitor(config, ledger_store, audit_store, snapshot_provider=lambda _config: devices)

            monitor.collect(self.now)
            monitor.close(self.now + timedelta(seconds=1))

            rollup = audit_store.recent_rollups(10)[0]
            self.assertEqual(rollup["uid"], 1001)
            self.assertEqual(rollup["reservation_ids"], ["idle-booking"])
            self.assertEqual(rollup["status"], "ok")
            self.assertEqual(rollup["avg_process_count"], 0)
            self.assertEqual(rollup["avg_sm_percent"], 0)

    def test_monitor_persists_compact_per_gpu_load_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self.write_ledger(data_dir, [])
            config = Config(data_dir=data_dir, gpu_count=1)
            ledger_store = LedgerStore(data_dir)
            audit_store = UsageAuditStore(data_dir)
            devices = [
                GpuSnapshot(
                    0,
                    "sim",
                    memory_used_mb=12000,
                    memory_total_mb=24000,
                    utilization_percent=75,
                    source="simulation",
                )
            ]
            monitor = UsageMonitor(config, ledger_store, audit_store, snapshot_provider=lambda _config: devices)

            monitor.collect(self.now)
            monitor.collect(self.now + timedelta(seconds=2))
            monitor.close(self.now + timedelta(seconds=3))

            history = audit_store.load_load_history()
            record = history["gpus"]["0"][0]
            self.assertEqual(record["known_samples"], 2)
            self.assertEqual(record["avg_utilization_percent"], 75)
            self.assertEqual(record["avg_memory_percent"], 50)
            self.assertEqual(record["busy_fraction"], 1)


if __name__ == "__main__":
    unittest.main()
