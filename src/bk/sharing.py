from __future__ import annotations

import re
from typing import Optional


DEFAULT_SHARE_UNITS = 1


def normalize_share_units(value: Optional[int], capacity_units: int) -> int:
    units = DEFAULT_SHARE_UNITS if value is None else _whole_number(value, "share slots")
    if units < 1 or units > capacity_units:
        raise ValueError(f"share slots must be between 1 and {capacity_units}")
    return units


def parse_share_units(value: str | int, capacity_units: int) -> int:
    try:
        units = _whole_number(value, "share slots")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"share slots must be a whole number between 1 and {capacity_units}"
        ) from exc
    return normalize_share_units(units, capacity_units)


def reservation_share_units(reservation: dict, capacity_units: int) -> int:
    raw = reservation.get("share_units", DEFAULT_SHARE_UNITS)
    try:
        units = _whole_number(raw, "share slots")
    except (TypeError, ValueError):
        return max(1, capacity_units)
    if units < 1:
        return max(1, capacity_units)
    return units


def share_text(units: int, capacity_units: int) -> str:
    noun = "slot" if units == 1 else "slots"
    return f"{units} {noun} (max {capacity_units})"


def share_example(capacity_units: int) -> str:
    if capacity_units <= 1:
        return "1"
    return str(capacity_units - 1)


def inferred_share_memory_mb(usable_memory_mb: int, capacity_units: int, share_units: int) -> int:
    return max(1, usable_memory_mb * share_units // max(1, capacity_units))


def _whole_number(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value)
    raise ValueError(f"{label} must be a whole number")
