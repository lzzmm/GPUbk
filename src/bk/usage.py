from __future__ import annotations

import math
import grp
import pwd
import shlex
import stat
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .gpu import GpuProcessSnapshot, GpuSnapshot
from .timeparse import parse_iso, utc_now


USAGE_AUTHORIZED = "ok"
USAGE_WRONG_GPU = "wrong-gpu"
USAGE_UNRESERVED = "unreserved"
USAGE_UNKNOWN = "unknown"
USAGE_SYSTEM = "system"

GPU_LIVE_IDLE = "idle"
GPU_LIVE_UNKNOWN = "unknown"
GPU_LIVE_BUSY = "busy"
GPU_BUSY_UTILIZATION_PERCENT = 10
GPU_BUSY_MEMORY_MIN_MB = 1024
CONTAINER_IDENTITY_HOST = "host"
CONTAINER_IDENTITY_INFERRED = "container-reservation"
CONTAINER_IDENTITY_AMBIGUOUS = "container-ambiguous"
_CONTAINER_ACCESS_CACHE: Dict[Tuple[str, int], Tuple[float, bool]] = {}
_CONTAINER_ACCESS_CACHE_SECONDS = 60.0

SYSTEM_GPU_PROCESS_NAMES = {
    "gnome-shell",
    "kwin_wayland",
    "kwin_x11",
    "nvidia-persistenced",
    "nvidia-powerd",
    "xorg",
    "xwayland",
}


@dataclass(frozen=True)
class ProcessUsage:
    gpu: int
    process: GpuProcessSnapshot
    status: str
    reservation_ids: Tuple[str, ...] = ()

    @property
    def violation(self) -> bool:
        return self.status in {USAGE_WRONG_GPU, USAGE_UNRESERVED}


@dataclass(frozen=True)
class GpuLiveState:
    index: int
    status: str
    reason: str = ""
    score: int = 0
    utilization_percent: Optional[int] = None
    memory_percent: float = 0.0
    process_count: int = 0


@dataclass(frozen=True)
class HistoricalGpuLoad:
    index: int
    predicted_percent: float = 0.0
    average_utilization_percent: float = 0.0
    busy_fraction: float = 0.0
    memory_percent: float = 0.0
    sample_count: int = 0


def classify_process_usage(
    snapshots: Sequence[GpuSnapshot],
    reservations: Sequence[dict],
    at: Optional[datetime] = None,
    container_uid_allowed: Optional[Callable[[str, int], bool]] = None,
) -> Dict[int, List[ProcessUsage]]:
    at = at or utc_now()
    current = [
        item
        for item in reservations
        if item.get("status") == "active"
        and parse_iso(item["start_at"]) <= at
        and at < parse_iso(item["end_at"])
    ]
    result: Dict[int, List[ProcessUsage]] = {}
    access_check = container_uid_allowed or _uid_can_use_container_runtime
    for gpu in snapshots:
        rows = [
            _classify_process(gpu.index, process, current, access_check)
            for process in gpu.processes
        ]
        result[gpu.index] = sorted(rows, key=lambda item: (not item.violation, item.process.username, item.process.pid))
    return result


def assess_gpu_live_states(snapshots: Sequence[GpuSnapshot], gpu_count: int) -> Dict[int, GpuLiveState]:
    """Classify current hardware activity for soft, idle-first GPU selection."""
    by_index = {item.index: item for item in snapshots}
    result: Dict[int, GpuLiveState] = {}
    for index in range(gpu_count):
        gpu = by_index.get(index)
        if gpu is None or gpu.source == "none":
            result[index] = GpuLiveState(index, GPU_LIVE_UNKNOWN, "live state unavailable", 0)
            continue

        active_processes = [process for process in gpu.processes if not is_system_gpu_process(process)]
        if active_processes:
            process = sorted(
                active_processes,
                key=lambda item: (-(item.sm_utilization_percent or 0), -item.gpu_memory_mb, item.pid),
            )[0]
            owner = process.username or (str(process.uid) if process.uid is not None else "unknown")
            reason = f"user={owner} pid={process.pid}"
            score = 100_000 + len(active_processes) * 10_000 + (gpu.utilization_percent or 0) * 100 + gpu.memory_used_mb
            memory_percent = _memory_percent(gpu)
            result[index] = GpuLiveState(
                index,
                GPU_LIVE_BUSY,
                reason,
                score,
                gpu.utilization_percent,
                memory_percent,
                len(active_processes),
            )
            continue

        utilization = gpu.utilization_percent or 0
        memory_limit = max(
            GPU_BUSY_MEMORY_MIN_MB,
            int(gpu.memory_total_mb * 0.10) if gpu.memory_total_mb else GPU_BUSY_MEMORY_MIN_MB,
        )
        if utilization >= GPU_BUSY_UTILIZATION_PERCENT:
            reason = f"util={utilization}%"
            score = 50_000 + utilization * 100 + gpu.memory_used_mb
            result[index] = GpuLiveState(
                index,
                GPU_LIVE_BUSY,
                reason,
                score,
                gpu.utilization_percent,
                _memory_percent(gpu),
                0,
            )
            continue
        if gpu.memory_used_mb >= memory_limit:
            reason = f"memory={gpu.memory_used_mb}MiB"
            score = 40_000 + gpu.memory_used_mb
            result[index] = GpuLiveState(
                index,
                GPU_LIVE_BUSY,
                reason,
                score,
                gpu.utilization_percent,
                _memory_percent(gpu),
                0,
            )
            continue

        score = utilization * 100 + gpu.memory_used_mb
        result[index] = GpuLiveState(
            index,
            GPU_LIVE_IDLE,
            "",
            score,
            gpu.utilization_percent,
            _memory_percent(gpu),
            0,
        )
    return result


