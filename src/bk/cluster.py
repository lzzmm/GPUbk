from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import timedelta
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
from .timeparse import parse_iso, utc_now


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
    except OSError as exc:
        raise BookingError(f"cannot read trusted cluster catalog: {path}: {exc}") from exc
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid not in {0, os.getuid()}:
            raise BookingError("cluster catalog must be owned by root or the current UID")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise BookingError("cluster catalog must not be writable by group or other users")
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
        raise BookingError(f"cluster catalog parent must not be writable by group or others: {parent}")
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
                **({"timeout_seconds": node.timeout_seconds} if node.timeout_seconds != 8 else {}),
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
    config = load_cluster_config()
    args = list(argv)
    action = args.pop(0) if args else "status"
    if action in {"status", "st", "list", "ls", "context", "ctx"}:
        parser = argparse.ArgumentParser(prog=f"bk cluster {action}")
        parser.add_argument("--json", action="store_true")
        parsed = parser.parse_args(args)
        return _cluster_status(config, json_output=parsed.json)
    if action in {"recommend", "rec"}:
        return _cluster_recommend(config, args, book=False)
    if action in {"book", "b"}:
        return _cluster_recommend(config, args, book=True)
    if action in {"usage", "u"}:
        return _cluster_usage(config, args)
    if action in {"history", "hist"}:
        return _cluster_history(config, args)
    if action in {"tui", "t"}:
        if args:
            raise BookingError("cluster tui takes no arguments")
        from .cluster_tui import run_cluster_tui

        return run_cluster_tui(config)
    if action in {"edit", "e"}:
        return _cluster_edit(config, args)
    if action in {"cancel", "del", "d"}:
        return _cluster_cancel(config, args)
    if action in {"-h", "--help", "help"}:
        _print_cluster_help()
        return 0
    raise BookingError(f"unknown cluster command: {action}")


