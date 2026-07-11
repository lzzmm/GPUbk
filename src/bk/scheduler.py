from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .config import Config
from .models import (
    MODE_EXCLUSIVE,
    MODE_SHARED,
    JOB_CANCELLED,
    JOB_CLAIMED,
    JOB_MISSED,
    JOB_PENDING,
    JOB_RUNNING,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    Actor,
    BookingError,
    BookingRequest,
    BookingResult,
    EditRequest,
)
from .policy import BOOKING_GRANULARITY_SECONDS, bind_ledger_policy, validate_ledger_policy
from .schedule_index import ReservationIndex, ReservationSpan
from .storage import LedgerStore
from .timeparse import parse_iso, to_iso, utc_now


MAX_EDIT_OPERATIONS_PER_RESERVATION = 256


def add_booking(store: LedgerStore, config: Config, request: BookingRequest) -> BookingResult:
    if request.mode not in {MODE_SHARED, MODE_EXCLUSIVE}:
        raise BookingError(f"unsupported booking mode: {request.mode}")
    if request.count < 1:
        raise BookingError("GPU count must be >= 1")
    if request.duration_seconds <= 0:
        raise BookingError("duration must be positive")
    _validate_duration_granularity(request.duration_seconds)
    job_metadata = _normalize_job_metadata(request.job_spec_id, request.job_digest, request.job_summary)
    op_id = _normalize_operation_id(request.op_id)
    expected_memory_mb = _normalize_expected_memory(request.expected_memory_mb)
    if request.mode == MODE_SHARED and config.require_shared_memory and expected_memory_mb is None:
        raise BookingError("shared reservations must declare expected memory with --mem")
    memory_capacities = _normalize_memory_capacities(request.gpu_memory_capacity_mb, config)

    def mutate(ledger: dict):
        now = utc_now()
        changed = bind_ledger_policy(ledger, config)
        changed = _maintain_ledger(ledger, now, config.ledger_retention_days) or changed
        start = _normalize_start(request.start_at, request.allow_queue)
        duration = timedelta(seconds=request.duration_seconds)
        end = start + duration
        preferred = _normalize_preferred_gpus(request.preferred_gpus)
        gpu_order = _normalize_gpu_order(request.gpu_order, config)
        gpu_scores = _normalize_gpu_scores(request.gpu_scores, config)
        if preferred is not None:
            if len(preferred) != request.count:
                raise BookingError("--gpu count must match requested GPU count")
            for gpu in preferred:
                _validate_gpu_index(config, gpu)

        operation_signature = _create_operation_signature(
            request,
            start,
            preferred,
            expected_memory_mb,
            job_metadata,
        )
        if op_id:
            applied = _find_applied_operation(ledger, request.actor.uid, op_id)
            if applied is not None:
                action, existing, existing_signature = applied
                if action != "create" or (
                    existing_signature is not None and existing_signature != operation_signature
                ):
                    raise BookingError("operation ID was already used for a different write")
                return ledger, BookingResult(existing, False, "operation already applied"), [], changed

        if preferred is not None:
            if not request.allow_queue:
                duplicate = _find_exact_duplicate(
                    ledger,
                    request.actor.uid,
                    preferred,
                    start,
                    end,
                    request.mode,
                    expected_memory_mb,
                    job_metadata,
                )
                if duplicate is not None:
                    return ledger, BookingResult(duplicate, False, "duplicate request ignored"), [], changed
        else:
            if not request.allow_queue:
                duplicate = _find_auto_duplicate(
                    ledger,
                    request.actor.uid,
                    request.count,
                    start,
                    end,
                    request.mode,
                    expected_memory_mb,
                    job_metadata,
                )
                if duplicate is not None:
                    return ledger, BookingResult(duplicate, False, "duplicate request ignored"), [], changed

        slot = find_earliest_slot(
            ledger,
            config,
            request.count,
            start,
            duration,
            request.mode,
            request.actor.uid,
            preferred,
            request.allow_queue,
            gpu_order,
            gpu_scores,
            expected_memory_mb,
            memory_capacities,
        )
        if slot is None:
            reason = _availability_failure_message(ledger, config, request, start, end, preferred)
            raise BookingError(reason)
        scheduled_start, gpus = slot
        scheduled_end = scheduled_start + duration
        queued = scheduled_start > start

        if not request.allow_queue and scheduled_start != start:
            raise BookingError("internal scheduler error: exact request moved unexpectedly")

        reservation = {
            "id": str(uuid.uuid4()),
            "op_id": op_id or str(uuid.uuid4()),
            "uid": request.actor.uid,
            "username": request.actor.username,
            "gpus": gpus,
            "mode": request.mode,
            "start_at": to_iso(scheduled_start),
            "end_at": to_iso(scheduled_end),
            "status": STATUS_ACTIVE,
            "created_at": to_iso(now),
            "updated_at": to_iso(now),
        }
        if expected_memory_mb is not None:
            reservation["expected_memory_mb"] = expected_memory_mb
        if op_id is not None:
            reservation["operation_signature"] = operation_signature
        if job_metadata is not None:
            reservation["job"] = {
                **job_metadata,
                "status": JOB_PENDING,
                "submitted_at": to_iso(now),
                "claimed_at": None,
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "runner_pid": None,
                "runner_host": None,
                "log_path": None,
            }
        ledger["reservations"].append(reservation)
        log = _log_item(request.actor, "add", reservation, "ok", "queued" if queued else "created")
        return ledger, BookingResult(reservation, True, "queued" if queued else "created", queued), [log], True

    return store.transaction(mutate)


