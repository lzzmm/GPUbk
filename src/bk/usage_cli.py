from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .config import Config
from .monitor import authorize_monitor
from .timeparse import parse_duration_seconds, parse_iso, utc_now
from .usage_api import UsageQueryService
from .usage_store import UsageAuditStore, UsageRetentionPolicy


def run_usage_cli(argv: List[str], config: Config) -> int:
    args_argv = list(argv)
    if args_argv and args_argv[0] == "help":
        if len(args_argv) == 1:
            _usage_help_parser().parse_args(["--help"])
        args_argv = [args_argv[1], "--help", *args_argv[2:]]
    if args_argv and args_argv[0] in {"-h", "--help"}:
        _usage_help_parser().parse_args(args_argv)
    legacy_rollups = "--rollups" in args_argv
    if legacy_rollups:
        args_argv.remove("--rollups")
    actions = {
        "me",
        "users",
        "events",
        "samples",
        "storage",
        "capabilities",
        "maintain",
        "migrate",
        "demo",
    }
    action = args_argv.pop(0) if args_argv and args_argv[0] in actions else ("samples" if legacy_rollups else "me")

    if action == "demo":
        from .live_usage_demo import main as run_live_usage_demo

        return run_live_usage_demo(args_argv)

    if action in {"storage", "capabilities", "maintain", "migrate"}:
        return _admin_command(action, args_argv, config)

    parser = argparse.ArgumentParser(prog=f"bk usage {action}")
    parser.add_argument("--since", default="24h", help="lookback such as 2h, 30d, or 1d12h")
    parser.add_argument("--from", dest="start", help="start time, ISO/local date, or -2h")
    parser.add_argument("--until", help="end time, default now")
    parser.add_argument("--resolution", default="auto", choices=["auto", "1m", "5m", "10m", "1h", "1d"])
    parser.add_argument("--user", help="numeric UID or 'me'")
    parser.add_argument("--all", action="store_true", help="include all visible UIDs")
    parser.add_argument("--gpu", type=int)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--json", action="store_true", help="emit one stable versioned JSON object")
    parser.add_argument("--compact", action="store_true", help="compact JSON with --json")
    parser.add_argument(
        "--no-chart",
        action="store_true",
        help="omit the 7-day and 8-week activity charts from the default personal summary",
    )
    args = parser.parse_args(args_argv)
    if args.limit < 1:
        raise ValueError("--limit must be >= 1")

    end = _parse_time(args.until, utc_now()) if args.until else utc_now()
    if args.start:
        start = _parse_time(args.start, end)
    else:
        start = end - timedelta(seconds=parse_duration_seconds(args.since))
    uid = _uid_filter(action, args.user, args.all)
    store = _store(config)
    api = UsageQueryService(config, store)
    if action == "events":
        payload = api.events(start=start, end=end, uid=uid, gpu=args.gpu, limit=args.limit)
    elif action == "samples":
        payload = api.samples(
            start=start,
            end=end,
            resolution=args.resolution,
            uid=uid,
            gpu=args.gpu,
            limit=args.limit,
        )
    else:
        payload = api.users(
            start=start,
            end=end,
            resolution=args.resolution,
            uid=uid,
            limit=args.limit,
        )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=None if args.compact else 2))
    elif legacy_rollups:
        for record in payload.get("records", []):
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    else:
        _print_payload(payload)
        if action == "me" and not args.no_chart:
            _print_usage_trends(api, uid if uid is not None else os.getuid(), end)
    return 0


def _usage_help_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bk usage",
        description="Query versioned GPU usage history or inspect telemetry storage.",
    )
    commands = parser.add_subparsers(dest="action", metavar="COMMAND")
    commands.add_parser("me", help="summarize the current UID (default without a command)")
    commands.add_parser("users", help="summarize visible users")
    commands.add_parser("events", help="show process audit events")
    commands.add_parser("samples", help="show versioned time-series samples")
    commands.add_parser("storage", help="inspect retention tiers and storage size")
    commands.add_parser("capabilities", help="show telemetry API capabilities")
    commands.add_parser("maintain", help="preview or apply retention maintenance")
    commands.add_parser("migrate", help="preview or migrate legacy telemetry")
    commands.add_parser("demo", help="book one idle GPU and verify live usage accounting")
    return parser


