import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bk.config import Config
from bk.gpu import GpuProcessSnapshot, GpuSnapshot
from bk.monitor import (
    MonitorAuthorizationError,
    UsageAuditStore,
    UsageMonitor,
    _proc_parent_pid,
    authorize_monitor,
    monitor_configuration_error,
    run_monitor,
)
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

    def test_recent_events_reads_only_the_tail_needed_for_the_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageAuditStore(Path(tmp))
            store.ensure()
            with store.events_path.open("w", encoding="utf-8") as fh:
                for index in range(5000):
                    fh.write(json.dumps({"index": index}) + "\n")
                fh.write("not-json\n")
                for index in range(5000, 5003):
                    fh.write(json.dumps({"index": index}) + "\n")

            with mock.patch("bk.usage_store.json.loads", wraps=json.loads) as loads:
                recent = store.recent_events(3)

        self.assertEqual([item["index"] for item in recent], [5000, 5001, 5002])
        self.assertLessEqual(loads.call_count, 4)

    def test_proc_parent_parser_handles_commands_with_spaces(self):
        self.assertEqual(_proc_parent_pid("123 (python worker 0) S 42 1 2 3"), 42)

    def test_read_only_loads_do_not_create_an_empty_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "not-created"
            store = UsageAuditStore(data_dir)

            self.assertEqual(store.load_state(), {})
            self.assertEqual(store.load_load_history(), {"version": 1, "updated_at": None, "gpus": {}})
            self.assertEqual(store.recent_events(), [])
            self.assertEqual(store.recent_rollups(), [])
            self.assertFalse(data_dir.exists())

    def test_monitor_uses_configured_cadence_when_not_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            config = Config(
                data_dir=data_dir,
                monitor_interval_seconds=2.5,
                monitor_rollup_seconds=30,
            )
            monitor = UsageMonitor(
                config,
                LedgerStore(data_dir),
                UsageAuditStore(data_dir),
            )

            self.assertEqual(monitor.interval_seconds, 2.5)
            self.assertEqual(monitor.rollup_seconds, 30)

    def test_monitor_override_rejects_inexact_rollup_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with self.assertRaisesRegex(ValueError, "integer multiple"):
                UsageMonitor(
                    Config(data_dir=data_dir),
                    LedgerStore(data_dir),
                    UsageAuditStore(data_dir),
                    interval_seconds=7,
                    rollup_seconds=60,
                )

    def test_shared_monitor_requires_root_owned_config_and_assigned_uid(self):
        data_dir = Path("/tmp/bk-shared-monitor-policy")
        missing_path = Config(data_dir=data_dir, dir_mode=0o2770, file_mode=0o660)
        user_owned = Config(
            data_dir=data_dir,
            dir_mode=0o2770,
            file_mode=0o660,
            config_file=Path("/tmp/config.json"),
            config_owner_uid=1001,
            monitor_uid=1001,
        )
        missing_uid = Config(
            data_dir=data_dir,
            dir_mode=0o2770,
            file_mode=0o660,
            config_file=Path("/etc/gpubk/config.json"),
            config_owner_uid=0,
        )
        configured = Config(
            data_dir=data_dir,
            dir_mode=0o2770,
            file_mode=0o660,
            config_file=Path("/etc/gpubk/config.json"),
            config_owner_uid=0,
            monitor_uid=1001,
        )

        self.assertIn("explicit BK_CONFIG_FILE", monitor_configuration_error(missing_path))
        self.assertIn("root-owned", monitor_configuration_error(user_owned))
        self.assertIn("monitor_uid", monitor_configuration_error(missing_uid))
        self.assertIsNone(monitor_configuration_error(configured))
        self.assertEqual(authorize_monitor(configured, uid=1001), 1001)
        with self.assertRaisesRegex(MonitorAuthorizationError, "current UID is 1002"):
            authorize_monitor(configured, uid=1002)

    def test_private_monitor_remains_available_without_role_configuration(self):
        config = Config(data_dir=Path("/tmp/bk-private-monitor-policy"))

        self.assertIsNone(monitor_configuration_error(config))
        self.assertEqual(authorize_monitor(config, uid=1234), 1234)

    def test_shared_monitor_rejects_policy_before_creating_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "absent"
            config = Config(
                data_dir=data_dir,
                dir_mode=0o2770,
                file_mode=0o660,
            )
            store = LedgerStore(
                data_dir,
                file_mode=config.file_mode,
                dir_mode=config.dir_mode,
            )

            with self.assertRaisesRegex(MonitorAuthorizationError, "explicit BK_CONFIG_FILE"):
                run_monitor(config, store, once=True)

            self.assertFalse(data_dir.exists())

    def test_event_append_rejects_symbolic_link_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = UsageAuditStore(data_dir)
            store.ensure()
            target = Path(tmp) / "victim"
            target.write_text("keep", encoding="utf-8")
            event = {
                "event": "process-start",
                "timestamp": "2030-01-01T12:00:00Z",
                "event_id": "unsafe",
                "key": "g0:p1:s1",
                "gpu": 0,
                "pid": 1,
                "uid": 1001,
                "username": "alice",
                "status": "ok",
                "reservation_ids": [],
            }
            partition = store._partition_path("events", datetime(2030, 1, 1, tzinfo=timezone.utc).date())
            partition.parent.mkdir(parents=True)
            partition.symlink_to(target)

            with self.assertRaises(OSError):
                store.append_events([event])

            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

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
            self.assertEqual(len(rollup["workload_ids"]), 1)
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

    def test_system_display_processes_do_not_pollute_long_term_user_history(self):
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
                    processes=(GpuProcessSnapshot(10, 0, "root", "/usr/lib/Xorg", host_start_id="xorg"),),
                    source="simulation",
                )
            ]
            monitor = UsageMonitor(config, ledger_store, audit_store, snapshot_provider=lambda _config: devices)

            monitor.collect(self.now)
            monitor.close(self.now + timedelta(seconds=1))

            self.assertEqual(audit_store.recent_events(10), [])
            self.assertEqual(audit_store.recent_rollups(10), [])
            self.assertEqual(audit_store.workloads(), {})

    def test_managed_runner_pid_enriches_workload_without_reading_private_job_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            booking = reservation(
                "managed-booking",
                1001,
                0,
                self.now - timedelta(minutes=1),
                self.now + timedelta(minutes=5),
            )
            booking["job"] = {"summary": "torchrun train.py (+4 args)", "runner_pid": 10}
            self.write_ledger(data_dir, [booking])
            config = Config(data_dir=data_dir, gpu_count=1)
            audit_store = UsageAuditStore(data_dir)
            devices = [
                GpuSnapshot(
                    0,
                    "sim",
                    processes=(
                        GpuProcessSnapshot(10, 1001, "alice", "python train.py", host_start_id="managed"),
                    ),
                    source="simulation",
                )
            ]
            monitor = UsageMonitor(
                config,
                LedgerStore(data_dir),
                audit_store,
                snapshot_provider=lambda _config: devices,
            )

            monitor.collect(self.now)
            monitor.close(self.now + timedelta(seconds=1))
            workload = next(iter(audit_store.workloads().values()))

            self.assertEqual(workload["source"], "managed")
            self.assertEqual(workload["launcher"], "torchrun")
            self.assertEqual(workload["label"], "torchrun train.py (+4 args)")


if __name__ == "__main__":
    unittest.main()
