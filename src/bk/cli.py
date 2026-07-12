from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from datetime import datetime, timedelta, timezone
from itertools import combinations, islice
from pathlib import Path
from typing import List, Optional

from . import __version__
from .advisor import GpuAdvice, build_gpu_advice
from .config import CONFIG_ENV_MAP, CONFIG_VERSION, Config, load_config
from .fileio import open_existing_regular
from .gpu import snapshot
from .identity import current_actor
from .joblogs import WorkerBusyError, JobLogCleanupResult, cleanup_job_logs, job_log_paths
from .monitor import (
    MONITOR_AUTH_EXIT_CODE,
    MONITOR_BUSY_EXIT_CODE,
    MonitorAuthorizationError,
    MonitorBusyError,
    authorize_monitor,
    monitor_configuration_error,
    run_monitor,
)
from .models import MODE_EXCLUSIVE, MODE_SHARED, Actor, BookingError
from .policy import validate_ledger_policy
from .scheduler import (
    find_earliest_slot,
    find_policy_violations,
    list_active,
    shared_memory_headroom_for_reservation,
)
from .sharing import (
    inferred_share_memory_mb,
    parse_share_units,
    reservation_share_units,
    share_example,
    share_text,
    share_units_for_peer_limit,
)
from .service import (
    AGENT_SCHEMA_VERSION,
    BookingSubmission,
    booking_result_payload,
    build_agent_context,
    public_reservation,
    recommend_booking,
    scheduled_job_worker_warning,
    submit_booking,
    submit_cancellation,
    submit_edit,
)
from .storage import AUDIT_SCHEMA_VERSION, LedgerStore
from .timeparse import (
    format_local,
    format_local_range,
    parse_duration_seconds,
    parse_friendly_start,
    parse_iso,
    parse_memory_mb,
    parse_start,
    utc_now,
)
from .tui import run_tui
from .usage import USAGE_SYSTEM, assess_gpu_live_states, classify_process_usage, summarize_process_command
from .usage_cli import run_usage_cli
from .usage_store import UsageAuditStore
from .worker import (
    JobSpecCleanupResult,
    WORKER_BUSY_EXIT_CODE,
    WORKER_WAITING_EXIT_CODE,
    cleanup_job_specs,
    retry_job,
    run_worker,
)
from .worker_status import inspect_worker_status, reservations_need_worker

try:
    import readline  # noqa: F401
except ImportError:
    pass


TIMELINE_CELL_WIDTH = 3
TIMELINE_DEFAULT_WINDOW_SECONDS = 2 * 60 * 60
TIMELINE_MAX_SLOTS = 240
TIMELINE_AUTO_STEPS = (300, 600, 900, 1800, 3600, 7200, 14400, 28800, 43200, 86400)
TIMELINE_AUTO_FACTORS = (1, 2, 3, 4, 6, 12, 24, 48, 96, 144, 288)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv:
            head = argv[0]
            if head in {"-h", "--help", "help"}:
                _print_help()
                return 0
            if head in {"-V", "--version", "version"}:
                print(f"bk {__version__}")
                return 0
            if head == "skill":
                return _skill_command(argv[1:])

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
            return run_usage_cli(argv[1:], config)
        if head in {"worker", "w"}:
            return _worker_command(argv[1:], config, store)
        if head in {"jobs", "j"}:
            return _jobs_command(argv[1:], config, store)
        if head in {"job-log", "jl"}:
            return _job_log_command(argv[1:], config, store)
        if head in {"job-retry", "jr"}:
            return _job_retry_command(argv[1:], store)
        if head in {"status", "st"}:
            return _status_command(argv[1:], config, store)
        if head in {"timeline", "tl"}:
            return _status_command(argv[1:], config, store, timeline_only=True)
        if head in {"slots", "slot", "free", "sl"}:
            return _slots_command(argv[1:], config, store)
        if head in {"agent", "ai"}:
            return _agent_command(argv[1:], config, store)
        if head == "mcp":
            from .mcp_server import main as mcp_main

            mcp_main()
            return 0
        if head == "service":
            return _service_command(argv[1:], config)
        if head in {"config", "cfg"}:
            return _config_command(argv[1:], config, store)
        if head in {"add", "a"}:
            return _add_interactive(config, store)
        if head in {"edit", "e"}:
            return _edit_command(argv[1:], config, store)
        if head in {"del", "delete", "d", "rm"}:
            return _delete_command(argv[1:], config, store)
        if head == "reset":
            return _reset_command(argv[1:], config, store)
        if head in {"log", "lg"}:
            return _log_command(argv[1:], store)
        if head in {"doctor", "dr"}:
            return _doctor_command(argv[1:], config, store)
        if head in {"list", "ls", "l"}:
            return _list_command(argv[1:], config, store)
        print(f"未知命令: {head}", file=sys.stderr)
        _print_help(file=sys.stderr)
        return 2
    except MonitorBusyError as exc:
        print(f"bk: {exc}", file=sys.stderr)
        return MONITOR_BUSY_EXIT_CODE
    except MonitorAuthorizationError as exc:
        print(f"bk: {exc}", file=sys.stderr)
        return MONITOR_AUTH_EXIT_CODE
    except WorkerBusyError as exc:
        print(f"bk: {exc}", file=sys.stderr)
        return WORKER_BUSY_EXIT_CODE
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
    print(f"booking slice: {config.slot_minutes} minutes")
    print(f"shared capacity: {config.max_shared_users} units per GPU (default booking: 1 unit)")
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
    if head in {"status", "refresh", "r", "st"}:
        _status_command(args[1:], config, store)
        return True
    if head in {"timeline", "tl"}:
        _status_command(args[1:], config, store, timeline_only=True)
        return True
    if head in {"slots", "slot", "free", "sl"}:
        _slots_command(args[1:], config, store)
        return True
    if head in {"list", "ls", "l"}:
        _list_command(args[1:], config, store)
        return True
    if head in {"log", "logs", "lg"}:
        _log_command(args[1:], store)
        return True
    if head in {"doctor", "dr"}:
        _doctor_command(args[1:], config, store)
        return True
    if head in {"config", "cfg"}:
        _config_command(args[1:], config, store)
        return True
    if head in {"del", "delete", "cancel", "d", "rm"}:
        _delete_command(args[1:], config, store)
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
        run_usage_cli(args[1:], config)
        return True
    if head in {"worker", "w"}:
        _worker_command(args[1:], config, store)
        return True
    if head in {"jobs", "j"}:
        _jobs_command(args[1:], config, store)
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
    start_group = parser.add_mutually_exclusive_group()
    start_group.add_argument(
        "--start",
        help="exact ISO time, e.g. 2030-01-01T20:00:00+08:00; omitted means now with queueing",
    )
    start_group.add_argument(
        "--at",
        help="exact local-friendly time: +30m, 20:00, 'tomorrow 09:00', or 07-13 20:00",
    )
    parser.add_argument("--gpu", help="comma separated GPU indexes, for example 0,1")
    parser.add_argument("--mem", help="expected memory on each GPU, for example 12g or 4096m")
    _add_share_arguments(parser, config)
    parser.add_argument("--op-id", help="idempotency key for agents and retry-safe scripts")
    parser.add_argument("--json", action="store_true", help="emit a stable machine-readable result")
    parser.add_argument("-v", "--verbose", action="store_true", help="show placement scores and load history")
    parser.add_argument("-q", "--quiet", action="store_true", help="print only the booking result line")
    args = parser.parse_args(booking_argv)

    preferred = _parse_gpu_list(args.gpu) if args.gpu else None
    expected_memory_mb = parse_memory_mb(args.mem) if args.mem else None
    share_units = _share_units_from_args(args, config, mode)
    duration_seconds = parse_duration_seconds(args.duration)
    if args.at is not None:
        start_at = parse_friendly_start(args.at, slot_minutes=config.slot_minutes)
    elif args.start is not None:
        start_at = parse_start(args.start)
    else:
        start_at = utc_now()
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
        allow_queue=args.start is None and args.at is None,
        operation_id=args.op_id,
        command_argv=command_argv,
        working_directory=os.getcwd() if command_argv is not None else None,
        expected_memory_mb=expected_memory_mb,
        share_units=share_units,
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
                booking_result_payload(status, submission, actor, store.last_warning),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    gpus = ",".join(str(item) for item in reservation["gpus"])
    print(
        f"{status}: {_short_id(reservation)} mode={reservation['mode']} "
        f"{_reservation_share_label(reservation, config)}"
        f"gpu={gpus} {format_local_range(reservation['start_at'], reservation['end_at'])}"
    )
    if not args.quiet:
        if args.share_with is not None:
            print(
                f"share: --share-with {args.share_with} reserved "
                f"{share_text(share_units or 1, config.max_shared_users)} per GPU; "
                "this is admission weight, not enforced compute bandwidth"
            )
        _print_booking_advice(
            config,
            store,
            reservation,
            advice,
            expected_memory_mb,
            verbose=args.verbose,
        )
    if not args.quiet:
        if allocator.source == "external":
            print(f"allocator: external{f' ({allocator.reason})' if allocator.reason else ''}")
        elif allocator.warning:
            print(f"warning: {allocator.warning}", file=sys.stderr)
    if isinstance(reservation.get("job"), dict):
        if not args.quiet:
            print(
                f"job: {reservation['job'].get('status')} "
                f"command={reservation['job'].get('summary', 'private command')}"
            )
        _print_scheduled_job_worker(submission, quiet=args.quiet)
    if store.last_warning:
        print(f"warning: {store.last_warning}", file=sys.stderr)
    return 0


