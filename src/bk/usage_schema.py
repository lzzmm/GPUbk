from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .timeparse import parse_iso, to_iso
from .workload import WorkloadDescriptor


STORE_FORMAT = "gpubk.usage"
STORE_FORMAT_MAJOR = 1
STORE_FORMAT_MINOR = 0
EVENT_SCHEMA_VERSION = 1
ROLLUP_SCHEMA_VERSION = 1
WORKLOAD_SCHEMA_VERSION = 1
USAGE_API_VERSION = "gpubk.usage.v1"

RESOLUTIONS = {
    "1m": 60,
    "5m": 5 * 60,
    "10m": 10 * 60,
    "1h": 60 * 60,
    "1d": 24 * 60 * 60,
}
TIER_FOR_RESOLUTION = {
    60: "minute",
    5 * 60: "five-minute",
    10 * 60: "ten-minute",
    60 * 60: "hourly",
    24 * 60 * 60: "daily",
}

STATUS_CODES = {
    "idle": 0,
    "ok": 1,
    "unreserved": 2,
    "wrong-gpu": 3,
    "unknown": 4,
    "system": 5,
}
EVENT_CODES = {
    "process-start": 1,
    "process-stop": 2,
    "authorization-change": 3,
    "workload-change": 4,
    "event-burst": 5,
}

LAUNCHER_CODES = {
    "unknown": 0,
    "python": 1,
    "torchrun": 2,
    "deepspeed": 3,
    "accelerate": 4,
    "mpi": 5,
    "slurm": 6,
    "ray": 7,
    "jupyter": 8,
    "shell": 9,
    "container": 10,
    "service": 11,
    "native": 12,
}
ENTRYPOINT_CODES = {
    "unknown": 0,
    "script": 1,
    "module": 2,
    "inline": 3,
    "runtime": 4,
    "notebook": 5,
    "shell": 6,
    "container": 7,
    "service": 8,
    "binary": 9,
}
WORKLOAD_KIND_CODES = {
    "unknown": 0,
    "training": 1,
    "evaluation": 2,
    "inference-batch": 3,
    "inference-service": 4,
    "data-processing": 5,
    "profiling": 6,
    "interactive": 7,
    "simulation": 8,
    "rendering": 9,
    "custom": 10,
}
FRAMEWORK_CODES = {
    "unknown": 0,
    "pytorch": 1,
    "tensorflow": 2,
    "jax": 3,
    "vllm": 4,
    "triton": 5,
    "tensorrt": 6,
    "ollama": 7,
    "cuda": 8,
    "custom": 9,
}
EXECUTION_CODES = {
    "unknown": 0,
    "single": 1,
    "distributed": 2,
    "interactive": 3,
    "container": 4,
    "service": 5,
}
SOURCE_CODES = {
    "unknown": 0,
    "declared": 1,
    "managed": 2,
    "inferred": 3,
    "coarse": 4,
    "ai": 5,
}

EVENT_STORAGE_FIELDS = frozenset({"v", "e", "t", "id", "k", "g", "p", "u", "n", "w", "pk", "s", "o", "r", "x"})
ROLLUP_STORAGE_FIELDS = frozenset(
    {
        "v",
        "id",
        "t",
        "d",
        "g",
        "u",
        "n",
        "s",
        "r",
        "q",
        "c",
        "o",
        "ac",
        "ao",
        "pa",
        "px",
        "sc",
        "sa",
        "sx",
        "ma",
        "mx",
        "dc",
        "da",
        "dx",
        "w",
        "wo",
        "x",
    }
)


def parse_resolution(value: str) -> int:
    normalized = value.strip().lower()
    aliases = {"minute": "1m", "hour": "1h", "daily": "1d", "day": "1d"}
    normalized = aliases.get(normalized, normalized)
    try:
        return RESOLUTIONS[normalized]
    except KeyError as exc:
        raise ValueError("resolution must be auto, 1m, 5m, 10m, 1h, or 1d") from exc


def resolution_label(seconds: int) -> str:
    for label, value in RESOLUTIONS.items():
        if value == seconds:
            return label
    return f"{seconds}s"


