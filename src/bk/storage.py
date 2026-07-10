from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Tuple, TypeVar


T = TypeVar("T")


def _empty_ledger() -> dict:
    return {"version": 1, "reservations": []}


class FileLock:
    def __init__(self, path: Path, timeout_seconds: float):
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._write_metadata()
                return self
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timeout waiting for lock {self.path}") from exc
                time.sleep(0.05)

    def _write_metadata(self) -> None:
        assert self._fh is not None
        self._fh.seek(0)
        self._fh.truncate()
        payload = {"pid": os.getpid(), "locked_at": datetime.now(timezone.utc).isoformat()}
        self._fh.write(json.dumps(payload, ensure_ascii=False))
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def __exit__(self, exc_type, exc, tb):
        assert self._fh is not None
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None


class LedgerStore:
    def __init__(self, data_dir: Path, lock_timeout_seconds: float = 10.0, backup_keep: int = 10):
        self.data_dir = data_dir
        self.lock_timeout_seconds = lock_timeout_seconds
        self.backup_keep = backup_keep
        self.ledger_path = data_dir / "ledger.json"
        self.lock_path = data_dir / "ledger.lock"
        self.log_path = data_dir / "ops.log"
        self.backup_dir = data_dir / "backups"

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        self.ensure()
        return self._load_unlocked()

    def transaction(self, mutator: Callable[[dict], Tuple[dict, T, Iterable[dict], bool]]) -> T:
        self.ensure()
        with FileLock(self.lock_path, self.lock_timeout_seconds):
            ledger = self._load_unlocked()
            new_ledger, result, logs, changed = mutator(ledger)
            if changed:
                self._atomic_write(new_ledger)
                self._write_backup(new_ledger)
            log_items = list(logs)
            if log_items:
                self._append_logs(log_items)
            return result

    def reset(self) -> dict:
        self.ensure()
        with FileLock(self.lock_path, self.lock_timeout_seconds):
            previous = self._load_unlocked()
            reservation_count = len(previous.get("reservations", []))
            log_count = 0
            if self.log_path.exists():
                with self.log_path.open("r", encoding="utf-8") as fh:
                    log_count = sum(1 for _line in fh)
            backup_count = len(list(self.backup_dir.glob("ledger-*.json"))) if self.backup_dir.exists() else 0

            self._atomic_write(_empty_ledger())
            self.log_path.unlink(missing_ok=True)
            if self.backup_dir.exists():
                for path in self.backup_dir.glob("ledger-*.json"):
                    path.unlink(missing_ok=True)
            return {
                "reservations": reservation_count,
                "logs": log_count,
                "backups": backup_count,
            }

    def _load_unlocked(self) -> dict:
        if not self.ledger_path.exists():
            return _empty_ledger()
        try:
            with self.ledger_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._validate_ledger(data)
            return data
        except (json.JSONDecodeError, OSError, ValueError):
            restored = self._load_latest_backup()
            if restored is not None:
                return restored
            return _empty_ledger()

    def _load_latest_backup(self) -> dict:
        if not self.backup_dir.exists():
            return None
        backups = sorted(self.backup_dir.glob("ledger-*.json"), reverse=True)
        for path in backups:
            try:
                with path.open("r", encoding="utf-8") as fh:
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

    def _atomic_write(self, ledger: dict) -> None:
        payload = json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        stat = shutil.disk_usage(self.data_dir)
        if stat.free < max(len(payload) * 2, 1024 * 1024):
            raise OSError(errno.ENOSPC, "not enough free space for safe ledger write")

        fd, tmp_name = tempfile.mkstemp(prefix=".ledger.", suffix=".tmp", dir=str(self.data_dir))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            with tmp_path.open("r", encoding="utf-8") as check:
                self._validate_ledger(json.load(check))
            os.replace(tmp_path, self.ledger_path)
            self._fsync_dir(self.data_dir)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _write_backup(self, ledger: dict) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = self.backup_dir / f"ledger-{stamp}.json"
        with path.open("w", encoding="utf-8") as fh:
            json.dump(ledger, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._prune_backups()

    def _prune_backups(self) -> None:
        backups = sorted(self.backup_dir.glob("ledger-*.json"), reverse=True)
        for path in backups[self.backup_keep :]:
            path.unlink(missing_ok=True)

    def _append_logs(self, logs: List[dict]) -> None:
        with self.log_path.open("a", encoding="utf-8") as fh:
            for item in logs:
                fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
