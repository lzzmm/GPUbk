from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from .config import MAX_GPU_COUNT, MAX_MONITOR_INTERVAL_SECONDS, MAX_MONITOR_ROLLUP_SECONDS, MAX_UID
from .timeparse import parse_iso, to_iso


COLLECTOR_STATUS_SCHEMA_VERSION = "gpubk.collector.v1"
COLLECTOR_STATES = frozenset({"running", "degraded", "stopped"})
MAX_MONITOR_ID_LENGTH = 128
MAX_HOSTNAME_LENGTH = 255
MAX_SOURCE_LENGTH = 64
MAX_PID = 2**31 - 1
MIN_STALE_AFTER_SECONDS = 30.0
STALE_HEARTBEAT_MULTIPLIER = 3.0
MAX_CLOCK_SKEW_SECONDS = 300.0


class CollectorStatusError(ValueError):
    pass


def validate_collector_document(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise CollectorStatusError("collector status must be a JSON object")
    if payload.get("schema_version") != COLLECTOR_STATUS_SCHEMA_VERSION:
        raise CollectorStatusError(
            f"unsupported collector status schema: {payload.get('schema_version')!r}"
        )
    _bounded_text(payload.get("monitor_id"), "monitor_id", MAX_MONITOR_ID_LENGTH)
    state = payload.get("status")
    if state not in COLLECTOR_STATES:
        raise CollectorStatusError(f"collector status must be one of {sorted(COLLECTOR_STATES)}")
    _bounded_int(payload.get("uid"), "uid", 0, MAX_UID)
    _bounded_int(payload.get("pid"), "pid", 1, MAX_PID)
    _bounded_text(payload.get("hostname"), "hostname", MAX_HOSTNAME_LENGTH)
    heartbeat = _bounded_number(
        payload.get("heartbeat_interval_seconds"),
        "heartbeat_interval_seconds",
        0.1,
        MAX_MONITOR_INTERVAL_SECONDS,
    )
    _bounded_number(
        payload.get("sample_interval_seconds"),
        "sample_interval_seconds",
        0.1,
        MAX_MONITOR_INTERVAL_SECONDS,
    )
    _bounded_int(
        payload.get("rollup_seconds"),
        "rollup_seconds",
        1,
        MAX_MONITOR_ROLLUP_SECONDS,
    )
    started_at = _timestamp(payload.get("started_at"), "started_at")
    sampled_at = _timestamp(payload.get("sampled_at"), "sampled_at")
    written_at = _timestamp(payload.get("written_at"), "written_at")
    if sampled_at < started_at:
        raise CollectorStatusError("sampled_at must not precede started_at")
    if written_at < sampled_at:
        raise CollectorStatusError("written_at must not precede sampled_at")
    stopped_at = payload.get("stopped_at")
    if state == "stopped":
        parsed_stopped_at = _timestamp(stopped_at, "stopped_at")
        if parsed_stopped_at < sampled_at or written_at < parsed_stopped_at:
            raise CollectorStatusError(
                "stopped_at must be between sampled_at and written_at"
            )
    elif stopped_at is not None:
        raise CollectorStatusError("stopped_at is only valid for stopped collectors")

    devices = payload.get("devices")
    if not isinstance(devices, list):
        raise CollectorStatusError("devices must be an array")
    if len(devices) > MAX_GPU_COUNT:
        raise CollectorStatusError(f"devices exceeds the {MAX_GPU_COUNT} GPU limit")
    indices = []
    stable_identifier_capabilities = []
    stable_identifier_fields_present = []
    expected_process_gap = []
    expected_utilization_gap = []
    for position, device in enumerate(devices):
        if not isinstance(device, dict):
            raise CollectorStatusError(f"devices[{position}] must be an object")
        gpu = _bounded_int(device.get("gpu"), f"devices[{position}].gpu", 0, MAX_GPU_COUNT - 1)
        indices.append(gpu)
        _bounded_text(device.get("source"), f"devices[{position}].source", MAX_SOURCE_LENGTH)
        for key in (
            "device_telemetry",
            "process_telemetry",
            "process_utilization",
        ):
            if not isinstance(device.get(key), bool):
                raise CollectorStatusError(f"devices[{position}].{key} must be boolean")
        device_telemetry = bool(device["device_telemetry"])
        process_telemetry = bool(device["process_telemetry"])
        process_utilization = bool(device["process_utilization"])
        stable_identifier_present = "stable_device_identifier" in device
        stable_identifier = device.get("stable_device_identifier")
        if stable_identifier_present and not isinstance(stable_identifier, bool):
            raise CollectorStatusError(
                f"devices[{position}].stable_device_identifier must be boolean"
            )
        stable_identifier_fields_present.append(stable_identifier_present)
        stable_identifier_capabilities.append(
            stable_identifier if stable_identifier_present else None
        )
        if process_telemetry and not device_telemetry:
            raise CollectorStatusError(
                f"devices[{position}].process_telemetry requires device telemetry"
            )
        if stable_identifier is True and not device_telemetry:
            raise CollectorStatusError(
                f"devices[{position}].stable_device_identifier requires device telemetry"
            )
        if process_utilization and not process_telemetry:
            raise CollectorStatusError(
                f"devices[{position}].process_utilization requires process telemetry"
            )
        if not process_telemetry:
            expected_process_gap.append(gpu)
        elif not process_utilization:
            expected_utilization_gap.append(gpu)
    if indices != list(range(len(devices))):
        raise CollectorStatusError("device GPU indices must be unique and contiguous from 0")
    if any(stable_identifier_fields_present) and not all(
        stable_identifier_fields_present
    ):
        raise CollectorStatusError(
            "stable device identifier capability must be present for every GPU or none"
        )
    stable_gap_present = "stable_device_identifier_gap" in payload
    identity_gap_present = "process_identity_gap" in payload
    if stable_gap_present != all(stable_identifier_fields_present):
        raise CollectorStatusError(
            "stable_device_identifier_gap must accompany per-device stable identifier capabilities"
        )
    process_gap = _gpu_list(payload.get("process_telemetry_gap"), "process_telemetry_gap")
    identity_gap = (
        _gpu_list(payload.get("process_identity_gap"), "process_identity_gap")
        if identity_gap_present
        else []
    )
    utilization_gap = _gpu_list(
        payload.get("process_utilization_gap"), "process_utilization_gap"
    )
    stable_identifier_gap = (
        _gpu_list(
            payload.get("stable_device_identifier_gap"),
            "stable_device_identifier_gap",
        )
        if stable_gap_present
        else []
    )
    device_indices = set(indices)
    if not set(process_gap).issubset(device_indices):
        raise CollectorStatusError("process_telemetry_gap contains an unknown GPU")
    if not set(identity_gap).issubset(device_indices):
        raise CollectorStatusError("process_identity_gap contains an unknown GPU")
    if not set(utilization_gap).issubset(device_indices):
        raise CollectorStatusError("process_utilization_gap contains an unknown GPU")
    if not set(stable_identifier_gap).issubset(device_indices):
        raise CollectorStatusError("stable_device_identifier_gap contains an unknown GPU")
    if process_gap != expected_process_gap:
        raise CollectorStatusError(
            "process_telemetry_gap must match per-device process telemetry capabilities"
        )
    if identity_gap_present and not set(process_gap).issubset(identity_gap):
        raise CollectorStatusError(
            "process_identity_gap must include every process telemetry gap"
        )
    if utilization_gap != expected_utilization_gap:
        raise CollectorStatusError(
            "process_utilization_gap must match per-device utilization capabilities"
        )
    if stable_gap_present:
        expected_stable_gap = [
            gpu
            for gpu, available in zip(indices, stable_identifier_capabilities)
            if available is False
        ]
        if stable_identifier_gap != expected_stable_gap:
            raise CollectorStatusError(
                "stable_device_identifier_gap must match per-device stable identifier capabilities"
            )
    safety_degraded = bool(
        process_gap
        or identity_gap
        or stable_identifier_gap
        or any(not bool(item["device_telemetry"]) for item in devices)
    )
    any_capability_gap = bool(safety_degraded or utilization_gap)
    if state == "running" and safety_degraded:
        raise CollectorStatusError("running collector status contains degraded capabilities")
    if state == "degraded" and not any_capability_gap:
        raise CollectorStatusError("degraded collector status has no capability gap")
    if heartbeat <= 0:
        raise CollectorStatusError("heartbeat_interval_seconds must be positive")
    return payload


def classify_collector_document(
    payload: dict,
    *,
    now: Optional[datetime] = None,
    expected_gpu_count: Optional[int] = None,
) -> dict:
    validate_collector_document(payload)
    if expected_gpu_count is not None:
        _bounded_int(expected_gpu_count, "expected_gpu_count", 1, MAX_GPU_COUNT)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    written_at = parse_iso(str(payload["written_at"]))
    raw_age = (current - written_at).total_seconds()
    heartbeat = float(payload["heartbeat_interval_seconds"])
    stale_after = max(MIN_STALE_AFTER_SECONDS, heartbeat * STALE_HEARTBEAT_MULTIPLIER)
    reported = str(payload["status"])
    legacy_stable_capability = "stable_device_identifier_gap" not in payload
    legacy_process_identity = "process_identity_gap" not in payload
    topology_match = expected_gpu_count is None or len(payload["devices"]) == expected_gpu_count
    if raw_age < -MAX_CLOCK_SKEW_SECONDS:
        state = "clock-skew"
        fresh = False
    elif reported == "stopped":
        state = "stopped"
        fresh = False
    elif raw_age > stale_after:
        state = "stale"
        fresh = False
    elif not topology_match:
        state = "topology-mismatch"
        fresh = False
    else:
        state = (
            "degraded"
            if (legacy_stable_capability or legacy_process_identity)
            and reported == "running"
            else reported
        )
        fresh = True
    public_devices = []
    for item in payload["devices"]:
        public_item = dict(item)
        public_item.setdefault("stable_device_identifier", False)
        public_devices.append(public_item)
    stable_identifier_gap = (
        list(payload["stable_device_identifier_gap"])
        if not legacy_stable_capability
        else [int(item["gpu"]) for item in payload["devices"]]
    )
    process_identity_gap = (
        list(payload["process_identity_gap"])
        if not legacy_process_identity
        else [int(item["gpu"]) for item in payload["devices"]]
    )
    result = {
        "schema_version": COLLECTOR_STATUS_SCHEMA_VERSION,
        "state": state,
        "reported_status": reported,
        "fresh": fresh,
        "age_seconds": round(max(0.0, raw_age), 3),
        "stale_after_seconds": round(stale_after, 3),
        "monitor_id": str(payload["monitor_id"]),
        "uid": int(payload["uid"]),
        "pid": int(payload["pid"]),
        "hostname": str(payload["hostname"]),
        "started_at": str(payload["started_at"]),
        "sampled_at": str(payload["sampled_at"]),
        "written_at": str(payload["written_at"]),
        "stopped_at": payload.get("stopped_at"),
        "heartbeat_interval_seconds": heartbeat,
        "sample_interval_seconds": float(payload["sample_interval_seconds"]),
        "rollup_seconds": int(payload["rollup_seconds"]),
        "devices": public_devices,
        "stable_device_identifier_capability_known": not legacy_stable_capability,
        "stable_device_identifier_gap": stable_identifier_gap,
        "process_telemetry_gap": list(payload["process_telemetry_gap"]),
        "process_identity_capability_known": not legacy_process_identity,
        "process_identity_gap": process_identity_gap,
        "process_utilization_gap": list(payload["process_utilization_gap"]),
    }
    if state == "clock-skew":
        result["clock_skew_seconds"] = round(-raw_age, 3)
    if expected_gpu_count is not None:
        result["expected_gpu_count"] = expected_gpu_count
        result["topology_match"] = topology_match
    return result


def absent_collector_status() -> dict:
    return {
        "schema_version": COLLECTOR_STATUS_SCHEMA_VERSION,
        "state": "not-seen",
        "reported_status": None,
        "fresh": None,
        "age_seconds": None,
        "stale_after_seconds": None,
    }


def invalid_collector_status(message: str, *, incompatible: bool = False) -> dict:
    return {
        "schema_version": COLLECTOR_STATUS_SCHEMA_VERSION,
        "state": "incompatible" if incompatible else "invalid",
        "reported_status": None,
        "fresh": False,
        "age_seconds": None,
        "stale_after_seconds": None,
        "error": message,
    }


def collector_document(
    *,
    monitor_id: str,
    status: str,
    uid: int,
    pid: int,
    hostname: str,
    heartbeat_interval_seconds: float,
    sample_interval_seconds: float,
    rollup_seconds: int,
    started_at: datetime,
    sampled_at: datetime,
    devices: list[dict],
    stable_device_identifier_gap: list[int],
    process_telemetry_gap: list[int],
    process_utilization_gap: list[int],
    process_identity_gap: Optional[list[int]] = None,
    written_at: Optional[datetime] = None,
    stopped_at: Optional[datetime] = None,
) -> dict:
    written = (written_at or sampled_at).astimezone(timezone.utc)
    payload = {
        "schema_version": COLLECTOR_STATUS_SCHEMA_VERSION,
        "monitor_id": monitor_id,
        "status": status,
        "uid": uid,
        "pid": pid,
        "hostname": hostname,
        "heartbeat_interval_seconds": heartbeat_interval_seconds,
        "sample_interval_seconds": sample_interval_seconds,
        "rollup_seconds": rollup_seconds,
        "started_at": to_iso(started_at),
        "sampled_at": to_iso(sampled_at),
        "written_at": to_iso(written),
        "stopped_at": to_iso(stopped_at) if stopped_at is not None else None,
        "devices": devices,
        "stable_device_identifier_gap": stable_device_identifier_gap,
        "process_telemetry_gap": process_telemetry_gap,
        "process_identity_gap": (
            process_telemetry_gap
            if process_identity_gap is None
            else process_identity_gap
        ),
        "process_utilization_gap": process_utilization_gap,
    }
    validate_collector_document(payload)
    return payload


def safe_hostname(value: object) -> str:
    text = "".join(
        character if 0x20 <= ord(character) < 0x7F else "?"
        for character in str(value)
    ).strip()
    return text[:MAX_HOSTNAME_LENGTH] or "unknown"


def _gpu_list(value: object, label: str) -> list[int]:
    if not isinstance(value, list):
        raise CollectorStatusError(f"{label} must be an array")
    result = [
        _bounded_int(item, f"{label}[{position}]", 0, MAX_GPU_COUNT - 1)
        for position, item in enumerate(value)
    ]
    if result != sorted(set(result)):
        raise CollectorStatusError(f"{label} must contain sorted unique GPU indices")
    return result


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CollectorStatusError(f"{label} must be an ISO 8601 timestamp")
    try:
        return parse_iso(value)
    except (TypeError, ValueError) as exc:
        raise CollectorStatusError(f"{label} must be an ISO 8601 timestamp") from exc


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CollectorStatusError(f"{label} must be 1-{maximum} characters")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise CollectorStatusError(f"{label} contains a control character")
    return value


def _bounded_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CollectorStatusError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise CollectorStatusError(f"{label} must be between {minimum} and {maximum}")
    return value


def _bounded_number(
    value: object,
    label: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CollectorStatusError(f"{label} must be a number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise CollectorStatusError(f"{label} must be between {minimum:g} and {maximum:g}")
    return parsed