def _slots_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    positional_mode = None
    if argv and argv[0] in {"s", "shared", "x", "exclusive"}:
        positional_mode = argv.pop(0)
    parser = argparse.ArgumentParser(prog="bk slots")
    parser.add_argument("count", type=int)
    parser.add_argument("duration")
    parser.add_argument("--mode", choices=["s", "shared", "x", "exclusive"])
    parser.add_argument(
        "--from",
        dest="start",
        default="now",
        help="earliest start: now, +30m, 20:00, 'tomorrow 09:00', or ISO",
    )
    parser.add_argument("--gpu", help="restrict the search to one exact GPU set, e.g. 0,1")
    parser.add_argument("--mem", help="expected VRAM on each GPU, e.g. 12g")
    _add_share_arguments(parser, config)
    parser.add_argument("--limit", type=int, default=5, help="number of alternatives to show (default: 5)")
    args = parser.parse_args(argv)

    if args.count < 1 or args.count > config.gpu_count:
        raise ValueError(f"GPU count must be between 1 and {config.gpu_count}")
    if args.limit < 1 or args.limit > 20:
        raise ValueError("--limit must be between 1 and 20")
    duration_seconds = parse_duration_seconds(args.duration)
    if duration_seconds % config.slot_seconds:
        raise ValueError(f"duration must be a multiple of {config.slot_minutes} minutes")
    mode_value = args.mode or positional_mode or "shared"
    mode = MODE_EXCLUSIVE if mode_value in {"x", "exclusive"} else MODE_SHARED
    start = parse_friendly_start(args.start, slot_minutes=config.slot_minutes)
    expected_memory_mb = parse_memory_mb(args.mem) if args.mem else None
    share_units = _share_units_from_args(args, config, mode)
    preferred = _parse_gpu_list(args.gpu) if args.gpu else None
    if preferred is not None:
        if len(preferred) != args.count:
            raise ValueError("--gpu count must match requested GPU count")
        invalid = [gpu for gpu in preferred if gpu < 0 or gpu >= config.gpu_count]
        if invalid:
            raise ValueError(f"GPU IDs must be between 0 and {config.gpu_count - 1}")

    advice = build_gpu_advice(config)
    ledger = store.load()
    duration = timedelta(seconds=duration_seconds)
    actor = _current_actor()
    evaluated_limit = 2048
    if preferred is not None:
        gpu_sets = [tuple(preferred)]
        truncated = False
    else:
        all_sets = combinations(advice.order, args.count)
        gpu_sets = list(islice(all_sets, evaluated_limit + 1))
        truncated = len(gpu_sets) > evaluated_limit
        gpu_sets = gpu_sets[:evaluated_limit]

    snapshots = {item.index: item for item in advice.snapshots}
    options = []
    for gpu_set in gpu_sets:
        slot = find_earliest_slot(
            ledger,
            config,
            args.count,
            start,
            duration,
            mode,
            actor.uid,
            preferred_gpus=gpu_set,
            allow_queue=True,
            gpu_order=advice.order,
            gpu_scores=advice.scores,
            expected_memory_mb=expected_memory_mb,
            gpu_memory_capacity_mb=advice.memory_capacities_mb,
            share_units=share_units or 1,
        )
        if slot is None:
            continue
        scheduled_start, gpus = slot
        score = sum(advice.scores[gpu] for gpu in gpus)
        options.append((scheduled_start, score, tuple(gpus)))

    options.sort(key=lambda item: (item[0], item[1], item[2]))
    options = options[: args.limit]
    if not options:
        print(f"No legal {mode} slot found in the next {config.queue_search_hours}h.")
        return 3

    print(
        f"Earliest {mode} options | {args.count} GPU | {_duration_compact(duration_seconds)} "
        f"{f'| share {share_text(share_units or 1, config.max_shared_users)} ' if mode == MODE_SHARED else ''}"
        f"| local time | read-only"
    )
    wide_slots = shutil.get_terminal_size(fallback=(100, 24)).columns >= 88
    if wide_slots:
        print(f"{'#':>2} {'GPUs':<12} {'Start':<22} {'End':<22} {'Live':<9} {'Free now':>10}")
    else:
        print(f"{'#':>2} {'GPUs':<8} {'Start':<11} {'End':<11} {'Live':<7} {'Free':>8}")
    for index, (scheduled_start, _score, gpus) in enumerate(options, 1):
        scheduled_end = scheduled_start + duration
        states = [advice.live_states[gpu].status for gpu in gpus]
        live = "idle" if all(state == "idle" for state in states) else ("busy" if "busy" in states else "unknown")
        free_values = [
            max(0, snapshots[gpu].memory_total_mb - snapshots[gpu].memory_used_mb)
            for gpu in gpus
            if gpu in snapshots and snapshots[gpu].memory_total_mb
        ]
        free_text = _format_memory_mb(min(free_values)) if free_values else "unknown"
        gpu_text = ",".join(map(str, gpus))
        if wide_slots:
            print(
                f"{index:>2} {gpu_text:<12} "
                f"{format_local(scheduled_start):<22} {format_local(scheduled_end):<22} "
                f"{live:<9} {free_text:>10}"
            )
        else:
            print(
                f"{index:>2} {_clip_text(gpu_text, 8):<8} "
                f"{scheduled_start.astimezone():%m-%d %H:%M} {scheduled_end.astimezone():%m-%d %H:%M} "
                f"{live:<7} {_clip_text(free_text, 8):>8}"
            )
    if truncated:
        print(f"note: evaluated the best {evaluated_limit} GPU combinations")
    first_start, _first_score, first_gpus = options[0]
    mode_prefix = "x " if mode == MODE_EXCLUSIVE else ""
    first_gpu_text = ",".join(map(str, first_gpus))
    first_at = first_start.astimezone().strftime("%m-%d %H:%M")
    memory_arg = f" --mem {args.mem}" if args.mem else ""
    share_arg = (
        f" --share {share_text(share_units, config.max_shared_users)}"
        if mode == MODE_SHARED and share_units is not None
        else ""
    )
    print(
        f"Book option 1: bk {mode_prefix}{args.count} {_duration_compact(duration_seconds)} "
        f"--gpu {first_gpu_text} --at \"{first_at}\"{memory_arg}{share_arg}"
    )
    return 0


def _monitor_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk monitor")
    parser.add_argument(
        "--interval",
        type=float,
        help=(
            "sampling interval in seconds "
            f"(configured default: {config.monitor_interval_seconds:g})"
        ),
    )
    parser.add_argument(
        "--rollup",
        type=int,
        help=(
            "rollup window in seconds "
            f"(configured default: {config.monitor_rollup_seconds})"
        ),
    )
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


def _worker_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(
        prog="bk worker",
        epilog="exit 3: due work is waiting; exit 75: another worker holds this UID's lease",
    )
    parser.add_argument("--once", action="store_true", help="run due jobs, wait for them, then exit")
    parser.add_argument("--poll", type=float, help="poll interval in seconds")
    parser.add_argument("--max-parallel", type=int, help="maximum child jobs for this worker")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--status",
        action="store_true",
        help="inspect this UID's worker lease without starting a worker",
    )
    parser.add_argument("--json", action="store_true", help="emit status as versioned JSON")
    parser.add_argument(
        "--require-running",
        action="store_true",
        help="inspect status and return 2 unless a worker holds the lease",
    )
    args = parser.parse_args(argv)
    status_mode = args.status or args.require_running
    if args.json and not status_mode:
        parser.error("--json requires --status or --require-running")
    if status_mode:
        if args.once or args.poll is not None or args.max_parallel is not None or args.quiet:
            parser.error("worker run options cannot be combined with status inspection")
        status = inspect_worker_status(config, _current_actor())
        if args.json:
            print(json.dumps(status, ensure_ascii=False, sort_keys=True))
        else:
            print(_worker_status_line(status))
        return 0 if not args.require_running or status.get("running") is True else 2
    summary = run_worker(
        config,
        store,
        _current_actor(),
        once=args.once,
        poll_seconds=args.poll,
        max_parallel=args.max_parallel,
        quiet=args.quiet,
    )
    if summary.failed:
        return 1
    if args.once and summary.waiting:
        return WORKER_WAITING_EXIT_CODE
    return 0


