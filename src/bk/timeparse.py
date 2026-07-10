from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Union


_DURATION_RE = re.compile(r"^(?P<num>\d+)(?P<unit>[mhd])$")


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
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise ValueError("duration must look like 30m, 4h, or 1d")
    amount = int(match.group("num"))
    unit = match.group("unit")
    if amount < 1:
        raise ValueError("duration must be positive")
    if unit == "m":
        return amount * 60
    if unit == "h":
        return amount * 60 * 60
    if unit == "d":
        return amount * 24 * 60 * 60
    raise ValueError("unsupported duration unit")
