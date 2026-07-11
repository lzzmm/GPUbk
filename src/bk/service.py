from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from .advisor import GpuAdvice, build_gpu_advice
from .allocator import AllocatorDecision, apply_external_allocator
from .config import Config
from .models import MODE_EXCLUSIVE, MODE_SHARED, Actor, BookingError, BookingRequest, BookingResult
from .scheduler import (
    BOOKING_GRANULARITY_SECONDS,
    add_booking,
    find_earliest_slot,
    list_active,
    reservation_pressure_score,
    shared_memory_headroom_for_reservation,
)
from .storage import LedgerStore
from .timeparse import to_iso, utc_now
from .worker import delete_job_spec, prepare_job_spec


AGENT_SCHEMA_VERSION = "bk.agent.v1"


@dataclass(frozen=True)
class BookingSubmission:
    result: BookingResult
    advice: GpuAdvice
    allocator: AllocatorDecision


def submit_booking(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    *,
    count: int,
    duration_seconds: int,
    start_at: datetime,
    mode: str = MODE_SHARED,
    preferred_gpus: Optional[Sequence[int]] = None,
    expected_memory_mb: Optional[int] = None,
    allow_queue: bool = True,
    operation_id: Optional[str] = None,
    command_argv: Optional[List[str]] = None,
    working_directory: Optional[str] = None,
    advice: Optional[GpuAdvice] = None,
) -> BookingSubmission:
    _validate_recommendation_request(config, count, duration_seconds, start_at, mode, expected_memory_mb, allow_queue)
    gpu_advice = advice or build_gpu_advice(config)
    allocator = (
        apply_external_allocator(
            config,
            store,
            actor,
            gpu_advice,
            count=count,
            duration_seconds=duration_seconds,
            start_at=start_at,
            mode=mode,
            expected_memory_mb=expected_memory_mb,
        )
        if preferred_gpus is None
        else AllocatorDecision(list(gpu_advice.order), dict(gpu_advice.scores), "fixed-gpu")
    )
    if command_argv is not None and working_directory is None:
        working_directory = os.getcwd()
    job_spec = (
        prepare_job_spec(config, actor, command_argv, str(working_directory))
        if command_argv is not None
        else None
    )
    try:
        result = add_booking(
            store,
            config,
            BookingRequest(
                actor=actor,
                count=count,
                duration_seconds=duration_seconds,
                start_at=start_at,
                mode=mode,
                preferred_gpus=list(preferred_gpus) if preferred_gpus is not None else None,
                gpu_order=allocator.order,
                gpu_scores=allocator.scores,
                op_id=operation_id,
                allow_queue=allow_queue,
                job_spec_id=job_spec.spec_id if job_spec else None,
                job_digest=job_spec.digest if job_spec else None,
                job_summary=job_spec.summary if job_spec else None,
                expected_memory_mb=expected_memory_mb,
                gpu_memory_capacity_mb=gpu_advice.memory_capacities_mb,
            ),
        )
    except Exception:
        if job_spec is not None:
            delete_job_spec(config, job_spec.spec_id)
        raise
    if job_spec is not None and (
        not result.created or result.reservation.get("job", {}).get("spec_id") != job_spec.spec_id
    ):
        delete_job_spec(config, job_spec.spec_id)
    return BookingSubmission(result, gpu_advice, allocator)


def build_agent_context(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    *,
    at: Optional[datetime] = None,
    advice: Optional[GpuAdvice] = None,
) -> dict:
    generated_at = at or utc_now()
    ledger = store.load()
    active = list_active(ledger, generated_at)
    gpu_advice = advice or build_gpu_advice(config, at=generated_at)
    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        "kind": "context",
        "generated_at": to_iso(generated_at),
        "actor": {"uid": actor.uid, "username": actor.username},
        "policy": {
            "gpu_count": config.gpu_count,
            "default_mode": MODE_SHARED,
            "modes": [MODE_SHARED, MODE_EXCLUSIVE],
            "granularity_minutes": BOOKING_GRANULARITY_SECONDS // 60,
            "max_shared_reservations_per_gpu": config.max_shared_users,
            "require_shared_memory": config.require_shared_memory,
            "shared_memory_reserve_mb": config.shared_memory_reserve_mb,
            "queue_search_hours": config.queue_search_hours,
        },
        "gpu_advice": gpu_advice.as_dict(),
        "reservations": [_public_reservation(item, actor) for item in active],
        "capabilities": {
            "read_only_recommendation": True,
            "idempotent_booking": True,
            "scheduled_jobs": True,
            "private_job_specs": True,
            "external_allocator_is_advisory": True,
            "external_allocator_configured": bool(config.allocator_command),
        },
    }


