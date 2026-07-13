from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .models import (
    JOB_CLAIMED,
    JOB_RUNNING,
    JOB_UNCERTAIN,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    Actor,
)
from .storage import LedgerStore
from .timeparse import parse_iso, to_iso


MAX_PROC_ENVIRON_BYTES = 1024 * 1024
RECOVERABLE_STATES = {JOB_CLAIMED, JOB_RUNNING}
RETRYABLE_RECOVERY_STATES = {
    "terminating",
    "remote-unverified",
    "unverified",
    "termination-unverified",
    "legacy-remote",
    "legacy-unverified",
    "legacy-not-found",
}


@dataclass(frozen=True)
class ManagedProcessGroup:
    pgid: int
    member_pid: int
    member_start_id: str


@dataclass(frozen=True)
class ProcessDiscovery:
    available: bool
    groups: Tuple[ManagedProcessGroup, ...] = ()
    warning: Optional[str] = None


@dataclass(frozen=True)
class RecoverySummary:
    examined: int = 0
    terminated_groups: int = 0
    uncertain_jobs: int = 0
    remote_jobs: int = 0
    legacy_jobs: int = 0
    legacy_active_jobs: int = 0
    warnings: Tuple[str, ...] = ()


ProcessGroupDiscoverer = Callable[[str, int], ProcessDiscovery]


def discover_managed_process_groups(
    reservation_id: str,
    uid: int,
    *,
    proc_root: Path = Path("/proc"),
) -> ProcessDiscovery:
    if not proc_root.is_dir():
        return ProcessDiscovery(False, warning="Linux /proc process discovery is unavailable")
    try:
        entries = list(proc_root.iterdir())
    except OSError as exc:
        return ProcessDiscovery(False, warning=f"cannot scan /proc: {exc}")

    marker = f"BK_RESERVATION_ID={reservation_id}".encode("utf-8")
    groups: Dict[int, ManagedProcessGroup] = {}
    current_group = os.getpgrp()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            metadata = entry.stat()
            if metadata.st_uid != uid:
                continue
            pgid = _proc_process_group((entry / "stat").read_text(encoding="utf-8"))
            if pgid <= 1 or pgid == current_group:
                continue
            environ = _read_proc_environ(entry / "environ")
        except (OSError, ValueError):
            continue
        if marker not in environ.split(b"\0"):
            continue
        pid = int(entry.name)
        candidate = ManagedProcessGroup(
            pgid=pgid,
            member_pid=pid,
            member_start_id=f"{int(metadata.st_ino)}:{int(metadata.st_ctime_ns)}",
        )
        previous = groups.get(pgid)
        if previous is None or candidate.member_pid < previous.member_pid:
            groups[pgid] = candidate
    return ProcessDiscovery(True, tuple(groups[key] for key in sorted(groups)))