def cancel_booking(store: LedgerStore, reservation_id: str, actor: Actor) -> dict:
    def mutate(ledger: dict):
        now = utc_now()
        _expire_old_reservations(ledger, now)
        for reservation in ledger["reservations"]:
            if reservation.get("id") != reservation_id:
                continue
            if reservation.get("status") != STATUS_ACTIVE:
                raise BookingError("reservation is not active")
            if int(reservation.get("uid")) != actor.uid:
                raise BookingError("permission denied: reservation belongs to another UID")
            reservation["status"] = STATUS_CANCELLED
            reservation["updated_at"] = to_iso(now)
            job = reservation.get("job")
            if isinstance(job, dict):
                if job.get("status") in {JOB_PENDING, JOB_CLAIMED}:
                    job["status"] = JOB_CANCELLED
                    job["finished_at"] = to_iso(now)
                elif job.get("status") == JOB_RUNNING:
                    job["cancel_requested_at"] = to_iso(now)
            log = _log_item(actor, "cancel", reservation, "ok", "cancelled")
            return ledger, reservation, [log], True
        raise BookingError("reservation not found")

    return store.transaction(mutate)


def edit_booking(store: LedgerStore, config: Config, request: EditRequest) -> BookingResult:
    op_id = _normalize_operation_id(request.op_id)
    operation_signature = _edit_operation_signature(request)

    def mutate(ledger: dict):
        now = utc_now()
        changed = bind_ledger_policy(ledger, config)
        changed = _maintain_ledger(ledger, now, config.ledger_retention_days) or changed
        if op_id:
            applied = _find_applied_operation(ledger, request.actor.uid, op_id)
            if applied is not None:
                action, existing, existing_signature = applied
                if (
                    action != "edit"
                    or existing.get("id") != request.reservation_id
                    or existing_signature != operation_signature
                ):
                    raise BookingError("operation ID was already used for a different write")
                return ledger, BookingResult(existing, False, "operation already applied"), [], changed

        reservation = _find_reservation(ledger, request.reservation_id)
        if reservation is None:
            raise BookingError("reservation not found")
        if reservation.get("status") != STATUS_ACTIVE:
            raise BookingError("reservation is not active")
        if int(reservation.get("uid")) != request.actor.uid:
            raise BookingError("permission denied: reservation belongs to another UID")
        job = reservation.get("job")
        if isinstance(job, dict) and job.get("status") != JOB_PENDING:
            raise BookingError(f"cannot edit reservation after job entered {job.get('status')} state")

        mode = request.mode or reservation.get("mode", MODE_SHARED)
        if mode not in {MODE_SHARED, MODE_EXCLUSIVE}:
            raise BookingError(f"unsupported booking mode: {mode}")
        expected_memory_mb = _normalize_expected_memory(
            request.expected_memory_mb
            if request.update_expected_memory
            else reservation.get("expected_memory_mb")
        )
        if mode == MODE_SHARED and config.require_shared_memory and expected_memory_mb is None:
            raise BookingError("shared reservations must declare expected memory with --mem")
        memory_capacities = _normalize_memory_capacities(request.gpu_memory_capacity_mb, config)

        current_start = parse_iso(reservation["start_at"])
        current_end = parse_iso(reservation["end_at"])
        if current_start <= now:
            raise BookingError("cannot edit a reservation after it has started")
        start = (request.start_at or current_start).astimezone(timezone.utc).replace(microsecond=0)
        duration_seconds = request.duration_seconds or int((current_end - current_start).total_seconds())
        if duration_seconds <= 0:
            raise BookingError("duration must be positive")
        _validate_duration_granularity(duration_seconds)
        start = _normalize_start(start, request.allow_queue)
        earliest_start = _ceil_to_granularity(now)
        if start < earliest_start:
            if request.allow_queue:
                start = earliest_start
            else:
                raise BookingError("edit start must not be in the past")
        duration = timedelta(seconds=duration_seconds)
        end = start + duration

        preferred = _normalize_preferred_gpus(request.preferred_gpus) if request.preferred_gpus is not None else None
        if preferred is None and request.count is None:
            preferred = _normalize_preferred_gpus(reservation.get("gpus", []))
        count = request.count or (len(preferred) if preferred is not None else len(reservation.get("gpus", [])))
        if count < 1:
            raise BookingError("GPU count must be >= 1")
        if preferred is not None:
            if len(preferred) != count:
                raise BookingError("--gpu count must match requested GPU count")
            for gpu in preferred:
                _validate_gpu_index(config, gpu)

        shadow_ledger = {
            **ledger,
            "reservations": [item for item in ledger.get("reservations", []) if item.get("id") != reservation.get("id")],
        }
        gpu_order = _normalize_gpu_order(request.gpu_order, config)
        gpu_scores = _normalize_gpu_scores(request.gpu_scores, config)
        slot = find_earliest_slot(
            shadow_ledger,
            config,
            count,
            start,
            duration,
            mode,
            request.actor.uid,
            preferred,
            request.allow_queue,
            gpu_order,
            gpu_scores,
            expected_memory_mb,
            memory_capacities,
        )
        if slot is None:
            reason_request = BookingRequest(
                actor=request.actor,
                count=count,
                duration_seconds=duration_seconds,
                start_at=start,
                mode=mode,
                preferred_gpus=list(preferred) if preferred is not None else None,
                gpu_order=gpu_order,
                gpu_scores=gpu_scores,
                allow_queue=request.allow_queue,
                expected_memory_mb=expected_memory_mb,
                gpu_memory_capacity_mb=memory_capacities,
            )
            reason = _availability_failure_message(shadow_ledger, config, reason_request, start, end, preferred)
            raise BookingError(reason)

        scheduled_start, gpus = slot
        scheduled_end = scheduled_start + duration
        queued = scheduled_start > start
        if not request.allow_queue and scheduled_start != start:
            raise BookingError("internal scheduler error: exact edit moved unexpectedly")

        reservation["gpus"] = gpus
        reservation["mode"] = mode
        reservation["start_at"] = to_iso(scheduled_start)
        reservation["end_at"] = to_iso(scheduled_end)
        if expected_memory_mb is None:
            reservation.pop("expected_memory_mb", None)
        else:
            reservation["expected_memory_mb"] = expected_memory_mb
        if op_id:
            history = reservation.get("edit_operations", [])
            if not isinstance(history, list):
                raise BookingError("invalid edit operation history")
            if len(history) >= MAX_EDIT_OPERATIONS_PER_RESERVATION:
                raise BookingError(
                    "reservation reached the idempotent edit limit; recreate it before further Agent edits"
                )
            reservation["edit_operations"] = [
                *history,
                {"op_id": op_id, "signature": operation_signature},
            ]
        reservation["updated_at"] = to_iso(now)
        log = _log_item(
            request.actor,
            "edit",
            reservation,
            "ok",
            "queued" if queued else "updated",
            operation_id=op_id,
        )
        return ledger, BookingResult(reservation, True, "queued" if queued else "updated", queued), [log], True

    return store.transaction(mutate)


