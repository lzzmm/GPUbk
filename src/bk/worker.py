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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .config import DEFAULT_WORKER_TERMINATION_GRACE_SECONDS, Config
from .fileio import fsync_directory, open_existing_regular
from .gpu import GpuSnapshot, snapshot
from .job_recovery import active_legacy_job_count, recover_abandoned_jobs
from .joblogs import (
    MIB,
    JobLogPump,
    acquire_job_worker_lease,
    cleanup_job_logs,
    ensure_job_log_dir,
    ensure_private_directory,
    job_log_path,
    job_log_root,
    validate_private_directory,
)
from .launch_guard import LaunchGuardDecision, assess_job_launch
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
from .scheduler import list_active
from .timeparse import parse_iso, to_iso, utc_now


SnapshotProvider = Callable[[Config], Sequence[GpuSnapshot]]
WORKER_WAITING_EXIT_CODE = 3
WORKER_BUSY_EXIT_CODE = 75
JOB_SPEC_CLEANUP_INTERVAL_SECONDS = 5 * 60
JOB_SPEC_ORPHAN_GRACE_SECONDS = 24 * 60 * 60


@dataclass
class RunningJob:
    reservation_id: str
    claim_token: str
    process: subprocess.Popen
    log_pump: JobLogPump
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
    waiting: int = 0
    recovered_uncertain: int = 0
    terminated_groups: int = 0


@dataclass(frozen=True)
class JobSpecReference:
    spec_id: str
    digest: str
    summary: str


@dataclass(frozen=True)
class JobSpecCleanupResult:
    removed: int = 0
    retained: int = 0
    deferred_orphans: int = 0
    failed: int = 0
    warnings: Tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "removed": self.removed,
            "retained": self.retained,
            "deferred_orphans": self.deferred_orphans,
            "failed": self.failed,
            "warnings": list(self.warnings),
        }


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
    ensure_job_log_dir(config, actor)
    path = job_spec_path(config, spec_id)
    ensure_private_directory(path.parent, actor)
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
        fsync_directory(path.parent)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    return JobSpecReference(spec_id, digest, _job_command_summary(argv))


def delete_job_spec(config: Config, spec_id: str) -> bool:
    path = job_spec_path(config, spec_id)
    status, _message = _remove_job_spec_file(path, os.getuid())
    return status == "removed"