def run_node_cli(node_name: str, argv: Sequence[str]) -> int:
    config = load_cluster_config()
    node = config.node(node_name)
    if not argv:
        raise BookingError(f"bk @{node_name} requires a GPUBK command")
    command = list(argv)
    booking = _is_booking_command(command)
    if not booking:
        raise BookingError(
            "bk @NODE currently accepts booking commands only; use bk cluster "
            "for cross-node status, usage, edit, and cancel"
        )
    requested_json = "--json" in command
    context = _invoke(node, ["agent", "context", "--compact"])
    _require_write_capabilities(context, _BOOK_CAPABILITIES)
    operation_id = _argument_value(command, "--op-id")
    if operation_id is None:
        operation_id = str(uuid.uuid4())
        command += ["--op-id", operation_id]
    if not requested_json:
        command.append("--json")
    reply = _invoke_idempotent_write(node, command, operation_id)
    if reply.error:
        raise BookingError(f"node {node.name}: {reply.error}")
    payload = reply.payload or {}
    if not requested_json:
        reservation = payload.get("reservation", {})
        short_id = reservation.get("short_id") or str(reservation.get("id", ""))[:8]
        print(
            f"{payload.get('status', 'created')} on {node.name}: {short_id} "
            f"GPU={','.join(map(str, reservation.get('gpus', [])))} "
            f"{reservation.get('start_at', '?')} -> {reservation.get('end_at', '?')}"
        )
    elif payload:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _cluster_status(config: ClusterConfig, *, json_output: bool = False) -> int:
    replies = _parallel(config.nodes, ["agent", "context", "--compact"])
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
                            "available": reply.error is None,
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
    print(f"{'Node':<16} {'State':<12} {'GPUs':>5} {'Idle':>5} {'Mine':>5} {'Actor':<18}")
    failed = 0
    reservations = []
    for reply in replies:
        if reply.error:
            failed += 1
            print(f"{reply.node.name:<16} {'unreachable':<12} {'-':>5} {'-':>5} {'-':>5} {_clip(reply.error, 18):<18}")
            continue
        payload = reply.payload or {}
        policy = payload.get("policy", {})
        gpus = payload.get("gpu_advice", {}).get("gpus", [])
        idle = sum(1 for gpu in gpus if gpu.get("live", {}).get("status") == "idle")
        mine = sum(1 for item in payload.get("reservations", []) if item.get("mine"))
        actor = payload.get("actor", {})
        principal = _principal_for(config, reply.node.node_id, actor.get("uid"))
        actor_text = principal or f"{actor.get('username', '?')}:{actor.get('uid', '?')}"
        collector = payload.get("policy", {}).get("monitoring", {}).get("collector")
        state = collector.get("state", "ok") if isinstance(collector, dict) else "unknown"
        skew = _clock_skew_seconds(payload)
        if skew is None or skew > MAX_CLOCK_SKEW_SECONDS:
            state = "clock-skew"
        print(
            f"{reply.node.name:<16} {_clip(str(state), 12):<12} "
            f"{int(policy.get('gpu_count', len(gpus))):>5} {idle:>5} {mine:>5} "
            f"{_clip(actor_text, 18):<18}"
        )
        for reservation in payload.get("reservations", []):
            if isinstance(reservation, dict):
                reservations.append((reply.node.name, reservation))
    if reservations:
        print("\nReservations")
        print(f"{'ID':<25} {'User':<18} {'Mode':<10} {'GPU':<10} {'Start':<22} {'End':<22}")
        for node_name, reservation in sorted(
            reservations,
            key=lambda item: (str(item[1].get("start_at", "")), item[0]),
        ):
            short_id = reservation.get("short_id") or str(reservation.get("id", ""))[:8]
            qualified = f"{node_name}/{short_id}"
            print(
                f"{_clip(qualified, 25):<25} "
                f"{_clip(str(reservation.get('username', '?')), 18):<18} "
                f"{_clip(str(reservation.get('mode', '?')), 10):<10} "
                f"{_clip(','.join(map(str, reservation.get('gpus', []))), 10):<10} "
                f"{_clip(str(reservation.get('start_at', '?')), 22):<22} "
                f"{_clip(str(reservation.get('end_at', '?')), 22):<22}"
            )
    return 3 if failed == len(config.nodes) else 0


