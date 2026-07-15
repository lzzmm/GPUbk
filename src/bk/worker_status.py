from __future__ import annotations

import fcntl
import json
import os
import stat
import sys
from datetime import datetime
from typing import Iterable, Optional

from .config import MAX_UID, Config
from .fileio import open_existing_regular
from .joblogs import (
    WORKER_LEASE_FILENAME,
    job_log_root,
    validate_private_directory,
    worker_instance_id,
    worker_instance_lease_path,
)
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
WORKER_INSTANCE_ID_LENGTH = 64
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
    persistence = inspect_worker_persistence(actor)
    if actor.uid != os.getuid():
        return _status(
            "unavailable",
            checked_at,
            running=None,
            lease_present=None,
            warning="worker status is available only for the current process UID",
            persistence=persistence,
        )

    root = job_log_root(config)
    if not root.is_absolute():
        return _invalid(checked_at, f"job log directory must be absolute: {root}", persistence=persistence)
    try:
        root.lstat()
    except FileNotFoundError:
        return _status(
            "not-seen",
            checked_at,
            running=False,
            lease_present=False,
            lease_held=False,
            persistence=persistence,
        )
    except OSError as exc:
        return _invalid(checked_at, f"cannot inspect private job directory {root}: {exc}", persistence=persistence)

    try:
        validate_private_directory(root, actor)
    except (BookingError, OSError) as exc:
        return _invalid(checked_at, str(exc), persistence=persistence)

    path = root / WORKER_LEASE_FILENAME
    try:
        fd = open_existing_regular(path, expected_mode=0o600)
    except FileNotFoundError:
        return _status(
            "not-seen",
            checked_at,
            running=False,
            lease_present=False,
            lease_held=False,
            persistence=persistence,
        )
    except OSError as exc:
        return _invalid(checked_at, f"cannot safely inspect worker lease: {exc}", lease_present=True, persistence=persistence)

    try:
        try:
            metadata = os.fstat(fd)
        except OSError as exc:
            return _invalid(
                checked_at,
                f"cannot inspect worker lease: {exc}",
                lease_present=True,
                persistence=persistence,
            )
        if metadata.st_uid != actor.uid:
            return _invalid(
                checked_at,
                f"worker lease is not owned by UID {actor.uid}",
                lease_present=True,
                persistence=persistence,
            )
        raw, read_warning = _read_lease_bytes(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            lease_held = False
        except BlockingIOError:
            lease_held = True
        except OSError as exc:
            return _invalid(
                checked_at,
                f"cannot probe worker lease lock: {exc}",
                lease_present=True,
                persistence=persistence,
            )
    finally:
        os.close(fd)

    lease, validation_warning, recorded_instance_match = (
        _parse_lease(raw, actor.uid, worker_instance_id(config))
        if raw is not None
        else (None, None, None)
    )
    instance_lease_held, instance_warning = (
        _inspect_instance_lease(config, actor)
        if lease_held
        else (None, None)
    )
    if lease is not None and (not lease_held or instance_lease_held is True):
        validation_warning = None
    warning = read_warning or validation_warning or instance_warning
    metadata_valid = lease is not None
    if lease_held:
        if instance_lease_held is True:
            state = "running"
            ready = True
            instance_match = True
        elif recorded_instance_match is False:
            state = "other-instance"
            ready = False
            instance_match = False
        else:
            state = "unverified"
            ready = None
            instance_match = None
    else:
        state = "stopped"
        ready = False
        instance_match = None
    return _status(
        state,
        checked_at,
        running=ready,
        lease_present=True,
        lease=lease,
        lease_held=lease_held,
        instance_lease_held=instance_lease_held,
        metadata_valid=metadata_valid,
        instance_match=instance_match,
        warning=warning,
        evidence="kernel-flock",
        persistence=persistence,
    )


def inspect_worker_persistence(actor: Actor) -> dict:
    """Report whether systemd will keep this user's manager alive after logout."""

    command = ["sudo", "loginctl", "enable-linger", actor.username]
    result = {
        "kind": "systemd-linger",
        "state": "not-applicable",
        "logout_safe": None,
        "admin_argv": command,
    }
    if not actor.username or "/" in actor.username or actor.username in {".", ".."}:
        return {
            **result,
            "state": "unknown",
            "warning": "Linux account name is unsafe for linger inspection",
        }
    if not sys.platform.startswith("linux") or not os.path.isdir("/run/systemd/system"):
        return result
    path = os.path.join("/var/lib/systemd/linger", actor.username)
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return {**result, "state": "disabled", "logout_safe": False}
    except OSError as exc:
        return {**result, "state": "unknown", "warning": str(exc)}
    if not stat.S_ISREG(metadata.st_mode):
        return {
            **result,
            "state": "unknown",
            "warning": "linger marker is not a regular file",
        }
    if metadata.st_uid != 0:
        return {
            **result,
            "state": "unknown",
            "warning": "linger marker is not owned by root",
        }
    return {
        **result,
        "state": "enabled",
        "logout_safe": True,
        "marker_uid": metadata.st_uid,
    }


def _inspect_instance_lease(
    config: Config,
    actor: Actor,
) -> tuple[Optional[bool], Optional[str]]:
    path = worker_instance_lease_path(config)
    try:
        fd = open_existing_regular(path, expected_mode=0o600)
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        return None, f"cannot safely inspect worker instance lease: {exc}"
    try:
        try:
            metadata = os.fstat(fd)
        except OSError as exc:
            return None, f"cannot inspect worker instance lease: {exc}"
        if metadata.st_uid != actor.uid:
            return None, f"worker instance lease is not owned by UID {actor.uid}"
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return False, None
        except BlockingIOError:
            return True, None
        except OSError as exc:
            return None, f"cannot probe worker instance lease lock: {exc}"
    finally:
        os.close(fd)


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


def _parse_lease(
    raw: bytes,
    expected_uid: int,
    expected_instance_id: str,
) -> tuple[Optional[dict], Optional[str], Optional[bool]]:
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
        if "instance_id" in payload:
            instance_id = _bounded_instance_id(payload["instance_id"])
            instance_match = instance_id == expected_instance_id
            instance_warning = (
                None
                if instance_match
                else "worker lease belongs to another GPUBK data directory"
            )
        else:
            instance_id = None
            instance_match = None
            instance_warning = (
                "worker lease predates data-directory binding; restart the worker"
            )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        return None, f"invalid worker lease metadata: {exc}", None
    lease = {
        "worker_id": worker_id,
        "pid": pid,
        "uid": uid,
        "hostname": hostname,
        "acquired_at": acquired_at,
    }
    if instance_id is not None:
        lease["instance_id"] = instance_id
    return lease, instance_warning, instance_match


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


def _bounded_instance_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != WORKER_INSTANCE_ID_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("instance_id must be a lowercase SHA-256 digest")
    return value


def _invalid(
    checked_at: str,
    warning: str,
    *,
    lease_present: Optional[bool] = None,
    persistence: Optional[dict] = None,
) -> dict:
    return _status(
        "invalid",
        checked_at,
        running=None,
        lease_present=lease_present,
        warning=warning,
        persistence=persistence,
    )


def _status(
    state: str,
    checked_at: str,
    *,
    running: Optional[bool],
    lease_present: Optional[bool],
    lease: Optional[dict] = None,
    lease_held: Optional[bool] = None,
    instance_lease_held: Optional[bool] = None,
    metadata_valid: Optional[bool] = None,
    instance_match: Optional[bool] = None,
    warning: Optional[str] = None,
    evidence: Optional[str] = None,
    persistence: Optional[dict] = None,
) -> dict:
    return {
        "schema_version": WORKER_STATUS_SCHEMA_VERSION,
        "state": state,
        "running": running,
        "lease_present": lease_present,
        "lease_held": lease_held,
        "instance_lease_held": instance_lease_held,
        "metadata_valid": metadata_valid,
        "instance_match": instance_match,
        "evidence": evidence,
        "checked_at": checked_at,
        "lease": lease,
        "warning": warning,
        "persistence": persistence,
    }
