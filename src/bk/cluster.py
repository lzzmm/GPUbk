from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Optional, Sequence

from .cluster_transport import (
    ClusterNode,
    NodeReply,
    invoke_node as _invoke_once,
    node_command,
)
from .fileio import fsync_directory, open_existing_regular
from .models import BookingError
from .node_identity import stable_node_identity
from .timeparse import (
    format_local,
    parse_duration_seconds,
    parse_iso,
    parse_memory_mb,
    utc_now,
)


CLUSTER_SCHEMA_VERSION = "gpubk.cluster.v1"
SYSTEM_CLUSTER_FILE = Path("/etc/gpubk/cluster.json")
MAX_CLUSTER_FILE_BYTES = 1024 * 1024
MAX_CLUSTER_NODES = 128
MAX_CLOCK_SKEW_SECONDS = 30
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
_NODE_ID = re.compile(r"^[0-9a-f]{20}$")
_SSH_TARGET = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.@:\[\]-]{0,254}$")
_BOOK_CAPABILITIES = (
    "federated_node_identity",
    "idempotent_booking",
    "operation_status",
    "preflight_idempotent_replay",
)
_SCHEDULED_BOOK_CAPABILITIES = (
    *_BOOK_CAPABILITIES,
    "scheduled_jobs",
    "scheduled_job_path_snapshot",
    "private_job_specs",
)
_EDIT_CAPABILITIES = (
    "federated_node_identity",
    "idempotent_edit",
    "operation_status",
)
_CANCEL_CAPABILITIES = (
    "federated_node_identity",
    "idempotent_cancel",
    "operation_status",
)

# Compatibility for callers and tests that inspect the generated SSH command.
_node_command = node_command


@dataclass(frozen=True)
class ClusterConfig:
    path: Path
    nodes: tuple[ClusterNode, ...]
    principals: tuple[dict, ...] = ()
    history_root: Optional[Path] = None

    def node(self, name: str) -> ClusterNode:
        matches = [node for node in self.nodes if node.name == name]
        if not matches:
            raise BookingError(f"unknown cluster node {name!r}")
        return matches[0]

    @property
    def enabled_nodes(self) -> tuple[ClusterNode, ...]:
        return tuple(node for node in self.nodes if node.enabled)


@dataclass(frozen=True)
class ClusterBookingIntent:
    count: int
    duration: str
    mode: str
    start: Optional[str]
    gpu: Optional[str]
    exclude_gpu: Optional[str]
    memory: Optional[str]
    share: Optional[int]
    operation_id: Optional[str]
    json_output: bool
    command_argv: Optional[tuple[str, ...]]

    @classmethod
    def from_namespace(
        cls,
        args: argparse.Namespace,
        *,
        command_argv: Optional[Sequence[str]] = None,
    ) -> "ClusterBookingIntent":
        return cls(
            count=args.count,
            duration=args.duration,
            mode="exclusive" if args.mode in {"x", "exclusive"} else "shared",
            start=args.start,
            gpu=args.gpu,
            exclude_gpu=args.exclude_gpu,
            memory=args.mem or args.memory,
            share=args.share,
            operation_id=args.op_id,
            json_output=args.json,
            command_argv=(tuple(command_argv) if command_argv is not None else None),
        )

    def recommendation_argv(self) -> list[str]:
        argv = [
            "agent",
            "recommend",
            str(self.count),
            self.duration,
            "--mode",
            self.mode,
        ]
        return _append_cluster_booking_options(argv, self, compact=True)

    def booking_argv(self, operation_id: str) -> list[str]:
        argv = [] if self.mode == "shared" else ["x"]
        argv += [str(self.count), self.duration]
        argv = _append_cluster_booking_options(argv, self)
        argv += ["--op-id", operation_id, "--json"]
        if self.command_argv is not None:
            argv += ["--", *self.command_argv]
        return argv


@dataclass(frozen=True)
class ClusterCandidate:
    ranked_start: datetime
    confidence_rank: int
    reply: NodeReply

    @property
    def sort_key(self) -> tuple[datetime, int, int, str]:
        return (
            self.ranked_start,
            self.reply.node.priority,
            self.confidence_rank,
            self.reply.node.name,
        )


def cluster_config_path() -> Path:
    value = os.environ.get("BK_CLUSTER_CONFIG")
    if value:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise BookingError("BK_CLUSTER_CONFIG must be an absolute path")
        return path
    return SYSTEM_CLUSTER_FILE


def cluster_configured() -> bool:
    if os.environ.get("BK_CLUSTER_DISABLE") == "1":
        return False
    try:
        return os.path.lexists(cluster_config_path())
    except BookingError:
        return False


