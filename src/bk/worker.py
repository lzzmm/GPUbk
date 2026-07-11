from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import signal
import socket
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import Config
from .models import (
    JOB_CANCELLED,
    JOB_CLAIMED,
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_MISSED,
    JOB_PENDING,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    JOB_TIMED_OUT,
    JOB_UNCERTAIN,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    Actor,
    BookingError,
)
from .storage import LedgerStore
from .timeparse import parse_iso, to_iso, utc_now


@dataclass
class RunningJob:
    reservation_id: str
    claim_token: str
    process: subprocess.Popen
    end_at: datetime
    termination_reason: Optional[str] = None
    termination_requested_at: Optional[float] = None


@dataclass(frozen=True)
class WorkerSummary:
    claimed: int = 0
    started: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0


@dataclass(frozen=True)
class JobSpecReference:
    spec_id: str
    digest: str
    summary: str


def prepare_job_spec(
    config: Config,
    actor: Actor,
    command_argv: List[str],
    working_directory: str,
) -> JobSpecReference:
    if actor.uid != os.getuid():
        raise BookingError("job spec actor must match the current process UID")
    argv, cwd = _validate_submission_payload(command_argv, working_directory)
    spec_id = str(uuid.uuid4())
    payload = {
        "version": 1,
        "spec_id": spec_id,
        "uid": actor.uid,
        "argv": argv,
        "cwd": cwd,
        "created_at": to_iso(utc_now()),
    }
    digest = _job_spec_digest(payload)
    payload["digest"] = digest
    path = job_spec_path(config, spec_id)
    _ensure_private_directory(path.parent, actor)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    return JobSpecReference(spec_id, digest, _job_command_summary(argv))