def encode_event(record: dict) -> dict:
    return _drop_none(
        {
            "v": EVENT_SCHEMA_VERSION,
            "e": _encode_enum(EVENT_CODES, str(record.get("event", "unknown"))),
            "t": _epoch(record.get("timestamp")),
            "id": str(record.get("event_id", "")),
            "k": str(record.get("key", "")),
            "g": _optional_int(record.get("gpu")),
            "p": _optional_int(record.get("pid")),
            "u": _optional_int(record.get("uid")),
            "n": str(record.get("username", ""))[:128] or None,
            "w": _optional_int(record.get("workload_id")),
            "pk": str(record.get("kind", "")),
            "s": _encode_enum(STATUS_CODES, record.get("status")),
            "o": _encode_enum(STATUS_CODES, record.get("old_status")),
            "r": sorted(str(value) for value in record.get("reservation_ids", []) if value),
            "x": record.get("extensions") or None,
        }
    )


def decode_event(record: dict, username_for_uid: Optional[Callable[[Optional[int]], str]] = None) -> dict:
    if "timestamp" in record:
        result = dict(record)
        result.setdefault("schema_version", "gpubk.usage.event.legacy-v1")
        return result
    uid = _optional_int(record.get("u"))
    result = {
        "schema_version": "gpubk.usage.event.v1",
        "event": _decode_enum(EVENT_CODES, record.get("e")),
        "timestamp": _iso_from_epoch(record.get("t")),
        "event_id": str(record.get("id", "")),
        "key": str(record.get("k", "")),
        "gpu": _optional_int(record.get("g")),
        "pid": _optional_int(record.get("p")),
        "uid": uid,
        "username": str(record.get("n")) if record.get("n") else (
            username_for_uid(uid) if username_for_uid else (str(uid) if uid is not None else "?")
        ),
        "workload_id": _optional_int(record.get("w")),
        "kind": str(record.get("pk", "")),
        "status": _decode_enum(STATUS_CODES, record.get("s")),
        "reservation_ids": [str(value) for value in record.get("r", [])],
    }
    if "o" in record:
        result["old_status"] = _decode_enum(STATUS_CODES, record.get("o"))
    if isinstance(record.get("x"), dict):
        result["extensions"] = record["x"]
    return result


def encode_rollup(record: dict) -> dict:
    start = parse_iso(str(record["window_start"]))
    end = parse_iso(str(record["window_end"]))
    workload_seconds = record.get("workload_observed_seconds", {})
    return _drop_none(
        {
            "v": ROLLUP_SCHEMA_VERSION,
            "id": str(record.get("record_id") or _rollup_id(record)),
            "t": int(start.timestamp()),
            "d": max(1, int((end - start).total_seconds())),
            "g": int(record["gpu"]),
            "u": _optional_int(record.get("uid")),
            "n": str(record.get("username", ""))[:128] or None,
            "s": _encode_enum(STATUS_CODES, record.get("status")),
            "r": sorted(str(value) for value in record.get("reservation_ids", []) if value),
            "q": bool(record.get("partial", False)),
            "c": max(0, int(record.get("sample_count", 0))),
            "o": max(0.0, float(record.get("observed_seconds", 0))),
            "ac": max(0, int(record.get("active_sample_count", 0))),
            "ao": max(0.0, float(record.get("active_observed_seconds", 0))),
            "pa": _optional_float(record.get("avg_process_count")),
            "px": _optional_int(record.get("max_process_count")),
            "sc": max(0, int(record.get("sm_sample_count", 0))),
            "sa": _optional_float(record.get("avg_sm_percent")),
            "sx": _optional_float(record.get("max_sm_percent")),
            "ma": _optional_float(record.get("avg_gpu_memory_mb")),
            "mx": _optional_int(record.get("max_gpu_memory_mb")),
            "dc": max(0, int(record.get("device_util_sample_count", 0))),
            "da": _optional_float(record.get("avg_device_util_percent")),
            "dx": _optional_float(record.get("max_device_util_percent")),
            "w": sorted({int(value) for value in record.get("workload_ids", []) if int(value) > 0}),
            "wo": {str(int(key)): float(value) for key, value in workload_seconds.items() if int(key) > 0},
            "x": record.get("extensions") or None,
        }
    )