def _jobs_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk jobs")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="prune this UID's private command specs and bounded job logs",
    )
    args = parser.parse_args(argv)
    actor = _current_actor()
    worker_status = inspect_worker_status(config, actor)
    spec_cleanup = None
    log_cleanup = None
    if args.cleanup:
        try:
            spec_cleanup = cleanup_job_specs(config, store, actor)
        except (BookingError, OSError, ValueError) as exc:
            spec_cleanup = JobSpecCleanupResult(
                failed=1,
                warnings=(f"private job spec cleanup failed: {exc}",),
            )
        try:
            log_cleanup = cleanup_job_logs(config, store.load(), actor)
        except (BookingError, OSError, ValueError) as exc:
            log_cleanup = JobLogCleanupResult(
                failed=1,
                warnings=(f"private job log cleanup failed: {exc}",),
            )
    reservations = _own_job_reservations(store, actor)
    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": AGENT_SCHEMA_VERSION,
                    "kind": "jobs",
                    "worker": worker_status,
                    "jobs": [
                        public_reservation(item, actor, config.max_shared_users)
                        for item in reservations
                    ],
                    "private_job_cleanup": (
                        spec_cleanup.as_dict() if spec_cleanup is not None else None
                    ),
                    "private_job_log_cleanup": (
                        log_cleanup.as_dict() if log_cleanup is not None else None
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2 if any(item is not None and item.failed for item in (spec_cleanup, log_cleanup)) else 0
    print(_worker_status_line(worker_status))
    if spec_cleanup is not None:
        print(
            f"spec cleanup: removed={spec_cleanup.removed} retained={spec_cleanup.retained} "
            f"deferred={spec_cleanup.deferred_orphans} failed={spec_cleanup.failed}"
        )
        for warning in spec_cleanup.warnings:
            print(f"warning: {warning}", file=sys.stderr)
    if log_cleanup is not None:
        print(
            f"log cleanup: removed={log_cleanup.removed} retained={log_cleanup.retained} "
            f"kept={_format_bytes(log_cleanup.bytes_retained)} failed={log_cleanup.failed}"
        )
        for warning in log_cleanup.warnings:
            print(f"warning: {warning}", file=sys.stderr)
    if not reservations:
        print("No jobs.")
        return 2 if any(item is not None and item.failed for item in (spec_cleanup, log_cleanup)) else 0
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
        if job.get("message"):
            print(f"   note: {_clip_text(str(job['message']), 88)}")
    return 2 if any(item is not None and item.failed for item in (spec_cleanup, log_cleanup)) else 0


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
    paths = job_log_paths(config, str(reservation["id"]))
    if not paths:
        reason = (
            f"removed by the {config.job_log_retention_days}-day retention policy"
            if config.job_log_retention_days
            else "removed manually"
        )
        print(f"job log unavailable: it has not been created or was {reason}")
        return 0
    for path in paths:
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
    recommend_parser.add_argument(
        "--start",
        help="exact ISO start, e.g. 2030-01-01T20:00:00+08:00; omitted means earliest slot",
    )
    recommend_parser.add_argument("--gpu", help="fixed comma-separated GPU indexes")
    recommend_parser.add_argument("--mem", help="expected memory per GPU, e.g. 12g")
    _add_share_arguments(recommend_parser, config)
    recommend_parser.add_argument("--compact", action="store_true")
    edit_parser = subparsers.add_parser("edit", aliases=["e"], help="idempotently edit this UID's reservation")
    edit_parser.add_argument("reservation_id", help="reservation number or unique ID prefix")
    edit_parser.add_argument("--op-id", help="stable retry-safe operation ID (required)")
    edit_parser.add_argument("--duration")
    edit_parser.add_argument(
        "--start",
        help="exact ISO start, e.g. 2030-01-01T20:00:00+08:00, unless --queue is used",
    )
    edit_parser.add_argument("--gpu", help="fixed comma-separated GPU indexes")
    edit_parser.add_argument("--count", type=int, help="new GPU count with automatic selection")
    edit_parser.add_argument("--mode", choices=["s", "shared", "x", "exclusive"])
    edit_parser.add_argument("--mem", help="new expected memory per GPU; use - to clear")
    _add_share_arguments(edit_parser, config)
    edit_parser.add_argument("--queue", action="store_true", help="allow moving to the next legal slot")
    edit_parser.add_argument("--compact", action="store_true")
    cancel_parser = subparsers.add_parser("cancel", aliases=["del"], help="cancel this UID's reservation")
    cancel_parser.add_argument("reservation_id", help="reservation number or unique ID prefix")
    cancel_parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    actor = _current_actor()
    try:
        if args.action in {"context", "ctx"}:
            payload = build_agent_context(config, store, actor)
            compact = args.compact
            exit_code = 0
        elif args.action in {"recommend", "rec"}:
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
                share_units=_share_units_from_args(args, config, mode),
                allow_queue=args.start is None,
            )
            compact = args.compact
            exit_code = 0 if payload["available"] else 3
        elif args.action in {"edit", "e"}:
            if not args.op_id:
                raise BookingError("operation ID is required for retry-safe Agent edits")
            if not any(
                [
                    args.duration,
                    args.start,
                    args.gpu,
                    args.count,
                    args.mode,
                    args.mem,
                    args.share,
                    args.share_with,
                ]
            ):
                raise BookingError("edit requires at least one changed field")
            reservation_id = _resolve_own_retained_reservation_id(store, args.reservation_id, actor)
            edit_mode = None
            if args.mode in {"x", "exclusive"}:
                edit_mode = MODE_EXCLUSIVE
            elif args.mode in {"s", "shared"}:
                edit_mode = MODE_SHARED
            share_units = _share_units_from_args(args, config, edit_mode)
            expected_memory_mb = None
            if args.mem not in {None, "-"}:
                expected_memory_mb = parse_memory_mb(args.mem)
            submission = submit_edit(
                config,
                store,
                actor,
                reservation_id,
                duration_seconds=parse_duration_seconds(args.duration) if args.duration else None,
                start_at=parse_start(args.start) if args.start else None,
                mode=edit_mode,
                preferred_gpus=_parse_gpu_list(args.gpu) if args.gpu else None,
                count=args.count,
                expected_memory_mb=expected_memory_mb,
                update_expected_memory=args.mem is not None,
                share_units=share_units,
                update_share_units=args.share is not None or args.share_with is not None,
                allow_queue=args.queue,
                operation_id=args.op_id,
            )
            result = submission.result
            status = "exists" if not result.created else ("queued" if result.queued else "updated")
            payload = booking_result_payload(
                status,
                submission,
                actor,
                store.last_warning,
            )
            compact = args.compact
            exit_code = 0
        else:
            reservation_id = _resolve_own_reservation_id(store, args.reservation_id, actor)
            cancellation = submit_cancellation(config, store, actor, reservation_id)
            reservation = cancellation.reservation
            payload = {
                "schema_version": AGENT_SCHEMA_VERSION,
                "kind": "cancellation_result",
                "reservation": public_reservation(
                    reservation, actor, config.max_shared_users
                ),
                "private_job_cleanup": cancellation.cleanup.as_dict(),
                "warning": store.last_warning,
            }
            compact = args.compact
            exit_code = 0
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


def _service_command(argv: List[str], config: Config) -> int:
    from .systemd import install_user_unit, service_environment, unit_text

    parser = argparse.ArgumentParser(prog="bk service")
    subparsers = parser.add_subparsers(dest="action", required=True)
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("kind", choices=["monitor", "worker"])
    install_parser.add_argument("--target-dir", type=Path)
    install_parser.add_argument("--force", action="store_true")
    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("kind", choices=["monitor", "worker"])
    args = parser.parse_args(argv)
    environment = service_environment(config, args.kind)
    if args.action == "show":
        print(unit_text(args.kind, environment=environment), end="")
        return 0
    if args.kind == "monitor":
        authorize_monitor(config)
    path = install_user_unit(
        args.kind,
        args.target_dir,
        environment=environment,
        force=args.force,
    )
    print(f"installed unit: {path}")
    print(f"captured data directory: {environment['BK_DATA_DIR']}")
    if "BK_CONFIG_FILE" in environment:
        print(f"captured config file: {environment['BK_CONFIG_FILE']}")
    if "BK_JOB_LOG_DIR" in environment:
        print(f"captured job log directory: {environment['BK_JOB_LOG_DIR']}")
    print("not enabled or started; review it, then run systemctl --user daemon-reload")
    if args.kind == "monitor":
        print("shared server note: run exactly one trusted monitor writer; do not enable one per user")
        print("after starting it, verify: bk doctor --require-monitor --strict")
    else:
        print("after starting it, verify: bk worker --status --require-running")
    username = shlex.quote(_current_actor().username)
    print(
        "Linux boot/logout persistence (optional, admin): "
        f"sudo loginctl enable-linger {username}"
    )
    return 0


def _worker_status_line(status: dict) -> str:
    state = str(status.get("state", "invalid"))
    lease = status.get("lease")
    if state == "running" and isinstance(lease, dict):
        return (
            f"worker: running (lease held) recorded-pid={lease.get('pid')} "
            f"host={lease.get('hostname')} "
            f"since={format_local(str(lease.get('acquired_at')))}"
        )
    if state == "running":
        suffix = f"; {status['warning']}" if status.get("warning") else ""
        return f"worker: running (kernel lease held; metadata unavailable{suffix})"
    if state == "stopped" and isinstance(lease, dict):
        return (
            f"worker: stopped (last pid={lease.get('pid')} host={lease.get('hostname')} "
            f"since={format_local(str(lease.get('acquired_at')))})"
        )
    if state == "stopped":
        suffix = f": {status['warning']}" if status.get("warning") else ""
        return f"worker: stopped{suffix}"
    if state == "not-seen":
        return "worker: not seen (start `bk w` to launch scheduled commands)"
    warning = str(status.get("warning") or "status unavailable")
    return f"worker: {state}: {warning}"


def _print_scheduled_job_worker(
    submission: BookingSubmission,
    *,
    quiet: bool = False,
) -> None:
    status = submission.worker_status
    if status is None:
        return
    if not quiet:
        print(_worker_status_line(status))
    warning = scheduled_job_worker_warning(status)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)


def _config_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk config")
    parser.add_argument("--json", action="store_true", help="emit a stable machine-readable report")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    args = parser.parse_args(argv)
    config_path = config.config_path
    policy = {
        "status": "unbound",
        "bound": False,
        "matches": None,
        "message": None,
    }
    warning = None
    try:
        ledger = store.load_read_only()
        warning = store.last_warning
        if ledger.get("policy") is not None:
            policy["bound"] = True
            try:
                validate_ledger_policy(ledger, config)
                policy["status"] = "match"
                policy["matches"] = True
            except BookingError as exc:
                policy["status"] = "mismatch"
                policy["matches"] = False
                policy["message"] = str(exc)
    except (BookingError, OSError, ValueError) as exc:
        policy["status"] = "unreadable"
        policy["matches"] = False
        policy["message"] = f"{type(exc).__name__}: {exc}"

    environment_names = {
        "BK_DATA_DIR",
        "BK_CONFIG_FILE",
        "BK_JOB_LOG_DIR",
        "BK_ALLOCATOR_COMMAND",
        "BK_ALLOCATOR_TIMEOUT_SECONDS",
        "BK_ALLOCATOR_WEIGHT",
        *CONFIG_ENV_MAP.values(),
    }
    report = {
        "schema_version": "gpubk.config.v1",
        "kind": "configuration",
        "config_version": CONFIG_VERSION,
        "config_file": {
            "path": str(config_path),
            "present": os.path.lexists(config_path),
            "owner_uid": config.config_owner_uid,
        },
        "environment_overrides": sorted(name for name in environment_names if name in os.environ),
        "effective": _effective_config(config),
        "ledger_policy": policy,
        "warning": warning,
    }
    healthy = policy["status"] in {"unbound", "match"}
    if args.json or args.compact:
        print(
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                indent=None if args.compact else 2,
                separators=(",", ":") if args.compact else None,
            )
        )
        return 0 if healthy else 2

    effective = report["effective"]
    print("GPUbk configuration")
    print(
        f"source: {config_path} "
        f"({'present' if report['config_file']['present'] else 'defaults/environment'})"
    )
    overrides = ", ".join(report["environment_overrides"]) or "none"
    print(f"environment overrides: {overrides}")
    print(
        f"scheduling: GPUs={effective['gpu_count']} slice={effective['slot_minutes']}m "
        f"shared={effective['max_shared_users']} queue={effective['queue_search_hours']}h"
    )
    print(
        f"storage: data={effective['data_dir']} modes={effective['file_mode']}/"
        f"{effective['dir_mode']} backups={effective['backup_keep']}"
    )
    print(
        f"worker: poll={effective['worker_poll_seconds']}s "
        f"claim={effective['worker_claim_timeout_seconds']}s "
        f"live-guard={'on' if effective['worker_live_guard'] else 'off'}"
    )
    print(
        f"monitor: uid={effective['monitor_uid'] if effective['monitor_uid'] is not None else 'local'} "
        f"sample={effective['monitor_interval_seconds']}s "
        f"rollup={effective['monitor_rollup_seconds']}s"
    )
    print(
        f"display: timeline={effective['timeline_hours']}h "
        f"tui-refresh={effective['tui_refresh_seconds']}s"
    )
    print(
        f"allocator: {'configured' if effective['allocator_command_configured'] else 'builtin'} "
        f"timeout={effective['allocator_timeout_seconds']}s"
    )
    policy_text = str(policy["status"])
    if policy["message"]:
        policy_text += f" ({policy['message']})"
    print(f"ledger policy: {policy_text}")
    if warning:
        print(f"warning: {warning}")
    print("Use `bk config --json` for every effective setting.")
    return 0 if healthy else 2