def recover_abandoned_jobs(
    store: LedgerStore,
    actor: Actor,
    *,
    hostname: str,
    worker_id: str,
    now: datetime,
    grace_seconds: float,
    discoverer: ProcessGroupDiscoverer = discover_managed_process_groups,
) -> RecoverySummary:
    if grace_seconds <= 0:
        raise ValueError("worker recovery grace must be positive")
    current = now.astimezone(timezone.utc)
    candidates = _recovery_candidates(store.load(), actor)
    if not candidates:
        return RecoverySummary()

    decisions: Dict[str, Tuple[str, ProcessDiscovery, str]] = {}
    warnings: List[str] = []
    remote_jobs = 0
    legacy_jobs = 0
    legacy_active_jobs = 0
    for reservation in candidates:
        reservation_id = str(reservation.get("id", ""))
        job = reservation["job"]
        lease_id = str(job.get("worker_lease_id") or "")
        runner_host = str(job.get("runner_host") or "")
        end_at = _safe_end_at(reservation)
        if not lease_id:
            legacy_jobs += 1
            if end_at is None or end_at > current:
                legacy_active_jobs += 1
                continue
            if runner_host and runner_host != hostname:
                remote_jobs += 1
                decisions[reservation_id] = (
                    "legacy-remote",
                    ProcessDiscovery(False),
                    "legacy job expired on another host; process state cannot be verified locally",
                )
                continue
            discovery = discoverer(reservation_id, actor.uid)
            if not discovery.available:
                message = discovery.warning or "managed process discovery is unavailable"
                warnings.append(f"{reservation_id[:8]}: {message}")
                decisions[reservation_id] = ("legacy-unverified", discovery, message)
            elif not discovery.groups:
                decisions[reservation_id] = (
                    "legacy-not-found",
                    discovery,
                    "legacy job expired without a visible owned process group",
                )
            else:
                decisions[reservation_id] = (
                    "terminating",
                    discovery,
                    "legacy job outlived its reservation; terminating its verified process group",
                )
            continue
        if lease_id == worker_id:
            continue
        if runner_host and runner_host != hostname:
            remote_jobs += 1
            decisions[reservation_id] = (
                "remote-unverified",
                ProcessDiscovery(False),
                "previous worker ran on another host; process state cannot be verified locally",
            )
            continue
        discovery = discoverer(reservation_id, actor.uid)
        if not discovery.available:
            message = discovery.warning or "managed process discovery is unavailable"
            warnings.append(f"{reservation_id[:8]}: {message}")
            decisions[reservation_id] = ("unverified", discovery, message)
        elif not discovery.groups:
            decisions[reservation_id] = (
                "not-found",
                discovery,
                "previous worker disappeared; no owned process group was found; not retried automatically",
            )
        else:
            decisions[reservation_id] = (
                "terminating",
                discovery,
                "previous worker disappeared; terminating its verified process group",
            )

    terminating_ids = _record_recovery_decisions(
        store,
        actor,
        current,
        worker_id,
        decisions,
    )
    signalled, signal_warnings = _discover_and_signal_groups(
        terminating_ids,
        actor.uid,
        discoverer,
        signal.SIGTERM,
    )
    warnings.extend(signal_warnings)
    remaining = _wait_for_groups(
        terminating_ids,
        actor.uid,
        discoverer,
        grace_seconds,
    )
    if remaining:
        killed, kill_warnings = _discover_and_signal_groups(
            tuple(remaining),
            actor.uid,
            discoverer,
            signal.SIGKILL,
        )
        for reservation_id, groups in killed.items():
            known = {item.pgid: item for item in signalled.get(reservation_id, ())}
            known.update({item.pgid: item for item in groups})
            signalled[reservation_id] = tuple(known[key] for key in sorted(known))
        warnings.extend(kill_warnings)
        remaining = _wait_for_groups(
            tuple(remaining),
            actor.uid,
            discoverer,
            min(1.0, grace_seconds),
        )
    terminated_groups = sum(
        len(groups)
        for reservation_id, groups in signalled.items()
        if reservation_id not in remaining
    )
    _finish_recovery(
        store,
        actor,
        datetime.now(timezone.utc),
        worker_id,
        terminating_ids,
        remaining,
        set(signalled),
    )
    return RecoverySummary(
        examined=len(candidates),
        terminated_groups=terminated_groups,
        uncertain_jobs=len(decisions),
        remote_jobs=remote_jobs,
        legacy_jobs=legacy_jobs,
        legacy_active_jobs=legacy_active_jobs,
        warnings=tuple(warnings),
    )


def active_legacy_job_count(ledger: dict, actor: Actor, now: datetime) -> int:
    current = now.astimezone(timezone.utc)
    count = 0
    for reservation in ledger.get("reservations", []):
        if not isinstance(reservation, dict) or not _owned_by(reservation, actor.uid):
            continue
        job = reservation.get("job")
        if not isinstance(job, dict) or job.get("status") not in RECOVERABLE_STATES:
            continue
        if job.get("worker_lease_id"):
            continue
        end_at = _safe_end_at(reservation)
        if end_at is None or end_at > current:
            count += 1
    return count


