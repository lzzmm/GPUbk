import gzip
import json
import os
import stat
import tempfile
import tracemalloc
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bk.collector_status import CollectorStatusError, collector_document
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

    def test_append_discards_only_an_incomplete_trailing_record(self):
        self.store.append_rollups([self._rollup_at(0)])
        path = self.store._partition_path("minute", self.at.date())
        with path.open("ab") as fh:
            fh.write(b'{"v":1,"partial"')

        self.store.append_rollups([self._rollup_at(1)])

        lines = path.read_bytes().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(isinstance(json.loads(line), dict) for line in lines))
        self.assertEqual(len(self.store.recent_rollups(10)), 2)
        self.assertTrue(any("discarded an incomplete trailing" in item for item in self.store.last_warnings))

    def test_append_preserves_a_valid_final_record_missing_only_its_newline(self):
        self.store.append_rollups([self._rollup_at(0)])
        path = self.store._partition_path("minute", self.at.date())
        path.write_bytes(path.read_bytes().removesuffix(b"\n"))

        self.store.append_rollups([self._rollup_at(1)])

        self.assertEqual(len(path.read_bytes().splitlines()), 2)
        self.assertEqual(len(self.store.recent_rollups(10)), 2)
        self.assertTrue(any("restored a missing final newline" in item for item in self.store.last_warnings))

    def test_failed_append_rolls_the_partition_back_to_its_original_size(self):
        self.store.append_rollups([self._rollup_at(0)])
        path = self.store._partition_path("minute", self.at.date())
        original = path.read_bytes()
        real_write = os.write
        calls = 0

        def fail_after_partial_write(fd, payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, bytes(payload[:7]))
            raise OSError("simulated full disk")

        with mock.patch("bk.usage_store.os.write", side_effect=fail_after_partial_write):
            with self.assertRaisesRegex(OSError, "simulated full disk"):
                self.store.append_rollups([self._rollup_at(1)])

        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.store.recent_rollups(10)), 1)

    def test_append_rejects_partition_mode_drift_without_mutation(self):
        self.store.append_rollups([self._rollup_at(0)])
        path = self.store._partition_path("minute", self.at.date())
        original = path.read_bytes()
        path.chmod(0o644)

        with self.assertRaisesRegex(PermissionError, "expected 0600"):
            self.store.append_rollups([self._rollup_at(1)])

        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_append_rejects_ancestor_directory_mode_drift_without_mutation(self):
        self.store.append_rollups([self._rollup_at(0)])
        path = self.store._partition_path("minute", self.at.date())
        original = path.read_bytes()
        tier_dir = self.store.usage_dir / "minute"
        tier_dir.chmod(0o755)

        with self.assertRaisesRegex(PermissionError, "expected 0700"):
            self.store.append_rollups([self._rollup_at(1)])

        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(tier_dir.stat().st_mode), 0o755)

    def test_writer_rejects_core_file_mode_drift(self):
        self.store.ensure()
        self.store.meta_path.chmod(0o644)

        with self.assertRaisesRegex(PermissionError, "expected 0600"):
            self.store.append_rollups([self._rollup_at(0)])

        self.assertFalse(self.store._partition_path("minute", self.at.date()).exists())

    def test_collector_status_read_is_side_effect_free_and_atomic(self):
        self.assertEqual(self.store.load_collector_status()["state"], "not-seen")
        self.assertFalse(self.store.usage_dir.exists())

        payload = self._collector_status()
        self.store.save_collector_status(payload)
        observed = self.store.load_collector_status(now=self.at + timedelta(seconds=20))
        mismatched = self.store.load_collector_status(
            now=self.at + timedelta(seconds=20),
            expected_gpu_count=2,
        )

        self.assertEqual(observed["state"], "running")
        self.assertTrue(observed["fresh"])
        self.assertEqual(observed["monitor_id"], "monitor-test")
        self.assertEqual(mismatched["state"], "topology-mismatch")
        self.assertFalse(mismatched["topology_match"])
        self.assertEqual(stat.S_IMODE(self.store.collector_path.stat().st_mode), 0o600)
        self.assertFalse(any(self.store.usage_dir.glob(".collector.*.tmp")))

    def test_invalid_collector_status_is_rejected_before_storage_creation(self):
        payload = self._collector_status()
        payload["status"] = "future-state"

        with self.assertRaises(CollectorStatusError):
            self.store.save_collector_status(payload)

        self.assertFalse(self.store.usage_dir.exists())

    def test_corrupt_or_newer_collector_status_is_reported_read_only(self):
        self.store.ensure()
        self.store.collector_path.write_text("{broken", encoding="utf-8")
        self.store.collector_path.chmod(0o600)

        invalid = self.store.load_collector_status(now=self.at)
        issues = self.store.health_issues()

        self.assertEqual(invalid["state"], "invalid")
        self.assertTrue(any(item["type"] == "usage-collector-status" for item in issues))

        newer = self._collector_status()
        newer["schema_version"] = "gpubk.collector.v2"
        self.store.collector_path.write_text(json.dumps(newer), encoding="utf-8")
        incompatible = self.store.load_collector_status(now=self.at)
        self.assertEqual(incompatible["state"], "incompatible")

    def test_collector_status_symlink_is_never_followed_or_replaced(self):
        self.store.ensure()
        target = self.data_dir / "outside.json"
        target.write_text("keep", encoding="utf-8")
        self.store.collector_path.symlink_to(target)

        status = self.store.load_collector_status(now=self.at)
        with self.assertRaises(OSError):
            self.store.save_collector_status(self._collector_status())

        self.assertEqual(status["state"], "invalid")
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_plain_partition_reader_yields_before_parsing_the_next_record(self):
        path = self.data_dir / "stream.v1.jsonl"
        path.write_text('{"one":1}\n{"two":2}\n', encoding="utf-8")
        original_loads = json.loads
        calls = 0

        def fail_on_second_record(raw):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("read past the requested record")
            return original_loads(raw)

        with mock.patch("bk.usage_store.json.loads", side_effect=fail_on_second_record):
            records = self.store._read_jsonl(path)
            self.assertEqual(next(records), {"one": 1})
            with self.assertRaisesRegex(RuntimeError, "read past"):
                next(records)

    def test_limited_forward_query_has_bounded_partition_memory(self):
        self.store.append_rollups([self._rollup_at(0)])
        path = self.store._partition_path("minute", self.at.date())
        line = path.read_bytes()
        with path.open("wb") as fh:
            for _ in range(50_000):
                fh.write(line)

        tracemalloc.start()
        try:
            records = list(self.store.iter_rollups("minute", limit=1))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(len(records), 1)
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_limited_reverse_query_has_bounded_partition_memory(self):
        self.store.append_rollups([self._rollup_at(0)])
        path = self.store._partition_path("minute", self.at.date())
        raw = json.loads(path.read_text(encoding="utf-8"))
        first_timestamp = int(self.at.timestamp())
        with path.open("w", encoding="utf-8") as fh:
            for offset in range(50_000):
                raw["id"] = f"rollup-{offset}"
                raw["t"] = first_timestamp + offset * 60
                fh.write(json.dumps(raw, separators=(",", ":"), sort_keys=True) + "\n")

        tracemalloc.start()
        try:
            records = list(self.store.iter_rollups("minute", newest_first=True, limit=3))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(
            [record["record_id"] for record in records],
            ["rollup-49999", "rollup-49998", "rollup-49997"],
        )
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_reverse_query_keeps_latest_duplicate_position(self):
        retry = self._rollup_at(1)
        self.store.append_rollups([self._rollup_at(0), retry, self._rollup_at(2), retry])

        records = list(self.store.iter_rollups("minute", newest_first=True, limit=3))

        self.assertEqual(
            [record["window_start"] for record in records],
            [
                (self.at + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                (self.at + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
                self.at.isoformat().replace("+00:00", "Z"),
            ],
        )

    def test_query_skips_invalid_versioned_timestamp_with_warning(self):
        self.store.append_rollups([self._rollup_at(0)])
        path = self.store._partition_path("minute", self.at.date())
        raw = json.loads(path.read_text(encoding="utf-8"))
        invalid = dict(raw, id="invalid", t="not-a-timestamp")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(invalid) + "\n")

        records = list(self.store.iter_rollups("minute", newest_first=True, limit=2))

        self.assertEqual(len(records), 1)
        self.assertTrue(any("skipped invalid rollup" in item for item in self.store.last_warnings))

    def test_append_refuses_to_create_a_partition_the_reader_cannot_open(self):
        self.store.append_rollups([self._rollup_at(0)])
        path = self.store._partition_path("minute", self.at.date())
        original = path.read_bytes()

        with mock.patch(
            "bk.usage_store.MAX_PARTITION_UNCOMPRESSED_BYTES",
            len(original) + 1,
        ):
            with self.assertRaisesRegex(UsageFormatError, "safety limit"):
                self.store.append_rollups([self._rollup_at(1)])

        self.assertEqual(path.read_bytes(), original)

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

    def test_short_workload_key_write_is_rejected_and_cleaned_up(self):
        self.store.ensure()

        with mock.patch("bk.usage_store.os.write", return_value=1):
            with self.assertRaisesRegex(OSError, "short write"):
                self.store.register_workload(1001, describe_workload("python train.py"))

        self.assertFalse(self.store.key_path.exists())
        self.assertFalse(self.store.workloads_path.exists())

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

    def test_health_rejects_hard_linked_usage_file(self):
        self.store.ensure()
        alias = self.data_dir / "store-alias"
        os.link(self.store.meta_path, alias)

        issues = self.store.health_issues()
        by_path = {item.get("path"): item for item in issues}

        self.assertEqual(by_path[str(self.store.meta_path)]["type"], "usage-file-links")
        self.assertEqual(by_path[str(self.store.meta_path)]["actual"], 2)
        with self.assertRaisesRegex(OSError, "2 hard links"):
            self.store.append_rollups([self._rollup_at(0)])
        self.assertFalse(self.store._partition_path("minute", self.at.date()).exists())

    def test_shared_mode_health_accepts_private_workload_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageAuditStore(
                Path(tmp) / "shared",
                file_mode=0o660,
                dir_mode=0o2770,
            )
            store.register_workload(1001, describe_workload("python train.py"))

            self.assertEqual(store.health_issues(), [])

    def test_shared_mode_applies_exact_mode_to_partition_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageAuditStore(
                Path(tmp) / "shared",
                file_mode=0o660,
                dir_mode=0o2770,
            )
            store.append_rollups([self._rollup_at(0)])
            partition = store._partition_path("minute", self.at.date())
            expected_gid = store.data_dir.stat().st_gid

            self.assertEqual(stat.S_IMODE(partition.stat().st_mode), 0o660)
            self.assertEqual(partition.stat().st_gid, expected_gid)
            cursor = partition.parent
            while cursor != store.usage_dir:
                self.assertEqual(stat.S_IMODE(cursor.stat().st_mode), 0o2770)
                self.assertEqual(cursor.stat().st_gid, expected_gid)
                cursor = cursor.parent
            self.assertEqual(store.usage_dir.stat().st_gid, expected_gid)
            self.assertEqual(store.health_issues(), [])

    def test_shared_mode_health_reports_partition_gid_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageAuditStore(
                Path(tmp) / "shared",
                file_mode=0o660,
                dir_mode=0o2770,
            )
            store.append_rollups([self._rollup_at(0)])
            partition = store._partition_path("minute", self.at.date())
            expected_gid = store.data_dir.stat().st_gid
            original_lstat = Path.lstat

            def drifted_lstat(path):
                metadata = original_lstat(path)
                if path == partition:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_nlink=metadata.st_nlink,
                        st_gid=expected_gid + 1,
                    )
                return metadata

            with mock.patch.object(Path, "lstat", autospec=True, side_effect=drifted_lstat):
                issues = store.health_issues()
                with self.assertRaisesRegex(UsageFormatError, "usage-file-gid"):
                    store.maintain(
                        UsageRetentionPolicy(),
                        now=self.at + timedelta(days=40),
                    )

            issue = next(item for item in issues if item.get("path") == str(partition))
            self.assertEqual(issue["type"], "usage-file-gid")
            self.assertEqual(issue["expected_gid"], expected_gid)
            self.assertEqual(issue["actual_gid"], expected_gid + 1)
            self.assertTrue(partition.exists())

    def test_configured_storage_gid_rejects_the_wrong_usage_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "shared"
            data_dir.mkdir(mode=0o700)
            data_dir.chmod(0o2770)
            actual_gid = data_dir.stat().st_gid
            store = UsageAuditStore(
                data_dir,
                file_mode=0o660,
                dir_mode=0o2770,
                storage_gid=actual_gid + 1,
            )

            with self.assertRaisesRegex(PermissionError, "configured storage_gid"):
                store.append_rollups([self._rollup_at(0)])

            self.assertFalse(store.usage_dir.exists())
            issue = next(
                item
                for item in store.health_issues()
                if item.get("path") == str(data_dir)
                and item["type"] == "usage-directory-gid"
            )
            self.assertEqual(issue["expected_gid"], actual_gid + 1)
            self.assertEqual(issue["actual_gid"], actual_gid)

    def test_append_rejects_partition_gid_drift_before_updating_users(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageAuditStore(
                Path(tmp) / "shared",
                file_mode=0o660,
                dir_mode=0o2770,
            )
            store.append_rollups([self._rollup_at(0)])
            partition = store._partition_path("minute", self.at.date())
            original_partition = partition.read_bytes()
            original_users = store.users_path.read_bytes()
            partition_inode = partition.stat().st_ino
            real_fstat = os.fstat
            changed = self._rollup_at(1)
            changed["username"] = "changed-name"

            def drifted_fstat(fd):
                metadata = real_fstat(fd)
                if metadata.st_ino == partition_inode:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_nlink=metadata.st_nlink,
                        st_gid=metadata.st_gid + 1,
                    )
                return metadata

            with mock.patch("bk.fileio.os.fstat", side_effect=drifted_fstat):
                with self.assertRaisesRegex(PermissionError, "GID"):
                    store.append_rollups([changed])

            self.assertEqual(partition.read_bytes(), original_partition)
            self.assertEqual(store.users_path.read_bytes(), original_users)

    def test_atomic_usage_write_rejects_bad_inherited_gid_without_residue(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageAuditStore(
                Path(tmp) / "shared",
                file_mode=0o660,
                dir_mode=0o2770,
            )
            store.ensure()
            expected_gid = store.data_dir.stat().st_gid
            captured_fds = set()
            real_mkstemp = tempfile.mkstemp
            real_fstat = os.fstat

            def captured_mkstemp(*args, **kwargs):
                fd, path = real_mkstemp(*args, **kwargs)
                captured_fds.add(fd)
                return fd, path

            def bad_temporary_gid(fd):
                metadata = real_fstat(fd)
                if fd in captured_fds:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_nlink=metadata.st_nlink,
                        st_gid=expected_gid + 1,
                    )
                return metadata

            with (
                mock.patch("bk.usage_store.tempfile.mkstemp", side_effect=captured_mkstemp),
                mock.patch("bk.usage_store.os.fstat", side_effect=bad_temporary_gid),
            ):
                with self.assertRaisesRegex(PermissionError, "temporary file"):
                    store.save_state({"one": {"pid": 1}})

            self.assertFalse(store.state_path.exists())
            self.assertEqual(list(store.usage_dir.glob(".state.*.tmp")), [])

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

    def test_closed_partition_parses_the_same_inode_that_was_verified(self):
        self.store.append_rollups([self._rollup_at(0)])
        self.store.maintain(UsageRetentionPolicy(minute_days=0), now=self.at + timedelta(days=1))
        path = self.store._partition_path("minute", self.at.date()).with_suffix(".jsonl.gz")
        with gzip.open(path, "rb") as fh:
            replacement_record = json.loads(fh.read())
        replacement_record["g"] = 7
        replacement = path.with_name("replacement.jsonl.gz")
        with gzip.open(replacement, "wb") as fh:
            fh.write(
                (json.dumps(replacement_record, separators=(",", ":"), sort_keys=True) + "\n").encode()
            )
        replacement.chmod(0o600)
        real_scan = self.store._scan_closed_stream

        def replace_path_after_verification(raw, scanned_path):
            result = real_scan(raw, scanned_path)
            os.replace(replacement, scanned_path)
            return result

        with mock.patch.object(
            self.store,
            "_scan_closed_stream",
            side_effect=replace_path_after_verification,
        ):
            records = list(self.store.iter_rollups("minute"))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["gpu"], 0)

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
        usage_dir.mkdir(mode=0o700)
        store_path = usage_dir / "store.json"
        store_path.write_text(
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
        store_path.chmod(0o600)

        with self.assertRaisesRegex(UsageFormatError, "newer gpubk"):
            self.store.append_events([self._event(None)])

    def test_same_major_store_can_require_a_newer_writer_minor(self):
        usage_dir = self.data_dir / "usage"
        usage_dir.mkdir(mode=0o700)
        store_path = usage_dir / "store.json"
        store_path.write_text(
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
        store_path.chmod(0o600)

        with self.assertRaisesRegex(UsageFormatError, "newer gpubk"):
            self.store.append_events([self._event(None)])

    def test_store_can_require_a_newer_reader_without_risking_partial_decode(self):
        usage_dir = self.data_dir / "usage"
        usage_dir.mkdir(mode=0o700)
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

    def test_state_transition_recovers_after_journal_directory_fsync_failure(self):
        event = self._event(None)
        processes = {"g0:p12:sabc": {"status": "ok"}}
        self.store.ensure()

        with mock.patch(
            "bk.usage_store.fsync_directory",
            side_effect=OSError("usage directory sync failed"),
        ):
            with self.assertRaisesRegex(OSError, "usage directory sync failed"):
                self.store.commit_state_transition([event], processes)

        self.assertTrue(self.store.transition_journal_path.exists())
        self.assertEqual(self.store.recent_events(10), [])

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

    def _collector_status(self):
        return collector_document(
            monitor_id="monitor-test",
            status="running",
            uid=1001,
            pid=4321,
            hostname="gpu-host",
            heartbeat_interval_seconds=60.0,
            sample_interval_seconds=2.0,
            rollup_seconds=60,
            started_at=self.at - timedelta(minutes=5),
            sampled_at=self.at,
            written_at=self.at,
            devices=[
                {
                    "gpu": 0,
                    "source": "nvml",
                    "device_telemetry": True,
                    "stable_device_identifier": True,
                    "process_telemetry": True,
                    "process_utilization": True,
                }
            ],
            stable_device_identifier_gap=[],
            process_telemetry_gap=[],
            process_utilization_gap=[],
        )

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

    def _rollup_at(self, minute_offset):
        item = self._rollup(None)
        start = self.at + timedelta(minutes=minute_offset)
        item["window_start"] = start.isoformat().replace("+00:00", "Z")
        item["window_end"] = (start + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        return item


if __name__ == "__main__":
    unittest.main()
