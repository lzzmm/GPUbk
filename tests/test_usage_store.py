import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bk.usage_store import UsageAuditStore, UsageFormatError, UsageRetentionPolicy
from bk.workload import describe_workload


class UsageStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.store = UsageAuditStore(self.data_dir)
        self.at = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writer_uses_versioned_daily_partitions_and_compact_objects(self):
        workload_id = self.store.register_workload(1001, describe_workload("python train.py --secret value"))
        self.store.append_events([self._event(workload_id)])
        self.store.append_rollups([self._rollup(workload_id)])

        event_path = self.data_dir / "usage/events/2030/01/2030-01-02.v1.jsonl"
        rollup_path = self.data_dir / "usage/minute/2030/01/2030-01-02.v1.jsonl"
        event_raw = json.loads(event_path.read_text(encoding="utf-8"))
        rollup_raw = json.loads(rollup_path.read_text(encoding="utf-8"))

        self.assertEqual(json.loads((self.data_dir / "usage/store.json").read_text())["format"], "gpubk.usage")
        self.assertIsInstance(event_raw, dict)
        self.assertIsInstance(rollup_raw, dict)
        self.assertNotIn("command", event_raw)
        self.assertEqual(rollup_raw["n"], "alice")
        self.assertEqual(self.store.recent_events(1)[0]["workload_id"], workload_id)
        self.assertEqual(self.store.recent_rollups(1)[0]["username"], "alice")

    def test_workload_identity_is_stable_per_uid_and_does_not_store_raw_arguments(self):
        descriptor = describe_workload("python /private/train.py --token secret")

        first = self.store.register_workload(1001, descriptor)
        retry = self.store.register_workload(1001, descriptor)
        other_user = self.store.register_workload(1002, descriptor)
        text = self.store.workloads_path.read_text(encoding="utf-8")

        self.assertEqual(first, retry)
        self.assertNotEqual(first, other_user)
        self.assertNotIn("private", text)
        self.assertNotIn("secret", text)

    def test_health_rejects_symlink_usage_root_without_reading_target(self):
        outside = self.data_dir / "outside-usage"
        outside.mkdir()
        sentinel = outside / "store.json"
        sentinel.write_text("not a gpubk store", encoding="utf-8")
        original = sentinel.read_bytes()
        self.store.usage_dir.symlink_to(outside, target_is_directory=True)

        issues = self.store.health_issues()

        self.assertEqual(issues[0]["type"], "usage-directory-type")
        self.assertEqual(issues[0]["actual"], "symbolic-link")
        self.assertNotIn("usage-format", {item["type"] for item in issues})
        self.assertEqual(sentinel.read_bytes(), original)

    def test_health_rejects_nested_usage_symlink(self):
        self.store.usage_dir.mkdir(mode=0o700)
        target = self.data_dir / "outside-meta"
        target.write_text("keep", encoding="utf-8")
        self.store.meta_path.symlink_to(target)

        issues = self.store.health_issues()

        by_path = {item.get("path"): item for item in issues}
        self.assertEqual(by_path[str(self.store.meta_path)]["type"], "usage-file-type")
        self.assertEqual(by_path[str(self.store.meta_path)]["actual"], "symbolic-link")
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_shared_mode_health_accepts_private_workload_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageAuditStore(
                Path(tmp) / "shared",
                file_mode=0o660,
                dir_mode=0o2770,
            )
            store.register_workload(1001, describe_workload("python train.py"))

            self.assertEqual(store.health_issues(), [])

    def test_maintenance_builds_all_resolutions_before_removing_old_minutes(self):
        workload_id = self.store.register_workload(1001, describe_workload("python train.py"))
        self.store.append_rollups([self._rollup(workload_id)])
        now = self.at + timedelta(days=40)

        report = self.store.maintain(UsageRetentionPolicy(), now=now)

        self.assertTrue(any(item.startswith("five-minute/2030-01-02") for item in report["generated"]))
        self.assertFalse(self.store._partition_exists("minute", self.at.date()))
        for tier in ("five-minute", "ten-minute", "hourly", "daily"):
            self.assertTrue(self.store._partition_exists(tier, self.at.date()), tier)
        five = list(self.store.iter_rollups("five-minute"))
        self.assertEqual(len(five), 1)
        self.assertEqual(five[0]["resolution_seconds"], 300)

    def test_retry_duplicate_rollup_does_not_double_count_compaction(self):
        workload_id = self.store.register_workload(1001, describe_workload("python train.py"))
        record = self._rollup(workload_id)
        self.store.append_rollups([record])
        self.store.append_rollups([record])

        self.store.maintain(UsageRetentionPolicy(minute_days=0), now=self.at + timedelta(days=1))
        five = list(self.store.iter_rollups("five-minute"))

        self.assertEqual(len(five), 1)
        self.assertEqual(five[0]["sample_count"], 30)
        self.assertEqual(five[0]["observed_seconds"], 60)

    def test_future_unknown_field_blocks_compaction_and_source_removal(self):
        workload_id = self.store.register_workload(1001, describe_workload("python train.py"))
        self.store.append_rollups([self._rollup(workload_id)])
        path = self.store._partition_path("minute", self.at.date())
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["future_metric"] = 42
        path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

        report = self.store.maintain(UsageRetentionPolicy(), now=self.at + timedelta(days=40))

        self.assertTrue(report["blocked"])
        self.assertTrue(self.store._partition_exists("minute", self.at.date()))
        self.assertFalse(self.store._partition_exists("five-minute", self.at.date()))

    def test_closed_partition_checksum_detects_corruption(self):
        workload_id = self.store.register_workload(1001, describe_workload("python train.py"))
        self.store.append_rollups([self._rollup(workload_id)])
        policy = UsageRetentionPolicy(minute_days=0)
        self.store.maintain(policy, now=self.at + timedelta(days=1))
        path = self.store._partition_path("minute", self.at.date()).with_suffix(".jsonl.gz")
        content = bytearray(path.read_bytes())
        content[-5] ^= 0xFF
        path.write_bytes(content)

        with self.assertRaises((OSError, UsageFormatError, EOFError)):
            list(self.store.iter_rollups("minute"))

    def test_corrupt_derived_partition_prevents_fine_source_deletion(self):
        workload_id = self.store.register_workload(1001, describe_workload("python train.py"))
        self.store.append_rollups([self._rollup(workload_id)])
        self.store.maintain(UsageRetentionPolicy(minute_days=0), now=self.at + timedelta(days=1))
        derived = self.store._partition_path("five-minute", self.at.date()).with_suffix(".jsonl.gz")
        content = bytearray(derived.read_bytes())
        content[-5] ^= 0xFF
        derived.write_bytes(content)

        report = self.store.maintain(UsageRetentionPolicy(), now=self.at + timedelta(days=40))

        self.assertTrue(self.store._partition_exists("minute", self.at.date()))
        self.assertTrue(any("derived history is incomplete" in item for item in report["blocked"]))

    def test_legacy_migration_is_explicit_repeatable_and_retains_sources(self):
        self.store.events_path.write_text(json.dumps(self._event(None)) + "\n", encoding="utf-8")
        self.store.rollups_path.write_text(json.dumps(self._rollup(None)) + "\n", encoding="utf-8")

        preview = self.store.migrate_legacy(dry_run=True)
        applied = self.store.migrate_legacy(dry_run=False)
        repeated = self.store.migrate_legacy(dry_run=False)

        self.assertEqual(preview["events"], 1)
        self.assertEqual(applied["rollups"], 1)
        self.assertTrue(repeated["already_migrated"])
        self.assertTrue(self.store.events_path.exists())
        self.assertTrue(self.store.rollups_path.exists())
        self.assertEqual(len(self.store.recent_events(10)), 1)
        self.assertEqual(len(self.store.recent_rollups(10)), 1)

        late = self._event(None)
        late["event_id"] = "event-2"
        late["timestamp"] = (self.at + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        with self.store.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(late) + "\n")
        self.assertEqual(len(self.store.recent_events(10)), 2)

        incremental = self.store.migrate_legacy(dry_run=False)
        self.assertTrue(incremental["source_changed"])
        self.assertEqual(len(self.store.recent_events(10)), 2)

    def test_newer_store_format_refuses_writes(self):
        usage_dir = self.data_dir / "usage"
        usage_dir.mkdir()
        (usage_dir / "store.json").write_text(
            json.dumps(
                {
                    "format": "gpubk.usage",
                    "format_major": 2,
                    "format_minor": 0,
                    "min_writer_major": 2,
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(UsageFormatError, "newer gpubk"):
            self.store.append_events([self._event(None)])

    def test_same_major_store_can_require_a_newer_writer_minor(self):
        usage_dir = self.data_dir / "usage"
        usage_dir.mkdir()
        (usage_dir / "store.json").write_text(
            json.dumps(
                {
                    "format": "gpubk.usage",
                    "format_major": 1,
                    "format_minor": 3,
                    "min_writer_major": 1,
                    "min_writer_minor": 1,
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(UsageFormatError, "newer gpubk"):
            self.store.append_events([self._event(None)])

    def test_store_can_require_a_newer_reader_without_risking_partial_decode(self):
        usage_dir = self.data_dir / "usage"
        usage_dir.mkdir()
        (usage_dir / "store.json").write_text(
            json.dumps(
                {
                    "format": "gpubk.usage",
                    "format_major": 2,
                    "format_minor": 0,
                    "min_reader_major": 2,
                    "min_writer_major": 2,
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(UsageFormatError, "newer gpubk reader"):
            list(self.store.iter_events())

    def test_state_transition_journal_recovers_without_duplicate_event(self):
        event = self._event(None)
        processes = {"g0:p12:sabc": {"status": "ok"}}
        with mock.patch.object(self.store, "save_state", side_effect=OSError("simulated crash")):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self.store.commit_state_transition([event], processes)

        recovered = UsageAuditStore(self.data_dir)
        state = recovered.load_state()

        self.assertEqual(state, processes)
        self.assertEqual(len(recovered.recent_events(10)), 1)
        self.assertFalse(recovered.transition_journal_path.exists())

    def _event(self, workload_id):
        return {
            "event": "process-start",
            "timestamp": self.at.isoformat().replace("+00:00", "Z"),
            "event_id": "event-1",
            "key": "g0:p12:sabc",
            "gpu": 0,
            "pid": 12,
            "uid": 1001,
            "username": "alice",
            "workload_id": workload_id,
            "kind": "C",
            "status": "ok",
            "reservation_ids": ["booking"],
        }

    def _rollup(self, workload_id):
        return {
            "window_start": self.at.isoformat().replace("+00:00", "Z"),
            "window_end": (self.at + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "partial": False,
            "gpu": 0,
            "uid": 1001,
            "username": "alice",
            "status": "ok",
            "reservation_ids": ["booking"],
            "sample_count": 30,
            "observed_seconds": 60,
            "active_sample_count": 30,
            "active_observed_seconds": 60,
            "avg_process_count": 1,
            "max_process_count": 1,
            "sm_sample_count": 30,
            "avg_sm_percent": 50,
            "max_sm_percent": 75,
            "avg_gpu_memory_mb": 4096,
            "max_gpu_memory_mb": 6144,
            "device_util_sample_count": 30,
            "avg_device_util_percent": 60,
            "max_device_util_percent": 80,
            "workload_ids": [workload_id] if workload_id else [],
            "workload_observed_seconds": {str(workload_id): 60} if workload_id else {},
        }


if __name__ == "__main__":
    unittest.main()