def _recovery_candidates(ledger: dict, actor: Actor) -> List[dict]:
    candidates = []
    for reservation in ledger.get("reservations", []):
        if not isinstance(reservation, dict) or not _owned_by(reservation, actor.uid):
            continue
        job = reservation.get("job")
        if not isinstance(job, dict):
            continue
        status = job.get("status")
        recovery_state = job.get("recovery_state")
        if status in RECOVERABLE_STATES or (
            status == JOB_UNCERTAIN and recovery_state in RETRYABLE_RECOVERY_STATES
        ):
            candidates.append(reservation)
    return candidates


def _record_recovery_decisions(
    store: LedgerStore,
    actor: Actor,
    now: datetime,
    worker_id: str,
    decisions: Dict[str, Tuple[str, ProcessDiscovery, str]],
) -> Tuple[str, ...]:
    def mutate(ledger: dict):
        changed = False
        logs = []
        terminating = []
        for reservation in ledger.get("reservations", []):
            reservation_id = str(reservation.get("id", ""))
            decision = decisions.get(reservation_id)
            if decision is None or not _owned_by(reservation, actor.uid):
                continue
            job = reservation.get("job")
            if not isinstance(job, dict):
                continue
            state, _discovery, message = decision
            if job.get("status") not in {
                JOB_CLAIMED,
                JOB_RUNNING,
                JOB_UNCERTAIN,
            }:
                continue
            if job.get("worker_lease_id") == worker_id:
                continue
            end_at = _safe_end_at(reservation)
            if end_at is not None and end_at <= now and reservation.get("status") == STATUS_ACTIVE:
                reservation["status"] = STATUS_EXPIRED
            job["status"] = JOB_UNCERTAIN
            job["finished_at"] = to_iso(now)
            job["message"] = message
            job["recovery_state"] = state
            job["recovery_worker_id"] = worker_id
            job["recovered_at"] = to_iso(now)
            reservation["updated_at"] = to_iso(now)
            logs.append(_recovery_log(actor, reservation, state, message))
            changed = True
            if state == "terminating":
                terminating.append(reservation_id)
        return ledger, tuple(terminating), logs, changed

    return store.transaction(mutate)


def _wait_for_groups(
    reservation_ids: Sequence[str],
    uid: int,
    discoverer: ProcessGroupDiscoverer,
    timeout: float,
) -> Dict[str, Tuple[ManagedProcessGroup, ...]]:
    deadline = time.monotonic() + timeout
    remaining: Dict[str, Tuple[ManagedProcessGroup, ...]] = {}
    while True:
        remaining = {}
        for reservation_id in reservation_ids:
            discovery = discoverer(reservation_id, uid)
            if not discovery.available:
                remaining[reservation_id] = ()
            elif discovery.groups:
                remaining[reservation_id] = discovery.groups
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _signal_groups(
    groups_by_reservation: Dict[str, Tuple[ManagedProcessGroup, ...]],
    signum: int,
) -> Tuple[Dict[str, Tuple[ManagedProcessGroup, ...]], List[str]]:
    signalled: Dict[str, Tuple[ManagedProcessGroup, ...]] = {}
    warnings: List[str] = []
    current_group = os.getpgrp()
    for reservation_id, groups in groups_by_reservation.items():
        successful = []
        for group in groups:
            if group.pgid <= 1 or group.pgid == current_group:
                warnings.append(f"{reservation_id[:8]}: refused unsafe process group {group.pgid}")
                continue
            try:
                os.killpg(group.pgid, signum)
            except ProcessLookupError:
                continue
            except OSError as exc:
                warnings.append(
                    f"{reservation_id[:8]}: cannot signal process group {group.pgid}: {exc}"
                )
                continue
            successful.append(group)
        if successful:
            signalled[reservation_id] = tuple(successful)
    return signalled, warnings


