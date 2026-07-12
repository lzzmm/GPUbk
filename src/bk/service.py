from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from .advisor import GpuAdvice, build_gpu_advice
from .allocator import AllocatorDecision, apply_external_allocator
from .config import Config
from .granularity import is_slot_aligned, slot_phrase
from .models import MODE_EXCLUSIVE, MODE_SHARED, Actor, BookingError, BookingRequest, BookingResult, EditRequest
from .policy import validate_ledger_policy
from .scheduler import (
    MAX_EDIT_OPERATIONS_PER_RESERVATION,
    add_booking,
    cancel_booking,
    edit_booking,
    find_earliest_slot,
    list_active,
    reservation_pressure_scores,
    shared_memory_headroom_for_reservation,
)
from .sharing import normalize_share_units, reservation_share_units, share_text
from .storage import AUDIT_SCHEMA_VERSION, LedgerStore
from .timeparse import normalize_queue_start, parse_iso, to_iso, utc_now
from .worker import (
    JOB_SPEC_ORPHAN_GRACE_SECONDS,
    WORKER_BUSY_EXIT_CODE,
    WORKER_WAITING_EXIT_CODE,
    JobSpecCleanupResult,
    cleanup_job_specs,
    delete_job_spec,
    prepare_job_spec,
)


AGENT_SCHEMA_VERSION = "bk.agent.v1"


@dataclass(frozen=True)
class BookingSubmission:
    result: BookingResult
    advice: GpuAdvice
    allocator: AllocatorDecision
    share_capacity_units: int


