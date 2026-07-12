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
    legacy_rollups = "--rollups" in args_argv
    if legacy_rollups:
        args_argv.remove("--rollups")
    actions = {"me", "users", "events", "samples", "storage", "capabilities", "maintain", "migrate"}
    action = args_argv.pop(0) if args_argv and args_argv[0] in actions else ("samples" if legacy_rollups else "me")

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
    return 0


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
    kind = payload.get("kind")
    if kind == "usage-users":
        _print_users(payload)
    elif kind == "usage-events":
        _print_events(payload)
    elif kind == "usage-samples":
        _print_samples(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _print_users(payload: dict) -> None:
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
