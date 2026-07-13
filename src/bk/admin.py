from __future__ import annotations

import argparse
import base64
import grp
import hashlib
import json
import os
import pwd
import shutil
import socket
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from .config import (
    BROKER_ALL_SOCKET_MODE,
    BROKER_DIR_MODE,
    BROKER_FILE_MODE,
    BROKER_GROUP_SOCKET_MODE,
    CONFIG_VERSION,
    MAX_GPU_COUNT,
    MAX_SHARED_UNITS,
    SYSTEM_CONFIG_FILE,
    Config,
)
from .fileio import fsync_directory, open_existing_regular
from .gpu import detect_gpu_count, snapshot
from .granularity import DEFAULT_SLOT_MINUTES, validate_slot_minutes
from .models import BookingError


ADMIN_SCHEMA_VERSION = "gpubk.admin.v1"
INSTALL_SCHEMA_VERSION = "gpubk.install.v1"
DEFAULT_SYSTEM_DATA_DIR = Path("/var/lib/gpubk")
DEFAULT_BROKER_SOCKET = Path("/run/gpubk/broker.sock")
CONFIG_DIRECTORY_MODE = 0o755
CONFIG_FILE_MODE = 0o644
INSTALL_MANIFEST_NAME = "install.json"
INSTALL_MANIFEST_MODE = 0o600
BROKER_SOCKET_DIRECTORY_MODE = 0o755
MANAGED_DATA_NAMES = frozenset(
    {
        "backups",
        "ledger.json",
        "ledger.lock",
        "ops.log",
        "transaction.json",
        "usage",
        "usage-events.jsonl",
        "usage-load.json",
        "usage-rollups.jsonl",
        "usage-state.json",
        "usage.lock",
    }
)


@dataclass(frozen=True)
class AdminIdentity:
    uid: int
    username: str
    primary_gid: int


@dataclass(frozen=True)
class AdminInitPlan:
    config_file: Path
    data_dir: Path
    access: str
    gpu_count: int
    slot_minutes: int
    max_shared_users: int
    require_shared_memory: bool
    service: AdminIdentity
    group_name: Optional[str]
    broker_gid: Optional[int]
    broker_socket: Path
    broker_socket_mode: int
    file_mode: int
    dir_mode: int

    def config_document(self) -> dict:
        document = {
            "config_version": CONFIG_VERSION,
            "data_dir": str(self.data_dir),
            "gpu_count": self.gpu_count,
            "slot_minutes": self.slot_minutes,
            "max_shared_users": self.max_shared_users,
            "queue_search_hours": 168,
            "require_shared_memory": self.require_shared_memory,
            "shared_memory_reserve_mb": 512,
            "monitor_uid": self.service.uid,
            "broker_socket": str(self.broker_socket),
            "broker_uid": self.service.uid,
            "broker_socket_mode": f"{self.broker_socket_mode:04o}",
            "file_mode": f"{self.file_mode:04o}",
            "dir_mode": f"{self.dir_mode:04o}",
        }
        if self.broker_gid is not None:
            document["broker_gid"] = self.broker_gid
        return document

    def public_document(self, *, status: str) -> dict:
        return {
            "schema_version": ADMIN_SCHEMA_VERSION,
            "kind": "admin-init",
            "status": status,
            "config_file": str(self.config_file),
            "data_dir": str(self.data_dir),
            "access": {
                "mode": self.access,
                "group": self.group_name,
                "socket": str(self.broker_socket),
                "socket_mode": f"{self.broker_socket_mode:04o}",
                "file_mode": f"{self.file_mode:04o}",
                "dir_mode": f"{self.dir_mode:04o}",
                "write_boundary": "service-account-only",
            },
            "gpu_count": self.gpu_count,
            "slot_minutes": self.slot_minutes,
            "max_shared_users": self.max_shared_users,
            "require_shared_memory": self.require_shared_memory,
            "service": {
                "uid": self.service.uid,
                "username": self.service.username,
            },
            "config": self.config_document(),
        }


@dataclass(frozen=True)
class AdminInspection:
    existing_config: Optional[dict]
    data_exists: bool
    data_nonempty: bool
    socket_directory_exists: bool
    socket_directory_nonempty: bool
    config_action: str
    data_action: str
    socket_directory_action: str

    def public_document(self) -> dict:
        return {
            "config_action": self.config_action,
            "data_action": self.data_action,
            "data_exists": self.data_exists,
            "data_nonempty": self.data_nonempty,
            "socket_directory_action": self.socket_directory_action,
            "socket_directory_exists": self.socket_directory_exists,
            "socket_directory_nonempty": self.socket_directory_nonempty,
        }


