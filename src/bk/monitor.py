from __future__ import annotations

import hashlib
import json
import os
import signal
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import Config
from .gpu import GpuSnapshot, snapshot
from .scheduler import list_active
from .storage import FileLock, LedgerStore
from .timeparse import parse_iso, to_iso, utc_now
from .usage import GPU_LIVE_BUSY, ProcessUsage, assess_gpu_live_states, classify_process_usage


SnapshotProvider = Callable[[Config], List[GpuSnapshot]]


@dataclass(frozen=True)
class MonitorSample:
    sampled_at: datetime
    device_count: int
    process_count: int
    violation_count: int
    events: Tuple[dict, ...]
    rollups_flushed: int


class UsageAuditStore:
    def __init__(self, data_dir: Path, lock_timeout_seconds: float = 10.0):
        self.data_dir = data_dir
        self.lock_timeout_seconds = lock_timeout_seconds
        self.lock_path = data_dir / "usage.lock"
        self.state_path = data_dir / "usage-state.json"
        self.events_path = data_dir / "usage-events.jsonl"
        self.rollups_path = data_dir / "usage-rollups.jsonl"
        self.load_path = data_dir / "usage-load.json"

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def lock(self) -> FileLock:
        self.ensure()
        return FileLock(self.lock_path, self.lock_timeout_seconds)

    def load_state(self) -> Dict[str, dict]:
        self.ensure()
        if not self.state_path.exists():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("processes"), dict):
                return {}
            return payload["processes"]
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def save_state(self, processes: Dict[str, dict]) -> None:
        self.ensure()
        payload = json.dumps(
            {"version": 1, "processes": processes},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".usage-state.", suffix=".tmp", dir=str(self.data_dir))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.state_path)
            _fsync_dir(self.data_dir)
        finally:
            tmp_path.unlink(missing_ok=True)

    def append_events(self, events: Iterable[dict]) -> int:
        return self._append_jsonl(self.events_path, events)

    def append_rollups(self, rollups: Iterable[dict]) -> int:
        return self._append_jsonl(self.rollups_path, rollups)

    def recent_events(self, limit: int = 20) -> List[dict]:
        return self._recent_jsonl(self.events_path, limit)

    def recent_rollups(self, limit: int = 20) -> List[dict]:
        return self._recent_jsonl(self.rollups_path, limit)

    def load_load_history(self) -> dict:
        self.ensure()
        if not self.load_path.exists():
            return {"version": 1, "updated_at": None, "gpus": {}}
        try:
            payload = json.loads(self.load_path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("gpus"), dict):
                raise ValueError("invalid load history")
            return payload
        except (OSError, ValueError, json.JSONDecodeError):
            return {"version": 1, "updated_at": None, "gpus": {}}

    def save_load_history(self, history: dict) -> None:
        self.ensure()
        payload = json.dumps(history, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".usage-load.", suffix=".tmp", dir=str(self.data_dir))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.load_path)
            _fsync_dir(self.data_dir)
        finally:
            tmp_path.unlink(missing_ok=True)

    def clear_unlocked(self) -> dict:
        result = {
            "usage_events": _line_count(self.events_path),
            "usage_rollups": _line_count(self.rollups_path),
            "usage_state": 1 if self.state_path.exists() else 0,
            "usage_load": 1 if self.load_path.exists() else 0,
        }
        self.events_path.unlink(missing_ok=True)
        self.rollups_path.unlink(missing_ok=True)
        self.state_path.unlink(missing_ok=True)
        self.load_path.unlink(missing_ok=True)
        return result

    def _append_jsonl(self, path: Path, records: Iterable[dict]) -> int:
        items = list(records)
        if not items:
            return 0
        self.ensure()
        with path.open("a", encoding="utf-8") as fh:
            for item in items:
                fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return len(items)

    @staticmethod
    def _recent_jsonl(path: Path, limit: int) -> List[dict]:
        if limit < 1 or not path.exists():
            return []
        items: deque[dict] = deque(maxlen=limit)
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    items.append(value)
        return list(items)


