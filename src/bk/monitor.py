from __future__ import annotations

import hashlib
import json
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .gpu import GpuSnapshot, snapshot
from .models import BookingError
from .scheduler import list_active
from .storage import LedgerStore
from .timeparse import parse_iso, to_iso, utc_now
from .usage_store import TelemetrySink, UsageAuditStore, UsageRetentionPolicy
from .usage import (
    GPU_LIVE_BUSY,
    USAGE_SYSTEM,
    ProcessUsage,
    assess_gpu_live_states,
    classify_process_usage,
    summarize_process_command,
)
from .workload import describe_workload


SnapshotProvider = Callable[[Config], List[GpuSnapshot]]
MONITOR_BUSY_EXIT_CODE = 75


class MonitorBusyError(BookingError):
    pass


@dataclass(frozen=True)
class MonitorSample:
    sampled_at: datetime
    device_count: int
    process_count: int
    violation_count: int
    events: Tuple[dict, ...]
    rollups_flushed: int
    warnings: Tuple[str, ...] = ()


class UsageMonitor:
    def __init__(
        self,
        config: Config,
        ledger_store: LedgerStore,
        audit_store: TelemetrySink,
        interval_seconds: float = 2.0,
        rollup_seconds: int = 60,
        snapshot_provider: SnapshotProvider = snapshot,
    ):
        if interval_seconds < 0.2:
            raise ValueError("monitor interval must be >= 0.2 seconds")
        if rollup_seconds < 1:
            raise ValueError("rollup interval must be >= 1 second")
        self.config = config
        self.ledger_store = ledger_store
        self.audit_store = audit_store
        self.interval_seconds = interval_seconds
        self.rollup_seconds = rollup_seconds
        self.snapshot_provider = snapshot_provider
        self.previous_state = audit_store.load_state()
        self.load_history = audit_store.load_load_history()
        self._rollups: Dict[Tuple[object, ...], dict] = {}
        self._device_rollups: Dict[Tuple[object, ...], dict] = {}
        self._workload_cache: Dict[Tuple[Optional[int], str, Optional[str]], int] = {}
        self._next_maintenance_check: Optional[datetime] = None
        self._retention_policy = UsageRetentionPolicy.from_config(config)
        self._reported_sink_warnings = 0

    def collect(self, sampled_at: Optional[datetime] = None) -> MonitorSample:
        sampled_at = sampled_at or utc_now()
        warnings = self._maintain_storage(sampled_at)
        devices = self.snapshot_provider(self.config)
        reservations = list_active(self.ledger_store.load(), sampled_at)
        usage_by_gpu = classify_process_usage(devices, reservations, sampled_at)
        workload_ids = _register_sample_workloads(
            self.audit_store,
            usage_by_gpu,
            reservations,
            self._workload_cache,
        )
        current_state = _build_process_state(usage_by_gpu, self.previous_state, sampled_at, workload_ids)
        events = _state_events(self.previous_state, current_state, sampled_at)
        if current_state != self.previous_state:
            self.audit_store.commit_state_transition(events, current_state)
        self.previous_state = current_state

        groups = _usage_groups(devices, usage_by_gpu, reservations, sampled_at, workload_ids)
        self._record_groups(groups, sampled_at)
        self._record_device_load(devices, sampled_at)
        flushed = self.flush_rollups(sampled_at)
        warnings.extend(self.take_warnings())
        process_count = sum(len(rows) for rows in usage_by_gpu.values())
        violation_count = sum(1 for rows in usage_by_gpu.values() for item in rows if item.violation)
        return MonitorSample(
            sampled_at=sampled_at,
            device_count=len(devices),
            process_count=process_count,
            violation_count=violation_count,
            events=tuple(events),
            rollups_flushed=flushed,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _maintain_storage(self, sampled_at: datetime) -> List[str]:
        if self._next_maintenance_check is not None and sampled_at < self._next_maintenance_check:
            return []
        self._next_maintenance_check = sampled_at + timedelta(hours=24)
        try:
            report = self.audit_store.maintain(self._retention_policy, now=sampled_at)
        except OSError as exc:
            return [f"usage maintenance deferred: {exc}"]
        return [str(item) for item in report.get("blocked", [])]

    def close(self, closed_at: Optional[datetime] = None) -> int:
        return self.flush_rollups(closed_at or utc_now(), force=True)

    def take_warnings(self) -> Tuple[str, ...]:
        raw = getattr(self.audit_store, "last_warnings", ())
        if not isinstance(raw, (list, tuple)):
            return ()
        start = min(self._reported_sink_warnings, len(raw))
        pending = tuple(str(item) for item in raw[start:])
        if isinstance(raw, list):
            raw.clear()
            self._reported_sink_warnings = 0
        else:
            self._reported_sink_warnings = len(raw)
        return pending

    def flush_rollups(self, at: datetime, force: bool = False) -> int:
        ready = []
        keys = []
        for key, aggregate in self._rollups.items():
            bucket_end = parse_iso(aggregate["window_end"])
            if force or bucket_end <= at:
                ready.append(_finalize_rollup(aggregate, at, partial=force and at < bucket_end))
                keys.append(key)
        written = self.audit_store.append_rollups(ready)
        for key in keys:
            self._rollups.pop(key, None)
        ready_loads = []
        load_keys = []
        for key, aggregate in self._device_rollups.items():
            bucket_end = parse_iso(aggregate["window_end"])
            if force or bucket_end <= at:
                ready_loads.append(_finalize_device_load(aggregate, at, partial=force and at < bucket_end))
                load_keys.append(key)
        if ready_loads:
            self.load_history = _merge_device_load_history(
                self.load_history,
                ready_loads,
                at,
                keep_minutes=self.config.usage_load_window_minutes,
            )
            self.audit_store.save_load_history(self.load_history)
        for key in load_keys:
            self._device_rollups.pop(key, None)
        return written

    def _record_groups(self, groups: Sequence[dict], sampled_at: datetime) -> None:
        bucket_start = _bucket_start(sampled_at, self.rollup_seconds)
        bucket_end = bucket_start + timedelta(seconds=self.rollup_seconds)
        for group in groups:
            reservation_ids = tuple(group["reservation_ids"])
            key = (
                to_iso(bucket_start),
                group["gpu"],
                group["uid"],
                group["status"],
                reservation_ids,
            )
            aggregate = self._rollups.setdefault(
                key,
                {
                    "window_start": to_iso(bucket_start),
                    "window_end": to_iso(bucket_end),
                    "gpu": group["gpu"],
                    "uid": group["uid"],
                    "username": group["username"],
                    "status": group["status"],
                    "reservation_ids": list(reservation_ids),
                    "sample_count": 0,
                    "_interval_seconds": self.interval_seconds,
                    "_process_total": 0,
                    "max_process_count": 0,
                    "_active_samples": 0,
                    "_sm_total": 0.0,
                    "_sm_samples": 0,
                    "max_sm_percent": None,
                    "_memory_total": 0.0,
                    "max_gpu_memory_mb": 0,
                    "_device_util_total": 0.0,
                    "_device_util_samples": 0,
                    "max_device_util_percent": None,
                    "_workloads": set(),
                    "_workload_samples": {},
                },
            )
            aggregate["sample_count"] += 1
            aggregate["_process_total"] += group["process_count"]
            aggregate["max_process_count"] = max(aggregate["max_process_count"], group["process_count"])
            if group["process_count"]:
                aggregate["_active_samples"] += 1
            if group["sm_percent"] is not None:
                aggregate["_sm_total"] += group["sm_percent"]
                aggregate["_sm_samples"] += 1
                previous_sm = aggregate["max_sm_percent"]
                aggregate["max_sm_percent"] = group["sm_percent"] if previous_sm is None else max(previous_sm, group["sm_percent"])
            aggregate["_memory_total"] += group["gpu_memory_mb"]
            aggregate["max_gpu_memory_mb"] = max(aggregate["max_gpu_memory_mb"], group["gpu_memory_mb"])
            if group["device_util_percent"] is not None:
                aggregate["_device_util_total"] += group["device_util_percent"]
                aggregate["_device_util_samples"] += 1
                previous_util = aggregate["max_device_util_percent"]
                aggregate["max_device_util_percent"] = (
                    group["device_util_percent"] if previous_util is None else max(previous_util, group["device_util_percent"])
                )
            aggregate["_workloads"].update(group["workload_ids"])
            for workload_id in group["workload_ids"]:
                aggregate["_workload_samples"][workload_id] = (
                    aggregate["_workload_samples"].get(workload_id, 0) + 1
                )

    def _record_device_load(self, devices: Sequence[GpuSnapshot], sampled_at: datetime) -> None:
        bucket_start = _bucket_start(sampled_at, self.rollup_seconds)
        bucket_end = bucket_start + timedelta(seconds=self.rollup_seconds)
        by_index = {device.index: device for device in devices}
        live_states = assess_gpu_live_states(devices, self.config.gpu_count)
        for gpu in range(self.config.gpu_count):
            device = by_index.get(gpu)
            state = live_states[gpu]
            key = (to_iso(bucket_start), gpu)
            aggregate = self._device_rollups.setdefault(
                key,
                {
                    "window_start": to_iso(bucket_start),
                    "window_end": to_iso(bucket_end),
                    "gpu": gpu,
                    "sample_count": 0,
                    "known_samples": 0,
                    "_util_total": 0.0,
                    "_memory_total": 0.0,
                    "_busy_total": 0,
                },
            )
            aggregate["sample_count"] += 1
            if device is None or device.source == "none":
                continue
            aggregate["known_samples"] += 1
            aggregate["_util_total"] += float(device.utilization_percent or 0)
            memory_percent = 0.0
            if device.memory_total_mb:
                memory_percent = min(100.0, device.memory_used_mb * 100.0 / device.memory_total_mb)
            aggregate["_memory_total"] += memory_percent
            aggregate["_busy_total"] += int(state.status == GPU_LIVE_BUSY)


def run_monitor(
    config: Config,
    ledger_store: LedgerStore,
    interval_seconds: float = 2.0,
    rollup_seconds: int = 60,
    once: bool = False,
    max_samples: Optional[int] = None,
    verbose: bool = False,
) -> int:
    if max_samples is not None and max_samples < 1:
        raise ValueError("--samples must be >= 1")
    audit_store = UsageAuditStore(
        config.data_dir,
        config.lock_timeout_seconds,
        config.file_mode,
        config.dir_mode,
    )
    lease = audit_store.lock(timeout_seconds=min(2.0, config.lock_timeout_seconds))
    try:
        lease.__enter__()
    except TimeoutError as exc:
        raise MonitorBusyError(
            f"another monitor or telemetry maintenance writer is active for {config.data_dir}"
        ) from exc
    samples = 0
    try:
        stop_event = threading.Event()
        previous_handlers = _install_signal_handlers(stop_event)
        try:
            monitor = UsageMonitor(config, ledger_store, audit_store, interval_seconds, rollup_seconds)
            print(
                f"monitor started: interval={interval_seconds:g}s rollup={rollup_seconds}s "
                f"data={config.data_dir}"
            )
            try:
                while not stop_event.is_set():
                    started = time.monotonic()
                    result = monitor.collect()
                    samples += 1
                    if once or verbose or result.events or result.warnings:
                        _print_monitor_sample(result)
                    if once or (max_samples is not None and samples >= max_samples):
                        break
                    delay = max(0.0, interval_seconds - (time.monotonic() - started))
                    stop_event.wait(delay)
            finally:
                flushed = monitor.close()
                for warning in monitor.take_warnings():
                    print(f"monitor warning: {warning}")
                print(f"monitor stopped: samples={samples} partial_rollups={flushed}")
        finally:
            _restore_signal_handlers(previous_handlers)
    finally:
        lease.__exit__(None, None, None)
    return 0


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


def _print_monitor_sample(sample: MonitorSample) -> None:
    print(
        f"sample {sample.sampled_at.astimezone():%Y-%m-%d %H:%M:%S %z}: "
        f"gpus={sample.device_count} processes={sample.process_count} "
        f"violations={sample.violation_count} events={len(sample.events)}"
    )
    for event in sample.events:
        print(
            f"  {event['event']} gpu={event['gpu']} pid={event['pid']} "
            f"user={event['username']} {event.get('old_status', '-')}->{event.get('status', '-')}"
        )
    for warning in sample.warnings:
        print(f"  warning: {warning}")


def _register_sample_workloads(
    sink: TelemetrySink,
    usage_by_gpu: Dict[int, List[ProcessUsage]],
    reservations: Sequence[dict],
    cache: Dict[Tuple[Optional[int], str, Optional[str]], int],
) -> Dict[Tuple[int, int, str], int]:
    result = {}
    for gpu, rows in usage_by_gpu.items():
        for item in rows:
            if item.status == USAGE_SYSTEM:
                continue
            process = item.process
            managed_summary = _managed_summary(process.pid, process.uid, reservations)
            cache_key = (process.uid, process.command, managed_summary)
            workload_id = cache.get(cache_key)
            if workload_id is None:
                descriptor = describe_workload(process.command, managed_summary)
                workload_id = sink.register_workload(process.uid, descriptor)
                cache[cache_key] = workload_id
                if len(cache) > 2048:
                    cache.pop(next(iter(cache)))
            result[_process_sample_key(gpu, process.pid, process.host_start_id)] = workload_id
    return result


def _managed_summary(pid: int, uid: Optional[int], reservations: Sequence[dict]) -> Optional[str]:
    matches = []
    for reservation in reservations:
        if uid is None or int(reservation.get("uid", -1)) != uid:
            continue
        job = reservation.get("job")
        if not isinstance(job, dict) or not job.get("summary"):
            continue
        runner_pid = _optional_positive_int(job.get("runner_pid"))
        if runner_pid is None:
            continue
        if runner_pid == pid or _pid_descends_from(pid, runner_pid):
            matches.append(str(job["summary"]))
    return matches[0] if len(matches) == 1 else None


def _pid_descends_from(pid: int, ancestor_pid: int, max_depth: int = 32) -> bool:
    current = pid
    seen = set()
    for _depth in range(max_depth):
        if current <= 1 or current in seen:
            return False
        if current == ancestor_pid:
            return True
        seen.add(current)
        try:
            raw_stat = (Path("/proc") / str(current) / "stat").read_text(encoding="utf-8")
            current = _proc_parent_pid(raw_stat)
        except (OSError, ValueError):
            return False
    return False


def _proc_parent_pid(raw_stat: str) -> int:
    command_end = raw_stat.rfind(")")
    if command_end < 0:
        raise ValueError("invalid /proc stat record")
    fields = raw_stat[command_end + 1 :].split()
    if len(fields) < 2:
        raise ValueError("invalid /proc stat record")
    return int(fields[1])


def _optional_positive_int(value) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _process_sample_key(gpu: int, pid: int, start_id: str) -> Tuple[int, int, str]:
    return gpu, pid, start_id


def _build_process_state(
    usage_by_gpu: Dict[int, List[ProcessUsage]],
    previous: Dict[str, dict],
    sampled_at: datetime,
    workload_ids: Dict[Tuple[int, int, str], int],
) -> Dict[str, dict]:
    current = {}
    for gpu, rows in usage_by_gpu.items():
        for item in rows:
            process = item.process
            identity = process.host_start_id or _short_hash(f"{process.uid}:{process.command}")
            key = f"g{gpu}:p{process.pid}:s{identity}"
            previous_item = previous.get(key, {})
            current[key] = {
                "key": key,
                "gpu": gpu,
                "pid": process.pid,
                "uid": process.uid,
                "username": process.username,
                "command": summarize_process_command(process.command),
                "workload_id": workload_ids.get(_process_sample_key(gpu, process.pid, process.host_start_id)),
                "kind": process.kind,
                "status": item.status,
                "reservation_ids": list(item.reservation_ids),
                "first_seen_at": previous_item.get("first_seen_at", to_iso(sampled_at)),
            }
    return current


def _state_events(previous: Dict[str, dict], current: Dict[str, dict], at: datetime) -> List[dict]:
    events = []
    for key in sorted(current.keys() - previous.keys()):
        events.append(_event("process-start", current[key], at))
    for key in sorted(previous.keys() & current.keys()):
        old = previous[key]
        new = current[key]
        if old.get("status") != new.get("status") or old.get("reservation_ids") != new.get("reservation_ids"):
            events.append(_event("authorization-change", new, at, old_status=old.get("status")))
        if old.get("workload_id") != new.get("workload_id"):
            events.append(_event("workload-change", new, at))
    for key in sorted(previous.keys() - current.keys()):
        events.append(_event("process-stop", previous[key], at, old_status=previous[key].get("status")))
    return events


def _event(event_type: str, item: dict, at: datetime, old_status: Optional[str] = None) -> dict:
    payload = {
        "event": event_type,
        "timestamp": to_iso(at),
        "key": item["key"],
        "gpu": item["gpu"],
        "pid": item["pid"],
        "uid": item.get("uid"),
        "username": item.get("username", "?"),
        "command": item.get("command", ""),
        "workload_id": item.get("workload_id"),
        "kind": item.get("kind", ""),
        "status": item.get("status"),
        "reservation_ids": item.get("reservation_ids", []),
    }
    if old_status is not None:
        payload["old_status"] = old_status
    payload["event_id"] = _short_hash(json.dumps(payload, sort_keys=True, ensure_ascii=True), length=20)
    return payload


def _usage_groups(
    devices: Sequence[GpuSnapshot],
    usage_by_gpu: Dict[int, List[ProcessUsage]],
    reservations: Sequence[dict],
    at: datetime,
    workload_ids: Dict[Tuple[int, int, str], int],
) -> List[dict]:
    current_reservations = [
        item
        for item in reservations
        if item.get("status") == "active" and parse_iso(item["start_at"]) <= at < parse_iso(item["end_at"])
    ]
    device_by_gpu = {device.index: device for device in devices}
    groups: Dict[Tuple[int, Optional[int], str], dict] = {}
    for reservation in current_reservations:
        uid = int(reservation.get("uid", -1))
        for gpu in reservation.get("gpus", []):
            key = (int(gpu), uid, "ok")
            group = groups.setdefault(
                key,
                _empty_group(int(gpu), uid, str(reservation.get("username", uid)), "ok"),
            )
            group["reservation_ids"].add(str(reservation.get("id", "")))

    for gpu, rows in usage_by_gpu.items():
        for item in rows:
            process = item.process
            key = (gpu, process.uid, item.status)
            group = groups.setdefault(key, _empty_group(gpu, process.uid, process.username, item.status))
            group["reservation_ids"].update(item.reservation_ids)
            group["processes"].append(process)
            workload_id = workload_ids.get(_process_sample_key(gpu, process.pid, process.host_start_id))
            if workload_id is not None:
                group["workload_ids"].add(workload_id)

    for device in devices:
        if not any(key[0] == device.index for key in groups):
            groups[(device.index, None, "idle")] = _empty_group(device.index, None, "-", "idle")

    result = []
    for group in groups.values():
        processes = group.pop("processes")
        workload_set = group.pop("workload_ids")
        sm_values = [item.sm_utilization_percent for item in processes if item.sm_utilization_percent is not None]
        sm_percent = 0 if not processes else (sum(sm_values) if sm_values else None)
        device = device_by_gpu.get(group["gpu"])
        group["reservation_ids"] = sorted(group["reservation_ids"])
        group["process_count"] = len(processes)
        group["sm_percent"] = sm_percent
        group["gpu_memory_mb"] = sum(item.gpu_memory_mb for item in processes)
        group["device_util_percent"] = device.utilization_percent if device is not None else None
        group["workload_ids"] = sorted(workload_set)
        result.append(group)
    return result


def _empty_group(gpu: int, uid: Optional[int], username: str, status: str) -> dict:
    return {
        "gpu": gpu,
        "uid": uid,
        "username": username,
        "status": status,
        "reservation_ids": set(),
        "processes": [],
        "workload_ids": set(),
    }


def _finalize_rollup(aggregate: dict, flushed_at: datetime, partial: bool) -> dict:
    samples = aggregate["sample_count"]
    sm_samples = aggregate["_sm_samples"]
    device_samples = aggregate["_device_util_samples"]
    return {
        "window_start": aggregate["window_start"],
        "window_end": aggregate["window_end"],
        "flushed_at": to_iso(flushed_at),
        "partial": partial,
        "gpu": aggregate["gpu"],
        "uid": aggregate["uid"],
        "username": aggregate["username"],
        "status": aggregate["status"],
        "reservation_ids": aggregate["reservation_ids"],
        "sample_count": samples,
        "observed_seconds": round(samples * aggregate.get("_interval_seconds", 0), 3),
        "active_sample_count": aggregate["_active_samples"],
        "active_observed_seconds": round(
            aggregate["_active_samples"] * aggregate.get("_interval_seconds", 0), 3
        ),
        "avg_process_count": round(aggregate["_process_total"] / samples, 3),
        "max_process_count": aggregate["max_process_count"],
        "sm_sample_count": sm_samples,
        "avg_sm_percent": round(aggregate["_sm_total"] / sm_samples, 3) if sm_samples else None,
        "max_sm_percent": aggregate["max_sm_percent"],
        "avg_gpu_memory_mb": round(aggregate["_memory_total"] / samples, 3),
        "max_gpu_memory_mb": aggregate["max_gpu_memory_mb"],
        "device_util_sample_count": device_samples,
        "avg_device_util_percent": (
            round(aggregate["_device_util_total"] / device_samples, 3) if device_samples else None
        ),
        "max_device_util_percent": aggregate["max_device_util_percent"],
        "workload_ids": sorted(aggregate["_workloads"]),
        "workload_observed_seconds": {
            str(workload_id): round(count * aggregate.get("_interval_seconds", 0), 3)
            for workload_id, count in sorted(aggregate["_workload_samples"].items())
        },
    }


def _finalize_device_load(aggregate: dict, flushed_at: datetime, partial: bool) -> dict:
    known = aggregate["known_samples"]
    return {
        "window_start": aggregate["window_start"],
        "window_end": aggregate["window_end"],
        "flushed_at": to_iso(flushed_at),
        "partial": partial,
        "gpu": aggregate["gpu"],
        "sample_count": aggregate["sample_count"],
        "known_samples": known,
        "avg_utilization_percent": round(aggregate["_util_total"] / known, 3) if known else None,
        "avg_memory_percent": round(aggregate["_memory_total"] / known, 3) if known else None,
        "busy_fraction": round(aggregate["_busy_total"] / known, 4) if known else None,
    }


def _merge_device_load_history(
    history: dict,
    records: Sequence[dict],
    at: datetime,
    keep_minutes: int = 120,
) -> dict:
    raw_gpus = history.get("gpus", {}) if isinstance(history, dict) else {}
    gpus = {str(key): list(value) for key, value in raw_gpus.items() if isinstance(value, list)}
    cutoff = at.astimezone(timezone.utc) - timedelta(minutes=max(1, keep_minutes))
    for record in records:
        key = str(record["gpu"])
        existing = [
            item
            for item in gpus.get(key, [])
            if item.get("window_start") != record.get("window_start")
            and _record_after_cutoff(item, cutoff)
        ]
        existing.append(record)
        existing.sort(key=lambda item: str(item.get("window_start", "")))
        gpus[key] = existing
    return {
        "version": 1,
        "updated_at": to_iso(at),
        "gpus": gpus,
    }


def _record_after_cutoff(record: dict, cutoff: datetime) -> bool:
    try:
        return parse_iso(str(record["window_end"])) >= cutoff
    except (KeyError, TypeError, ValueError):
        return False


def _bucket_start(value: datetime, bucket_seconds: int) -> datetime:
    value = value.astimezone(timezone.utc)
    timestamp = int(value.timestamp())
    return datetime.fromtimestamp(timestamp - (timestamp % bucket_seconds), timezone.utc)


def _short_hash(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]