def list_active(ledger: dict, now: Optional[datetime] = None) -> List[dict]:
    return ReservationIndex.from_ledger(ledger, now or utc_now()).records()


def find_available_gpus(
    ledger: dict,
    config: Config,
    count: int,
    start: datetime,
    end: datetime,
    mode: str,
    uid: int,
    gpu_order: Optional[Sequence[int]] = None,
    gpu_scores: Optional[Dict[int, float]] = None,
    expected_memory_mb: Optional[int] = None,
    gpu_memory_capacity_mb: Optional[Dict[int, int]] = None,
) -> List[int]:
    gpus, _reason = find_available_gpus_with_reason(
        ledger,
        config,
        count,
        start,
        end,
        mode,
        uid,
        gpu_order,
        gpu_scores,
        expected_memory_mb,
        gpu_memory_capacity_mb,
    )
    return gpus


def find_available_gpus_with_reason(
    ledger: dict,
    config: Config,
    count: int,
    start: datetime,
    end: datetime,
    mode: str,
    uid: int,
    gpu_order: Optional[Sequence[int]] = None,
    gpu_scores: Optional[Dict[int, float]] = None,
    expected_memory_mb: Optional[int] = None,
    gpu_memory_capacity_mb: Optional[Dict[int, int]] = None,
) -> Tuple[List[int], str]:
    validate_ledger_policy(ledger, config)
    index = ReservationIndex.from_ledger(ledger, start)
    return _find_available_gpus_with_reason(
        index,
        config,
        count,
        start,
        end,
        mode,
        gpu_order,
        gpu_scores,
        expected_memory_mb,
        gpu_memory_capacity_mb,
    )


def _find_available_gpus_with_reason(
    index: ReservationIndex,
    config: Config,
    count: int,
    start: datetime,
    end: datetime,
    mode: str,
    gpu_order: Optional[Sequence[int]] = None,
    gpu_scores: Optional[Dict[int, float]] = None,
    expected_memory_mb: Optional[int] = None,
    gpu_memory_capacity_mb: Optional[Dict[int, int]] = None,
) -> Tuple[List[int], str]:
    available = []
    reasons = []
    order = _normalize_gpu_order(gpu_order, config)
    base_rank = {gpu: rank for rank, gpu in enumerate(order)}
    for gpu in order:
        ok, reason = _availability_detail_indexed(
            index,
            gpu,
            start,
            end,
            mode,
            config.max_shared_users,
            expected_memory_mb,
            gpu_memory_capacity_mb,
            config.shared_memory_reserve_mb,
        )
        if ok:
            pressure = _reservation_pressure_score_indexed(index, gpu, start, end, config.max_shared_users)
            score = float((gpu_scores or {}).get(gpu, 0.0)) + pressure
            available.append((score, base_rank[gpu], gpu))
        else:
            reasons.append(reason)
    available.sort()
    result = [gpu for _score, _rank, gpu in available[:count]]
    if len(result) == count:
        return result, ""
    return result, _combine_reasons(reasons)