def recommend_booking(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    *,
    count: int,
    duration_seconds: int,
    start_at: datetime,
    mode: str = MODE_SHARED,
    preferred_gpus: Optional[Sequence[int]] = None,
    expected_memory_mb: Optional[int] = None,
    allow_queue: bool = True,
    advice: Optional[GpuAdvice] = None,
) -> dict:
    _validate_recommendation_request(config, count, duration_seconds, start_at, mode, expected_memory_mb, allow_queue)
    generated_at = utc_now()
    gpu_advice = advice or build_gpu_advice(config, at=generated_at)
    allocator = (
        apply_external_allocator(
            config,
            store,
            actor,
            gpu_advice,
            count=count,
            duration_seconds=duration_seconds,
            start_at=start_at,
            mode=mode,
            expected_memory_mb=expected_memory_mb,
        )
        if preferred_gpus is None
        else AllocatorDecision(list(gpu_advice.order), dict(gpu_advice.scores), "fixed-gpu")
    )
    ledger = store.load()
    duration = timedelta(seconds=duration_seconds)
    slot = find_earliest_slot(
        ledger,
        config,
        count,
        start_at,
        duration,
        mode,
        actor.uid,
        preferred_gpus,
        allow_queue,
        allocator.order,
        allocator.scores,
        expected_memory_mb,
        gpu_advice.memory_capacities_mb,
    )
    nearest = slot
    if slot is None and not allow_queue:
        nearest = find_earliest_slot(
            ledger,
            config,
            count,
            start_at,
            duration,
            mode,
            actor.uid,
            preferred_gpus,
            True,
            allocator.order,
            allocator.scores,
            expected_memory_mb,
            gpu_advice.memory_capacities_mb,
        )

    response = {
        "schema_version": AGENT_SCHEMA_VERSION,
        "kind": "recommendation",
        "generated_at": to_iso(generated_at),
        "request": {
            "count": count,
            "duration_seconds": duration_seconds,
            "start_at": to_iso(start_at),
            "mode": mode,
            "preferred_gpus": list(preferred_gpus) if preferred_gpus is not None else None,
            "expected_memory_mb_per_gpu": expected_memory_mb,
            "allow_queue": allow_queue,
        },
        "available": slot is not None,
        "recommendation": None,
        "nearest_available": _slot_dict(nearest, duration) if slot is None and nearest is not None else None,
        "warnings": [],
        "allocator": {
            "source": allocator.source,
            "reason": allocator.reason,
            "warning": allocator.warning,
        },
    }
    if allocator.warning:
        response["warnings"].append(allocator.warning)
    if slot is None:
        response["warnings"].append("no legal slot found for the requested semantics")
        return response

    scheduled_start, gpus = slot
    scheduled_end = scheduled_start + duration
    fake_reservation = {
        "id": "recommendation",
        "uid": actor.uid,
        "username": actor.username,
        "gpus": list(gpus),
        "mode": mode,
        "start_at": to_iso(scheduled_start),
        "end_at": to_iso(scheduled_end),
        "status": "active",
    }
    if expected_memory_mb is not None:
        fake_reservation["expected_memory_mb"] = expected_memory_mb
    projected = shared_memory_headroom_for_reservation(
        [*list_active(ledger, scheduled_start), fake_reservation],
        fake_reservation,
        gpu_advice.memory_capacities_mb,
        config.max_shared_users,
        config.shared_memory_reserve_mb,
    )
    snapshot_by_gpu = {item.index: item for item in gpu_advice.snapshots}
    gpu_details = []
    for gpu in gpus:
        live = gpu_advice.live_states[gpu]
        history = gpu_advice.historical_loads[gpu]
        snapshot = snapshot_by_gpu.get(gpu)
        gpu_details.append(
            {
                "gpu": gpu,
                "load_score": allocator.scores[gpu],
                "reservation_pressure_score": reservation_pressure_score(
                    ledger,
                    gpu,
                    scheduled_start,
                    scheduled_end,
                    config.max_shared_users,
                ),
                "live_status": live.status,
                "live_reason": live.reason,
                "recent_predicted_load_percent": round(history.predicted_percent, 3),
                "history_sample_count": history.sample_count,
                "memory_total_mb": snapshot.memory_total_mb if snapshot and snapshot.memory_total_mb else None,
                "memory_free_now_mb": (
                    max(0, snapshot.memory_total_mb - snapshot.memory_used_mb)
                    if snapshot and snapshot.memory_total_mb
                    else None
                ),
                "projected_memory_headroom_mb": projected.get(gpu),
            }
        )
    confidence = _recommendation_confidence(gpu_details, snapshot_by_gpu)
    response["recommendation"] = {
        "gpus": list(gpus),
        "start_at": to_iso(scheduled_start),
        "end_at": to_iso(scheduled_end),
        "queued": scheduled_start > start_at,
        "confidence": confidence,
        "gpu_details": gpu_details,
    }
    if any(item["live_status"] == "busy" for item in gpu_details):
        response["warnings"].append("one or more selected GPUs are currently busy; live task end times are unknown")
    if mode == MODE_SHARED and expected_memory_mb is None:
        response["warnings"].append("expected memory was omitted; equal-share memory assumptions were used where possible")
    if any(item["history_sample_count"] == 0 for item in gpu_details):
        response["warnings"].append("recent load history is incomplete; keep bk monitor running for better forecasts")
    return response