def decode_rollup(record: dict, username_for_uid: Optional[Callable[[Optional[int]], str]] = None) -> dict:
    if "window_start" in record:
        result = dict(record)
        result.setdefault("schema_version", "gpubk.usage.rollup.legacy-v1")
        result.setdefault("workload_ids", [])
        result.setdefault("workload_observed_seconds", {})
        result.setdefault("active_sample_count", result.get("sample_count", 0) if result.get("avg_process_count", 0) else 0)
        result.setdefault("active_observed_seconds", result.get("observed_seconds", 0) if result.get("avg_process_count", 0) else 0)
        result.setdefault("sm_sample_count", result.get("sample_count", 0) if result.get("avg_sm_percent") is not None else 0)
        result.setdefault(
            "device_util_sample_count",
            result.get("sample_count", 0) if result.get("avg_device_util_percent") is not None else 0,
        )
        return result
    start = _datetime_from_epoch(record.get("t"))
    duration = max(1, int(record.get("d", 60)))
    uid = _optional_int(record.get("u"))
    result = {
        "schema_version": "gpubk.usage.rollup.v1",
        "record_id": str(record.get("id", "")),
        "window_start": to_iso(start),
        "window_end": to_iso(datetime.fromtimestamp(int(start.timestamp()) + duration, timezone.utc)),
        "resolution_seconds": duration,
        "partial": bool(record.get("q", False)),
        "gpu": int(record.get("g", -1)),
        "uid": uid,
        "username": str(record.get("n")) if record.get("n") else (
            username_for_uid(uid) if username_for_uid else (str(uid) if uid is not None else "?")
        ),
        "status": _decode_enum(STATUS_CODES, record.get("s")),
        "reservation_ids": [str(value) for value in record.get("r", [])],
        "sample_count": max(0, int(record.get("c", 0))),
        "observed_seconds": max(0.0, float(record.get("o", 0))),
        "active_sample_count": max(0, int(record.get("ac", 0))),
        "active_observed_seconds": max(0.0, float(record.get("ao", 0))),
        "avg_process_count": _optional_float(record.get("pa")),
        "max_process_count": _optional_int(record.get("px")),
        "sm_sample_count": max(0, int(record.get("sc", 0))),
        "avg_sm_percent": _optional_float(record.get("sa")),
        "max_sm_percent": _optional_float(record.get("sx")),
        "avg_gpu_memory_mb": _optional_float(record.get("ma")),
        "max_gpu_memory_mb": _optional_int(record.get("mx")),
        "device_util_sample_count": max(0, int(record.get("dc", 0))),
        "avg_device_util_percent": _optional_float(record.get("da")),
        "max_device_util_percent": _optional_float(record.get("dx")),
        "workload_ids": [int(value) for value in record.get("w", [])],
        "workload_observed_seconds": {
            str(int(key)): float(value) for key, value in record.get("wo", {}).items()
        },
    }
    if isinstance(record.get("x"), dict):
        result["extensions"] = record["x"]
    return result


def encode_workload(workload_id: int, fingerprint: str, descriptor: WorkloadDescriptor, created_at: datetime) -> dict:
    return {
        "v": WORKLOAD_SCHEMA_VERSION,
        "id": workload_id,
        "fp": fingerprint,
        "la": _encode_enum(LAUNCHER_CODES, descriptor.launcher),
        "ep": _encode_enum(ENTRYPOINT_CODES, descriptor.entrypoint_kind),
        "ki": _encode_enum(WORKLOAD_KIND_CODES, descriptor.kind),
        "fw": _encode_enum(FRAMEWORK_CODES, descriptor.framework),
        "ex": _encode_enum(EXECUTION_CODES, descriptor.execution),
        "src": _encode_enum(SOURCE_CODES, descriptor.source),
        "cf": max(0, min(100, int(descriptor.confidence))),
        "label": descriptor.label,
        "at": int(created_at.astimezone(timezone.utc).timestamp()),
    }


