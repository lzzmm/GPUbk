from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence

from .models import STATUS_ACTIVE
from .timeparse import parse_iso


LOGIN_NOTICE_SCHEMA_VERSION = "gpubk.login-notice.v1"


def build_login_summary(
    ledger: dict,
    uid: int,
    *,
    now: datetime,
    within_seconds: int,
) -> dict:
    current = now.astimezone(timezone.utc).replace(microsecond=0)
    horizon = current + timedelta(seconds=within_seconds)
    mine = [
        reservation
        for reservation in ledger.get("reservations", [])
        if reservation.get("status") == STATUS_ACTIVE
        and int(reservation.get("uid", -1)) == uid
        and parse_iso(reservation["end_at"]) > current
    ]
    mine.sort(key=lambda reservation: (parse_iso(reservation["start_at"]), reservation["id"]))
    active = [
        reservation
        for reservation in mine
        if parse_iso(reservation["start_at"]) <= current < parse_iso(reservation["end_at"])
    ]
    upcoming = [
        reservation
        for reservation in mine
        if current < parse_iso(reservation["start_at"]) <= horizon
    ]
    return {
        "schema_version": LOGIN_NOTICE_SCHEMA_VERSION,
        "kind": "login-notice",
        "generated_at": current.isoformat().replace("+00:00", "Z"),
        "within_seconds": within_seconds,
        "active": [_public_item(item) for item in active],
        "upcoming": [_public_item(item) for item in upcoming],
    }


def render_login_summary(summary: dict) -> str:
    active = summary.get("active", [])
    upcoming = summary.get("upcoming", [])
    if not active and not upcoming:
        return ""

    labels = []
    if active:
        labels.append(f"{len(active)} active")
    if upcoming:
        labels.append(f"{len(upcoming)} upcoming")
    lines = [f"GPUBK: {', '.join(labels)}"]
    generated_at = parse_iso(summary["generated_at"])
    if active:
        lines.append(_active_line(active[0], generated_at))
        if len(active) > 1:
            lines[-1] += f"  (+{len(active) - 1} more active)"
    if upcoming:
        lines.append(_upcoming_line(upcoming[0], generated_at))
        if len(upcoming) > 1:
            lines[-1] += f"  (+{len(upcoming) - 1} more upcoming)"
    return "\n".join(lines)


def _public_item(reservation: dict) -> dict:
    return {
        "id": str(reservation["id"]),
        "mode": str(reservation["mode"]),
        "gpus": [int(gpu) for gpu in reservation.get("gpus", [])],
        "start_at": str(reservation["start_at"]),
        "end_at": str(reservation["end_at"]),
    }


def _active_line(reservation: dict, now: datetime) -> str:
    end = parse_iso(reservation["end_at"])
    return (
        f"  NOW  {_short_id(reservation)}  {_gpu_text(reservation.get('gpus', []))}  "
        f"until {end.astimezone().strftime('%H:%M')}  "
        f"({_relative_text(end - now)} left)"
    )


def _upcoming_line(reservation: dict, now: datetime) -> str:
    start = parse_iso(reservation["start_at"])
    local_start = start.astimezone()
    local_now = now.astimezone()
    when = (
        local_start.strftime("%H:%M")
        if local_start.date() == local_now.date()
        else local_start.strftime("%a %m-%d %H:%M")
    )
    return (
        f"  NEXT {_short_id(reservation)}  {_gpu_text(reservation.get('gpus', []))}  "
        f"at {when}  (in {_relative_text(start - now)})"
    )


def _gpu_text(gpus: Sequence[int]) -> str:
    return "GPU " + ",".join(str(gpu) for gpu in gpus)


def _short_id(reservation: dict) -> str:
    return str(reservation.get("id", ""))[:6]


def _relative_text(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds()) // 60)
    days, remainder = divmod(minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return "".join(parts)
