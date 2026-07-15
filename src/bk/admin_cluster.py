from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .cluster import (
    ClusterConfig,
    ClusterNode,
    cluster_config_path,
    load_cluster_config,
    write_cluster_config,
)
from .config import load_config
from .models import BookingError
from .node_identity import stable_node_identity


def add_admin_cluster_parser(commands) -> None:
    cluster_parser = commands.add_parser(
        "cluster",
        help="inspect or update the optional federated node catalog",
    )
    cluster_commands = cluster_parser.add_subparsers(
        dest="cluster_action",
        required=True,
    )
    cluster_status = cluster_commands.add_parser(
        "status",
        help="validate and show nodes",
    )
    cluster_status.add_argument("--cluster-file", type=Path)
    cluster_status.add_argument("--json", action="store_true")

    cluster_init = cluster_commands.add_parser(
        "init",
        help="create a catalog with this local node",
    )
    cluster_init.add_argument("name")
    cluster_init.add_argument("--cluster-file", type=Path)
    cluster_init.add_argument("--yes", action="store_true")

    cluster_add = cluster_commands.add_parser(
        "add",
        help="add one SSH-reachable GPU node",
    )
    cluster_add.add_argument("name")
    cluster_add.add_argument("target")
    cluster_add.add_argument("node_id")
    cluster_add.add_argument("--executable", default="/usr/local/bin/bk")
    cluster_add.add_argument("--priority", type=int, default=0)
    cluster_add.add_argument("--timeout", type=float, default=8)
    cluster_add.add_argument("--cluster-file", type=Path)
    cluster_add.add_argument("--yes", action="store_true")

    cluster_remove = cluster_commands.add_parser(
        "remove",
        help="remove one node by name",
    )
    cluster_remove.add_argument("name")
    cluster_remove.add_argument("--cluster-file", type=Path)
    cluster_remove.add_argument("--yes", action="store_true")

    for action, help_text in (
        ("disable", "temporarily remove one node from live cluster routing"),
        ("enable", "return one configured node to live cluster routing"),
    ):
        lifecycle = cluster_commands.add_parser(action, help=help_text)
        lifecycle.add_argument("name")
        lifecycle.add_argument("--cluster-file", type=Path)
        lifecycle.add_argument("--yes", action="store_true")

    cluster_map = cluster_commands.add_parser(
        "map",
        help="map one node-local numeric UID to a global principal",
    )
    cluster_map.add_argument("principal")
    cluster_map.add_argument("node")
    cluster_map.add_argument("uid", type=int)
    cluster_map.add_argument("--cluster-file", type=Path)
    cluster_map.add_argument("--yes", action="store_true")

    cluster_unmap = cluster_commands.add_parser(
        "unmap",
        help="remove one node-local UID from its global principal",
    )
    cluster_unmap.add_argument("node")
    cluster_unmap.add_argument("uid", type=int)
    cluster_unmap.add_argument("--cluster-file", type=Path)
    cluster_unmap.add_argument("--yes", action="store_true")

    cluster_history_root = cluster_commands.add_parser(
        "history-root",
        help="set an optional shared, read-only cluster history root",
    )
    cluster_history_root.add_argument("path", help="absolute directory or 'off'")
    cluster_history_root.add_argument("--cluster-file", type=Path)
    cluster_history_root.add_argument("--yes", action="store_true")

    cluster_export = cluster_commands.add_parser(
        "export-history",
        help="atomically export this node's public usage history",
    )
    cluster_export.add_argument("--since", default="30d")
    cluster_export.add_argument("--from", dest="start")
    cluster_export.add_argument("--until")
    cluster_export.add_argument(
        "--resolution",
        choices=["5m", "10m", "1h", "1d"],
        default="10m",
    )
    cluster_export.add_argument("--cluster-file", type=Path)
    cluster_export.add_argument("--yes", action="store_true")
    cluster_export.add_argument("--json", action="store_true")

    cluster_verify = cluster_commands.add_parser(
        "verify-history",
        help="verify immutable history manifests and payload checksums",
    )
    cluster_verify.add_argument("--cluster-file", type=Path)
    cluster_verify.add_argument("--json", action="store_true")


def run_admin_cluster(args: argparse.Namespace) -> int:
    path = args.cluster_file or cluster_config_path()
    if not path.is_absolute():
        raise BookingError("cluster catalog path must be absolute")
    if args.cluster_action in {"export-history", "verify-history"}:
        return _run_history_action(args, path)
    if os.geteuid() != 0:
        raise BookingError("cluster catalog administration must run as root; use sudo")

    if args.cluster_action == "init":
        desired = _initial_cluster_config(args, path)
    else:
        current = load_cluster_config(path)
        if args.cluster_action == "status":
            return print_admin_cluster(current, json_output=args.json)
        desired = _updated_cluster_config(current, args)
    return _confirm_and_write(desired, args.yes)