def _admin_command(action: str, argv: List[str], config: Config) -> int:
    parser = argparse.ArgumentParser(prog=f"bk usage {action}")
    parser.add_argument("--json", action="store_true")
    if action in {"maintain", "migrate"}:
        parser.add_argument("--yes", action="store_true", help="apply changes; otherwise report a dry run")
    args = parser.parse_args(argv)
    store = _store(config)
    api = UsageQueryService(config, store)
    if action == "capabilities":
        payload = api.capabilities()
    elif action == "storage":
        payload = {
            "schema_version": "gpubk.usage.v1",
            "kind": "usage-storage",
            "storage": store.storage_info(),
        }
    elif action == "maintain":
        if args.yes:
            authorize_monitor(config)
            with store.lock():
                report = store.maintain(UsageRetentionPolicy.from_config(config), dry_run=False)
        else:
            report = store.maintain(UsageRetentionPolicy.from_config(config), dry_run=True)
        payload = {"schema_version": "gpubk.usage.v1", "kind": "usage-maintenance", "report": report}
    else:
        if args.yes:
            authorize_monitor(config)
            with store.lock():
                report = store.migrate_legacy(dry_run=False)
        else:
            report = store.migrate_legacy(dry_run=True)
        payload = {"schema_version": "gpubk.usage.v1", "kind": "usage-migration", "report": report}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_payload(payload)
    return 0


def _store(config: Config) -> UsageAuditStore:
    return UsageAuditStore(
        config.data_dir,
        config.lock_timeout_seconds,
        config.file_mode,
        config.dir_mode,
        config.storage_gid,
    )


def _uid_filter(action: str, value: Optional[str], include_all: bool) -> Optional[int]:
    if include_all:
        if value is not None:
            raise ValueError("--all and --user cannot be combined")
        return None
    if value is None:
        return None if action in {"users", "events", "samples"} else os.getuid()
    if value.lower() == "me":
        return os.getuid()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError("--user must be a numeric UID or 'me'") from exc
    if parsed < 0:
        raise ValueError("--user must be >= 0")
    return parsed


def _parse_time(value: str, reference: datetime) -> datetime:
    raw = value.strip()
    if raw.lower() == "now":
        return reference
    if raw.startswith("-"):
        return reference - timedelta(seconds=parse_duration_seconds(raw[1:]))
    try:
        return parse_iso(raw)
    except ValueError:
        pass
    local_zone = reference.astimezone().tzinfo
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, pattern)
            return parsed.replace(tzinfo=local_zone).astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError("usage time must be -2h, YYYY-MM-DD, YYYY-MM-DD HH:MM, or ISO 8601")


