from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Union


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