def _run_history_action(args: argparse.Namespace, path: Path) -> int:
    current = load_cluster_config(path)
    if current.history_root is None:
        raise BookingError(
            "cluster history root is not configured; run sudo bk admin cluster "
            "history-root PATH --yes"
        )
    from .cluster_history import (
        export_cluster_history,
        resolve_history_window,
        verify_cluster_history,
    )

    node_name: Optional[str] = None
    through: Optional[datetime] = None
    if args.cluster_action == "verify-history":
        result = verify_cluster_history(
            current.history_root,
            expected_node_ids={node.node_id for node in current.nodes},
        )
    else:
        exported = _export_history(
            args,
            current,
            resolve_history_window,
            export_cluster_history,
        )
        if exported is None:
            return 1
        result, node_name, through = exported
    _print_history_result(
        result,
        json_output=args.json,
        node_name=node_name,
        through=through,
    )
    return 0


def _export_history(
    args: argparse.Namespace,
    current: ClusterConfig,
    resolve_history_window: Callable[..., tuple[datetime, datetime]],
    export_cluster_history: Callable[..., dict],
) -> Optional[tuple[dict, str, datetime]]:
    config = load_config()
    allowed_uids = {0}
    if config.monitor_uid is not None:
        allowed_uids.add(config.monitor_uid)
    if os.path.lexists(config.data_dir):
        allowed_uids.add(config.data_dir.stat().st_uid)
    if os.geteuid() not in allowed_uids:
        choices = ",".join(str(uid) for uid in sorted(allowed_uids))
        raise BookingError(
            "history export must run as root or the configured telemetry owner "
            f"UID ({choices})"
        )
    local = next(
        (node for node in current.nodes if node.transport == "local"),
        None,
    )
    if local is None:
        raise BookingError("history export requires one local node in this catalog")
    start, end = resolve_history_window(
        current.history_root,
        local.node_id,
        since=args.since,
        start=args.start,
        until=args.until,
        incremental=args.start is None,
    )
    if start == end:
        return (
            {
                "schema_version": "gpubk.cluster-history.v1",
                "status": "up-to-date",
                "root": str(current.history_root),
                "node_id": local.node_id,
                "generations": 0,
                "files": 0,
                "bytes": 0,
            },
            local.name,
            end,
        )
    if not args.yes:
        print(
            f"History export: {local.name} {start:%Y-%m-%d} -> "
            f"{end:%Y-%m-%d} resolution={args.resolution}"
        )
        print(f"Destination: {current.history_root}/{local.node_id}")
        print("No files written. Pass --yes after reviewing this range.")
        return None
    return (
        export_cluster_history(
            current.history_root,
            config,
            start=start,
            end=end,
            resolution=args.resolution,
        ),
        local.name,
        end,
    )


def _print_history_result(
    result: dict,
    *,
    json_output: bool,
    node_name: Optional[str] = None,
    through: Optional[datetime] = None,
) -> None:
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if result["status"] == "up-to-date" and result.get("generation") is None:
        print(
            f"cluster history up-to-date: node={node_name or result['node_id']} "
            + (f"through={through:%Y-%m-%d} " if through is not None else "")
            + f"root={result['root']}"
        )
        return
    generation_detail = (
        f"generation={result['generation']}"
        if result.get("generation")
        else f"generations={result.get('generations', 0)}"
    )
    print(
        f"cluster history {result['status']}: root={result['root']} "
        f"nodes={len(result.get('nodes', [result.get('node_id')]))} "
        f"{generation_detail} files={result['files']} bytes={result['bytes']}"
    )


def _initial_cluster_config(args: argparse.Namespace, path: Path) -> ClusterConfig:
    if os.path.lexists(path):
        raise BookingError(f"cluster catalog already exists: {path}")
    identity = stable_node_identity()
    return ClusterConfig(
        path,
        (
            ClusterNode(
                args.name,
                identity["id"],
                "local",
                None,
                "/usr/local/bin/bk",
                0,
                8,
            ),
        ),
    )


def _updated_cluster_config(
    current: ClusterConfig,
    args: argparse.Namespace,
) -> ClusterConfig:
    nodes = list(current.nodes)
    principals = _copy_principals(current)
    history_root = current.history_root
    if args.cluster_action == "add":
        if any(node.name == args.name for node in nodes):
            raise BookingError(f"cluster node already exists: {args.name}")
        nodes.append(
            ClusterNode(
                args.name,
                args.node_id,
                "ssh",
                args.target,
                args.executable,
                args.priority,
                args.timeout,
            )
        )
    elif args.cluster_action == "remove":
        nodes, principals = _remove_cluster_node(current, principals, args.name)
    elif args.cluster_action in {"enable", "disable"}:
        nodes = _set_cluster_node_enabled(
            current,
            args.name,
            enabled=args.cluster_action == "enable",
        )
    elif args.cluster_action == "map":
        return _map_cluster_principal(current, principals, args)
    elif args.cluster_action == "unmap":
        return _unmap_cluster_principal(current, principals, args)
    elif args.cluster_action == "history-root":
        history_root = _history_root(args.path)
    else:
        raise BookingError(f"unsupported cluster administration action: {args.cluster_action}")
    return ClusterConfig(current.path, tuple(nodes), tuple(principals), history_root)