def run_admin_cli(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="bk admin",
        description="Initialize or safely remove a shared GPUbk server.",
    )
    commands = parser.add_subparsers(dest="action", required=True)
    init_parser = commands.add_parser(
        "init",
        help="preview or initialize shared server configuration",
    )
    init_parser.add_argument("--config-file", type=Path, default=SYSTEM_CONFIG_FILE)
    init_parser.add_argument("--data-dir", type=Path)
    init_parser.add_argument("--access", choices=("all", "group"))
    init_parser.add_argument(
        "--group", help="existing Unix group used by --access group"
    )
    init_parser.add_argument(
        "--service-user",
        help="existing non-root account that exclusively writes GPUbk state",
    )
    init_parser.add_argument(
        "--broker-socket", type=Path, default=DEFAULT_BROKER_SOCKET
    )
    init_parser.add_argument("--gpu-count", type=int)
    init_parser.add_argument("--slot-minutes", type=int)
    init_parser.add_argument("--max-shared-users", type=int)
    memory = init_parser.add_mutually_exclusive_group()
    memory.add_argument(
        "--require-shared-memory",
        dest="require_shared_memory",
        action="store_true",
    )
    memory.add_argument(
        "--allow-implicit-shared-memory",
        dest="require_shared_memory",
        action="store_false",
    )
    init_parser.set_defaults(require_shared_memory=None)
    init_parser.add_argument("--yes", action="store_true", help="apply without confirmation")
    init_parser.add_argument("--dry-run", action="store_true", help="show the plan without writing")
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="replace a different config only while the selected data directory is empty",
    )
    init_parser.add_argument("--json", action="store_true")
    uninstall_parser = commands.add_parser(
        "uninstall",
        help="safely remove administrator-managed server state",
    )
    uninstall_parser.add_argument(
        "--config-file", type=Path, default=SYSTEM_CONFIG_FILE
    )
    uninstall_parser.add_argument(
        "--purge-data",
        action="store_true",
        help="remove validated GPUbk ledger and usage data",
    )
    uninstall_parser.add_argument(
        "--yes", action="store_true", help="apply without confirmation"
    )
    uninstall_parser.add_argument(
        "--dry-run", action="store_true", help="show the plan only"
    )
    uninstall_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv))

    if args.action == "uninstall":
        return _run_admin_uninstall(args)

    interactive = sys.stdin.isatty() and not args.yes and not args.json
    detected_gpu_count = _detected_gpu_count(args.gpu_count)
    default_service = _default_service_identity(args.service_user)
    plan = _build_plan(
        args, detected_gpu_count, default_service, interactive=interactive
    )
    _validate_plan(plan)
    inspection = inspect_admin_init(plan, force=args.force, expected_owner=0)

    if not args.json:
        _print_plan(plan, inspection)

    if args.dry_run:
        if args.json:
            print(
                json.dumps(
                    {
                        **plan.public_document(status="dry-run"),
                        "inspection": inspection.public_document(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return 0
    if not args.yes:
        if not interactive:
            if args.json:
                print(
                    json.dumps(
                        {
                            **plan.public_document(status="planned"),
                            "inspection": inspection.public_document(),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            print("bk: pass --yes to apply this administrator plan", file=sys.stderr)
            return 1
        answer = input("Apply this configuration? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("No changes made.")
            return 1

    result = apply_admin_init(plan, force=args.force)
    if args.json:
        payload = plan.public_document(status="initialized")
        payload["inspection"] = inspection.public_document()
        payload["result"] = result
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"initialized config: {plan.config_file}")
        print(f"initialized data:   {plan.data_dir}")
        print("ready: local users with the bk command can make reservations")
        print(
            f"next: start 'bk broker' as {plan.service.username}, then run "
            "'bk doctor --probe --strict' as a normal user"
        )
    return 0


def _run_admin_uninstall(args: argparse.Namespace) -> int:
    config_file = _absolute_path(args.config_file)
    inspection = inspect_admin_uninstall(
        config_file,
        purge_data=args.purge_data,
        expected_owner=0,
    )
    if not args.json:
        _print_uninstall_plan(inspection)
    if args.dry_run:
        if args.json:
            print(json.dumps(inspection, ensure_ascii=False, sort_keys=True))
        return 0
    if not args.yes:
        if args.json:
            print(json.dumps(inspection, ensure_ascii=False, sort_keys=True))
        if not sys.stdin.isatty() or args.json:
            print("bk: pass --yes to apply this uninstall plan", file=sys.stderr)
            return 1
        answer = input("Apply this uninstall plan? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("No changes made.")
            return 1
    result = apply_admin_uninstall(config_file, purge_data=args.purge_data)
    if args.json:
        print(
            json.dumps(
                {"status": "uninstalled", "inspection": inspection, "result": result},
                sort_keys=True,
            )
        )
    else:
        print(f"removed server configuration: {config_file}")
        print("preserved: service account and Unix groups were never modified")
        print(
            "next: uninstall the Python package with 'python3 -m pip uninstall gpubk'"
        )
    return 0


def inspect_admin_uninstall(
    config_file: Path,
    *,
    purge_data: bool,
    expected_owner: int = 0,
) -> dict:
    config_file = _absolute_path(config_file)
    manifest_path = _manifest_path(config_file)
    if not os.path.lexists(manifest_path):
        raise BookingError(
            f"install manifest is missing; refusing untracked removal: {manifest_path}"
        )
    manifest = _read_manifest(manifest_path, expected_owner=expected_owner)
    if manifest.get("admin_uid") != expected_owner:
        raise BookingError("install manifest administrator UID does not match")
    if manifest.get("config_file") != str(config_file):
        raise BookingError("install manifest belongs to a different configuration path")

    config_before = _validated_config_state(manifest.get("config_before"))
    current_config = _current_managed_config(config_file, expected_owner=expected_owner)
    if current_config is not None:
        allowed_config_digests = {manifest["config_sha256"]}
        previous_digest = manifest.get("previous_config_sha256")
        if isinstance(previous_digest, str):
            allowed_config_digests.add(previous_digest)
        if config_before["exists"]:
            allowed_config_digests.add(config_before["sha256"])
        if _sha256(current_config) not in allowed_config_digests:
            raise BookingError(
                "managed configuration changed after initialization; "
                "review it before uninstalling"
            )

    data_dir = _manifest_absolute_path(manifest, "data_dir")
    broker_socket = _manifest_absolute_path(manifest, "broker_socket")
    service_uid = _manifest_nonnegative_int(manifest, "service_uid")
    service_gid = _manifest_nonnegative_int(manifest, "service_gid")
    data_nonempty = False
    if os.path.lexists(data_dir):
        metadata = data_dir.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise BookingError(f"managed data path is not a real directory: {data_dir}")
        if (
            metadata.st_uid != service_uid
            or metadata.st_gid != service_gid
            or stat.S_IMODE(metadata.st_mode) != BROKER_DIR_MODE
        ):
            raise BookingError(
                f"managed data directory ownership or mode drifted: {data_dir}"
            )
        data_nonempty = _directory_nonempty(data_dir)
        if purge_data:
            _validate_managed_data_tree(data_dir)

    socket_state = _broker_socket_state(broker_socket, service_uid=service_uid)
    backup_path = _validated_backup_path(manifest, config_file)
    config_directory_before = _validated_directory_state(
        manifest.get("config_directory_before"),
        "config_directory_before",
    )
    socket_directory_before = _validated_directory_state(
        manifest.get("socket_directory_before"),
        "socket_directory_before",
    )
    _validated_directory_state(
        manifest.get("data_directory_before"),
        "data_directory_before",
    )

    if not config_directory_before["exists"]:
        allowed = {config_file.name, manifest_path.name}
        if backup_path is not None:
            allowed.add(backup_path.name)
        _validate_directory_entries(config_file.parent, allowed, "configuration")
    if not socket_directory_before["exists"]:
        _validate_directory_entries(
            broker_socket.parent, {broker_socket.name}, "broker socket"
        )

    blockers = []
    if socket_state == "active":
        blockers.append("broker is running; stop it before uninstalling")
    if data_nonempty and not purge_data:
        blockers.append("data exists; pass --purge-data to remove it")
    actions = []
    if socket_state == "stale":
        actions.append(f"remove stale socket {broker_socket}")
    if os.path.lexists(data_dir):
        if data_nonempty:
            actions.append(
                f"purge validated data {data_dir}"
                if purge_data
                else f"preserve data {data_dir}"
            )
        elif manifest["data_directory_before"].get("exists"):
            actions.append(f"restore directory metadata {data_dir}")
        else:
            actions.append(f"remove empty directory {data_dir}")
    config_before = manifest["config_before"]
    actions.append(
        f"restore prior configuration {config_file}"
        if config_before.get("exists")
        else f"remove configuration {config_file}"
    )
    actions.append(f"remove install manifest {manifest_path}")
    if not socket_directory_before["exists"]:
        actions.append(f"remove socket directory {broker_socket.parent}")
    if not config_directory_before["exists"]:
        actions.append(f"remove configuration directory {config_file.parent}")
    return {
        "schema_version": ADMIN_SCHEMA_VERSION,
        "kind": "admin-uninstall",
        "status": "blocked" if blockers else "ready",
        "config_file": str(config_file),
        "data_dir": str(data_dir),
        "broker_socket": str(broker_socket),
        "purge_data": purge_data,
        "data_nonempty": data_nonempty,
        "socket_state": socket_state,
        "actions": actions,
        "blockers": blockers,
    }


def apply_admin_uninstall(
    config_file: Path,
    *,
    purge_data: bool,
    require_root: bool = True,
) -> dict:
    if require_root and os.geteuid() != 0:
        raise BookingError(
            "administrator uninstall must run as root; use sudo bk admin uninstall"
        )
    expected_owner = 0 if require_root else os.geteuid()
    inspection = inspect_admin_uninstall(
        config_file,
        purge_data=purge_data,
        expected_owner=expected_owner,
    )
    if inspection["blockers"]:
        raise BookingError("; ".join(inspection["blockers"]))

    config_file = _absolute_path(config_file)
    manifest_path = _manifest_path(config_file)
    manifest = _read_manifest(manifest_path, expected_owner=expected_owner)
    data_dir = _manifest_absolute_path(manifest, "data_dir")
    broker_socket = _manifest_absolute_path(manifest, "broker_socket")
    backup_path = _validated_backup_path(manifest, config_file)

    if os.path.lexists(broker_socket):
        broker_socket.unlink()
        fsync_directory(broker_socket.parent)

    data_before = _validated_directory_state(
        manifest["data_directory_before"],
        "data_directory_before",
    )
    if os.path.lexists(data_dir):
        if _directory_nonempty(data_dir):
            _purge_managed_data(data_dir)
        if data_before["exists"]:
            _restore_directory_state(data_dir, data_before)
        else:
            data_dir.rmdir()
            fsync_directory(data_dir.parent)

    config_before = _validated_config_state(manifest["config_before"])
    if config_before["exists"]:
        _restore_config_file(config_file, config_before)
    elif os.path.lexists(config_file):
        config_file.unlink()
        fsync_directory(config_file.parent)

    if backup_path is not None and os.path.lexists(backup_path):
        backup_path.unlink()
        fsync_directory(backup_path.parent)
    manifest_path.unlink()
    fsync_directory(manifest_path.parent)

    socket_before = _validated_directory_state(
        manifest["socket_directory_before"],
        "socket_directory_before",
    )
    if os.path.lexists(broker_socket.parent):
        if socket_before["exists"]:
            _restore_directory_state(broker_socket.parent, socket_before)
        else:
            broker_socket.parent.rmdir()
            fsync_directory(broker_socket.parent.parent)

    config_directory_before = _validated_directory_state(
        manifest["config_directory_before"],
        "config_directory_before",
    )
    if config_directory_before["exists"]:
        _restore_directory_state(config_file.parent, config_directory_before)
    elif os.path.lexists(config_file.parent):
        config_file.parent.rmdir()
        fsync_directory(config_file.parent.parent)
    return {
        "config_removed": not config_before["exists"],
        "config_restored": bool(config_before["exists"]),
        "data_purged": bool(purge_data),
        "manifest_removed": True,
        "accounts_changed": False,
    }


def apply_admin_init(
    plan: AdminInitPlan,
    *,
    force: bool = False,
    require_root: bool = True,
) -> dict:
    if require_root and os.geteuid() != 0:
        raise BookingError("administrator initialization must run as root; use sudo bk admin init")

    expected_owner = 0 if require_root else os.geteuid()
    inspection = inspect_admin_init(plan, force=force, expected_owner=expected_owner)
    manifest_path, manifest = _ensure_install_manifest(
        plan,
        inspection,
        expected_owner=expected_owner,
    )
    desired_config = plan.config_document()
    data_created = _prepare_owned_directory(
        plan.data_dir,
        owner_uid=plan.service.uid,
        owner_gid=plan.service.primary_gid,
        mode=plan.dir_mode,
        nonempty=inspection.data_nonempty,
        label="data",
    )
    socket_directory_created = _prepare_owned_directory(
        plan.broker_socket.parent,
        owner_uid=plan.service.uid,
        owner_gid=plan.service.primary_gid,
        mode=BROKER_SOCKET_DIRECTORY_MODE,
        nonempty=inspection.socket_directory_nonempty,
        label="broker socket",
    )
    config_changed = inspection.existing_config != desired_config
    backup = None
    if config_changed:
        create_backup = False
        if inspection.existing_config is not None:
            backup = plan.config_file.with_name(f"{plan.config_file.name}.bak")
            if os.path.lexists(backup):
                if _validated_backup_path(manifest, plan.config_file) != backup:
                    raise BookingError(
                        f"refusing to replace an untracked configuration backup: {backup}"
                    )
            elif manifest.get("backup_path") is None:
                create_backup = True
                manifest = {
                    **manifest,
                    "backup_path": str(backup),
                    "backup_sha256": _sha256(
                        _config_payload(inspection.existing_config)
                    ),
                }
                _write_manifest(manifest_path, manifest, replace=True)
        written_backup = _atomic_write_config(
            plan.config_file,
            desired_config,
            previous=inspection.existing_config if create_backup else None,
        )
        if written_backup is not None:
            backup = written_backup
        elif backup is not None and not os.path.lexists(backup):
            backup = None
        if manifest.get("previous_config_sha256") is not None:
            manifest = {**manifest, "previous_config_sha256": None}
            _write_manifest(manifest_path, manifest, replace=True)
    return {
        "config_changed": config_changed,
        "config_backup": str(backup) if backup is not None else None,
        "data_created": data_created,
        "socket_directory_created": socket_directory_created,
        "manifest": str(manifest_path),
    }


def _ensure_install_manifest(
    plan: AdminInitPlan,
    inspection: AdminInspection,
    *,
    expected_owner: int,
) -> tuple[Path, dict]:
    path = _manifest_path(plan.config_file)
    desired_digest = _sha256(_config_payload(plan.config_document()))
    if os.path.lexists(path):
        manifest = _read_manifest(path, expected_owner=expected_owner)
        _validate_manifest_matches_plan(manifest, plan, expected_owner=expected_owner)
        if manifest["config_sha256"] != desired_digest:
            manifest = {
                **manifest,
                "previous_config_sha256": manifest["config_sha256"],
                "config_sha256": desired_digest,
            }
            _write_manifest(path, manifest, replace=True)
        return path, manifest

    config_directory_before = _directory_state(plan.config_file.parent)
    manifest = {
        "schema_version": INSTALL_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "admin_uid": expected_owner,
        "config_file": str(plan.config_file),
        "config_sha256": desired_digest,
        "previous_config_sha256": None,
        "config_before": _config_file_state(
            plan.config_file,
            expected_owner=expected_owner,
        ),
        "config_directory_before": config_directory_before,
        "data_dir": str(plan.data_dir),
        "data_directory_before": _directory_state(plan.data_dir),
        "broker_socket": str(plan.broker_socket),
        "socket_directory_before": _directory_state(plan.broker_socket.parent),
        "service_uid": plan.service.uid,
        "service_gid": plan.service.primary_gid,
        "backup_path": None,
        "backup_sha256": None,
    }
    _ensure_config_directory(plan.config_file.parent, expected_owner=expected_owner)
    try:
        _write_manifest(path, manifest, replace=False)
    except BaseException:
        if not config_directory_before["exists"] and not _directory_nonempty(
            plan.config_file.parent
        ):
            plan.config_file.parent.rmdir()
            fsync_directory(plan.config_file.parent.parent)
        raise
    return path, manifest


def _validate_manifest_matches_plan(
    manifest: dict,
    plan: AdminInitPlan,
    *,
    expected_owner: int,
) -> None:
    expected = {
        "admin_uid": expected_owner,
        "config_file": str(plan.config_file),
        "data_dir": str(plan.data_dir),
        "broker_socket": str(plan.broker_socket),
        "service_uid": plan.service.uid,
        "service_gid": plan.service.primary_gid,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise BookingError(
                f"existing install manifest does not match {key}: {_manifest_path(plan.config_file)}"
            )


def _manifest_path(config_file: Path) -> Path:
    return config_file.parent / INSTALL_MANIFEST_NAME


def _config_payload(document: dict) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _directory_state(path: Path) -> dict:
    if not os.path.lexists(path):
        return {"exists": False}
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise BookingError(f"managed directory path is not a real directory: {path}")
    return {
        "exists": True,
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _config_file_state(path: Path, *, expected_owner: int) -> dict:
    if not os.path.lexists(path):
        return {"exists": False}
    fd = open_existing_regular(path, expected_mode=CONFIG_FILE_MODE)
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != expected_owner:
            raise BookingError(
                f"existing configuration must be owned by UID {expected_owner}: {path}"
            )
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            payload = handle.read()
    finally:
        if fd >= 0:
            os.close(fd)
    return {
        "exists": True,
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "mode": stat.S_IMODE(metadata.st_mode),
        "sha256": _sha256(payload),
        "content_b64": base64.b64encode(payload).decode("ascii"),
    }


def _ensure_config_directory(path: Path, *, expected_owner: int) -> None:
    if not os.path.lexists(path):
        if not path.parent.is_dir():
            raise BookingError(
                f"configuration-directory parent does not exist: {path.parent}"
            )
        os.mkdir(path, CONFIG_DIRECTORY_MODE)
        fsync_directory(path.parent)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise BookingError(f"configuration parent is not a real directory: {path}")
    if metadata.st_uid != expected_owner:
        raise BookingError(
            f"configuration directory must be owned by UID {expected_owner}: {path}"
        )
    if stat.S_IMODE(metadata.st_mode) != CONFIG_DIRECTORY_MODE:
        raise BookingError(
            f"configuration directory mode must be {CONFIG_DIRECTORY_MODE:04o}: {path}"
        )


def _write_manifest(path: Path, manifest: dict, *, replace: bool) -> None:
    payload = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    _write_new_file(path, payload, INSTALL_MANIFEST_MODE, replace=replace)


def _read_manifest(path: Path, *, expected_owner: int) -> dict:
    fd = open_existing_regular(path, expected_mode=INSTALL_MANIFEST_MODE)
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != expected_owner:
            raise BookingError(
                f"install manifest must be owned by UID {expected_owner}: {path}"
            )
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            manifest = json.load(handle)
    except json.JSONDecodeError as exc:
        raise BookingError(f"install manifest is invalid JSON: {path}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != INSTALL_SCHEMA_VERSION
    ):
        raise BookingError(f"unsupported or invalid install manifest: {path}")
    required = {
        "admin_uid",
        "config_file",
        "config_sha256",
        "config_before",
        "config_directory_before",
        "data_dir",
        "data_directory_before",
        "broker_socket",
        "socket_directory_before",
        "service_uid",
        "service_gid",
        "backup_path",
        "backup_sha256",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise BookingError(f"install manifest is missing fields: {', '.join(missing)}")
    return manifest


def _current_managed_config(path: Path, *, expected_owner: int) -> Optional[bytes]:
    if not os.path.lexists(path):
        return None
    fd = open_existing_regular(path, expected_mode=CONFIG_FILE_MODE)
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != expected_owner:
            raise BookingError(f"managed configuration owner drifted: {path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _manifest_absolute_path(manifest: dict, key: str) -> Path:
    value = manifest.get(key)
    if not isinstance(value, str):
        raise BookingError(f"install manifest field {key} must be a path")
    return _absolute_path(Path(value))


def _manifest_nonnegative_int(manifest: dict, key: str) -> int:
    value = manifest.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BookingError(
            f"install manifest field {key} must be a non-negative integer"
        )
    return value


def _validated_directory_state(value: object, label: str) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("exists"), bool):
        raise BookingError(f"install manifest {label} is invalid")
    if not value["exists"]:
        if set(value) != {"exists"}:
            raise BookingError(f"install manifest {label} has invalid absent metadata")
        return value
    for key in ("uid", "gid", "mode"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise BookingError(f"install manifest {label}.{key} is invalid")
    if value["mode"] > 0o7777:
        raise BookingError(f"install manifest {label}.mode is invalid")
    return value


def _validated_config_state(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("exists"), bool):
        raise BookingError("install manifest config_before is invalid")
    if not value["exists"]:
        if set(value) != {"exists"}:
            raise BookingError(
                "install manifest config_before has invalid absent metadata"
            )
        return value
    for key in ("uid", "gid", "mode"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise BookingError(f"install manifest config_before.{key} is invalid")
    try:
        payload = base64.b64decode(value.get("content_b64", ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise BookingError(
            "install manifest prior configuration is not valid base64"
        ) from exc
    digest = value.get("sha256")
    if not isinstance(digest, str) or _sha256(payload) != digest:
        raise BookingError(
            "install manifest prior configuration checksum does not match"
        )
    return value


def _broker_socket_state(path: Path, *, service_uid: int) -> str:
    if not os.path.lexists(path):
        return "absent"
    metadata = path.lstat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != service_uid:
        raise BookingError(f"refusing unsafe broker path during uninstall: {path}")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        return "stale"
    except OSError as exc:
        raise BookingError(f"cannot verify broker socket state: {exc}") from exc
    else:
        return "active"
    finally:
        probe.close()


def _validated_backup_path(manifest: dict, config_file: Path) -> Optional[Path]:
    value = manifest.get("backup_path")
    digest = manifest.get("backup_sha256")
    if value is None:
        if digest is not None:
            raise BookingError("install manifest has a backup checksum without a path")
        return None
    expected = config_file.with_name(f"{config_file.name}.bak")
    path = _absolute_path(Path(value)) if isinstance(value, str) else None
    if path != expected or not isinstance(digest, str):
        raise BookingError("install manifest backup metadata is invalid")
    if os.path.lexists(path):
        fd = open_existing_regular(path, expected_mode=0o600)
        try:
            metadata = os.fstat(fd)
            if metadata.st_uid != manifest["admin_uid"]:
                raise BookingError(f"configuration backup owner drifted: {path}")
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                payload = handle.read()
        finally:
            if fd >= 0:
                os.close(fd)
        if _sha256(payload) != digest:
            raise BookingError(f"configuration backup checksum drifted: {path}")
    return path


def _validate_directory_entries(path: Path, allowed: set[str], label: str) -> None:
    if not os.path.lexists(path):
        return
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise BookingError(f"managed {label} path is not a real directory: {path}")
    unknown = sorted(item.name for item in path.iterdir() if item.name not in allowed)
    if unknown:
        raise BookingError(
            f"refusing to remove {label} directory containing unknown entries: "
            f"{', '.join(unknown)}"
        )


def _validate_managed_data_tree(data_dir: Path) -> None:
    unknown = sorted(
        item.name for item in data_dir.iterdir() if item.name not in MANAGED_DATA_NAMES
    )
    if unknown:
        raise BookingError(
            f"refusing to purge data directory containing unknown entries: {', '.join(unknown)}"
        )
    for root, directories, files in os.walk(data_dir, topdown=True, followlinks=False):
        for name in [*directories, *files]:
            path = Path(root) / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise BookingError(f"refusing symbolic link in managed data: {path}")
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise BookingError(f"refusing special file in managed data: {path}")


def _purge_managed_data(data_dir: Path) -> None:
    _validate_managed_data_tree(data_dir)
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise BookingError(
            "this platform cannot safely purge a privileged directory tree"
        )
    for item in tuple(data_dir.iterdir()):
        metadata = item.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            shutil.rmtree(item)
        else:
            item.unlink()
    fsync_directory(data_dir)


def _restore_directory_state(path: Path, state: dict) -> None:
    state = _validated_directory_state(state, "directory state")
    if not state["exists"]:
        raise BookingError("cannot restore an absent directory state")
    fd = os.open(
        str(path),
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fchown(fd, state["uid"], state["gid"])
        os.fchmod(fd, state["mode"])
    finally:
        os.close(fd)
    fsync_directory(path.parent)


def _restore_config_file(path: Path, state: dict) -> None:
    state = _validated_config_state(state)
    payload = base64.b64decode(state["content_b64"], validate=True)
    _write_new_file(path, payload, state["mode"], replace=True)
    os.chown(path, state["uid"], state["gid"])
    os.chmod(path, state["mode"])
    fsync_directory(path.parent)


def _print_uninstall_plan(inspection: dict) -> None:
    print("GPUbk administrator uninstall")
    print(f"  config:     {inspection['config_file']}")
    print(f"  data:       {inspection['data_dir']}")
    print(f"  socket:     {inspection['broker_socket']} ({inspection['socket_state']})")
    print(f"  status:     {inspection['status']}")
    for action in inspection["actions"]:
        print(f"  action:     {action}")
    for blocker in inspection["blockers"]:
        print(f"  blocked:    {blocker}")


def inspect_admin_init(
    plan: AdminInitPlan,
    *,
    force: bool = False,
    expected_owner: int = 0,
) -> AdminInspection:
    _validate_plan(plan)
    _validate_config_destination(plan.config_file, expected_owner=expected_owner)
    existing_config = _read_existing_config(
        plan.config_file,
        expected_owner=expected_owner,
    )
    desired_config = plan.config_document()
    data_exists = os.path.lexists(plan.data_dir)
    data_nonempty = _directory_nonempty(plan.data_dir) if data_exists else False

    if data_exists:
        metadata = plan.data_dir.lstat()
        actual_mode = stat.S_IMODE(metadata.st_mode)
        owner_mismatch = (
            metadata.st_uid != plan.service.uid
            or metadata.st_gid != plan.service.primary_gid
        )
        if data_nonempty and (actual_mode != plan.dir_mode or owner_mismatch):
            raise BookingError(
                "refusing to change owner or mode of a non-empty data directory; "
                "use a reviewed migration instead"
            )
    elif not plan.data_dir.parent.is_dir():
        raise BookingError(
            f"data-directory parent does not exist: {plan.data_dir.parent}"
        )

    socket_directory = plan.broker_socket.parent
    socket_directory_exists = os.path.lexists(socket_directory)
    socket_directory_nonempty = (
        _directory_nonempty(socket_directory) if socket_directory_exists else False
    )
    if socket_directory_exists:
        metadata = socket_directory.lstat()
        actual_mode = stat.S_IMODE(metadata.st_mode)
        owner_mismatch = (
            metadata.st_uid != plan.service.uid
            or metadata.st_gid != plan.service.primary_gid
        )
        if socket_directory_nonempty and (
            actual_mode != BROKER_SOCKET_DIRECTORY_MODE or owner_mismatch
        ):
            raise BookingError(
                "refusing to change owner or mode of a non-empty broker socket directory"
            )
    elif not socket_directory.parent.is_dir():
        raise BookingError(
            f"broker socket-directory parent does not exist: {socket_directory.parent}"
        )
    if os.path.lexists(plan.broker_socket):
        metadata = plan.broker_socket.lstat()
        expected_gid = (
            plan.broker_gid if plan.broker_gid is not None else plan.service.primary_gid
        )
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != plan.service.uid
            or metadata.st_gid != expected_gid
            or stat.S_IMODE(metadata.st_mode) != plan.broker_socket_mode
        ):
            raise BookingError(
                f"refusing unsafe existing broker socket: {plan.broker_socket}"
            )

    if existing_config is None and data_nonempty:
        raise BookingError(
            "refusing to initialize an unconfigured non-empty data directory; "
            "use a reviewed migration instead"
        )
    if existing_config is not None and existing_config != desired_config:
        if not force:
            raise BookingError(
                f"configuration already exists and differs: {plan.config_file}; "
                "review it or pass --force while the selected data directory is empty"
            )
        if data_nonempty:
            raise BookingError(
                "refusing to replace configuration for a non-empty data directory; "
                "use a reviewed migration instead"
            )

    config_action = (
        "create"
        if existing_config is None
        else "unchanged"
        if existing_config == desired_config
        else "replace"
    )
    if not data_exists:
        data_action = "create"
    elif (
        stat.S_IMODE(plan.data_dir.lstat().st_mode) != plan.dir_mode
        or plan.data_dir.lstat().st_uid != plan.service.uid
        or plan.data_dir.lstat().st_gid != plan.service.primary_gid
    ):
        data_action = "repair-empty-owner-or-mode"
    else:
        data_action = "unchanged"
    if not socket_directory_exists:
        socket_directory_action = "create"
    elif (
        stat.S_IMODE(socket_directory.lstat().st_mode) != BROKER_SOCKET_DIRECTORY_MODE
        or socket_directory.lstat().st_uid != plan.service.uid
        or socket_directory.lstat().st_gid != plan.service.primary_gid
    ):
        socket_directory_action = "repair-empty-owner-or-mode"
    else:
        socket_directory_action = "unchanged"
    return AdminInspection(
        existing_config=existing_config,
        data_exists=data_exists,
        data_nonempty=data_nonempty,
        socket_directory_exists=socket_directory_exists,
        socket_directory_nonempty=socket_directory_nonempty,
        config_action=config_action,
        data_action=data_action,
        socket_directory_action=socket_directory_action,
    )


def _build_plan(
    args: argparse.Namespace,
    detected_gpu_count: int,
    default_service: Optional[AdminIdentity],
    *,
    interactive: bool,
) -> AdminInitPlan:
    data_default = _absolute_path(DEFAULT_SYSTEM_DATA_DIR)
    data_dir = _ask_absolute_path(
        "Shared data directory",
        args.data_dir or data_default,
        enabled=interactive and args.data_dir is None,
    )
    access = _ask_choice(
        "Access mode",
        args.access or "all",
        ("all", "group"),
        enabled=interactive and args.access is None,
    )

    service_default = (
        default_service.username if default_service is not None else "gpubk"
    )
    service_value = _ask(
        "Service account",
        args.service_user or service_default,
        enabled=interactive and args.service_user is None,
    )
    if not service_value:
        raise BookingError(
            "service account is required; create gpubk or pass --service-user USER"
        )
    service = _ask_identity(
        service_value,
        enabled=interactive and args.service_user is None,
        label="service account",
    )

    group_name = args.group
    broker_gid = None
    if access == "group":
        group_name, group_record = _ask_group(
            group_name or "gpuusers",
            enabled=interactive and args.group is None,
        )
        broker_gid = int(group_record.gr_gid)
        broker_socket_mode = BROKER_GROUP_SOCKET_MODE
    else:
        if group_name:
            raise BookingError("--group is only valid with --access group")
        group_name = None
        broker_socket_mode = BROKER_ALL_SOCKET_MODE

    broker_socket = _ask_absolute_path(
        "Broker socket",
        args.broker_socket,
        enabled=interactive and args.broker_socket == DEFAULT_BROKER_SOCKET,
    )

    gpu_count = _ask_int(
        "GPU count",
        args.gpu_count if args.gpu_count is not None else detected_gpu_count,
        minimum=1,
        maximum=MAX_GPU_COUNT,
        enabled=interactive and args.gpu_count is None,
    )
    slot_minutes = _ask_slot_minutes(
        args.slot_minutes if args.slot_minutes is not None else DEFAULT_SLOT_MINUTES,
        enabled=interactive and args.slot_minutes is None,
    )
    max_shared_users = _ask_int(
        "Shared capacity units per GPU",
        args.max_shared_users if args.max_shared_users is not None else 2,
        minimum=1,
        maximum=MAX_SHARED_UNITS,
        enabled=interactive and args.max_shared_users is None,
    )
    require_shared_memory = (
        args.require_shared_memory
        if args.require_shared_memory is not None
        else _ask_bool(
            "Require expected VRAM for shared bookings",
            True,
            enabled=interactive,
        )
    )

    return AdminInitPlan(
        config_file=_absolute_path(args.config_file),
        data_dir=data_dir,
        access=access,
        gpu_count=gpu_count,
        slot_minutes=slot_minutes,
        max_shared_users=max_shared_users,
        require_shared_memory=require_shared_memory,
        service=service,
        group_name=group_name,
        broker_gid=broker_gid,
        broker_socket=broker_socket,
        broker_socket_mode=broker_socket_mode,
        file_mode=BROKER_FILE_MODE,
        dir_mode=BROKER_DIR_MODE,
    )


def _validate_plan(plan: AdminInitPlan) -> None:
    if plan.config_file == plan.data_dir or plan.config_file.is_relative_to(
        plan.data_dir
    ):
        raise BookingError(
            "trusted configuration must be outside the shared data directory"
        )
    if plan.broker_socket == plan.config_file:
        raise BookingError("broker socket must not replace the trusted configuration")
    if plan.broker_socket.parent == plan.config_file.parent:
        raise BookingError(
            "broker socket directory must be separate from trusted configuration"
        )
    managed_directories = (
        plan.config_file.parent,
        plan.data_dir,
        plan.broker_socket.parent,
    )
    for index, left in enumerate(managed_directories):
        for right in managed_directories[index + 1 :]:
            if _paths_overlap(left, right):
                raise BookingError(
                    f"administrator-managed directories must not overlap: {left} and {right}"
                )
    if plan.file_mode != BROKER_FILE_MODE or plan.dir_mode != BROKER_DIR_MODE:
        raise BookingError("broker storage must use service-owned modes 0644/0755")
    if plan.access == "all":
        if (
            plan.group_name is not None
            or plan.broker_gid is not None
            or plan.broker_socket_mode != BROKER_ALL_SOCKET_MODE
        ):
            raise BookingError(
                "all-user access must use a 0666 broker socket without a group"
            )
    elif plan.access == "group":
        if not plan.group_name or plan.broker_gid is None:
            raise BookingError("group access requires an existing Unix group")
        try:
            group_record = grp.getgrnam(plan.group_name)
        except KeyError as exc:
            raise BookingError(f"Unix group does not exist: {plan.group_name}") from exc
        if int(group_record.gr_gid) != plan.broker_gid:
            raise BookingError("group name and broker GID do not match")
        if plan.broker_socket_mode != BROKER_GROUP_SOCKET_MODE:
            raise BookingError("group access must use broker socket mode 0660")
        memberships = set(
            os.getgrouplist(plan.service.username, plan.service.primary_gid)
        )
        if plan.broker_gid not in memberships:
            raise BookingError(
                f"service account {plan.service.username} is not in group {plan.group_name}; "
                f"run: sudo usermod -aG {plan.group_name} {plan.service.username}"
            )
    else:
        raise BookingError(f"unknown access mode: {plan.access}")
    Config(
        data_dir=plan.data_dir,
        gpu_count=plan.gpu_count,
        slot_minutes=plan.slot_minutes,
        max_shared_users=plan.max_shared_users,
        require_shared_memory=plan.require_shared_memory,
        monitor_uid=plan.service.uid,
        file_mode=plan.file_mode,
        dir_mode=plan.dir_mode,
        broker_socket=plan.broker_socket,
        broker_uid=plan.service.uid,
        broker_gid=plan.broker_gid,
        broker_socket_mode=plan.broker_socket_mode,
    )


def _detected_gpu_count(explicit: Optional[int]) -> int:
    if explicit is not None:
        if explicit < 1 or explicit > MAX_GPU_COUNT:
            raise BookingError(f"--gpu-count must be between 1 and {MAX_GPU_COUNT}")
        return explicit
    detected = int(detect_gpu_count())
    if detected < 1:
        raise BookingError("no NVIDIA GPU detected; pass --gpu-count for a simulation setup")
    if detected > MAX_GPU_COUNT:
        raise BookingError(f"detected GPU count exceeds supported maximum {MAX_GPU_COUNT}")
    probe = snapshot(Config(DEFAULT_SYSTEM_DATA_DIR, gpu_count=detected))
    if not probe or all(device.source == "unknown" for device in probe):
        raise BookingError(
            "GPU hardware could not be verified; install gpubk[gpu] or make nvidia-smi "
            "available, otherwise pass --gpu-count explicitly for simulation"
        )
    return detected


def _default_service_identity(explicit: Optional[str]) -> Optional[AdminIdentity]:
    if explicit:
        return _resolve_identity(explicit)
    try:
        return _resolve_identity("gpubk")
    except BookingError:
        return None


def _resolve_identity(value: str) -> AdminIdentity:
    text = str(value).strip()
    try:
        record = pwd.getpwuid(int(text)) if text.isdigit() else pwd.getpwnam(text)
    except KeyError as exc:
        raise BookingError(f"local account does not exist: {text}") from exc
    if int(record.pw_uid) == 0:
        raise BookingError("service account must be non-root")
    return AdminIdentity(
        uid=int(record.pw_uid),
        username=str(record.pw_name),
        primary_gid=int(record.pw_gid),
    )


def _ask_identity(
    value: str, *, enabled: bool, label: str = "account"
) -> AdminIdentity:
    candidate = value
    while True:
        try:
            return _resolve_identity(candidate)
        except BookingError as exc:
            if not enabled:
                raise
            print(f"Invalid {label}: {exc}")
            candidate = _ask(label.title(), "", enabled=True)


def _ask_group(value: str, *, enabled: bool) -> tuple[str, grp.struct_group]:
    candidate = _ask("Existing Unix group", value, enabled=True) if enabled else value
    while True:
        try:
            return candidate, grp.getgrnam(candidate)
        except KeyError as exc:
            if not enabled:
                raise BookingError(
                    f"Unix group does not exist: {candidate}; "
                    "create it first or use --access all"
                ) from exc
            print(f"Unix group does not exist: {candidate}")
            candidate = _ask("Existing Unix group", "", enabled=True)


def _ask_absolute_path(label: str, default: Path, *, enabled: bool) -> Path:
    candidate = _ask(label, str(default), enabled=True) if enabled else str(default)
    while True:
        try:
            return _absolute_path(Path(candidate))
        except BookingError as exc:
            if not enabled:
                raise
            print(f"Invalid path: {exc}")
            candidate = _ask(label, str(default), enabled=True)


def _prepare_owned_directory(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
    mode: int,
    nonempty: bool,
    label: str,
) -> bool:
    created = False
    if os.path.lexists(path):
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise BookingError(f"{label} path is not a real directory: {path}")
        actual_mode = stat.S_IMODE(metadata.st_mode)
        owner_mismatch = metadata.st_uid != owner_uid or metadata.st_gid != owner_gid
        if nonempty and (actual_mode != mode or owner_mismatch):
            raise BookingError(
                f"refusing to change owner or mode of a non-empty {label} directory; "
                "use a reviewed migration instead"
            )
    else:
        parent = path.parent
        if not parent.is_dir():
            raise BookingError(f"{label} directory parent does not exist: {parent}")
        os.mkdir(path, mode)
        created = True

    fd = os.open(
        str(path),
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fchown(fd, owner_uid, owner_gid)
        os.fchmod(fd, mode)
        metadata = os.fstat(fd)
        if stat.S_IMODE(metadata.st_mode) != mode:
            raise BookingError(
                f"failed to apply {label} directory mode {mode:04o}: {path}"
            )
        if metadata.st_uid != owner_uid or metadata.st_gid != owner_gid:
            raise BookingError(
                f"failed to apply {label} directory owner {owner_uid}:{owner_gid}: {path}"
            )
    finally:
        os.close(fd)
    fsync_directory(path.parent)
    return created


def _validate_config_destination(path: Path, *, expected_owner: int) -> None:
    directory = path.parent
    if os.path.lexists(directory):
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise BookingError(f"configuration parent is not a real directory: {directory}")
        if stat.S_IMODE(metadata.st_mode) != CONFIG_DIRECTORY_MODE:
            raise BookingError(
                f"configuration directory mode must be {CONFIG_DIRECTORY_MODE:04o}: {directory}"
            )
        if metadata.st_uid != expected_owner:
            raise BookingError(
                f"configuration directory must be owned by UID {expected_owner}: {directory}"
            )
    else:
        parent = directory.parent
        if not parent.is_dir():
            raise BookingError(f"configuration-directory parent does not exist: {parent}")


def _read_existing_config(path: Path, *, expected_owner: int) -> Optional[dict]:
    if not os.path.lexists(path):
        return None
    fd = open_existing_regular(path)
    try:
        metadata = os.fstat(fd)
        if stat.S_IMODE(metadata.st_mode) != CONFIG_FILE_MODE:
            raise BookingError(
                f"existing configuration mode must be {CONFIG_FILE_MODE:04o}: {path}"
            )
        if metadata.st_uid != expected_owner:
            raise BookingError(
                f"existing configuration must be owned by UID {expected_owner}: {path}"
            )
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            payload = json.load(handle)
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(payload, dict):
        raise BookingError(f"existing configuration must contain a JSON object: {path}")
    return payload


def _atomic_write_config(path: Path, document: dict, *, previous: Optional[dict]) -> Optional[Path]:
    directory = path.parent
    if os.path.lexists(directory):
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise BookingError(f"configuration parent is not a real directory: {directory}")
        if stat.S_IMODE(metadata.st_mode) != CONFIG_DIRECTORY_MODE:
            raise BookingError(
                f"configuration directory mode must be {CONFIG_DIRECTORY_MODE:04o}: {directory}"
            )
    else:
        parent = directory.parent
        if not parent.is_dir():
            raise BookingError(f"configuration-directory parent does not exist: {parent}")
        os.mkdir(directory, CONFIG_DIRECTORY_MODE)
        fsync_directory(parent)

    backup = None
    if previous is not None:
        backup = directory / f"{path.name}.bak"
        if os.path.lexists(backup):
            raise BookingError(
                f"refusing to replace an existing configuration backup: {backup}"
            )
        _write_new_file(
            backup,
            (
                json.dumps(previous, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode(),
            0o600,
            replace=True,
        )
    payload = _config_payload(document)
    _write_new_file(path, payload, CONFIG_FILE_MODE, replace=True)
    return backup


def _write_new_file(path: Path, payload: bytes, mode: int, *, replace: bool) -> None:
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(directory)
    )
    temporary = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while installing administrator configuration")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        if not replace and os.path.lexists(path):
            raise FileExistsError(path)
        os.replace(temporary, path)
        fsync_directory(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def _directory_nonempty(path: Path) -> bool:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise BookingError(f"data path is not a real directory: {path}")
    return any(path.iterdir())


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise BookingError(f"administrator paths must be absolute: {path}")
    return Path(os.path.abspath(os.fspath(expanded)))


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _print_plan(plan: AdminInitPlan, inspection: AdminInspection) -> None:
    print("GPUbk administrator setup")
    print(f"  GPUs:       {plan.gpu_count}")
    print(f"  data:       {plan.data_dir}")
    print(f"  config:     {plan.config_file}")
    print(f"  time slice: {plan.slot_minutes} minutes")
    print(f"  sharing:    {plan.max_shared_users} capacity units per GPU")
    print(f"  service:    {plan.service.username} (UID {plan.service.uid})")
    print(f"  socket:     {plan.broker_socket} mode={plan.broker_socket_mode:04o}")
    print(
        f"  actions:    config={inspection.config_action}, "
        f"data={inspection.data_action}, "
        f"socket-dir={inspection.socket_directory_action}"
    )
    if plan.access == "all":
        print("  access:     all local users may connect to the broker")
    else:
        print(
            f"  access:     broker socket group {plan.group_name} (GID {plan.broker_gid})"
        )
    print(
        f"  storage:    service-only writes; files {plan.file_mode:04o}, "
        f"directories {plan.dir_mode:04o}"
    )


def _ask(label: str, default: str, *, enabled: bool) -> str:
    if not enabled:
        return str(default)
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or str(default)


def _ask_choice(
    label: str,
    default: str,
    choices: Sequence[str],
    *,
    enabled: bool,
) -> str:
    if not enabled:
        return default
    allowed = {choice.lower(): choice for choice in choices}
    while True:
        value = _ask(f"{label} ({'/'.join(choices)})", default, enabled=True).lower()
        if value in allowed:
            return allowed[value]
        print(f"Please choose one of: {', '.join(choices)}")


def _ask_int(
    label: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
    enabled: bool,
) -> int:
    if not enabled:
        value = int(default)
        if value < minimum or value > maximum:
            raise BookingError(f"{label} must be between {minimum} and {maximum}")
        return value
    while True:
        raw = _ask(label, str(default), enabled=True)
        try:
            value = int(raw)
        except ValueError:
            print("Please enter a whole number.")
            continue
        if minimum <= value <= maximum:
            return value
        print(f"Please enter a value between {minimum} and {maximum}.")


def _ask_slot_minutes(default: int, *, enabled: bool) -> int:
    if not enabled:
        return validate_slot_minutes(default)
    while True:
        raw = _ask("Reservation slice in minutes", str(default), enabled=True)
        try:
            return validate_slot_minutes(int(raw))
        except (TypeError, ValueError) as exc:
            print(f"Invalid slice: {exc}")


def _ask_bool(label: str, default: bool, *, enabled: bool) -> bool:
    if not enabled:
        return default
    marker = "Y/n" if default else "y/N"
    while True:
        value = input(f"{label} [{marker}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer y or n.")