def _effective_config(config: Config) -> dict:
    return {
        "data_dir": str(config.data_dir),
        "config_file": str(config.config_path),
        "gpu_count": config.gpu_count,
        "slot_minutes": config.slot_minutes,
        "max_shared_users": config.max_shared_users,
        "queue_search_hours": config.queue_search_hours,
        "ledger_retention_days": config.ledger_retention_days,
        "usage_load_window_minutes": config.usage_load_window_minutes,
        "usage_minute_retention_days": config.usage_minute_retention_days,
        "usage_five_minute_retention_days": config.usage_five_minute_retention_days,
        "usage_ten_minute_retention_days": config.usage_ten_minute_retention_days,
        "usage_hourly_retention_days": config.usage_hourly_retention_days,
        "usage_daily_retention_days": config.usage_daily_retention_days,
        "usage_event_retention_days": config.usage_event_retention_days,
        "lock_timeout_seconds": config.lock_timeout_seconds,
        "backup_keep": config.backup_keep,
        "timeline_hours": config.timeline_hours,
        "require_shared_memory": config.require_shared_memory,
        "shared_memory_reserve_mb": config.shared_memory_reserve_mb,
        "job_log_dir": str(config.job_log_dir) if config.job_log_dir is not None else None,
        "job_log_retention_days": config.job_log_retention_days,
        "job_log_max_mb": config.job_log_max_mb,
        "job_log_total_max_mb": config.job_log_total_max_mb,
        "worker_poll_seconds": config.worker_poll_seconds,
        "worker_claim_timeout_seconds": config.worker_claim_timeout_seconds,
        "worker_recovery_grace_seconds": config.worker_recovery_grace_seconds,
        "worker_live_guard": config.worker_live_guard,
        "monitor_interval_seconds": config.monitor_interval_seconds,
        "monitor_rollup_seconds": config.monitor_rollup_seconds,
        "monitor_uid": config.monitor_uid,
        "tui_refresh_seconds": config.tui_refresh_seconds,
        "file_mode": f"{config.file_mode:04o}",
        "dir_mode": f"{config.dir_mode:04o}",
        "allocator_command_configured": config.allocator_command is not None,
        "allocator_timeout_seconds": config.allocator_timeout_seconds,
        "allocator_weight": config.allocator_weight,
    }


def _add_interactive(config: Config, store: LedgerStore) -> int:
    print("Guided booking. Enter accepts a default; type back or cancel at any field.")
    actor = _current_actor()
    try:
        values = _guided_booking_fields(config)
    except (EOFError, KeyboardInterrupt):
        print("\ncancelled")
        return 0
    if values is None:
        print("cancelled")
        return 0
    mode_raw = values["mode"]
    count = values["count"]
    duration = values["duration"]
    start, allow_queue = values["start"]
    preferred = values["gpus"]
    expected_memory_mb = values["memory"]
    share_units = values["share"]
    command_argv = values["command"]

    start_text = (
        f"current {config.slot_minutes}-minute interval, then earliest queueable slot"
        if allow_queue
        else format_local(start)
    )
    gpu_text = "automatic" if preferred is None else ",".join(map(str, preferred))
    memory_text = "share-weighted estimate" if expected_memory_mb is None else _format_memory_mb(expected_memory_mb)
    print("Review")
    print(f"  mode={mode_raw} GPUs={count} ({gpu_text}) duration={_duration_compact(duration)}")
    if mode_raw == MODE_SHARED:
        print(f"  share={share_text(share_units, config.max_shared_users)} per GPU")
    print(f"  start={start_text}")
    print(f"  expected VRAM/GPU={memory_text}")
    if command_argv:
        print(f"  command={shlex.join(command_argv)}")
    try:
        confirmed = _guided_value("create this reservation? [Y/n]: ", _guided_confirmation)
    except (EOFError, KeyboardInterrupt):
        print("\ncancelled")
        return 0
    if not confirmed:
        print("cancelled")
        return 0

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
        share_units=share_units if mode_raw == MODE_SHARED else None,
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
        _print_scheduled_job_worker(submission)
    if store.last_warning:
        print(f"warning: {store.last_warning}", file=sys.stderr)
    return 0


def _guided_booking_fields(config: Config) -> Optional[dict]:
    fields = [
        (
            "mode",
            lambda _values: "mode [s shared / x exclusive] (s): ",
            lambda raw, _values: _guided_mode(raw),
        ),
        (
            "share",
            lambda values: (
                f"share per GPU [1-{config.max_shared_users}, {share_example(config.max_shared_users)}] "
                f"(1/{config.max_shared_users}): "
                if values["mode"] == MODE_SHARED
                else "share [not used by exclusive] (Enter): "
            ),
            lambda raw, values: _guided_share(raw, values["mode"], config.max_shared_users),
        ),
        (
            "count",
            lambda _values: f"GPU count [1-{config.gpu_count}]: ",
            lambda raw, _values: _guided_gpu_count(raw, config.gpu_count),
        ),
        (
            "duration",
            lambda _values: (
                f"duration [multiple of {config.slot_minutes}m; "
                f"e.g. {config.slot_minutes}m, 1h, 1d]: "
            ),
            lambda raw, _values: _guided_duration(raw, config.slot_minutes),
        ),
        (
            "start",
            lambda _values: "start [now, +30m, 20:00, tomorrow 09:00, 07-13 20:00] (now): ",
            lambda raw, _values: _guided_start(raw, config.slot_minutes),
        ),
        (
            "gpus",
            lambda values: f"GPU IDs [auto or {','.join(map(str, range(min(values['count'], config.gpu_count))))}] (auto): ",
            lambda raw, values: _guided_gpus(raw, values["count"], config.gpu_count),
        ),
        (
            "memory",
            lambda _values: "expected VRAM per GPU [auto or 12g] (auto): ",
            lambda raw, _values: _guided_memory(raw),
        ),
        (
            "command",
            lambda _values: "command to run at start [optional]: ",
            lambda raw, _values: _guided_command(raw),
        ),
    ]
    return _guided_fields(fields)


def _guided_edit_fields(config: Config, reservation: dict) -> Optional[dict]:
    current_gpus = ",".join(map(str, reservation.get("gpus", [])))
    current_memory = reservation.get("expected_memory_mb")
    memory_text = _format_memory_mb(int(current_memory)) if current_memory is not None else "automatic"
    current_share = reservation_share_units(reservation, config.max_shared_users)
    fields = [
        (
            "mode",
            lambda _values: f"mode [keep {reservation['mode']} | s shared | x exclusive] (keep): ",
            lambda raw, _values: None if not raw else _guided_mode(raw),
        ),
        (
            "share",
            lambda values: (
                f"share/GPU [keep {current_share}/{config.max_shared_users} | "
                f"{share_example(config.max_shared_users)}] (keep): "
                if (values.get("mode") or reservation["mode"]) == MODE_SHARED
                else "share [not used by exclusive] (keep): "
            ),
            lambda raw, values: _guided_edit_share(
                raw,
                values.get("mode") or reservation["mode"],
                config.max_shared_users,
            ),
        ),
        (
            "duration",
            lambda _values: (
                f"duration [keep | multiple of {config.slot_minutes}m] (keep): "
            ),
            lambda raw, _values: (
                None if not raw else _guided_duration(raw, config.slot_minutes)
            ),
        ),
        (
            "start",
            lambda _values: "start [keep | +30m | 20:00 | tomorrow 09:00] (keep): ",
            lambda raw, _values: (
                None
                if not raw
                else parse_friendly_start(raw, slot_minutes=config.slot_minutes)
            ),
        ),
        (
            "gpus",
            lambda _values: f"GPU IDs [keep {current_gpus} | e.g. 0,1] (keep): ",
            lambda raw, _values: _guided_optional_gpus(raw, config.gpu_count),
        ),
        (
            "count",
            lambda _values: f"GPU count for auto-pick [keep | 1-{config.gpu_count}] (keep): ",
            lambda raw, values: _guided_optional_count(raw, config.gpu_count, values.get("gpus")),
        ),
        (
            "memory",
            lambda _values: f"expected VRAM/GPU [keep {memory_text} | 12g | - automatic] (keep): ",
            lambda raw, _values: _guided_edit_memory(raw),
        ),
        (
            "queue",
            lambda _values: "move to the next slot if this edit conflicts? [y/N]: ",
            lambda raw, _values: _guided_yes_no(raw),
        ),
    ]
    return _guided_fields(fields)


def _guided_fields(fields) -> Optional[dict]:
    values = {}
    index = 0
    while index < len(fields):
        name, prompt, parser = fields[index]
        raw = input(prompt(values)).strip()
        control = raw.lower()
        if control == "cancel":
            return None
        if control == "back":
            if index == 0:
                print("  Already at the first field.")
            else:
                index -= 1
                values.pop(fields[index][0], None)
            continue
        try:
            values[name] = parser(raw, values)
        except (BookingError, ValueError) as exc:
            print(f"  Invalid input: {exc}. Please try again.")
            continue
        index += 1
    return values


def _guided_value(prompt: str, parser):
    while True:
        raw = input(prompt).strip()
        try:
            return parser(raw)
        except (BookingError, ValueError) as exc:
            print(f"  Invalid input: {exc}. Please try again.")


def _guided_mode(raw: str) -> str:
    value = (raw or "s").lower()
    if value in {"s", MODE_SHARED}:
        return MODE_SHARED
    if value in {"x", MODE_EXCLUSIVE}:
        return MODE_EXCLUSIVE
    raise ValueError("use s/shared or x/exclusive")


def _guided_share(raw: str, mode: str, capacity_units: int) -> Optional[int]:
    if mode == MODE_EXCLUSIVE:
        if raw:
            raise ValueError("share applies only to shared reservations")
        return None
    if not raw:
        return 1
    return parse_share_units(raw, capacity_units)


def _guided_edit_share(
    raw: str, mode: str, capacity_units: int
) -> tuple[Optional[int], bool]:
    if not raw:
        return None, False
    if mode == MODE_EXCLUSIVE:
        raise ValueError("share applies only to shared reservations")
    if raw.lower() in {"default", "auto", "-"}:
        return 1, True
    return parse_share_units(raw, capacity_units), True


def _guided_gpu_count(raw: str, gpu_count: int) -> int:
    if not raw:
        raise ValueError("GPU count is required, for example 1")
    try:
        count = int(raw)
    except ValueError as exc:
        raise ValueError("GPU count must be a whole number") from exc
    if count < 1 or count > gpu_count:
        raise ValueError(f"GPU count must be between 1 and {gpu_count}")
    return count


def _guided_duration(raw: str, slot_minutes: int) -> int:
    duration = parse_duration_seconds(raw)
    if duration % (slot_minutes * 60):
        raise ValueError(f"duration must be a multiple of {slot_minutes} minutes")
    return duration


def _guided_start(raw: str, slot_minutes: int) -> tuple[datetime, bool]:
    value = raw or "now"
    return (
        parse_friendly_start(value, slot_minutes=slot_minutes),
        value.lower() == "now",
    )


