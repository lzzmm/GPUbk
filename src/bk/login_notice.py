from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional, Sequence

from .models import STATUS_ACTIVE
from .terminal import style
from .timeparse import parse_iso


LOGIN_NOTICE_SCHEMA_VERSION = "gpubk.login-notice.v1"


def build_login_summary(
    ledger: dict,
    uid: int,
    *,
    now: datetime,
    within_seconds: int,
    process_state: Optional[Mapping[str, dict]] = None,
    reliable_gpus: Sequence[int] = (),
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
    overdue = _overdue_occupancy(
        ledger,
        uid,
        current,
        active,
        process_state or {},
        set(int(gpu) for gpu in reliable_gpus),
    )
    notifications = []
    notification_cutoff = current - timedelta(seconds=max(86400, within_seconds))
    for reservation in ledger.get("reservations", []):
        if int(reservation.get("uid", -1)) != uid:
            continue
        for notice in reservation.get("notifications", []):
            if not isinstance(notice, dict):
                continue
            try:
                created_at = parse_iso(str(notice["created_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            if created_at >= notification_cutoff:
                notifications.append(
                    {**notice, "reservation_id": str(reservation.get("id", ""))}
                )
    notifications.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return {
        "schema_version": LOGIN_NOTICE_SCHEMA_VERSION,
        "kind": "login-notice",
        "generated_at": current.isoformat().replace("+00:00", "Z"),
        "within_seconds": within_seconds,
        "active": [_public_item(item) for item in active],
        "upcoming": [_public_item(item) for item in upcoming],
        "overdue": overdue,
        "notifications": notifications[:5],
    }


def render_login_summary(summary: dict, *, color: bool = False) -> str:
    active = summary.get("active", [])
    upcoming = summary.get("upcoming", [])
    overdue = summary.get("overdue", [])
    notifications = summary.get("notifications", [])
    if not active and not upcoming and not overdue and not notifications:
        return ""

    labels = []
    if active:
        labels.append(f"{len(active)} active")
    if upcoming:
        labels.append(f"{len(upcoming)} upcoming")
    if overdue:
        labels.append(f"{len(overdue)} overdue occupancy")
    if notifications:
        labels.append(f"{len(notifications)} notice{'s' if len(notifications) != 1 else ''}")
    lines = [style(f"GPUBK: {', '.join(labels)}", "heading", enabled=color)]
    generated_at = parse_iso(summary["generated_at"])
    if active:
        lines.append(style(_active_line(active[0], generated_at), "success", enabled=color))
        if len(active) > 1:
            lines[-1] += f"  (+{len(active) - 1} more active)"
    if upcoming:
        lines.append(style(_upcoming_line(upcoming[0], generated_at), "accent", enabled=color))
        if len(upcoming) > 1:
            lines[-1] += f"  (+{len(upcoming) - 1} more upcoming)"
    for item in overdue:
        lines.append(style(_overdue_line(item), "error", enabled=color))
    if notifications:
        lines.append(
            style(
                "  NOTICE " + str(notifications[0].get("message", "administrator update")),
                "error",
                enabled=color,
            )
        )
        if len(notifications) > 1:
            lines[-1] += f"  (+{len(notifications) - 1} more; run `bk n`)"
    return "\n".join(lines)


def _overdue_occupancy(
    ledger: dict,
    uid: int,
    now: datetime,
    active: Sequence[dict],
    process_state: Mapping[str, dict],
    reliable_gpus: set[int],
) -> list[dict]:
    active_gpus = {
        int(gpu) for reservation in active for gpu in reservation.get("gpus", [])
    }
    latest_expired = {}
    for reservation in ledger.get("reservations", []):
        if int(reservation.get("uid", -1)) != uid:
            continue
        if reservation.get("status") == "cancelled":
            continue
        try:
            ended = parse_iso(reservation["end_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if ended > now:
            continue
        for raw_gpu in reservation.get("gpus", []):
            gpu = int(raw_gpu)
            previous = latest_expired.get(gpu)
            if previous is None or ended > previous[0]:
                latest_expired[gpu] = (ended, reservation)

    processes_by_gpu: dict[int, list[dict]] = {}
    for process in process_state.values():
        if not isinstance(process, dict) or process.get("status") != "unreserved":
            continue
        try:
            process_uid = int(process.get("uid", -1))
            gpu = int(process.get("gpu", -1))
            pid = int(process.get("pid", -1))
        except (TypeError, ValueError):
            continue
        if (
            process_uid != uid
            or gpu not in reliable_gpus
            or gpu in active_gpus
            or gpu not in latest_expired
            or pid <= 0
        ):
            continue
        processes_by_gpu.setdefault(gpu, []).append(process)

    result = []
    for gpu in sorted(processes_by_gpu):
        ended, reservation = latest_expired[gpu]
        processes = sorted(processes_by_gpu[gpu], key=lambda item: int(item["pid"]))
        result.append(
            {
                "gpu": gpu,
                "reservation_id": str(reservation.get("id", "")),
                "expired_at": ended.isoformat().replace("+00:00", "Z"),
                "pids": [int(item["pid"]) for item in processes],
            }
        )
    return result


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


def _overdue_line(item: dict) -> str:
    ended = parse_iso(item["expired_at"]).astimezone().strftime("%H:%M")
    pids = ",".join(str(pid) for pid in item.get("pids", []))
    return (
        f"  ALERT GPU {item['gpu']} still has your PID {pids} after reservation "
        f"{str(item.get('reservation_id', ''))[:6]} ended at {ended}"
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
