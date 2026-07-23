from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .config import Config
from .monitor import authorize_monitor
from .timeparse import parse_duration_seconds, parse_iso, utc_now
from .terminal import color_enabled, style
from .usage_api import UsageQueryService
from .usage_schema import USAGE_API_VERSION
from .usage_store import UsageAuditStore, UsageRetentionPolicy
from .usage_view import (
    activity_bar_cells,
    build_activity_trends,
    format_usage_duration,
    format_usage_memory,
)


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
    parser.add_argument("-s", "--since", default="24h", help="lookback such as 2h, 30d, or 1d12h")
    parser.add_argument("-f", "--from", dest="start", help="start time, ISO/local date, or -2h")
    parser.add_argument("-u", "--until", help="end time, default now")
    parser.add_argument("-r", "--resolution", default="auto", choices=["auto", "1m", "5m", "10m", "1h", "1d"])
    parser.add_argument("--user", help="numeric UID or 'me'")
    parser.add_argument("--all", action="store_true", help="include all visible UIDs")
    parser.add_argument("-g", "--gpu", type=int)
    parser.add_argument("-n", "--limit", type=int, default=1000)
    parser.add_argument("-j", "--json", action="store_true", help="emit one stable versioned JSON object")
    parser.add_argument("-c", "--compact", action="store_true", help="compact JSON with --json")
    parser.add_argument(
        "--no-chart",
        action="store_true",
        help="omit the 7-day and 4-week activity charts from the default personal summary",
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
        _print_payload(payload, personal=action == "me")
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
            "schema_version": USAGE_API_VERSION,
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
        payload = {"schema_version": USAGE_API_VERSION, "kind": "usage-maintenance", "report": report}
    else:
        if args.yes:
            authorize_monitor(config)
            with store.lock():
                report = store.migrate_legacy(dry_run=False)
        else:
            report = store.migrate_legacy(dry_run=True)
        payload = {"schema_version": USAGE_API_VERSION, "kind": "usage-migration", "report": report}
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


def _print_payload(payload: dict, *, personal: bool = False) -> None:
    _print_collector_summary(payload.get("collector"))
    kind = payload.get("kind")
    if kind == "usage-users":
        _print_users(payload, personal=personal)
    elif kind == "usage-events":
        _print_events(payload)
    elif kind == "usage-samples":
        _print_samples(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    _print_query_notices(payload)


def _print_query_notices(payload: dict) -> None:
    colors = color_enabled(sys.stdout)
    if payload.get("truncated") is True:
        print(
            style(
                "warning: result limit reached; narrow the time range or increase --limit",
                "warning",
                enabled=colors,
            )
        )
    warnings = payload.get("warnings", [])
    if not isinstance(warnings, list):
        return
    for warning in dict.fromkeys(str(item) for item in warnings if item):
        print(style(f"warning: {warning}", "warning", enabled=colors))


def _print_collector_summary(collector: object) -> None:
    if not isinstance(collector, dict):
        return
    state = str(collector.get("state", "unknown"))
    if state in {"running", "degraded", "stale"}:
        age = collector.get("age_seconds")
        detail_parts = []
        started_at = collector.get("started_at")
        if started_at:
            try:
                uptime = max(
                    0,
                    int((utc_now() - parse_iso(str(started_at))).total_seconds()),
                )
                detail_parts.append(f"up {format_usage_duration(uptime)}")
            except ValueError:
                pass
        if isinstance(age, (int, float)):
            detail_parts.append(f"sampled {age:g}s ago")
        rollup = collector.get("rollup_seconds")
        if isinstance(rollup, (int, float)) and rollup > 0:
            detail_parts.append(
                f"latest history may lag up to {format_usage_duration(int(rollup))}"
            )
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
                detail_parts.append("gaps=" + ";".join(gaps))
        detail = " | " + " | ".join(detail_parts) if detail_parts else ""
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
    colors = color_enabled(sys.stdout)
    role = "success" if state == "running" else "warning" if state in {"degraded", "stale"} else "muted"
    label = "healthy" if state == "running" else state
    print(style(f"monitor: {label}{detail}", role, enabled=colors))


def _print_users(payload: dict, *, personal: bool = False) -> None:
    query = payload.get("query", {})
    try:
        start = parse_iso(str(query["start_at"])).astimezone()
        end = parse_iso(str(query["end_at"])).astimezone()
        print(f"requested: {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}")
    except (KeyError, TypeError, ValueError):
        print("requested: sampled history ending now")
    coverage = payload.get("coverage", {})
    if isinstance(coverage, dict) and coverage.get("first_sample_at") and coverage.get("last_sample_at"):
        try:
            first = parse_iso(str(coverage["first_sample_at"])).astimezone()
            last = parse_iso(str(coverage["last_sample_at"])).astimezone()
            count = int(coverage.get("record_count", 0))
            print(
                f"recorded:  {first:%Y-%m-%d %H:%M} -> {last:%Y-%m-%d %H:%M} "
                f"({count} stored sample{'s' if count != 1 else ''}; gaps are not treated as zero)"
            )
        except (TypeError, ValueError):
            pass
    print("scope: sampled past only; future reservations excluded")
    users = payload.get("users", [])
    if not users:
        _print_empty_user_usage(payload)
        return
    if personal and len(users) == 1:
        _print_personal_dashboard(users[0])
        return
    print(f"{'UID':>7} {'User':<16} {'Active':>9} {'Reserved':>9} {'Idle':>9} {'Viol':>8} {'PeakMem':>9} {'AvgSM':>7} Workloads")
    for item in users:
        workloads = ",".join(str(workload.get("label", "?")) for workload in item.get("workloads", [])[:3]) or "-"
        avg_sm = "-" if item.get("avg_sm_percent") is None else f"{item['avg_sm_percent']:.1f}%"
        print(
            f"{item['uid']:>7} {_clip_text(str(item['username']), 16):<16} "
            f"{format_usage_duration(int(item['active_gpu_seconds'])):>9} "
            f"{format_usage_duration(int(item['reserved_gpu_seconds'])):>9} "
            f"{format_usage_duration(int(item['idle_reserved_gpu_seconds'])):>9} "
            f"{format_usage_duration(int(item['violation_gpu_seconds'])):>8} "
            f"{format_usage_memory(int(item['max_gpu_memory_mb'])):>9} {avg_sm:>7} {workloads}"
        )


def _print_empty_user_usage(payload: dict) -> None:
    collector = payload.get("collector", {})
    state = str(collector.get("state", "unknown")) if isinstance(collector, dict) else "unknown"
    colors = color_enabled(sys.stdout)
    coverage = payload.get("coverage", {})
    if isinstance(coverage, dict) and coverage.get("matching_process_event"):
        print(style("Your GPU process was observed, but no usage summary is finalized yet.", "warning", enabled=colors))
        print("Wait one summary interval; if it remains empty, run `bk doctor --require-monitor`.")
        return
    if isinstance(coverage, dict) and coverage.get("store_has_samples"):
        print(style("Usage samples exist, but none match your current numeric UID.", "warning", enabled=colors))
        print(
            f"Current UID: {os.getuid()}. This usually means a changed UID or a process "
            "attributed to root/container ownership; ask the administrator to inspect `bk u users`."
        )
        return
    if state in {"running", "degraded"} and isinstance(collector, dict):
        rollup = max(1, int(collector.get("rollup_seconds", 60)))
        started_at = collector.get("started_at")
        if started_at:
            try:
                uptime = max(0, int((utc_now() - parse_iso(str(started_at))).total_seconds()))
            except ValueError:
                uptime = rollup
            if uptime < rollup:
                remaining = max(1, rollup - uptime)
                print(style("No finalized usage yet.", "warning", enabled=colors))
                print(
                    f"The monitor started {format_usage_duration(uptime)} ago; its first "
                    f"summary should appear in about {format_usage_duration(remaining)}."
                )
                return
        print(style("No recorded GPU use for this user in the requested interval.", "muted", enabled=colors))
        print(
            "A record appears after an active reservation or an attributable GPU process "
            f"is sampled and the {format_usage_duration(rollup)} summary closes."
        )
        return
    print(style("Usage history is unavailable because the monitor is not healthy.", "warning", enabled=colors))
    print("Run `bk doctor --require-monitor` or ask the GPUBK administrator to check the monitor service.")


def _print_personal_dashboard(item: dict) -> None:
    colors = color_enabled(sys.stdout)
    active = max(0, int(item.get("active_gpu_seconds", 0)))
    reserved = max(0, int(item.get("reserved_gpu_seconds", 0)))
    idle = max(0, int(item.get("idle_reserved_gpu_seconds", 0)))
    violations = max(0, int(item.get("violation_gpu_seconds", 0)))
    ratio = 0.0 if reserved <= 0 else min(1.0, active / reserved)
    width = max(12, min(32, shutil.get_terminal_size(fallback=(100, 24)).columns - 48))
    active_cells = round(width * ratio)
    idle_cells = max(0, width - active_cells)
    bar = style("━" * active_cells, "success", enabled=colors) + style(
        "─" * idle_cells,
        "muted",
        enabled=colors,
    )
    print()
    print(style("YOUR GPU USE", "heading", enabled=colors))
    print(
        f"  ACTIVE {format_usage_duration(active):>7}   RESERVED {format_usage_duration(reserved):>7}   "
        f"USE {ratio * 100:>5.1f}%"
    )
    print(f"  {bar}  active {ratio * 100:.0f}% / idle {idle / reserved * 100 if reserved else 0:.0f}%")
    peak = format_usage_memory(int(item.get("max_gpu_memory_mb", 0)))
    avg_sm = item.get("avg_sm_percent")
    avg_text = "-" if avg_sm is None else f"{float(avg_sm):.1f}%"
    violation_text = format_usage_duration(violations)
    violation_role = "error" if violations else "muted"
    print(
        f"  PEAK VRAM {peak:<8} AVG SM {avg_text:<7} VIOLATION "
        + style(violation_text, violation_role, enabled=colors)
    )
    verified = max(0, int(item.get("verified_gpu_seconds", 0)))
    inferred = max(0, int(item.get("inferred_gpu_seconds", 0)))
    ambiguous = max(0, int(item.get("ambiguous_gpu_seconds", 0)))
    print(
        "  IDENTITY "
        + style(f"verified {format_usage_duration(verified)}", "success", enabled=colors)
        + "  "
        + style(f"inferred {format_usage_duration(inferred)}", "warning", enabled=colors)
        + "  "
        + style(f"ambiguous {format_usage_duration(ambiguous)}", "error", enabled=colors)
    )
    workloads = ", ".join(
        str(workload.get("label", "?")) for workload in item.get("workloads", [])[:4]
    )
    print(f"  WORKLOADS {workloads or '-'}")


def _print_usage_trends(api: UsageQueryService, uid: int, end: datetime) -> None:
    start = end - timedelta(days=28)
    payload = api.samples(
        start=start,
        end=end,
        resolution="1d",
        uid=uid,
        limit=1000,
    )
    daily_rows, weekly_rows = build_activity_trends(payload.get("records", []), end)
    colors = color_enabled(sys.stdout)
    print("\n" + style("ACTIVITY TREND", "heading", enabled=colors))
    print(style("Last 7 days", "accent", enabled=colors))
    _print_trend_rows(daily_rows, label_format=lambda day: day.strftime("%a %m-%d"))
    print(style("Last 4 weeks", "accent", enabled=colors))
    _print_trend_rows(weekly_rows, label_format=lambda day: day.strftime("%m-%d"))


def _print_trend_rows(rows, *, label_format) -> None:
    colors = color_enabled(sys.stdout)
    active_values = [values[0] / 3600 for _, values in rows]
    reserved_values = [values[1] / 3600 for _, values in rows]
    peak = max(reserved_values, default=0.0)
    width = max(12, min(24, shutil.get_terminal_size(fallback=(100, 24)).columns - 38))
    for (period, values), active_hours, reserved_hours in zip(rows, active_values, reserved_values):
        active_cells, idle_cells = activity_bar_cells(
            active_hours,
            reserved_hours,
            peak,
            width,
        )
        bar = style("━" * active_cells, "success", enabled=colors) + style(
            "─" * idle_cells,
            "muted",
            enabled=colors,
        )
        padding = " " * (width - active_cells - idle_cells)
        print(
            f"  {label_format(period):<9} {bar}{padding} "
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
        active = format_usage_duration(int(record.get("active_observed_seconds", 0)))
        memory = format_usage_memory(int(record.get("max_gpu_memory_mb") or 0))
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


def _clip_text(value: object, width: int) -> str:
    text = str(value)
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "+"