def _cluster_recommend(config: ClusterConfig, argv: list[str], *, book: bool) -> int:
    parser = _cluster_booking_parser("bk cluster book" if book else "bk cluster recommend")
    args = parser.parse_args(argv)
    recommend_args = ["agent", "recommend", str(args.count), args.duration]
    mode = "exclusive" if args.mode in {"x", "exclusive"} else "shared"
    recommend_args += ["--mode", mode]
    for flag, value in (
        ("--start", args.start),
        ("--gpu", args.gpu),
        ("--exclude-gpu", args.exclude_gpu),
        ("--mem", args.mem or args.memory),
        ("--share", str(args.share) if args.share is not None else None),
    ):
        if value is not None:
            recommend_args += [flag, value]
    recommend_args.append("--compact")
    replies = _parallel(config.nodes, recommend_args)
    candidates = []
    rejected: dict[str, str] = {}
    ranking_now = utc_now()
    for reply in replies:
        payload = reply.payload
        recommendation = payload.get("recommendation") if payload else None
        skew = _clock_skew_seconds(payload) if payload else None
        if reply.error or not isinstance(recommendation, dict) or skew is None:
            continue
        if args.start is not None and skew > MAX_CLOCK_SKEW_SECONDS:
            rejected[reply.node.name] = f"clock skew is {skew:.0f}s"
            continue
        remote_start = parse_iso(str(recommendation["start_at"]))
        if args.start is None:
            generated_at = parse_iso(str(payload["generated_at"]))
            wait = max(remote_start - generated_at, timedelta(0))
            ranked_start = ranking_now + wait
        else:
            ranked_start = remote_start
        candidates.append(
            (
                ranked_start,
                reply.node.priority,
                _confidence_rank(recommendation.get("confidence")),
                reply.node.name,
                reply,
            )
        )
    candidates.sort(key=lambda item: item[:4])
    if not candidates:
        reasons = "; ".join(
            f"{reply.node.name}: "
            f"{reply.error or rejected.get(reply.node.name) or 'no legal slot'}"
            for reply in replies
        )
        raise BookingError("no cluster node can satisfy this request" + (f" ({reasons})" if reasons else ""))
    selectable = candidates
    if book:
        selectable = [
            candidate
            for candidate in candidates
            if _missing_capabilities(candidate[4].payload, _BOOK_CAPABILITIES)
            == []
        ]
        if not selectable:
            reasons = "; ".join(
                f"{candidate[4].node.name}: missing "
                f"{','.join(_missing_capabilities(candidate[4].payload, _BOOK_CAPABILITIES))}"
                for candidate in candidates
            )
            raise BookingError(
                "no write-compatible cluster node can satisfy this request"
                + (f" ({reasons})" if reasons else "")
            )
    _start, _priority, _confidence, _name, selected = selectable[0]
    if not book:
        if args.json:
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
                                "recommendation": reply.payload,
                            }
                            for reply in replies
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        _print_cluster_candidates(candidates)
        return 0

    operation_id = args.op_id or str(uuid.uuid4())
    book_args = [] if mode == "shared" else ["x"]
    book_args += [str(args.count), args.duration]
    if args.memory:
        book_args.append(args.memory)
    for flag, value in (
        ("--start", args.start),
        ("--gpu", args.gpu),
        ("--exclude-gpu", args.exclude_gpu),
        ("--mem", args.mem),
        ("--share", str(args.share) if args.share is not None else None),
    ):
        if value is not None:
            book_args += [flag, value]
    book_args += ["--op-id", operation_id, "--json"]
    result = _invoke_idempotent_write(selected.node, book_args, operation_id)
    if result.error:
        if result.error_code != "uncertain":
            raise BookingError(
                f"cluster booking on {selected.node.name} was rejected: "
                f"{result.error}"
            )
        raise BookingError(
            f"cluster booking on {selected.node.name} is unresolved: {result.error}; "
            f"retry the same request with --op-id {operation_id}"
        )
    payload = result.payload or {}
    reservation = payload.get("reservation", {})
    if args.json:
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
        f"GPU={','.join(map(str, reservation.get('gpus', [])))} "
        f"{reservation.get('start_at', '?')} -> {reservation.get('end_at', '?')}"
    )
    return 0


