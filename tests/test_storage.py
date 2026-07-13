import json
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bk.config import Config
from bk.models import Actor, BookingError, BookingRequest, EditRequest
from bk.scheduler import add_booking, edit_booking
from bk.storage import FileLock, LedgerStore
from bk.timeparse import parse_iso


def _reservation(reservation_id):
    return {
        "id": reservation_id,
        "uid": 1001,
        "username": "user1001",
        "gpus": [0],
        "mode": "shared",
        "start_at": "2030-01-01T00:00:00Z",
        "end_at": "2030-01-01T01:00:00Z",
        "status": "active",
        "created_at": "2029-12-31T23:00:00Z",
        "updated_at": "2029-12-31T23:00:00Z",
    }


def _concurrent_booking(data_dir, start_at, uid, result_queue):
    config = Config(data_dir=Path(data_dir), gpu_count=1, max_shared_users=2)
    store = LedgerStore(config.data_dir)
    try:
        add_booking(
            store,
            config,
            BookingRequest(
                actor=Actor(uid, f"user{uid}"),
                count=1,
                duration_seconds=30 * 60,
                start_at=parse_iso(start_at),
                preferred_gpus=[0],
            ),
        )
        result_queue.put("ok")
    except BookingError:
        result_queue.put("conflict")


def _concurrent_weighted_booking(data_dir, start_at, uid, result_queue):
    config = Config(data_dir=Path(data_dir), gpu_count=1, max_shared_users=4)
    store = LedgerStore(config.data_dir)
    try:
        add_booking(
            store,
            config,
            BookingRequest(
                actor=Actor(uid, f"user{uid}"),
                count=1,
                duration_seconds=30 * 60,
                start_at=parse_iso(start_at),
                preferred_gpus=[0],
                share_units=3,
            ),
        )
        result_queue.put("ok")
    except BookingError:
        result_queue.put("conflict")


def _concurrent_idempotent_edit(data_dir, reservation_id, result_queue):
    config = Config(data_dir=Path(data_dir), gpu_count=1, max_shared_users=2)
    store = LedgerStore(config.data_dir)
    try:
        result = edit_booking(
            store,
            config,
            EditRequest(
                actor=Actor(1001, "alice"),
                reservation_id=reservation_id,
                op_id="concurrent-agent-edit",
                duration_seconds=45 * 60,
            ),
        )
        result_queue.put("updated" if result.created else "exists")
    except BookingError as exc:
        result_queue.put(f"error:{exc}")