@dataclass(frozen=True)
class CancellationSubmission:
    reservation: dict
    cleanup: JobSpecCleanupResult


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
    share_units: Optional[int] = None,
    allow_queue: bool = True,
    operation_id: Optional[str] = None,
    command_argv: Optional[List[str]] = None,
    working_directory: Optional[str] = None,
    advice: Optional[GpuAdvice] = None,
) -> BookingSubmission:
    generated_at = utc_now()
    effective_start = (
        normalize_queue_start(start_at, generated_at, config.slot_minutes)
        if allow_queue
        else start_at
    )
    effective_share_units = _validate_recommendation_request(
        config,
        count,
        duration_seconds,
        effective_start,
        mode,
        expected_memory_mb,
        allow_queue,
        share_units,
    )
    validate_ledger_policy(store.load(), config)
    gpu_advice = advice or build_gpu_advice(config)
    allocator = _allocation_decision(
        config,
        store,
        actor,
        gpu_advice,
        count,
        duration_seconds,
        effective_start,
        mode,
        preferred_gpus,
        expected_memory_mb,
        effective_share_units,
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
                start_at=effective_start,
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
                share_units=effective_share_units if mode == MODE_SHARED else None,
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
    return BookingSubmission(result, gpu_advice, allocator, config.max_shared_users)


def submit_edit(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    reservation_id: str,
    *,
    duration_seconds: Optional[int] = None,
    start_at: Optional[datetime] = None,
    mode: Optional[str] = None,
    preferred_gpus: Optional[Sequence[int]] = None,
    count: Optional[int] = None,
    expected_memory_mb: Optional[int] = None,
    update_expected_memory: bool = False,
    share_units: Optional[int] = None,
    update_share_units: bool = False,
    allow_queue: bool = False,
    operation_id: Optional[str] = None,
    advice: Optional[GpuAdvice] = None,
) -> BookingSubmission:
    ledger = store.load()
    reservation = next(
        (item for item in ledger.get("reservations", []) if item.get("id") == reservation_id),
        None,
    )
    if reservation is None:
        raise BookingError("reservation not found")
    if int(reservation.get("uid", -1)) != actor.uid:
        raise BookingError("permission denied: reservation belongs to another UID")

    current_start = _reservation_datetime(reservation, "start_at")
    current_end = _reservation_datetime(reservation, "end_at")
    effective_duration = duration_seconds or int((current_end - current_start).total_seconds())
    effective_start = start_at or current_start
    effective_mode = mode or str(reservation.get("mode", MODE_SHARED))
    effective_memory = (
        expected_memory_mb
        if update_expected_memory
        else _optional_int(reservation.get("expected_memory_mb"))
    )
    if effective_mode == MODE_SHARED:
        effective_share_units = (
            share_units
            if update_share_units
            else (
                reservation_share_units(reservation, config.max_shared_users)
                if reservation.get("mode") == MODE_SHARED
                else None
            )
        )
    else:
        effective_share_units = share_units if update_share_units else None
    effective_preferred = preferred_gpus
    if effective_preferred is None and count is None:
        effective_preferred = [int(gpu) for gpu in reservation.get("gpus", [])]
    effective_count = count or (
        len(effective_preferred)
        if effective_preferred is not None
        else len(reservation.get("gpus", []))
    )
    normalized_share_units = _validate_recommendation_request(
        config,
        effective_count,
        effective_duration,
        effective_start,
        effective_mode,
        effective_memory,
        allow_queue,
        effective_share_units,
    )

    gpu_advice = advice or build_gpu_advice(config)
    allocator = _allocation_decision(
        config,
        store,
        actor,
        gpu_advice,
        effective_count,
        effective_duration,
        effective_start,
        effective_mode,
        effective_preferred,
        effective_memory,
        normalized_share_units,
    )
    result = edit_booking(
        store,
        config,
        EditRequest(
            actor=actor,
            reservation_id=reservation_id,
            op_id=operation_id,
            start_at=start_at,
            duration_seconds=duration_seconds,
            mode=mode,
            preferred_gpus=list(preferred_gpus) if preferred_gpus is not None else None,
            gpu_order=allocator.order,
            gpu_scores=allocator.scores,
            count=count,
            allow_queue=allow_queue,
            expected_memory_mb=expected_memory_mb,
            update_expected_memory=update_expected_memory,
            gpu_memory_capacity_mb=gpu_advice.memory_capacities_mb,
            share_units=share_units,
            update_share_units=update_share_units,
        ),
    )
    return BookingSubmission(result, gpu_advice, allocator, config.max_shared_users)


def submit_cancellation(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    reservation_id: str,
) -> CancellationSubmission:
    reservation = cancel_booking(store, reservation_id, actor)
    transaction_warning = store.last_warning
    try:
        cleanup = cleanup_job_specs(config, store, actor)
    except (BookingError, OSError, ValueError) as exc:
        cleanup = JobSpecCleanupResult(
            failed=1,
            warnings=(f"private job spec cleanup failed: {exc}",),
        )
    warnings = [item for item in (transaction_warning, store.last_warning) if item]
    if cleanup.failed:
        detail = cleanup.warnings[0] if cleanup.warnings else "unknown cleanup error"
        warnings.append(f"{cleanup.failed} private job spec cleanup issue(s): {detail}")
    store.last_warning = "; ".join(dict.fromkeys(warnings)) or None
    return CancellationSubmission(reservation, cleanup)


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
    validate_ledger_policy(ledger, config)
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
            "granularity_minutes": config.slot_minutes,
            "max_shared_reservations_per_gpu": config.max_shared_users,
            "shared_capacity_units_per_gpu": config.max_shared_users,
            "default_share_units_per_gpu": 1,
            "require_shared_memory": config.require_shared_memory,
            "shared_memory_reserve_mb": config.shared_memory_reserve_mb,
            "queue_search_hours": config.queue_search_hours,
            "ledger_retention_days": config.ledger_retention_days,
            "worker_live_guard": config.worker_live_guard,
            "worker_recovery_grace_seconds": config.worker_recovery_grace_seconds,
            "worker_busy_exit_code": WORKER_BUSY_EXIT_CODE,
            "worker_waiting_exit_code": WORKER_WAITING_EXIT_CODE,
            "job_log_retention_days": config.job_log_retention_days,
            "job_log_max_mb": config.job_log_max_mb,
            "job_log_total_max_mb": config.job_log_total_max_mb,
            "usage_retention": {
                "load_minutes": config.usage_load_window_minutes,
                "minute_days": config.usage_minute_retention_days,
                "five_minute_days": config.usage_five_minute_retention_days,
                "ten_minute_days": config.usage_ten_minute_retention_days,
                "hourly_days": config.usage_hourly_retention_days,
                "daily_days": config.usage_daily_retention_days,
                "event_days": config.usage_event_retention_days,
            },
        },
        "gpu_advice": gpu_advice.as_dict(),
        "reservations": [
            _public_reservation(item, actor, config.max_shared_users) for item in active
        ],
        "capabilities": {
            "read_only_recommendation": True,
            "idempotent_booking": True,
            "idempotent_edit": True,
            "weighted_shared_capacity": True,
            "configurable_booking_granularity": True,
            "idempotent_edit_history_limit": MAX_EDIT_OPERATIONS_PER_RESERVATION,
            "structured_cancel": True,
            "scheduled_jobs": True,
            "scheduled_job_live_guard": config.worker_live_guard,
            "single_worker_lease": True,
            "scheduled_job_crash_recovery": True,
            "private_job_specs": True,
            "private_job_spec_cleanup": True,
            "private_job_spec_orphan_grace_seconds": JOB_SPEC_ORPHAN_GRACE_SECONDS,
            "bounded_private_job_logs": True,
            "private_job_log_cleanup": True,
            "bounded_personal_audit_log": True,
            "audit_api_schema": AUDIT_SCHEMA_VERSION,
            "versioned_usage_history": True,
            "usage_api_schema": "gpubk.usage.v1",
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
    share_units: Optional[int] = None,
    allow_queue: bool = True,
    advice: Optional[GpuAdvice] = None,
) -> dict:
    generated_at = utc_now()
    effective_start = (
        normalize_queue_start(start_at, generated_at, config.slot_minutes)
        if allow_queue
        else start_at
    )
    effective_share_units = _validate_recommendation_request(
        config,
        count,
        duration_seconds,
        effective_start,
        mode,
        expected_memory_mb,
        allow_queue,
        share_units,
    )
    ledger = store.load()
    validate_ledger_policy(ledger, config)
    gpu_advice = advice or build_gpu_advice(config, at=generated_at)
    allocator = _allocation_decision(
        config,
        store,
        actor,
        gpu_advice,
        count,
        duration_seconds,
        effective_start,
        mode,
        preferred_gpus,
        expected_memory_mb,
        effective_share_units,
    )
    duration = timedelta(seconds=duration_seconds)
    slot = find_earliest_slot(
        ledger,
        config,
        count,
        effective_start,
        duration,
        mode,
        actor.uid,
        preferred_gpus,
        allow_queue,
        allocator.order,
        allocator.scores,
        expected_memory_mb,
        gpu_advice.memory_capacities_mb,
        effective_share_units,
    )
    nearest = slot
    if slot is None and not allow_queue:
        nearest = find_earliest_slot(
            ledger,
            config,
            count,
            effective_start,
            duration,
            mode,
            actor.uid,
            preferred_gpus,
            True,
            allocator.order,
            allocator.scores,
            expected_memory_mb,
            gpu_advice.memory_capacities_mb,
            effective_share_units,
        )

    response = {
        "schema_version": AGENT_SCHEMA_VERSION,
        "kind": "recommendation",
        "generated_at": to_iso(generated_at),
        "request": {
            "count": count,
            "duration_seconds": duration_seconds,
            "start_at": to_iso(effective_start),
            "mode": mode,
            "preferred_gpus": list(preferred_gpus) if preferred_gpus is not None else None,
            "expected_memory_mb_per_gpu": expected_memory_mb,
            "share_units_per_gpu": effective_share_units if mode == MODE_SHARED else None,
            "share_fraction_per_gpu": (
                share_text(effective_share_units, config.max_shared_users)
                if mode == MODE_SHARED
                else None
            ),
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
    if mode == MODE_SHARED:
        fake_reservation["share_units"] = effective_share_units
    projected = shared_memory_headroom_for_reservation(
        [*list_active(ledger, scheduled_start), fake_reservation],
        fake_reservation,
        gpu_advice.memory_capacities_mb,
        config.max_shared_users,
        config.shared_memory_reserve_mb,
    )
    gpu_details, confidence = _recommendation_gpu_details(
        config,
        ledger,
        gpu_advice,
        allocator,
        gpus,
        scheduled_start,
        scheduled_end,
        projected,
    )
    response["recommendation"] = {
        "gpus": list(gpus),
        "start_at": to_iso(scheduled_start),
        "end_at": to_iso(scheduled_end),
        "queued": scheduled_start > effective_start,
        "confidence": confidence,
        "gpu_details": gpu_details,
    }
    if any(item["live_status"] == "busy" for item in gpu_details):
        response["warnings"].append("one or more selected GPUs are currently busy; live task end times are unknown")
    if mode == MODE_SHARED and expected_memory_mb is None:
        response["warnings"].append(
            "expected memory was omitted; share-weighted memory assumptions were used where possible"
        )
    if any(item["history_sample_count"] == 0 for item in gpu_details):
        response["warnings"].append("recent load history is incomplete; keep bk monitor running for better forecasts")
    return response


def _allocation_decision(
    config: Config,
    store: LedgerStore,
    actor: Actor,
    advice: GpuAdvice,
    count: int,
    duration_seconds: int,
    start_at: datetime,
    mode: str,
    preferred_gpus: Optional[Sequence[int]],
    expected_memory_mb: Optional[int],
    share_units: int,
) -> AllocatorDecision:
    if preferred_gpus is not None:
        return AllocatorDecision(list(advice.order), dict(advice.scores), "fixed-gpu")
    return apply_external_allocator(
        config,
        store,
        actor,
        advice,
        count=count,
        duration_seconds=duration_seconds,
        start_at=start_at,
        mode=mode,
        expected_memory_mb=expected_memory_mb,
        share_units=share_units,
    )


def _recommendation_gpu_details(
    config: Config,
    ledger: dict,
    advice: GpuAdvice,
    allocator: AllocatorDecision,
    gpus: Sequence[int],
    start: datetime,
    end: datetime,
    projected_memory: Dict[int, int],
) -> tuple[List[dict], str]:
    snapshots = {item.index: item for item in advice.snapshots}
    pressure_scores = reservation_pressure_scores(ledger, gpus, start, end, config.max_shared_users)
    details = []
    for gpu in gpus:
        live = advice.live_states[gpu]
        history = advice.historical_loads[gpu]
        snapshot = snapshots.get(gpu)
        total_memory = snapshot.memory_total_mb if snapshot and snapshot.memory_total_mb else None
        details.append(
            {
                "gpu": gpu,
                "load_score": allocator.scores[gpu],
                "reservation_pressure_score": pressure_scores[gpu],
                "live_status": live.status,
                "live_reason": live.reason,
                "recent_predicted_load_percent": round(history.predicted_percent, 3),
                "history_sample_count": history.sample_count,
                "memory_total_mb": total_memory,
                "memory_free_now_mb": max(0, total_memory - snapshot.memory_used_mb) if total_memory else None,
                "projected_memory_headroom_mb": projected_memory.get(gpu),
            }
        )
    return details, _recommendation_confidence(details, snapshots)


def public_reservation(
    reservation: dict,
    actor: Actor,
    share_capacity_units: Optional[int] = None,
) -> dict:
    return _public_reservation(reservation, actor, share_capacity_units)


def booking_result_payload(
    status: str,
    submission: BookingSubmission,
    actor: Actor,
    warning: Optional[str] = None,
) -> dict:
    reservation = submission.result.reservation
    selected = []
    for gpu in reservation.get("gpus", []):
        index = int(gpu)
        live = submission.advice.live_states[index]
        historical = submission.advice.historical_loads[index]
        selected.append(
            {
                "gpu": index,
                "load_score": submission.allocator.scores[index],
                "live_status": live.status,
                "live_reason": live.reason,
                "recent_predicted_load_percent": round(historical.predicted_percent, 3),
                "history_sample_count": historical.sample_count,
            }
        )
    warnings = [warning] if warning else []
    if submission.allocator.warning:
        warnings.append(submission.allocator.warning)
    if any(item["live_status"] == "busy" for item in selected):
        warnings.append("selected GPU is currently busy; live task end time is unknown")
    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        "kind": "booking_result",
        "status": status,
        "reservation": public_reservation(
            reservation, actor, submission.share_capacity_units
        ),
        "allocation": {"selected": selected},
        "allocator": {
            "source": submission.allocator.source,
            "reason": submission.allocator.reason,
        },
        "warnings": warnings,
    }


def _public_reservation(
    reservation: dict,
    actor: Actor,
    share_capacity_units: Optional[int] = None,
) -> dict:
    job = reservation.get("job")
    share_units = (
        reservation_share_units(reservation, max(1, share_capacity_units or 1))
        if reservation.get("mode") == MODE_SHARED
        else None
    )
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
        "share_units_per_gpu": share_units,
        "share_capacity_units_per_gpu": (
            share_capacity_units if reservation.get("mode") == MODE_SHARED else None
        ),
        "share_fraction_per_gpu": (
            share_text(share_units, share_capacity_units)
            if share_units is not None and share_capacity_units is not None
            else None
        ),
        "job": (
            {
                "status": job.get("status"),
                "summary": job.get("summary", "legacy/private command"),
                "launch_guard_state": job.get("launch_guard_state"),
                "message": job.get("message"),
                "waiting_since": job.get("waiting_since"),
                "recovery_state": job.get("recovery_state"),
                "recovered_at": job.get("recovered_at"),
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
    share_units: Optional[int],
) -> int:
    if mode not in {MODE_SHARED, MODE_EXCLUSIVE}:
        raise BookingError(f"unsupported booking mode: {mode}")
    if count < 1 or count > config.gpu_count:
        raise BookingError(f"GPU count must be between 1 and {config.gpu_count}")
    if duration_seconds <= 0 or duration_seconds % config.slot_seconds:
        raise BookingError(
            f"duration must be a positive multiple of {config.slot_minutes} minutes"
        )
    if expected_memory_mb is not None and expected_memory_mb <= 0:
        raise BookingError("expected GPU memory must be positive")
    if mode == MODE_SHARED and config.require_shared_memory and expected_memory_mb is None:
        raise BookingError("shared reservations must declare expected memory")
    if mode == MODE_EXCLUSIVE:
        if share_units is not None:
            raise BookingError("share units apply only to shared reservations")
        normalized_share_units = config.max_shared_users
    else:
        try:
            normalized_share_units = normalize_share_units(share_units, config.max_shared_users)
        except (TypeError, ValueError) as exc:
            raise BookingError(str(exc)) from exc
    if not allow_queue and not is_slot_aligned(start_at, config.slot_minutes):
        raise BookingError(
            f"exact start time must align to a {slot_phrase(config.slot_minutes)} boundary"
        )
    return normalized_share_units


def _slot_dict(slot, duration: timedelta) -> Optional[dict]:
    if slot is None:
        return None
    start, gpus = slot
    return {"gpus": list(gpus), "start_at": to_iso(start), "end_at": to_iso(start + duration)}


def _reservation_datetime(reservation: dict, key: str) -> datetime:
    value = reservation.get(key)
    if not isinstance(value, str):
        raise BookingError(f"reservation has invalid {key}")
    return parse_iso(value)


def _optional_int(value: object) -> Optional[int]:
    return int(value) if value is not None else None


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