def historical_gpu_loads(
    history: dict,
    gpu_count: int,
    at: Optional[datetime] = None,
    window_minutes: int = 30,
    half_life_minutes: int = 10,
) -> Dict[int, HistoricalGpuLoad]:
    at = at or utc_now()
    raw_gpus = history.get("gpus", {}) if isinstance(history, dict) else {}
    result: Dict[int, HistoricalGpuLoad] = {}
    for index in range(gpu_count):
        records = raw_gpus.get(str(index), []) if isinstance(raw_gpus, dict) else []
        weighted_util = 0.0
        weighted_busy = 0.0
        weighted_memory = 0.0
        total_weight = 0.0
        sample_count = 0
        for record in records if isinstance(records, list) else []:
            try:
                window_end = parse_iso(str(record["window_end"]))
                age_minutes = max(0.0, (at - window_end).total_seconds() / 60.0)
                if age_minutes > window_minutes:
                    continue
                samples = max(1, int(record.get("known_samples", record.get("sample_count", 1))))
                weight = math.exp(-math.log(2) * age_minutes / max(1, half_life_minutes)) * samples
                util = float(record.get("avg_utilization_percent") or 0.0)
                busy = float(record.get("busy_fraction") or 0.0)
                memory = float(record.get("avg_memory_percent") or 0.0)
            except (KeyError, TypeError, ValueError):
                continue
            weighted_util += util * weight
            weighted_busy += busy * weight
            weighted_memory += memory * weight
            total_weight += weight
            sample_count += samples

        if total_weight <= 0:
            result[index] = HistoricalGpuLoad(index=index)
            continue
        average_util = weighted_util / total_weight
        busy_fraction = weighted_busy / total_weight
        memory_percent = weighted_memory / total_weight
        predicted = min(100.0, average_util * 0.65 + busy_fraction * 25.0 + memory_percent * 0.10)
        result[index] = HistoricalGpuLoad(
            index=index,
            predicted_percent=predicted,
            average_utilization_percent=average_util,
            busy_fraction=busy_fraction,
            memory_percent=memory_percent,
            sample_count=sample_count,
        )
    return result


def combined_gpu_scores(
    states: Dict[int, GpuLiveState],
    historical: Dict[int, HistoricalGpuLoad],
) -> Dict[int, float]:
    scores: Dict[int, float] = {}
    for index, state in states.items():
        utilization = float(state.utilization_percent or 0)
        if state.status == GPU_LIVE_BUSY:
            live_score = min(100.0, 70.0 + utilization * 0.20 + state.memory_percent * 0.10 + state.process_count * 5.0)
        elif state.status == GPU_LIVE_UNKNOWN:
            live_score = 45.0
        else:
            live_score = min(20.0, utilization * 0.50 + state.memory_percent * 0.10)

        recent = historical.get(index, HistoricalGpuLoad(index=index))
        if recent.sample_count:
            scores[index] = round(live_score * 0.65 + recent.predicted_percent * 0.35, 3)
        else:
            scores[index] = round(live_score, 3)
    return scores


def gpu_selection_order(states: Dict[int, GpuLiveState], scores: Optional[Dict[int, float]] = None) -> List[int]:
    status_order = {GPU_LIVE_IDLE: 0, GPU_LIVE_UNKNOWN: 1, GPU_LIVE_BUSY: 2}
    return [
        item.index
        for item in sorted(
            states.values(),
            key=lambda item: (
                scores.get(item.index, 0.0) if scores is not None else status_order.get(item.status, 1),
                status_order.get(item.status, 1),
                item.score,
                item.index,
            ),
        )
    ]


def _memory_percent(gpu: GpuSnapshot) -> float:
    if not gpu.memory_total_mb:
        return 0.0
    return min(100.0, max(0.0, gpu.memory_used_mb * 100.0 / gpu.memory_total_mb))


