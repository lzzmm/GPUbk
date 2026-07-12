from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Union

from .granularity import (
    DEFAULT_SLOT_MINUTES,
    ceil_to_slot,
    floor_to_slot,
    is_slot_aligned,
    slot_phrase,
    validate_slot_minutes,
)


_DURATION_TOKEN_RE = re.compile(r"(?P<num>\d+)(?P<unit>[dhm])")
_MEMORY_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)(?P<unit>g|gb|gib|m|mb|mib)$", re.IGNORECASE)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def parse_start(value: str) -> datetime:
    if value == "now":
        return utc_now()
    return parse_iso(value)


def parse_friendly_start(
    value: str,
    now: datetime | None = None,
    slot_minutes: int = DEFAULT_SLOT_MINUTES,
    *,
    allow_past: bool = False,
) -> datetime:
    slot_minutes = validate_slot_minutes(slot_minutes)
    raw = value.strip()
    current = (now if now is not None else datetime.now(timezone.utc)).astimezone(timezone.utc)
    current_floor = floor_to_slot(current, slot_minutes)
    if not raw or raw.lower() == "now":
        return floor_to_slot(current, slot_minutes)
    if raw.startswith("+"):
        try:
            seconds = parse_duration_seconds(raw[1:])
        except ValueError as exc:
            raise ValueError("relative start must look like +30m or +1h30m") from exc
        return ceil_to_slot(current + timedelta(seconds=seconds), slot_minutes)

    local_now = current.astimezone()
    local_zone = local_now.tzinfo
    lowered = raw.lower()
    day_offset = None
    clock_text = raw
    if lowered.startswith("today "):
        day_offset = 0
        clock_text = raw[6:].strip()
    elif lowered.startswith("tomorrow "):
        day_offset = 1
        clock_text = raw[9:].strip()

    if day_offset is not None or re.fullmatch(r"\d{1,2}:\d{2}", clock_text):
        hour, minute = _friendly_clock(clock_text)
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day_offset is not None:
            candidate += timedelta(days=day_offset)
            if (
                not allow_past
                and candidate.astimezone(timezone.utc) < current_floor
            ):
                raise ValueError(f"start time is before the current {slot_phrase(slot_minutes)} interval")
        elif (
            not allow_past
            and candidate.astimezone(timezone.utc) < current_floor
        ):
            candidate += timedelta(days=1)
        return _validate_friendly_boundary(candidate, slot_minutes)

    month_day = re.fullmatch(r"(?P<month>\d{1,2})-(?P<day>\d{1,2})[ T](?P<time>\d{1,2}:\d{2})", raw)
    if month_day:
        hour, minute = _friendly_clock(month_day.group("time"))
        try:
            candidate = datetime(
                local_now.year,
                int(month_day.group("month")),
                int(month_day.group("day")),
                hour,
                minute,
                tzinfo=local_zone,
            )
            if (
                not allow_past
                and candidate.astimezone(timezone.utc) < current_floor
            ):
                candidate = candidate.replace(year=local_now.year + 1)
        except ValueError as exc:
            raise ValueError("invalid calendar date") from exc
        return _validate_friendly_boundary(candidate, slot_minutes)

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "start must be now, +30m, 20:00, tomorrow 09:00, 07-13 20:00, or ISO 8601"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_zone)
    parsed = _validate_friendly_boundary(parsed, slot_minutes)
    if (
        not allow_past
        and parsed.astimezone(timezone.utc) < current_floor
    ):
        raise ValueError(f"start time is before the current {slot_phrase(slot_minutes)} interval")
    return parsed


def normalize_queue_start(
    value: datetime,
    now: datetime | None = None,
    slot_minutes: int = DEFAULT_SLOT_MINUTES,
) -> datetime:
    start = value.astimezone(timezone.utc)
    current = (now or utc_now()).astimezone(timezone.utc)
    if start <= current:
        return floor_to_slot(current, slot_minutes)
    return ceil_to_slot(start, slot_minutes)


def _friendly_clock(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(?P<hour>\d{1,2}):(?P<minute>\d{2})", value)
    if not match:
        raise ValueError("clock time must look like 20:00")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour > 23 or minute > 59:
        raise ValueError("clock time is out of range")
    return hour, minute


def _validate_friendly_boundary(
    value: datetime,
    slot_minutes: int = DEFAULT_SLOT_MINUTES,
) -> datetime:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    if not is_slot_aligned(normalized, slot_minutes):
        raise ValueError(f"start time must align to a {slot_phrase(slot_minutes)} boundary")
    return normalized


def format_local(value: Union[datetime, str]) -> str:
    if isinstance(value, str):
        value = parse_iso(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime("%Y-%m-%d %H:%M %z")


def format_local_range(start: Union[datetime, str], end: Union[datetime, str]) -> str:
    return f"{format_local(start)} -> {format_local(end)}"


def parse_duration_seconds(value: str) -> int:
    raw = value.strip().lower()
    if not raw:
        raise ValueError("duration must look like 30m, 1h30m, or 1d")

    total = 0
    position = 0
    seen_units = set()
    unit_seconds = {"d": 24 * 60 * 60, "h": 60 * 60, "m": 60}
    for match in _DURATION_TOKEN_RE.finditer(raw):
        if match.start() != position:
            raise ValueError("duration must look like 30m, 1h30m, or 1d")
        unit = match.group("unit")
        if unit in seen_units:
            raise ValueError("duration units may only appear once")
        seen_units.add(unit)
        total += int(match.group("num")) * unit_seconds[unit]
        position = match.end()

    if position != len(raw):
        raise ValueError("duration must look like 30m, 1h30m, or 1d")
    if total <= 0:
        raise ValueError("duration must be positive")
    return total


def parse_memory_mb(value: str) -> int:
    match = _MEMORY_RE.match(value.strip())
    if not match:
        raise ValueError("memory must look like 12g or 4096m")
    amount = float(match.group("num"))
    if amount <= 0:
        raise ValueError("memory must be positive")
    unit = match.group("unit").lower()
    multiplier = 1024 if unit in {"g", "gb", "gib"} else 1
    memory_mb = int(amount * multiplier + 0.5)
    if memory_mb < 1:
        raise ValueError("memory must be at least 1 MiB")
    return memory_mb
