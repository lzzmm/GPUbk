import gzip
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bk.cluster_history import (
    _query_sample_batches,
    _remove_temporary_generation,
    export_cluster_history,
    load_archived_user_usage,
    resolve_history_window,
    verify_cluster_history,
)
from bk.config import Config
from bk.models import BookingError
from bk.usage_api import UsageQueryService
from bk.usage_store import UsageAuditStore
from bk.workload import describe_workload


class ClusterHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.archive = self.root / "archive"
        self.archive.mkdir(mode=0o755)
        self.config = Config(data_dir=self.data, gpu_count=2)
        self.store = UsageAuditStore(self.data)
        self.api = UsageQueryService(self.config, self.store)
        self.start = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.end = self.start + timedelta(days=1)
        self.node = {"schema": 1, "id": "a" * 20, "hostname": "gpu-a"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_exports_verifies_reads_and_replays_one_immutable_generation(self):
        workload = self.store.register_workload(
            1001,
            describe_workload("python train.py --token very-secret"),
        )
        self.store.append_rollups([self.record(workload)])

        with self.identity():
            first = export_cluster_history(
                self.archive,
                self.config,
                start=self.start,
                end=self.end,
                resolution="10m",
                api=self.api,
            )
            second = export_cluster_history(
                self.archive,
                self.config,
                start=self.start,
                end=self.end,
                resolution="10m",
                api=self.api,
            )
            verified = verify_cluster_history(
                self.archive,
                expected_node_ids={self.node["id"]},
            )
            archived, report = load_archived_user_usage(
                self.archive,
                start=self.start,
                end=self.end,
                node_ids=[self.node["id"]],
            )

        self.assertEqual(first["status"], "exported")
        self.assertEqual(second["status"], "exists")
        self.assertEqual(verified["generations"], 1)
        self.assertEqual(verified["files"], 2)
        self.assertEqual(report["chunks"], 1)
        self.assertEqual(archived[0].payload["users"][0]["uid"], 1001)
        self.assertEqual(archived[0].payload["users"][0]["active_gpu_seconds"], 60)

        generation = self.archive / self.node["id"] / first["generation"]
        self.assertEqual(generation.stat().st_mode & 0o777, 0o555)
        self.assertEqual((generation / "manifest.json").stat().st_mode & 0o777, 0o444)
        exported_text = ""
        for path in generation.glob("*.json.gz"):
            exported_text += gzip.decompress(path.read_bytes()).decode("utf-8")
        self.assertNotIn("very-secret", exported_text)
        self.assertNotIn("--token", exported_text)
        self.assertIn("train.py", exported_text)

    def test_rejects_overlapping_generation_and_payload_tampering(self):
        with self.identity():
            result = export_cluster_history(
                self.archive,
                self.config,
                start=self.start,
                end=self.end,
                api=self.api,
            )
            with self.assertRaisesRegex(BookingError, "overlaps existing"):
                export_cluster_history(
                    self.archive,
                    self.config,
                    start=self.start,
                    end=self.end + timedelta(days=1),
                    resolution="1h",
                    api=self.api,
                )

        generation = self.archive / self.node["id"] / result["generation"]
        payload = next(generation.glob("*.json.gz"))
        os.chmod(payload, 0o644)
        changed = bytearray(payload.read_bytes())
        changed[-1] ^= 1
        payload.write_bytes(changed)
        os.chmod(payload, 0o444)
        with self.assertRaisesRegex(BookingError, "checksum mismatch"):
            verify_cluster_history(self.archive, expected_node_ids={self.node["id"]})

    def test_rejects_symlinked_root_and_private_public_payload_fields(self):
        linked = self.root / "linked"
        linked.symlink_to(self.archive, target_is_directory=True)
        with self.identity(), self.assertRaisesRegex(BookingError, "symlink"):
            export_cluster_history(
                linked,
                self.config,
                start=self.start,
                end=self.end,
                api=self.api,
            )

        class UnsafeAPI:
            def samples(inner, **_kwargs):
                return self.payload("usage-samples", records=[], command="secret")

        with self.identity(), self.assertRaisesRegex(BookingError, "private field"):
            export_cluster_history(
                self.archive,
                self.config,
                start=self.start,
                end=self.end,
                api=UnsafeAPI(),
            )
        namespace = self.archive / self.node["id"]
        self.assertEqual(
            [item.name for item in namespace.iterdir() if item.name.startswith(".tmp-")],
            [],
        )

    def test_incremental_window_starts_after_latest_complete_generation(self):
        with self.identity():
            export_cluster_history(
                self.archive,
                self.config,
                start=self.start,
                end=self.end,
                api=self.api,
            )
            start, end = resolve_history_window(
                self.archive,
                self.node["id"],
                since="30d",
                now=self.end + timedelta(days=2, hours=12),
                incremental=True,
            )
        self.assertEqual(start, self.end)
        self.assertEqual(end, self.end + timedelta(days=2))

        start, end = resolve_history_window(
            self.archive,
            self.node["id"],
            since="1d",
            now=self.end + timedelta(days=60, hours=12),
            incremental=True,
        )
        self.assertEqual(start, self.end)
        self.assertEqual(end, self.end + timedelta(days=60))

        start, end = resolve_history_window(
            self.archive,
            self.node["id"],
            since="30d",
            now=self.end + timedelta(hours=12),
            incremental=True,
        )
        self.assertEqual(start, end)

    def test_export_cleans_a_safe_stale_temporary_generation(self):
        namespace = self.archive / self.node["id"]
        namespace.mkdir(mode=0o755)
        stale = namespace / (
            ".tmp-20300101T000000Z-20300102T000000Z-10m-" + "b" * 32
        )
        stale.mkdir(mode=0o700)
        payload = stale / "day-00000-samples.json.gz"
        payload.write_bytes(b"incomplete")
        payload.chmod(0o444)
        stale.chmod(0o555)

        with self.identity():
            result = export_cluster_history(
                self.archive,
                self.config,
                start=self.start,
                end=self.end,
                resolution="10m",
                api=self.api,
            )

        self.assertEqual(result["status"], "exported")
        self.assertFalse(stale.exists())

    def test_export_rejects_an_unrecognized_temporary_entry(self):
        namespace = self.archive / self.node["id"]
        namespace.mkdir(mode=0o755)
        (namespace / ".tmp-unrecognized").mkdir(mode=0o700)

        with self.identity(), self.assertRaisesRegex(
            BookingError,
            "unexpected temporary entry",
        ):
            export_cluster_history(
                self.archive,
                self.config,
                start=self.start,
                end=self.end,
                resolution="10m",
                api=self.api,
            )

    def test_stale_history_cleanup_never_follows_a_temporary_symlink(self):
        namespace = self.archive / self.node["id"]
        namespace.mkdir(mode=0o755)
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "keep"
        marker.write_text("safe")
        stale = namespace / (
            ".tmp-20300101T000000Z-20300102T000000Z-10m-" + "c" * 32
        )
        stale.symlink_to(outside, target_is_directory=True)

        with self.identity(), self.assertRaisesRegex(
            BookingError,
            "cannot safely remove stale history export directory",
        ):
            export_cluster_history(
                self.archive,
                self.config,
                start=self.start,
                end=self.end,
                resolution="10m",
                api=self.api,
            )

        self.assertTrue(stale.is_symlink())
        self.assertEqual(marker.read_text(), "safe")

    def test_best_effort_temporary_cleanup_never_masks_the_export_error(self):
        temporary = self.root / "temporary"
        temporary.mkdir()
        with mock.patch(
            "bk.cluster_history._directory_entries",
            side_effect=BookingError("cannot inspect incomplete export"),
        ):
            self.assertFalse(_remove_temporary_generation(temporary))

    def test_public_queries_are_month_batched_and_split_only_when_bounded(self):
        calls = []

        class BatchAPI:
            def samples(inner, **kwargs):
                calls.append((kwargs["start"], kwargs["end"]))
                return self.sample_payload(kwargs["start"], kwargs["end"])

        with self.identity():
            batches = list(
                _query_sample_batches(
                    BatchAPI(),
                    self.start,
                    self.start + timedelta(days=61),
                    "10m",
                    self.node["id"],
                )
            )
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(batches), 3)

        calls.clear()

        class SplittingAPI:
            def samples(inner, **kwargs):
                calls.append((kwargs["start"], kwargs["end"]))
                truncated = kwargs["end"] - kwargs["start"] > timedelta(days=1)
                return self.sample_payload(
                    kwargs["start"],
                    kwargs["end"],
                    truncated=truncated,
                )

        with self.identity():
            batches = list(
                _query_sample_batches(
                    SplittingAPI(),
                    self.start,
                    self.start + timedelta(days=2),
                    "10m",
                    self.node["id"],
                )
            )
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(batches), 2)

    def payload(self, kind, **values):
        collection = "users" if kind == "usage-users" else "records"
        payload = {
            "schema_version": "gpubk.usage.v1",
            "kind": kind,
            "generated_at": self.start.isoformat(),
            "node": self.node,
            "collector": {},
            "query": {
                "start_at": self.start.isoformat().replace("+00:00", "Z"),
                "end_at": self.end.isoformat().replace("+00:00", "Z"),
                "resolution": "10m",
                "resolution_seconds": 600,
            },
            collection: values.pop(collection, []),
            "truncated": False,
            **values,
        }
        return payload

    def sample_payload(self, start, end, *, truncated=False):
        return {
            "schema_version": "gpubk.usage.v1",
            "kind": "usage-samples",
            "generated_at": self.start.isoformat(),
            "node": self.node,
            "collector": {},
            "query": {
                "start_at": start.isoformat().replace("+00:00", "Z"),
                "end_at": end.isoformat().replace("+00:00", "Z"),
                "resolution": "10m",
                "resolution_seconds": 600,
                "limit": 200_000,
            },
            "records": [],
            "truncated": truncated,
            "warnings": [],
        }

    def identity(self):
        return _IdentityPatches(self.node)

    def record(self, workload_id):
        return {
            "window_start": self.start.isoformat().replace("+00:00", "Z"),
            "window_end": (self.start + timedelta(minutes=1)).isoformat().replace(
                "+00:00", "Z"
            ),
            "partial": False,
            "gpu": 0,
            "uid": 1001,
            "username": "alice",
            "status": "ok",
            "reservation_ids": ["reservation-1"],
            "sample_count": 30,
            "observed_seconds": 60,
            "active_sample_count": 30,
            "active_observed_seconds": 60,
            "avg_process_count": 1,
            "max_process_count": 1,
            "sm_sample_count": 30,
            "avg_sm_percent": 50,
            "max_sm_percent": 70,
            "avg_gpu_memory_mb": 4096,
            "max_gpu_memory_mb": 6144,
            "device_util_sample_count": 30,
            "avg_device_util_percent": 60,
            "max_device_util_percent": 80,
            "workload_ids": [workload_id],
            "workload_observed_seconds": {str(workload_id): 60},
        }


class _IdentityPatches:
    def __init__(self, identity):
        self.patches = (
            mock.patch("bk.cluster_history.stable_node_identity", return_value=identity),
            mock.patch("bk.usage_api.stable_node_identity", return_value=identity),
        )

    def __enter__(self):
        for patcher in self.patches:
            patcher.start()
        return self

    def __exit__(self, *args):
        for patcher in reversed(self.patches):
            patcher.stop()


if __name__ == "__main__":
    unittest.main()