def _classify_process(
    gpu: int,
    process: GpuProcessSnapshot,
    current: Sequence[dict],
    container_uid_allowed: Callable[[str, int], bool],
) -> ProcessUsage:
    if _is_system_process(process):
        return ProcessUsage(gpu, process, USAGE_SYSTEM)
    if process.uid is None:
        return ProcessUsage(gpu, process, USAGE_UNKNOWN)
    user_reservations = [item for item in current if int(item.get("uid", -1)) == process.uid]
    matches = [item for item in user_reservations if gpu in item.get("gpus", [])]
    if matches:
        ids = tuple(str(item.get("id", "")) for item in matches)
        return ProcessUsage(gpu, process, USAGE_AUTHORIZED, ids)
    if process.uid == 0 and process.container_runtime and process.container_id:
        inferred = _infer_container_owner(
            gpu,
            process,
            current,
            container_uid_allowed,
        )
        if inferred is not None:
            return inferred
    if user_reservations:
        ids = tuple(str(item.get("id", "")) for item in user_reservations)
        return ProcessUsage(gpu, process, USAGE_WRONG_GPU, ids)
    return ProcessUsage(gpu, process, USAGE_UNRESERVED)


def _infer_container_owner(
    gpu: int,
    process: GpuProcessSnapshot,
    current: Sequence[dict],
    container_uid_allowed: Callable[[str, int], bool],
) -> Optional[ProcessUsage]:
    candidates: Dict[int, List[dict]] = {}
    for reservation in current:
        if gpu not in reservation.get("gpus", []):
            continue
        try:
            uid = int(reservation.get("uid", -1))
        except (TypeError, ValueError):
            continue
        if uid <= 0 or not container_uid_allowed(process.container_runtime, uid):
            continue
        candidates.setdefault(uid, []).append(reservation)
    if len(candidates) > 1:
        ambiguous = replace(
            process,
            host_uid=process.host_uid if process.host_uid is not None else process.uid,
            identity_source=CONTAINER_IDENTITY_AMBIGUOUS,
        )
        return ProcessUsage(gpu, ambiguous, USAGE_UNKNOWN)
    if len(candidates) != 1:
        return None
    uid, reservations = next(iter(candidates.items()))
    username = str(reservations[0].get("username", uid))
    attributed = replace(
        process,
        uid=uid,
        username=username,
        host_uid=process.host_uid if process.host_uid is not None else process.uid,
        identity_source=CONTAINER_IDENTITY_INFERRED,
    )
    ids = tuple(str(item.get("id", "")) for item in reservations)
    return ProcessUsage(gpu, attributed, USAGE_AUTHORIZED, ids)


def _uid_can_use_container_runtime(runtime: str, uid: int) -> bool:
    if runtime != "docker" or uid <= 0:
        return False
    key = (runtime, uid)
    now = time.monotonic()
    cached = _CONTAINER_ACCESS_CACHE.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    allowed = _uid_can_access_docker_socket(uid)
    _CONTAINER_ACCESS_CACHE[key] = (now + _CONTAINER_ACCESS_CACHE_SECONDS, allowed)
    if len(_CONTAINER_ACCESS_CACHE) > 4096:
        expired = [item for item, value in _CONTAINER_ACCESS_CACHE.items() if value[0] <= now]
        for item in expired:
            _CONTAINER_ACCESS_CACHE.pop(item, None)
    return allowed


def _uid_can_access_docker_socket(
    uid: int,
    socket_path: Path = Path("/var/run/docker.sock"),
) -> bool:
    try:
        socket_stat = socket_path.stat()
    except OSError:
        try:
            docker_gid = grp.getgrnam("docker").gr_gid
        except (KeyError, OSError):
            return False
        mode = stat.S_IWGRP
    else:
        if socket_stat.st_mode & stat.S_IWOTH:
            return True
        docker_gid = socket_stat.st_gid
        mode = socket_stat.st_mode
    if not mode & stat.S_IWGRP:
        return False
    try:
        account = pwd.getpwuid(uid)
    except (KeyError, OSError):
        return False
    if account.pw_gid == docker_gid:
        return True
    try:
        group = grp.getgrgid(docker_gid)
    except (KeyError, OSError):
        return False
    return account.pw_name in group.gr_mem


def process_owner_label(process: GpuProcessSnapshot) -> str:
    if process.identity_source == CONTAINER_IDENTITY_INFERRED:
        return f"{process.username}*"
    if process.identity_source == CONTAINER_IDENTITY_AMBIGUOUS:
        return "container?"
    return process.username


def process_container_label(process: GpuProcessSnapshot) -> str:
    if not process.container_runtime or not process.container_id:
        return ""
    return f"{process.container_runtime}:{process.container_id[:12]}"


def _is_system_process(process: GpuProcessSnapshot) -> bool:
    executable = process.command.strip().split(maxsplit=1)[0] if process.command.strip() else ""
    name = executable.rsplit("/", 1)[-1].lower()
    return name in SYSTEM_GPU_PROCESS_NAMES


def is_system_gpu_process(process: GpuProcessSnapshot) -> bool:
    return _is_system_process(process)


def summarize_process_command(command: str) -> str:
    """Return an identifying command label without exposing arbitrary arguments."""
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.strip().split()
    if not argv:
        return "?"
    executable = Path(argv[0]).name or argv[0]
    if executable.lower().startswith("python") and len(argv) > 1:
        if argv[1] == "-m" and len(argv) > 2:
            return f"{executable} -m {argv[2]}"
        if argv[1] == "-c":
            return f"{executable} -c"
        if not argv[1].startswith("-"):
            return f"{executable} {Path(argv[1]).name}"
    return executable