def _cluster_usage(config: ClusterConfig, argv: list[str]) -> int:
    json_output = "--json" in argv
    forwarded = [item for item in argv if item not in {"--json", "--compact"}]
    remote_args = ["usage", "me", *forwarded, "--json", "--compact"]
    replies = _parallel(config.nodes, remote_args)
    principals = _aggregate_cluster_usage(config, replies)
    payload = {
        "schema_version": CLUSTER_SCHEMA_VERSION,
        "kind": "cluster-usage",
        "principals": principals,
        "nodes": [
            {
                "name": reply.node.name,
                "node_id": reply.node.node_id,
                "available": reply.error is None,
                "error": reply.error,
                "usage": reply.payload,
            }
            for reply in replies
        ],
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("Cluster usage | sampled history only; future reservations excluded")
        print(f"{'Principal':<26} {'Nodes':>5} {'Active':>10} {'Reserved':>10} {'Idle':>10} {'Viol':>10}")
        for principal in principals:
            print(
                f"{_clip(principal['id'], 26):<26} {len(principal['nodes']):>5} "
                f"{_duration(principal['active_gpu_seconds']):>10} "
                f"{_duration(principal['reserved_gpu_seconds']):>10} "
                f"{_duration(principal['idle_reserved_gpu_seconds']):>10} "
                f"{_duration(principal['violation_gpu_seconds']):>10}"
            )
        for reply in replies:
            if reply.error:
                print(f"warning: {reply.node.name}: {reply.error}")
    return 3 if all(reply.error for reply in replies) else 0


def _cluster_history(config: ClusterConfig, argv: list[str]) -> int:
    if config.history_root is None:
        raise BookingError(
            "cluster history is not configured; an administrator can set it with "
            "sudo bk admin cluster history-root PATH --yes"
        )
    parser = argparse.ArgumentParser(prog="bk cluster history")
    parser.add_argument("-s", "--since", default="30d", help="complete UTC-day lookback")
    parser.add_argument("-f", "--from", dest="start", help="UTC date or ISO start")
    parser.add_argument("-u", "--until", help="UTC date or ISO end")
    parser.add_argument("--all", action="store_true", help="show every archived principal")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

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
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
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


def _aggregate_cluster_usage(config: ClusterConfig, replies: Sequence[NodeReply]) -> list[dict]:
    groups: dict[str, dict] = {}
    for reply in replies:
        if reply.error or not reply.payload:
            continue
        for user in reply.payload.get("users", []):
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
                group[key] += max(0.0, float(user.get(key, 0)))
            group["max_gpu_memory_mb"] = max(
                group["max_gpu_memory_mb"], int(user.get("max_gpu_memory_mb") or 0)
            )
            average = user.get("avg_sm_percent")
            if isinstance(average, (int, float)):
                group["_sm_weighted"] += float(average) * max(
                    0.0, float(user.get("sampled_gpu_seconds", 0))
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


def _cluster_edit(config: ClusterConfig, argv: list[str]) -> int:
    if not argv:
        raise BookingError("cluster edit requires NODE/ID")
    node, reservation_id = _qualified_reservation(config, argv[0])
    arguments = list(argv[1:])
    context = _invoke(node, ["agent", "context", "--compact"])
    _require_write_capabilities(context, _EDIT_CAPABILITIES)
    operation_id = _argument_value(arguments, "--op-id")
    if operation_id is None:
        operation_id = str(uuid.uuid4())
        arguments += ["--op-id", operation_id]
    reply = _invoke_idempotent_write(
        node,
        ["agent", "edit", reservation_id, *arguments, "--compact"],
        operation_id,
    )
    return _print_cluster_mutation(reply)


def _cluster_cancel(config: ClusterConfig, argv: list[str]) -> int:
    if len(argv) != 1:
        raise BookingError("cluster cancel requires exactly one NODE/ID")
    node, reservation_id = _qualified_reservation(config, argv[0])
    context = _invoke(node, ["agent", "context", "--compact"])
    _require_write_capabilities(context, _CANCEL_CAPABILITIES)
    operation_id = str(uuid.uuid4())
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
    )
    return _print_cluster_mutation(reply)


def _qualified_reservation(config: ClusterConfig, value: str) -> tuple[ClusterNode, str]:
    node_name, separator, reservation_id = value.partition("/")
    if not separator or not reservation_id:
        raise BookingError("use a node-qualified reservation ID such as gpu-a/1a2b3c")
    return config.node(node_name), reservation_id


def _print_cluster_mutation(reply: NodeReply) -> int:
    if reply.error:
        raise BookingError(f"node {reply.node.name}: {reply.error}")
    payload = reply.payload or {}
    reservation = payload.get("reservation", {})
    short_id = reservation.get("short_id") or str(reservation.get("id", ""))[:8]
    print(f"{payload.get('status', payload.get('kind', 'updated'))}: {reply.node.name}/{short_id}")
    return 0


def _parallel(
    nodes: Sequence[ClusterNode],
    argv: list[str],
    cancel_event: Optional[Event] = None,
) -> list[NodeReply]:
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
    return _parallel(
        config.nodes,
        ["agent", "context", "--compact"],
        cancel_event,
    )


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
) -> NodeReply:
    result = _invoke(node, argv)
    if result.error is None or result.error_code in {"remote", "identity", "cancelled"}:
        return result

    recovered = _probe_operation(node, operation_id)
    if recovered.error is None and recovered.payload is not None:
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
    recovered = _probe_operation(node, operation_id)
    if recovered.error is None and recovered.payload is not None:
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