def find_earliest_slot(
    ledger: dict,
    config: Config,
    count: int,
    earliest_start: datetime,
    duration: timedelta,
    mode: str,
    uid: int,
    preferred_gpus: Optional[Sequence[int]] = None,
    allow_queue: bool = False,
    gpu_order: Optional[Sequence[int]] = None,
    gpu_scores: Optional[Dict[int, float]] = None,
    expected_memory_mb: Optional[int] = None,
    gpu_memory_capacity_mb: Optional[Dict[int, int]] = None,
) -> Optional[Tuple[datetime, List[int]]]:
    validate_ledger_policy(ledger, config)
    search_until = earliest_start + timedelta(hours=config.queue_search_hours)
    now = utc_now()
    index = ReservationIndex.from_ledger(ledger, min(earliest_start, now))
    candidate_starts = _candidate_starts_from_index(index, earliest_start, search_until, now)
    if not allow_queue:
        candidate_starts = [earliest_start]

    for candidate_start in candidate_starts:
        candidate_end = candidate_start + duration
        if preferred_gpus is not None:
            reasons = []
            for gpu in preferred_gpus:
                ok, reason = _availability_detail_indexed(
                    index,
                    gpu,
                    candidate_start,
                    candidate_end,
                    mode,
                    config.max_shared_users,
                    expected_memory_mb,
                    gpu_memory_capacity_mb,
                    config.shared_memory_reserve_mb,
                )
                if not ok:
                    reasons.append(reason)
            if not reasons:
                return candidate_start, list(preferred_gpus)
            continue

        gpus, _reason = _find_available_gpus_with_reason(
            index,
            config,
            count,
            candidate_start,
            candidate_end,
            mode,
            gpu_order,
            gpu_scores,
            expected_memory_mb,
            gpu_memory_capacity_mb,
        )
        if len(gpus) == count:
            return candidate_start, gpus
    return None


def can_place_gpu(
    ledger: dict,
    gpu: int,
    start: datetime,
    end: datetime,
    mode: str,
    uid: int,
    max_shared_users: int,
) -> bool:
    ok, _reason = availability_detail(ledger, gpu, start, end, mode, uid, max_shared_users)
    return ok


def availability_detail(
    ledger: dict,
    gpu: int,
    start: datetime,
    end: datetime,
    mode: str,
    uid: int,
    max_shared_users: int,
    expected_memory_mb: Optional[int] = None,
    gpu_memory_capacity_mb: Optional[Dict[int, int]] = None,
    shared_memory_reserve_mb: int = 0,
) -> Tuple[bool, str]:
    index = ReservationIndex.from_ledger(ledger, start)
    return _availability_detail_indexed(
        index,
        gpu,
        start,
        end,
        mode,
        max_shared_users,
        expected_memory_mb,
        gpu_memory_capacity_mb,
        shared_memory_reserve_mb,
    )