def public_reservation(reservation: dict, actor: Actor) -> dict:
    return _public_reservation(reservation, actor)


def _public_reservation(reservation: dict, actor: Actor) -> dict:
    job = reservation.get("job")
    return {
        "id": reservation.get("id"),
        "short_id": str(reservation.get("id", ""))[:8],
        "uid": reservation.get("uid"),
        "username": reservation.get("username"),
        "mine": int(reservation.get("uid", -1)) == actor.uid,
        "gpus": list(reservation.get("gpus", [])),
        "mode": reservation.get("mode"),
        "start_at": reservation.get("start_at"),
        "end_at": reservation.get("end_at"),
        "status": reservation.get("status"),
        "expected_memory_mb_per_gpu": reservation.get("expected_memory_mb"),
        "job": (
            {
                "status": job.get("status"),
                "summary": job.get("summary", "legacy/private command"),
            }
            if isinstance(job, dict)
            else None
        ),
    }


def _validate_recommendation_request(
    config: Config,
    count: int,
    duration_seconds: int,
    start_at: datetime,
    mode: str,
    expected_memory_mb: Optional[int],
    allow_queue: bool,
) -> None:
    if mode not in {MODE_SHARED, MODE_EXCLUSIVE}:
        raise BookingError(f"unsupported booking mode: {mode}")
    if count < 1 or count > config.gpu_count:
        raise BookingError(f"GPU count must be between 1 and {config.gpu_count}")
    if duration_seconds <= 0 or duration_seconds % BOOKING_GRANULARITY_SECONDS:
        raise BookingError("duration must be a positive multiple of 5 minutes")
    if expected_memory_mb is not None and expected_memory_mb <= 0:
        raise BookingError("expected GPU memory must be positive")
    if mode == MODE_SHARED and config.require_shared_memory and expected_memory_mb is None:
        raise BookingError("shared reservations must declare expected memory")
    if not allow_queue and int(start_at.timestamp()) % BOOKING_GRANULARITY_SECONDS:
        raise BookingError("exact start time must align to a 5-minute boundary")


def _slot_dict(slot, duration: timedelta) -> Optional[dict]:
    if slot is None:
        return None
    start, gpus = slot
    return {"gpus": list(gpus), "start_at": to_iso(start), "end_at": to_iso(start + duration)}


def _recommendation_confidence(gpu_details: List[dict], snapshots: Dict[int, object]) -> str:
    if not gpu_details:
        return "low"
    telemetry_known = all(getattr(snapshots.get(item["gpu"]), "source", "none") != "none" for item in gpu_details)
    history_known = all(item["history_sample_count"] > 0 for item in gpu_details)
    if telemetry_known and history_known:
        return "high"
    if telemetry_known or history_known:
        return "medium"
    return "low"