def _print_payload(payload: dict) -> None:
    _print_collector_summary(payload.get("collector"))
    kind = payload.get("kind")
    if kind == "usage-users":
        _print_users(payload)
    elif kind == "usage-events":
        _print_events(payload)
    elif kind == "usage-samples":
        _print_samples(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _print_collector_summary(collector: object) -> None:
    if not isinstance(collector, dict):
        return
    state = str(collector.get("state", "unknown"))
    if state in {"running", "degraded", "stale"}:
        age = collector.get("age_seconds")
        detail = f" age={age:g}s" if isinstance(age, (int, float)) else ""
        if state == "degraded":
            gaps = []
            for label, key in (
                ("stable-id", "stable_device_identifier_gap"),
                ("process", "process_telemetry_gap"),
                ("identity", "process_identity_gap"),
                ("util", "process_utilization_gap"),
            ):
                values = collector.get(key)
                if isinstance(values, list) and values:
                    gaps.append(f"{label}:{','.join(str(item) for item in values)}")
            if gaps:
                detail += " gaps=" + ";".join(gaps)
    elif state == "stopped" and collector.get("stopped_at"):
        try:
            detail = f" at={parse_iso(str(collector['stopped_at'])).astimezone():%m-%d %H:%M:%S}"
        except ValueError:
            detail = ""
    elif state == "not-seen":
        detail = " (no monitor heartbeat has been recorded)"
    elif state == "topology-mismatch":
        detail = (
            f" (reported={len(collector.get('devices', []))}, "
            f"expected={collector.get('expected_gpu_count')})"
        )
    else:
        detail = f" ({collector.get('error')})" if collector.get("error") else ""
    print(f"collector: {state}{detail}")


def _print_users(payload: dict) -> None:
    query = payload.get("query", {})
    try:
        start = parse_iso(str(query["start_at"])).astimezone()
        end = parse_iso(str(query["end_at"])).astimezone()
        print(
            f"history: {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} "
            "(sampled past only; future reservations excluded)"
        )
    except (KeyError, TypeError, ValueError):
        print("history: sampled past only; future reservations excluded")
    users = payload.get("users", [])
    if not users:
        print("no user usage records in this interval")
        return
    print(f"{'UID':>7} {'User':<16} {'Active':>9} {'Reserved':>9} {'Idle':>9} {'Viol':>8} {'PeakMem':>9} {'AvgSM':>7} Workloads")
    for item in users:
        workloads = ",".join(str(workload.get("label", "?")) for workload in item.get("workloads", [])[:3]) or "-"
        avg_sm = "-" if item.get("avg_sm_percent") is None else f"{item['avg_sm_percent']:.1f}%"
        print(
            f"{item['uid']:>7} {_clip_text(str(item['username']), 16):<16} "
            f"{_usage_duration(int(item['active_gpu_seconds'])):>9} "
            f"{_usage_duration(int(item['reserved_gpu_seconds'])):>9} "
            f"{_usage_duration(int(item['idle_reserved_gpu_seconds'])):>9} "
            f"{_usage_duration(int(item['violation_gpu_seconds'])):>8} "
            f"{_memory_compact(int(item['max_gpu_memory_mb'])):>9} {avg_sm:>7} {workloads}"
        )


def _print_usage_trends(api: UsageQueryService, uid: int, end: datetime) -> None:
    start = end - timedelta(days=56)
    payload = api.samples(
        start=start,
        end=end,
        resolution="1d",
        uid=uid,
        limit=1000,
    )
    records = payload.get("records", [])
    daily: dict[datetime.date, list[float]] = {}
    weekly: dict[datetime.date, list[float]] = {}
    for record in records:
        try:
            local_start = parse_iso(str(record["window_start"])).astimezone()
        except (KeyError, TypeError, ValueError):
            continue
        active = max(0.0, float(record.get("active_observed_seconds", 0)))
        reserved = (
            max(0.0, float(record.get("observed_seconds", 0)))
            if str(record.get("status", "")) == "ok"
            else 0.0
        )
        day = local_start.date()
        week = day - timedelta(days=day.weekday())
        day_values = daily.setdefault(day, [0.0, 0.0])
        week_values = weekly.setdefault(week, [0.0, 0.0])
        day_values[0] += active
        day_values[1] += reserved
        week_values[0] += active
        week_values[1] += reserved

    today = end.astimezone().date()
    current_week = today - timedelta(days=today.weekday())
    daily_rows = [
        (today - timedelta(days=offset), daily.get(today - timedelta(days=offset), [0.0, 0.0]))
        for offset in range(6, -1, -1)
    ]
    weekly_rows = [
        (
            current_week - timedelta(days=7 * offset),
            weekly.get(current_week - timedelta(days=7 * offset), [0.0, 0.0]),
        )
        for offset in range(7, -1, -1)
    ]
    print("\nActive GPU time (historical samples; active/reserved GPU-hours)")
    print("Last 7 days")
    _print_trend_rows(daily_rows, label_format=lambda day: day.strftime("%a %m-%d"))
    print("Last 8 weeks")
    _print_trend_rows(weekly_rows, label_format=lambda day: day.strftime("%m-%d"))


def _print_trend_rows(rows, *, label_format) -> None:
    hours = [values[0] / 3600 for _, values in rows]
    peak = max(hours, default=0.0)
    for (period, values), active_hours in zip(rows, hours):
        reserved_hours = values[1] / 3600
        length = 0 if peak <= 0 else max(1, round(active_hours / peak * 18))
        bar = "█" * length
        print(
            f"  {label_format(period):<9} {bar:<18} "
            f"{active_hours:>6.1f}/{reserved_hours:<6.1f}"
        )


def _print_events(payload: dict) -> None:
    records = payload.get("records", [])
    if not records:
        print("no usage records in this interval")
        return
    print(f"{'Time':<16} {'GPU':>3} {'UID':>7} {'User':<12} {'Event':<14} {'State':<11} Workload")
    for record in records:
        workload = record.get("workload", {})
        label = str(workload.get("label", "-"))
        timestamp = parse_iso(str(record["timestamp"])).astimezone().strftime("%m-%d %H:%M:%S")
        print(
            f"{timestamp:<16} {str(record.get('gpu', '-')):>3} {str(record.get('uid', '-')):>7} "
            f"{_clip_text(str(record.get('username', '?')), 12):<12} "
            f"{_clip_text(str(record.get('event', '?')), 14):<14} "
            f"{_clip_text(str(record.get('status', '?')), 11):<11} {label}"
        )


def _print_samples(payload: dict) -> None:
    records = payload.get("records", [])
    if not records:
        print("no usage records in this interval")
        return
    wide = shutil.get_terminal_size(fallback=(100, 24)).columns >= 110
    if wide:
        print(
            f"{'Start':<14} {'Res':>4} {'GPU':>3} {'UID':>7} {'User':<12} "
            f"{'State':<11} {'Active':>7} {'AvgSM':>6} {'PeakMem':>8} Workload"
        )
    else:
        print(f"{'Start':<14} {'GPU':>3} {'User':<10} {'State':<10} {'Active':>7} {'SM':>4} {'Mem':>7} Workload")
    for record in records:
        workloads = record.get("workloads", [])
        workload = ",".join(str(item.get("label", "?")) for item in workloads[:2]) or "-"
        start = parse_iso(str(record["window_start"])).astimezone().strftime("%m-%d %H:%M")
        resolution = _duration_compact(int(record.get("resolution_seconds", 0)))
        avg_sm = "-" if record.get("avg_sm_percent") is None else f"{record['avg_sm_percent']:.0f}%"
        active = _usage_duration(int(record.get("active_observed_seconds", 0)))
        memory = _memory_compact(int(record.get("max_gpu_memory_mb") or 0))
        if wide:
            print(
                f"{start:<14} {resolution:>4} {str(record.get('gpu', '-')):>3} "
                f"{str(record.get('uid', '-')):>7} {_clip_text(str(record.get('username', '?')), 12):<12} "
                f"{_clip_text(str(record.get('status', '?')), 11):<11} "
                f"{active:>7} {avg_sm:>6} {memory:>8} {workload}"
            )
        else:
            print(
                f"{start:<14} {str(record.get('gpu', '-')):>3} "
                f"{_clip_text(str(record.get('username', '?')), 10):<10} "
                f"{_clip_text(str(record.get('status', '?')), 10):<10} "
                f"{active:>7} {avg_sm:>4} {memory:>7} {workload}"
            )


def _duration_compact(seconds: int) -> str:
    minutes = max(0, seconds) // 60
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return "".join(parts) or "0m"


def _memory_compact(memory_mb: int) -> str:
    return "-" if memory_mb <= 0 else f"{memory_mb / 1024:.1f}G"


def _usage_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    return f"{seconds}s" if seconds < 60 else _duration_compact(seconds)


def _clip_text(value: object, width: int) -> str:
    text = str(value)
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "+"
