import json
import multiprocessing
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bk.config import Config
from bk.models import Actor, BookingError, BookingRequest, EditRequest
from bk.scheduler import add_booking, edit_booking
from bk.storage import FileLock, LedgerStore
from bk.timeparse import parse_iso


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

    def test_symbolic_link_backup_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            target = Path(tmp) / "outside"
            target.mkdir()
            (data_dir / "backups").symlink_to(target, target_is_directory=True)
            store = LedgerStore(data_dir)

            with self.assertRaises(NotADirectoryError):
                store.transaction(lambda ledger: (ledger, None, [], False))

            self.assertEqual(list(target.iterdir()), [])

    def test_atomic_files_keep_configured_shared_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "shared"
            store = LedgerStore(data_dir, file_mode=0o660, dir_mode=0o2770)

            def mutate(ledger):
                ledger["reservations"].append({"id": "one"})
                return ledger, "ok", [{"action": "test"}], True

            self.assertEqual(store.transaction(mutate), "ok")

            self.assertEqual(stat.S_IMODE(data_dir.stat().st_mode), 0o2770)
            self.assertEqual(stat.S_IMODE(store.ledger_path.stat().st_mode), 0o660)
            self.assertEqual(stat.S_IMODE(store.log_path.stat().st_mode), 0o660)
            self.assertEqual(stat.S_IMODE(store.lock_path.stat().st_mode), 0o660)
            backup = next(store.backup_dir.glob("ledger-*.json"))
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o660)

    def test_durable_journal_recovers_after_apply_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = LedgerStore(data_dir)

            def mutate(ledger):
                ledger["reservations"].append({"id": "durable"})
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
                    "reservations": [{"id": "one"}],
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

    def test_invalid_journal_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            store.ensure()
            store.journal_path.write_text("not-json", encoding="utf-8")

            with self.assertRaisesRegex(OSError, "invalid transaction journal"):
                store.load()

            self.assertTrue(store.journal_path.exists())

    def test_invalid_ledger_without_backup_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            store.ensure()
            store.ledger_path.write_text("not-json", encoding="utf-8")
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
                ledger["reservations"].append({"id": "backed-up"})
                return ledger, None, [], True

            store.transaction(mutate)
            store.ledger_path.write_text("not-json", encoding="utf-8")

            restored = store.load()

            self.assertEqual([item["id"] for item in restored["reservations"]], ["backed-up"])
            self.assertIn("latest valid backup", store.last_warning)

    def test_valid_journal_recovers_over_an_invalid_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            store.ensure()
            store.ledger_path.write_text("not-json", encoding="utf-8")
            journal = {
                "version": 1,
                "transaction_id": "repair-transaction",
                "created_at": "2030-01-01T00:00:00Z",
                "ledger": {
                    "version": 1,
                    "last_transaction_id": "repair-transaction",
                    "reservations": [{"id": "recovered"}],
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