def cleanup_job_specs(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    *,
    now: Optional[datetime] = None,
    orphan_grace_seconds: int = JOB_SPEC_ORPHAN_GRACE_SECONDS,
) -> JobSpecCleanupResult:
    if actor.uid != os.getuid():
        raise BookingError("job spec cleanup actor must match the current process UID")
    if orphan_grace_seconds < 0:
        raise ValueError("job spec orphan grace must be nonnegative")

    root = job_log_root(config)
    if not os.path.lexists(root):
        return JobSpecCleanupResult()
    validate_private_directory(root, actor)
    spec_dir = root / "specs"
    if not os.path.lexists(spec_dir):
        return JobSpecCleanupResult()
    validate_private_directory(spec_dir, actor)

    current = (now if now is not None else datetime.now(timezone.utc)).astimezone(timezone.utc)
    ledger = store.load()
    references: Dict[str, bool] = {}
    warnings: List[str] = []
    failed = 0
    for reservation in ledger.get("reservations", []):
        if not isinstance(reservation, dict):
            continue
        job = reservation.get("job")
        if not isinstance(job, dict) or job.get("spec_id") is None:
            continue
        try:
            spec_id = str(uuid.UUID(str(job["spec_id"])))
        except (ValueError, AttributeError):
            try:
                reservation_uid = int(reservation.get("uid", -1))
            except (TypeError, ValueError):
                reservation_uid = actor.uid
            if reservation_uid == actor.uid:
                failed += 1
                warnings.append(
                    f"reservation {str(reservation.get('id', ''))[:8]} has an invalid private job spec ID"
                )
            continue
        try:
            reservation_uid = int(reservation.get("uid", -1))
        except (TypeError, ValueError):
            references[spec_id] = True
            failed += 1
            warnings.append(
                f"reservation {str(reservation.get('id', ''))[:8]} has an invalid UID; retained {spec_id[:8]}"
            )
            continue
        if reservation_uid != actor.uid:
            continue
        needed = _job_spec_is_needed(reservation, job, current)
        references[spec_id] = references.get(spec_id, False) or needed

    removed = 0
    retained = 0
    for spec_id, needed in references.items():
        path = job_spec_path(config, spec_id)
        if needed:
            retained += 1
            issue = _job_spec_file_issue(path, actor.uid)
            if issue is not None:
                failed += 1
                warnings.append(f"{spec_id[:8]}: {issue}")
            continue
        status, message = _remove_job_spec_file(path, actor.uid)
        if status == "removed":
            removed += 1
        elif status == "failed":
            failed += 1
            warnings.append(f"{spec_id[:8]}: {message}")

    deferred_orphans = 0
    try:
        candidates = list(spec_dir.iterdir())
    except OSError as exc:
        return JobSpecCleanupResult(
            removed,
            retained,
            deferred_orphans,
            failed + 1,
            tuple([*warnings, f"cannot scan private job specs: {exc}"]),
        )
    for path in candidates:
        spec_id = _spec_id_from_filename(path.name)
        if spec_id is None or spec_id in references:
            continue
        issue = _job_spec_file_issue(path, actor.uid)
        if issue is not None:
            failed += 1
            warnings.append(f"{path.name}: {issue}")
            continue
        try:
            age_seconds = current.timestamp() - path.lstat().st_mtime
        except OSError as exc:
            failed += 1
            warnings.append(f"{path.name}: cannot inspect age: {exc}")
            continue
        if age_seconds < orphan_grace_seconds:
            deferred_orphans += 1
            continue
        status, message = _remove_job_spec_file(path, actor.uid)
        if status == "removed":
            removed += 1
        elif status == "failed":
            failed += 1
            warnings.append(f"{path.name}: {message}")

    return JobSpecCleanupResult(
        removed,
        retained,
        deferred_orphans,
        failed,
        tuple(warnings),
    )


