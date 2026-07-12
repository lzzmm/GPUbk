from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence, Tuple

from .config import Config
from .gpu import (
    GpuSnapshot,
    cuda_visible_device_tokens,
    has_process_telemetry,
    has_stable_device_identifier,
)
from .models import MODE_EXCLUSIVE
from .sharing import inferred_share_memory_mb, reservation_share_units
from .timeparse import utc_now
from .usage import (
    GPU_LIVE_BUSY,
    GPU_LIVE_UNKNOWN,
    USAGE_AUTHORIZED,
    USAGE_SYSTEM,
    assess_gpu_live_states,
    classify_process_usage,
)


@dataclass(frozen=True)
class LaunchGuardDecision:
    ready: bool
    reason: str = ""
    key: str = ""
    cuda_visible_devices: Tuple[str, ...] = ()


def assess_job_launch(
    config: Config,
    reservation: dict,
    snapshots: Sequence[GpuSnapshot],
    active_reservations: Sequence[dict],
    *,
    at: datetime | None = None,
) -> LaunchGuardDecision:
    checked_at = at or utc_now()
    devices = {item.index: item for item in snapshots}
    live = assess_gpu_live_states(snapshots, config.gpu_count)
    usage = classify_process_usage(snapshots, active_reservations, checked_at)
    mode = str(reservation.get("mode", "shared"))
    expected_memory_mb = _positive_int(reservation.get("expected_memory_mb"))
    selected_gpus = list(reservation.get("gpus", []))
    if not selected_gpus:
        return LaunchGuardDecision(False, "reservation has no GPU assignment", "invalid-assignment")

    for gpu_index in selected_gpus:
        index = int(gpu_index)
        device = devices.get(index)
        state = live.get(index)
        if device is None or state is None or device.source == "none" or state.status == GPU_LIVE_UNKNOWN:
            return LaunchGuardDecision(
                False,
                f"GPU {index} live telemetry is unavailable",
                f"gpu:{index}:telemetry",
            )
        if not has_process_telemetry(device):
            return LaunchGuardDecision(
                False,
                f"GPU {index} process telemetry is unavailable",
                f"gpu:{index}:process-telemetry",
            )
        if device.source == "nvml" and not has_stable_device_identifier(device):
            return LaunchGuardDecision(
                False,
                f"GPU {index} stable identifier is unavailable",
                f"gpu:{index}:stable-identifier",
            )

        rows = [item for item in usage.get(index, []) if item.status != USAGE_SYSTEM]
        unsafe = [item for item in rows if item.status != USAGE_AUTHORIZED]
        if unsafe:
            item = unsafe[0]
            owner = item.process.username or (
                str(item.process.uid) if item.process.uid is not None else "unknown"
            )
            return LaunchGuardDecision(
                False,
                f"GPU {index} has {item.status} process user={owner} pid={item.process.pid}",
                f"gpu:{index}:process:{item.status}",
            )

        if mode == MODE_EXCLUSIVE and rows:
            item = rows[0]
            owner = item.process.username or (
                str(item.process.uid) if item.process.uid is not None else "unknown"
            )
            return LaunchGuardDecision(
                False,
                f"exclusive GPU {index} still has process user={owner} pid={item.process.pid}",
                f"gpu:{index}:exclusive-process",
            )

        required_memory_mb = expected_memory_mb
        if mode != MODE_EXCLUSIVE and required_memory_mb is None:
            required_memory_mb = _inferred_share_memory(config, reservation, device)
            if required_memory_mb is None:
                return LaunchGuardDecision(
                    False,
                    f"GPU {index} memory capacity is unavailable",
                    f"gpu:{index}:memory-capacity",
                )
        if required_memory_mb is not None:
            if device.memory_total_mb <= 0:
                return LaunchGuardDecision(
                    False,
                    f"GPU {index} memory capacity is unavailable",
                    f"gpu:{index}:memory-capacity",
                )
            available_mb = max(
                0,
                device.memory_total_mb
                - device.memory_used_mb
                - config.shared_memory_reserve_mb,
            )
            if available_mb < required_memory_mb:
                return LaunchGuardDecision(
                    False,
                    f"GPU {index} has {available_mb}MiB launch headroom; "
                    f"{required_memory_mb}MiB is required",
                    f"gpu:{index}:vram",
                )

        if state.status == GPU_LIVE_BUSY and (mode == MODE_EXCLUSIVE or not rows):
            reason = state.reason or "activity is present but process ownership is unavailable"
            return LaunchGuardDecision(
                False,
                f"GPU {index} is busy: {reason}",
                f"gpu:{index}:busy",
            )

    return LaunchGuardDecision(
        True,
        cuda_visible_devices=cuda_visible_device_tokens(selected_gpus, snapshots),
    )


def _inferred_share_memory(
    config: Config, reservation: dict, device: GpuSnapshot
) -> int | None:
    if device.memory_total_mb <= 0:
        return None
    usable = max(0, device.memory_total_mb - config.shared_memory_reserve_mb)
    return inferred_share_memory_mb(
        usable,
        config.max_shared_users,
        reservation_share_units(reservation, config.max_shared_users),
    )


def _positive_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