def decode_workload(record: dict) -> dict:
    return {
        "schema_version": "gpubk.usage.workload.v1",
        "id": int(record["id"]),
        "launcher": _decode_enum(LAUNCHER_CODES, record.get("la")),
        "entrypoint_kind": _decode_enum(ENTRYPOINT_CODES, record.get("ep")),
        "kind": _decode_enum(WORKLOAD_KIND_CODES, record.get("ki")),
        "framework": _decode_enum(FRAMEWORK_CODES, record.get("fw")),
        "execution": _decode_enum(EXECUTION_CODES, record.get("ex")),
        "source": _decode_enum(SOURCE_CODES, record.get("src")),
        "confidence": max(0, min(100, int(record.get("cf", 0)))),
        "label": str(record.get("label", "?")),
        "created_at": _iso_from_epoch(record.get("at")),
    }


def aggregate_rollups(records: Sequence[dict], resolution_seconds: int) -> List[dict]:
    if resolution_seconds not in TIER_FOR_RESOLUTION:
        raise ValueError("unsupported rollup resolution")
    groups: Dict[Tuple[int, int, Optional[int], str], dict] = {}
    for record in records:
        start = parse_iso(str(record["window_start"]))
        bucket = int(start.timestamp())
        bucket -= bucket % resolution_seconds
        key = (bucket, int(record["gpu"]), _optional_int(record.get("uid")), str(record.get("status", "unknown")))
        group = groups.setdefault(key, _new_aggregate(record, bucket, resolution_seconds))
        _merge_rollup(group, record)
    return [_finish_aggregate(groups[key]) for key in sorted(groups)]


def _new_aggregate(record: dict, bucket: int, resolution_seconds: int) -> dict:
    return {
        "window_start": to_iso(datetime.fromtimestamp(bucket, timezone.utc)),
        "window_end": to_iso(datetime.fromtimestamp(bucket + resolution_seconds, timezone.utc)),
        "resolution_seconds": resolution_seconds,
        "partial": False,
        "gpu": int(record["gpu"]),
        "uid": _optional_int(record.get("uid")),
        "username": str(record.get("username", record.get("uid", "?"))),
        "status": str(record.get("status", "unknown")),
        "reservation_ids": set(),
        "sample_count": 0,
        "observed_seconds": 0.0,
        "active_sample_count": 0,
        "active_observed_seconds": 0.0,
        "_process_total": 0.0,
        "max_process_count": 0,
        "sm_sample_count": 0,
        "_sm_total": 0.0,
        "max_sm_percent": None,
        "_memory_total": 0.0,
        "_memory_samples": 0,
        "max_gpu_memory_mb": 0,
        "device_util_sample_count": 0,
        "_device_total": 0.0,
        "max_device_util_percent": None,
        "workload_ids": set(),
        "workload_observed_seconds": {},
        "extensions": {},
    }


def _merge_rollup(group: dict, record: dict) -> None:
    samples = max(0, int(record.get("sample_count", 0)))
    group["sample_count"] += samples
    group["observed_seconds"] += max(0.0, float(record.get("observed_seconds", 0)))
    group["active_sample_count"] += max(0, int(record.get("active_sample_count", 0)))
    group["active_observed_seconds"] += max(0.0, float(record.get("active_observed_seconds", 0)))
    group["partial"] = group["partial"] or bool(record.get("partial", False))
    group["reservation_ids"].update(str(value) for value in record.get("reservation_ids", []) if value)
    group["workload_ids"].update(int(value) for value in record.get("workload_ids", []) if int(value) > 0)

    process_avg = _optional_float(record.get("avg_process_count"))
    if process_avg is not None:
        group["_process_total"] += process_avg * samples
    group["max_process_count"] = max(group["max_process_count"], int(record.get("max_process_count") or 0))

    sm_samples = max(0, int(record.get("sm_sample_count", samples if record.get("avg_sm_percent") is not None else 0)))
    sm_avg = _optional_float(record.get("avg_sm_percent"))
    if sm_avg is not None:
        group["_sm_total"] += sm_avg * sm_samples
        group["sm_sample_count"] += sm_samples
    group["max_sm_percent"] = _max_optional(group["max_sm_percent"], record.get("max_sm_percent"))

    memory_avg = _optional_float(record.get("avg_gpu_memory_mb"))
    if memory_avg is not None:
        group["_memory_total"] += memory_avg * samples
        group["_memory_samples"] += samples
    group["max_gpu_memory_mb"] = max(group["max_gpu_memory_mb"], int(record.get("max_gpu_memory_mb") or 0))

    device_samples = max(
        0,
        int(record.get("device_util_sample_count", samples if record.get("avg_device_util_percent") is not None else 0)),
    )
    device_avg = _optional_float(record.get("avg_device_util_percent"))
    if device_avg is not None:
        group["_device_total"] += device_avg * device_samples
        group["device_util_sample_count"] += device_samples
    group["max_device_util_percent"] = _max_optional(
        group["max_device_util_percent"], record.get("max_device_util_percent")
    )
    for workload_id, seconds in record.get("workload_observed_seconds", {}).items():
        key = str(int(workload_id))
        group["workload_observed_seconds"][key] = group["workload_observed_seconds"].get(key, 0.0) + float(seconds)
    extensions = record.get("extensions")
    if isinstance(extensions, dict):
        for namespace, value in extensions.items():
            if namespace in group["extensions"] and group["extensions"][namespace] != value:
                raise ValueError(f"extension {namespace!r} has no registered aggregation rule")
            group["extensions"][namespace] = value