def _probe_operation(node: ClusterNode, operation_id: str) -> NodeReply:
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
    if not isinstance(document, dict) or document.get("schema_version") != CLUSTER_SCHEMA_VERSION:
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
        raise BookingError(f"cluster catalog nodes must contain 1-{MAX_CLUSTER_NODES} entries")
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
        "name", "node_id", "transport", "target", "executable", "priority", "timeout_seconds", "enabled"
    }
    if unknown:
        raise BookingError(f"unknown cluster node field: {sorted(unknown)[0]}")
    if value.get("enabled", True) is False:
        raise BookingError("disabled nodes must be removed from the active catalog")
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
    if transport == "ssh" and (not isinstance(target, str) or not _SSH_TARGET.fullmatch(target) or target.startswith("-")):
        raise BookingError(f"cluster node {name} has an invalid SSH target")
    if transport == "local" and target is not None:
        raise BookingError(f"local cluster node {name} must not define target")
    executable = value.get("executable", "/usr/local/bin/bk")
    if not isinstance(executable, str) or not Path(executable).is_absolute() or any(ord(ch) < 32 for ch in executable):
        raise BookingError(f"cluster node {name} executable must be an absolute safe path")
    priority = value.get("priority", 0)
    timeout = value.get("timeout_seconds", 8)
    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 1_000_000:
        raise BookingError(f"cluster node {name} priority is invalid")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
        raise BookingError(f"cluster node {name} timeout_seconds must be 1-300")
    return ClusterNode(name, node_id, transport, target, executable, priority, float(timeout))


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
        if node_id not in known or isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            raise BookingError(f"cluster principal {principal_id} member is invalid")
        normalized.append({"node_id": node_id, "uid": uid})
    return {"id": principal_id, "members": normalized}


def _cluster_booking_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("count", type=int)
    parser.add_argument("duration")
    parser.add_argument("memory", nargs="?")
    parser.add_argument("--mode", choices=["s", "shared", "x", "exclusive"], default="s")
    parser.add_argument("--start", help="exact ISO start; omitted means earliest on each node")
    placement = parser.add_mutually_exclusive_group()
    placement.add_argument("-g", "--gpu")
    placement.add_argument("-e", "--exclude-gpu", "--exclude", dest="exclude_gpu")
    parser.add_argument("-m", "--mem")
    parser.add_argument("-s", "--share", type=int)
    parser.add_argument("--op-id")
    parser.add_argument("--json", action="store_true")
    return parser


def _print_cluster_candidates(candidates: list[tuple]) -> None:
    print(f"{'Node':<16} {'GPUs':<12} {'Start':<27} {'End':<27}")
    for _start, _priority, _confidence, _name, reply in candidates:
        recommendation = reply.payload["recommendation"]
        print(
            f"{reply.node.name:<16} {','.join(map(str, recommendation['gpus'])):<12} "
            f"{recommendation['start_at']:<27} {recommendation['end_at']:<27}"
        )


def _print_cluster_help() -> None:
    print(
        """GPUBK cluster federation

  bk cluster                  show all configured nodes
  bk cluster recommend 2 1h  compare earliest legal placements
  bk cluster book 2 1h       book the best single node
  bk cluster usage --since 7d  aggregate this SSH identity's history
  bk cluster history --since 30d  read verified offline history when configured
  bk cluster tui              browse nodes in a full-screen view
  bk @NODE 2 1h --json       run a normal command on one explicit node
  bk cluster edit NODE/ID --duration 2h
  bk cluster cancel NODE/ID

Each destination broker performs the final transaction. A reservation never spans nodes.
"""
    )


def _clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[: max(0, width - 1)] + "~"


def _duration(seconds: object) -> str:
    value = max(0, int(float(seconds)))
    hours, remainder = divmod(value, 3600)
    minutes = remainder // 60
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


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
