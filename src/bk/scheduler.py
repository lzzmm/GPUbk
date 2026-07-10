from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

from .config import Config
from .models import (
    MODE_EXCLUSIVE,
    MODE_SHARED,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    Actor,
    BookingError,
    BookingRequest,
    BookingResult,
    EditRequest,
)
from .storage import LedgerStore
from .timeparse import parse_iso, to_iso, utc_now


BOOKING_GRANULARITY_SECONDS = 5 * 60


def add_booking(store: LedgerStore, config: Config, request: BookingRequest) -> BookingResult:
    if request.mode not in {MODE_SHARED, MODE_EXCLUSIVE}:
        raise BookingError(f"unsupported booking mode: {request.mode}")
    if request.count < 1:
        raise BookingError("GPU count must be >= 1")
    if request.duration_seconds <= 0:
        raise BookingError("duration must be positive")
    _validate_duration_granularity(request.duration_seconds)

    def mutate(ledger: dict):
        now = utc_now()
        changed = _expire_old_reservations(ledger, now)
        start = _normalize_start(request.start_at, request.allow_queue)
        duration = timedelta(seconds=request.duration_seconds)
        end = start + duration
        preferred = _normalize_preferred_gpus(request.preferred_gpus)

        if preferred is not None:
            if len(preferred) != request.count:
                raise BookingError("--gpu count must match requested GPU count")
            for gpu in preferred:
                _validate_gpu_index(config, gpu)
            if not request.allow_queue:
                duplicate = _find_exact_duplicate(
                    ledger,
                    request.actor.uid,
                    preferred,
                    start,
                    end,
                    request.mode,
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
            "op_id": request.op_id or str(uuid.uuid4()),
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
        ledger["reservations"].append(reservation)
        log = _log_item(request.actor, "add", reservation, "ok", "queued" if queued else "created")
        return ledger, BookingResult(reservation, True, "queued" if queued else "created", queued), [log], True

    return store.transaction(mutate)


def cancel_booking(store: LedgerStore, reservation_id: str, actor: Actor) -> dict:
    def mutate(ledger: dict):
        now = utc_now()
        changed = _expire_old_reservations(ledger, now)
        for reservation in ledger["reservations"]:
            if reservation.get("id") != reservation_id:
                continue
            if reservation.get("status") != STATUS_ACTIVE:
                raise BookingError("reservation is not active")
            if int(reservation.get("uid")) != actor.uid:
                raise BookingError("permission denied: reservation belongs to another UID")
            reservation["status"] = STATUS_CANCELLED
            reservation["updated_at"] = to_iso(now)
            log = _log_item(actor, "cancel", reservation, "ok", "cancelled")
            return ledger, reservation, [log], True
        raise BookingError("reservation not found")

    return store.transaction(mutate)


def edit_booking(store: LedgerStore, config: Config, request: EditRequest) -> BookingResult:
    def mutate(ledger: dict):
        now = utc_now()
        changed = _expire_old_reservations(ledger, now)
        reservation = _find_reservation(ledger, request.reservation_id)
        if reservation is None:
            raise BookingError("reservation not found")
        if reservation.get("status") != STATUS_ACTIVE:
            raise BookingError("reservation is not active")
        if int(reservation.get("uid")) != request.actor.uid:
            raise BookingError("permission denied: reservation belongs to another UID")

        mode = request.mode or reservation.get("mode", MODE_SHARED)
        if mode not in {MODE_SHARED, MODE_EXCLUSIVE}:
            raise BookingError(f"unsupported booking mode: {mode}")

        current_start = parse_iso(reservation["start_at"])
        current_end = parse_iso(reservation["end_at"])
        start = (request.start_at or current_start).astimezone(timezone.utc).replace(microsecond=0)
        duration_seconds = request.duration_seconds or int((current_end - current_start).total_seconds())
        if duration_seconds <= 0:
            raise BookingError("duration must be positive")
        _validate_duration_granularity(duration_seconds)
        start = _normalize_start(start, request.allow_queue)
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
        )
        if slot is None:
            reason_request = BookingRequest(
                actor=request.actor,
                count=count,
                duration_seconds=duration_seconds,
                start_at=start,
                mode=mode,
                preferred_gpus=list(preferred) if preferred is not None else None,
                allow_queue=request.allow_queue,
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
        reservation["updated_at"] = to_iso(now)
        log = _log_item(request.actor, "edit", reservation, "ok", "queued" if queued else "updated")
        return ledger, BookingResult(reservation, True, "queued" if queued else "updated", queued), [log], True

    return store.transaction(mutate)


def list_active(ledger: dict, now: Optional[datetime] = None) -> List[dict]:
    now = now or utc_now()
    active = []
    for reservation in ledger.get("reservations", []):
        if reservation.get("status") != STATUS_ACTIVE:
            continue
        if parse_iso(reservation["end_at"]) <= now:
            continue
        active.append(reservation)
    return sorted(active, key=_reservation_sort_key)


def find_available_gpus(
    ledger: dict,
    config: Config,
    count: int,
    start: datetime,
    end: datetime,
    mode: str,
    uid: int,
) -> List[int]:
    gpus, _reason = find_available_gpus_with_reason(ledger, config, count, start, end, mode, uid)
    return gpus


def find_available_gpus_with_reason(
    ledger: dict,
    config: Config,
    count: int,
    start: datetime,
    end: datetime,
    mode: str,
    uid: int,
) -> Tuple[List[int], str]:
    result = []
    reasons = []
    for gpu in range(config.gpu_count):
        ok, reason = availability_detail(ledger, gpu, start, end, mode, uid, config.max_shared_users)
        if ok:
            result.append(gpu)
            if len(result) == count:
                return result, ""
        else:
            reasons.append(reason)
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
) -> Optional[Tuple[datetime, List[int]]]:
    search_until = earliest_start + timedelta(hours=config.queue_search_hours)
    candidate_starts = _candidate_starts(ledger, earliest_start, search_until)
    if not allow_queue:
        candidate_starts = [earliest_start]

    for candidate_start in candidate_starts:
        candidate_end = candidate_start + duration
        if preferred_gpus is not None:
            reasons = []
            for gpu in preferred_gpus:
                ok, reason = availability_detail(
                    ledger,
                    gpu,
                    candidate_start,
                    candidate_end,
                    mode,
                    uid,
                    config.max_shared_users,
                )
                if not ok:
                    reasons.append(reason)
            if not reasons:
                return candidate_start, list(preferred_gpus)
            continue

        gpus, _reason = find_available_gpus_with_reason(
            ledger,
            config,
            count,
            candidate_start,
            candidate_end,
            mode,
            uid,
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
) -> Tuple[bool, str]:
    relevant = [
        item
        for item in list_active(ledger, start)
        if gpu in item.get("gpus", []) and _overlaps(start, end, parse_iso(item["start_at"]), parse_iso(item["end_at"]))
    ]
    if mode == MODE_EXCLUSIVE:
        if relevant:
            if any(item.get("mode") == MODE_EXCLUSIVE for item in relevant):
                return False, f"exclusive conflict on GPU {gpu}"
            return False, f"GPU {gpu} already has shared reservations"
        return True, ""
    if mode != MODE_SHARED:
        return False, f"unsupported booking mode: {mode}"
    if any(item.get("mode") == MODE_EXCLUSIVE for item in relevant):
        return False, f"exclusive conflict on GPU {gpu}"

    points = {start, end}
    for item in relevant:
        points.add(max(start, parse_iso(item["start_at"])))
        points.add(min(end, parse_iso(item["end_at"])))
    ordered = sorted(points)
    for left, right in zip(ordered, ordered[1:]):
        if left >= right:
            continue
        segment_count = _shared_record_count_in_segment(relevant, left, right)
        if segment_count + 1 > max_shared_users:
            return False, f"shared capacity full on GPU {gpu}"
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


