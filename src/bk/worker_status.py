from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime
from typing import Iterable, Optional

from .config import MAX_UID, Config
from .fileio import open_existing_regular
from .joblogs import WORKER_LEASE_FILENAME, job_log_root, validate_private_directory
from .models import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_MISSED,
    JOB_SUCCEEDED,
    JOB_TIMED_OUT,
    JOB_UNCERTAIN,
    Actor,
    BookingError,
)
from .timeparse import parse_iso, to_iso, utc_now


WORKER_STATUS_SCHEMA_VERSION = "gpubk.worker.v1"
MAX_WORKER_LEASE_BYTES = 16 * 1024
MAX_WORKER_ID_LENGTH = 128
MAX_HOSTNAME_LENGTH = 255
MAX_PID = 2**31 - 1
WORKER_TERMINAL_JOB_STATES = frozenset(
    {
        JOB_SUCCEEDED,
        JOB_FAILED,
        JOB_CANCELLED,
        JOB_MISSED,
        JOB_TIMED_OUT,
        JOB_INTERRUPTED,
        JOB_UNCERTAIN,
    }
)


def reservations_need_worker(reservations: Iterable[dict], uid: int) -> bool:
    """Return whether this UID has a job that may still run automatically."""

    for reservation in reservations:
        if reservation.get("uid") != uid:
            continue
        job = reservation.get("job")
        if not isinstance(job, dict):
            continue
        if job.get("status") not in WORKER_TERMINAL_JOB_STATES:
            return True
    return False


def inspect_worker_status(
    config: Config,
    actor: Actor,
    *,
    at: Optional[datetime] = None,
) -> dict:
    """Inspect this UID's worker lease without creating or modifying storage."""

    checked_at = to_iso(at or utc_now())
    if actor.uid != os.getuid():
        return _status(
            "unavailable",
            checked_at,
            running=None,
            lease_present=None,
            warning="worker status is available only for the current process UID",
        )

    root = job_log_root(config)
    if not root.is_absolute():
        return _invalid(checked_at, f"job log directory must be absolute: {root}")
    try:
        root.lstat()
    except FileNotFoundError:
        return _status("not-seen", checked_at, running=False, lease_present=False)
    except OSError as exc:
        return _invalid(checked_at, f"cannot inspect private job directory {root}: {exc}")

    try:
        validate_private_directory(root, actor)
    except (BookingError, OSError) as exc:
        return _invalid(checked_at, str(exc))

    path = root / WORKER_LEASE_FILENAME
    try:
        fd = open_existing_regular(path, expected_mode=0o600)
    except FileNotFoundError:
        return _status("not-seen", checked_at, running=False, lease_present=False)
    except OSError as exc:
        return _invalid(checked_at, f"cannot safely inspect worker lease: {exc}", lease_present=True)

    try:
        try:
            metadata = os.fstat(fd)
        except OSError as exc:
            return _invalid(
                checked_at,
                f"cannot inspect worker lease: {exc}",
                lease_present=True,
            )
        if metadata.st_uid != actor.uid:
            return _invalid(
                checked_at,
                f"worker lease is not owned by UID {actor.uid}",
                lease_present=True,
            )
        raw, read_warning = _read_lease_bytes(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            running = False
        except BlockingIOError:
            running = True
        except OSError as exc:
            return _invalid(
                checked_at,
                f"cannot probe worker lease lock: {exc}",
                lease_present=True,
            )
    finally:
        os.close(fd)

    lease, validation_warning = _parse_lease(raw, actor.uid) if raw is not None else (None, None)
    warning = read_warning or validation_warning
    return _status(
        "running" if running else "stopped",
        checked_at,
        running=running,
        lease_present=True,
        lease=lease,
        metadata_valid=warning is None,
        warning=warning,
        evidence="kernel-flock",
    )


def _read_lease_bytes(fd: int) -> tuple[Optional[bytes], Optional[str]]:
    try:
        size = os.fstat(fd).st_size
    except OSError as exc:
        return None, f"cannot inspect worker lease metadata: {exc}"
    if size > MAX_WORKER_LEASE_BYTES:
        return None, f"worker lease metadata exceeds {MAX_WORKER_LEASE_BYTES} bytes"
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        data = bytearray()
        while len(data) <= MAX_WORKER_LEASE_BYTES:
            chunk = os.read(fd, min(4096, MAX_WORKER_LEASE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
    except OSError as exc:
        return None, f"cannot read worker lease metadata: {exc}"
    if len(data) > MAX_WORKER_LEASE_BYTES:
        return None, f"worker lease metadata exceeds {MAX_WORKER_LEASE_BYTES} bytes"
    return bytes(data), None


def _parse_lease(raw: bytes, expected_uid: int) -> tuple[Optional[dict], Optional[str]]:
    try:
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("metadata must be a JSON object")
        if payload.get("version") != 1:
            raise ValueError(f"unsupported metadata version: {payload.get('version')!r}")
        worker_id = _bounded_text(payload.get("worker_id"), "worker_id", MAX_WORKER_ID_LENGTH)
        hostname = _bounded_text(payload.get("hostname"), "hostname", MAX_HOSTNAME_LENGTH)
        pid = _bounded_int(payload.get("pid"), "pid", 1, MAX_PID)
        uid = _bounded_int(payload.get("uid"), "uid", 0, MAX_UID)
        if uid != expected_uid:
            raise ValueError(f"metadata UID {uid} does not match expected UID {expected_uid}")
        acquired_at = payload.get("acquired_at")
        if not isinstance(acquired_at, str):
            raise ValueError("acquired_at must be a timestamp string")
        parse_iso(acquired_at)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        return None, f"invalid worker lease metadata: {exc}"
    return {
        "worker_id": worker_id,
        "pid": pid,
        "uid": uid,
        "hostname": hostname,
        "acquired_at": acquired_at,
    }, None


def _bounded_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _invalid(checked_at: str, warning: str, *, lease_present: Optional[bool] = None) -> dict:
    return _status(
        "invalid",
        checked_at,
        running=None,
        lease_present=lease_present,
        warning=warning,
    )


def _status(
    state: str,
    checked_at: str,
    *,
    running: Optional[bool],
    lease_present: Optional[bool],
    lease: Optional[dict] = None,
    metadata_valid: Optional[bool] = None,
    warning: Optional[str] = None,
    evidence: Optional[str] = None,
) -> dict:
    return {
        "schema_version": WORKER_STATUS_SCHEMA_VERSION,
        "state": state,
        "running": running,
        "lease_present": lease_present,
        "metadata_valid": metadata_valid,
        "evidence": evidence,
        "checked_at": checked_at,
        "lease": lease,
        "warning": warning,
    }
