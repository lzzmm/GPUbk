from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import MAX_GPU_COUNT, MAX_UID
from .models import (
    MODE_EXCLUSIVE,
    MODE_SHARED,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
)
from .timeparse import parse_iso


MAX_RESERVATION_ID_LENGTH = 128
MAX_USERNAME_LENGTH = 256
MAX_EDIT_OPERATIONS_PER_RESERVATION = 256


def validate_ledger_document(data: object) -> None:
    if not isinstance(data, dict):
        raise ValueError("ledger must be an object")
    version = data.get("version")
    if isinstance(version, bool) or version != 1:
        raise ValueError("unsupported ledger version")
    reservations = data.get("reservations")
    if not isinstance(reservations, list):
        raise ValueError("ledger reservations must be a list")
    transaction_id = data.get("last_transaction_id")
    if transaction_id is not None:
        _bounded_text(transaction_id, "last_transaction_id", 128)

    seen_ids: set[str] = set()
    seen_operations: set[tuple[int, str]] = set()
    for index, reservation in enumerate(reservations):
        _validate_reservation(reservation, index, seen_ids, seen_operations)


def _validate_reservation(
    reservation: object,
    index: int,
    seen_ids: set[str],
    seen_operations: set[tuple[int, str]],
) -> None:
    path = f"reservations[{index}]"
    if not isinstance(reservation, dict):
        raise ValueError(f"{path} must be an object")

    reservation_id = _bounded_text(
        reservation.get("id"),
        f"{path}.id",
        MAX_RESERVATION_ID_LENGTH,
    )
    if reservation_id in seen_ids:
        raise ValueError(f"{path}.id duplicates reservation {reservation_id!r}")
    seen_ids.add(reservation_id)

    uid = _uid(reservation.get("uid"), f"{path}.uid")
    username = reservation.get("username")
    if username is not None:
        _bounded_text(username, f"{path}.username", MAX_USERNAME_LENGTH)

    mode = reservation.get("mode")
    if mode not in {MODE_SHARED, MODE_EXCLUSIVE}:
        raise ValueError(f"{path}.mode must be shared or exclusive")
    status = reservation.get("status")
    if status not in {STATUS_ACTIVE, STATUS_CANCELLED, STATUS_EXPIRED}:
        raise ValueError(f"{path}.status is unsupported")

    gpus = reservation.get("gpus")
    if not isinstance(gpus, list) or not gpus:
        raise ValueError(f"{path}.gpus must be a non-empty list")
    if len(gpus) > MAX_GPU_COUNT:
        raise ValueError(f"{path}.gpus contains too many entries")
    normalized_gpus = []
    for gpu_index, gpu in enumerate(gpus):
        if isinstance(gpu, bool) or not isinstance(gpu, int):
            raise ValueError(f"{path}.gpus[{gpu_index}] must be an integer")
        if gpu < 0 or gpu >= MAX_GPU_COUNT:
            raise ValueError(
                f"{path}.gpus[{gpu_index}] must be between 0 and {MAX_GPU_COUNT - 1}"
            )
        normalized_gpus.append(gpu)
    if len(set(normalized_gpus)) != len(normalized_gpus):
        raise ValueError(f"{path}.gpus must not contain duplicates")

    start = _timestamp(reservation.get("start_at"), f"{path}.start_at")
    end = _timestamp(reservation.get("end_at"), f"{path}.end_at")
    if start >= end:
        raise ValueError(f"{path}.end_at must be later than start_at")
    for field in ("created_at", "updated_at"):
        if reservation.get(field) is not None:
            _timestamp(reservation[field], f"{path}.{field}")

    _optional_positive_integer(
        reservation.get("expected_memory_mb"),
        f"{path}.expected_memory_mb",
    )
    operation_id = reservation.get("op_id")
    if operation_id is not None:
        operation_id = _bounded_text(operation_id, f"{path}.op_id", 128)
        _register_operation_id(uid, operation_id, f"{path}.op_id", seen_operations)
    if reservation.get("operation_signature") is not None:
        _bounded_text(
            reservation["operation_signature"],
            f"{path}.operation_signature",
            128,
        )

    job = reservation.get("job")
    if job is not None and not isinstance(job, dict):
        raise ValueError(f"{path}.job must be an object or null")
    history = reservation.get("edit_operations")
    if history is not None:
        _validate_edit_operations(history, path, uid, seen_operations)


def _validate_edit_operations(
    value: object,
    reservation_path: str,
    uid: int,
    seen_operations: set[tuple[int, str]],
) -> None:
    path = f"{reservation_path}.edit_operations"
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")
    if len(value) > MAX_EDIT_OPERATIONS_PER_RESERVATION:
        raise ValueError(
            f"{path} exceeds {MAX_EDIT_OPERATIONS_PER_RESERVATION} entries"
        )
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{item_path} must be an object")
        operation_id = _bounded_text(item.get("op_id"), f"{item_path}.op_id", 128)
        _bounded_text(item.get("signature"), f"{item_path}.signature", 128)
        _register_operation_id(uid, operation_id, f"{item_path}.op_id", seen_operations)


def _register_operation_id(
    uid: int,
    operation_id: str,
    path: str,
    seen_operations: set[tuple[int, str]],
) -> None:
    key = uid, operation_id
    if key in seen_operations:
        raise ValueError(f"{path} duplicates operation ID {operation_id!r} for UID {uid}")
    seen_operations.add(key)


def _uid(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{path} must be an integer UID")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{path} must be an integer UID") from exc
    if parsed < 0 or parsed > MAX_UID:
        raise ValueError(f"{path} must be between 0 and {MAX_UID}")
    return parsed


def _timestamp(value: object, path: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError(f"{path} must be an ISO 8601 timestamp")
    try:
        return parse_iso(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{path} must be an ISO 8601 timestamp: {exc}") from exc


def _bounded_text(value: Any, path: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"{path} must contain 1-{limit} characters")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{path} must not contain control characters")
    return value


def _optional_positive_integer(value: object, path: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{path} must be a positive integer")