def _shared_record_count_in_segment(reservations: Sequence[dict], start: datetime, end: datetime) -> int:
    count = 0
    for item in reservations:
        if item.get("mode") != MODE_SHARED:
            continue
        if _overlaps(start, end, parse_iso(item["start_at"]), parse_iso(item["end_at"])):
            count += 1
    return count


def _find_exact_duplicate(
    ledger: dict,
    uid: int,
    gpus: Sequence[int],
    start: datetime,
    end: datetime,
    mode: str,
) -> Optional[dict]:
    normalized_gpus = sorted(gpus)
    for item in list_active(ledger, start):
        if int(item.get("uid")) != uid:
            continue
        if item.get("mode") != mode:
            continue
        if sorted(item.get("gpus", [])) != normalized_gpus:
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
) -> Optional[dict]:
    for item in list_active(ledger, start):
        if int(item.get("uid")) != uid:
            continue
        if item.get("mode") != mode:
            continue
        if len(item.get("gpus", [])) != count:
            continue
        if parse_iso(item["start_at"]) == start and parse_iso(item["end_at"]) == end:
            return item
    return None


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
            changed = True
    return changed


def _normalize_preferred_gpus(gpus: Optional[Sequence[int]]) -> Optional[List[int]]:
    if gpus is None:
        return None
    normalized = sorted(set(int(gpu) for gpu in gpus))
    return normalized


def _validate_gpu_index(config: Config, gpu: int) -> None:
    if gpu < 0 or gpu >= config.gpu_count:
        raise BookingError(f"GPU index out of range: {gpu}")


def _overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def _candidate_starts(ledger: dict, earliest_start: datetime, search_until: datetime) -> List[datetime]:
    candidates = {_ceil_to_granularity(earliest_start)}
    for reservation in list_active(ledger, utc_now()):
        end = _ceil_to_granularity(parse_iso(reservation["end_at"]))
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
            ok, reason = availability_detail(ledger, gpu, start, end, request.mode, request.actor.uid, config.max_shared_users)
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


def _reservation_sort_key(reservation: dict) -> Tuple[datetime, datetime, str]:
    return (
        parse_iso(reservation["start_at"]),
        parse_iso(reservation["end_at"]),
        str(reservation.get("id", "")),
    )


def _log_item(actor: Actor, action: str, reservation: dict, result: str, message: str) -> dict:
    return {
        "ts": to_iso(utc_now()),
        "uid": actor.uid,
        "username": actor.username,
        "action": action,
        "reservation_id": reservation.get("id"),
        "op_id": reservation.get("op_id"),
        "gpus": reservation.get("gpus", []),
        "mode": reservation.get("mode"),
        "start_at": reservation.get("start_at"),
        "end_at": reservation.get("end_at"),
        "result": result,
        "message": message,
    }