def run_worker(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    *,
    once: bool = False,
    poll_seconds: Optional[float] = None,
    max_parallel: Optional[int] = None,
    quiet: bool = False,
    snapshot_provider: SnapshotProvider = snapshot,
) -> WorkerSummary:
    if actor.uid != os.getuid():
        raise BookingError("worker actor must match the current process UID")
    poll = config.worker_poll_seconds if poll_seconds is None else float(poll_seconds)
    if poll < 0.1:
        raise ValueError("worker poll interval must be >= 0.1 seconds")
    parallel = (
        max_parallel
        if max_parallel is not None
        else config.effective_worker_max_parallel
    )
    if parallel < 1:
        raise ValueError("worker max parallel jobs must be >= 1")

    worker_id = str(uuid.uuid4())
    hostname = socket.gethostname()
    lease = acquire_job_worker_lease(config, actor, worker_id, hostname)
    log_dir = lease.path.parent
    stop_event = threading.Event()
    previous_handlers: Dict[int, object] = {}
    running: Dict[str, RunningJob] = {}
    counts = {
        "claimed": 0,
        "started": 0,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
        "waiting": 0,
        "recovered_uncertain": 0,
        "terminated_groups": 0,
    }

    def reconcile(at: datetime) -> None:
        _reconcile_running(
            store,
            actor,
            running,
            at,
            counts,
            quiet,
            termination_grace_seconds=config.worker_termination_grace_seconds,
        )

    next_spec_cleanup_at = 0.0
    try:
        previous_handlers = _install_signal_handlers(stop_event)
        recovery = recover_abandoned_jobs(
            store,
            actor,
            hostname=hostname,
            worker_id=worker_id,
            now=utc_now(),
            grace_seconds=config.worker_recovery_grace_seconds,
        )
        counts["recovered_uncertain"] = recovery.uncertain_jobs
        counts["terminated_groups"] = recovery.terminated_groups
        legacy_blocked = recovery.legacy_active_jobs
        if not quiet:
            print(
                f"worker started: uid={actor.uid} poll={poll:g}s "
                f"parallel={parallel} "
                f"stop-grace={config.worker_termination_grace_seconds:g}s "
                f"logs={log_dir}"
            )
            if recovery.examined:
                print(
                    f"recovery: examined={recovery.examined} "
                    f"uncertain={recovery.uncertain_jobs} "
                    f"terminated_groups={recovery.terminated_groups} "
                    f"remote={recovery.remote_jobs} legacy={recovery.legacy_jobs} "
                    f"legacy_active={recovery.legacy_active_jobs}"
                )
            for warning in recovery.warnings:
                print(f"warning: {warning}")
        while True:
            now = utc_now()
            reconcile(now)
            monotonic_now = time.monotonic()
            if monotonic_now >= next_spec_cleanup_at:
                _cleanup_job_specs_best_effort(config, store, actor, now, quiet)
                _cleanup_job_logs_best_effort(config, store, actor, now, quiet)
                next_spec_cleanup_at = monotonic_now + JOB_SPEC_CLEANUP_INTERVAL_SECONDS

            if stop_event.is_set():
                _stop_running_jobs(running, now)
                reconcile(utc_now())
                if not running:
                    break
            else:
                current_legacy = active_legacy_job_count(store.load(), actor, now)
                if current_legacy:
                    counts["waiting"] = max(counts["waiting"], current_legacy)
                    if not quiet and current_legacy != legacy_blocked:
                        print(
                            f"waiting: {current_legacy} pre-lease job(s) may still be owned "
                            "by an older worker"
                        )
                    legacy_blocked = current_legacy
                    if once and not running:
                        break
                    stop_event.wait(poll)
                    continue
                counts["waiting"] = 0
                if legacy_blocked:
                    followup = recover_abandoned_jobs(
                        store,
                        actor,
                        hostname=hostname,
                        worker_id=worker_id,
                        now=now,
                        grace_seconds=config.worker_recovery_grace_seconds,
                    )
                    counts["recovered_uncertain"] += followup.uncertain_jobs
                    counts["terminated_groups"] += followup.terminated_groups
                    legacy_blocked = followup.legacy_active_jobs
                    if not quiet:
                        for warning in followup.warnings:
                            print(f"warning: {warning}")
                    if legacy_blocked:
                        continue
                capacity = max(0, parallel - len(running))
                eligible_ids = None
                launch_devices: Dict[str, Tuple[str, ...]] = {}
                if config.worker_live_guard:
                    eligible_ids, waiting, notices, launch_devices = _launch_guard_eligibility(
                        config,
                        store,
                        actor,
                        now,
                        snapshot_provider,
                    )
                    counts["waiting"] = waiting
                    if not quiet:
                        for notice in notices:
                            print(notice)
                claimed = claim_due_jobs(
                    store,
                    actor,
                    now,
                    worker_id=worker_id,
                    worker_lease_id=lease.worker_id,
                    runner_host=hostname,
                    runner_pid=os.getpid(),
                    claim_timeout_seconds=config.worker_claim_timeout_seconds,
                    limit=capacity,
                    eligible_ids=eligible_ids,
                )
                counts["claimed"] += len(claimed)
                for reservation in claimed:
                    started = _start_claimed_job(
                        config,
                        store,
                        actor,
                        reservation,
                        log_dir,
                        cuda_visible_devices=launch_devices.get(
                            str(reservation.get("id", ""))
                        ),
                        require_validated_devices=config.worker_live_guard,
                    )
                    if started is None:
                        if _stored_job_status(store, str(reservation.get("id", ""))) == JOB_CANCELLED:
                            counts["cancelled"] += 1
                        else:
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
        try:
            try:
                if running:
                    _stop_running_jobs(running, utc_now())
                    deadline = (
                        time.monotonic()
                        + config.worker_termination_grace_seconds
                    )
                    while running and time.monotonic() < deadline:
                        reconcile(utc_now())
                        time.sleep(0.05)
                    for item in running.values():
                        _kill_process_group(item.process)
                    reconcile(utc_now())
                _cleanup_job_specs_best_effort(config, store, actor, utc_now(), quiet)
                _cleanup_job_logs_best_effort(config, store, actor, utc_now(), quiet)
            finally:
                _restore_signal_handlers(previous_handlers)
        finally:
            lease.release()
    summary = WorkerSummary(**counts)
    if not quiet:
        print(
            f"worker stopped: claimed={summary.claimed} started={summary.started} "
            f"succeeded={summary.succeeded} failed={summary.failed} "
            f"cancelled={summary.cancelled} waiting={summary.waiting} "
            f"recovered={summary.recovered_uncertain} "
            f"terminated_groups={summary.terminated_groups}"
        )
    return summary


