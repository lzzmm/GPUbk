from __future__ import annotations

import hashlib
import json
import os
import selectors
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, List, Optional, Tuple

from .config import Config
from .fileio import ensure_directory, open_existing_regular, open_or_create_regular
from .models import (
    JOB_CLAIMED,
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_PENDING,
    JOB_RUNNING,
    JOB_UNCERTAIN,
    STATUS_ACTIVE,
    Actor,
    BookingError,
)
from .timeparse import parse_iso


MIB = 1024 * 1024
LOG_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class JobLogCleanupResult:
    removed: int = 0
    retained: int = 0
    failed: int = 0
    bytes_removed: int = 0
    bytes_retained: int = 0
    quota_excess_bytes: int = 0
    warnings: Tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "removed": self.removed,
            "retained": self.retained,
            "failed": self.failed,
            "bytes_removed": self.bytes_removed,
            "bytes_retained": self.bytes_retained,
            "quota_excess_bytes": self.quota_excess_bytes,
            "warnings": list(self.warnings),
        }


class JobLogPump:
    """Drain one child stream into a private two-segment rolling log."""

    def __init__(self, path: Path, actor: Actor, max_bytes: int, header: dict):
        if max_bytes < 0:
            raise ValueError("job log byte limit must be nonnegative")
        self.path = path
        self.rotated_path = rotated_job_log_path(path)
        self.actor = actor
        self.max_bytes = max_bytes
        self.header = (json.dumps(header, ensure_ascii=False) + "\n").encode("utf-8")
        self.rotation_count = 0
        self.error: Optional[str] = None
        self._stream: Optional[BinaryIO] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._fh: Optional[BinaryIO] = None
        self._segment_bytes = 0
        self._segment_limit = max_bytes // 2 if max_bytes else 0
        if self._segment_limit and self._segment_limit <= len(self.header) + LOG_READ_CHUNK_BYTES:
            raise ValueError("job log byte limit is too small for safe rotation")
        validate_private_directory(path.parent, actor)
        self._prepare_current_segment()

    def start(self, stream: BinaryIO) -> None:
        if self._thread is not None:
            raise RuntimeError("job log pump already started")
        self._stream = stream
        os.set_blocking(stream.fileno(), False)
        self._thread = threading.Thread(
            target=self._drain,
            name=f"bk-job-log-{self.path.stem[:8]}",
            daemon=True,
        )
        self._thread.start()

    def record_event(self, event: dict) -> None:
        if self._thread is not None:
            raise RuntimeError("cannot write a control event after the log pump starts")
        payload = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        self._write_output(payload)

    def finish(self, timeout: float = 2.0) -> Optional[str]:
        thread = self._thread
        if thread is None:
            self._close_log()
            return self.error
        thread.join(timeout)
        if thread.is_alive() and self._stream is not None:
            self.error = self.error or "job log stream remained open after the command exited"
            self._stop_event.set()
            thread.join(0.5)
        if thread.is_alive():
            self.error = self.error or "job log drain did not stop"
        return self.error

    def abort(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(0.5)
            if self._thread.is_alive():
                self.error = self.error or "job log drain did not stop during launch abort"
                return
        elif self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
        self._close_log()

    def _prepare_current_segment(self) -> None:
        if self.max_bytes and os.path.lexists(self.path):
            issue = _job_log_file_issue(self.path, self.actor.uid)
            if issue is not None:
                raise BookingError(issue)
            if self.path.lstat().st_size:
                self._rotate_file(count_rotation=False)
        self._open_segment()

    def _open_segment(self) -> None:
        fd = open_or_create_regular(self.path, os.O_WRONLY | os.O_APPEND, 0o600)
        metadata = os.fstat(fd)
        if metadata.st_uid != self.actor.uid:
            os.close(fd)
            raise BookingError(f"job log is not owned by UID {self.actor.uid}: {self.path}")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            os.close(fd)
            raise BookingError(f"job log is accessible by group or other users: {self.path}")
        os.fchmod(fd, 0o600)
        self._fh = os.fdopen(fd, "ab", buffering=0)
        self._segment_bytes = metadata.st_size
        self._write_raw(self.header)

    def _write_raw(self, data: bytes) -> None:
        if self._fh is None:
            return
        self._fh.write(data)
        self._segment_bytes += len(data)

    def _write_output(self, data: bytes) -> None:
        if self._fh is None:
            return
        if self._segment_limit and self._segment_bytes + len(data) > self._segment_limit:
            self._rotate_file(count_rotation=True)
            self._open_segment()
        self._write_raw(data)

    def _rotate_file(self, *, count_rotation: bool) -> None:
        self._close_log()
        if os.path.lexists(self.rotated_path):
            issue = _job_log_file_issue(self.rotated_path, self.actor.uid)
            if issue is not None:
                raise BookingError(issue)
            self.rotated_path.unlink()
        if os.path.lexists(self.path):
            issue = _job_log_file_issue(self.path, self.actor.uid)
            if issue is not None:
                raise BookingError(issue)
            os.replace(self.path, self.rotated_path)
        _fsync_directory(self.path.parent)
        if count_rotation:
            self.rotation_count += 1

    def _drain(self) -> None:
        stream = self._stream
        if stream is None:
            return
        selector = selectors.DefaultSelector()
        try:
            selector.register(stream, selectors.EVENT_READ)
            while True:
                if self._stop_event.is_set():
                    break
                events = selector.select(0.2)
                if not events:
                    if self._stop_event.is_set():
                        break
                    continue
                try:
                    data = os.read(stream.fileno(), LOG_READ_CHUNK_BYTES)
                except BlockingIOError:
                    continue
                if not data:
                    break
                if self._fh is None:
                    continue
                try:
                    self._write_output(data)
                except (OSError, BookingError, ValueError) as exc:
                    self.error = f"job log write failed: {exc}"
                    self._close_log()
        except (KeyError, OSError, ValueError) as exc:
            self.error = self.error or f"job log drain failed: {exc}"
        finally:
            selector.close()
            try:
                stream.close()
            except OSError:
                pass
            self._close_log(sync=True)

    def _close_log(self, *, sync: bool = False) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        if sync:
            try:
                os.fsync(fh.fileno())
            except OSError as exc:
                self.error = self.error or f"job log sync failed: {exc}"
        try:
            fh.close()
        except OSError as exc:
            self.error = self.error or f"job log close failed: {exc}"


def job_log_root(config: Config) -> Path:
    return config.job_log_dir or (Path.home() / ".local" / "state" / "bk" / "jobs")


def ensure_job_log_dir(config: Config, actor: Actor) -> Path:
    path = job_log_root(config)
    if not path.is_absolute():
        raise BookingError(f"job log directory must be absolute: {path}")
    ensure_private_directory(path, actor)
    return path


def ensure_private_directory(path: Path, actor: Actor) -> None:
    ensure_directory(path, 0o700)
    metadata = path.lstat()
    if metadata.st_uid != actor.uid:
        raise BookingError(f"private job directory is not owned by UID {actor.uid}: {path}")
    path.chmod(0o700)


def validate_private_directory(path: Path, actor: Actor) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BookingError(f"cannot inspect private job directory {path}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise BookingError(f"private job path is not a directory: {path}")
    if metadata.st_uid != actor.uid:
        raise BookingError(f"private job directory is not owned by UID {actor.uid}: {path}")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise BookingError(f"private job directory must not be accessible by group or other users: {path}")


def job_log_path(config: Config, reservation_id: str) -> Path:
    root = job_log_root(config)
    try:
        normalized = str(uuid.UUID(str(reservation_id)))
    except (ValueError, AttributeError):
        normalized = hashlib.sha256(str(reservation_id).encode("utf-8", errors="replace")).hexdigest()
    return root / f"{normalized}.log"


def rotated_job_log_path(path: Path) -> Path:
    return path.with_name(path.name + ".1")


def job_log_paths(config: Config, reservation_id: str) -> List[Path]:
    current = job_log_path(config, reservation_id)
    return [path for path in (rotated_job_log_path(current), current) if os.path.lexists(path)]


def read_job_log_tail(config: Config, reservation_id: str, max_chars: int) -> str:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    paths = job_log_paths(config, reservation_id)
    remaining = max_chars * 4
    chunks: List[bytes] = []
    for path in reversed(paths):
        if remaining <= 0:
            break
        chunk = _read_tail_bytes(path, remaining)
        chunks.append(chunk)
        remaining -= len(chunk)
    text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
    return text[-max_chars:]


def cleanup_job_logs(
    config: Config,
    ledger: dict,
    actor: Actor,
    *,
    now: Optional[datetime] = None,
) -> JobLogCleanupResult:
    if actor.uid != os.getuid():
        raise BookingError("job log cleanup actor must match the current process UID")
    root = job_log_root(config)
    if not os.path.lexists(root):
        return JobLogCleanupResult()
    validate_private_directory(root, actor)

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    references, reference_warnings = _job_log_references(ledger, actor, current)
    warnings = list(reference_warnings)
    failed = len(reference_warnings)
    groups: Dict[str, List[Path]] = {}
    try:
        candidates = list(root.iterdir())
    except OSError as exc:
        return JobLogCleanupResult(failed=1, warnings=(f"cannot scan private job logs: {exc}",))
    for path in candidates:
        reservation_id = _reservation_id_from_log_filename(path.name)
        if reservation_id is not None:
            groups.setdefault(reservation_id, []).append(path)

    safe_groups: Dict[str, Tuple[List[Path], int, float, bool]] = {}
    retained_unsafe = 0
    unsafe_bytes = 0
    for reservation_id, paths in groups.items():
        issues = [issue for path in paths if (issue := _job_log_file_issue(path, actor.uid))]
        if issues:
            failed += 1
            retained_unsafe += 1
            for path in paths:
                try:
                    metadata = path.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    unsafe_bytes += metadata.st_size
            warnings.append(f"{reservation_id[:8]}: {issues[0]}")
            continue
        metadata = [path.lstat() for path in paths]
        safe_groups[reservation_id] = (
            paths,
            sum(item.st_size for item in metadata),
            max(item.st_mtime for item in metadata),
            references.get(reservation_id, False),
        )

    removed = 0
    bytes_removed = 0
    retention_cutoff = (
        current - timedelta(days=config.job_log_retention_days)
        if config.job_log_retention_days
        else None
    )
    for reservation_id, group in list(safe_groups.items()):
        paths, size, modified_at, needed = group
        if needed or retention_cutoff is None:
            continue
        if datetime.fromtimestamp(modified_at, timezone.utc) > retention_cutoff:
            continue
        error = _remove_log_group(paths, actor.uid)
        if error is not None:
            failed += 1
            warnings.append(f"{reservation_id[:8]}: {error}")
            continue
        removed += 1
        bytes_removed += size
        del safe_groups[reservation_id]

    total_bytes = unsafe_bytes + sum(group[1] for group in safe_groups.values())
    quota_bytes = config.job_log_total_max_mb * MIB
    if quota_bytes and total_bytes > quota_bytes:
        quota_candidates = sorted(
            (
                (reservation_id, group)
                for reservation_id, group in safe_groups.items()
                if not group[3]
            ),
            key=lambda item: (item[1][2], item[0]),
        )
        for reservation_id, group in quota_candidates:
            if total_bytes <= quota_bytes:
                break
            paths, size, _modified_at, _needed = group
            error = _remove_log_group(paths, actor.uid)
            if error is not None:
                failed += 1
                warnings.append(f"{reservation_id[:8]}: {error}")
                continue
            removed += 1
            bytes_removed += size
            total_bytes -= size
            del safe_groups[reservation_id]

    quota_excess = max(0, total_bytes - quota_bytes) if quota_bytes else 0
    if quota_excess:
        warnings.append(
            f"private job logs exceed the configured quota by {quota_excess} bytes; active or retryable logs were retained"
        )
    return JobLogCleanupResult(
        removed=removed,
        retained=len(safe_groups) + retained_unsafe,
        failed=failed,
        bytes_removed=bytes_removed,
        bytes_retained=total_bytes,
        quota_excess_bytes=quota_excess,
        warnings=tuple(warnings),
    )


def _job_log_references(
    ledger: dict,
    actor: Actor,
    now: datetime,
) -> Tuple[Dict[str, bool], List[str]]:
    references: Dict[str, bool] = {}
    warnings: List[str] = []
    for reservation in ledger.get("reservations", []):
        if not isinstance(reservation, dict) or not isinstance(reservation.get("job"), dict):
            continue
        try:
            reservation_id = str(uuid.UUID(str(reservation.get("id", ""))))
        except (ValueError, AttributeError):
            continue
        try:
            reservation_uid = int(reservation.get("uid", -1))
        except (TypeError, ValueError):
            references[reservation_id] = True
            warnings.append(
                f"reservation {reservation_id[:8]} has an invalid UID; retained its private log"
            )
            continue
        if reservation_uid != actor.uid:
            continue
        needed = _job_log_is_needed(reservation, reservation["job"], now)
        references[reservation_id] = references.get(reservation_id, False) or needed
    return references, warnings


def _job_log_is_needed(reservation: dict, job: dict, now: datetime) -> bool:
    status = job.get("status")
    if status in {JOB_PENDING, JOB_CLAIMED, JOB_RUNNING}:
        return True
    if status not in {JOB_FAILED, JOB_INTERRUPTED, JOB_UNCERTAIN}:
        return False
    if reservation.get("status") != STATUS_ACTIVE:
        return False
    try:
        return parse_iso(str(reservation["end_at"])) > now
    except (KeyError, TypeError, ValueError):
        return True


def _reservation_id_from_log_filename(name: str) -> Optional[str]:
    stem = name[:-6] if name.endswith(".log.1") else name[:-4] if name.endswith(".log") else None
    if stem is None:
        return None
    try:
        return str(uuid.UUID(stem))
    except (ValueError, AttributeError):
        return None


def _job_log_file_issue(path: Path, uid: int) -> Optional[str]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        return f"cannot inspect private job log: {exc}"
    if not stat.S_ISREG(metadata.st_mode):
        return f"private job log is not a regular file: {path.name}"
    if metadata.st_uid != uid:
        return f"private job log is not owned by UID {uid}: {path.name}"
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        return f"private job log is accessible by group or other users: {path.name}"
    return None


def _remove_log_group(paths: Iterable[Path], uid: int) -> Optional[str]:
    existing = [path for path in paths if os.path.lexists(path)]
    for path in existing:
        issue = _job_log_file_issue(path, uid)
        if issue is not None:
            return issue
    try:
        for path in existing:
            path.unlink(missing_ok=True)
        if existing:
            _fsync_directory(existing[0].parent)
    except OSError as exc:
        return str(exc)
    return None


def _read_tail_bytes(path: Path, max_bytes: int) -> bytes:
    fd = open_existing_regular(path)
    with os.fdopen(fd, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        return fh.read()


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