def _copy_principals(current: ClusterConfig) -> list[dict]:
    return [
        {"id": item["id"], "members": [dict(member) for member in item["members"]]}
        for item in current.principals
    ]


def _remove_cluster_node(
    current: ClusterConfig,
    principals: list[dict],
    name: str,
) -> tuple[list[ClusterNode], list[dict]]:
    matches = [node for node in current.nodes if node.name == name]
    if not matches:
        raise BookingError(f"unknown cluster node: {name}")
    removed_id = matches[0].node_id
    nodes = [node for node in current.nodes if node.name != name]
    cleaned = []
    for principal in principals:
        members = [
            member
            for member in principal["members"]
            if member["node_id"] != removed_id
        ]
        if members:
            cleaned.append({"id": principal["id"], "members": members})
    return nodes, cleaned


def _set_cluster_node_enabled(
    current: ClusterConfig,
    name: str,
    *,
    enabled: bool,
) -> list[ClusterNode]:
    node = current.node(name)
    if node.enabled == enabled:
        return list(current.nodes)
    return [
        replace(item, enabled=enabled) if item.name == name else item
        for item in current.nodes
    ]


def _map_cluster_principal(
    current: ClusterConfig,
    principals: list[dict],
    args: argparse.Namespace,
) -> ClusterConfig:
    node = current.node(args.node)
    pair = node.node_id, args.uid
    for principal in principals:
        if any(
            (member["node_id"], member["uid"]) == pair
            for member in principal["members"]
        ):
            if principal["id"] == args.principal:
                return current
            raise BookingError(
                "node-local UID is already mapped to principal "
                f"{principal['id']}"
            )
    target = next(
        (item for item in principals if item["id"] == args.principal),
        None,
    )
    if target is None:
        target = {"id": args.principal, "members": []}
        principals.append(target)
    target["members"].append({"node_id": node.node_id, "uid": args.uid})
    return ClusterConfig(
        current.path,
        current.nodes,
        tuple(principals),
        current.history_root,
    )


def _unmap_cluster_principal(
    current: ClusterConfig,
    principals: list[dict],
    args: argparse.Namespace,
) -> ClusterConfig:
    node = current.node(args.node)
    pair = node.node_id, args.uid
    removed = False
    updated = []
    for principal in principals:
        members = []
        for member in principal["members"]:
            if (member["node_id"], member["uid"]) == pair:
                removed = True
            else:
                members.append(member)
        if members:
            updated.append({"id": principal["id"], "members": members})
    if not removed:
        raise BookingError(
            f"node-local UID is not mapped: {args.node}:{args.uid}"
        )
    return ClusterConfig(
        current.path,
        current.nodes,
        tuple(updated),
        current.history_root,
    )


def _history_root(value: str) -> Path | None:
    if value.lower() == "off":
        return None
    path = Path(value)
    if not path.is_absolute():
        raise BookingError("cluster history root must be an absolute path")
    return path


def _confirm_and_write(desired: ClusterConfig, confirmed: bool) -> int:
    print_admin_cluster(desired, json_output=False)
    if not confirmed:
        if not sys.stdin.isatty():
            print("bk: pass --yes to update the cluster catalog", file=sys.stderr)
            return 1
        if input("Write this cluster catalog? [y/N]: ").strip().lower() not in {
            "y",
            "yes",
        }:
            print("No changes made.")
            return 1
    write_cluster_config(desired)
    print(f"cluster catalog updated: {desired.path}")
    return 0


def print_admin_cluster(config: ClusterConfig, *, json_output: bool) -> int:
    document = {
        "schema_version": "gpubk.cluster.v1",
        "path": str(config.path),
        "nodes": [
            {
                "name": node.name,
                "node_id": node.node_id,
                "transport": node.transport,
                "target": node.target,
                "priority": node.priority,
                "enabled": node.enabled,
            }
            for node in config.nodes
        ],
        "principals": list(config.principals),
        "history_root": str(config.history_root) if config.history_root else None,
    }
    if json_output:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True))
        return 0
    print(f"Cluster catalog: {config.path}")
    for node in config.nodes:
        endpoint = "this host" if node.transport == "local" else node.target
        state = "enabled" if node.enabled else "disabled"
        print(
            f"  {node.name:<16} {node.node_id} {node.transport:<5} "
            f"{state:<9} priority={node.priority} endpoint={endpoint}"
        )
    if config.principals:
        names = {node.node_id: node.name for node in config.nodes}
        print(f"Identity mappings: {len(config.principals)} principal(s)")
        for principal in config.principals:
            members = ", ".join(
                f"{names.get(member['node_id'], member['node_id'])}:{member['uid']}"
                for member in principal["members"]
            )
            print(f"  {principal['id']}: {members}")
    if config.history_root is not None:
        print(f"History root: {config.history_root}")
    return 0
