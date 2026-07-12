from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple, TypeVar

from .fileio import (
    ensure_directory,
    file_type_name,
    open_existing_regular,
    open_or_create_regular,
)
from .models import BookingError
from .policy import ledger_storage_modes


T = TypeVar("T")


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
    ):
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.file_mode = file_mode
        self.dir_mode = dir_mode
        self._fh = None

    def __enter__(self):
        ensure_directory(self.path.parent, self.dir_mode)
        fd = open_or_create_regular(self.path, os.O_RDWR, self.file_mode)
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
    ):
        self.data_dir = data_dir
        self.lock_timeout_seconds = lock_timeout_seconds
        self.backup_keep = backup_keep
        self.file_mode = file_mode
        self.dir_mode = dir_mode
        self.ledger_path = data_dir / "ledger.json"
        self.lock_path = data_dir / "ledger.lock"
        self.log_path = data_dir / "ops.log"
        self.backup_dir = data_dir / "backups"
        self.journal_path = data_dir / "transaction.json"
        self.last_warning: Optional[str] = None

    def ensure(self) -> None:
        ensure_directory(self.data_dir, self.dir_mode)
        ensure_directory(self.backup_dir, self.dir_mode)

    def load(self) -> dict:
        self.last_warning = None
        if self.journal_path.exists():
            with self._lock():
                self._recover_journal_unlocked()
        return self._load_unlocked()

    def load_read_only(self) -> dict:
        """Read the committed ledger snapshot without recovering pending writes."""
        self.last_warning = None
        return self._load_unlocked()

    def transaction(self, mutator: Callable[[dict], Tuple[dict, T, Iterable[dict], bool]]) -> T:
        self.ensure()
        self.last_warning = None
        with self._lock():
            self._recover_journal_unlocked()
            self._validate_existing_log_unlocked()
            ledger = self._load_unlocked()
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
            self._write_journal(journal)

            try:
                self._apply_journal_unlocked(journal)
            except OSError as exc:
                self.last_warning = f"transaction accepted but deferred recovery is required: {exc}"
                return result

            self._clear_journal_best_effort()
            return result

    def reset(self) -> dict:
        self.ensure()
        with self._lock():
            self._recover_journal_unlocked()
            previous = self._load_unlocked()
            reservation_count = len(previous.get("reservations", []))
            log_count = _line_count(self.log_path)
            backup_count = len(list(self.backup_dir.glob("ledger-*.json"))) if self.backup_dir.exists() else 0

            self._atomic_write_ledger(_empty_ledger())
            self.log_path.unlink(missing_ok=True)
            self.journal_path.unlink(missing_ok=True)
            if self.backup_dir.exists():
                for path in self.backup_dir.glob("ledger-*.json"):
                    path.unlink(missing_ok=True)
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
        return issues

    def _lock(self) -> FileLock:
        return FileLock(
            self.lock_path,
            self.lock_timeout_seconds,
            self.file_mode,
            self.dir_mode,
        )

    def _load_unlocked(self) -> dict:
        if not self.ledger_path.exists():
            return _empty_ledger()
        try:
            fd = open_existing_regular(self.ledger_path)
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._validate_ledger(data)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            restored = self._load_latest_backup()
            if restored is not None:
                self.last_warning = f"ledger is invalid; loaded the latest valid backup: {exc}"
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

    def _load_latest_backup(self) -> Optional[dict]:
        if not self.backup_dir.exists():
            return None
        backups = sorted(self.backup_dir.glob("ledger-*.json"), reverse=True)
        for path in backups:
            try:
                fd = open_existing_regular(path)
                with os.fdopen(fd, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self._validate_ledger(data)
                return data
            except (json.JSONDecodeError, OSError, ValueError):
                continue
        return None

    @staticmethod
    def _validate_ledger(data: dict) -> None:
        if not isinstance(data, dict):
            raise ValueError("ledger must be an object")
        if data.get("version") != 1:
            raise ValueError("unsupported ledger version")
        if not isinstance(data.get("reservations"), list):
            raise ValueError("ledger reservations must be a list")

    def _write_journal(self, journal: dict) -> None:
        _atomic_write_json(
            self.journal_path,
            journal,
            self.file_mode,
            self.data_dir,
            prefix=".transaction.",
        )

    def _recover_journal_unlocked(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            fd = open_existing_regular(self.journal_path)
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                journal = json.load(fh)
            self._validate_journal(journal)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise OSError(f"cannot recover invalid transaction journal {self.journal_path}: {exc}") from exc
        self._apply_journal_unlocked(journal)
        self._clear_journal_best_effort()

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

    def _apply_journal_unlocked(self, journal: dict) -> None:
        ledger = journal.get("ledger")
        if ledger is not None:
            try:
                current = self._load_unlocked()
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
                self.last_warning = f"ledger committed but backup failed: {exc}"

    def _atomic_write_ledger(self, ledger: dict) -> None:
        self._validate_ledger(ledger)
        _atomic_write_json(
            self.ledger_path,
            ledger,
            self.file_mode,
            self.data_dir,
            prefix=".ledger.",
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
        )
        self._prune_backups()

    def _prune_backups(self) -> None:
        backups = sorted(self.backup_dir.glob("ledger-*.json"), reverse=True)
        for path in backups[self.backup_keep :]:
            path.unlink(missing_ok=True)

    def _append_missing_logs(self, logs: List[dict]) -> None:
        event_ids = {str(item["event_id"]) for item in logs}
        existing = self._recent_event_ids(event_ids)
        missing = [item for item in logs if str(item["event_id"]) not in existing]
        if not missing:
            return
        fd = open_or_create_regular(self.log_path, os.O_WRONLY | os.O_APPEND, self.file_mode)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            for item in missing:
                fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _recent_event_ids(self, wanted: set[str], tail_bytes: int = 1024 * 1024) -> set[str]:
        if not wanted or not self.log_path.exists():
            return set()
        found = set()
        fd = open_existing_regular(self.log_path)
        with os.fdopen(fd, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            start = max(0, size - tail_bytes)
            fh.seek(start)
            if start:
                fh.readline()
            for raw_line in fh:
                try:
                    item = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                event_id = str(item.get("event_id", ""))
                if event_id in wanted:
                    found.add(event_id)
                    if found == wanted:
                        break
        return found

    def _validate_existing_log_unlocked(self) -> None:
        if not os.path.lexists(self.log_path):
            return
        fd = open_existing_regular(self.log_path, os.O_WRONLY | os.O_APPEND)
        os.close(fd)

    def _clear_journal_best_effort(self) -> None:
        try:
            self.journal_path.unlink(missing_ok=True)
            _fsync_dir(self.data_dir)
        except OSError as exc:
            self.last_warning = f"transaction committed but journal cleanup failed: {exc}"


def _prepare_logs(logs: List[dict], transaction_id: str) -> List[dict]:
    prepared = []
    for item in logs:
        value = dict(item)
        value["transaction_id"] = transaction_id
        value.setdefault("event_id", str(uuid.uuid4()))
        prepared.append(value)
    return prepared


def _atomic_write_json(
    path: Path,
    value: dict,
    file_mode: int,
    directory: Path,
    *,
    prefix: str,
) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    stat = shutil.disk_usage(directory)
    if stat.free < max(len(payload) * 2, 1024 * 1024):
        raise OSError(errno.ENOSPC, "not enough free space for safe file transaction")

    fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=str(directory))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, file_mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(directory)
    finally:
        tmp_path.unlink(missing_ok=True)


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    fd = open_existing_regular(path)
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        return sum(1 for _line in fh)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