def _discover_and_signal_groups(
    reservation_ids: Sequence[str],
    uid: int,
    discoverer: ProcessGroupDiscoverer,
    signum: int,
) -> Tuple[Dict[str, Tuple[ManagedProcessGroup, ...]], List[str]]:
    current: Dict[str, Tuple[ManagedProcessGroup, ...]] = {}
    warnings: List[str] = []
    for reservation_id in reservation_ids:
        discovery = discoverer(reservation_id, uid)
        if not discovery.available:
            warnings.append(
                f"{reservation_id[:8]}: process identity could not be rechecked before signalling"
            )
            continue
        if discovery.groups:
            current[reservation_id] = discovery.groups
    signalled, signal_warnings = _signal_groups(current, signum)
    warnings.extend(signal_warnings)
    return signalled, warnings


def _finish_recovery(
    store: LedgerStore,
    actor: Actor,
    now: datetime,
    worker_id: str,
    reservation_ids: Sequence[str],
    remaining: Dict[str, Tuple[ManagedProcessGroup, ...]],
    signalled: set[str],
) -> None:
    wanted = set(reservation_ids)

    def mutate(ledger: dict):
        changed = False
        logs = []
        for reservation in ledger.get("reservations", []):
            reservation_id = str(reservation.get("id", ""))
            if reservation_id not in wanted or not _owned_by(reservation, actor.uid):
                continue
            job = reservation.get("job")
            if (
                not isinstance(job, dict)
                or job.get("status") != JOB_UNCERTAIN
                or job.get("recovery_worker_id") != worker_id
                or job.get("recovery_state") != "terminating"
            ):
                continue
            if reservation_id in remaining:
                state = "termination-unverified"
                message = "orphaned process group could not be confirmed stopped; do not retry automatically"
            elif reservation_id not in signalled:
                state = "disappeared"
                message = "orphaned process group disappeared during recovery; prior completion remains uncertain"
            else:
                state = "terminated"
                message = "orphaned process group stopped after worker restart; prior completion remains uncertain"
            job["recovery_state"] = state
            job["message"] = message
            job["finished_at"] = to_iso(now)
            reservation["updated_at"] = to_iso(now)
            logs.append(_recovery_log(actor, reservation, state, message))
            changed = True
        return ledger, None, logs, changed

    store.transaction(mutate)


def _read_proc_environ(path: Path) -> bytes:
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        data = os.read(fd, MAX_PROC_ENVIRON_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_PROC_ENVIRON_BYTES:
        raise ValueError("process environment exceeds the recovery inspection limit")
    return data


def _proc_process_group(raw_stat: str) -> int:
    close = raw_stat.rfind(")")
    if close < 0:
        raise ValueError("invalid /proc stat record")
    fields = raw_stat[close + 1 :].split()
    if len(fields) < 3:
        raise ValueError("incomplete /proc stat record")
    return int(fields[2])


def _safe_end_at(reservation: dict) -> Optional[datetime]:
    try:
        return parse_iso(str(reservation["end_at"]))
    except (KeyError, TypeError, ValueError):
        return None


def _owned_by(reservation: dict, uid: int) -> bool:
    try:
        return int(reservation.get("uid", -1)) == uid
    except (TypeError, ValueError):
        return False


def _recovery_log(actor: Actor, reservation: dict, state: str, message: str) -> dict:
    return {
        "ts": to_iso(datetime.now(timezone.utc)),
        "uid": actor.uid,
        "username": actor.username,
        "action": "job-recovery",
        "reservation_id": reservation.get("id"),
        "op_id": reservation.get("op_id"),
        "gpus": reservation.get("gpus", []),
        "mode": reservation.get("mode"),
        "start_at": reservation.get("start_at"),
        "end_at": reservation.get("end_at"),
        "result": JOB_UNCERTAIN,
        "message": f"{state}: {message}"[:1000],
    }