def claim_due_jobs(
    store: LedgerStore,
    actor: Actor,
    now: datetime,
    *,
    worker_id: str,
    worker_lease_id: str,
    runner_host: str,
    runner_pid: int,
    claim_timeout_seconds: float,
    limit: int,
    eligible_ids: Optional[Set[str]] = None,
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
                message = (
                    "reservation window ended while waiting for live GPU safety"
                    if job.get("launch_guard_state") == "waiting"
                    else "reservation window ended"
                )
                job["message"] = message
                logs.append(_job_log(actor, "job-missed", reservation, message))
                changed = True
                continue
            if reservation_status != STATUS_ACTIVE or parse_iso(reservation["start_at"]) > now:
                continue
            reservation_id = str(reservation.get("id", ""))
            if eligible_ids is not None and reservation_id not in eligible_ids:
                continue
            if len(claimed) >= limit:
                continue

            claim_token = str(uuid.uuid4())
            job["status"] = JOB_CLAIMED
            job["claim_token"] = claim_token
            job["claimed_at"] = to_iso(now)
            job["worker_id"] = worker_id
            job["worker_lease_id"] = worker_lease_id
            job["runner_host"] = runner_host
            job["runner_pid"] = runner_pid
            job["message"] = None
            job.pop("launch_guard_state", None)
            job.pop("launch_guard_key", None)
            job.pop("waiting_since", None)
            reservation["updated_at"] = to_iso(now)
            claimed.append(copy.deepcopy(reservation))
            logs.append(_job_log(actor, "job-claim", reservation, "claimed"))
            changed = True
        return ledger, claimed, logs, changed

    return store.transaction(mutate)


def _launch_guard_eligibility(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    now: datetime,
    snapshot_provider: SnapshotProvider,
) -> tuple[Set[str], int, List[str], Dict[str, Tuple[str, ...]]]:
    ledger = store.load()
    due = _due_pending_jobs(ledger, actor, now)
    if not due:
        return set(), 0, [], {}
    devices = list(snapshot_provider(config))
    active = list_active(ledger, now)
    decisions = {
        str(reservation.get("id", "")): assess_job_launch(
            config,
            reservation,
            devices,
            active,
            at=now,
        )
        for reservation in due
    }
    notices = _record_launch_guard_decisions(store, actor, decisions, now)
    eligible = {
        reservation_id
        for reservation_id, decision in decisions.items()
        if decision.ready
    }
    launch_devices = {
        reservation_id: decision.cuda_visible_devices
        for reservation_id, decision in decisions.items()
        if decision.ready and decision.cuda_visible_devices
    }
    return eligible, len(decisions) - len(eligible), notices, launch_devices


def _due_pending_jobs(ledger: dict, actor: Actor, now: datetime) -> List[dict]:
    return [
        reservation
        for reservation in ledger.get("reservations", [])
        if int(reservation.get("uid", -1)) == actor.uid
        and reservation.get("status") == STATUS_ACTIVE
        and isinstance(reservation.get("job"), dict)
        and reservation["job"].get("status") == JOB_PENDING
        and parse_iso(reservation["start_at"]) <= now < parse_iso(reservation["end_at"])
    ]


def _record_launch_guard_decisions(
    store: LedgerStore,
    actor: Actor,
    decisions: Dict[str, LaunchGuardDecision],
    now: datetime,
) -> List[str]:
    def mutate(ledger: dict):
        changed = False
        logs = []
        notices = []
        for reservation in ledger.get("reservations", []):
            reservation_id = str(reservation.get("id", ""))
            decision = decisions.get(reservation_id)
            if decision is None or int(reservation.get("uid", -1)) != actor.uid:
                continue
            job = reservation.get("job")
            if not isinstance(job, dict) or job.get("status") != JOB_PENDING:
                continue
            if decision.ready:
                if job.get("launch_guard_state") != "waiting":
                    continue
                job.pop("launch_guard_state", None)
                job.pop("launch_guard_key", None)
                job.pop("waiting_since", None)
                job["message"] = None
                reservation["updated_at"] = to_iso(now)
                logs.append(_job_log(actor, "job-ready", reservation, "live GPU guard cleared"))
                notices.append(f"ready: {reservation_id[:8]} live GPU guard cleared")
                changed = True
                continue
            reason = decision.reason[:1000]
            if (
                job.get("launch_guard_state") == "waiting"
                and job.get("launch_guard_key") == decision.key
            ):
                continue
            job["launch_guard_state"] = "waiting"
            job["launch_guard_key"] = decision.key
            if not job.get("waiting_since"):
                job["waiting_since"] = to_iso(now)
            job["message"] = reason
            reservation["updated_at"] = to_iso(now)
            logs.append(_job_log(actor, "job-waiting", reservation, reason[:200]))
            notices.append(f"waiting: {reservation_id[:8]} {reason}")
            changed = True
        return ledger, notices, logs, changed

    return store.transaction(mutate)


def job_spec_path(config: Config, spec_id: str) -> Path:
    log_dir = job_log_root(config)
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
            "worker_lease_id",
            "message",
            "cancel_requested_at",
            "launch_guard_state",
            "launch_guard_key",
            "waiting_since",
            "log_warning",
            "log_rotated",
            "recovery_state",
            "recovery_worker_id",
            "recovered_at",
        ):
            job.pop(key, None)
        reservation["updated_at"] = to_iso(now)
        return ledger, reservation, [_job_log(actor, "job-retry", reservation, "pending")], True

    return store.transaction(mutate)


