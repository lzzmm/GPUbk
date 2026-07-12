from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone


DEFAULT_SLOT_MINUTES = 5
VALID_SLOT_MINUTES = tuple(value for value in range(1, 61) if 60 % value == 0)


def validate_slot_minutes(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("slot_minutes must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("slot_minutes must be an integer") from exc
    if parsed not in VALID_SLOT_MINUTES:
        choices = ", ".join(str(item) for item in VALID_SLOT_MINUTES)
        raise ValueError(f"slot_minutes must divide one hour; choose one of: {choices}")
    return parsed


def slot_seconds(slot_minutes: int) -> int:
    return validate_slot_minutes(slot_minutes) * 60


def floor_to_slot(value: datetime, slot_minutes: int = DEFAULT_SLOT_MINUTES) -> datetime:
    normalized = value.astimezone(timezone.utc)
    step = slot_seconds(slot_minutes)
    timestamp = math.floor(normalized.timestamp())
    return datetime.fromtimestamp(timestamp - (timestamp % step), timezone.utc)


def ceil_to_slot(value: datetime, slot_minutes: int = DEFAULT_SLOT_MINUTES) -> datetime:
    normalized = value.astimezone(timezone.utc)
    floored = floor_to_slot(normalized, slot_minutes)
    return floored if normalized == floored else floored + timedelta(minutes=slot_minutes)


def is_slot_aligned(value: datetime, slot_minutes: int = DEFAULT_SLOT_MINUTES) -> bool:
    normalized = value.astimezone(timezone.utc)
    return normalized == floor_to_slot(normalized, slot_minutes)


def slot_phrase(slot_minutes: int) -> str:
    value = validate_slot_minutes(slot_minutes)
    return f"{value}-minute"