def _availability_detail_indexed(
    index: ReservationIndex,
    gpu: int,
    start: datetime,
    end: datetime,
    mode: str,
    max_shared_users: int,
    expected_memory_mb: Optional[int] = None,
    gpu_memory_capacity_mb: Optional[Dict[int, int]] = None,
    shared_memory_reserve_mb: int = 0,
) -> Tuple[bool, str]:
    relevant = index.overlapping(gpu, start, end)
    if mode == MODE_EXCLUSIVE:
        if relevant:
            if any(item.mode == MODE_EXCLUSIVE for item in relevant):
                return False, f"exclusive conflict on GPU {gpu}"
            return False, f"GPU {gpu} already has shared reservations"
        return True, ""
    if mode != MODE_SHARED:
        return False, f"unsupported booking mode: {mode}"
    if any(item.mode == MODE_EXCLUSIVE for item in relevant):
        return False, f"exclusive conflict on GPU {gpu}"

    points = {start, end}
    for item in relevant:
        points.add(max(start, item.start))
        points.add(min(end, item.end))
    ordered = sorted(points)
    for left, right in zip(ordered, ordered[1:]):
        if left >= right:
            continue
        segment_count = _shared_record_count_in_spans(relevant, left, right)
        if segment_count + 1 > max_shared_users:
            return False, f"shared capacity full on GPU {gpu}"
        capacity = (gpu_memory_capacity_mb or {}).get(gpu)
        if capacity:
            usable = max(0, capacity - shared_memory_reserve_mb)
            requested = expected_memory_mb or max(1, usable // max_shared_users)
            committed = _shared_memory_in_spans(relevant, left, right, usable, max_shared_users)
            if requested > usable or committed + requested > usable:
                return (
                    False,
                    f"shared memory full on GPU {gpu} "
                    f"(need {requested}MiB, projected {committed + requested}/{usable}MiB)",
                )
    return True, ""


def shared_record_count_for_gpu(reservations: Sequence[dict], gpu: int, start: datetime, end: datetime) -> int:
    return _shared_record_count_in_segment(
        [
            item
            for item in reservations
            if gpu in item.get("gpus", [])
        ],
        start,
        end,
    )


def max_shared_record_count_for_reservation(reservations: Sequence[dict], reservation: dict) -> int:
    if reservation.get("mode") != MODE_SHARED:
        return 0
    start = parse_iso(reservation["start_at"])
    end = parse_iso(reservation["end_at"])
    points = {start, end}
    for item in reservations:
        if item.get("mode") != MODE_SHARED:
            continue
        if not set(item.get("gpus", [])) & set(reservation.get("gpus", [])):
            continue
        item_start = parse_iso(item["start_at"])
        item_end = parse_iso(item["end_at"])
        if not _overlaps(start, end, item_start, item_end):
            continue
        points.add(max(start, item_start))
        points.add(min(end, item_end))

    peak = 0
    ordered = sorted(points)
    for left, right in zip(ordered, ordered[1:]):
        if left >= right:
            continue
        for gpu in reservation.get("gpus", []):
            peak = max(peak, shared_record_count_for_gpu(reservations, gpu, left, right))
    return peak


def shared_memory_headroom_for_reservation(
    reservations: Sequence[dict],
    reservation: dict,
    gpu_memory_capacity_mb: Dict[int, int],
    max_shared_users: int,
    shared_memory_reserve_mb: int = 0,
) -> Dict[int, int]:
    if reservation.get("mode") != MODE_SHARED:
        return {}
    start = parse_iso(reservation["start_at"])
    end = parse_iso(reservation["end_at"])
    result: Dict[int, int] = {}
    for gpu in reservation.get("gpus", []):
        capacity = gpu_memory_capacity_mb.get(int(gpu))
        if not capacity:
            continue
        usable = max(0, capacity - shared_memory_reserve_mb)
        relevant = [
            item
            for item in reservations
            if item.get("mode") == MODE_SHARED
            and gpu in item.get("gpus", [])
            and _overlaps(start, end, parse_iso(item["start_at"]), parse_iso(item["end_at"]))
        ]
        points = {start, end}
        for item in relevant:
            points.add(max(start, parse_iso(item["start_at"])))
            points.add(min(end, parse_iso(item["end_at"])))
        peak = 0
        ordered = sorted(points)
        for left, right in zip(ordered, ordered[1:]):
            if left < right:
                peak = max(
                    peak,
                    _shared_memory_in_segment(relevant, left, right, usable, max_shared_users),
                )
        result[int(gpu)] = max(0, usable - peak)
    return result


def _shared_record_count_in_segment(reservations: Sequence[dict], start: datetime, end: datetime) -> int:
    count = 0
    for item in reservations:
        if item.get("mode") != MODE_SHARED:
            continue
        if _overlaps(start, end, parse_iso(item["start_at"]), parse_iso(item["end_at"])):
            count += 1
    return count


def _shared_memory_in_segment(
    reservations: Sequence[dict],
    start: datetime,
    end: datetime,
    usable_memory_mb: int,
    max_shared_users: int,
) -> int:
    default_memory = max(1, usable_memory_mb // max_shared_users)
    total = 0
    for item in reservations:
        if item.get("mode") != MODE_SHARED:
            continue
        if not _overlaps(start, end, parse_iso(item["start_at"]), parse_iso(item["end_at"])):
            continue
        try:
            expected = int(item.get("expected_memory_mb") or default_memory)
        except (TypeError, ValueError):
            expected = default_memory
        total += max(1, expected)
    return total


def _shared_record_count_in_spans(
    reservations: Sequence[ReservationSpan],
    start: datetime,
    end: datetime,
) -> int:
    return sum(
        1
        for item in reservations
        if item.mode == MODE_SHARED and _overlaps(start, end, item.start, item.end)
    )


def _shared_memory_in_spans(
    reservations: Sequence[ReservationSpan],
    start: datetime,
    end: datetime,
    usable_memory_mb: int,
    max_shared_users: int,
) -> int:
    default_memory = max(1, usable_memory_mb // max_shared_users)
    total = 0
    for item in reservations:
        if item.mode != MODE_SHARED or not _overlaps(start, end, item.start, item.end):
            continue
        try:
            expected = int(item.record.get("expected_memory_mb") or default_memory)
        except (TypeError, ValueError):
            expected = default_memory
        total += max(1, expected)
    return total


def _find_exact_duplicate(
    ledger: dict,
    uid: int,
    gpus: Sequence[int],
    start: datetime,
    end: datetime,
    mode: str,
    expected_memory_mb: Optional[int],
    job_metadata: Optional[dict],
) -> Optional[dict]:
    normalized_gpus = sorted(gpus)
    for item in list_active(ledger, start):
        if int(item.get("uid")) != uid:
            continue
        if item.get("mode") != mode:
            continue
        if sorted(item.get("gpus", [])) != normalized_gpus:
            continue
        if not _same_request_metadata(item, expected_memory_mb, job_metadata):
            continue
        if parse_iso(item["start_at"]) == start and parse_iso(item["end_at"]) == end:
            return item
    return None


def _find_auto_duplicate(
    ledger: dict,
    uid: int,
    count: int,
    start: datetime,
    end: datetime,
    mode: str,
    expected_memory_mb: Optional[int],
    job_metadata: Optional[dict],
) -> Optional[dict]:
    for item in list_active(ledger, start):
        if int(item.get("uid")) != uid:
            continue
        if item.get("mode") != mode:
            continue
        if len(item.get("gpus", [])) != count:
            continue
        if not _same_request_metadata(item, expected_memory_mb, job_metadata):
            continue
        if parse_iso(item["start_at"]) == start and parse_iso(item["end_at"]) == end:
            return item
    return None


def _same_request_metadata(
    reservation: dict,
    expected_memory_mb: Optional[int],
    job_metadata: Optional[dict],
) -> bool:
    stored_memory = reservation.get("expected_memory_mb")
    try:
        normalized_stored_memory = int(stored_memory) if stored_memory is not None else None
    except (TypeError, ValueError):
        return False
    if normalized_stored_memory != expected_memory_mb:
        return False
    job = reservation.get("job")
    if job_metadata is None:
        return not isinstance(job, dict)
    if not isinstance(job, dict):
        return False
    return job.get("digest") == job_metadata.get("digest")


def _find_reservation(ledger: dict, reservation_id: str) -> Optional[dict]:
    for reservation in ledger.get("reservations", []):
        if reservation.get("id") == reservation_id:
            return reservation
    return None


def find_policy_violations(ledger: dict, max_shared_users: int, now: Optional[datetime] = None) -> List[dict]:
    active = list_active(ledger, now)
    issues: List[dict] = []
    issues.extend(_find_exclusive_overlap_violations(active))
    issues.extend(_find_shared_capacity_violations(active, max_shared_users))
    return sorted(issues, key=lambda item: (item.get("start_at", ""), item.get("gpu", -1), item.get("type", "")))


def _find_exclusive_overlap_violations(active: Sequence[dict]) -> List[dict]:
    issues: List[dict] = []
    for index, left in enumerate(active):
        for right in active[index + 1 :]:
            if left.get("mode") != MODE_EXCLUSIVE and right.get("mode") != MODE_EXCLUSIVE:
                continue
            overlap_gpus = sorted(set(left.get("gpus", [])) & set(right.get("gpus", [])))
            if not overlap_gpus:
                continue
            left_start = parse_iso(left["start_at"])
            left_end = parse_iso(left["end_at"])
            right_start = parse_iso(right["start_at"])
            right_end = parse_iso(right["end_at"])
            if not _overlaps(left_start, left_end, right_start, right_end):
                continue
            for gpu in overlap_gpus:
                issues.append(
                    {
                        "type": "exclusive-overlap",
                        "gpu": gpu,
                        "left_id": left.get("id"),
                        "right_id": right.get("id"),
                        "left_start_at": left.get("start_at"),
                        "left_end_at": left.get("end_at"),
                        "right_start_at": right.get("start_at"),
                        "right_end_at": right.get("end_at"),
                        "start_at": to_iso(max(left_start, right_start)),
                        "end_at": to_iso(min(left_end, right_end)),
                    }
                )
    return issues


def _find_shared_capacity_violations(active: Sequence[dict], max_shared_users: int) -> List[dict]:
    issues: List[dict] = []
    gpus = sorted({gpu for item in active for gpu in item.get("gpus", [])})
    for gpu in gpus:
        shared = [item for item in active if item.get("mode") == MODE_SHARED and gpu in item.get("gpus", [])]
        points = sorted({parse_iso(item["start_at"]) for item in shared} | {parse_iso(item["end_at"]) for item in shared})
        for left, right in zip(points, points[1:]):
            if left >= right:
                continue
            overlapping = [item for item in shared if _overlaps(left, right, parse_iso(item["start_at"]), parse_iso(item["end_at"]))]
            if len(overlapping) <= max_shared_users:
                continue
            issues.append(
                {
                    "type": "shared-capacity",
                    "gpu": gpu,
                    "count": len(overlapping),
                    "limit": max_shared_users,
                    "reservation_ids": [item.get("id") for item in overlapping],
                    "start_at": to_iso(left),
                    "end_at": to_iso(right),
                }
            )
    return issues


def _expire_old_reservations(ledger: dict, now: datetime) -> bool:
    changed = False
    for reservation in ledger.get("reservations", []):
        if reservation.get("status") == STATUS_ACTIVE and parse_iso(reservation["end_at"]) <= now:
            reservation["status"] = STATUS_EXPIRED
            reservation["updated_at"] = to_iso(now)
            job = reservation.get("job")
            if isinstance(job, dict) and job.get("status") in {JOB_PENDING, JOB_CLAIMED}:
                job["status"] = JOB_MISSED
                job["finished_at"] = to_iso(now)
            changed = True
    return changed


def _maintain_ledger(ledger: dict, now: datetime, retention_days: int) -> bool:
    changed = _expire_old_reservations(ledger, now)
    if retention_days <= 0:
        return changed
    cutoff = now - timedelta(days=retention_days)
    retained = []
    for reservation in ledger.get("reservations", []):
        status = reservation.get("status")
        if status == STATUS_EXPIRED:
            terminal_at = parse_iso(reservation["end_at"])
        elif status == STATUS_CANCELLED:
            terminal_at = parse_iso(reservation.get("updated_at") or reservation["end_at"])
        else:
            retained.append(reservation)
            continue
        if terminal_at > cutoff:
            retained.append(reservation)
        else:
            changed = True
    if len(retained) != len(ledger.get("reservations", [])):
        ledger["reservations"] = retained
    return changed


def _normalize_preferred_gpus(gpus: Optional[Sequence[int]]) -> Optional[List[int]]:
    if gpus is None:
        return None
    normalized = sorted(set(int(gpu) for gpu in gpus))
    return normalized


def _normalize_gpu_order(gpus: Optional[Sequence[int]], config: Config) -> List[int]:
    if gpus is None:
        return list(range(config.gpu_count))
    result = []
    seen = set()
    for value in gpus:
        gpu = int(value)
        _validate_gpu_index(config, gpu)
        if gpu not in seen:
            result.append(gpu)
            seen.add(gpu)
    result.extend(gpu for gpu in range(config.gpu_count) if gpu not in seen)
    return result


def _normalize_gpu_scores(scores: Optional[Dict[int, float]], config: Config) -> Dict[int, float]:
    if scores is None:
        return {}
    result = {}
    for raw_gpu, raw_score in scores.items():
        gpu = int(raw_gpu)
        _validate_gpu_index(config, gpu)
        score = float(raw_score)
        if not math.isfinite(score):
            raise BookingError(f"GPU score must be finite: {raw_score}")
        result[gpu] = score
    return result


def _normalize_memory_capacities(capacities: Optional[Dict[int, int]], config: Config) -> Dict[int, int]:
    if capacities is None:
        return {}
    result = {}
    for raw_gpu, raw_capacity in capacities.items():
        gpu = int(raw_gpu)
        _validate_gpu_index(config, gpu)
        capacity = int(raw_capacity)
        if capacity <= 0:
            continue
        result[gpu] = capacity
    return result


def _normalize_expected_memory(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    parsed = int(value)
    if parsed <= 0:
        raise BookingError("expected GPU memory must be positive")
    return parsed


def _normalize_job_metadata(
    spec_id: Optional[str],
    digest: Optional[str],
    summary: Optional[str],
) -> Optional[dict]:
    values = (spec_id, digest, summary)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise BookingError("job spec ID, digest, and summary must be provided together")
    try:
        normalized_spec_id = str(uuid.UUID(str(spec_id)))
    except (ValueError, AttributeError) as exc:
        raise BookingError("invalid job spec ID") from exc
    normalized_digest = str(digest).lower()
    if re.fullmatch(r"[0-9a-f]{64}", normalized_digest) is None:
        raise BookingError("invalid job spec digest")
    normalized_summary = str(summary).strip()
    if not normalized_summary or len(normalized_summary) > 200:
        raise BookingError("job summary must contain 1-200 characters")
    if any(ord(char) < 32 for char in normalized_summary):
        raise BookingError("job summary contains control characters")
    return {
        "spec_id": normalized_spec_id,
        "digest": normalized_digest,
        "summary": normalized_summary,
    }


def _normalize_operation_id(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized or len(normalized) > 128:
        raise BookingError("operation ID must contain 1-128 characters")
    if re.fullmatch(r"[A-Za-z0-9._:-]+", normalized) is None:
        raise BookingError("operation ID contains unsupported characters")
    return normalized


def _create_operation_signature(
    request: BookingRequest,
    start: datetime,
    preferred_gpus: Optional[Sequence[int]],
    expected_memory_mb: Optional[int],
    job_metadata: Optional[dict],
) -> str:
    return _operation_signature(
        "create",
        {
            "count": request.count,
            "duration_seconds": request.duration_seconds,
            "start_at": None if request.allow_queue else to_iso(start),
            "mode": request.mode,
            "preferred_gpus": list(preferred_gpus) if preferred_gpus is not None else None,
            "allow_queue": request.allow_queue,
            "expected_memory_mb": expected_memory_mb,
            "job_summary": job_metadata.get("summary") if job_metadata is not None else None,
        },
    )


def _edit_operation_signature(request: EditRequest) -> str:
    preferred = _normalize_preferred_gpus(request.preferred_gpus)
    return _operation_signature(
        "edit",
        {
            "reservation_id": request.reservation_id,
            "start_at": to_iso(request.start_at) if request.start_at is not None else None,
            "duration_seconds": request.duration_seconds,
            "mode": request.mode,
            "preferred_gpus": preferred,
            "count": request.count,
            "allow_queue": request.allow_queue,
            "expected_memory_mb": request.expected_memory_mb,
            "update_expected_memory": request.update_expected_memory,
        },
    )


def _operation_signature(kind: str, payload: dict) -> str:
    raw = json.dumps(
        {"kind": kind, **payload},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _find_applied_operation(
    ledger: dict,
    uid: int,
    operation_id: str,
) -> Optional[Tuple[str, dict, Optional[str]]]:
    for reservation in ledger.get("reservations", []):
        if int(reservation.get("uid", -1)) != uid:
            continue
        if reservation.get("op_id") == operation_id:
            signature = reservation.get("operation_signature")
            return "create", reservation, str(signature) if signature is not None else None
        history = reservation.get("edit_operations", [])
        if not isinstance(history, list):
            raise BookingError("invalid edit operation history")
        if len(history) > MAX_EDIT_OPERATIONS_PER_RESERVATION:
            raise BookingError("edit operation history exceeds the safety limit")
        for item in history:
            if not isinstance(item, dict):
                raise BookingError("invalid edit operation history")
            if item.get("op_id") == operation_id:
                signature = item.get("signature")
                if not isinstance(signature, str):
                    raise BookingError("invalid edit operation history")
                return "edit", reservation, signature
    return None


def _reservation_pressure_score_indexed(
    index: ReservationIndex,
    gpu: int,
    start: datetime,
    end: datetime,
    max_shared_users: int,
) -> float:
    duration = max(1.0, (end - start).total_seconds())
    overlap_total = 0.0
    for item in index.overlapping(gpu, start, end):
        if item.mode != MODE_SHARED:
            continue
        overlap = max(0.0, (min(end, item.end) - max(start, item.start)).total_seconds())
        overlap_total += overlap
    return round(100.0 * overlap_total / duration / max(1, max_shared_users), 3)


def reservation_pressure_score(
    ledger: dict,
    gpu: int,
    start: datetime,
    end: datetime,
    max_shared_users: int,
) -> float:
    return reservation_pressure_scores(ledger, [gpu], start, end, max_shared_users)[gpu]


def reservation_pressure_scores(
    ledger: dict,
    gpus: Sequence[int],
    start: datetime,
    end: datetime,
    max_shared_users: int,
) -> Dict[int, float]:
    index = ReservationIndex.from_ledger(ledger, start)
    return {
        gpu: _reservation_pressure_score_indexed(index, gpu, start, end, max_shared_users)
        for gpu in gpus
    }


def _validate_gpu_index(config: Config, gpu: int) -> None:
    if gpu < 0 or gpu >= config.gpu_count:
        raise BookingError(f"GPU index out of range: {gpu}")


def _overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def _candidate_starts(ledger: dict, earliest_start: datetime, search_until: datetime) -> List[datetime]:
    now = utc_now()
    index = ReservationIndex.from_ledger(ledger, min(earliest_start, now))
    return _candidate_starts_from_index(index, earliest_start, search_until, now)


def _candidate_starts_from_index(
    index: ReservationIndex,
    earliest_start: datetime,
    search_until: datetime,
    now: datetime,
) -> List[datetime]:
    candidates = {_ceil_to_granularity(earliest_start)}
    for reservation in index.spans:
        if reservation.end <= now:
            continue
        end = _ceil_to_granularity(reservation.end)
        if earliest_start <= end <= search_until:
            candidates.add(end)
    return sorted(candidates)


def _normalize_start(value: datetime, allow_queue: bool) -> datetime:
    start = value.astimezone(timezone.utc).replace(microsecond=0)
    if allow_queue:
        return _ceil_to_granularity(start)
    if not _is_granularity_aligned(start):
        raise BookingError("start time must align to a 5-minute boundary")
    return start


def _validate_duration_granularity(duration_seconds: int) -> None:
    if duration_seconds % BOOKING_GRANULARITY_SECONDS != 0:
        raise BookingError("duration must be a multiple of 5 minutes")


def _is_granularity_aligned(value: datetime) -> bool:
    return int(value.timestamp()) % BOOKING_GRANULARITY_SECONDS == 0


def _ceil_to_granularity(value: datetime) -> datetime:
    value = value.astimezone(timezone.utc).replace(microsecond=0)
    timestamp = int(value.timestamp())
    remainder = timestamp % BOOKING_GRANULARITY_SECONDS
    if remainder == 0:
        return value
    return datetime.fromtimestamp(timestamp + BOOKING_GRANULARITY_SECONDS - remainder, timezone.utc)


def _availability_failure_message(
    ledger: dict,
    config: Config,
    request: BookingRequest,
    start: datetime,
    end: datetime,
    preferred: Optional[Sequence[int]],
) -> str:
    if preferred is not None:
        reasons = []
        for gpu in preferred:
            ok, reason = availability_detail(
                ledger,
                gpu,
                start,
                end,
                request.mode,
                request.actor.uid,
                config.max_shared_users,
                request.expected_memory_mb,
                request.gpu_memory_capacity_mb,
                config.shared_memory_reserve_mb,
            )
            if not ok:
                reasons.append(reason)
        reason = _combine_reasons(reasons) or "GPU(s) unavailable for this time range"
    else:
        _gpus, reason = find_available_gpus_with_reason(
            ledger,
            config,
            request.count,
            start,
            end,
            request.mode,
            request.actor.uid,
            request.gpu_order,
            request.gpu_scores,
            request.expected_memory_mb,
            request.gpu_memory_capacity_mb,
        )
        reason = reason or "not enough GPUs available for this request"

    hint = _nearest_available_hint(ledger, config, request, start, preferred)
    if hint:
        return f"{reason}; {hint}"
    return f"{reason}; no available slot within next {config.queue_search_hours} hours"


def _nearest_available_hint(
    ledger: dict,
    config: Config,
    request: BookingRequest,
    start: datetime,
    preferred: Optional[Sequence[int]],
) -> str:
    slot = find_earliest_slot(
        ledger,
        config,
        request.count,
        start,
        timedelta(seconds=request.duration_seconds),
        request.mode,
        request.actor.uid,
        preferred,
        True,
        request.gpu_order,
        request.gpu_scores,
        request.expected_memory_mb,
        request.gpu_memory_capacity_mb,
    )
    if slot is None:
        return ""
    candidate_start, gpus = slot
    candidate_end = candidate_start + timedelta(seconds=request.duration_seconds)
    return f"nearest available: GPU={','.join(map(str, gpus))} {to_iso(candidate_start)} -> {to_iso(candidate_end)}"


def _combine_reasons(reasons: Sequence[str]) -> str:
    seen = []
    for reason in reasons:
        if reason and reason not in seen:
            seen.append(reason)
    return "; ".join(seen[:3])


def _log_item(
    actor: Actor,
    action: str,
    reservation: dict,
    result: str,
    message: str,
    *,
    operation_id: Optional[str] = None,
) -> dict:
    return {
        "ts": to_iso(utc_now()),
        "uid": actor.uid,
        "username": actor.username,
        "action": action,
        "reservation_id": reservation.get("id"),
        "op_id": operation_id or reservation.get("op_id"),
        "gpus": reservation.get("gpus", []),
        "mode": reservation.get("mode"),
        "start_at": reservation.get("start_at"),
        "end_at": reservation.get("end_at"),
        "result": result,
        "message": message,
    }
