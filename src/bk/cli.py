from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import timedelta
from pathlib import Path
from typing import List, Optional

from . import __version__
from .advisor import GpuAdvice, build_gpu_advice
from .allocator import AllocatorDecision
from .config import Config, load_config
from .fileio import open_existing_regular
from .gpu import snapshot
from .identity import current_actor
from .monitor import UsageAuditStore, run_monitor
from .models import MODE_EXCLUSIVE, MODE_SHARED, Actor, BookingError, EditRequest
from .scheduler import (
    cancel_booking,
    edit_booking,
    find_policy_violations,
    list_active,
    shared_memory_headroom_for_reservation,
)
from .service import (
    AGENT_SCHEMA_VERSION,
    build_agent_context,
    public_reservation,
    recommend_booking,
    submit_booking,
)
from .storage import LedgerStore
from .timeparse import (
    format_local,
    format_local_range,
    parse_duration_seconds,
    parse_iso,
    parse_memory_mb,
    parse_start,
    utc_now,
)
from .tui import run_tui
from .usage import classify_process_usage, summarize_process_command
from .worker import job_log_path, retry_job, run_worker

try:
    import readline  # noqa: F401
except ImportError:
    pass


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        config = load_config()
        store = LedgerStore(
            config.data_dir,
            config.lock_timeout_seconds,
            config.backup_keep,
            config.file_mode,
            config.dir_mode,
        )
        if not argv:
            return _interactive_shell(config, store)

        head = argv[0]
        if _looks_like_auto_request(argv):
            return _book_command(argv, MODE_SHARED, config, store)
        if head in {"auto", "shared", "s"}:
            return _book_command(argv[1:], MODE_SHARED, config, store)
        if head in {"exclusive", "x"}:
            return _book_command(argv[1:], MODE_EXCLUSIVE, config, store)
        if head in {"tui", "t"}:
            return run_tui(config, store)
        if head in {"monitor", "m"}:
            return _monitor_command(argv[1:], config, store)
        if head in {"usage", "u"}:
            return _usage_command(argv[1:], config)
        if head in {"worker", "w"}:
            return _worker_command(argv[1:], config, store)
        if head in {"jobs", "j"}:
            return _jobs_command(argv[1:], store)
        if head in {"job-log", "jl"}:
            return _job_log_command(argv[1:], config, store)
        if head in {"job-retry", "jr"}:
            return _job_retry_command(argv[1:], store)
        if head in {"status", "timeline", "st"}:
            _print_status(config, store)
            return 0
        if head in {"agent", "ai"}:
            return _agent_command(argv[1:], config, store)
        if head == "mcp":
            from .mcp_server import main as mcp_main

            mcp_main()
            return 0
        if head == "skill":
            return _skill_command(argv[1:])
        if head == "service":
            return _service_command(argv[1:])
        if head in {"add", "a"}:
            return _add_interactive(config, store)
        if head in {"edit", "e"}:
            return _edit_command(argv[1:], config, store)
        if head in {"del", "delete", "d", "rm"}:
            return _delete_command(argv[1:], store)
        if head == "reset":
            return _reset_command(argv[1:], config, store)
        if head in {"log", "lg"}:
            return _log_command(config, store)
        if head in {"doctor", "dr"}:
            return _doctor_command(config, store)
        if head in {"list", "ls", "l"}:
            return _list_command(argv[1:], store)
        if head in {"-h", "--help", "help"}:
            _print_help()
            return 0
        if head in {"-V", "--version", "version"}:
            print(f"bk {__version__}")
            return 0
        print(f"未知命令: {head}", file=sys.stderr)
        _print_help(file=sys.stderr)
        return 2
    except (BookingError, ValueError, TimeoutError, OSError) as exc:
        if _json_requested(argv):
            print(
                json.dumps(
                    {
                        "schema_version": AGENT_SCHEMA_VERSION,
                        "kind": "error",
                        "error": {"type": exc.__class__.__name__, "message": str(exc)},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 2
        print(f"bk: {exc}", file=sys.stderr)
        return 2


def _looks_like_auto_request(argv: List[str]) -> bool:
    return len(argv) >= 2 and argv[0].isdigit()


def _interactive_shell(config: Config, store: LedgerStore) -> int:
    print("GPUbk booking")
    print(f"data: {config.data_dir}")
    print(f"shared limit: {config.max_shared_users}")
    print("Type 'help' for commands. Type 'quit' to exit.")
    print()
    _print_status(config, store)

    while True:
        try:
            line = input("bk> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        line = line.strip()
        if not line:
            continue
        try:
            args = shlex.split(line)
        except ValueError as exc:
            print(f"bk: {exc}")
            continue
        if not args:
            continue

        try:
            keep_running = _dispatch_shell_command(args, config, store)
        except SystemExit as exc:
            if exc.code not in (0, None):
                print("bk: invalid command arguments")
            keep_running = True
        except (BookingError, ValueError, TimeoutError, OSError) as exc:
            print(f"bk: {exc}")
            keep_running = True

        if not keep_running:
            return 0


def _dispatch_shell_command(args: List[str], config: Config, store: LedgerStore) -> bool:
    head = args[0]
    if head in {"q", "quit", "exit"}:
        return False
    if head in {"h", "help", "?"}:
        _print_shell_help()
        return True
    if head in {"status", "refresh", "r", "timeline", "st"}:
        _print_status(config, store)
        return True
    if head in {"list", "ls", "l"}:
        _list_command(args[1:], store)
        return True
    if head in {"log", "logs", "lg"}:
        _log_command(config, store)
        return True
    if head in {"doctor", "dr"}:
        _doctor_command(config, store)
        return True
    if head in {"del", "delete", "cancel", "d", "rm"}:
        _delete_command(args[1:], store)
        return True
    if head in {"edit", "e"}:
        _edit_command(args[1:], config, store)
        return True
    if head == "reset":
        _reset_command(args[1:], config, store)
        return True
    if head in {"add", "a"}:
        _add_interactive(config, store)
        return True
    if head in {"tui", "t"}:
        run_tui(config, store)
        return True
    if head in {"monitor", "m"}:
        _monitor_command(args[1:], config, store)
        return True
    if head in {"usage", "u"}:
        _usage_command(args[1:], config)
        return True
    if head in {"worker", "w"}:
        _worker_command(args[1:], config, store)
        return True
    if head in {"jobs", "j"}:
        _jobs_command(args[1:], store)
        return True
    if head in {"job-log", "jl"}:
        _job_log_command(args[1:], config, store)
        return True
    if head in {"job-retry", "jr"}:
        _job_retry_command(args[1:], store)
        return True
    if head in {"agent", "ai"}:
        _agent_command(args[1:], config, store)
        return True
    if _looks_like_auto_request(args):
        _book_command(args, MODE_SHARED, config, store)
        return True
    if head in {"auto", "shared", "s"}:
        _book_command(args[1:], MODE_SHARED, config, store)
        return True
    if head in {"exclusive", "x"}:
        _book_command(args[1:], MODE_EXCLUSIVE, config, store)
        return True
    print(f"未知命令: {head}")
    _print_shell_help()
    return True


def _book_command(argv: List[str], mode: str, config: Config, store: LedgerStore) -> int:
    booking_argv, command_argv = _split_job_command(argv)
    parser = argparse.ArgumentParser(prog=f"bk {'exclusive' if mode == MODE_EXCLUSIVE else ''}".strip())
    parser.add_argument("count", type=int)
    parser.add_argument("duration")
    parser.add_argument("--start", help="ISO time; omitted means now with automatic queueing")
    parser.add_argument("--gpu", help="comma separated GPU indexes, for example 0,1")
    parser.add_argument("--mem", help="expected memory on each GPU, for example 12g or 4096m")
    parser.add_argument("--op-id", help="idempotency key for agents and retry-safe scripts")
    parser.add_argument("--json", action="store_true", help="emit a stable machine-readable result")
    args = parser.parse_args(booking_argv)

    start_raw = args.start or "now"
    preferred = _parse_gpu_list(args.gpu) if args.gpu else None
    expected_memory_mb = parse_memory_mb(args.mem) if args.mem else None
    duration_seconds = parse_duration_seconds(args.duration)
    start_at = parse_start(start_raw)
    actor = _current_actor()
    submission = submit_booking(
        config,
        store,
        actor,
        count=args.count,
        duration_seconds=duration_seconds,
        start_at=start_at,
        mode=mode,
        preferred_gpus=preferred,
        allow_queue=args.start is None,
        operation_id=args.op_id,
        command_argv=command_argv,
        working_directory=os.getcwd() if command_argv is not None else None,
        expected_memory_mb=expected_memory_mb,
    )
    result = submission.result
    advice = submission.advice
    allocator = submission.allocator
    reservation = result.reservation
    if not result.created:
        status = "exists"
    elif result.queued:
        status = "queued"
    else:
        status = "created"
    if args.json:
        print(
            json.dumps(
                _booking_result_payload(status, reservation, actor, advice, allocator, store.last_warning),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    gpus = ",".join(str(item) for item in reservation["gpus"])
    print(
        f"{status}: {_short_id(reservation)} mode={reservation['mode']} "
        f"gpu={gpus} {format_local_range(reservation['start_at'], reservation['end_at'])}"
    )
    _print_booking_advice(config, store, reservation, advice, expected_memory_mb)
    if allocator.source == "external":
        print(f"allocator: external{f' ({allocator.reason})' if allocator.reason else ''}")
    elif allocator.warning:
        print(f"warning: {allocator.warning}", file=sys.stderr)
    if isinstance(reservation.get("job"), dict):
        print(
            f"job: {reservation['job'].get('status')} "
            f"command={reservation['job'].get('summary', 'private command')}"
        )
        print("worker: keep `bk w` running before the scheduled start")
    return 0


def _monitor_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk monitor")
    parser.add_argument("--interval", type=float, default=2.0, help="sampling interval in seconds (default: 2)")
    parser.add_argument("--rollup", type=int, default=60, help="rollup window in seconds (default: 60)")
    parser.add_argument("--once", action="store_true", help="collect one sample and exit")
    parser.add_argument("--samples", type=int, help="collect a bounded number of samples and exit")
    parser.add_argument("--verbose", action="store_true", help="print every sample instead of state changes only")
    args = parser.parse_args(argv)
    return run_monitor(
        config,
        store,
        interval_seconds=args.interval,
        rollup_seconds=args.rollup,
        once=args.once,
        max_samples=1 if args.once else args.samples,
        verbose=args.verbose,
    )


def _usage_command(argv: List[str], config: Config) -> int:
    parser = argparse.ArgumentParser(prog="bk usage")
    parser.add_argument("--rollups", action="store_true", help="show utilization rollups instead of events")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    if args.limit < 1:
        raise ValueError("--limit must be >= 1")
    store = UsageAuditStore(
        config.data_dir,
        config.lock_timeout_seconds,
        config.file_mode,
        config.dir_mode,
    )
    records = store.recent_rollups(args.limit) if args.rollups else store.recent_events(args.limit)
    if not records:
        print("no usage records")
        return 0
    for record in records:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0


def _worker_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk worker")
    parser.add_argument("--once", action="store_true", help="run due jobs, wait for them, then exit")
    parser.add_argument("--poll", type=float, help="poll interval in seconds")
    parser.add_argument("--max-parallel", type=int, help="maximum child jobs for this worker")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    summary = run_worker(
        config,
        store,
        _current_actor(),
        once=args.once,
        poll_seconds=args.poll,
        max_parallel=args.max_parallel,
        quiet=args.quiet,
    )
    return 0 if summary.failed == 0 else 1


def _jobs_command(argv: List[str], store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk jobs")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    actor = _current_actor()
    reservations = _own_job_reservations(store, actor)
    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": AGENT_SCHEMA_VERSION,
                    "kind": "jobs",
                    "jobs": [public_reservation(item, actor) for item in reservations],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if not reservations:
        print("No jobs.")
        return 0
    print("#  ID       State        GPU       Start                  Command")
    for index, reservation in enumerate(reservations, 1):
        job = reservation["job"]
        command = str(job.get("summary", "legacy/private command"))
        if len(command) > 48:
            command = command[:45] + "..."
        print(
            f"{index:<2} {_short_id(reservation):<8} {str(job.get('status', '?')):<12} "
            f"{','.join(map(str, reservation.get('gpus', []))):<9} "
            f"{format_local(reservation['start_at']):<22} {command}"
        )
    return 0


def _job_log_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk job-log")
    parser.add_argument("reservation_id", nargs="?")
    args = parser.parse_args(argv)
    actor = _current_actor()
    reservations = _own_job_reservations(store, actor)
    if not reservations:
        raise BookingError("you have no jobs")
    token = args.reservation_id or input("job number or short id: ").strip()
    reservation = _resolve_job_reservation(reservations, token)
    path = job_log_path(config, str(reservation["id"]))
    if not path.exists():
        print(f"job log not created yet: {path}")
        return 0
    fd = open_existing_regular(path)
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as fh:
        while chunk := fh.read(64 * 1024):
            sys.stdout.write(chunk)
    return 0


def _job_retry_command(argv: List[str], store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk job-retry")
    parser.add_argument("reservation_id")
    parser.add_argument(
        "--accept-duplicate-risk",
        action="store_true",
        help="required for an uncertain claim that might already be running",
    )
    args = parser.parse_args(argv)
    actor = _current_actor()
    reservation = _resolve_job_reservation(_own_job_reservations(store, actor), args.reservation_id)
    updated = retry_job(
        store,
        actor,
        str(reservation["id"]),
        accept_duplicate_risk=args.accept_duplicate_risk,
    )
    print(f"job retry queued: {_short_id(updated)}")
    return 0


def _agent_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk agent")
    subparsers = parser.add_subparsers(dest="action", required=True)
    context_parser = subparsers.add_parser("context", aliases=["ctx"], help="emit privacy-safe allocation context")
    context_parser.add_argument("--compact", action="store_true")
    recommend_parser = subparsers.add_parser("recommend", aliases=["rec"], help="compute a read-only legal placement")
    recommend_parser.add_argument("count", type=int)
    recommend_parser.add_argument("duration")
    recommend_parser.add_argument("--mode", default="s", choices=["s", "shared", "x", "exclusive"])
    recommend_parser.add_argument("--start", help="exact ISO start; omitted means earliest queueable slot")
    recommend_parser.add_argument("--gpu", help="fixed comma-separated GPU indexes")
    recommend_parser.add_argument("--mem", help="expected memory per GPU, e.g. 12g")
    recommend_parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    actor = _current_actor()
    try:
        if args.action in {"context", "ctx"}:
            payload = build_agent_context(config, store, actor)
            compact = args.compact
            exit_code = 0
        else:
            mode = MODE_EXCLUSIVE if args.mode in {"x", "exclusive"} else MODE_SHARED
            payload = recommend_booking(
                config,
                store,
                actor,
                count=args.count,
                duration_seconds=parse_duration_seconds(args.duration),
                start_at=parse_start(args.start or "now"),
                mode=mode,
                preferred_gpus=_parse_gpu_list(args.gpu) if args.gpu else None,
                expected_memory_mb=parse_memory_mb(args.mem) if args.mem else None,
                allow_queue=args.start is None,
            )
            compact = args.compact
            exit_code = 0 if payload["available"] else 3
    except (BookingError, ValueError, OSError) as exc:
        payload = {
            "schema_version": AGENT_SCHEMA_VERSION,
            "kind": "error",
            "error": {"type": exc.__class__.__name__, "message": str(exc)},
        }
        compact = getattr(args, "compact", False)
        exit_code = 2
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=None if compact else 2,
        )
    )
    return exit_code


def _skill_command(argv: List[str]) -> int:
    from .skill import default_skill_path, install_skill, skill_text

    parser = argparse.ArgumentParser(prog="bk skill")
    subparsers = parser.add_subparsers(dest="action", required=True)
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--target", type=Path, help="exact destination skill directory")
    install_parser.add_argument("--force", action="store_true")
    subparsers.add_parser("show")
    subparsers.add_parser("path")
    args = parser.parse_args(argv)
    if args.action == "show":
        print(skill_text(), end="")
        return 0
    if args.action == "path":
        print(default_skill_path())
        return 0
    installed = install_skill(args.target, force=args.force)
    print(f"installed skill: {installed}")
    return 0


def _service_command(argv: List[str]) -> int:
    from .systemd import install_user_unit, unit_text

    parser = argparse.ArgumentParser(prog="bk service")
    subparsers = parser.add_subparsers(dest="action", required=True)
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("kind", choices=["monitor", "worker"])
    install_parser.add_argument("--target-dir", type=Path)
    install_parser.add_argument("--force", action="store_true")
    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("kind", choices=["monitor", "worker"])
    args = parser.parse_args(argv)
    if args.action == "show":
        print(unit_text(args.kind), end="")
        return 0
    path = install_user_unit(args.kind, args.target_dir, force=args.force)
    print(f"installed unit: {path}")
    print("not enabled or started; review it, then run systemctl --user daemon-reload")
    return 0


def _add_interactive(config: Config, store: LedgerStore) -> int:
    actor = _current_actor()
    mode_raw = input("mode [s/x] (shared): ").strip() or MODE_SHARED
    if mode_raw in {"s", "x"}:
        mode_raw = MODE_SHARED if mode_raw == "s" else MODE_EXCLUSIVE
    if mode_raw not in {MODE_SHARED, MODE_EXCLUSIVE}:
        raise ValueError("mode must be shared or exclusive")
    count = int(input("gpu count: ").strip())
    duration = parse_duration_seconds(input("duration (30m/1h30m/1d): ").strip())
    start_raw = input("start ISO or now (now): ").strip()
    allow_queue = start_raw in {"", "now"}
    start = parse_start(start_raw or "now")
    gpu_raw = input("gpu indexes optional, for example 0,1: ").strip()
    preferred = _parse_gpu_list(gpu_raw) if gpu_raw else None
    memory_raw = input("expected memory per GPU optional, for example 12g: ").strip()
    expected_memory_mb = parse_memory_mb(memory_raw) if memory_raw else None
    command_raw = input("command optional, for example python train.py: ").strip()
    command_argv = shlex.split(command_raw) if command_raw else None
    submission = submit_booking(
        config,
        store,
        actor,
        count=count,
        duration_seconds=duration,
        start_at=start,
        mode=mode_raw,
        preferred_gpus=preferred,
        expected_memory_mb=expected_memory_mb,
        allow_queue=allow_queue,
        command_argv=command_argv,
        working_directory=os.getcwd() if command_argv is not None else None,
    )
    result = submission.result
    advice = submission.advice
    reservation = result.reservation
    print(f"{'queued' if result.queued else 'created'}: {_short_id(reservation)} {format_local_range(reservation['start_at'], reservation['end_at'])}")
    _print_booking_advice(config, store, reservation, advice, expected_memory_mb)
    if isinstance(reservation.get("job"), dict):
        print(f"job: pending command={reservation['job'].get('summary', 'private command')}")
        print("worker: keep `bk w` running before the scheduled start")
    return 0


def _delete_command(argv: List[str], store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk del")
    parser.add_argument("reservation_id", nargs="?")
    args = parser.parse_args(argv)
    actor = _current_actor()
    reservation_id = args.reservation_id or _prompt_reservation_token(store, actor, "delete")
    resolved = _resolve_own_reservation_id(store, reservation_id, actor)
    reservation = cancel_booking(store, resolved, actor)
    print(f"cancelled: {_short_id(reservation)}")
    return 0


def _edit_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk edit")
    parser.add_argument("reservation_id", nargs="?")
    parser.add_argument("--duration")
    parser.add_argument("--start")
    parser.add_argument("--gpu", help="comma separated GPU indexes; use with --count to change GPU count")
    parser.add_argument("--count", type=int)
    parser.add_argument("--mode", choices=["s", MODE_SHARED, "x", MODE_EXCLUSIVE])
    parser.add_argument("--mem", help="expected memory per GPU; use - to clear")
    parser.add_argument("--queue", action="store_true", help="allow moving to the next available slot")
    args = parser.parse_args(argv)

    actor = _current_actor()
    token = args.reservation_id or _prompt_reservation_token(store, actor, "edit")
    reservation_id = _resolve_own_reservation_id(store, token, actor)
    if not any([args.duration, args.start, args.gpu, args.count, args.mode, args.mem, args.queue]):
        return _edit_interactive(config, store, reservation_id, actor)

    preferred = _parse_gpu_list(args.gpu) if args.gpu else None
    advice = build_gpu_advice(config)
    expected_memory_mb = None if args.mem == "-" else (parse_memory_mb(args.mem) if args.mem else None)
    edit_mode = MODE_EXCLUSIVE if args.mode == "x" else (MODE_SHARED if args.mode == "s" else args.mode)
    result = edit_booking(
        store,
        config,
        EditRequest(
            actor=actor,
            reservation_id=reservation_id,
            start_at=parse_start(args.start) if args.start else None,
            duration_seconds=parse_duration_seconds(args.duration) if args.duration else None,
            mode=edit_mode,
            preferred_gpus=preferred,
            gpu_order=advice.order,
            gpu_scores=advice.scores,
            count=args.count,
            allow_queue=args.queue,
            expected_memory_mb=expected_memory_mb,
            update_expected_memory=args.mem is not None,
            gpu_memory_capacity_mb=advice.memory_capacities_mb,
        ),
    )
    _print_edit_result(result.reservation, result)
    return 0


def _edit_interactive(config: Config, store: LedgerStore, reservation_id: str, actor: Actor) -> int:
    reservation = _get_reservation(store, reservation_id)
    print(f"editing {_short_id(reservation)}")
    print(f"current: mode={reservation['mode']} gpu={','.join(map(str, reservation.get('gpus', [])))} {format_local_range(reservation['start_at'], reservation['end_at'])}")
    mode = input(f"mode [{reservation['mode']}]: ").strip() or None
    if mode in {"s", "x"}:
        mode = MODE_SHARED if mode == "s" else MODE_EXCLUSIVE
    if mode and mode not in {MODE_SHARED, MODE_EXCLUSIVE}:
        raise ValueError("mode must be s/shared or x/exclusive")
    duration = input("duration (blank keep, e.g. 30m/4h): ").strip()
    start = input("start ISO/now (blank keep): ").strip()
    gpu_raw = input("gpu list (blank keep, e.g. 0,1): ").strip()
    count_raw = input("gpu count for auto-pick (blank keep): ").strip()
    current_memory = reservation.get("expected_memory_mb")
    memory_default = _format_memory_mb(int(current_memory)) if current_memory is not None else "auto"
    memory_raw = input(f"expected memory per GPU [{memory_default}] (- clears, blank keep): ").strip()
    queue_raw = input("queue if conflict? [y/N]: ").strip().lower()
    preferred = _parse_gpu_list(gpu_raw) if gpu_raw else None
    advice = build_gpu_advice(config)
    expected_memory_mb = None if memory_raw == "-" else (parse_memory_mb(memory_raw) if memory_raw else None)
    result = edit_booking(
        store,
        config,
        EditRequest(
            actor=actor,
            reservation_id=reservation_id,
            start_at=parse_start(start) if start else None,
            duration_seconds=parse_duration_seconds(duration) if duration else None,
            mode=mode,
            preferred_gpus=preferred,
            gpu_order=advice.order,
            gpu_scores=advice.scores,
            count=int(count_raw) if count_raw else None,
            allow_queue=queue_raw in {"y", "yes"},
            expected_memory_mb=expected_memory_mb,
            update_expected_memory=bool(memory_raw),
            gpu_memory_capacity_mb=advice.memory_capacities_mb,
        ),
    )
    _print_edit_result(result.reservation, result)
    return 0


def _print_edit_result(reservation: dict, result) -> None:
    status = "queued" if result.queued else "updated"
    print(
        f"{status}: {_short_id(reservation)} mode={reservation['mode']} "
        f"gpu={','.join(map(str, reservation.get('gpus', [])))} "
        f"{format_local_range(reservation['start_at'], reservation['end_at'])}"
    )


def _log_command(config: Config, store: LedgerStore) -> int:
    uid = _current_actor().uid
    if not store.log_path.exists():
        return 0
    fd = open_existing_regular(store.log_path)
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        for line in fh:
            item = json.loads(line)
            if int(item.get("uid")) != uid:
                continue
            when = format_local(item["ts"]) if item.get("ts") else ""
            time_range = ""
            if item.get("start_at") and item.get("end_at"):
                time_range = f" {format_local_range(item['start_at'], item['end_at'])}"
            print(
                f"{when} {item['action']} {item['result']} "
                f"{str(item.get('reservation_id', ''))[:8]} mode={item.get('mode')} gpu={','.join(map(str, item.get('gpus', [])))}{time_range}"
            )
    return 0


def _list_command(argv: List[str], store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk list")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    active = list_active(store.load())
    actor = _current_actor()
    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": AGENT_SCHEMA_VERSION,
                    "kind": "reservations",
                    "reservations": [public_reservation(item, actor) for item in active],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if not active:
        print("No active reservations.")
        return 0
    mine = _own_active_reservations(store, actor)
    mine_index = {reservation["id"]: index + 1 for index, reservation in enumerate(mine)}
    for reservation in active:
        gpus = ",".join(str(item) for item in reservation.get("gpus", []))
        index = mine_index.get(reservation["id"], "-")
        print(
            f"{index:>2} {_short_id(reservation)} {reservation['mode']} uid={reservation['uid']} "
            f"user={reservation['username']} gpu={gpus} "
            f"job={reservation.get('job', {}).get('status', '-')} "
            f"{format_local_range(reservation['start_at'], reservation['end_at'])}"
        )
    return 0


def _doctor_command(config: Config, store: LedgerStore) -> int:
    storage_issues = store.health_issues()
    ledger = store.load()
    issues = find_policy_violations(ledger, config.max_shared_users)
    privacy_issues = [
        item
        for item in ledger.get("reservations", [])
        if isinstance(item.get("job"), dict) and "argv" in item["job"]
    ]
    if not issues and not storage_issues and not privacy_issues:
        print("No policy issues found.")
        return 0
    print(
        f"Found {len(storage_issues)} storage issue(s), {len(issues)} policy issue(s), "
        f"{len(privacy_issues)} privacy issue(s):"
    )
    for issue in storage_issues:
        details = " ".join(
            f"{key}={value}"
            for key, value in issue.items()
            if key not in {"type", "message"}
        )
        print(f"{issue['type']} {details} {issue.get('message', '')}".rstrip())
    for reservation in privacy_issues:
        print(
            f"legacy-inline-job-command id={_short_id(reservation)} "
            "full argv is visible in the shared ledger; recreate this pending job"
        )
    for issue in issues:
        if issue["type"] == "shared-capacity":
            print(
                "shared-capacity "
                f"gpu={issue['gpu']} count={issue['count']} limit={issue['limit']} "
                f"{format_local_range(issue['start_at'], issue['end_at'])} "
                f"ids={','.join(str(item)[:8] for item in issue['reservation_ids'])}"
            )
        elif issue["type"] == "exclusive-overlap":
            print(
                "exclusive-overlap "
                f"gpu={issue['gpu']} "
                f"{str(issue['left_id'])[:8]}[{format_local_range(issue['left_start_at'], issue['left_end_at'])}] "
                f"overlaps {str(issue['right_id'])[:8]}[{format_local_range(issue['right_start_at'], issue['right_end_at'])}]"
            )
    return 0


def _reset_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk reset")
    parser.add_argument("--yes", action="store_true", help="required to reset without an interactive confirmation")
    args = parser.parse_args(argv)
    if not args.yes:
        answer = input(f"Clear all bk data in {store.data_dir}? Type reset to continue: ").strip()
        if answer != "reset":
            print("reset cancelled")
            return 1
    audit_store = UsageAuditStore(
        config.data_dir,
        config.lock_timeout_seconds,
        config.file_mode,
        config.dir_mode,
    )
    with audit_store.lock():
        result = store.reset()
        usage_result = audit_store.clear_unlocked()
    print(
        f"reset: removed {result['reservations']} reservation record(s), "
        f"{result['logs']} log line(s), {result['backups']} backup file(s), "
        f"{usage_result['usage_events']} usage event(s), "
        f"{usage_result['usage_rollups']} usage rollup(s)"
    )
    return 0


def _parse_gpu_list(value: str) -> List[int]:
    gpus = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        gpus.append(int(part))
    if not gpus:
        raise ValueError("--gpu must contain at least one GPU index")
    return gpus


def _split_job_command(argv: List[str]) -> tuple[List[str], Optional[List[str]]]:
    if "--" not in argv:
        return argv, None
    separator = argv.index("--")
    command = argv[separator + 1 :]
    if not command:
        raise ValueError("-- must be followed by a job command")
    return argv[:separator], command


def _json_requested(argv: List[str]) -> bool:
    booking_args = argv[: argv.index("--")] if "--" in argv else argv
    return "--json" in booking_args


def _print_booking_advice(
    config: Config,
    store: LedgerStore,
    reservation: dict,
    advice: GpuAdvice,
    expected_memory_mb: Optional[int],
) -> None:
    selected = [int(gpu) for gpu in reservation.get("gpus", [])]
    has_telemetry = any(item.source != "none" for item in advice.snapshots)
    has_history = any(item.sample_count for item in advice.historical_loads.values())
    if has_telemetry or has_history:
        parts = []
        for gpu in selected:
            state = advice.live_states[gpu]
            recent = advice.historical_loads[gpu]
            recent_text = f"{recent.predicted_percent:.0f}%" if recent.sample_count else "n/a"
            parts.append(f"GPU {gpu} score={advice.scores[gpu]:.1f} now={state.status} recent={recent_text}")
        print("selection: " + "; ".join(parts))

    busy_selected = [gpu for gpu in selected if advice.live_states[gpu].status == "busy"]
    busy_avoided = [
        gpu
        for gpu in advice.order
        if gpu not in selected and advice.live_states[gpu].status == "busy"
    ]
    if busy_selected:
        details = ", ".join(f"GPU {gpu} ({advice.live_states[gpu].reason})" for gpu in busy_selected)
        print(f"warning: selected GPU currently busy: {details}", file=sys.stderr)
    elif busy_avoided:
        details = ", ".join(f"GPU {gpu} ({advice.live_states[gpu].reason})" for gpu in busy_avoided)
        print(f"note: avoided currently busy {details}")

    capacities = advice.memory_capacities_mb
    if not capacities:
        if reservation.get("mode") == MODE_SHARED and expected_memory_mb is None:
            print("note: GPU memory telemetry unavailable; shared memory admission used record limit only")
        return

    snapshots = {item.index: item for item in advice.snapshots}
    headroom = shared_memory_headroom_for_reservation(
        list_active(store.load(), parse_iso(reservation["start_at"])),
        reservation,
        capacities,
        config.max_shared_users,
        config.shared_memory_reserve_mb,
    )
    memory_parts = []
    for gpu in selected:
        item = snapshots.get(gpu)
        if item is None or not item.memory_total_mb:
            continue
        now_free = max(0, item.memory_total_mb - item.memory_used_mb)
        projected = headroom.get(gpu)
        projected_text = f", projected-headroom={_format_memory_mb(projected)}" if projected is not None else ""
        memory_parts.append(f"GPU {gpu} now-free={_format_memory_mb(now_free)}{projected_text}")
    if memory_parts:
        print("memory: " + "; ".join(memory_parts))
    if reservation.get("mode") == MODE_SHARED and expected_memory_mb is None:
        assumptions = [
            max(1, (capacities[gpu] - config.shared_memory_reserve_mb) // config.max_shared_users)
            for gpu in selected
            if gpu in capacities
        ]
        if assumptions:
            print(
                f"note: --mem omitted; assumed {_format_memory_mb(min(assumptions))} per GPU "
                f"from shared limit {config.max_shared_users}"
            )


def _booking_result_payload(
    status: str,
    reservation: dict,
    actor: Actor,
    advice: GpuAdvice,
    allocator: AllocatorDecision,
    warning: Optional[str],
) -> dict:
    selected = []
    for gpu in reservation.get("gpus", []):
        live = advice.live_states[int(gpu)]
        historical = advice.historical_loads[int(gpu)]
        selected.append(
            {
                "gpu": int(gpu),
                "load_score": allocator.scores[int(gpu)],
                "live_status": live.status,
                "live_reason": live.reason,
                "recent_predicted_load_percent": round(historical.predicted_percent, 3),
                "history_sample_count": historical.sample_count,
            }
        )
    warnings = [warning] if warning else []
    if allocator.warning:
        warnings.append(allocator.warning)
    if any(item["live_status"] == "busy" for item in selected):
        warnings.append("selected GPU is currently busy; live task end time is unknown")
    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        "kind": "booking_result",
        "status": status,
        "reservation": public_reservation(reservation, actor),
        "allocation": {"selected": selected},
        "allocator": {
            "source": allocator.source,
            "reason": allocator.reason,
        },
        "warnings": warnings,
    }


def _format_memory_mb(value: Optional[int]) -> str:
    if value is None:
        return "unknown"
    if value >= 1024:
        return f"{value / 1024:.1f}GiB"
    return f"{value}MiB"


def _current_actor() -> Actor:
    return current_actor()


def _short_id(reservation: dict) -> str:
    return str(reservation.get("id", ""))[:8]


def _own_active_reservations(store: LedgerStore, actor: Actor) -> List[dict]:
    return [item for item in list_active(store.load()) if int(item.get("uid")) == actor.uid]


def _own_job_reservations(store: LedgerStore, actor: Actor) -> List[dict]:
    result = [
        item
        for item in store.load().get("reservations", [])
        if int(item.get("uid", -1)) == actor.uid and isinstance(item.get("job"), dict)
    ]
    return sorted(result, key=lambda item: (str(item.get("start_at", "")), str(item.get("id", ""))))


def _resolve_job_reservation(reservations: List[dict], token: str) -> dict:
    if not token:
        raise BookingError("job number or short id is required")
    if token.isdigit():
        index = int(token)
        if 1 <= index <= len(reservations):
            return reservations[index - 1]
    matches = [item for item in reservations if str(item.get("id", "")).startswith(token)]
    if not matches:
        raise BookingError(f"job not found for current user: {token}")
    if len(matches) > 1:
        raise BookingError(f"ambiguous job id {token}")
    return matches[0]


def _prompt_reservation_token(store: LedgerStore, actor: Actor, action: str) -> str:
    mine = _own_active_reservations(store, actor)
    if not mine:
        raise BookingError("you have no active reservations")
    print("Your active reservations:")
    for index, reservation in enumerate(mine, 1):
        print(
            f"  {index}. {_short_id(reservation)} {reservation['mode']} "
            f"GPU={','.join(map(str, reservation.get('gpus', [])))} "
            f"{format_local_range(reservation['start_at'], reservation['end_at'])}"
        )
    token = input(f"reservation to {action} (number or short id): ").strip()
    if not token:
        raise BookingError("reservation id is required")
    return token


def _resolve_own_reservation_id(store: LedgerStore, token: str, actor: Actor) -> str:
    mine = _own_active_reservations(store, actor)
    if token.isdigit():
        index = int(token)
        if 1 <= index <= len(mine):
            return mine[index - 1]["id"]
    matches = [item for item in mine if str(item.get("id", "")).startswith(token)]
    if not matches:
        raise BookingError(f"reservation not found for current user: {token}")
    if len(matches) > 1:
        choices = ", ".join(_short_id(item) for item in matches)
        raise BookingError(f"ambiguous reservation id {token}; matches: {choices}")
    return matches[0]["id"]


def _get_reservation(store: LedgerStore, reservation_id: str) -> dict:
    for reservation in list_active(store.load()):
        if reservation.get("id") == reservation_id:
            return reservation
    raise BookingError("reservation not found")


def _print_help(file=None) -> None:
    file = file or sys.stdout
    print(
        """usage:
  bk
  bk <count> <duration> [--gpu 0,1] [--start ISO]
  bk <count> <duration> -- <command> [args...]
  bk s <count> <duration>       shared (shared/auto also accepted)
  bk x <count> <duration>       exclusive
  bk t                          TUI
  bk m [--once]                 monitor
  bk u [--rollups]              usage records
  bk w                          run this UID's scheduled jobs
  bk j                          list this UID's jobs
  bk jl <number_or_short_id>    show a job log
  bk jr <number_or_short_id>    retry a failed job
  bk agent context              machine-readable resource context
  bk agent recommend 2 1h30m   read-only placement recommendation
  bk mcp                        run optional local stdio MCP server
  bk skill install              install bundled Codex skill
  bk service install worker     install, but do not enable, a user unit
  bk a                          guided add
  bk e [number_or_short_id]     edit
  bk d <number_or_short_id>     delete
  bk l                          list
  bk reset --yes
  bk list
  bk log
  bk doctor

duration examples: 30m, 1h30m, 1d
shared memory: --mem 12g (expected memory per GPU)
agent writes: add --op-id <unique-key> --json
default mode: shared
omitted --start: queue to the earliest available slot
explicit --start: exact time, no automatic move
default interaction: plain prompt, no fullscreen terminal takeover
""",
        file=file,
    )


def _print_shell_help() -> None:
    print(
        """Commands:
  st | status               show GPU summary and active reservations
  1 4h [--gpu 0]            shared booking, default mode
  s 1 4h [--gpu 0]          shared booking
  x 1 4h [--gpu 0]          exclusive booking
  a | add                   guided booking prompts
  e <number|short_id>       modify your reservation
  d <number|short_id>       cancel your reservation
  l | list                  list active reservations
  lg | log                  show your operation log
  dr | doctor               report policy violations in the ledger
  m | monitor               continuously audit GPU process usage
  u [--rollups]             show recent usage events or minute rollups
  w | worker                execute only this UID's due jobs
  j | jobs                  list scheduled job states
  jl <number|short_id>      show a job log
  jr <number|short_id>      retry a failed job
  agent context             emit stable allocation context JSON
  agent recommend 2 1h30m  compute a read-only legal placement
  reset --yes               clear ledger, logs, and backups in this data dir
  t | tui                   open full-screen TUI
  quit                      exit
"""
    )


def _print_status(config: Config, store: LedgerStore) -> None:
    now = utc_now()
    active = list_active(store.load(), now)
    gpu_snapshots = snapshot(config)
    usage_by_gpu = classify_process_usage(gpu_snapshots, active, now)
    print("GPU summary")
    for gpu in gpu_snapshots:
        if gpu.memory_total_mb:
            mem = f"{gpu.memory_used_mb}/{gpu.memory_total_mb} MiB"
        else:
            mem = "unknown"
        util = f"{gpu.utilization_percent}%" if gpu.utilization_percent is not None else "unknown"
        rows = usage_by_gpu.get(gpu.index, [])
        violations = sum(1 for item in rows if item.violation)
        print(
            f"  GPU {gpu.index}: {gpu.name} util={util} mem={mem} "
            f"processes={len(rows)} violations={violations} source={gpu.source}"
        )
        for item in rows:
            process = item.process
            sm = f"{process.sm_utilization_percent}%" if process.sm_utilization_percent is not None else "-"
            print(
                f"    pid={process.pid} uid={process.uid if process.uid is not None else '?'} "
                f"user={process.username} sm={sm} mem={process.gpu_memory_mb}MiB "
                f"state={item.status} cmd={summarize_process_command(process.command)}"
            )

    print()
    _print_timeline(config, store)
    print("Active reservations")
    if not active:
        print("  none")
    else:
        actor = _current_actor()
        mine = _own_active_reservations(store, actor)
        mine_index = {reservation["id"]: index + 1 for index, reservation in enumerate(mine)}
        for reservation in active:
            gpus = ",".join(str(item) for item in reservation.get("gpus", []))
            index = mine_index.get(reservation["id"], "-")
            print(
                f"  {index:>2} {_short_id(reservation)} {reservation['mode']:<9} "
                f"GPU={gpus:<7} {reservation['username']} "
                f"job={reservation.get('job', {}).get('status', '-'):<10} "
                f"{format_local_range(reservation['start_at'], reservation['end_at'])}"
            )
    print()


def _print_timeline(config: Config, store: LedgerStore) -> None:
    now = utc_now()
    hours = min(config.timeline_hours, 24)
    active = list_active(store.load(), now)
    actor = _current_actor()
    print(f"Timeline (next {hours}h, local)")
    label = "      " + " ".join((now + timedelta(hours=i)).astimezone().strftime("%H") for i in range(hours))
    print(label)
    for gpu in range(config.gpu_count):
        cells = []
        for offset in range(hours):
            slot_start = now + timedelta(hours=offset)
            slot_end = slot_start + timedelta(hours=1)
            overlapping = [
                item
                for item in active
                if gpu in item.get("gpus", [])
                and parse_iso(item["start_at"]) < slot_end
                and slot_start < parse_iso(item["end_at"])
            ]
            if not overlapping:
                cells.append(".")
            elif any(int(item.get("uid")) == actor.uid for item in overlapping):
                cells.append("M")
            elif any(item.get("mode") == MODE_EXCLUSIVE for item in overlapping):
                cells.append("X")
            else:
                records = [item for item in overlapping if item.get("mode") == MODE_SHARED]
                cells.append(str(min(len(records), 9)))
        print(f"GPU{gpu:<2} " + " ".join(cells))
    print("Legend: . free, M mine, X exclusive, 1-9 shared record count")
    print()
