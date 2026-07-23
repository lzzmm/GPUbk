from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional, Sequence

from .models import STATUS_ACTIVE
from .announcements import active_announcements
from .scheduler import exclusive_blocks_for_uid
from .terminal import wrap_display_text, style
from .timeparse import parse_iso
from .worker_guidance import WORKER_FOREGROUND_COMMAND


LOGIN_NOTICE_SCHEMA_VERSION = "gpubk.login-notice.v1"
LOGIN_NOTICE_COLUMNS = 80
_OUTER_ANSI = re.compile(
    r"^(?P<prefix>\x1b\[[0-9;]*m)?(?P<text>.*?)(?P<suffix>\x1b\[0m)?$",
    re.DOTALL,
)


def build_login_summary(
    ledger: dict,
    uid: int,
    *,
    now: datetime,
    within_seconds: int,
    process_state: Optional[Mapping[str, dict]] = None,
    reliable_gpus: Sequence[int] = (),
    worker: Optional[dict] = None,
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
    unreserved = _unreserved_occupancy(
        uid,
        process_state or {},
        set(int(gpu) for gpu in reliable_gpus),
        active,
        overdue,
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
    exclusive_blocks = exclusive_blocks_for_uid(
        ledger,
        uid,
        now=current,
        until=horizon,
    )
    announcements = [
        item
        for item in active_announcements(ledger, now=current)
        if item.get("level") == "critical"
    ]
    return {
        "schema_version": LOGIN_NOTICE_SCHEMA_VERSION,
        "kind": "login-notice",
        "generated_at": current.isoformat().replace("+00:00", "Z"),
        "within_seconds": within_seconds,
        "active": [_public_item(item) for item in active],
        "upcoming": [_public_item(item) for item in upcoming],
        "overdue": overdue,
        "unreserved": unreserved,
        "notifications": notifications[:5],
        "announcements": announcements[:3],
        "exclusive_blocks": [_public_item(item) for item in exclusive_blocks],
        "worker": worker,
    }


def render_login_summary(summary: dict, *, color: bool = False) -> str:
    active = summary.get("active", [])
    upcoming = summary.get("upcoming", [])
    overdue = summary.get("overdue", [])
    unreserved = summary.get("unreserved", [])
    notifications = summary.get("notifications", [])
    announcements = summary.get("announcements", [])
    exclusive_blocks = summary.get("exclusive_blocks", [])
    worker = summary.get("worker")
    if (
        not active
        and not upcoming
        and not overdue
        and not unreserved
        and not notifications
        and not announcements
        and not exclusive_blocks
        and not worker
    ):
        return ""

    labels = []
    if active:
        labels.append(f"{len(active)} active")
    if upcoming:
        labels.append(f"{len(upcoming)} upcoming")
    if overdue:
        labels.append(f"{len(overdue)} overdue occupancy")
    if unreserved:
        labels.append(f"{len(unreserved)} unreserved GPU{'s' if len(unreserved) != 1 else ''}")
    if notifications:
        labels.append(f"{len(notifications)} notice{'s' if len(notifications) != 1 else ''}")
    if announcements:
        labels.append(f"{len(announcements)} critical announcement{'s' if len(announcements) != 1 else ''}")
    generated_at = parse_iso(summary["generated_at"])
    exclusive_now = [
        item
        for item in exclusive_blocks
        if parse_iso(item["start_at"]) <= generated_at
    ]
    exclusive_upcoming = [item for item in exclusive_blocks if item not in exclusive_now]
    if exclusive_now:
        unavailable = {int(gpu) for item in exclusive_now for gpu in item.get("gpus", [])}
        labels.append(f"{len(unavailable)} GPU{'s' if len(unavailable) != 1 else ''} exclusive")
    if exclusive_upcoming:
        labels.append(
            f"{len(exclusive_upcoming)} upcoming exclusive"
            f"{'s' if len(exclusive_upcoming) != 1 else ''}"
        )
    lines = [style(f"GPUBK: {', '.join(labels)}", "heading", enabled=color)]
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
    for item in unreserved:
        lines.append(style(_unreserved_line(item), "warning", enabled=color))
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
    for announcement in announcements:
        lines.append(
            style(
                "  CRITICAL " + str(announcement.get("message", "administrator announcement")),
                "warning",
                enabled=color,
            )
        )
    for item in exclusive_now[:3]:
        lines.append(style(_exclusive_line(item, generated_at, active=True), "error", enabled=color))
    if len(exclusive_now) > 3:
        lines.append(f"  (+{len(exclusive_now) - 3} more active exclusive reservations; run `bk st`)")
    if exclusive_upcoming:
        lines.append(style(_exclusive_line(exclusive_upcoming[0], generated_at, active=False), "warning", enabled=color))
        if len(exclusive_upcoming) > 1:
            lines[-1] += f"  (+{len(exclusive_upcoming) - 1} later; run `bk tl`)"
    if isinstance(worker, dict):
        if worker.get("running") is not True:
            lines.append(
                style(
                    f"  AUTO-RUN worker is not running; use tmux with `{WORKER_FOREGROUND_COMMAND}`, "
                    "or enable the user service",
                    "warning",
                    enabled=color,
                )
            )
        persistence = worker.get("persistence")
        if isinstance(persistence, dict) and persistence.get("logout_safe") is False:
            lines.append(
                style(
                    "  AUTO-RUN may stop after logout; contact the administrator (`bk info`) "
                    "for persistent launch",
                    "warning",
                    enabled=color,
                )
            )
    return "\n".join(
        wrapped
        for line in lines
        for wrapped in _wrap_login_line(line, width=LOGIN_NOTICE_COLUMNS)
    )


def _wrap_login_line(value: str, *, width: int) -> list[str]:
    """Wrap one optionally styled line by terminal display width."""
    match = _OUTER_ANSI.fullmatch(value)
    prefix = match.group("prefix") or "" if match else ""
    suffix = match.group("suffix") or "" if match else ""
    text = match.group("text") if match else value
    output = []
    indent = text[: len(text) - len(text.lstrip())]
    for wrapped in wrap_display_text(
        text,
        width,
        subsequent_indent=indent or "  ",
        preserve_newlines=True,
    ):
        output.append(f"{prefix}{wrapped}{suffix}")
    return output


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


def _unreserved_occupancy(
    uid: int,
    process_state: Mapping[str, dict],
    reliable_gpus: set[int],
    active: Sequence[dict],
    overdue: Sequence[dict],
) -> list[dict]:
    active_gpus = {
        int(gpu) for reservation in active for gpu in reservation.get("gpus", [])
    }
    overdue_gpus = {int(item["gpu"]) for item in overdue}
    processes_by_gpu: dict[int, list[int]] = {}
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
            or gpu in overdue_gpus
            or pid <= 0
        ):
            continue
        processes_by_gpu.setdefault(gpu, []).append(pid)
    return [
        {"gpu": gpu, "pids": sorted(set(pids))}
        for gpu, pids in sorted(processes_by_gpu.items())
    ]


def _public_item(reservation: dict) -> dict:
    return {
        "id": str(reservation["id"]),
        "mode": str(reservation["mode"]),
        "gpus": [int(gpu) for gpu in reservation.get("gpus", [])],
        "start_at": str(reservation["start_at"]),
        "end_at": str(reservation["end_at"]),
        "username": str(reservation.get("username", "unknown")),
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


def _unreserved_line(item: dict) -> str:
    pids = ",".join(str(pid) for pid in item.get("pids", []))
    return (
        f"  WARNING GPU {item['gpu']} has your unreserved PID {pids}; "
        "reserve it with `bk a` or stop the task"
    )


def _exclusive_line(item: dict, now: datetime, *, active: bool) -> str:
    gpus = _gpu_text(item.get("gpus", []))
    owner = str(item.get("username", "another user"))[:16]
    if active:
        end = parse_iso(item["end_at"])
        return (
            f"  AVOID {gpus}: exclusive to {owner} until "
            f"{end.astimezone().strftime('%H:%M')} ({_relative_text(end - now)} left); use `bk g`"
        )
    start = parse_iso(item["start_at"])
    local_start = start.astimezone()
    local_now = now.astimezone()
    when = (
        local_start.strftime("%H:%M")
        if local_start.date() == local_now.date()
        else local_start.strftime("%a %m-%d %H:%M")
    )
    return (
        f"  SOON  {gpus}: exclusive to {owner} at {when} "
        f"(in {_relative_text(start - now)}); use `bk tl`"
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