def _start_claimed_job(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    reservation: dict,
    log_dir: Path,
    cuda_visible_devices: Optional[Sequence[str]] = None,
    *,
    require_validated_devices: bool = False,
) -> Optional[RunningJob]:
    job = reservation.get("job", {})
    claim_token = str(job.get("claim_token", ""))
    reservation_id = str(reservation.get("id", ""))
    log_path = job_log_path(config, reservation_id)
    if log_path.parent != log_dir:
        _mark_launch_failure(store, actor, reservation_id, claim_token, "invalid log path")
        return None
    process: Optional[subprocess.Popen] = None
    log_pump: Optional[JobLogPump] = None
    try:
        argv, cwd = _validated_job_payload(config, actor, job)
        header = {
            "event": "bk-job-start",
            "timestamp": to_iso(utc_now()),
            "reservation_id": reservation_id,
            "gpus": reservation.get("gpus", []),
            "cwd": cwd,
            "argv": argv,
        }
        log_pump = JobLogPump(
            log_path,
            actor,
            config.job_log_max_mb * MIB,
            header,
        )

        env = os.environ.copy()
        reserved_gpus = tuple(str(item) for item in reservation.get("gpus", []))
        launch_devices = tuple(cuda_visible_devices or ())
        if len(launch_devices) != len(reserved_gpus):
            if require_validated_devices:
                raise BookingError("validated GPU launch binding is unavailable")
            launch_devices = reserved_gpus
        env["CUDA_VISIBLE_DEVICES"] = ",".join(launch_devices)
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        env["BK_RESERVATION_ID"] = reservation_id
        env["BK_RESERVED_GPUS"] = ",".join(reserved_gpus)
        if reservation.get("expected_memory_mb") is not None:
            env["BK_EXPECTED_GPU_MEMORY_MB"] = str(reservation["expected_memory_mb"])
        if not _claim_is_launchable(store, actor, reservation_id, claim_token):
            log_pump.record_event(
                {
                    "event": "bk-job-launch-aborted",
                    "timestamp": to_iso(utc_now()),
                    "reason": "claim is no longer active",
                }
            )
            log_pump.abort()
            _mark_aborted_claim(store, actor, reservation_id, claim_token)
            return None
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=True,
            close_fds=True,
        )
        if process.stdout is None:
            raise OSError("failed to open the job log stream")
        log_pump.start(process.stdout)
    except (OSError, ValueError, BookingError) as exc:
        if process is not None:
            _terminate_and_reap_process_group(
                process,
                config.worker_termination_grace_seconds,
            )
        if log_pump is not None:
            try:
                log_pump.record_event(
                    {
                        "event": "bk-job-launch-error",
                        "timestamp": to_iso(utc_now()),
                        "error": str(exc),
                    }
                )
            except (OSError, ValueError, BookingError, RuntimeError):
                pass
            log_pump.abort()
        _mark_launch_failure(
            store,
            actor,
            reservation_id,
            claim_token,
            _public_launch_failure(exc),
        )
        return None

    try:
        marked_running = _mark_running(store, actor, reservation_id, claim_token, process.pid)
    except Exception:
        _terminate_and_reap_process_group(
            process,
            config.worker_termination_grace_seconds,
        )
        log_pump.finish()
        raise
    if not marked_running:
        _terminate_and_reap_process_group(
            process,
            config.worker_termination_grace_seconds,
        )
        log_pump.finish()
        _mark_aborted_claim(store, actor, reservation_id, claim_token)
        return None
    return RunningJob(
        reservation_id=reservation_id,
        claim_token=claim_token,
        process=process,
        log_pump=log_pump,
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
    fd = open_existing_regular(path)
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
        fh = os.fdopen(fd, "r", encoding="utf-8")
        fd = -1
        with fh:
            payload = json.load(fh)
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
        reservation["updated_at"] = to_iso(now)
        return ledger, True, [_job_log(actor, "job-start", reservation, f"pid={child_pid}")], True

    return store.transaction(mutate)


def _claim_is_launchable(
    store: LedgerStore,
    actor: Actor,
    reservation_id: str,
    claim_token: str,
) -> bool:
    reservation = _find_reservation(store.load(), reservation_id)
    if reservation is None or int(reservation.get("uid", -1)) != actor.uid:
        return False
    job = reservation.get("job")
    return (
        reservation.get("status") == STATUS_ACTIVE
        and isinstance(job, dict)
        and job.get("status") == JOB_CLAIMED
        and job.get("claim_token") == claim_token
        and parse_iso(reservation["end_at"]) > utc_now()
    )


def _mark_launch_failure(
    store: LedgerStore,
    actor: Actor,
    reservation_id: str,
    claim_token: str,
    message: str,
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
        reservation["updated_at"] = to_iso(now)
        return ledger, None, [_job_log(actor, "job-failed", reservation, message[:200])], True

    store.transaction(mutate)


def _mark_aborted_claim(
    store: LedgerStore,
    actor: Actor,
    reservation_id: str,
    claim_token: str,
) -> None:
    def mutate(ledger: dict):
        reservation = _find_reservation(ledger, reservation_id)
        if reservation is None or int(reservation.get("uid", -1)) != actor.uid:
            return ledger, None, [], False
        job = reservation.get("job")
        if (
            not isinstance(job, dict)
            or job.get("status") != JOB_CLAIMED
            or job.get("claim_token") != claim_token
        ):
            return ledger, None, [], False
        now = utc_now()
        if reservation.get("status") == STATUS_CANCELLED:
            status = JOB_CANCELLED
            message = "reservation was cancelled while the claimed command was starting"
        elif parse_iso(reservation["end_at"]) <= now:
            status = JOB_MISSED
            message = "reservation ended while the claimed command was starting"
        else:
            status = JOB_UNCERTAIN
            message = "claimed command was stopped before running state could be committed"
        job["status"] = status
        job["finished_at"] = to_iso(now)
        job["message"] = message
        reservation["updated_at"] = to_iso(now)
        return ledger, None, [_job_log(actor, f"job-{status}", reservation, message)], True

    store.transaction(mutate)


def _reconcile_running(
    store: LedgerStore,
    actor: Actor,
    running: Dict[str, RunningJob],
    now: datetime,
    counts: Dict[str, int],
    quiet: bool,
    *,
    termination_grace_seconds: float = DEFAULT_WORKER_TERMINATION_GRACE_SECONDS,
) -> None:
    if not running:
        return
    ledger = store.load()
    by_id = {str(item.get("id", "")): item for item in ledger.get("reservations", [])}
    graceful_start = timedelta(seconds=termination_grace_seconds)
    for reservation_id, item in list(running.items()):
        reservation = by_id.get(reservation_id)
        if reservation is None or reservation.get("status") == STATUS_CANCELLED:
            if item.termination_reason != "cancelled":
                _request_termination(item, "cancelled")
        elif item.termination_reason is None and now >= item.end_at - graceful_start:
            _request_termination(item, "deadline")

        if item.termination_reason == "deadline":
            if now >= item.end_at:
                _kill_process_group(item.process)
        elif (
            item.termination_requested_at is not None
            and time.monotonic() - item.termination_requested_at
            >= termination_grace_seconds
        ):
            _kill_process_group(item.process)

        exit_code = item.process.poll()
        if exit_code is None or _process_group_alive(item.process):
            continue
        log_warning = item.log_pump.finish()
        if log_warning and not quiet:
            print(f"warning: {reservation_id[:8]} private job log is incomplete: {log_warning}")
        status = _completion_status(exit_code, item.termination_reason)
        _complete_job(
            store,
            actor,
            item,
            status,
            exit_code,
            log_warning=log_warning,
            log_rotations=item.log_pump.rotation_count,
        )
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
    *,
    log_warning: Optional[str],
    log_rotations: int,
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
        for key in ("recovery_state", "recovery_worker_id", "recovered_at"):
            job.pop(key, None)
        if log_rotations:
            job["log_rotated"] = True
        if log_warning:
            job["log_warning"] = "private job log is incomplete; inspect the owning worker"
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


def _public_launch_failure(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, FileNotFoundError):
        return "scheduled executable or working directory was not found"
    if isinstance(exc, PermissionError):
        return "scheduled executable or private job storage was not accessible"
    if isinstance(exc, BookingError):
        if "working directory" in message:
            return "scheduled working directory is invalid or no longer exists"
        if "digest" in message or "internally inconsistent" in message:
            return "private job spec integrity check failed"
        if "job spec" in message or "private job" in message or "job log" in message:
            return "private job storage validation failed"
        return "scheduled command validation failed"
    if isinstance(exc, ValueError):
        return "scheduled command configuration is invalid"
    return "scheduled command could not be started"


def _stop_running_jobs(
    running: Dict[str, RunningJob],
    now: Optional[datetime] = None,
) -> None:
    current = now or utc_now()
    for item in running.values():
        if item.termination_reason == "cancelled":
            continue
        reason = "deadline" if current >= item.end_at else "worker-stop"
        _request_termination(item, reason)


def _request_termination(item: RunningJob, reason: str) -> None:
    item.termination_reason = reason
    if item.termination_requested_at is None:
        item.termination_requested_at = time.monotonic()
        _terminate_process_group(item.process)


def _terminate_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass


def _terminate_and_reap_process_group(
    process: subprocess.Popen,
    grace_seconds: float = 5.0,
) -> None:
    _terminate_process_group(process)
    deadline = time.monotonic() + grace_seconds
    while _process_group_alive(process) and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.05)
    if _process_group_alive(process):
        _kill_process_group(process)
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass


def _process_group_alive(process: subprocess.Popen) -> bool:
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _job_spec_is_needed(reservation: dict, job: dict, now: datetime) -> bool:
    status = job.get("status")
    if status in {JOB_CLAIMED, JOB_RUNNING}:
        return True
    if status not in {
        JOB_PENDING,
        JOB_SUCCEEDED,
        JOB_FAILED,
        JOB_CANCELLED,
        JOB_MISSED,
        JOB_TIMED_OUT,
        JOB_INTERRUPTED,
        JOB_UNCERTAIN,
    }:
        return True
    if status in {JOB_SUCCEEDED, JOB_CANCELLED, JOB_MISSED, JOB_TIMED_OUT}:
        return False
    if reservation.get("status") != STATUS_ACTIVE:
        return False
    try:
        return parse_iso(str(reservation["end_at"])) > now
    except (KeyError, TypeError, ValueError):
        return True


def _spec_id_from_filename(name: str) -> Optional[str]:
    if not name.endswith(".json"):
        return None
    try:
        return str(uuid.UUID(name[:-5]))
    except (ValueError, AttributeError):
        return None


def _job_spec_file_issue(path: Path, uid: int) -> Optional[str]:
    if not os.path.lexists(path):
        return "referenced private job spec is missing"
    try:
        metadata = path.lstat()
    except OSError as exc:
        return f"cannot inspect private job spec: {exc}"
    if not stat.S_ISREG(metadata.st_mode):
        return "private job spec is not a regular file"
    if metadata.st_uid != uid:
        return f"private job spec is not owned by UID {uid}"
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        return "private job spec is accessible by group or other users"
    return None


def _remove_job_spec_file(path: Path, uid: int) -> Tuple[str, Optional[str]]:
    if not os.path.lexists(path):
        return "missing", None
    issue = _job_spec_file_issue(path, uid)
    if issue is not None:
        return "failed", issue
    try:
        path.unlink()
        fsync_directory(path.parent)
    except OSError as exc:
        return "failed", str(exc)
    return "removed", None


def _cleanup_job_specs_best_effort(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    now: datetime,
    quiet: bool,
) -> None:
    try:
        result = cleanup_job_specs(config, store, actor, now=now)
    except (BookingError, OSError, ValueError) as exc:
        if not quiet:
            print(f"warning: private job spec cleanup failed: {exc}")
        return
    if result.failed and not quiet:
        detail = result.warnings[0] if result.warnings else "unknown cleanup error"
        print(f"warning: {result.failed} private job spec cleanup issue(s): {detail}")


def _cleanup_job_logs_best_effort(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    now: datetime,
    quiet: bool,
) -> None:
    try:
        result = cleanup_job_logs(config, store.load(), actor, now=now)
    except (BookingError, OSError, ValueError) as exc:
        if not quiet:
            print(f"warning: private job log cleanup failed: {exc}")
        return
    if result.failed and not quiet:
        detail = result.warnings[0] if result.warnings else "unknown cleanup error"
        print(f"warning: {result.failed} private job log cleanup issue(s): {detail}")


def _find_reservation(ledger: dict, reservation_id: str) -> Optional[dict]:
    for reservation in ledger.get("reservations", []):
        if reservation.get("id") == reservation_id:
            return reservation
    return None


def _stored_job_status(store: LedgerStore, reservation_id: str) -> Optional[str]:
    reservation = _find_reservation(store.load(), reservation_id)
    job = reservation.get("job") if reservation is not None else None
    return str(job.get("status")) if isinstance(job, dict) else None


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