def delete_job_spec(config: Config, spec_id: str) -> None:
    path = job_spec_path(config, spec_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def run_worker(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    *,
    once: bool = False,
    poll_seconds: Optional[float] = None,
    max_parallel: Optional[int] = None,
    quiet: bool = False,
) -> WorkerSummary:
    if actor.uid != os.getuid():
        raise BookingError("worker actor must match the current process UID")
    poll = config.worker_poll_seconds if poll_seconds is None else float(poll_seconds)
    if poll < 0.1:
        raise ValueError("worker poll interval must be >= 0.1 seconds")
    parallel = max_parallel if max_parallel is not None else max(1, config.gpu_count)
    if parallel < 1:
        raise ValueError("worker max parallel jobs must be >= 1")

    log_dir = _ensure_job_log_dir(config, actor)
    worker_id = str(uuid.uuid4())
    hostname = socket.gethostname()
    stop_event = threading.Event()
    previous_handlers = _install_signal_handlers(stop_event)
    running: Dict[str, RunningJob] = {}
    counts = {"claimed": 0, "started": 0, "succeeded": 0, "failed": 0, "cancelled": 0}
    if not quiet:
        print(f"worker started: uid={actor.uid} poll={poll:g}s logs={log_dir}")
    try:
        while True:
            now = utc_now()
            _reconcile_running(store, actor, running, now, counts, quiet)

            if stop_event.is_set():
                _stop_running_jobs(running)
                _reconcile_running(store, actor, running, utc_now(), counts, quiet)
                if not running:
                    break
            else:
                capacity = max(0, parallel - len(running))
                claimed = claim_due_jobs(
                    store,
                    actor,
                    now,
                    worker_id=worker_id,
                    runner_host=hostname,
                    runner_pid=os.getpid(),
                    claim_timeout_seconds=config.worker_claim_timeout_seconds,
                    limit=capacity,
                )
                counts["claimed"] += len(claimed)
                for reservation in claimed:
                    started = _start_claimed_job(config, store, actor, reservation, log_dir)
                    if started is None:
                        counts["failed"] += 1
                        continue
                    running[started.reservation_id] = started
                    counts["started"] += 1
                    if not quiet:
                        print(
                            f"started: {started.reservation_id[:8]} pid={started.process.pid} "
                            f"gpu={','.join(map(str, reservation.get('gpus', [])))}"
                        )

                if once and not running and not claimed:
                    break

            stop_event.wait(min(poll, 0.2) if once and running else poll)
    finally:
        if running:
            _stop_running_jobs(running)
            deadline = time.monotonic() + 5.0
            while running and time.monotonic() < deadline:
                _reconcile_running(store, actor, running, utc_now(), counts, quiet)
                time.sleep(0.05)
            for item in running.values():
                _kill_process_group(item.process)
            _reconcile_running(store, actor, running, utc_now(), counts, quiet)
        _restore_signal_handlers(previous_handlers)
    summary = WorkerSummary(**counts)
    if not quiet:
        print(
            f"worker stopped: claimed={summary.claimed} started={summary.started} "
            f"succeeded={summary.succeeded} failed={summary.failed} cancelled={summary.cancelled}"
        )
    return summary


def claim_due_jobs(
    store: LedgerStore,
    actor: Actor,
    now: datetime,
    *,
    worker_id: str,
    runner_host: str,
    runner_pid: int,
    claim_timeout_seconds: float,
    limit: int,
) -> List[dict]:
    if limit <= 0:
        return []

    def mutate(ledger: dict):
        changed = False
        logs = []
        claimed = []
        stale_before = now - timedelta(seconds=claim_timeout_seconds)
        reservations = sorted(
            ledger.get("reservations", []),
            key=lambda item: (str(item.get("start_at", "")), str(item.get("id", ""))),
        )
        for reservation in reservations:
            if int(reservation.get("uid", -1)) != actor.uid:
                continue
            job = reservation.get("job")
            if not isinstance(job, dict):
                continue
            status = job.get("status")
            if status == JOB_CLAIMED and _timestamp_before(job.get("claimed_at"), stale_before):
                job["status"] = JOB_UNCERTAIN
                job["finished_at"] = to_iso(now)
                job["message"] = "worker disappeared after durable claim; not retried automatically"
                logs.append(_job_log(actor, "job-uncertain", reservation, "uncertain"))
                changed = True
                continue
            if status != JOB_PENDING:
                continue

            reservation_status = reservation.get("status")
            end_at = parse_iso(reservation["end_at"])
            if reservation_status == STATUS_CANCELLED:
                job["status"] = JOB_CANCELLED
                job["finished_at"] = to_iso(now)
                changed = True
                continue
            if reservation_status == STATUS_EXPIRED or end_at <= now:
                reservation["status"] = STATUS_EXPIRED
                reservation["updated_at"] = to_iso(now)
                job["status"] = JOB_MISSED
                job["finished_at"] = to_iso(now)
                logs.append(_job_log(actor, "job-missed", reservation, "reservation window ended"))
                changed = True
                continue
            if reservation_status != STATUS_ACTIVE or parse_iso(reservation["start_at"]) > now:
                continue
            if len(claimed) >= limit:
                continue

            claim_token = str(uuid.uuid4())
            job["status"] = JOB_CLAIMED
            job["claim_token"] = claim_token
            job["claimed_at"] = to_iso(now)
            job["worker_id"] = worker_id
            job["runner_host"] = runner_host
            job["runner_pid"] = runner_pid
            reservation["updated_at"] = to_iso(now)
            claimed.append(copy.deepcopy(reservation))
            logs.append(_job_log(actor, "job-claim", reservation, "claimed"))
            changed = True
        return ledger, claimed, logs, changed

    return store.transaction(mutate)


def job_log_path(config: Config, reservation_id: str) -> Path:
    log_dir = config.job_log_dir or (Path.home() / ".local" / "state" / "bk" / "jobs")
    try:
        normalized = str(uuid.UUID(str(reservation_id)))
    except (ValueError, AttributeError):
        normalized = hashlib.sha256(str(reservation_id).encode("utf-8", errors="replace")).hexdigest()
    return log_dir / f"{normalized}.log"


def job_spec_path(config: Config, spec_id: str) -> Path:
    log_dir = config.job_log_dir or (Path.home() / ".local" / "state" / "bk" / "jobs")
    try:
        normalized = str(uuid.UUID(str(spec_id)))
    except (ValueError, AttributeError) as exc:
        raise BookingError("invalid job spec ID") from exc
    return log_dir / "specs" / f"{normalized}.json"


def retry_job(
    store: LedgerStore,
    actor: Actor,
    reservation_id: str,
    *,
    accept_duplicate_risk: bool = False,
) -> dict:
    def mutate(ledger: dict):
        reservation = _find_reservation(ledger, reservation_id)
        if reservation is None:
            raise BookingError("job reservation not found")
        if int(reservation.get("uid", -1)) != actor.uid:
            raise BookingError("permission denied: job belongs to another UID")
        job = reservation.get("job")
        if not isinstance(job, dict):
            raise BookingError("reservation has no job")
        status = job.get("status")
        if status == JOB_UNCERTAIN and not accept_duplicate_risk:
            raise BookingError("uncertain job may already be running; pass --accept-duplicate-risk after checking")
        if status not in {JOB_FAILED, JOB_INTERRUPTED, JOB_UNCERTAIN}:
            raise BookingError(f"job in {status} state cannot be retried")
        now = utc_now()
        if reservation.get("status") != STATUS_ACTIVE or parse_iso(reservation["end_at"]) <= now:
            raise BookingError("reservation window is no longer active; create a new booking")
        job["status"] = JOB_PENDING
        for key in (
            "claim_token",
            "claimed_at",
            "started_at",
            "finished_at",
            "exit_code",
            "runner_pid",
            "runner_host",
            "worker_id",
            "message",
            "cancel_requested_at",
        ):
            job[key] = None
        reservation["updated_at"] = to_iso(now)
        return ledger, reservation, [_job_log(actor, "job-retry", reservation, "pending")], True

    return store.transaction(mutate)


def _start_claimed_job(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    reservation: dict,
    log_dir: Path,
) -> Optional[RunningJob]:
    job = reservation.get("job", {})
    claim_token = str(job.get("claim_token", ""))
    reservation_id = str(reservation.get("id", ""))
    log_path = job_log_path(config, reservation_id)
    if log_path.parent != log_dir:
        _mark_launch_failure(store, actor, reservation_id, claim_token, "invalid log path", None)
        return None
    try:
        argv, cwd = _validated_job_payload(config, actor, job)
        log_fh = _open_secure_log(log_path)
        header = {
            "event": "bk-job-start",
            "timestamp": to_iso(utc_now()),
            "reservation_id": reservation_id,
            "gpus": reservation.get("gpus", []),
            "cwd": cwd,
            "argv": argv,
        }
        log_fh.write((json.dumps(header, ensure_ascii=False) + "\n").encode("utf-8"))
        log_fh.flush()
        os.fsync(log_fh.fileno())

        env = os.environ.copy()
        physical_gpus = ",".join(str(item) for item in reservation.get("gpus", []))
        env["CUDA_VISIBLE_DEVICES"] = physical_gpus
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        env["BK_RESERVATION_ID"] = reservation_id
        env["BK_RESERVED_GPUS"] = physical_gpus
        if reservation.get("expected_memory_mb") is not None:
            env["BK_EXPECTED_GPU_MEMORY_MB"] = str(reservation["expected_memory_mb"])
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        log_fh.close()
    except (OSError, ValueError, BookingError) as exc:
        try:
            log_fh.close()  # type: ignore[possibly-undefined]
        except (UnboundLocalError, OSError):
            pass
        _mark_launch_failure(store, actor, reservation_id, claim_token, str(exc), str(log_path))
        return None

    try:
        marked_running = _mark_running(store, actor, reservation_id, claim_token, process.pid, str(log_path))
    except Exception:
        _terminate_process_group(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
        raise
    if not marked_running:
        _terminate_process_group(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
        return None
    return RunningJob(
        reservation_id=reservation_id,
        claim_token=claim_token,
        process=process,
        end_at=parse_iso(reservation["end_at"]),
    )


def _validated_job_payload(config: Config, actor: Actor, job: dict) -> Tuple[List[str], str]:
    if job.get("spec_id") is not None:
        payload = _read_job_spec(config, actor, str(job["spec_id"]))
        expected_digest = str(job.get("digest", ""))
        actual_digest = _job_spec_digest(payload)
        if not hmac.compare_digest(expected_digest, actual_digest):
            raise BookingError("private job spec digest does not match the shared ledger")
        if not hmac.compare_digest(str(payload.get("digest", "")), actual_digest):
            raise BookingError("private job spec is internally inconsistent")
        raw_argv = payload.get("argv")
        cwd = payload.get("cwd")
        return _validate_submission_payload(raw_argv, cwd)

    # Compatibility for pre-0.2 ledgers. New bookings never store argv in shared data.
    raw_argv = job.get("argv")
    cwd = job.get("cwd")
    return _validate_submission_payload(raw_argv, cwd)


def _validate_submission_payload(raw_argv, cwd) -> Tuple[List[str], str]:
    if not isinstance(raw_argv, list) or not raw_argv or not all(isinstance(item, str) for item in raw_argv):
        raise BookingError("invalid job argv")
    if len(raw_argv) > 256 or any("\x00" in item for item in raw_argv):
        raise BookingError("invalid job argv")
    if not raw_argv[0] or sum(len(item.encode("utf-8")) for item in raw_argv) > 64 * 1024:
        raise BookingError("invalid job argv")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or "\x00" in cwd:
        raise BookingError("job working directory must be absolute")
    if len(cwd.encode("utf-8")) > 4096:
        raise BookingError("invalid job working directory")
    path = Path(cwd)
    if not path.is_dir():
        raise BookingError(f"job working directory does not exist: {cwd}")
    return list(raw_argv), cwd


def _read_job_spec(config: Config, actor: Actor, spec_id: str) -> dict:
    path = job_spec_path(config, spec_id)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise BookingError("job spec is not a regular file")
        if metadata.st_uid != actor.uid:
            raise BookingError("job spec is not owned by the reservation UID")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise BookingError("job spec must not be accessible by group or other users")
        if metadata.st_size > 128 * 1024:
            raise BookingError("job spec is too large")
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
            fd = -1
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise BookingError("invalid private job spec")
    if int(payload.get("uid", -1)) != actor.uid or payload.get("spec_id") != spec_id:
        raise BookingError("private job spec identity mismatch")
    return payload


def _job_spec_digest(payload: dict) -> str:
    signed = {
        "version": payload.get("version"),
        "uid": payload.get("uid"),
        "argv": payload.get("argv"),
        "cwd": payload.get("cwd"),
    }
    raw = json.dumps(signed, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _job_command_summary(argv: List[str]) -> str:
    executable = Path(argv[0]).name or argv[0]
    detail = ""
    consumed = 1
    if executable.lower().startswith("python") and len(argv) > 1:
        if argv[1] == "-m" and len(argv) > 2:
            detail = f" -m {argv[2]}"
            consumed = 3
        elif argv[1] == "-c":
            detail = " -c"
            consumed = 2
        elif not argv[1].startswith("-"):
            detail = f" {Path(argv[1]).name}"
            consumed = 2
    hidden_count = max(0, len(argv) - consumed)
    suffix = f" (+{hidden_count} args)" if hidden_count else ""
    return f"{executable}{detail}{suffix}"[:200]


def _mark_running(
    store: LedgerStore,
    actor: Actor,
    reservation_id: str,
    claim_token: str,
    child_pid: int,
    log_path: str,
) -> bool:
    def mutate(ledger: dict):
        reservation = _find_reservation(ledger, reservation_id)
        if reservation is None or int(reservation.get("uid", -1)) != actor.uid:
            return ledger, False, [], False
        job = reservation.get("job")
        if (
            reservation.get("status") != STATUS_ACTIVE
            or not isinstance(job, dict)
            or job.get("status") != JOB_CLAIMED
            or job.get("claim_token") != claim_token
        ):
            return ledger, False, [], False
        now = utc_now()
        job["status"] = JOB_RUNNING
        job["started_at"] = to_iso(now)
        job["runner_pid"] = child_pid
        job["log_path"] = log_path
        reservation["updated_at"] = to_iso(now)
        return ledger, True, [_job_log(actor, "job-start", reservation, f"pid={child_pid}")], True

    return store.transaction(mutate)


def _mark_launch_failure(
    store: LedgerStore,
    actor: Actor,
    reservation_id: str,
    claim_token: str,
    message: str,
    log_path: Optional[str],
) -> None:
    def mutate(ledger: dict):
        reservation = _find_reservation(ledger, reservation_id)
        if reservation is None or int(reservation.get("uid", -1)) != actor.uid:
            return ledger, None, [], False
        job = reservation.get("job")
        if not isinstance(job, dict) or job.get("claim_token") != claim_token:
            return ledger, None, [], False
        now = utc_now()
        job["status"] = JOB_FAILED
        job["finished_at"] = to_iso(now)
        job["message"] = message[:1000]
        if log_path:
            job["log_path"] = log_path
        reservation["updated_at"] = to_iso(now)
        return ledger, None, [_job_log(actor, "job-failed", reservation, message[:200])], True

    store.transaction(mutate)


def _reconcile_running(
    store: LedgerStore,
    actor: Actor,
    running: Dict[str, RunningJob],
    now: datetime,
    counts: Dict[str, int],
    quiet: bool,
) -> None:
    if not running:
        return
    ledger = store.load()
    by_id = {str(item.get("id", "")): item for item in ledger.get("reservations", [])}
    for reservation_id, item in list(running.items()):
        reservation = by_id.get(reservation_id)
        if item.termination_reason is None:
            if reservation is None or reservation.get("status") == STATUS_CANCELLED:
                _request_termination(item, "cancelled")
            elif now >= item.end_at:
                _request_termination(item, "deadline")
        elif item.termination_requested_at is not None and time.monotonic() - item.termination_requested_at >= 5.0:
            _kill_process_group(item.process)

        exit_code = item.process.poll()
        if exit_code is None:
            continue
        status = _completion_status(exit_code, item.termination_reason)
        _complete_job(store, actor, item, status, exit_code)
        if status == JOB_SUCCEEDED:
            counts["succeeded"] += 1
        elif status == JOB_CANCELLED:
            counts["cancelled"] += 1
        else:
            counts["failed"] += 1
        if not quiet:
            print(f"finished: {reservation_id[:8]} status={status} exit={exit_code}")
        running.pop(reservation_id, None)


def _complete_job(
    store: LedgerStore,
    actor: Actor,
    running: RunningJob,
    status: str,
    exit_code: int,
) -> None:
    def mutate(ledger: dict):
        reservation = _find_reservation(ledger, running.reservation_id)
        if reservation is None or int(reservation.get("uid", -1)) != actor.uid:
            return ledger, None, [], False
        job = reservation.get("job")
        if not isinstance(job, dict) or job.get("claim_token") != running.claim_token:
            return ledger, None, [], False
        now = utc_now()
        job["status"] = status
        job["finished_at"] = to_iso(now)
        job["exit_code"] = int(exit_code)
        if running.termination_reason:
            job["message"] = running.termination_reason
        reservation["updated_at"] = to_iso(now)
        return ledger, None, [_job_log(actor, f"job-{status}", reservation, f"exit={exit_code}")], True

    store.transaction(mutate)


def _completion_status(exit_code: int, termination_reason: Optional[str]) -> str:
    if termination_reason == "cancelled":
        return JOB_CANCELLED
    if termination_reason == "deadline":
        return JOB_TIMED_OUT
    if termination_reason == "worker-stop":
        return JOB_INTERRUPTED
    return JOB_SUCCEEDED if exit_code == 0 else JOB_FAILED


def _stop_running_jobs(running: Dict[str, RunningJob]) -> None:
    for item in running.values():
        if item.termination_reason is None:
            _request_termination(item, "worker-stop")


def _request_termination(item: RunningJob, reason: str) -> None:
    item.termination_reason = reason
    item.termination_requested_at = time.monotonic()
    _terminate_process_group(item.process)


def _terminate_process_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            pass


def _kill_process_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _ensure_job_log_dir(config: Config, actor: Actor) -> Path:
    path = config.job_log_dir or (Path.home() / ".local" / "state" / "bk" / "jobs")
    if not path.is_absolute():
        raise BookingError(f"job log directory must be absolute: {path}")
    _ensure_private_directory(path, actor)
    return path


def _ensure_private_directory(path: Path, actor: Actor) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.stat()
    if metadata.st_uid != actor.uid:
        raise BookingError(f"private job directory is not owned by UID {actor.uid}: {path}")
    path.chmod(0o700)


def _open_secure_log(path: Path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "ab", buffering=0)


def _find_reservation(ledger: dict, reservation_id: str) -> Optional[dict]:
    for reservation in ledger.get("reservations", []):
        if reservation.get("id") == reservation_id:
            return reservation
    return None


def _timestamp_before(value: object, cutoff: datetime) -> bool:
    if not isinstance(value, str):
        return True
    try:
        return parse_iso(value) <= cutoff
    except (TypeError, ValueError):
        return True


def _job_log(actor: Actor, action: str, reservation: dict, message: str) -> dict:
    job = reservation.get("job", {})
    return {
        "ts": to_iso(utc_now()),
        "uid": actor.uid,
        "username": actor.username,
        "action": action,
        "reservation_id": reservation.get("id"),
        "op_id": reservation.get("op_id"),
        "gpus": reservation.get("gpus", []),
        "mode": reservation.get("mode"),
        "start_at": reservation.get("start_at"),
        "end_at": reservation.get("end_at"),
        "result": job.get("status"),
        "message": message,
    }


def _install_signal_handlers(stop_event: threading.Event) -> Dict[int, object]:
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, lambda _signum, _frame: stop_event.set())
    return previous


def _restore_signal_handlers(previous: Dict[int, object]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