class LedgerStorageTests(unittest.TestCase):
    def test_unheld_file_lock_uses_explicit_runtime_guard(self):
        lock = FileLock(Path("unused.lock"), timeout_seconds=1)

        with self.assertRaisesRegex(RuntimeError, "file lock is not held"):
            lock._write_metadata()
        with self.assertRaisesRegex(RuntimeError, "file lock is not held"):
            lock.__exit__(None, None, None)

    def test_file_lock_releases_descriptor_when_metadata_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata-failure.lock"
            broken = FileLock(path, timeout_seconds=0)

            with mock.patch.object(broken, "_write_metadata", side_effect=OSError("disk failure")):
                with self.assertRaisesRegex(OSError, "disk failure"):
                    broken.__enter__()

            self.assertIsNone(broken._fh)
            with FileLock(path, timeout_seconds=0):
                pass

    def test_file_lock_rejects_existing_mode_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.lock"
            path.write_text("keep", encoding="utf-8")
            path.chmod(0o644)

            with self.assertRaisesRegex(PermissionError, "expected 0600"):
                with FileLock(path, timeout_seconds=0):
                    pass

            self.assertEqual(path.read_text(encoding="utf-8"), "keep")

    def test_empty_load_is_side_effect_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "not-created"
            store = LedgerStore(data_dir)

            self.assertEqual(store.load(), {"version": 1, "reservations": []})
            self.assertFalse(data_dir.exists())

    def test_symbolic_link_lock_is_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = LedgerStore(data_dir)
            store.ensure()
            target = Path(tmp) / "victim"
            target.write_text("keep", encoding="utf-8")
            store.lock_path.symlink_to(target)
            mutator = mock.Mock()

            with self.assertRaises(OSError):
                store.transaction(mutator)

            mutator.assert_not_called()
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_symbolic_link_log_is_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = LedgerStore(data_dir)
            store.ensure()
            target = Path(tmp) / "victim"
            target.write_text("keep", encoding="utf-8")
            store.log_path.symlink_to(target)
            mutator = mock.Mock()

            with self.assertRaises(OSError):
                store.transaction(mutator)

            mutator.assert_not_called()
            self.assertFalse(store.ledger_path.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_transaction_rejects_ledger_mode_drift_before_mutator(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            store.ledger_path.write_text(
                json.dumps({"version": 1, "reservations": []}),
                encoding="utf-8",
            )
            store.ledger_path.chmod(0o644)
            mutator = mock.Mock()

            with self.assertRaisesRegex(PermissionError, "expected 0600"):
                store.transaction(mutator)

            mutator.assert_not_called()
            self.assertEqual(stat.S_IMODE(store.ledger_path.stat().st_mode), 0o644)

    def test_transaction_rejects_hard_linked_log_before_mutator(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            store.log_path.write_text('{"action":"keep"}\n', encoding="utf-8")
            store.log_path.chmod(0o600)
            alias = Path(tmp) / "outside-alias"
            os.link(store.log_path, alias)
            mutator = mock.Mock()

            with self.assertRaisesRegex(OSError, "2 hard links"):
                store.transaction(mutator)

            mutator.assert_not_called()
            self.assertEqual(alias.read_text(encoding="utf-8"), '{"action":"keep"}\n')

    def test_symbolic_link_backup_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir(mode=0o700)
            target = Path(tmp) / "outside"
            target.mkdir()
            (data_dir / "backups").symlink_to(target, target_is_directory=True)
            store = LedgerStore(data_dir)

            with self.assertRaises(NotADirectoryError):
                store.transaction(lambda ledger: (ledger, None, [], False))

            self.assertEqual(list(target.iterdir()), [])

    def test_health_reports_symbolic_links_without_following_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir(mode=0o700)
            outside_file = Path(tmp) / "outside-ledger"
            outside_file.write_text("keep", encoding="utf-8")
            outside_dir = Path(tmp) / "outside-backups"
            outside_dir.mkdir()
            (data_dir / "ledger.json").symlink_to(outside_file)
            (data_dir / "backups").symlink_to(outside_dir, target_is_directory=True)

            issues = LedgerStore(data_dir).health_issues()
            by_path = {item["path"]: item for item in issues if "path" in item}

            self.assertEqual(by_path[str(data_dir / "ledger.json")]["type"], "file-type")
            self.assertEqual(
                by_path[str(data_dir / "ledger.json")]["actual"],
                "symbolic-link",
            )
            self.assertEqual(by_path[str(data_dir / "backups")]["type"], "directory-type")
            self.assertEqual(outside_file.read_text(encoding="utf-8"), "keep")
            self.assertEqual(list(outside_dir.iterdir()), [])

    def test_health_reports_hard_linked_managed_files_and_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = LedgerStore(data_dir)
            store.ensure()
            store.log_path.write_text("{}\n", encoding="utf-8")
            store.log_path.chmod(0o600)
            os.link(store.log_path, data_dir / "log-alias")
            backup = store.backup_dir / "ledger-one.json"
            backup.write_text('{"version":1,"reservations":[]}\n', encoding="utf-8")
            backup.chmod(0o600)
            os.link(backup, data_dir / "backup-alias")

            issues = store.health_issues()
            linked_paths = {
                item["path"]
                for item in issues
                if item.get("type") == "file-links"
            }

            self.assertEqual(linked_paths, {str(store.log_path), str(backup)})

    def test_read_only_load_clears_a_stale_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            store.last_warning = "stale warning from an earlier operation"

            self.assertEqual(store.load_read_only()["reservations"], [])
            self.assertIsNone(store.last_warning)

    def test_atomic_files_keep_configured_shared_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "shared"
            store = LedgerStore(data_dir, file_mode=0o660, dir_mode=0o2770)

            def mutate(ledger):
                ledger["reservations"].append(_reservation("one"))
                return ledger, "ok", [{"action": "test"}], True

            self.assertEqual(store.transaction(mutate), "ok")

            self.assertEqual(stat.S_IMODE(data_dir.stat().st_mode), 0o2770)
            self.assertEqual(stat.S_IMODE(store.ledger_path.stat().st_mode), 0o660)
            self.assertEqual(stat.S_IMODE(store.log_path.stat().st_mode), 0o660)
            self.assertEqual(stat.S_IMODE(store.lock_path.stat().st_mode), 0o660)
            backup = next(store.backup_dir.glob("ledger-*.json"))
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o660)
            expected_gid = data_dir.stat().st_gid
            self.assertTrue(
                all(
                    path.stat().st_gid == expected_gid
                    for path in (
                        store.backup_dir,
                        store.ledger_path,
                        store.log_path,
                        store.lock_path,
                        backup,
                    )
                )
            )

    def test_health_reports_managed_file_gid_drift_in_setgid_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "shared"
            store = LedgerStore(data_dir, file_mode=0o660, dir_mode=0o2770)

            def mutate(ledger):
                ledger["reservations"].append(_reservation("gid-drift"))
                return ledger, None, [{"action": "test"}], True

            store.transaction(mutate)
            expected_gid = data_dir.stat().st_gid
            original_lstat = Path.lstat

            def drifted_lstat(path):
                metadata = original_lstat(path)
                if path == store.log_path:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_nlink=metadata.st_nlink,
                        st_gid=expected_gid + 1,
                    )
                return metadata

            with mock.patch.object(Path, "lstat", autospec=True, side_effect=drifted_lstat):
                issues = store.health_issues()

            issue = next(item for item in issues if item.get("path") == str(store.log_path))
            self.assertEqual(issue["type"], "file-gid")
            self.assertEqual(issue["expected_gid"], expected_gid)
            self.assertEqual(issue["actual_gid"], expected_gid + 1)

    def test_transaction_rejects_managed_gid_drift_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "shared"
            store = LedgerStore(data_dir, file_mode=0o660, dir_mode=0o2770)

            def seed(ledger):
                ledger["reservations"].append(_reservation("gid-seed"))
                return ledger, None, [{"action": "seed"}], True

            store.transaction(seed)
            original_ledger = store.ledger_path.read_bytes()
            original_log = store.log_path.read_bytes()
            log_inode = store.log_path.stat().st_ino
            real_fstat = os.fstat
            mutator = mock.Mock()

            def drifted_fstat(fd):
                metadata = real_fstat(fd)
                if metadata.st_ino == log_inode:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_nlink=metadata.st_nlink,
                        st_gid=metadata.st_gid + 1,
                    )
                return metadata

            with mock.patch("bk.fileio.os.fstat", side_effect=drifted_fstat):
                with self.assertRaisesRegex(PermissionError, "GID"):
                    store.transaction(mutator)

            mutator.assert_not_called()
            self.assertEqual(store.ledger_path.read_bytes(), original_ledger)
            self.assertEqual(store.log_path.read_bytes(), original_log)

    def test_configured_storage_gid_rejects_the_wrong_data_directory_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "shared"
            data_dir.mkdir(mode=0o700)
            data_dir.chmod(0o2770)
            actual_gid = data_dir.stat().st_gid
            store = LedgerStore(
                data_dir,
                file_mode=0o660,
                dir_mode=0o2770,
                storage_gid=actual_gid + 1,
            )
            mutator = mock.Mock()

            with self.assertRaisesRegex(PermissionError, "configured storage_gid"):
                store.transaction(mutator)

            mutator.assert_not_called()
            self.assertFalse(store.backup_dir.exists())
            issue = next(
                item
                for item in store.health_issues()
                if item.get("path") == str(data_dir) and item["type"] == "directory-gid"
            )
            self.assertEqual(issue["expected_gid"], actual_gid + 1)
            self.assertEqual(issue["actual_gid"], actual_gid)

    def test_atomic_ledger_write_rejects_bad_inherited_gid_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "shared"
            store = LedgerStore(data_dir, file_mode=0o660, dir_mode=0o2770)
            store.ensure()
            expected_gid = data_dir.stat().st_gid
            real_fstat = os.fstat

            def bad_regular_gid(fd):
                metadata = real_fstat(fd)
                if stat.S_ISREG(metadata.st_mode):
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_nlink=metadata.st_nlink,
                        st_gid=expected_gid + 1,
                    )
                return metadata

            with mock.patch("bk.storage.os.fstat", side_effect=bad_regular_gid):
                with self.assertRaisesRegex(PermissionError, "temporary file"):
                    store._atomic_write_ledger({"version": 1, "reservations": []})

            self.assertFalse(store.ledger_path.exists())
            self.assertEqual(list(data_dir.glob(".ledger.*.tmp")), [])

    def test_durable_journal_recovers_after_apply_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = LedgerStore(data_dir)

            def mutate(ledger):
                ledger["reservations"].append(_reservation("durable"))
                return ledger, "accepted", [{"action": "add", "reservation_id": "durable"}], True

            with mock.patch.object(store, "_apply_journal_unlocked", side_effect=OSError("injected failure")):
                result = store.transaction(mutate)

            self.assertEqual(result, "accepted")
            self.assertTrue(store.journal_path.exists())
            self.assertIn("deferred recovery", store.last_warning)

            recovered = LedgerStore(data_dir)
            ledger = recovered.load()
            self.assertEqual([item["id"] for item in ledger["reservations"]], ["durable"])
            self.assertFalse(recovered.journal_path.exists())
            lines = recovered.log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["reservation_id"], "durable")

    def test_journal_directory_fsync_failure_is_accepted_for_deferred_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = LedgerStore(data_dir)

            def mutate(ledger):
                ledger["reservations"].append(_reservation("journal-sync"))
                return ledger, "accepted", [{"action": "add"}], True

            with mock.patch(
                "bk.storage.fsync_directory",
                side_effect=OSError("journal directory sync failed"),
            ):
                result = store.transaction(mutate)

            self.assertEqual(result, "accepted")
            self.assertTrue(store.journal_path.exists())
            self.assertFalse(store.ledger_path.exists())
            self.assertIn("deferred recovery", store.last_warning)
            self.assertIn("journal directory sync failed", store.last_warning)

            recovered = LedgerStore(data_dir)
            ledger = recovered.load()
            self.assertEqual([item["id"] for item in ledger["reservations"]], ["journal-sync"])
            self.assertFalse(recovered.journal_path.exists())
            self.assertEqual(len(recovered.log_path.read_text(encoding="utf-8").splitlines()), 1)

    def test_ledger_directory_fsync_failure_retains_journal_for_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = LedgerStore(data_dir)

            def mutate(ledger):
                ledger["reservations"].append(_reservation("ledger-sync"))
                return ledger, "accepted", [{"action": "add"}], True

            with mock.patch(
                "bk.storage.fsync_directory",
                side_effect=[None, OSError("ledger directory sync failed")],
            ):
                result = store.transaction(mutate)

            self.assertEqual(result, "accepted")
            self.assertTrue(store.journal_path.exists())
            self.assertTrue(store.ledger_path.exists())
            self.assertIn("deferred recovery", store.last_warning)
            self.assertIn("ledger directory sync failed", store.last_warning)

            recovered = LedgerStore(data_dir)
            ledger = recovered.load()
            self.assertEqual([item["id"] for item in ledger["reservations"]], ["ledger-sync"])
            self.assertFalse(recovered.journal_path.exists())
            self.assertEqual(len(recovered.log_path.read_text(encoding="utf-8").splitlines()), 1)

    def test_recovery_does_not_duplicate_an_already_appended_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = LedgerStore(data_dir)
            journal = {
                "version": 1,
                "transaction_id": "tx-one",
                "created_at": "2030-01-01T00:00:00Z",
                "ledger": {
                    "version": 1,
                    "last_transaction_id": "tx-one",
                    "reservations": [_reservation("one")],
                },
                "logs": [
                    {
                        "event_id": "event-one",
                        "transaction_id": "tx-one",
                        "action": "add",
                    }
                ],
            }
            store.ensure()
            store._write_journal(journal)
            store._append_missing_logs(journal["logs"])

            store.load()
            store.load()

            lines = store.log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["event_id"], "event-one")

    def test_recovery_deduplicates_an_audit_batch_larger_than_one_mebibyte(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            logs = [
                {
                    "event_id": f"event-{index}",
                    "transaction_id": "large-transaction",
                    "uid": 1001,
                    "action": "test",
                    "result": "ok",
                    "message": str(index) * 450_000,
                }
                for index in range(3)
            ]
            journal = {
                "version": 1,
                "transaction_id": "large-transaction",
                "created_at": "2030-01-01T00:00:00Z",
                "ledger": None,
                "logs": logs,
            }
            store.ensure()
            store._write_journal(journal)
            store._append_missing_logs(logs)
            self.assertGreater(store.log_path.stat().st_size, 1024 * 1024)

            store.load()

            events = [json.loads(line) for line in store.log_path.read_bytes().splitlines()]
            self.assertEqual([item["event_id"] for item in events], [
                "event-0",
                "event-1",
                "event-2",
            ])
            self.assertFalse(store.journal_path.exists())

    def test_audit_append_discards_only_an_incomplete_trailing_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            first = {"uid": 1001, "action": "add", "result": "created"}
            second = {"uid": 1001, "action": "edit", "result": "updated"}

            store.transaction(lambda ledger: (ledger, "first", [first], False))
            with store.log_path.open("ab") as fh:
                fh.write(b'{"partial"')

            result = store.transaction(lambda ledger: (ledger, "second", [second], False))

            self.assertEqual(result, "second")
            events = [json.loads(line) for line in store.log_path.read_bytes().splitlines()]
            self.assertEqual([item["action"] for item in events], ["add", "edit"])
            self.assertIn("discarded an incomplete trailing audit record", store.last_warning)
            self.assertFalse(store.journal_path.exists())

    def test_audit_append_restores_a_valid_final_record_missing_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            first = {"uid": 1001, "action": "add", "result": "created"}
            second = {"uid": 1001, "action": "edit", "result": "updated"}

            store.transaction(lambda ledger: (ledger, None, [first], False))
            store.log_path.write_bytes(store.log_path.read_bytes().removesuffix(b"\n"))
            store.transaction(lambda ledger: (ledger, None, [second], False))

            events = [json.loads(line) for line in store.log_path.read_bytes().splitlines()]
            self.assertEqual([item["action"] for item in events], ["add", "edit"])
            self.assertIn("restored a missing final newline", store.last_warning)

    def test_failed_audit_append_keeps_journal_and_recovers_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = LedgerStore(data_dir)
            first = {"uid": 1001, "action": "add", "result": "created"}
            second = {"uid": 1001, "action": "edit", "result": "updated"}
            store.transaction(lambda ledger: (ledger, None, [first], False))
            original = store.log_path.read_bytes()

            real_write = os.write
            calls = 0

            def fail_after_partial_write(fd, payload):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return real_write(fd, bytes(payload[:7]))
                raise OSError("simulated audit disk failure")

            with mock.patch("bk.jsonl.os.write", side_effect=fail_after_partial_write):
                result = store.transaction(lambda ledger: (ledger, "accepted", [second], False))

            self.assertEqual(result, "accepted")
            self.assertEqual(store.log_path.read_bytes(), original)
            self.assertTrue(store.journal_path.exists())
            self.assertIn("deferred recovery", store.last_warning)

            recovered = LedgerStore(data_dir)
            recovered.load()
            events = [json.loads(line) for line in recovered.log_path.read_bytes().splitlines()]
            self.assertEqual([item["action"] for item in events], ["add", "edit"])
            self.assertEqual(len({item["event_id"] for item in events}), 2)
            self.assertFalse(recovered.journal_path.exists())

    def test_invalid_journal_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            store.ensure()
            store.journal_path.write_text("not-json", encoding="utf-8")
            store.journal_path.chmod(0o600)

            with self.assertRaisesRegex(OSError, "invalid transaction journal"):
                store.load()

            self.assertTrue(store.journal_path.exists())

    def test_invalid_journal_log_identity_is_rejected_before_ledger_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            store.ensure()
            journal = {
                "version": 1,
                "transaction_id": "tx-invalid-log",
                "created_at": "2030-01-01T00:00:00Z",
                "ledger": {
                    "version": 1,
                    "last_transaction_id": "tx-invalid-log",
                    "reservations": [_reservation("must-not-apply")],
                },
                "logs": [{"transaction_id": "tx-invalid-log", "action": "add"}],
            }
            store.journal_path.write_text(json.dumps(journal), encoding="utf-8")
            store.journal_path.chmod(0o600)

            with self.assertRaisesRegex(OSError, "event ID is invalid"):
                store.load()

            self.assertFalse(store.ledger_path.exists())
            self.assertTrue(store.journal_path.exists())

    def test_oversized_audit_event_is_rejected_before_journal_or_ledger_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))

            def mutate(ledger):
                ledger["reservations"].append(_reservation("must-not-apply"))
                return ledger, None, [{"action": "add", "message": "x" * (1024 * 1024)}], True

            with self.assertRaisesRegex(ValueError, "audit record exceeds"):
                store.transaction(mutate)

            self.assertFalse(store.ledger_path.exists())
            self.assertFalse(store.log_path.exists())
            self.assertFalse(store.journal_path.exists())

    def test_invalid_reservation_is_rejected_before_journal_or_ledger_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))

            def mutate(ledger):
                ledger["reservations"].append({"id": "incomplete"})
                return ledger, None, [], True

            with self.assertRaisesRegex(ValueError, r"reservations\[0\]\.uid"):
                store.transaction(mutate)

            self.assertFalse(store.ledger_path.exists())
            self.assertFalse(store.journal_path.exists())
            self.assertEqual(list(store.backup_dir.glob("ledger-*.json")), [])

    def test_invalid_ledger_without_backup_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            store.ensure()
            store.ledger_path.write_text("not-json", encoding="utf-8")
            store.ledger_path.chmod(0o600)
            original = store.ledger_path.read_bytes()
            mutator = mock.Mock()

            with self.assertRaisesRegex(OSError, "no valid backup exists"):
                store.load()
            with self.assertRaisesRegex(OSError, "no valid backup exists"):
                store.transaction(mutator)

            mutator.assert_not_called()
            self.assertEqual(store.ledger_path.read_bytes(), original)

    def test_invalid_ledger_loads_latest_valid_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))

            def mutate(ledger):
                ledger["reservations"].append(_reservation("backed-up"))
                return ledger, None, [], True

            store.transaction(mutate)
            store.ledger_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "reservations": [{"id": "semantically-invalid"}],
                    }
                ),
                encoding="utf-8",
            )

            restored = store.load()

            self.assertEqual([item["id"] for item in restored["reservations"]], ["backed-up"])
            self.assertIn("latest valid backup", store.last_warning)
            self.assertIn("reservations[0].uid", store.last_warning)

    def test_valid_journal_recovers_over_an_invalid_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            store.ensure()
            store.ledger_path.write_text("not-json", encoding="utf-8")
            store.ledger_path.chmod(0o600)
            journal = {
                "version": 1,
                "transaction_id": "repair-transaction",
                "created_at": "2030-01-01T00:00:00Z",
                "ledger": {
                    "version": 1,
                    "last_transaction_id": "repair-transaction",
                    "reservations": [_reservation("recovered")],
                },
                "logs": [],
            }
            store._write_journal(journal)

            recovered = store.load()

            self.assertEqual([item["id"] for item in recovered["reservations"]], ["recovered"])
            self.assertFalse(store.journal_path.exists())
            self.assertEqual(json.loads(store.ledger_path.read_text(encoding="utf-8")), recovered)

    def test_private_defaults_do_not_grant_group_or_world_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp) / "private")

            store.transaction(lambda ledger: (ledger, None, [{"action": "read"}], False))

            self.assertEqual(stat.S_IMODE(store.data_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(store.log_path.stat().st_mode), 0o600)

    def test_reset_counts_and_removes_audit_lines_with_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            store.ensure()
            store.log_path.write_bytes(b"valid\n\xff\n")
            store.log_path.chmod(0o600)

            result = store.reset()

            self.assertEqual(result["logs"], 2)
            self.assertFalse(store.log_path.exists())

    def test_reset_rejects_backup_mode_drift_before_clearing_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))

            def mutate(ledger):
                ledger["reservations"].append(_reservation("keep"))
                return ledger, None, [], True

            store.transaction(mutate)
            original = store.ledger_path.read_bytes()
            backup = next(store.backup_dir.glob("ledger-*.json"))
            backup.chmod(0o644)

            with self.assertRaisesRegex(PermissionError, "expected 0600"):
                store.reset()

            self.assertEqual(store.ledger_path.read_bytes(), original)
            self.assertTrue(backup.exists())

    def test_cross_process_booking_transaction_never_oversells_shared_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            start_at = "2030-01-01T00:00:00Z"
            context = multiprocessing.get_context("spawn")
            results = context.Queue()
            processes = [
                context.Process(
                    target=_concurrent_booking,
                    args=(str(data_dir), start_at, 1000 + index, results),
                )
                for index in range(8)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)

            outcomes = [results.get(timeout=1) for _process in processes]
            self.assertEqual(outcomes.count("ok"), 2)
            self.assertEqual(outcomes.count("conflict"), 6)
            store = LedgerStore(data_dir)
            self.assertEqual(len(store.load()["reservations"]), 2)
            events = [json.loads(line) for line in store.log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(events), 2)
            self.assertEqual(len({item["event_id"] for item in events}), 2)

    def test_cross_process_weighted_bookings_never_oversell_capacity_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            start_at = "2030-01-01T00:00:00Z"
            context = multiprocessing.get_context("spawn")
            results = context.Queue()
            processes = [
                context.Process(
                    target=_concurrent_weighted_booking,
                    args=(str(data_dir), start_at, 2000 + index, results),
                )
                for index in range(6)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)

            outcomes = [results.get(timeout=1) for _process in processes]
            self.assertEqual(outcomes.count("ok"), 1)
            self.assertEqual(outcomes.count("conflict"), 5)
            ledger = LedgerStore(data_dir).load()
            self.assertEqual(sum(item.get("share_units", 1) for item in ledger["reservations"]), 3)

    def test_cross_process_agent_edit_is_applied_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            config = Config(data_dir=data_dir, gpu_count=1, max_shared_users=2)
            store = LedgerStore(data_dir)
            created = add_booking(
                store,
                config,
                BookingRequest(
                    actor=Actor(1001, "alice"),
                    count=1,
                    duration_seconds=30 * 60,
                    start_at=parse_iso("2030-01-01T00:00:00Z"),
                ),
            )
            context = multiprocessing.get_context("spawn")
            results = context.Queue()
            processes = [
                context.Process(
                    target=_concurrent_idempotent_edit,
                    args=(str(data_dir), created.reservation["id"], results),
                )
                for _index in range(8)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)

            outcomes = [results.get(timeout=1) for _process in processes]
            self.assertEqual(outcomes.count("updated"), 1)
            self.assertEqual(outcomes.count("exists"), 7)
            self.assertFalse(any(item.startswith("error:") for item in outcomes))
            ledger = store.load()
            self.assertEqual(len(ledger["reservations"][0]["edit_operations"]), 1)
            events = [json.loads(line) for line in store.log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([item["action"] for item in events], ["add", "edit"])


if __name__ == "__main__":
    unittest.main()
