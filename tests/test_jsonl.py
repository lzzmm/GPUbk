import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bk.jsonl import READ_CHUNK_BYTES, append_json_objects, read_json_objects_tail


class JsonlTailTests(unittest.TestCase):
    def test_tail_filters_in_reverse_and_returns_matches_in_file_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            lines = [
                {"id": 1, "uid": 7},
                {"id": 2, "uid": 8},
                {"id": 3, "uid": 7},
                {"id": 4, "uid": 7},
            ]
            path.write_text(
                "".join(json.dumps(item) + "\n" for item in lines),
                encoding="utf-8",
            )

            result = read_json_objects_tail(
                path,
                limit=2,
                max_line_bytes=1024,
                predicate=lambda item: item.get("uid") == 7,
            )

            self.assertEqual([item["id"] for item in result.records], [3, 4])
            self.assertEqual(result.invalid_records, 0)
            self.assertFalse(result.scan_truncated)

    def test_tail_reports_malformed_oversized_and_unterminated_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_bytes(
                b'{"id":1}\n'
                + b"not-json\n"
                + b"x" * 200
                + b'\n{"id":2}'
            )

            result = read_json_objects_tail(path, limit=2, max_line_bytes=128)

            self.assertEqual([item["id"] for item in result.records], [1, 2])
            self.assertEqual(result.invalid_records, 1)
            self.assertEqual(result.oversized_records, 1)
            self.assertTrue(result.final_newline_missing)

    def test_oversized_line_spanning_chunks_does_not_hide_older_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_bytes(
                b'{"id":1}\n'
                + b"x" * (READ_CHUNK_BYTES * 2)
                + b'\n{"id":2}\n'
            )

            result = read_json_objects_tail(path, limit=2, max_line_bytes=1024)

            self.assertEqual([item["id"] for item in result.records], [1, 2])
            self.assertEqual(result.oversized_records, 1)

    def test_tail_scan_limit_is_explicit_when_no_match_is_reached(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            records = [{"id": 0, "uid": 7}] + [
                {"id": index, "uid": 8} for index in range(1, 40)
            ]
            path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )

            result = read_json_objects_tail(
                path,
                limit=1,
                max_line_bytes=1024,
                predicate=lambda item: item.get("uid") == 7,
                max_scan_bytes=128,
            )

            self.assertEqual(result.records, ())
            self.assertTrue(result.scan_truncated)

    def test_tail_skips_non_syntax_value_errors_from_json_decoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_bytes(b'{"id":1}\n{"id":2}\n')

            with mock.patch(
                "bk.jsonl.json.loads",
                side_effect=[ValueError("integer safety limit"), {"id": 1}],
            ):
                result = read_json_objects_tail(path, limit=1, max_line_bytes=1024)

            self.assertEqual([item["id"] for item in result.records], [1])
            self.assertEqual(result.invalid_records, 1)

    def test_append_surfaces_a_failed_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"

            with (
                mock.patch("bk.jsonl.os.write", side_effect=OSError("append failed")),
                mock.patch("bk.jsonl.os.ftruncate", side_effect=OSError("truncate failed")),
            ):
                with self.assertRaisesRegex(OSError, "append failed and rollback failed"):
                    append_json_objects(
                        path,
                        [{"id": 1}],
                        file_mode=0o600,
                        dir_mode=0o700,
                        max_line_bytes=1024,
                        max_file_bytes=None,
                        record_name="test",
                    )

    def test_append_rejects_non_objects_and_non_finite_numbers_before_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"

            with self.assertRaisesRegex(ValueError, "must be a JSON object"):
                append_json_objects(
                    path,
                    [[1, 2]],
                    file_mode=0o600,
                    dir_mode=0o700,
                    max_line_bytes=1024,
                    max_file_bytes=None,
                    record_name="test",
                )
            with self.assertRaisesRegex(ValueError, "Out of range float"):
                append_json_objects(
                    path,
                    [{"value": float("nan")}],
                    file_mode=0o600,
                    dir_mode=0o700,
                    max_line_bytes=1024,
                    max_file_bytes=None,
                    record_name="test",
                )

            self.assertFalse(path.exists())

    def test_append_rolls_back_when_directory_fsync_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            append_json_objects(
                path,
                [{"id": 1}],
                file_mode=0o600,
                dir_mode=0o700,
                max_line_bytes=1024,
                max_file_bytes=None,
                record_name="test",
            )
            original = path.read_bytes()

            with mock.patch(
                "bk.jsonl.fsync_directory",
                side_effect=OSError("directory sync failed"),
            ):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    append_json_objects(
                        path,
                        [{"id": 2}],
                        file_mode=0o600,
                        dir_mode=0o700,
                        max_line_bytes=1024,
                        max_file_bytes=None,
                        record_name="test",
                    )

            self.assertEqual(path.read_bytes(), original)

    def test_append_rejects_gid_drift_before_repairing_or_appending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_bytes(b'{"id":1}')
            path.chmod(0o600)
            original = path.read_bytes()
            target_inode = path.stat().st_ino
            expected_gid = path.stat().st_gid
            real_fstat = os.fstat

            def drifted_fstat(fd):
                metadata = real_fstat(fd)
                if metadata.st_ino == target_inode:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_nlink=metadata.st_nlink,
                        st_gid=expected_gid + 1,
                    )
                return metadata

            with mock.patch("bk.fileio.os.fstat", side_effect=drifted_fstat):
                with self.assertRaisesRegex(PermissionError, "GID"):
                    append_json_objects(
                        path,
                        [{"id": 2}],
                        file_mode=0o600,
                        dir_mode=0o700,
                        max_line_bytes=1024,
                        max_file_bytes=None,
                        record_name="test",
                        expected_gid=expected_gid,
                    )

            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
