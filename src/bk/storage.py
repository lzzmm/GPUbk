from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import stat
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple, TypeVar

from .fileio import (
    ensure_directory,
    file_type_name,
    fsync_directory,
    open_existing_regular,
    open_or_create_regular,
    setgid_directory_gid,
)
from .jsonl import (
    JsonlTailResult,
    append_json_objects,
    encode_json_object_line,
    read_json_objects_tail,
)
from .ledger_schema import validate_ledger_document
from .models import BookingError
from .policy import ledger_storage_modes


T = TypeVar("T")
AUDIT_SCHEMA_VERSION = "gpubk.audit.v1"
MAX_AUDIT_LINE_BYTES = 1024 * 1024
MAX_AUDIT_SCAN_BYTES = 64 * 1024 * 1024
AUDIT_TEXT_LIMITS = {
    "ts": 64,
    "username": 256,
    "action": 64,
    "reservation_id": 128,
    "op_id": 256,
    "mode": 32,
    "start_at": 64,
    "end_at": 64,
    "result": 64,
    "message": 4096,
    "transaction_id": 128,
    "event_id": 128,
}


def _empty_ledger() -> dict:
    return {"version": 1, "reservations": []}


class LedgerCorruptionError(OSError):
    pass


class FileLock:
    def __init__(
        self,
        path: Path,
        timeout_seconds: float,
        file_mode: int = 0o600,
        dir_mode: int = 0o700,
        expected_gid: Optional[int] = None,
    ):
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.file_mode = file_mode
        self.dir_mode = dir_mode
        self.expected_gid = expected_gid
        self._fh = None

    def __enter__(self):
        ensure_directory(
            self.path.parent,
            self.dir_mode,
            require_mode=True,
            expected_gid=self.expected_gid,
        )
        fd = open_or_create_regular(
            self.path,
            os.O_RDWR,
            self.file_mode,
            expected_gid=self.expected_gid,
        )
        self._fh = os.fdopen(fd, "r+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._write_metadata()
                    return self
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timeout waiting for lock {self.path}") from exc
                    time.sleep(0.05)
        except BaseException:
            self._fh.close()
            self._fh = None
            raise

    def _write_metadata(self) -> None:
        fh = self._require_handle()
        fh.seek(0)
        fh.truncate()
        payload = {"pid": os.getpid(), "locked_at": datetime.now(timezone.utc).isoformat()}
        fh.write(json.dumps(payload, ensure_ascii=False))
        fh.flush()
        os.fsync(fh.fileno())

    def __exit__(self, exc_type, exc, tb):
        fh = self._require_handle()
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
            self._fh = None

    def _require_handle(self):
        if self._fh is None:
            raise RuntimeError("file lock is not held")
        return self._fh


class LedgerStore:
    def __init__(
        self,
        data_dir: Path,
        lock_timeout_seconds: float = 10.0,
        backup_keep: int = 10,
        file_mode: int = 0o600,
        dir_mode: int = 0o700,
        storage_gid: Optional[int] = None,
    ):
        self.data_dir = data_dir
        self.lock_timeout_seconds = lock_timeout_seconds
        self.backup_keep = backup_keep
        self.file_mode = file_mode
        self.dir_mode = dir_mode
        self.storage_gid = storage_gid
        self.ledger_path = data_dir / "ledger.json"
        self.lock_path = data_dir / "ledger.lock"
        self.log_path = data_dir / "ops.log"
        self.backup_dir = data_dir / "backups"
        self.journal_path = data_dir / "transaction.json"
        self.last_warning: Optional[str] = None

    def ensure(self) -> None:
        ensure_directory(self.data_dir, self.dir_mode, require_mode=True)
        ensure_directory(
            self.backup_dir,
            self.dir_mode,
            require_mode=True,
            expected_gid=self._managed_gid(),
        )

    def load(self) -> dict:
        self.last_warning = None
        if os.path.lexists(self.journal_path):
            self.ensure()
            with self._lock():
                self._validate_managed_files_unlocked()
                self._recover_journal_unlocked()
        return self._load_unlocked()

    def load_read_only(self) -> dict:
        """Read the committed ledger snapshot without recovering pending writes."""
        self.last_warning = None
        return self._load_unlocked()

    def transaction(self, mutator: Callable[[dict], Tuple[dict, T, Iterable[dict], bool]]) -> T:
        self.ensure()
        self.last_warning = None
        self._preflight_storage_policy_on_mode_mismatch()
        with self._lock():
            self._validate_managed_files_unlocked()
            self._recover_journal_unlocked()
            ledger = self._load_unlocked(require_modes=True)
            new_ledger, result, logs, changed = mutator(ledger)
            log_items = list(logs)
            if not changed and not log_items:
                return result

            transaction_id = str(uuid.uuid4())
            prepared_logs = _prepare_logs(log_items, transaction_id)
            if changed:
                new_ledger["last_transaction_id"] = transaction_id
            journal = {
                "version": 1,
                "transaction_id": transaction_id,
                "ledger": new_ledger if changed else None,
                "logs": prepared_logs,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._validate_journal(journal)
            try:
                self._write_journal(journal)
            except OSError as exc:
                if not self._journal_matches_transaction(transaction_id):
                    raise
                self._add_warning(
                    "transaction accepted but deferred recovery is required after journal "
                    f"directory sync failed: {exc}"
                )
                return result

            try:
                self._apply_journal_unlocked(journal)
            except OSError as exc:
                self._add_warning(f"transaction accepted but deferred recovery is required: {exc}")
                return result

            self._clear_journal_best_effort()
            return result

    def reset(self) -> dict:
        self.ensure()
        self._preflight_storage_policy_on_mode_mismatch()
        with self._lock():
            self._validate_managed_files_unlocked()
            self._recover_journal_unlocked()
            previous = self._load_unlocked(require_modes=True)
            reservation_count = len(previous.get("reservations", []))
            log_count = _line_count(self.log_path)
            backup_paths = list(self.backup_dir.glob("ledger-*.json")) if self.backup_dir.exists() else []
            for path in backup_paths:
                fd = open_existing_regular(
                    path,
                    expected_mode=self.file_mode,
                    expected_gid=self._managed_gid(),
                )
                os.close(fd)
            backup_count = len(backup_paths)

            self._atomic_write_ledger(_empty_ledger())
            self.log_path.unlink(missing_ok=True)
            self.journal_path.unlink(missing_ok=True)
            if self.backup_dir.exists():
                for path in backup_paths:
                    path.unlink(missing_ok=True)
                fsync_directory(self.backup_dir)
            fsync_directory(self.data_dir)
            return {
                "reservations": reservation_count,
                "logs": log_count,
                "backups": backup_count,
            }

    def health_issues(self) -> List[dict]:
        issues = []
        if not os.path.lexists(self.data_dir):
            return issues
        try:
            metadata = self.data_dir.lstat()
        except OSError as exc:
            return [
                {
                    "type": "path-stat",
                    "path": str(self.data_dir),
                    "message": str(exc),
                }
            ]
        if file_type_name(metadata.st_mode) != "directory":
            return [
                {
                    "type": "directory-type",
                    "path": str(self.data_dir),
                    "expected": "directory",
                    "actual": file_type_name(metadata.st_mode),
                }
            ]
        actual = metadata.st_mode & 0o7777
        if actual != self.dir_mode:
            issues.append(
                {
                    "type": "directory-mode",
                    "path": str(self.data_dir),
                    "expected": f"{self.dir_mode:04o}",
                    "actual": f"{actual:04o}",
                }
            )
        if self.storage_gid is not None and metadata.st_gid != self.storage_gid:
            issues.append(
                {
                    "type": "directory-gid",
                    "path": str(self.data_dir),
                    "expected_gid": self.storage_gid,
                    "actual_gid": metadata.st_gid,
                    "message": "data directory does not match configured storage_gid",
                }
            )
        managed_gid = metadata.st_gid if self.dir_mode & stat.S_ISGID else None

        backup_dir_safe = False
        if os.path.lexists(self.backup_dir):
            try:
                metadata = self.backup_dir.lstat()
            except OSError as exc:
                issues.append(
                    {
                        "type": "path-stat",
                        "path": str(self.backup_dir),
                        "message": str(exc),
                    }
                )
            else:
                actual_type = file_type_name(metadata.st_mode)
                if actual_type != "directory":
                    issues.append(
                        {
                            "type": "directory-type",
                            "path": str(self.backup_dir),
                            "expected": "directory",
                            "actual": actual_type,
                        }
                    )
                else:
                    backup_dir_safe = True
                    group_issue = _managed_gid_issue(
                        self.backup_dir,
                        metadata,
                        managed_gid,
                        path_type="directory",
                    )
                    if group_issue is not None:
                        issues.append(group_issue)
                    actual = metadata.st_mode & 0o7777
                    if actual != self.dir_mode:
                        issues.append(
                            {
                                "type": "directory-mode",
                                "path": str(self.backup_dir),
                                "expected": f"{self.dir_mode:04o}",
                                "actual": f"{actual:04o}",
                            }
                        )

        for path in (
            self.ledger_path,
            self.lock_path,
            self.log_path,
            self.journal_path,
        ):
            if not os.path.lexists(path):
                continue
            try:
                metadata = path.lstat()
            except OSError as exc:
                issues.append(
                    {
                        "type": "path-stat",
                        "path": str(path),
                        "message": str(exc),
                    }
                )
                continue
            actual_type = file_type_name(metadata.st_mode)
            if actual_type != "regular-file":
                issues.append(
                    {
                        "type": "file-type",
                        "path": str(path),
                        "expected": "regular-file",
                        "actual": actual_type,
                    }
                )
                continue
            if path == self.journal_path:
                issues.append(
                    {
                        "type": "pending-journal",
                        "path": str(path),
                        "message": "a durable transaction is waiting for recovery",
                    }
                )
            group_issue = _managed_gid_issue(
                path,
                metadata,
                managed_gid,
                path_type="file",
            )
            if group_issue is not None:
                issues.append(group_issue)
            actual = metadata.st_mode & 0o777
            if actual != self.file_mode:
                issues.append(
                    {
                        "type": "file-mode",
                        "path": str(path),
                        "expected": f"{self.file_mode:04o}",
                        "actual": f"{actual:04o}",
                    }
                )
            if metadata.st_nlink != 1:
                issues.append(
                    {
                        "type": "file-links",
                        "path": str(path),
                        "expected": 1,
                        "actual": metadata.st_nlink,
                    }
                )

        if backup_dir_safe:
            try:
                backup_paths = list(self.backup_dir.glob("ledger-*.json"))
            except OSError as exc:
                issues.append(
                    {
                        "type": "path-scan",
                        "path": str(self.backup_dir),
                        "message": str(exc),
                    }
                )
            else:
                for path in backup_paths:
                    try:
                        metadata = path.lstat()
                    except OSError as exc:
                        issues.append(
                            {
                                "type": "path-stat",
                                "path": str(path),
                                "message": str(exc),
                            }
                        )
                        continue
                    actual_type = file_type_name(metadata.st_mode)
                    if actual_type != "regular-file":
                        issues.append(
                            {
                                "type": "file-type",
                                "path": str(path),
                                "expected": "regular-file",
                                "actual": actual_type,
                            }
                        )
                        continue
                    actual_mode = metadata.st_mode & 0o777
                    group_issue = _managed_gid_issue(
                        path,
                        metadata,
                        managed_gid,
                        path_type="file",
                    )
                    if group_issue is not None:
                        issues.append(group_issue)
                    if actual_mode != self.file_mode:
                        issues.append(
                            {
                                "type": "file-mode",
                                "path": str(path),
                                "expected": f"{self.file_mode:04o}",
                                "actual": f"{actual_mode:04o}",
                            }
                        )
                    if metadata.st_nlink != 1:
                        issues.append(
                            {
                                "type": "file-links",
                                "path": str(path),
                                "expected": 1,
                                "actual": metadata.st_nlink,
                            }
                        )

        unsafe_log = any(
            item.get("path") == str(self.log_path)
            and item.get("type") in {"file-type", "path-stat"}
            for item in issues
        )
        if os.path.lexists(self.log_path) and not unsafe_log:
            try:
                tail = read_json_objects_tail(
                    self.log_path,
                    limit=1,
                    max_line_bytes=MAX_AUDIT_LINE_BYTES,
                    max_scan_bytes=MAX_AUDIT_SCAN_BYTES,
                )
            except (OSError, ValueError) as exc:
                issues.append(
                    {
                        "type": "audit-log-read",
                        "path": str(self.log_path),
                        "message": str(exc),
                    }
                )
            else:
                details = _audit_tail_details(tail)
                if details:
                    issues.append(
                        {
                            "type": "audit-log-tail",
                            "path": str(self.log_path),
                            "message": "; ".join(details),
                        }
                    )
        return issues

    def recent_logs(self, uid: int, limit: int = 100) -> List[dict]:
        self.last_warning = None
        if not os.path.lexists(self.log_path):
            return []

        invalid_uid_records = 0

        def belongs_to_uid(item: dict) -> bool:
            nonlocal invalid_uid_records
            try:
                return int(item["uid"]) == uid
            except (KeyError, TypeError, ValueError):
                invalid_uid_records += 1
                return False

        tail = read_json_objects_tail(
            self.log_path,
            limit=limit,
            max_line_bytes=MAX_AUDIT_LINE_BYTES,
            predicate=belongs_to_uid,
            transform=lambda item: _audit_event_view(item, uid),
            max_scan_bytes=MAX_AUDIT_SCAN_BYTES,
        )
        details = _audit_tail_details(tail, invalid_uid_records=invalid_uid_records)
        if details:
            self._add_warning("; ".join(details))
        return list(tail.records)

    def _lock(self) -> FileLock:
        return FileLock(
            self.lock_path,
            self.lock_timeout_seconds,
            self.file_mode,
            self.dir_mode,
            self._managed_gid(),
        )

    def _managed_gid(self) -> Optional[int]:
        return setgid_directory_gid(
            self.data_dir,
            self.dir_mode,
            expected_gid=self.storage_gid,
        )

    def _load_unlocked(self, *, require_modes: bool = False) -> dict:
        if not os.path.lexists(self.ledger_path):
            return _empty_ledger()
        try:
            fd = open_existing_regular(
                self.ledger_path,
                expected_mode=self.file_mode if require_modes else None,
                expected_gid=self._managed_gid() if require_modes else None,
            )
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._validate_ledger(data)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            restored = self._load_latest_backup(require_modes=require_modes)
            if restored is not None:
                self._add_warning(f"ledger is invalid; loaded the latest valid backup: {exc}")
                data = restored
            else:
                raise LedgerCorruptionError(
                    errno.EIO,
                    f"ledger is invalid and no valid backup exists: {self.ledger_path}: {exc}",
                ) from exc
        self._validate_storage_modes(data)
        return data

    def _validate_storage_modes(self, ledger: dict) -> None:
        try:
            configured = ledger_storage_modes(ledger)
        except BookingError as exc:
            raise OSError(errno.EINVAL, str(exc)) from exc
        if configured is None:
            return
        actual = (f"{self.file_mode:04o}", f"{self.dir_mode:04o}")
        if configured != actual:
            raise PermissionError(
                errno.EPERM,
                "local storage modes do not match ledger policy: "
                f"ledger={configured[0]}/{configured[1]} local={actual[0]}/{actual[1]}",
            )

    def _load_latest_backup(self, *, require_modes: bool = False) -> Optional[dict]:
        if not os.path.lexists(self.backup_dir):
            return None
        metadata = self.backup_dir.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError(errno.ENOTDIR, f"refusing non-directory backup path: {self.backup_dir}")
        backups = sorted(self.backup_dir.glob("ledger-*.json"), reverse=True)
        for path in backups:
            try:
                fd = open_existing_regular(
                    path,
                    expected_mode=self.file_mode if require_modes else None,
                    expected_gid=self._managed_gid() if require_modes else None,
                )
                with os.fdopen(fd, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self._validate_ledger(data)
                return data
            except (json.JSONDecodeError, OSError, ValueError):
                continue
        return None

    @staticmethod
    def _validate_ledger(data: dict) -> None:
        validate_ledger_document(data)

    def _write_journal(self, journal: dict) -> None:
        _atomic_write_json(
            self.journal_path,
            journal,
            self.file_mode,
            self.data_dir,
            prefix=".transaction.",
            expected_gid=self._managed_gid(),
        )

    def _recover_journal_unlocked(self) -> None:
        if not os.path.lexists(self.journal_path):
            return
        try:
            fd = open_existing_regular(
                self.journal_path,
                expected_mode=self.file_mode,
                expected_gid=self._managed_gid(),
            )
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                journal = json.load(fh)
            self._validate_journal(journal)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise OSError(f"cannot recover invalid transaction journal {self.journal_path}: {exc}") from exc
        self._apply_journal_unlocked(journal)
        self._clear_journal_best_effort()

    def _journal_matches_transaction(self, transaction_id: str) -> bool:
        if not os.path.lexists(self.journal_path):
            return False
        try:
            fd = open_existing_regular(
                self.journal_path,
                expected_mode=self.file_mode,
                expected_gid=self._managed_gid(),
            )
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                journal = json.load(fh)
            self._validate_journal(journal)
        except (OSError, ValueError, json.JSONDecodeError, RecursionError):
            return False
        return journal.get("transaction_id") == transaction_id

    def _validate_journal(self, journal: dict) -> None:
        if not isinstance(journal, dict) or journal.get("version") != 1:
            raise ValueError("unsupported journal")
        transaction_id = journal.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id:
            raise ValueError("journal transaction ID is missing")
        ledger = journal.get("ledger")
        if ledger is not None:
            self._validate_ledger(ledger)
            self._validate_storage_modes(ledger)
            if ledger.get("last_transaction_id") != transaction_id:
                raise ValueError("journal ledger transaction ID mismatch")
        logs = journal.get("logs")
        if not isinstance(logs, list) or not all(isinstance(item, dict) for item in logs):
            raise ValueError("journal logs are invalid")
        event_ids = set()
        for item in logs:
            event_id = item.get("event_id")
            if not isinstance(event_id, str) or not event_id or len(event_id) > 128:
                raise ValueError("journal log event ID is invalid")
            if event_id in event_ids:
                raise ValueError("journal log event IDs must be unique")
            event_ids.add(event_id)
            if item.get("transaction_id") != transaction_id:
                raise ValueError("journal log transaction ID mismatch")
            encode_json_object_line(
                item,
                max_line_bytes=MAX_AUDIT_LINE_BYTES,
                record_name="journal audit",
                compact=False,
            )

    def _apply_journal_unlocked(self, journal: dict) -> None:
        ledger = journal.get("ledger")
        if ledger is not None:
            try:
                current = self._load_unlocked(require_modes=True)
            except LedgerCorruptionError:
                current = _empty_ledger()
            if current.get("last_transaction_id") != journal["transaction_id"]:
                self._atomic_write_ledger(ledger)
        logs = journal.get("logs", [])
        if logs:
            self._append_missing_logs(logs)
        if ledger is not None:
            try:
                self._write_backup(ledger)
            except OSError as exc:
                self._add_warning(f"ledger committed but backup failed: {exc}")

    def _atomic_write_ledger(self, ledger: dict) -> None:
        self._validate_ledger(ledger)
        _atomic_write_json(
            self.ledger_path,
            ledger,
            self.file_mode,
            self.data_dir,
            prefix=".ledger.",
            expected_gid=self._managed_gid(),
        )

    def _write_backup(self, ledger: dict) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = self.backup_dir / f"ledger-{stamp}.json"
        _atomic_write_json(
            path,
            ledger,
            self.file_mode,
            self.backup_dir,
            prefix=".ledger-backup.",
            expected_gid=self._managed_gid(),
        )
        self._prune_backups()

    def _prune_backups(self) -> None:
        backups = sorted(self.backup_dir.glob("ledger-*.json"), reverse=True)
        removed = False
        for path in backups[self.backup_keep :]:
            fd = open_existing_regular(
                path,
                expected_mode=self.file_mode,
                expected_gid=self._managed_gid(),
            )
            os.close(fd)
            path.unlink(missing_ok=True)
            removed = True
        if removed:
            fsync_directory(self.backup_dir)

    def _append_missing_logs(self, logs: List[dict]) -> None:
        event_ids = {str(item["event_id"]) for item in logs}
        batch_bytes = 1 + sum(
            len(
                encode_json_object_line(
                    item,
                    max_line_bytes=MAX_AUDIT_LINE_BYTES,
                    record_name="audit",
                    compact=False,
                )
            )
            for item in logs
        )
        existing = self._recent_event_ids(event_ids, batch_bytes)
        missing = [item for item in logs if str(item["event_id"]) not in existing]
        if not missing:
            return
        try:
            result = append_json_objects(
                self.log_path,
                missing,
                file_mode=self.file_mode,
                dir_mode=self.dir_mode,
                max_line_bytes=MAX_AUDIT_LINE_BYTES,
                max_file_bytes=None,
                record_name="audit",
                compact=False,
                expected_gid=self._managed_gid(),
            )
        except ValueError as exc:
            raise OSError(f"cannot append audit log: {exc}") from exc
        if result.warnings:
            self._add_warning(*result.warnings)

    def _recent_event_ids(self, wanted: set[str], tail_bytes: int) -> set[str]:
        if not wanted or not self.log_path.exists():
            return set()

        def is_wanted(item: dict) -> bool:
            return str(item.get("event_id", "")) in wanted

        tail = read_json_objects_tail(
            self.log_path,
            limit=len(wanted),
            max_line_bytes=MAX_AUDIT_LINE_BYTES,
            predicate=is_wanted,
            transform=lambda item: {"event_id": str(item.get("event_id", ""))},
            max_scan_bytes=max(1, tail_bytes),
        )
        return {str(item.get("event_id", "")) for item in tail.records}

    def _validate_managed_files_unlocked(self) -> None:
        for path in (self.ledger_path, self.log_path, self.journal_path):
            if not os.path.lexists(path):
                continue
            fd = open_existing_regular(
                path,
                expected_mode=self.file_mode,
                expected_gid=self._managed_gid(),
            )
            os.close(fd)

    def _preflight_storage_policy_on_mode_mismatch(self) -> None:
        if os.path.lexists(self.journal_path) or not os.path.lexists(self.ledger_path):
            return
        for path in (self.ledger_path, self.lock_path, self.log_path):
            if not os.path.lexists(path):
                continue
            try:
                metadata = path.lstat()
            except OSError:
                return
            if stat.S_ISREG(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) != self.file_mode:
                self._load_unlocked()
                return

    def _clear_journal_best_effort(self) -> None:
        try:
            self.journal_path.unlink(missing_ok=True)
            fsync_directory(self.data_dir)
        except OSError as exc:
            self._add_warning(f"transaction committed but journal cleanup failed: {exc}")

    def _add_warning(self, *messages: str) -> None:
        values = []
        if self.last_warning:
            values.append(self.last_warning)
        values.extend(message for message in messages if message)
        self.last_warning = "; ".join(dict.fromkeys(values)) or None


def _prepare_logs(logs: List[dict], transaction_id: str) -> List[dict]:
    prepared = []
    for item in logs:
        value = dict(item)
        value["transaction_id"] = transaction_id
        value.setdefault("event_id", str(uuid.uuid4()))
        prepared.append(value)
    return prepared


def _audit_tail_details(
    tail: JsonlTailResult,
    *,
    invalid_uid_records: int = 0,
) -> List[str]:
    details = []
    malformed = tail.invalid_records + invalid_uid_records
    if malformed:
        details.append(f"found {malformed} malformed audit record(s)")
    if tail.oversized_records:
        details.append(f"found {tail.oversized_records} oversized audit record(s)")
    if tail.final_newline_missing:
        details.append("the final audit record is not newline-terminated")
    if tail.scan_truncated:
        details.append("the audit scan reached its 64 MiB safety limit")
    return details


def _audit_event_view(item: dict, uid: int) -> dict:
    view = {"uid": uid}
    for key, limit in AUDIT_TEXT_LIMITS.items():
        value = item.get(key)
        view[key] = value[:limit] if isinstance(value, str) else None

    raw_gpus = item.get("gpus")
    view["gpus"] = (
        [
            value
            for value in raw_gpus[:256]
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000
        ]
        if isinstance(raw_gpus, list)
        else []
    )
    share_units = item.get("share_units")
    view["share_units"] = (
        share_units
        if isinstance(share_units, int)
        and not isinstance(share_units, bool)
        and 0 < share_units <= 1_000_000
        else None
    )
    return view


def _managed_gid_issue(
    path: Path,
    metadata: os.stat_result,
    expected_gid: Optional[int],
    *,
    path_type: str,
) -> Optional[dict]:
    if expected_gid is None or metadata.st_gid == expected_gid:
        return None
    return {
        "type": f"{path_type}-gid",
        "path": str(path),
        "expected_gid": expected_gid,
        "actual_gid": metadata.st_gid,
        "message": "path did not inherit the setgid data-directory group",
    }


def _atomic_write_json(
    path: Path,
    value: dict,
    file_mode: int,
    directory: Path,
    *,
    prefix: str,
    expected_gid: Optional[int] = None,
) -> None:
    directory_metadata = directory.lstat()
    if not stat.S_ISDIR(directory_metadata.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, f"refusing non-directory path: {directory}")
    if expected_gid is not None and directory_metadata.st_gid != expected_gid:
        raise PermissionError(
            errno.EPERM,
            f"refusing directory with GID {directory_metadata.st_gid}; "
            f"expected {expected_gid}: {directory}",
        )
    payload = (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    )
    disk_usage = shutil.disk_usage(directory)
    if disk_usage.free < max(len(payload) * 2, 1024 * 1024):
        raise OSError(errno.ENOSPC, "not enough free space for safe file transaction")

    if os.path.lexists(path):
        existing_fd = open_existing_regular(
            path,
            expected_mode=file_mode,
            expected_gid=expected_gid,
        )
        os.close(existing_fd)

    fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=str(directory))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, file_mode)
        actual_gid = os.fstat(fd).st_gid
        if expected_gid is not None and actual_gid != expected_gid:
            raise PermissionError(
                errno.EPERM,
                f"refusing temporary file with GID {actual_gid}; expected {expected_gid}: {tmp_path}",
            )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        fsync_directory(directory)
    finally:
        tmp_path.unlink(missing_ok=True)


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    fd = open_existing_regular(path)
    with os.fdopen(fd, "rb") as fh:
        return sum(1 for _line in fh)