def _finish_aggregate(group: dict) -> dict:
    samples = group.pop("sample_count")
    process_total = group.pop("_process_total")
    memory_total = group.pop("_memory_total")
    memory_samples = group.pop("_memory_samples")
    sm_total = group.pop("_sm_total")
    device_total = group.pop("_device_total")
    group["sample_count"] = samples
    group["avg_process_count"] = round(process_total / samples, 3) if samples else None
    group["avg_sm_percent"] = round(sm_total / group["sm_sample_count"], 3) if group["sm_sample_count"] else None
    group["avg_gpu_memory_mb"] = round(memory_total / memory_samples, 3) if memory_samples else None
    group["avg_device_util_percent"] = (
        round(device_total / group["device_util_sample_count"], 3) if group["device_util_sample_count"] else None
    )
    group["reservation_ids"] = sorted(group["reservation_ids"])
    group["workload_ids"] = sorted(group["workload_ids"])
    group["observed_seconds"] = round(group["observed_seconds"], 3)
    group["active_observed_seconds"] = round(group["active_observed_seconds"], 3)
    if not group["extensions"]:
        group.pop("extensions")
    return group


def unknown_storage_fields(record: dict, record_type: str) -> set[str]:
    allowed = EVENT_STORAGE_FIELDS if record_type == "events" else ROLLUP_STORAGE_FIELDS
    return set(record) - allowed


def _encode_enum(mapping: Dict[str, int], value) -> Optional[int]:
    if value is None:
        return None
    return mapping.get(str(value), 0)


def _decode_enum(mapping: Dict[str, int], value) -> str:
    try:
        code = int(value)
    except (TypeError, ValueError):
        return "unknown"
    reverse = {number: name for name, number in mapping.items()}
    return reverse.get(code, f"unknown({code})")


def _epoch(value) -> int:
    if isinstance(value, datetime):
        return int(value.astimezone(timezone.utc).timestamp())
    return int(parse_iso(str(value)).timestamp())


def _datetime_from_epoch(value) -> datetime:
    return datetime.fromtimestamp(int(value), timezone.utc)


def _iso_from_epoch(value) -> str:
    return to_iso(_datetime_from_epoch(value))


def _optional_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _max_optional(left, right) -> Optional[float]:
    parsed = _optional_float(right)
    if parsed is None:
        return left
    return parsed if left is None else max(float(left), parsed)


def _drop_none(value: dict) -> dict:
    return {key: item for key, item in value.items() if item is not None and item != {} and item != []}


def _rollup_id(record: dict) -> str:
    identity = {
        "window_start": str(record.get("window_start", "")),
        "window_end": str(record.get("window_end", "")),
        "gpu": record.get("gpu"),
        "uid": record.get("uid"),
        "status": record.get("status"),
        "reservation_ids": sorted(str(value) for value in record.get("reservation_ids", [])),
    }
    payload = json.dumps(identity, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()[:20]