class UsageMonitor:
    def __init__(
        self,
        config: Config,
        ledger_store: LedgerStore,
        audit_store: UsageAuditStore,
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

    def collect(self, sampled_at: Optional[datetime] = None) -> MonitorSample:
        sampled_at = sampled_at or utc_now()
        devices = self.snapshot_provider(self.config)
        reservations = list_active(self.ledger_store.load(), sampled_at)
        usage_by_gpu = classify_process_usage(devices, reservations, sampled_at)
        current_state = _build_process_state(usage_by_gpu, self.previous_state, sampled_at)
        events = _state_events(self.previous_state, current_state, sampled_at)
        if events:
            self.audit_store.append_events(events)
        if current_state != self.previous_state:
            self.audit_store.save_state(current_state)
        self.previous_state = current_state

        groups = _usage_groups(devices, usage_by_gpu, reservations, sampled_at)
        self._record_groups(groups, sampled_at)
        self._record_device_load(devices, sampled_at)
        flushed = self.flush_rollups(sampled_at)
        process_count = sum(len(rows) for rows in usage_by_gpu.values())
        violation_count = sum(1 for rows in usage_by_gpu.values() for item in rows if item.violation)
        return MonitorSample(
            sampled_at=sampled_at,
            device_count=len(devices),
            process_count=process_count,
            violation_count=violation_count,
            events=tuple(events),
            rollups_flushed=flushed,
        )

    def close(self, closed_at: Optional[datetime] = None) -> int:
        return self.flush_rollups(closed_at or utc_now(), force=True)

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
            self.load_history = _merge_device_load_history(self.load_history, ready_loads, at)
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
                    "_sm_total": 0.0,
                    "_sm_samples": 0,
                    "max_sm_percent": None,
                    "_memory_total": 0.0,
                    "max_gpu_memory_mb": 0,
                    "_device_util_total": 0.0,
                    "_device_util_samples": 0,
                    "max_device_util_percent": None,
                },
            )
            aggregate["sample_count"] += 1
            aggregate["_process_total"] += group["process_count"]
            aggregate["max_process_count"] = max(aggregate["max_process_count"], group["process_count"])
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
    audit_store = UsageAuditStore(config.data_dir, config.lock_timeout_seconds)
    stop_event = threading.Event()
    previous_handlers = _install_signal_handlers(stop_event)
    samples = 0
    try:
        with audit_store.lock():
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
                    if once or verbose or result.events:
                        _print_monitor_sample(result)
                    if once or (max_samples is not None and samples >= max_samples):
                        break
                    delay = max(0.0, interval_seconds - (time.monotonic() - started))
                    stop_event.wait(delay)
            finally:
                flushed = monitor.close()
                print(f"monitor stopped: samples={samples} partial_rollups={flushed}")
    finally:
        _restore_signal_handlers(previous_handlers)
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


def _build_process_state(
    usage_by_gpu: Dict[int, List[ProcessUsage]],
    previous: Dict[str, dict],
    sampled_at: datetime,
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
                "command": process.command,
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

    for device in devices:
        if not any(key[0] == device.index for key in groups):
            groups[(device.index, None, "idle")] = _empty_group(device.index, None, "-", "idle")

    result = []
    for group in groups.values():
        processes = group.pop("processes")
        sm_values = [item.sm_utilization_percent for item in processes if item.sm_utilization_percent is not None]
        sm_percent = 0 if not processes else (sum(sm_values) if sm_values else None)
        device = device_by_gpu.get(group["gpu"])
        group["reservation_ids"] = sorted(group["reservation_ids"])
        group["process_count"] = len(processes)
        group["sm_percent"] = sm_percent
        group["gpu_memory_mb"] = sum(item.gpu_memory_mb for item in processes)
        group["device_util_percent"] = device.utilization_percent if device is not None else None
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
        "avg_process_count": round(aggregate["_process_total"] / samples, 3),
        "max_process_count": aggregate["max_process_count"],
        "avg_sm_percent": round(aggregate["_sm_total"] / sm_samples, 3) if sm_samples else None,
        "max_sm_percent": aggregate["max_sm_percent"],
        "avg_gpu_memory_mb": round(aggregate["_memory_total"] / samples, 3),
        "max_gpu_memory_mb": aggregate["max_gpu_memory_mb"],
        "avg_device_util_percent": (
            round(aggregate["_device_util_total"] / device_samples, 3) if device_samples else None
        ),
        "max_device_util_percent": aggregate["max_device_util_percent"],
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


def _merge_device_load_history(history: dict, records: Sequence[dict], at: datetime, keep_per_gpu: int = 120) -> dict:
    raw_gpus = history.get("gpus", {}) if isinstance(history, dict) else {}
    gpus = {str(key): list(value) for key, value in raw_gpus.items() if isinstance(value, list)}
    for record in records:
        key = str(record["gpu"])
        existing = [item for item in gpus.get(key, []) if item.get("window_start") != record.get("window_start")]
        existing.append(record)
        existing.sort(key=lambda item: str(item.get("window_start", "")))
        gpus[key] = existing[-keep_per_gpu:]
    return {
        "version": 1,
        "updated_at": to_iso(at),
        "gpus": gpus,
    }


def _bucket_start(value: datetime, bucket_seconds: int) -> datetime:
    value = value.astimezone(timezone.utc)
    timestamp = int(value.timestamp())
    return datetime.fromtimestamp(timestamp - (timestamp % bucket_seconds), timezone.utc)


def _short_hash(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for _line in fh)