def _guided_gpus(raw: str, count: int, gpu_count: int) -> Optional[List[int]]:
    if not raw or raw.lower() == "auto":
        return None
    gpus = _parse_gpu_list(raw)
    if len(gpus) != count:
        raise ValueError(f"enter exactly {count} GPU ID(s), or auto")
    invalid = [gpu for gpu in gpus if gpu < 0 or gpu >= gpu_count]
    if invalid:
        raise ValueError(f"GPU IDs must be between 0 and {gpu_count - 1}")
    return gpus


def _guided_optional_gpus(raw: str, gpu_count: int) -> Optional[List[int]]:
    if not raw:
        return None
    gpus = _parse_gpu_list(raw)
    if len(set(gpus)) != len(gpus):
        raise ValueError("GPU IDs must not be repeated")
    invalid = [gpu for gpu in gpus if gpu < 0 or gpu >= gpu_count]
    if invalid:
        raise ValueError(f"GPU IDs must be between 0 and {gpu_count - 1}")
    return gpus


def _guided_optional_count(raw: str, gpu_count: int, gpus: Optional[List[int]]) -> Optional[int]:
    if not raw:
        return None
    count = _guided_gpu_count(raw, gpu_count)
    if gpus is not None and len(gpus) != count:
        raise ValueError(f"GPU count must match the {len(gpus)} selected GPU ID(s)")
    return count


def _guided_memory(raw: str) -> Optional[int]:
    if not raw or raw.lower() == "auto":
        return None
    return parse_memory_mb(raw)


def _guided_edit_memory(raw: str) -> tuple[Optional[int], bool]:
    if not raw:
        return None, False
    if raw in {"-", "auto"}:
        return None, True
    return parse_memory_mb(raw), True


def _guided_yes_no(raw: str) -> bool:
    value = (raw or "n").lower()
    if value in {"y", "yes"}:
        return True
    if value in {"n", "no"}:
        return False
    raise ValueError("answer y or n")


def _guided_command(raw: str) -> Optional[List[str]]:
    if not raw:
        return None
    try:
        return shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"command quoting is invalid: {exc}") from exc


def _guided_confirmation(raw: str) -> bool:
    value = (raw or "y").lower()
    if value in {"y", "yes"}:
        return True
    if value in {"n", "no"}:
        return False
    raise ValueError("answer y or n")


def _duration_compact(seconds: int) -> str:
    minutes = seconds // 60
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