def load_cluster_config(path: Optional[Path] = None) -> ClusterConfig:
    if os.environ.get("BK_CLUSTER_DISABLE") == "1":
        raise BookingError("cluster routing is disabled in this child process")
    path = cluster_config_path() if path is None else path
    if not path.is_absolute():
        raise BookingError(f"cluster catalog path must be absolute: {path}")
    try:
        fd = open_existing_regular(path)
    except FileNotFoundError as exc:
        raise BookingError(
            f"cluster mode is not configured: {path}; initialize it with "
            "'sudo bk admin cluster init NODE --yes'"
        ) from exc
    except OSError as exc:
        raise BookingError(
            f"cannot read trusted cluster catalog: {path}: {exc}"
        ) from exc
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid not in {0, os.getuid()}:
            raise BookingError(
                "cluster catalog must be owned by root or the current UID"
            )
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise BookingError(
                "cluster catalog must not be writable by group or other users"
            )
        if metadata.st_size > MAX_CLUSTER_FILE_BYTES:
            raise BookingError("cluster catalog exceeds 1 MiB")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            document = json.load(handle)
    except json.JSONDecodeError as exc:
        raise BookingError(f"cluster catalog is invalid JSON: {path}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    return _parse_cluster_config(path, document)


def write_cluster_config(config: ClusterConfig, *, require_root: bool = True) -> None:
    if require_root and os.geteuid() != 0:
        raise BookingError("cluster catalog updates must run as root")
    path = config.path
    if not path.is_absolute():
        raise BookingError("cluster catalog path must be absolute")
    if os.path.lexists(path):
        load_cluster_config(path)
    parent = path.parent
    metadata = parent.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise BookingError(f"cluster catalog parent is not a real directory: {parent}")
    if require_root and metadata.st_uid != 0:
        raise BookingError(f"cluster catalog parent must be root-owned: {parent}")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise BookingError(
            f"cluster catalog parent must not be writable by group or others: {parent}"
        )
    document = {
        "schema_version": CLUSTER_SCHEMA_VERSION,
        "nodes": [
            {
                "name": node.name,
                "node_id": node.node_id,
                "transport": node.transport,
                **({"target": node.target} if node.target is not None else {}),
                **(
                    {"executable": node.executable}
                    if node.executable != "/usr/local/bin/bk"
                    else {}
                ),
                **({"priority": node.priority} if node.priority else {}),
                **(
                    {"timeout_seconds": node.timeout_seconds}
                    if node.timeout_seconds != 8
                    else {}
                ),
                **({"enabled": False} if not node.enabled else {}),
            }
            for node in config.nodes
        ],
        **({"principals": list(config.principals)} if config.principals else {}),
        **(
            {"history_root": str(config.history_root)}
            if config.history_root is not None
            else {}
        ),
    }
    _parse_cluster_config(path, document)
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        fsync_directory(parent)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary_path.exists():
            temporary_path.unlink()


def run_cluster_cli(argv: Sequence[str]) -> int:
    action, args = _normalize_cluster_action(argv)
    if action in {"-h", "--help", "help"}:
        _print_cluster_help()
        return 0
    if action in {"status", "st", "list", "ls", "context", "ctx"}:
        parsed = _cluster_status_parser(action).parse_args(_cluster_help_args(args))
        return _cluster_status(load_cluster_config(), json_output=parsed.json)
    if action in {"check", "health", "doctor"}:
        parsed = _cluster_status_parser("check").parse_args(_cluster_help_args(args))
        return _cluster_check(load_cluster_config(), json_output=parsed.json)
    if action in {"recommend", "rec"}:
        booking_args, command_argv = _split_cluster_job_command(args)
        if command_argv is not None:
            raise BookingError(
                "cluster recommend does not accept a job command; use 'bk c ... -- COMMAND' to book it"
            )
        parsed = _cluster_booking_parser("bk cluster recommend").parse_args(
            _cluster_help_args(booking_args)
        )
        return _cluster_recommend(
            load_cluster_config(),
            ClusterBookingIntent.from_namespace(parsed),
            book=False,
        )
    if action in {"book", "b"}:
        booking_args, command_argv = _split_cluster_job_command(args)
        parsed = _cluster_booking_parser("bk cluster book").parse_args(
            _cluster_help_args(booking_args)
        )
        return _cluster_recommend(
            load_cluster_config(),
            ClusterBookingIntent.from_namespace(
                parsed,
                command_argv=command_argv,
            ),
            book=True,
        )
    if action in {"usage", "u"}:
        parsed = _cluster_usage_parser().parse_args(_cluster_help_args(args))
        if parsed.limit < 1:
            raise BookingError("--limit must be >= 1")
        forwarded = [
            item for item in args if item not in {"-j", "--json", "-c", "--compact"}
        ]
        return _cluster_usage(
            load_cluster_config(),
            forwarded,
            json_output=parsed.json,
            compact=parsed.compact,
        )
    if action in {"history", "hist"}:
        parsed = _cluster_history_parser().parse_args(_cluster_help_args(args))
        return _cluster_history(load_cluster_config(), parsed)
    if action in {"tui", "t"}:
        argparse.ArgumentParser(prog="bk cluster tui").parse_args(
            _cluster_help_args(args)
        )
        from .cluster_tui import run_cluster_tui

        return run_cluster_tui(load_cluster_config())
    if action in {"edit", "e"}:
        parsed = _cluster_edit_parser().parse_args(_cluster_help_args(args))
        return _cluster_edit(load_cluster_config(), parsed)
    if action in {"cancel", "del", "d"}:
        parsed = _cluster_cancel_parser().parse_args(_cluster_help_args(args))
        return _cluster_cancel(load_cluster_config(), parsed)
    raise BookingError(f"unknown cluster command: {action}")


def _normalize_cluster_action(argv: Sequence[str]) -> tuple[str, list[str]]:
    args = list(argv)
    if not args:
        return "status", []
    first = args.pop(0)
    if first.isdigit():
        return "book", [first, *args]
    modes = {
        "auto": "shared",
        "s": "shared",
        "shared": "shared",
        "x": "exclusive",
        "exclusive": "exclusive",
    }
    if first in modes:
        booking_args, command_argv = _split_cluster_job_command(args)
        if any(item == "--mode" or item.startswith("--mode=") for item in booking_args):
            raise BookingError("cluster booking mode was specified more than once")
        return "book", _join_cluster_job_command(
            [*booking_args, "--mode", modes[first]],
            command_argv,
        )
    return first, args


def _split_cluster_job_command(
    argv: Sequence[str],
) -> tuple[list[str], Optional[tuple[str, ...]]]:
    args = list(argv)
    if "--" not in args:
        return args, None
    separator = args.index("--")
    command_argv = tuple(args[separator + 1 :])
    if not command_argv:
        raise BookingError("-- must be followed by a job command")
    return args[:separator], command_argv


def _join_cluster_job_command(
    booking_args: Sequence[str],
    command_argv: Optional[Sequence[str]],
) -> list[str]:
    result = list(booking_args)
    if command_argv is not None:
        result += ["--", *command_argv]
    return result


def _booking_capabilities(
    command_argv: Optional[Sequence[str]],
) -> tuple[str, ...]:
    return (
        _SCHEDULED_BOOK_CAPABILITIES if command_argv is not None else _BOOK_CAPABILITIES
    )


def run_node_cli(node_name: str, argv: Sequence[str]) -> int:
    config = load_cluster_config()
    node = config.node(node_name)
    if not node.enabled:
        raise BookingError(
            f"cluster node {node_name!r} is disabled by the administrator"
        )
    if not argv:
        raise BookingError(f"bk @{node_name} requires a GPUBK command")
    command = list(argv)
    booking = _is_booking_command(command)
    if not booking:
        raise BookingError(
            "bk @NODE currently accepts booking commands only; use bk cluster "
            "for cross-node status, usage, edit, and cancel"
        )
    booking_args, command_argv = _split_cluster_job_command(command)
    requested_json = "--json" in booking_args
    context = _invoke(node, ["agent", "context", "--compact"])
    _require_write_capabilities(
        context,
        _booking_capabilities(command_argv),
    )
    operation_id = _argument_value(booking_args, "--op-id")
    if operation_id is None:
        operation_id = str(uuid.uuid4())
        booking_args += ["--op-id", operation_id]
    if not requested_json:
        booking_args.append("--json")
    command = _join_cluster_job_command(booking_args, command_argv)
    reply = _invoke_idempotent_write(
        node,
        command,
        operation_id,
        expected_action="create",
    )
    if reply.error:
        raise BookingError(f"node {node.name}: {reply.error}")
    payload = reply.payload or {}
    if not requested_json:
        reservation = payload.get("reservation", {})
        short_id = reservation.get("short_id") or str(reservation.get("id", ""))[:8]
        print(
            f"{payload.get('status', 'created')} on {node.name}: {short_id} "
            f"GPU={_gpu_text(reservation.get('gpus'))} "
            f"{_local_time(reservation.get('start_at'))} -> "
            f"{_local_time(reservation.get('end_at'))}"
        )
    elif payload:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _cluster_status(config: ClusterConfig, *, json_output: bool = False) -> int:
    replies = query_cluster_contexts(config)
    if json_output:
        print(
            json.dumps(
                {
                    "schema_version": CLUSTER_SCHEMA_VERSION,
                    "kind": "cluster-context",
                    "nodes": [
                        {
                            "name": reply.node.name,
                            "node_id": reply.node.node_id,
                            "priority": reply.node.priority,
                            "enabled": reply.node.enabled,
                            "available": reply.node.enabled and reply.error is None,
                            "error": reply.error,
                            "context": reply.payload,
                        }
                        for reply in replies
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 3 if all(reply.error for reply in replies) else 0
    print(f"GPUBK cluster | {len(config.nodes)} node(s)")
    print(
        f"{'Node':<16} {'Version':<10} {'State':<12} "
        f"{'GPUs':>5} {'Idle':>5} {'Mine':>5} {'Actor':<18}"
    )
    failed = 0
    reservations = []
    for reply in replies:
        if not reply.node.enabled:
            print(
                f"{reply.node.name:<16} {'-':<10} {'disabled':<12} "
                f"{'-':>5} {'-':>5} {'-':>5} {'maintenance':<18}"
            )
            continue
        if reply.error:
            failed += 1
            print(
                f"{reply.node.name:<16} {'-':<10} {'unreachable':<12} "
                f"{'-':>5} {'-':>5} {'-':>5} {_clip(reply.error, 18):<18}"
            )
            continue
        payload = reply.payload or {}
        software = payload.get("software", {})
        version = (
            software.get("version", "legacy")
            if isinstance(software, dict)
            else "legacy"
        )
        raw_policy = payload.get("policy")
        policy = raw_policy if isinstance(raw_policy, dict) else {}
        advice = payload.get("gpu_advice")
        raw_gpus = advice.get("gpus") if isinstance(advice, dict) else None
        gpus = (
            [gpu for gpu in raw_gpus if isinstance(gpu, dict)]
            if isinstance(raw_gpus, list)
            else []
        )
        idle = sum(
            1
            for gpu in gpus
            if isinstance(gpu.get("live"), dict) and gpu["live"].get("status") == "idle"
        )
        raw_reservations = payload.get("reservations")
        node_reservations = (
            [item for item in raw_reservations if isinstance(item, dict)]
            if isinstance(raw_reservations, list)
            else []
        )
        mine = sum(1 for item in node_reservations if item.get("mine"))
        raw_actor = payload.get("actor")
        actor = raw_actor if isinstance(raw_actor, dict) else {}
        principal = _principal_for(config, reply.node.node_id, actor.get("uid"))
        actor_text = (
            principal or f"{actor.get('username', '?')}:{actor.get('uid', '?')}"
        )
        monitoring = policy.get("monitoring") if isinstance(policy, dict) else None
        collector = (
            monitoring.get("collector") if isinstance(monitoring, dict) else None
        )
        state = (
            collector.get("state", "ok") if isinstance(collector, dict) else "unknown"
        )
        skew = _clock_skew_seconds(payload)
        if skew is None or skew > MAX_CLOCK_SKEW_SECONDS:
            state = "clock-skew"
        raw_gpu_count = policy.get("gpu_count")
        gpu_count = (
            raw_gpu_count
            if isinstance(raw_gpu_count, int) and not isinstance(raw_gpu_count, bool)
            else len(gpus)
        )
        print(
            f"{reply.node.name:<16} {_clip(str(version), 10):<10} "
            f"{_clip(str(state), 12):<12} "
            f"{gpu_count:>5} {idle:>5} {mine:>5} "
            f"{_clip(actor_text, 18):<18}"
        )
        reservations.extend((reply.node.name, item) for item in node_reservations)
    if reservations:
        print("\nReservations")
        print(
            f"{'ID':<25} {'Own':<3} {'User':<16} {'Mode':<6} {'Req':>5} "
            f"{'VRAM':>8} {'GPU':<10} {'Start':<22} {'End':<22}"
        )
        for node_name, reservation in sorted(
            reservations,
            key=lambda item: (str(item[1].get("start_at", "")), item[0]),
        ):
            short_id = reservation.get("short_id") or str(reservation.get("id", ""))[:8]
            qualified = f"{node_name}/{short_id}"
            print(
                f"{_clip(qualified, 25):<25} "
                f"{('*' if reservation.get('mine') is True else '-'):<3} "
                f"{_clip(str(reservation.get('username', '?')), 16):<16} "
                f"{_reservation_mode_text(reservation):<6} "
                f"{_reservation_request_text(reservation):>5} "
                f"{_reservation_memory_text(reservation):>8} "
                f"{_clip(_gpu_text(reservation.get('gpus')), 10):<10} "
                f"{_clip(_local_time(reservation.get('start_at')), 22):<22} "
                f"{_clip(_local_time(reservation.get('end_at')), 22):<22}"
            )
    enabled = config.enabled_nodes
    return 3 if not enabled or failed == len(enabled) else 0


def _cluster_check(config: ClusterConfig, *, json_output: bool = False) -> int:
    replies = query_cluster_contexts(config)
    required = tuple(
        dict.fromkeys((*_BOOK_CAPABILITIES, *_EDIT_CAPABILITIES, *_CANCEL_CAPABILITIES))
    )
    checks = []
    for reply in replies:
        check = {
            "name": reply.node.name,
            "node_id": reply.node.node_id,
            "enabled": reply.node.enabled,
            "status": "disabled" if not reply.node.enabled else "ready",
            "version": None,
            "actor": None,
            "clock_skew_seconds": None,
            "missing_capabilities": [],
            "warnings": [],
            "error": None,
        }
        if not reply.node.enabled:
            checks.append(check)
            continue
        if reply.error:
            check["status"] = "failed"
            check["error"] = reply.error
            checks.append(check)
            continue
        payload = reply.payload or {}
        software = payload.get("software")
        if isinstance(software, dict):
            check["version"] = software.get("version")
        actor = payload.get("actor")
        if isinstance(actor, dict):
            check["actor"] = {
                "uid": actor.get("uid"),
                "username": actor.get("username"),
            }
        skew = _clock_skew_seconds(payload)
        check["clock_skew_seconds"] = None if skew is None else round(skew, 3)
        missing = _missing_capabilities(payload, required)
        check["missing_capabilities"] = missing
        if missing:
            check["status"] = "failed"
            check["error"] = f"missing write capabilities: {','.join(missing)}"
        elif skew is None or skew > MAX_CLOCK_SKEW_SECONDS:
            check["status"] = "failed"
            check["error"] = "clock is unavailable or outside the 30s limit"
        elif (
            not isinstance(actor, dict)
            or isinstance(actor.get("uid"), bool)
            or not isinstance(actor.get("uid"), int)
        ):
            check["status"] = "failed"
            check["error"] = "remote actor identity is unavailable"
        policy = payload.get("policy")
        gpu_count = policy.get("gpu_count") if isinstance(policy, dict) else None
        if check["status"] == "ready" and (
            isinstance(gpu_count, bool)
            or not isinstance(gpu_count, int)
            or gpu_count < 1
        ):
            check["status"] = "failed"
            check["error"] = "node advertises no schedulable GPUs"
        monitoring = policy.get("monitoring") if isinstance(policy, dict) else None
        collector = (
            monitoring.get("collector") if isinstance(monitoring, dict) else None
        )
        collector_state = (
            collector.get("state") if isinstance(collector, dict) else None
        )
        if collector_state not in {"running", "ready"}:
            check["warnings"].append(
                f"telemetry collector state is {collector_state or 'unknown'}"
            )
        checks.append(check)

    enabled = [item for item in checks if item["enabled"]]
    failures = [item for item in enabled if item["status"] == "failed"]
    warnings = sum(len(item["warnings"]) for item in enabled)
    ready = bool(enabled) and not failures
    payload = {
        "schema_version": CLUSTER_SCHEMA_VERSION,
        "kind": "cluster-check",
        "ready": ready,
        "summary": {
            "configured": len(checks),
            "enabled": len(enabled),
            "disabled": len(checks) - len(enabled),
            "failed": len(failures),
            "warnings": warnings,
        },
        "nodes": checks,
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if ready else 3
    state = "ready" if ready else "not ready"
    print(
        f"Cluster check: {state} | {len(enabled)} enabled, "
        f"{len(checks) - len(enabled)} disabled, {warnings} warning(s)"
    )
    for item in checks:
        if item["status"] == "disabled":
            print(f"skip {item['name']}: disabled by administrator")
            continue
        if item["status"] == "failed":
            print(f"fail {item['name']}: {item['error']}")
            continue
        actor = item["actor"] or {}
        version = item["version"] or "unknown"
        skew = item["clock_skew_seconds"]
        clock = f"{skew:.1f}s" if isinstance(skew, (int, float)) else "unknown"
        print(
            f"pass {item['name']}: v{version} actor="
            f"{actor.get('username', '?')}:{actor.get('uid', '?')} clock={clock}"
        )
        for warning in item["warnings"]:
            print(f"warn {item['name']}: {warning}")
    if not enabled:
        print("fail cluster: no enabled nodes; ask an administrator to enable one")
    return 0 if ready else 3


def _cluster_recommend(
    config: ClusterConfig,
    intent: ClusterBookingIntent,
    *,
    book: bool,
) -> int:
    _validate_cluster_booking_intent(intent)
    required_capabilities = _booking_capabilities(intent.command_argv)
    operation_id = intent.operation_id or str(uuid.uuid4())
    replay_node = None
    if book and intent.operation_id is not None:
        replay_node = _find_cluster_operation_node(config, operation_id)
    replies = (
        []
        if replay_node is not None
        else _parallel(_require_enabled_nodes(config), intent.recommendation_argv())
    )
    candidates, rejected = _rank_cluster_candidates(intent, replies)
    if replay_node is None and not candidates:
        _raise_no_cluster_candidate(replies, rejected)
    if not book:
        return _emit_cluster_recommendation(intent, replies, candidates, rejected)

    if replay_node is not None:
        selected = _invoke(replay_node, ["agent", "context", "--compact"])
        _require_write_capabilities(selected, required_capabilities)
    else:
        selected = _select_cluster_write_candidate(
            candidates,
            required_capabilities,
        )
    return _submit_cluster_booking(selected, intent, operation_id)


def _validate_cluster_booking_intent(intent: ClusterBookingIntent) -> None:
    try:
        if intent.count < 1:
            raise ValueError("GPU count must be >= 1")
        parse_duration_seconds(intent.duration)
        if intent.start is not None:
            parse_iso(intent.start)
        if intent.gpu is not None:
            _parse_gpu_option(intent.gpu, "--gpu")
        if intent.exclude_gpu is not None:
            _parse_gpu_option(intent.exclude_gpu, "--exclude-gpu")
        if intent.memory is not None:
            parse_memory_mb(intent.memory)
        if intent.share is not None and intent.share < 1:
            raise ValueError("--share must be >= 1")
    except ValueError as exc:
        raise BookingError(str(exc)) from exc


def _append_cluster_booking_options(
    argv: list[str],
    intent: ClusterBookingIntent,
    *,
    compact: bool = False,
) -> list[str]:
    result = list(argv)
    for flag, value in (
        ("--start", intent.start),
        ("--gpu", intent.gpu),
        ("--exclude-gpu", intent.exclude_gpu),
        ("--mem", intent.memory),
        ("--share", str(intent.share) if intent.share is not None else None),
    ):
        if value is not None:
            result += [flag, value]
    if compact:
        result.append("--compact")
    return result


def _rank_cluster_candidates(
    intent: ClusterBookingIntent,
    replies: Sequence[NodeReply],
) -> tuple[list[ClusterCandidate], dict[str, str]]:
    candidates = []
    rejected: dict[str, str] = {}
    ranking_now = utc_now()
    try:
        duration_seconds = parse_duration_seconds(intent.duration)
        exact_start = parse_iso(intent.start) if intent.start is not None else None
    except ValueError as exc:
        raise BookingError(str(exc)) from exc
    for reply in replies:
        if reply.error:
            continue
        payload = reply.payload or {}
        recommendation = payload.get("recommendation")
        skew = _clock_skew_seconds(payload)
        if payload.get("available") is False:
            rejected[reply.node.name] = "no legal slot"
            continue
        if not isinstance(recommendation, dict) or skew is None:
            rejected[reply.node.name] = "invalid recommendation"
            continue
        if intent.start is not None and skew > MAX_CLOCK_SKEW_SECONDS:
            rejected[reply.node.name] = f"clock skew is {skew:.0f}s"
            continue
        try:
            remote_start = parse_iso(str(recommendation["start_at"]))
            remote_end = parse_iso(str(recommendation["end_at"]))
            gpus = _gpu_indices(recommendation.get("gpus"))
            if (
                gpus is None
                or len(gpus) != intent.count
                or len(set(gpus)) != len(gpus)
                or remote_end <= remote_start
                or int((remote_end - remote_start).total_seconds()) != duration_seconds
                or (exact_start is not None and remote_start != exact_start)
            ):
                raise ValueError("invalid recommendation placement")
            request_error = _recommendation_request_error(
                intent,
                payload.get("request"),
                duration_seconds=duration_seconds,
                exact_start=exact_start,
            )
            if request_error is not None:
                rejected[reply.node.name] = request_error
                continue
            if intent.start is None:
                generated_at = parse_iso(str(payload["generated_at"]))
                wait = max(remote_start - generated_at, timedelta(0))
                ranked_start = ranking_now + wait
            else:
                ranked_start = remote_start
        except (KeyError, TypeError, ValueError):
            rejected[reply.node.name] = "invalid recommendation"
            continue
        candidates.append(
            ClusterCandidate(
                ranked_start,
                _confidence_rank(recommendation.get("confidence")),
                reply,
            )
        )
    return sorted(candidates, key=lambda item: item.sort_key), rejected


def _recommendation_request_error(
    intent: ClusterBookingIntent,
    request: object,
    *,
    duration_seconds: int,
    exact_start: Optional[datetime],
) -> Optional[str]:
    # Older read-only nodes may not echo a request. Current nodes do, so validate
    # every field the client can normalize without relying on node-local policy.
    if request is None:
        return None
    if not isinstance(request, dict):
        return "invalid recommendation request echo"
    expected = {
        "count": intent.count,
        "duration_seconds": duration_seconds,
        "mode": intent.mode,
        "allow_queue": intent.start is None,
    }
    if any(request.get(key) != value for key, value in expected.items()):
        return "recommendation request echo does not match this request"
    if exact_start is not None:
        try:
            if parse_iso(str(request.get("start_at"))) != exact_start:
                return "recommendation request echo has a different start"
        except ValueError:
            return "invalid recommendation request start"
    if intent.gpu is not None:
        try:
            preferred = _parse_gpu_option(intent.gpu, "--gpu")
        except ValueError as exc:
            raise BookingError(str(exc)) from exc
        if request.get("preferred_gpus") != preferred:
            return "recommendation request echo has different GPUs"
    if intent.exclude_gpu is not None:
        try:
            excluded = _parse_gpu_option(intent.exclude_gpu, "--exclude-gpu")
        except ValueError as exc:
            raise BookingError(str(exc)) from exc
        if request.get("excluded_gpus") != excluded:
            return "recommendation request echo has different excluded GPUs"
    if intent.memory is not None:
        try:
            memory = parse_memory_mb(intent.memory)
        except ValueError as exc:
            raise BookingError(str(exc)) from exc
        if request.get("expected_memory_mb_per_gpu") != memory:
            return "recommendation request echo has a different memory budget"
    if intent.share is not None and request.get("share_units_per_gpu") != intent.share:
        return "recommendation request echo has a different shared-slot request"
    return None


def _parse_gpu_option(value: str, label: str) -> list[int]:
    result = []
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            index = int(item)
        except ValueError as exc:
            raise ValueError(
                f"{label} must contain comma-separated GPU indexes"
            ) from exc
        if index < 0:
            raise ValueError(f"{label} GPU indexes must be non-negative")
        result.append(index)
    if not result:
        raise ValueError(f"{label} must contain at least one GPU index")
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must not contain repeated GPU indexes")
    return result


def _raise_no_cluster_candidate(
    replies: Sequence[NodeReply],
    rejected: dict[str, str],
) -> None:
    reasons = "; ".join(
        f"{reply.node.name}: "
        f"{reply.error or rejected.get(reply.node.name) or 'no legal slot'}"
        for reply in replies
    )
    raise BookingError(
        "no cluster node can satisfy this request"
        + (f" ({reasons})" if reasons else "")
    )


def _select_cluster_write_candidate(
    candidates: Sequence[ClusterCandidate],
    required_capabilities: Sequence[str] = _BOOK_CAPABILITIES,
) -> NodeReply:
    missing_by_node = {
        candidate.reply.node.name: _missing_capabilities(
            candidate.reply.payload,
            required_capabilities,
        )
        for candidate in candidates
    }
    for candidate in candidates:
        if not missing_by_node[candidate.reply.node.name]:
            return candidate.reply
    reasons = "; ".join(
        f"{candidate.reply.node.name}: missing "
        f"{','.join(missing_by_node[candidate.reply.node.name])}"
        for candidate in candidates
    )
    raise BookingError(
        "no write-compatible cluster node can satisfy this request"
        + (f" ({reasons})" if reasons else "")
    )


def _emit_cluster_recommendation(
    intent: ClusterBookingIntent,
    replies: Sequence[NodeReply],
    candidates: Sequence[ClusterCandidate],
    rejected: dict[str, str],
) -> int:
    selected = candidates[0].reply
    required_capabilities = _booking_capabilities(intent.command_argv)
    if intent.json_output:
        print(
            json.dumps(
                {
                    "schema_version": CLUSTER_SCHEMA_VERSION,
                    "kind": "cluster-recommendation",
                    "selected_node": selected.node.name,
                    "nodes": [
                        {
                            "name": reply.node.name,
                            "node_id": reply.node.node_id,
                            "priority": reply.node.priority,
                            "error": reply.error,
                            "rejected_reason": rejected.get(reply.node.name),
                            "write_compatible": not _missing_capabilities(
                                reply.payload,
                                required_capabilities,
                            ),
                            "recommendation": reply.payload,
                        }
                        for reply in replies
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        _print_cluster_candidates(
            candidates,
            replies,
            rejected,
            required_capabilities,
        )
    return 0


def _submit_cluster_booking(
    selected: NodeReply,
    intent: ClusterBookingIntent,
    operation_id: str,
) -> int:
    result = _invoke_idempotent_write(
        selected.node,
        intent.booking_argv(operation_id),
        operation_id,
        expected_action="create",
    )
    if result.error:
        if result.error_code != "uncertain":
            raise BookingError(
                f"cluster booking on {selected.node.name} was rejected: {result.error}"
            )
        raise BookingError(
            f"cluster booking on {selected.node.name} is unresolved: {result.error}; "
            f"retry the same request with --op-id {operation_id}"
        )
    payload = result.payload or {}
    reservation = payload.get("reservation", {})
    if intent.json_output:
        print(
            json.dumps(
                {
                    "schema_version": CLUSTER_SCHEMA_VERSION,
                    "kind": "cluster-booking-result",
                    "node": {
                        "name": selected.node.name,
                        "node_id": selected.node.node_id,
                    },
                    "operation_id": operation_id,
                    "result": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    print(
        f"{payload.get('status', 'created')} on {selected.node.name}: "
        f"{reservation.get('short_id', reservation.get('id', '?'))} "
        f"GPU={_gpu_text(reservation.get('gpus'))} "
        f"{_local_time(reservation.get('start_at'))} -> "
        f"{_local_time(reservation.get('end_at'))}"
    )
    return 0


def _cluster_usage(
    config: ClusterConfig,
    forwarded: list[str],
    *,
    json_output: bool,
    compact: bool,
) -> int:
    remote_args = ["usage", "me", *forwarded, "--json", "--compact"]
    replies = _parallel(_require_enabled_nodes(config), remote_args)
    replies.extend(_disabled_replies(config))
    replies.sort(key=lambda item: (item.node.priority, item.node.name))
    principals = _aggregate_cluster_usage(config, replies)
    payload = {
        "schema_version": CLUSTER_SCHEMA_VERSION,
        "kind": "cluster-usage",
        "principals": principals,
        "nodes": [
            {
                "name": reply.node.name,
                "node_id": reply.node.node_id,
                "enabled": reply.node.enabled,
                "available": reply.node.enabled and reply.error is None,
                "error": reply.error,
                "usage": reply.payload,
            }
            for reply in replies
        ],
    }
    if json_output:
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=None if compact else 2,
                sort_keys=True,
            )
        )
    else:
        print("Cluster usage | sampled history only; future reservations excluded")
        print(
            f"{'Principal':<26} {'Nodes':>5} {'Active':>10} {'Reserved':>10} {'Idle':>10} {'Viol':>10}"
        )
        for principal in principals:
            print(
                f"{_clip(principal['id'], 26):<26} {len(principal['nodes']):>5} "
                f"{_duration(principal['active_gpu_seconds']):>10} "
                f"{_duration(principal['reserved_gpu_seconds']):>10} "
                f"{_duration(principal['idle_reserved_gpu_seconds']):>10} "
                f"{_duration(principal['violation_gpu_seconds']):>10}"
            )
        for reply in replies:
            if reply.error and reply.node.enabled:
                print(f"warning: {reply.node.name}: {reply.error}")
    enabled_replies = [reply for reply in replies if reply.node.enabled]
    return (
        3 if not enabled_replies or all(reply.error for reply in enabled_replies) else 0
    )


def _cluster_history(config: ClusterConfig, args: argparse.Namespace) -> int:
    if config.history_root is None:
        raise BookingError(
            "cluster history is not configured; an administrator can set it with "
            "sudo bk admin cluster history-root PATH --yes"
        )

    from .cluster_history import load_archived_user_usage, resolve_history_window

    start, end = resolve_history_window(
        config.history_root,
        config.nodes[0].node_id,
        since=args.since,
        start=args.start,
        until=args.until,
    )
    archived, report = load_archived_user_usage(
        config.history_root,
        start=start,
        end=end,
        node_ids=[node.node_id for node in config.nodes],
    )
    by_id = {node.node_id: node for node in config.nodes}
    replies = [
        NodeReply(by_id[item.node_id], item.payload, None)
        for item in archived
        if item.node_id in by_id
    ]
    principals = _aggregate_cluster_usage(config, replies)
    scope = "all"
    if not args.all:
        local = next((node for node in config.nodes if node.transport == "local"), None)
        if local is None:
            raise BookingError(
                "personal archive scope needs one local catalog node; use --all for an "
                "administrator-wide view"
            )
        scope = _principal_for(config, local.node_id, os.getuid()) or (
            f"{local.node_id}:{os.getuid()}"
        )
        principals = [item for item in principals if item["id"] == scope]
    payload = {
        "schema_version": CLUSTER_SCHEMA_VERSION,
        "kind": "cluster-history-usage",
        "scope": scope,
        "archive": report,
        "principals": principals,
    }
    if args.json:
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=None if args.compact else 2,
                sort_keys=True,
            )
        )
        return 0
    print(
        "Archived cluster usage | verified complete UTC-day chunks | "
        f"{start:%Y-%m-%d} -> {end:%Y-%m-%d}"
    )
    print(
        f"archive={config.history_root} generations={report['generations']} "
        f"chunks={report['chunks']} scope={scope}"
    )
    if not principals:
        print("no archived usage for this scope and interval")
        return 0
    print(
        f"{'Principal':<26} {'Nodes':>5} {'Active':>10} {'Reserved':>10} "
        f"{'Idle':>10} {'Viol':>10}"
    )
    for principal in principals:
        print(
            f"{_clip(principal['id'], 26):<26} {len(principal['nodes']):>5} "
            f"{_duration(principal['active_gpu_seconds']):>10} "
            f"{_duration(principal['reserved_gpu_seconds']):>10} "
            f"{_duration(principal['idle_reserved_gpu_seconds']):>10} "
            f"{_duration(principal['violation_gpu_seconds']):>10}"
        )
    return 0


def _aggregate_cluster_usage(
    config: ClusterConfig, replies: Sequence[NodeReply]
) -> list[dict]:
    groups: dict[str, dict] = {}
    for reply in replies:
        if reply.error or not reply.payload:
            continue
        raw_users = reply.payload.get("users")
        users = raw_users if isinstance(raw_users, list) else []
        for user in users:
            if not isinstance(user, dict):
                continue
            uid = user.get("uid")
            if isinstance(uid, bool) or not isinstance(uid, int):
                continue
            principal_id = _principal_for(config, reply.node.node_id, uid)
            identity = principal_id or f"{reply.node.node_id}:{uid}"
            group = groups.setdefault(
                identity,
                {
                    "id": identity,
                    "mapped": principal_id is not None,
                    "nodes": set(),
                    "members": [],
                    "_member_keys": set(),
                    "active_gpu_seconds": 0.0,
                    "reserved_gpu_seconds": 0.0,
                    "idle_reserved_gpu_seconds": 0.0,
                    "violation_gpu_seconds": 0.0,
                    "sampled_gpu_seconds": 0.0,
                    "max_gpu_memory_mb": 0,
                    "_sm_weighted": 0.0,
                },
            )
            group["nodes"].add(reply.node.name)
            member_key = (reply.node.node_id, uid)
            if member_key not in group["_member_keys"]:
                group["_member_keys"].add(member_key)
                group["members"].append(
                    {
                        "node": reply.node.name,
                        "node_id": reply.node.node_id,
                        "uid": uid,
                        "username": user.get("username"),
                    }
                )
            for key in (
                "active_gpu_seconds",
                "reserved_gpu_seconds",
                "idle_reserved_gpu_seconds",
                "violation_gpu_seconds",
                "sampled_gpu_seconds",
            ):
                group[key] += _nonnegative_number(user.get(key))
            memory = _nonnegative_number(user.get("max_gpu_memory_mb"))
            group["max_gpu_memory_mb"] = max(group["max_gpu_memory_mb"], int(memory))
            average = user.get("avg_sm_percent")
            if (
                isinstance(average, (int, float))
                and not isinstance(average, bool)
                and math.isfinite(float(average))
            ):
                group["_sm_weighted"] += float(average) * max(
                    0.0, _nonnegative_number(user.get("sampled_gpu_seconds"))
                )
    result = []
    for identity in sorted(groups):
        group = groups[identity]
        group.pop("_member_keys")
        sampled = group["sampled_gpu_seconds"]
        group["avg_sm_percent"] = (
            round(group.pop("_sm_weighted") / sampled, 3) if sampled else None
        )
        group["nodes"] = sorted(group["nodes"])
        for key in (
            "active_gpu_seconds",
            "reserved_gpu_seconds",
            "idle_reserved_gpu_seconds",
            "violation_gpu_seconds",
            "sampled_gpu_seconds",
        ):
            group[key] = round(group[key], 3)
        result.append(group)
    return result


def _cluster_edit(config: ClusterConfig, args: argparse.Namespace) -> int:
    if (
        not any(
            value is not None
            for value in (
                args.duration,
                args.start,
                args.gpu,
                args.exclude_gpu,
                args.count,
                args.mode,
                args.mem,
                args.share,
            )
        )
        and not args.queue
    ):
        raise BookingError("cluster edit requires at least one changed field")
    node, reservation_id = _qualified_reservation(config, args.reservation_id)
    arguments = []
    for flag, value in (
        ("--duration", args.duration),
        ("--start", args.start),
        ("--gpu", args.gpu),
        ("--exclude-gpu", args.exclude_gpu),
        ("--count", str(args.count) if args.count is not None else None),
        ("--mode", args.mode),
        ("--mem", args.mem),
        ("--share", str(args.share) if args.share is not None else None),
    ):
        if value is not None:
            arguments += [flag, value]
    if args.queue:
        arguments.append("--queue")
    context = _invoke(node, ["agent", "context", "--compact"])
    _require_write_capabilities(context, _EDIT_CAPABILITIES)
    operation_id = args.op_id or str(uuid.uuid4())
    arguments += ["--op-id", operation_id]
    reply = _invoke_idempotent_write(
        node,
        ["agent", "edit", reservation_id, *arguments, "--compact"],
        operation_id,
        expected_action="edit",
    )
    return _print_cluster_mutation(
        reply,
        action="edit",
        operation_id=operation_id,
        json_output=args.json,
    )


def _cluster_cancel(config: ClusterConfig, args: argparse.Namespace) -> int:
    node, reservation_id = _qualified_reservation(config, args.reservation_id)
    context = _invoke(node, ["agent", "context", "--compact"])
    _require_write_capabilities(context, _CANCEL_CAPABILITIES)
    operation_id = args.op_id or str(uuid.uuid4())
    reply = _invoke_idempotent_write(
        node,
        [
            "agent",
            "cancel",
            reservation_id,
            "--op-id",
            operation_id,
            "--compact",
        ],
        operation_id,
        expected_action="cancel",
    )
    return _print_cluster_mutation(
        reply,
        action="cancel",
        operation_id=operation_id,
        json_output=args.json,
    )


def _qualified_reservation(
    config: ClusterConfig, value: str
) -> tuple[ClusterNode, str]:
    node_name, separator, reservation_id = value.partition("/")
    if not separator or not reservation_id:
        raise BookingError("use a node-qualified reservation ID such as gpu-a/1a2b3c")
    node = config.node(node_name)
    if not node.enabled:
        raise BookingError(
            f"cluster node {node.name!r} is disabled by the administrator"
        )
    return node, reservation_id


def _print_cluster_mutation(
    reply: NodeReply,
    *,
    action: str,
    operation_id: str,
    json_output: bool,
) -> int:
    if reply.error:
        if reply.error_code == "uncertain":
            raise BookingError(
                f"cluster {action} on {reply.node.name} is unresolved: {reply.error}; "
                f"retry the same request with --op-id {operation_id}"
            )
        raise BookingError(f"node {reply.node.name}: {reply.error}")
    payload = reply.payload or {}
    if json_output:
        print(
            json.dumps(
                {
                    "schema_version": CLUSTER_SCHEMA_VERSION,
                    "kind": "cluster-mutation-result",
                    "node": reply.node.name,
                    "node_id": reply.node.node_id,
                    "operation_id": operation_id,
                    "result": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    reservation = payload.get("reservation", {})
    short_id = reservation.get("short_id") or str(reservation.get("id", ""))[:8]
    print(
        f"{payload.get('status', payload.get('kind', 'updated'))}: {reply.node.name}/{short_id}"
    )
    return 0


def _parallel(
    nodes: Sequence[ClusterNode],
    argv: list[str],
    cancel_event: Optional[Event] = None,
) -> list[NodeReply]:
    if not nodes:
        return []
    replies = []
    with ThreadPoolExecutor(max_workers=min(16, len(nodes))) as executor:
        futures = {
            executor.submit(_invoke, node, argv, cancel_event=cancel_event): node
            for node in nodes
        }
        for future in as_completed(futures):
            try:
                replies.append(future.result())
            except Exception as exc:
                replies.append(NodeReply(futures[future], None, str(exc)))
    return sorted(replies, key=lambda item: (item.node.priority, item.node.name))


def query_cluster_contexts(
    config: ClusterConfig,
    cancel_event: Optional[Event] = None,
) -> list[NodeReply]:
    replies = _parallel(
        config.enabled_nodes,
        ["agent", "context", "--compact"],
        cancel_event,
    )
    replies.extend(_disabled_replies(config))
    return sorted(replies, key=lambda item: (item.node.priority, item.node.name))


def _invoke(
    node: ClusterNode,
    argv: list[str],
    *,
    cancel_event: Optional[Event] = None,
) -> NodeReply:
    return _invoke_once(node, argv, cancel_event=cancel_event)


def _invoke_idempotent_write(
    node: ClusterNode,
    argv: list[str],
    operation_id: str,
    *,
    expected_action: Optional[str] = None,
) -> NodeReply:
    result = _invoke(node, argv)
    if result.error is None or result.error_code in {"remote", "identity", "cancelled"}:
        return result

    recovered = _probe_operation(node, operation_id, expected_action=expected_action)
    if recovered.error is None and recovered.payload is not None:
        return recovered
    if recovered.error_code == "operation-conflict":
        return recovered
    if recovered.error is not None:
        return NodeReply(
            node,
            None,
            f"operation {operation_id}: {result.error}; operation status is unknown "
            f"({recovered.error})",
            timed_out=result.timed_out,
            error_code="uncertain",
        )

    retried = _invoke(node, argv)
    if retried.error is None or retried.error_code in {
        "remote",
        "identity",
        "cancelled",
    }:
        return retried
    recovered = _probe_operation(node, operation_id, expected_action=expected_action)
    if recovered.error is None and recovered.payload is not None:
        return recovered
    if recovered.error_code == "operation-conflict":
        return recovered
    detail = recovered.error or "operation ID was not found"
    return NodeReply(
        node,
        None,
        f"operation {operation_id}: {retried.error}; operation status is unknown "
        f"({detail})",
        timed_out=retried.timed_out,
        error_code="uncertain",
    )


def _probe_operation(
    node: ClusterNode,
    operation_id: str,
    *,
    expected_action: Optional[str] = None,
) -> NodeReply:
    reply = _invoke(
        node,
        ["agent", "operation", operation_id, "--compact"],
    )
    if reply.error:
        return reply
    payload = reply.payload or {}
    if payload.get("kind") != "operation_status":
        return NodeReply(
            node,
            None,
            "operation query returned an unexpected response",
            error_code="protocol",
        )
    if not payload.get("found"):
        return NodeReply(node, None, None)
    action = payload.get("action")
    if expected_action is not None and action != expected_action:
        return NodeReply(
            node,
            None,
            f"operation ID is already bound to {action or 'an unknown action'}, "
            f"not {expected_action}",
            error_code="operation-conflict",
        )
    reservation = payload.get("reservation")
    if not isinstance(reservation, dict):
        return NodeReply(
            node,
            None,
            "operation query returned an invalid reservation",
            error_code="protocol",
        )
    return NodeReply(
        node,
        {**payload, "status": "exists"},
        None,
    )


def _find_cluster_operation_node(
    config: ClusterConfig,
    operation_id: str,
) -> Optional[ClusterNode]:
    replies = _parallel(
        config.enabled_nodes,
        ["agent", "operation", operation_id, "--compact"],
    )
    replies.extend(_disabled_replies(config))
    found = []
    failures = []
    for reply in replies:
        if reply.error:
            failures.append(f"{reply.node.name}: {reply.error}")
            continue
        payload = reply.payload or {}
        if payload.get("kind") != "operation_status":
            failures.append(f"{reply.node.name}: invalid operation response")
            continue
        if payload.get("found"):
            if not isinstance(payload.get("reservation"), dict):
                failures.append(f"{reply.node.name}: invalid operation reservation")
                continue
            found.append(reply.node)
    if len(found) > 1:
        names = ",".join(node.name for node in found)
        raise BookingError(
            f"operation ID exists on multiple cluster nodes ({names}); refusing to write"
        )
    if found:
        return found[0]
    if failures:
        raise BookingError(
            "cannot safely route this retry because operation status is unavailable "
            f"on {len(failures)} node(s): {'; '.join(failures)}"
        )
    return None


def _missing_capabilities(
    payload: Optional[dict],
    required: Sequence[str],
) -> list[str]:
    capabilities = payload.get("capabilities") if isinstance(payload, dict) else None
    if not isinstance(capabilities, dict):
        return list(required)
    return [name for name in required if capabilities.get(name) is not True]


def _require_write_capabilities(
    reply: NodeReply,
    required: Sequence[str],
) -> None:
    if reply.error:
        raise BookingError(f"node {reply.node.name}: {reply.error}")
    missing = _missing_capabilities(reply.payload, required)
    if missing:
        raise BookingError(
            f"node {reply.node.name} is read-only for this cluster client; "
            f"missing capabilities: {','.join(missing)}"
        )


def _argument_value(argv: Sequence[str], option: str) -> Optional[str]:
    for index, item in enumerate(argv):
        if item == option:
            if index + 1 >= len(argv):
                raise BookingError(f"{option} requires a value")
            return argv[index + 1]
        prefix = option + "="
        if item.startswith(prefix):
            return item[len(prefix) :]
    return None


def _parse_cluster_config(path: Path, document: object) -> ClusterConfig:
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != CLUSTER_SCHEMA_VERSION
    ):
        raise BookingError(f"unsupported or invalid cluster catalog: {path}")
    unknown = set(document) - {
        "schema_version",
        "nodes",
        "principals",
        "history_root",
    }
    if unknown:
        raise BookingError(f"unknown cluster catalog field: {sorted(unknown)[0]}")
    raw_nodes = document.get("nodes")
    if not isinstance(raw_nodes, list) or not 1 <= len(raw_nodes) <= MAX_CLUSTER_NODES:
        raise BookingError(
            f"cluster catalog nodes must contain 1-{MAX_CLUSTER_NODES} entries"
        )
    nodes = tuple(_parse_node(item) for item in raw_nodes)
    if len({node.name for node in nodes}) != len(nodes):
        raise BookingError("cluster node names must be unique")
    if len({node.node_id for node in nodes}) != len(nodes):
        raise BookingError("cluster stable node IDs must be unique")
    if sum(node.transport == "local" for node in nodes) > 1:
        raise BookingError("cluster catalog may contain at most one local node")
    local = [node for node in nodes if node.transport == "local"]
    if local and local[0].node_id != stable_node_identity()["id"]:
        raise BookingError("local cluster node ID does not match this machine")
    principals = document.get("principals", [])
    if not isinstance(principals, list):
        raise BookingError("cluster principals must be a list")
    normalized_principals = tuple(
        _validate_principal(item, nodes) for item in principals
    )
    principal_ids = [str(item["id"]) for item in normalized_principals]
    if len(set(principal_ids)) != len(principal_ids):
        raise BookingError("cluster principal IDs must be unique")
    seen_members: set[tuple[str, int]] = set()
    for principal in normalized_principals:
        for member in principal["members"]:
            key = str(member["node_id"]), int(member["uid"])
            if key in seen_members:
                raise BookingError(
                    "one node UID must not belong to multiple cluster principals"
                )
            seen_members.add(key)
    history_root_value = document.get("history_root")
    history_root = None
    if history_root_value is not None:
        if (
            not isinstance(history_root_value, str)
            or not Path(history_root_value).is_absolute()
            or any(ord(character) < 32 for character in history_root_value)
        ):
            raise BookingError("cluster history_root must be an absolute safe path")
        history_root = Path(history_root_value)
    return ClusterConfig(path, nodes, normalized_principals, history_root)


def _parse_node(value: object) -> ClusterNode:
    if not isinstance(value, dict):
        raise BookingError("cluster node must be an object")
    unknown = set(value) - {
        "name",
        "node_id",
        "transport",
        "target",
        "executable",
        "priority",
        "timeout_seconds",
        "enabled",
    }
    if unknown:
        raise BookingError(f"unknown cluster node field: {sorted(unknown)[0]}")
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise BookingError("cluster node enabled must be true or false")
    name = value.get("name")
    node_id = value.get("node_id")
    transport = value.get("transport")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise BookingError("cluster node name is invalid")
    if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
        raise BookingError(f"cluster node {name} has an invalid stable node ID")
    if transport not in {"local", "ssh"}:
        raise BookingError(f"cluster node {name} transport must be local or ssh")
    target = value.get("target")
    if transport == "ssh" and (
        not isinstance(target, str)
        or not _SSH_TARGET.fullmatch(target)
        or target.startswith("-")
    ):
        raise BookingError(f"cluster node {name} has an invalid SSH target")
    if transport == "local" and target is not None:
        raise BookingError(f"local cluster node {name} must not define target")
    executable = value.get("executable", "/usr/local/bin/bk")
    if (
        not isinstance(executable, str)
        or not Path(executable).is_absolute()
        or any(ord(ch) < 32 for ch in executable)
    ):
        raise BookingError(
            f"cluster node {name} executable must be an absolute safe path"
        )
    priority = value.get("priority", 0)
    timeout = value.get("timeout_seconds", 8)
    if (
        isinstance(priority, bool)
        or not isinstance(priority, int)
        or not 0 <= priority <= 1_000_000
    ):
        raise BookingError(f"cluster node {name} priority is invalid")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1 <= timeout <= 300
    ):
        raise BookingError(f"cluster node {name} timeout_seconds must be 1-300")
    return ClusterNode(
        name,
        node_id,
        transport,
        target,
        executable,
        priority,
        float(timeout),
        enabled,
    )


def _validate_principal(value: object, nodes: Sequence[ClusterNode]) -> dict:
    if not isinstance(value, dict) or set(value) != {"id", "members"}:
        raise BookingError("cluster principal must contain only id and members")
    principal_id = value.get("id")
    members = value.get("members")
    if not isinstance(principal_id, str) or not _NAME.fullmatch(principal_id):
        raise BookingError("cluster principal ID is invalid")
    known = {node.node_id for node in nodes}
    if not isinstance(members, list) or not members:
        raise BookingError(f"cluster principal {principal_id} has no members")
    normalized = []
    for member in members:
        if not isinstance(member, dict) or set(member) != {"node_id", "uid"}:
            raise BookingError(f"cluster principal {principal_id} member is invalid")
        node_id, uid = member.get("node_id"), member.get("uid")
        if (
            node_id not in known
            or isinstance(uid, bool)
            or not isinstance(uid, int)
            or uid < 0
        ):
            raise BookingError(f"cluster principal {principal_id} member is invalid")
        normalized.append({"node_id": node_id, "uid": uid})
    return {"id": principal_id, "members": normalized}


def _cluster_booking_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("count", type=int)
    parser.add_argument("duration")
    parser.add_argument("memory", nargs="?")
    parser.add_argument(
        "--mode", choices=["s", "shared", "x", "exclusive"], default="s"
    )
    parser.add_argument(
        "--start", help="exact ISO start; omitted means earliest on each node"
    )
    placement = parser.add_mutually_exclusive_group()
    placement.add_argument("-g", "--gpu")
    placement.add_argument("-e", "--exclude-gpu", "--exclude", dest="exclude_gpu")
    parser.add_argument("-m", "--mem")
    parser.add_argument("-s", "--share", type=int)
    parser.add_argument("--op-id")
    parser.add_argument("-j", "--json", action="store_true")
    return parser


def _cluster_status_parser(action: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"bk cluster {action}")
    parser.add_argument("-j", "--json", action="store_true")
    return parser


def _cluster_usage_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bk cluster usage",
        description="Aggregate sampled usage for this SSH identity across nodes.",
    )
    parser.add_argument("-s", "--since", default="24h")
    parser.add_argument("-f", "--from", dest="start")
    parser.add_argument("-u", "--until")
    parser.add_argument(
        "-r",
        "--resolution",
        default="auto",
        choices=["auto", "1m", "5m", "10m", "1h", "1d"],
    )
    parser.add_argument("--user", help="numeric UID or 'me' on each node")
    parser.add_argument("--all", action="store_true", help="include all visible UIDs")
    parser.add_argument("-g", "--gpu", type=int)
    parser.add_argument("-n", "--limit", type=int, default=1000)
    parser.add_argument("-j", "--json", action="store_true")
    parser.add_argument("-c", "--compact", action="store_true")
    parser.add_argument("--no-chart", action="store_true", help=argparse.SUPPRESS)
    return parser


def _cluster_history_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bk cluster history",
        description="Read verified complete UTC-day history from the shared archive.",
    )
    parser.add_argument(
        "-s", "--since", default="30d", help="complete UTC-day lookback"
    )
    parser.add_argument("-f", "--from", dest="start", help="UTC date or ISO start")
    parser.add_argument("-u", "--until", help="UTC date or ISO end")
    parser.add_argument(
        "--all", action="store_true", help="show every archived principal"
    )
    parser.add_argument("-j", "--json", action="store_true")
    parser.add_argument("-c", "--compact", action="store_true")
    return parser


def _cluster_edit_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bk cluster edit",
        description="Edit your reservation on its owning node without automatic failover.",
    )
    parser.add_argument("reservation_id", metavar="NODE/ID")
    parser.add_argument("--op-id", help="stable retry-safe operation ID")
    parser.add_argument("-d", "--duration")
    parser.add_argument("--start", help="exact ISO start unless --queue is used")
    placement = parser.add_mutually_exclusive_group()
    placement.add_argument("-g", "--gpu")
    placement.add_argument("-e", "--exclude-gpu", "--exclude", dest="exclude_gpu")
    parser.add_argument("--count", type=int)
    parser.add_argument("--mode", choices=["s", "shared", "x", "exclusive"])
    parser.add_argument(
        "-m", "--mem", help="new expected memory per GPU; use - to clear"
    )
    parser.add_argument("-s", "--share", type=int)
    parser.add_argument("--queue", action="store_true")
    parser.add_argument("-j", "--json", action="store_true")
    return parser


def _cluster_cancel_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bk cluster cancel",
        description="Cancel your reservation on its owning node without automatic failover.",
    )
    parser.add_argument("reservation_id", metavar="NODE/ID")
    parser.add_argument("--op-id", help="stable retry-safe operation ID")
    parser.add_argument("-j", "--json", action="store_true")
    return parser


def _cluster_help_args(argv: list[str]) -> list[str]:
    if argv and argv[0] == "help":
        return ["--help", *argv[1:]]
    return argv


def _print_cluster_candidates(
    candidates: Sequence[ClusterCandidate],
    replies: Sequence[NodeReply],
    rejected: dict[str, str],
    required_capabilities: Sequence[str] = _BOOK_CAPABILITIES,
) -> None:
    print(
        f"{'Node':<16} {'Choice':<8} {'Write':<6} {'GPUs':<12} "
        f"{'Start':<27} {'End':<27}"
    )
    shown = set()
    for index, candidate in enumerate(candidates):
        reply = candidate.reply
        recommendation = reply.payload["recommendation"]
        shown.add(reply.node.name)
        print(
            f"{reply.node.name:<16} "
            f"{('best' if index == 0 else 'ready'):<8} "
            f"{('yes' if not _missing_capabilities(reply.payload, required_capabilities) else 'no'):<6} "
            f"{_gpu_text(recommendation.get('gpus')):<12} "
            f"{_local_time(recommendation.get('start_at')):<27} "
            f"{_local_time(recommendation.get('end_at')):<27}"
        )
    for reply in replies:
        if reply.node.name in shown:
            continue
        reason = reply.error or rejected.get(reply.node.name) or "unavailable"
        state = "offline" if reply.error else "rejected"
        print(
            f"{reply.node.name:<16} {state:<8} {'-':<6} {'-':<12} "
            f"{_clip(reason, 55):<55}"
        )


def _print_cluster_help() -> None:
    print(
        """GPUBK cluster federation

First setup (administrator):
  sudo bk admin cluster init THIS-NODE --yes
  sudo bk admin cluster add NODE SSH_TARGET STABLE_NODE_ID --yes

Everyday commands:
  bk c                       show all configured nodes and reservations
  bk c check                 verify access, identity, clocks, and safe writes
  bk c rec 2 1h              compare earliest legal placements without writing
  bk c 2 1h                  book the best single node in shared mode
  bk c x 2 1h                book the best single node exclusively
  bk c 1 2h -- COMMAND       book and schedule a command on the selected node
  bk c u -s 7d               aggregate this SSH identity's sampled history
  bk c history -s 30d        read verified offline history when configured
  bk c tui                   browse nodes and reservation details
  bk @NODE 2 1h              book one explicit node
  bk c e NODE/ID -d 2h       edit your reservation on its owning node
  bk c d NODE/ID             cancel your reservation on its owning node

Each destination broker performs the final transaction. A reservation never spans nodes.
Run any subcommand with -h for its complete options.
"""
    )


def _clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[: max(0, width - 1)] + "~"


def _duration(seconds: object) -> str:
    value = max(0, int(float(seconds)))
    hours, remainder = divmod(value, 3600)
    minutes = remainder // 60
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _local_time(value: object) -> str:
    if not isinstance(value, str):
        return "?"
    try:
        return format_local(value)
    except (TypeError, ValueError):
        return value


def _gpu_indices(value: object) -> Optional[list[int]]:
    if not isinstance(value, list):
        return None
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0
        for item in value
    ):
        return None
    return value


def _gpu_text(value: object) -> str:
    indices = _gpu_indices(value)
    return ",".join(map(str, indices)) if indices else "-"


def _reservation_mode_text(reservation: dict) -> str:
    mode = reservation.get("mode")
    if mode == "shared":
        return "share"
    if mode == "exclusive":
        return "excl"
    return _clip(str(mode or "?"), 6)


def _reservation_request_text(reservation: dict) -> str:
    if reservation.get("mode") != "shared":
        return "all"
    units = reservation.get("share_units_per_gpu")
    capacity = reservation.get("share_capacity_units_per_gpu")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in (units, capacity)
    ):
        return "?/?"
    return f"{units}/{capacity}"


def _reservation_memory_text(reservation: dict) -> str:
    value = reservation.get("expected_memory_mb_per_gpu")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return "auto"
    if value >= 1024:
        gib = value / 1024
        return f"{gib:.0f}G" if gib.is_integer() else f"{gib:.1f}G"
    return f"{value}M"


def _nonnegative_number(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return 0.0
    return max(0.0, float(value))


def _clock_skew_seconds(payload: dict) -> Optional[float]:
    try:
        generated = parse_iso(str(payload["generated_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    return abs((utc_now() - generated).total_seconds())


def _confidence_rank(value: object) -> int:
    return {
        "high": 0,
        "medium": 1,
        "low": 2,
    }.get(str(value).lower(), 3)


def _principal_for(config: ClusterConfig, node_id: str, uid: object) -> Optional[str]:
    if isinstance(uid, bool) or not isinstance(uid, int):
        return None
    for principal in config.principals:
        if any(
            member["node_id"] == node_id and member["uid"] == uid
            for member in principal["members"]
        ):
            return str(principal["id"])
    return None


def _is_booking_command(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    return argv[0].isdigit() or argv[0] in {
        "book",
        "auto",
        "shared",
        "s",
        "exclusive",
        "x",
    }


def _require_enabled_nodes(config: ClusterConfig) -> tuple[ClusterNode, ...]:
    nodes = config.enabled_nodes
    if not nodes:
        raise BookingError(
            "cluster has no enabled nodes; ask an administrator to enable one"
        )
    return nodes


def _disabled_replies(config: ClusterConfig) -> list[NodeReply]:
    return [
        NodeReply(
            node,
            None,
            "disabled by administrator",
            error_code="disabled",
        )
        for node in config.nodes
        if not node.enabled
    ]