def _duration_detail(seconds: int) -> str:
    minutes = max(0, seconds // 60)
    hours, mins = divmod(minutes, 60)
    total = f"{hours}h{mins}m" if mins else f"{hours}h"
    if hours < 24:
        return total if hours else f"{mins}m"
    return f"{total} ({_duration_compact(seconds)})"


def _delete_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk del")
    parser.add_argument("reservation_id", nargs="?")
    args = parser.parse_args(argv)
    actor = _current_actor()
    reservation_id = args.reservation_id or _prompt_reservation_token(store, actor, "delete")
    resolved = _resolve_own_reservation_id(store, reservation_id, actor)
    cancellation = submit_cancellation(config, store, actor, resolved)
    print(f"cancelled: {_short_id(cancellation.reservation)}")
    if store.last_warning:
        print(f"warning: {store.last_warning}", file=sys.stderr)
    return 0


def _edit_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk edit")
    parser.add_argument("reservation_id", nargs="?")
    parser.add_argument("--duration")
    start_group = parser.add_mutually_exclusive_group()
    start_group.add_argument(
        "--start",
        help="exact ISO time, e.g. 2030-01-01T20:00:00+08:00",
    )
    start_group.add_argument(
        "--at",
        help="local-friendly time: +30m, 20:00, 'tomorrow 09:00', or 07-13 20:00",
    )
    parser.add_argument("--gpu", help="comma separated GPU indexes; use with --count to change GPU count")
    parser.add_argument("--count", type=int)
    parser.add_argument("--mode", choices=["s", MODE_SHARED, "x", MODE_EXCLUSIVE])
    parser.add_argument("--mem", help="expected memory per GPU; use - to clear")
    _add_share_arguments(parser, config)
    parser.add_argument("--queue", action="store_true", help="allow moving to the next available slot")
    args = parser.parse_args(argv)

    actor = _current_actor()
    token = args.reservation_id or _prompt_reservation_token(store, actor, "edit")
    reservation_id = _resolve_own_reservation_id(store, token, actor)
    if not any(
        [
            args.duration,
            args.start,
            args.at,
            args.gpu,
            args.count is not None,
            args.mode,
            args.mem,
            args.share,
            args.share_with,
            args.queue,
        ]
    ):
        return _edit_interactive(config, store, reservation_id, actor)

    preferred = _parse_gpu_list(args.gpu) if args.gpu else None
    expected_memory_mb = None if args.mem == "-" else (parse_memory_mb(args.mem) if args.mem else None)
    edit_mode = MODE_EXCLUSIVE if args.mode == "x" else (MODE_SHARED if args.mode == "s" else args.mode)
    share_units = _share_units_from_args(args, config, edit_mode)
    start_at = (
        parse_friendly_start(args.at, slot_minutes=config.slot_minutes)
        if args.at
        else (parse_start(args.start) if args.start else None)
    )
    submission = submit_edit(
        config,
        store,
        actor,
        reservation_id,
        start_at=start_at,
        duration_seconds=parse_duration_seconds(args.duration) if args.duration else None,
        mode=edit_mode,
        preferred_gpus=preferred,
        count=args.count,
        allow_queue=args.queue,
        expected_memory_mb=expected_memory_mb,
        update_expected_memory=args.mem is not None,
        share_units=share_units,
        update_share_units=args.share is not None or args.share_with is not None,
    )
    result = submission.result
    _print_edit_result(config, result.reservation, result)
    _print_scheduled_job_worker(submission)
    if store.last_warning:
        print(f"warning: {store.last_warning}", file=sys.stderr)
    return 0


def _edit_interactive(config: Config, store: LedgerStore, reservation_id: str, actor: Actor) -> int:
    reservation = _get_reservation(store, reservation_id)
    print(f"Guided edit {_short_id(reservation)}. Enter keeps a value; type back or cancel at any field.")
    print(
        f"Current: mode={reservation['mode']} gpu={','.join(map(str, reservation.get('gpus', [])))} "
        f"{format_local_range(reservation['start_at'], reservation['end_at'])}"
    )
    try:
        values = _guided_edit_fields(config, reservation)
    except (EOFError, KeyboardInterrupt):
        print("\ncancelled")
        return 0
    if values is None:
        print("cancelled")
        return 0

    mode = values["mode"]
    duration = values["duration"]
    start = values["start"]
    preferred = values["gpus"]
    count = values["count"]
    expected_memory_mb, update_expected_memory = values["memory"]
    share_units, update_share_units = values["share"]
    allow_queue = values["queue"]
    changes = []
    if mode is not None:
        changes.append(f"mode={mode}")
    if duration is not None:
        changes.append(f"duration={_duration_compact(duration)}")
    if start is not None:
        changes.append(f"start={format_local(start)}")
    if preferred is not None:
        changes.append(f"GPU={','.join(map(str, preferred))}")
    if count is not None:
        changes.append(f"GPU count={count} (auto-pick)")
    if update_expected_memory:
        memory_text = "automatic estimate" if expected_memory_mb is None else _format_memory_mb(expected_memory_mb)
        changes.append(f"expected VRAM/GPU={memory_text}")
    if update_share_units:
        changes.append(f"share={share_text(share_units, config.max_shared_users)} per GPU")
    if allow_queue:
        changes.append("queue on conflict=yes")
    if not changes:
        print("no changes")
        return 0

    print("Review")
    for change in changes:
        print(f"  {change}")
    try:
        confirmed = _guided_value("apply these changes? [Y/n]: ", _guided_confirmation)
    except (EOFError, KeyboardInterrupt):
        print("\ncancelled")
        return 0
    if not confirmed:
        print("cancelled")
        return 0

    submission = submit_edit(
        config,
        store,
        actor,
        reservation_id,
        start_at=start,
        duration_seconds=duration,
        mode=mode,
        preferred_gpus=preferred,
        count=count,
        allow_queue=allow_queue,
        expected_memory_mb=expected_memory_mb,
        update_expected_memory=update_expected_memory,
        share_units=share_units,
        update_share_units=update_share_units,
    )
    result = submission.result
    _print_edit_result(config, result.reservation, result)
    _print_scheduled_job_worker(submission)
    if store.last_warning:
        print(f"warning: {store.last_warning}", file=sys.stderr)
    return 0


def _print_edit_result(config: Config, reservation: dict, result) -> None:
    status = "queued" if result.queued else "updated"
    print(
        f"{status}: {_short_id(reservation)} mode={reservation['mode']} "
        f"{_reservation_share_label(reservation, config)}"
        f"gpu={','.join(map(str, reservation.get('gpus', [])))} "
        f"{format_local_range(reservation['start_at'], reservation['end_at'])}"
    )


def _log_command(argv: List[str], store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk log")
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum recent records for this UID (default: 100, max: 1000)",
    )
    parser.add_argument("--json", action="store_true", help="emit a stable machine-readable result")
    args = parser.parse_args(argv)
    if args.limit < 1 or args.limit > 1000:
        raise ValueError("--limit must be between 1 and 1000")

    uid = _current_actor().uid
    events = store.recent_logs(uid, args.limit)
    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": AUDIT_SCHEMA_VERSION,
                    "kind": "operation-log",
                    "uid": uid,
                    "limit": args.limit,
                    "events": events,
                    "warning": store.last_warning,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    for item in events:
        when = _safe_log_time(item.get("ts"))
        time_range = _safe_log_range(item.get("start_at"), item.get("end_at"))
        gpus = item.get("gpus")
        gpu_text = (
            ",".join(_safe_log_text(value, 8) for value in gpus[:64])
            if isinstance(gpus, list)
            else "-"
        )
        print(
            f"{when} {_safe_log_text(item.get('action'), 32)} "
            f"{_safe_log_text(item.get('result'), 32)} "
            f"{_safe_log_text(item.get('reservation_id'), 8)} "
            f"mode={_safe_log_text(item.get('mode'), 16)} "
            f"share={_safe_log_text(item.get('share_units'), 16)} "
            f"gpu={gpu_text or '-'}{time_range}"
        )
    if store.last_warning:
        print(f"warning: {store.last_warning}", file=sys.stderr)
    return 0


def _safe_log_time(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    try:
        return format_local(value)
    except (TypeError, ValueError):
        return "invalid-time"


def _safe_log_range(start: object, end: object) -> str:
    if not isinstance(start, str) or not isinstance(end, str):
        return ""
    try:
        return f" {format_local_range(start, end)}"
    except (TypeError, ValueError):
        return " invalid-range"


def _safe_log_text(value: object, width: int) -> str:
    if value is None or value == "":
        return "-"
    text = "".join(character if character.isprintable() else "?" for character in str(value))
    return text[:width]


def _list_command(argv: List[str], config: Config, store: LedgerStore) -> int:
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
                    "reservations": [
                        public_reservation(item, actor, config.max_shared_users)
                        for item in active
                    ],
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
        duration_seconds = int((parse_iso(reservation["end_at"]) - parse_iso(reservation["start_at"])).total_seconds())
        print(
            f"{index:>2} {_short_id(reservation)} {reservation['mode']} uid={reservation['uid']} "
            f"user={reservation['username']} gpu={gpus} "
            f"{_reservation_share_label(reservation, config)}"
            f"job={reservation.get('job', {}).get('status', '-')} "
            f"dur={_duration_detail(duration_seconds)} "
            f"{format_local_range(reservation['start_at'], reservation['end_at'])}"
        )
    return 0


def _doctor_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    from .diagnostics import DOCTOR_SCHEMA_VERSION, probes_ready, run_deployment_probes

    parser = argparse.ArgumentParser(prog="bk doctor")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="create and remove temporary files to verify deployment prerequisites",
    )
    parser.add_argument("--json", action="store_true", help="emit a stable machine-readable report")
    parser.add_argument("--strict", action="store_true", help="return nonzero for any issue or warning")
    parser.add_argument(
        "--require-monitor",
        action="store_true",
        help="require a fresh, complete monitor heartbeat for post-start verification",
    )
    args = parser.parse_args(argv)
    usage_store = UsageAuditStore(
        config.data_dir,
        config.lock_timeout_seconds,
        config.file_mode,
        config.dir_mode,
    )
    storage_issues = []
    try:
        storage_issues.extend(store.health_issues())
    except Exception as exc:
        storage_issues.append(
            {
                "type": "ledger-health",
                "path": str(store.data_dir),
                "message": f"{type(exc).__name__}: {exc}",
            }
        )

    unsafe_data_root = any(
        item.get("type") in {"directory-type", "path-stat"}
        and item.get("path") == str(store.data_dir)
        for item in storage_issues
    )
    if unsafe_data_root:
        storage_issues.append(
            {
                "type": "usage-health",
                "path": str(usage_store.usage_dir),
                "message": "skipped because the data root is unsafe",
            }
        )
    else:
        try:
            storage_issues.extend(usage_store.health_issues())
        except Exception as exc:
            storage_issues.append(
                {
                    "type": "usage-health",
                    "path": str(usage_store.usage_dir),
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
    collector = (
        {
            "schema_version": "gpubk.collector.v1",
            "state": "unavailable",
            "fresh": None,
            "error": "skipped because the data root is unsafe",
        }
        if unsafe_data_root
        else usage_store.load_collector_status(expected_gpu_count=config.gpu_count)
    )

    ledger = {"version": 1, "reservations": []}
    managed_ledger_paths = {
        str(store.data_dir),
        str(store.backup_dir),
        str(store.ledger_path),
    }
    unsafe_ledger_path = any(
        item.get("type") in {"directory-type", "file-type", "file-links", "path-stat"}
        and item.get("path") in managed_ledger_paths
        for item in storage_issues
    )
    ledger_readable = not unsafe_ledger_path
    if unsafe_ledger_path:
        storage_issues.append(
            {
                "type": "ledger-read",
                "path": str(store.ledger_path),
                "message": "skipped because an unsafe managed path was detected",
            }
        )
    else:
        try:
            ledger = store.load_read_only()
            if store.last_warning:
                storage_issues.append(
                    {
                        "type": "ledger-fallback",
                        "path": str(store.ledger_path),
                        "message": store.last_warning,
                    }
                )
        except Exception as exc:
            ledger_readable = False
            storage_issues.append(
                {
                    "type": "ledger-read",
                    "path": str(store.ledger_path),
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )

    issues = []
    monitor_error = monitor_configuration_error(config)
    if monitor_error:
        issues.append(
            {
                "type": "monitor-policy",
                "message": monitor_error,
            }
        )
    collector_state = collector.get("state")
    if args.require_monitor and collector_state == "not-seen":
        issues.append(
            {
                "type": "monitor-health",
                "message": (
                    "collector heartbeat has not been recorded; start the monitor and retry"
                ),
            }
        )
    if collector_state in {
        "degraded",
        "stale",
        "stopped",
        "clock-skew",
        "topology-mismatch",
    }:
        if collector_state == "degraded":
            message = (
                "collector is running with incomplete GPU telemetry; process gaps="
                f"{collector.get('process_telemetry_gap', [])}, utilization gaps="
                f"{collector.get('process_utilization_gap', [])}"
            )
        elif collector_state == "stale":
            message = (
                f"collector heartbeat is stale ({collector.get('age_seconds')}s old; "
                f"limit {collector.get('stale_after_seconds')}s)"
            )
        elif collector_state == "stopped":
            message = f"collector stopped at {collector.get('stopped_at')}"
        elif collector_state == "clock-skew":
            message = (
                f"collector clock is ahead by {collector.get('clock_skew_seconds')}s"
            )
        else:
            message = (
                f"collector reports {len(collector.get('devices', []))} GPU(s), but policy "
                f"expects {collector.get('expected_gpu_count')}"
            )
        issues.append({"type": "monitor-health", "message": message})
    if ledger_readable:
        try:
            validate_ledger_policy(ledger, config)
        except BookingError as exc:
            issues.append(
                {
                    "type": "ledger-policy-mismatch",
                    "message": str(exc),
                }
            )
        try:
            issues.extend(find_policy_violations(ledger, config.max_shared_users))
        except Exception as exc:
            issues.append(
                {
                    "type": "invalid-ledger-record",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
    privacy_issues = [
        {
            "type": "legacy-inline-job-command",
            "reservation_id": str(item.get("id", "")),
            "message": "full argv is visible in the shared ledger; recreate this pending job",
        }
        for item in ledger.get("reservations", [])
        if isinstance(item, dict)
        and isinstance(item.get("job"), dict)
        and "argv" in item["job"]
    ]
    probes = run_deployment_probes(config) if args.probe else []
    healthy = not issues and not storage_issues and not privacy_issues
    ready = healthy and probes_ready(probes) if args.probe else None
    strict_ok = bool(ready) if args.probe else healthy
    report = {
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "kind": "doctor",
        "healthy": healthy,
        "ready": ready,
        "data_dir": str(config.data_dir),
        "configured_gpu_count": config.gpu_count,
        "booking_slot_minutes": config.slot_minutes,
        "monitor_uid": config.monitor_uid,
        "monitor_required": args.require_monitor,
        "collector": collector,
        "file_mode": f"{config.file_mode:04o}",
        "dir_mode": f"{config.dir_mode:04o}",
        "storage_issues": storage_issues,
        "policy_issues": issues,
        "privacy_issues": privacy_issues,
        "probes": probes,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if not args.strict or strict_ok else 2
    if healthy and not probes:
        if args.require_monitor:
            print("Monitor is running with fresh, complete telemetry.")
        else:
            print("No policy issues found.")
        return 0
    if probes:
        print(f"Deployment preflight: {'ready' if ready else 'not ready'}")
        for probe in probes:
            details = " ".join(
                f"{key}={value}"
                for key, value in probe.items()
                if key not in {"name", "status", "message", "indices"}
            )
            suffix = f" {details}" if details else ""
            print(f"{probe['status']:<4} {probe['name']}: {probe['message']}{suffix}")
    if not issues and not storage_issues and not privacy_issues:
        return 0 if not args.strict or strict_ok else 2
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
    for issue in privacy_issues:
        print(
            f"legacy-inline-job-command id={str(issue['reservation_id'])[:8]} "
            f"{issue['message']}"
        )
    for issue in issues:
        if issue["type"] == "shared-capacity":
            print(
                "shared-capacity "
                f"gpu={issue['gpu']} units={issue.get('used_units', issue['count'])}/{issue['limit']} "
                f"count={issue['count']} "
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
        elif issue["type"] == "invalid-share-units":
            print(
                f"invalid-share-units id={str(issue['reservation_id'])[:8]} "
                f"value={issue.get('share_units')!r} {issue.get('message', '')}"
            )
        else:
            print(f"{issue.get('type', 'policy-issue')} {issue.get('message', '')}".rstrip())
    return 0 if not args.strict or strict_ok else 2


def _reset_command(argv: List[str], config: Config, store: LedgerStore) -> int:
    parser = argparse.ArgumentParser(prog="bk reset")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="required to reset a private/test data directory without confirmation",
    )
    args = parser.parse_args(argv)
    if config.dir_mode & 0o022:
        raise BookingError(
            "bk reset is disabled for shared data directories; stop writers, back up the "
            "data, and use an administrator-controlled maintenance procedure"
        )
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


def _add_share_arguments(parser: argparse.ArgumentParser, config: Config) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--share",
        metavar="PORTION",
        help=(
            f"shared capacity per GPU: units 1-{config.max_shared_users}, "
            f"a fraction such as {share_example(config.max_shared_users)}, or an exact percentage"
        ),
    )
    group.add_argument(
        "--share-with",
        type=int,
        metavar="PEERS",
        help="reserve enough capacity to leave room for at most this many minimum-share bookings",
    )


def _share_units_from_args(
    args: argparse.Namespace,
    config: Config,
    mode: Optional[str],
) -> Optional[int]:
    raw_share = getattr(args, "share", None)
    peer_count = getattr(args, "share_with", None)
    if raw_share is None and peer_count is None:
        return None
    if mode in {"x", MODE_EXCLUSIVE}:
        raise ValueError("--share and --share-with apply only to shared reservations")
    if raw_share is not None:
        if str(raw_share).strip().lower() in {"default", "auto", "-"}:
            return 1
        return parse_share_units(raw_share, config.max_shared_users)
    return share_units_for_peer_limit(peer_count, config.max_shared_users)


def _reservation_share_label(reservation: dict, config: Config) -> str:
    if reservation.get("mode") != MODE_SHARED:
        return ""
    units = reservation_share_units(reservation, config.max_shared_users)
    return f"share={share_text(units, config.max_shared_users)} "


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
    return "--json" in booking_args or "--compact" in booking_args


def _print_booking_advice(
    config: Config,
    store: LedgerStore,
    reservation: dict,
    advice: GpuAdvice,
    expected_memory_mb: Optional[int],
    *,
    verbose: bool = False,
) -> None:
    selected = [int(gpu) for gpu in reservation.get("gpus", [])]
    has_telemetry = any(item.source != "none" for item in advice.snapshots)
    has_history = any(item.sample_count for item in advice.historical_loads.values())
    if verbose and (has_telemetry or has_history):
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
            print("note: GPU memory telemetry unavailable; shared admission used capacity units only")
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
        projected_text = (
            f", reservation-budget-after={_format_memory_mb(projected)}" if projected is not None else ""
        )
        memory_parts.append(f"GPU {gpu} physical-free-now={_format_memory_mb(now_free)}{projected_text}")
    if memory_parts:
        print("VRAM: " + "; ".join(memory_parts))
    if reservation.get("mode") == MODE_SHARED and expected_memory_mb is None:
        units = reservation_share_units(reservation, config.max_shared_users)
        assumptions = [
            inferred_share_memory_mb(
                max(0, capacities[gpu] - config.shared_memory_reserve_mb),
                config.max_shared_users,
                units,
            )
            for gpu in selected
            if gpu in capacities
        ]
        if assumptions:
            print(
                f"assumption: --mem omitted; budgeted this reservation at "
                f"{_format_memory_mb(min(assumptions))}/GPU "
                f"({share_text(units, config.max_shared_users)} of usable VRAM)"
            )


def _format_memory_mb(value: Optional[int]) -> str:
    if value is None:
        return "unknown"
    if value >= 1024:
        return f"{value / 1024:.1f}GiB"
    return f"{value}MiB"


def _format_bytes(value: int) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.1f}GiB"
    if value >= 1024**2:
        return f"{value / 1024**2:.1f}MiB"
    if value >= 1024:
        return f"{value / 1024:.1f}KiB"
    return f"{value}B"


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


def _resolve_own_retained_reservation_id(store: LedgerStore, token: str, actor: Actor) -> str:
    mine = [
        item
        for item in store.load().get("reservations", [])
        if int(item.get("uid", -1)) == actor.uid
    ]
    matches = [item for item in mine if str(item.get("id", "")).startswith(token)]
    if not matches:
        raise BookingError(f"reservation not found for current user: {token}")
    if len(matches) > 1:
        choices = ", ".join(_short_id(item) for item in matches)
        raise BookingError(f"ambiguous reservation id {token}; matches: {choices}")
    return str(matches[0]["id"])


def _get_reservation(store: LedgerStore, reservation_id: str) -> dict:
    for reservation in list_active(store.load()):
        if reservation.get("id") == reservation_id:
            return reservation
    raise BookingError("reservation not found")


def _print_help(file=None) -> None:
    file = file or sys.stdout
    print(
        """GPUbk - shared GPU booking from the terminal

BOOK
  bk 2 1h                        earliest shared slot
  bk x 1 1h                      earliest exclusive slot
  bk 1 1h --gpu 3 --mem 12g      choose GPU and expected VRAM
  bk 1 1h --share 1/2            reserve half of shared capacity
  bk 1 1h --share-with 1         leave one minimum-share place
  bk 1 1h --at +30m              exact friendly local time
  bk 1 1h -- command args...     book and schedule a command
  bk a                            guided booking with input recovery

VIEW
  bk st                           compact live status
  bk tl [window] [--step auto]   fine-grained aligned timeline
  bk slots 2 1h                  read-only earliest alternatives
  bk l                            active reservations
  bk t                            full-screen TUI

MANAGE
  bk e [number|short_id]         guided edit
  bk e ID --duration 2h          direct edit
  bk e ID --at 20:00             move using local time
  bk d <number|short_id>         cancel
  bk lg [--limit 100] [--json]    recent personal operation log

JOBS AND USAGE
  bk w                            run this UID's due jobs
  bk w --status                   check this UID's worker
  bk j / bk jl ID / bk jr ID     list, inspect, or retry jobs
  bk j --cleanup                  prune private job files by policy
  bk m [--once]                  monitor GPU processes
  bk u / bk u users --since 30d  own or all-user summaries
  bk u events / bk u samples     audit events or time series

AGENTS AND ADMIN
  bk agent context               stable machine-readable context
  bk agent recommend 2 1h       read-only legal placement
  bk mcp / bk skill install      MCP server or bundled Codex skill
  bk config [--json]            inspect effective config and policy
  bk doctor --probe --strict     verify deployment prerequisites
  bk reset --yes                 private/test only; never shared

TIME AND POLICY
  Duration syntax: 30m, 1h30m, 1d; value must fit the configured slice.
  Reservations use the server-configured slice (default: 5m).
  Friendly time: --at +30m, --at 20:00, --at "tomorrow 09:00".
  Machine time: --start 2030-01-01T20:00:00+08:00.
  No time option: use the active slice, then queue to the earliest slot.
  Explicit --at/--start is exact. For edits, --queue allows a move.
  Shared is the default; s/shared and x/exclusive are accepted aliases.
  Share units control admission and inferred VRAM.
  They do not enforce GPU compute bandwidth.

Run `bk COMMAND --help` for more options.
Plain `bk` opens the prompt; `bk t` opens the full-screen TUI.
""",
        file=file,
    )


def _print_shell_help() -> None:
    print(
        """Commands:
  st | status               compact GPU status; add --timeline or -v
  tl | timeline [2h]        aligned timeline; --from/--window/--step/--gpu
  slots 2 1h               show read-only earliest booking alternatives
  1 4h [--gpu 0]            shared booking, default mode
  1 4h --share 3/4          weighted shared capacity (when server capacity is 4)
  s 1 4h [--gpu 0]          shared booking
  x 1 4h [--gpu 0]          exclusive booking
  a | add                   guided booking prompts
  e <number|short_id>       modify your reservation
  d <number|short_id>       cancel your reservation
  l | list                  list active reservations
  lg | log [--limit 100]    show your recent operation log
  cfg | config              inspect effective configuration and ledger policy
  dr | doctor               report policy or deployment issues
  m | monitor               continuously audit GPU process usage
  u                          summarize this UID's last 24h usage
  u users --since 30d       summarize visible users
  u events | u samples      audit events or versioned time series
  u storage                 inspect tiers, retention, and migration
  w | worker                execute only this UID's due jobs
  j | jobs                  list scheduled job states
  j --cleanup               prune private job files by policy
  jl <number|short_id>      show a job log
  jr <number|short_id>      retry a failed job
  agent context             emit stable allocation context JSON
  agent recommend 2 1h      compute a read-only legal placement
  agent edit ID --duration 2h --op-id KEY
  agent cancel ID           cancel with structured JSON output
  reset --yes               private/test only; never shared
  t | tui                   open full-screen TUI
  quit                      exit
"""
    )


def _status_command(
    argv: List[str],
    config: Config,
    store: LedgerStore,
    *,
    timeline_only: bool = False,
) -> int:
    parser = argparse.ArgumentParser(prog="bk timeline" if timeline_only else "bk status")
    if timeline_only:
        parser.add_argument("window_arg", nargs="?", help="display span shorthand, e.g. 8h")
    parser.add_argument(
        "--from",
        dest="start",
        default="now",
        help="window start: now, +30m, 20:00, tomorrow 09:00, or ISO",
    )
    parser.add_argument("--window", help="display span, e.g. 2h, 8h, or 1d")
    parser.add_argument(
        "--step",
        default=f"{config.slot_minutes}m",
        help=f"cell size: {config.slot_minutes}m, another whole slice multiple, or auto",
    )
    parser.add_argument("--gpu", help="show only comma-separated GPU IDs on the timeline")
    if not timeline_only:
        parser.add_argument("--timeline", action="store_true", help="append the configurable timeline")
        parser.add_argument("-v", "--verbose", action="store_true", help="show processes and all reservations")
    args = parser.parse_args(argv)

    window_raw = (
        args.window
        or (getattr(args, "window_arg", None) if timeline_only else None)
        or f"{config.timeline_hours}h"
    )
    window_seconds = parse_duration_seconds(window_raw)
    step_seconds = _resolve_timeline_step(args.step, window_seconds, config.slot_seconds)
    start = _floor_timeline_start(
        parse_friendly_start(args.start, slot_minutes=config.slot_minutes),
        step_seconds,
    )
    if window_seconds % step_seconds:
        raise ValueError("--window must be an exact multiple of --step")
    slots = window_seconds // step_seconds
    if slots > TIMELINE_MAX_SLOTS:
        raise ValueError(
            f"timeline would contain {slots} cells; increase --step or use --step auto "
            f"(maximum {TIMELINE_MAX_SLOTS})"
        )

    gpus = _parse_gpu_list(args.gpu) if args.gpu else None
    if gpus is not None:
        invalid = [gpu for gpu in gpus if gpu < 0 or gpu >= config.gpu_count]
        if invalid:
            raise ValueError(f"GPU IDs must be between 0 and {config.gpu_count - 1}")

    if timeline_only:
        _print_timeline(config, store, start, window_seconds, step_seconds, gpus)
    else:
        _print_status(
            config,
            store,
            start,
            window_seconds,
            step_seconds,
            show_timeline=args.timeline,
            verbose=args.verbose,
            timeline_gpus=gpus,
        )
    return 0


def _resolve_timeline_step(raw: str, window_seconds: int, slot_seconds: int = 5 * 60) -> int:
    if raw.lower() != "auto":
        step = parse_duration_seconds(raw)
        if step % slot_seconds:
            raise ValueError(
                f"--step must be a multiple of the configured {slot_seconds // 60}-minute slice"
            )
        return step

    terminal_columns = shutil.get_terminal_size(fallback=(100, 24)).columns
    target_slots = max(12, (terminal_columns - 6) // TIMELINE_CELL_WIDTH)
    dynamic_steps = {slot_seconds * factor for factor in TIMELINE_AUTO_FACTORS}
    dynamic_steps.update(step for step in TIMELINE_AUTO_STEPS if step % slot_seconds == 0)
    for step in sorted(dynamic_steps):
        if window_seconds % step == 0 and window_seconds // step <= min(target_slots, TIMELINE_MAX_SLOTS):
            return step
    raise ValueError("--window is too large for automatic timeline scaling")


def _floor_timeline_start(value: datetime, step_seconds: int = 5 * 60) -> datetime:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    timestamp = int(normalized.timestamp())
    return datetime.fromtimestamp(timestamp - (timestamp % step_seconds), timezone.utc)


def _print_status(
    config: Config,
    store: LedgerStore,
    timeline_start: Optional[datetime] = None,
    window_seconds: int = TIMELINE_DEFAULT_WINDOW_SECONDS,
    step_seconds: int = 5 * 60,
    *,
    show_timeline: bool = False,
    verbose: bool = False,
    timeline_gpus: Optional[List[int]] = None,
) -> None:
    now = utc_now()
    active = list_active(store.load(), now)
    gpu_snapshots = snapshot(config)
    if timeline_gpus is not None:
        selected_gpu_ids = set(timeline_gpus)
        gpu_snapshots = [gpu for gpu in gpu_snapshots if gpu.index in selected_gpu_ids]
    usage_by_gpu = classify_process_usage(gpu_snapshots, active, now)
    live_states = assess_gpu_live_states(gpu_snapshots, config.gpu_count)
    print("GPU status")
    wide_status = shutil.get_terminal_size(fallback=(100, 24)).columns >= 88
    if wide_status:
        print(
            f"{'GPU':<4} {'Model':<14} {'Util':>5} {'VRAM free/total':>16} "
            f"{'Proc':>4} {'State':<10} {'Share':>6} {'X-free':<11}"
        )
    else:
        print(f"{'GPU':<4} {'Util':>5} {'VRAM free/total':>16} {'Proc':>4} {'State':<10} {'Share':>6} {'X-free':<11}")
    for gpu in gpu_snapshots:
        if gpu.memory_total_mb:
            free = max(0, gpu.memory_total_mb - gpu.memory_used_mb) / 1024
            total = gpu.memory_total_mb / 1024
            mem = f"{free:.1f}/{total:.1f}G"
        else:
            mem = "-"
        util = f"{gpu.utilization_percent}%" if gpu.utilization_percent is not None else "-"
        rows = usage_by_gpu.get(gpu.index, [])
        workload_rows = [item for item in rows if item.status != USAGE_SYSTEM]
        violations = sum(1 for item in rows if item.violation)
        state = "unreserved" if violations else live_states[gpu.index].status
        overlapping_now = [
            item
            for item in active
            if gpu.index in item.get("gpus", [])
            and parse_iso(item["start_at"]) <= now < parse_iso(item["end_at"])
        ]
        if any(item.get("mode") == MODE_EXCLUSIVE for item in overlapping_now):
            share = "X"
        else:
            used_units = sum(
                reservation_share_units(item, config.max_shared_users)
                for item in overlapping_now
                if item.get("mode") == MODE_SHARED
            )
            share = share_text(used_units, config.max_shared_users)
        x_free = _compact_local_time(_next_exclusive_free(active, gpu.index, now), now)
        if wide_status:
            print(
                f"{gpu.index:<4} {_clip_text(gpu.name, 14):<14} {util:>5} {mem:>16} "
                f"{len(workload_rows):>4} {state:<10} {share:>6} {x_free:<11}"
            )
        else:
            print(
                f"{gpu.index:<4} {util:>5} {mem:>16} {len(workload_rows):>4} "
                f"{state:<10} {share:>6} {x_free:<11}"
            )
        if verbose:
            for item in rows:
                process = item.process
                sm = f"{process.sm_utilization_percent}%" if process.sm_utilization_percent is not None else "-"
                print(
                    f"     pid={process.pid} uid={process.uid if process.uid is not None else '?'} "
                    f"user={process.username} sm={sm} mem={process.gpu_memory_mb}MiB "
                    f"state={item.status} cmd={summarize_process_command(process.command)}"
                )

    actor = _current_actor()
    mine = _own_active_reservations(store, actor)
    print(f"Reservations: {len(active)} active, {len(mine)} yours | `bk l` details | `bk tl` timeline")
    if mine:
        for index, reservation in enumerate(mine, 1):
            gpus = ",".join(str(item) for item in reservation.get("gpus", []))
            if wide_status:
                print(
                    f"  {index:>2} {_short_id(reservation)} {reservation['mode']:<9} "
                    f"{_reservation_share_label(reservation, config)}"
                    f"GPU={gpus:<7} {reservation['username']} "
                    f"job={reservation.get('job', {}).get('status', '-'):<10} "
                    f"{format_local_range(reservation['start_at'], reservation['end_at'])}"
                )
            else:
                print(
                    f"  {index:>2} {_short_id(reservation)} {reservation['mode']:<9} "
                    f"{_reservation_share_label(reservation, config)}"
                    f"G={_clip_text(gpus, 8):<8} {_compact_local_range(reservation['start_at'], reservation['end_at'])}"
                )
    if reservations_need_worker(mine, actor.uid):
        worker_status = inspect_worker_status(config, actor)
        print(_worker_status_line(worker_status))
        worker_warning = scheduled_job_worker_warning(worker_status)
        if worker_warning:
            print(f"warning: {worker_warning}", file=sys.stderr)
    if verbose and active:
        others = [item for item in active if int(item.get("uid", -1)) != actor.uid]
        if others:
            print("Other reservations")
            for reservation in others:
                print(
                    f"  {_short_id(reservation)} {reservation['mode']:<9} "
                    f"{_reservation_share_label(reservation, config)}"
                    f"GPU={','.join(map(str, reservation.get('gpus', []))):<7} "
                    f"{reservation.get('username', '?')} "
                    f"{format_local_range(reservation['start_at'], reservation['end_at'])}"
                )
    if show_timeline:
        print()
        _print_timeline(
            config,
            store,
            timeline_start or _floor_timeline_start(now, step_seconds),
            window_seconds,
            step_seconds,
            timeline_gpus,
        )
    print()


def _print_timeline(
    config: Config,
    store: LedgerStore,
    start: Optional[datetime] = None,
    window_seconds: int = TIMELINE_DEFAULT_WINDOW_SECONDS,
    step_seconds: int = 5 * 60,
    gpus: Optional[List[int]] = None,
) -> None:
    start = _floor_timeline_start(start or utc_now(), step_seconds)
    end = start + timedelta(seconds=window_seconds)
    slots = window_seconds // step_seconds
    active = list_active(store.load(), start)
    actor = _current_actor()
    local_start = start.astimezone()
    local_end = end.astimezone()
    if local_start.date() == local_end.date():
        range_text = f"{_weekday_short(local_start)} {local_start:%m-%d %H:%M}->{local_end:%H:%M}"
    else:
        range_text = f"{_weekday_short(local_start)} {local_start:%m-%d %H:%M}->{local_end:%m-%d %H:%M}"
    print(f"Timeline | {range_text} | {_duration_compact(step_seconds)}/cell | {slots} cells")
    slot_starts = [start + timedelta(seconds=step_seconds * offset) for offset in range(slots)]
    block_size = _timeline_block_size(len(slot_starts))
    visible_gpus = gpus if gpus is not None else list(range(config.gpu_count))
    for block_index in range(0, len(slot_starts), block_size):
        block = slot_starts[block_index : block_index + block_size]
        if block_index:
            print()
        _print_timeline_axis(block)
        for gpu in visible_gpus:
            cells = []
            for slot_start in block:
                slot_end = slot_start + timedelta(seconds=step_seconds)
                overlapping = [
                    item
                    for item in active
                    if gpu in item.get("gpus", [])
                    and parse_iso(item["start_at"]) < slot_end
                    and slot_start < parse_iso(item["end_at"])
                ]
                if not overlapping:
                    cells.append("··")
                else:
                    exclusive = [item for item in overlapping if item.get("mode") == MODE_EXCLUSIVE]
                    if exclusive:
                        cells.append(
                            "MX"
                            if any(int(item.get("uid", -1)) == actor.uid for item in exclusive)
                            else "XX"
                        )
                        continue
                    shared = [item for item in overlapping if item.get("mode") == MODE_SHARED]
                    mine_units = sum(
                        reservation_share_units(item, config.max_shared_users)
                        for item in shared
                        if int(item.get("uid", -1)) == actor.uid
                    )
                    used_units = sum(
                        reservation_share_units(item, config.max_shared_users)
                        for item in shared
                    )
                    prefix = "M" if mine_units else "S"
                    cells.append(
                        f"{prefix}{used_units}" if used_units < 10 else f"{prefix}+"
                    )
            print(_timeline_cells(f"G{gpu}", cells))
    print("Legend: ·· free | M1-M9 total units, includes mine")
    print("        S1-S9 total units, others only | MX/XX exclusive")
    example_step = _duration_compact(config.slot_seconds * 3)
    print(
        f"Control: --from 20:00 --window 8h --step {example_step} "
        "| --step auto"
    )
    print()


def _print_timeline_axis(slot_starts: List[datetime]) -> None:
    local = [item.astimezone() for item in slot_starts]
    days = []
    hours = []
    minutes = []
    for index, item in enumerate(local):
        previous = local[index - 1] if index else None
        days.append(f"{item.day:02d}" if previous is None or item.date() != previous.date() else "")
        hours.append(f"{item.hour:02d}" if previous is None or item.hour != previous.hour else "")
        minutes.append(f"{item.minute:02d}")
    print(_timeline_cells("Day", days))
    print(_timeline_cells("Hour", hours))
    print(_timeline_cells("Min", minutes))


def _timeline_cells(label: str, cells: List[str]) -> str:
    return f"{label:<6}" + "".join(f"{cell:<{TIMELINE_CELL_WIDTH}}" for cell in cells).rstrip()


def _timeline_block_size(slot_count: int) -> int:
    terminal_columns = shutil.get_terminal_size(fallback=(100, 24)).columns
    fitting = max(1, (terminal_columns - 6) // TIMELINE_CELL_WIDTH)
    if fitting >= 12:
        fitting = max(12, (fitting // 12) * 12)
    return max(1, min(slot_count, fitting))


def _weekday_short(value: datetime) -> str:
    return ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[value.weekday()]


def _next_exclusive_free(active: List[dict], gpu: int, start: datetime) -> datetime:
    intervals = sorted(
        (
            parse_iso(item["start_at"]),
            parse_iso(item["end_at"]),
        )
        for item in active
        if gpu in item.get("gpus", []) and parse_iso(item["end_at"]) > start
    )
    cursor = start
    for interval_start, interval_end in intervals:
        if interval_end <= cursor:
            continue
        if interval_start > cursor:
            break
        cursor = max(cursor, interval_end)
    return cursor


def _compact_local_time(value: datetime, now: datetime) -> str:
    if value <= now:
        return "now"
    local = value.astimezone()
    if local.date() == now.astimezone().date():
        return f"{local:%H:%M}"
    return f"{local:%m-%d %H:%M}"


def _compact_local_range(start: str, end: str) -> str:
    local_start = parse_iso(start).astimezone()
    local_end = parse_iso(end).astimezone()
    if local_start.date() == local_end.date():
        return f"{local_start:%m-%d %H:%M}->{local_end:%H:%M}"
    return f"{local_start:%m-%d %H:%M}->{local_end:%m-%d %H:%M}"


def _clip_text(value: object, width: int) -> str:
    text = str(value)
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "+"
