from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import grp
import hashlib
import json
import os
import pwd
import shutil
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Sequence

from .admin_command import (
    apply_command_link_install,
    apply_command_link_uninstall,
    inspect_command_link,
    plan_command_link_install,
    validate_command_link_document,
)
from .admin_services import (
    PHASE_INSTALLED,
    SystemServicesPlan,
    apply_installed_system_services,
    apply_system_services_install,
    apply_system_services_uninstall,
    enabled_unit_links,
    inspect_system_service_files,
    plan_system_services_install,
    plan_system_services_uninstall,
    retarget_system_services_document,
    validate_system_services_document,
)
from .admin_data import (
    clear_data_directory,
    create_data_backup,
    restore_data_backup,
    verify_data_backup,
)
from .admin_cluster import add_admin_cluster_parser, run_admin_cluster
from .config import (
    BROKER_ALL_SOCKET_MODE,
    BROKER_DIR_MODE,
    BROKER_FILE_MODE,
    BROKER_GROUP_SOCKET_MODE,
    CONFIG_UPDATE_JOURNAL_NAME,
    CONFIG_VERSION,
    MAX_GPU_COUNT,
    MAX_BOOKING_HORIZON_DAYS,
    MAX_SHARED_UNITS,
    SYSTEM_CONFIG_FILE,
    Config,
    load_config,
    validate_gpu_list,
    validate_gpu_priority,
    validate_booking_blackouts,
)
from .fileio import fsync_directory, open_existing_regular
from .gpu import detect_gpu_count, snapshot
from .granularity import DEFAULT_SLOT_MINUTES, validate_slot_minutes
from .models import BookingError
from .systemd import DEFAULT_SYSTEM_UNIT_DIR, system_unit_names


ADMIN_SCHEMA_VERSION = "gpubk.admin.v1"
INSTALL_SCHEMA_VERSION = "gpubk.install.v1"
TRANSFER_SCHEMA_VERSION = "gpubk.transfer.v1"
CONFIG_UPDATE_SCHEMA_VERSION = "gpubk.config-update.v1"
DEFAULT_SYSTEM_DATA_DIR = Path("/var/lib/gpubk")
DEFAULT_BROKER_SOCKET = Path("/run/gpubk/broker.sock")
DEFAULT_COMMAND_LINK = Path("/usr/local/bin/bk")
CONFIG_DIRECTORY_MODE = 0o755
CONFIG_FILE_MODE = 0o644
INSTALL_MANIFEST_NAME = "install.json"
CLUSTER_CATALOG_NAME = "cluster.json"
TRANSFER_JOURNAL_NAME = "transfer.json"
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
    disabled_gpus: tuple[int, ...] = ()
    gpu_priority: tuple[tuple[int, int], ...] = ()

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
        if self.disabled_gpus:
            document["disabled_gpus"] = list(self.disabled_gpus)
        if self.gpu_priority:
            document["gpu_priority"] = {
                str(gpu): priority for gpu, priority in self.gpu_priority
            }
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
            "disabled_gpus": list(self.disabled_gpus),
            "gpu_priority": {
                str(gpu): priority for gpu, priority in self.gpu_priority
            },
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


@dataclass(frozen=True)
class AdminTransferPlan:
    config_file: Path
    data_dir: Path
    broker_socket: Path
    current_service: AdminIdentity
    target_service: AdminIdentity
    broker_gid: Optional[int]
    broker_socket_mode: int
    config_document: dict
    manifest: dict

    def public_document(self, *, status: str, blockers: Sequence[str]) -> dict:
        return {
            "schema_version": ADMIN_SCHEMA_VERSION,
            "kind": "admin-transfer",
            "status": status,
            "config_file": str(self.config_file),
            "data_dir": str(self.data_dir),
            "broker_socket": str(self.broker_socket),
            "from": {
                "uid": self.current_service.uid,
                "gid": self.current_service.primary_gid,
                "username": self.current_service.username,
            },
            "to": {
                "uid": self.target_service.uid,
                "gid": self.target_service.primary_gid,
                "username": self.target_service.username,
            },
            "preserves": [
                "reservations",
                "reservation_uids",
                "audit_history",
                "usage_history",
                "scheduling_policy",
            ],
            "blockers": list(blockers),
        }


def _public_blackouts(
    blackouts: Sequence[tuple[str, str, str]],
) -> list[dict[str, str]]:
    return [
        {"start_at": start, "end_at": end, "reason": reason}
        for start, end, reason in blackouts
    ]


@dataclass(frozen=True)
class AdminGpuPolicyPlan:
    config_file: Path
    data_dir: Path
    broker_socket: Path
    service: AdminIdentity
    current_disabled_gpus: tuple[int, ...]
    desired_disabled_gpus: tuple[int, ...]
    current_gpu_priority: tuple[tuple[int, int], ...]
    desired_gpu_priority: tuple[tuple[int, int], ...]
    current_require_shared_memory: bool
    desired_require_shared_memory: bool
    require_shared_memory_update: Optional[bool]
    current_document: dict
    desired_document: dict
    blockers: tuple[str, ...]
    current_booking_horizon_days: int = 30
    desired_booking_horizon_days: int = 30
    booking_horizon_days_update: Optional[int] = None
    current_booking_blackouts: tuple[tuple[str, str, str], ...] = ()
    desired_booking_blackouts: tuple[tuple[str, str, str], ...] = ()
    booking_blackouts_update: Optional[tuple[tuple[str, str, str], ...]] = None

    @property
    def changed(self) -> bool:
        return self.current_document != self.desired_document

    def public_document(self, *, status: str) -> dict:
        return {
            "schema_version": ADMIN_SCHEMA_VERSION,
            "kind": "admin-gpu-policy",
            "status": status,
            "config_file": str(self.config_file),
            "current": {
                "disabled_gpus": list(self.current_disabled_gpus),
                "gpu_priority": {
                    str(gpu): priority for gpu, priority in self.current_gpu_priority
                },
                "require_shared_memory": self.current_require_shared_memory,
                "booking_horizon_days": self.current_booking_horizon_days,
                "booking_blackouts": _public_blackouts(self.current_booking_blackouts),
            },
            "desired": {
                "disabled_gpus": list(self.desired_disabled_gpus),
                "gpu_priority": {
                    str(gpu): priority for gpu, priority in self.desired_gpu_priority
                },
                "require_shared_memory": self.desired_require_shared_memory,
                "booking_horizon_days": self.desired_booking_horizon_days,
                "booking_blackouts": _public_blackouts(self.desired_booking_blackouts),
            },
            "changed": self.changed,
            "blockers": list(self.blockers),
        }


def run_admin_cli(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="bk admin",
        description=(
            "Initialize, supervise, transfer, or safely remove a shared GPUBK server."
        ),
        epilog=(
            "Common maintenance:\n"
            "  sudo bk admin install\n"
            "  sudo bk admin data backup /var/backups/gpubk/pre-upgrade\n"
            "  sudo bk admin data verify /var/backups/gpubk/pre-upgrade\n"
            "  sudo bk admin data clear --backup-to /var/backups/gpubk/pre-clear --yes\n"
            "Run 'bk admin COMMAND -h' for command-specific safety requirements."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="action", required=True)
    install_parser = commands.add_parser(
        "install",
        help="initialize once, or reconcile an existing managed deployment",
        description=(
            "Initialize a new shared server. If a valid install manifest already "
            "exists, preserve configuration and data while reconciling the tracked "
            "command, systemd units, and service state."
        ),
    )
    install_parser.add_argument("--config-file", type=Path, default=SYSTEM_CONFIG_FILE)
    install_parser.add_argument("--data-dir", type=Path)
    install_parser.add_argument("--access", choices=("all", "group"))
    install_parser.add_argument("--group")
    install_parser.add_argument("--service-user")
    install_parser.add_argument(
        "--broker-socket",
        type=Path,
        help="broker socket for a new deployment (default: /run/gpubk/broker.sock)",
    )
    install_parser.add_argument("--gpu-count", type=int)
    install_parser.add_argument("--disabled-gpus")
    install_parser.add_argument("--gpu-priority")
    install_parser.add_argument("--slot-minutes", type=int)
    install_parser.add_argument("--max-shared-users", type=int)
    install_memory = install_parser.add_mutually_exclusive_group()
    install_memory.add_argument(
        "--require-shared-memory",
        dest="require_shared_memory",
        action="store_true",
    )
    install_memory.add_argument(
        "--allow-implicit-shared-memory",
        dest="require_shared_memory",
        action="store_false",
    )
    install_parser.set_defaults(require_shared_memory=None)
    install_parser.add_argument("--python-executable", type=Path)
    install_parser.add_argument(
        "--command-path",
        type=Path,
        help="system-wide bk link (default: /usr/local/bin/bk)",
    )
    install_parser.add_argument(
        "--command-target",
        type=Path,
        help="installed bk executable (default: sibling of the selected Python)",
    )
    install_parser.add_argument(
        "--no-command-link",
        action="store_true",
        help="leave the system-wide bk command unchanged",
    )
    install_parser.add_argument(
        "--login-hook",
        action="store_true",
        help="install or refresh the bounded interactive-login reminder",
    )
    install_parser.add_argument(
        "--no-start",
        action="store_true",
        help="write tracked files but do not manage service state",
    )
    install_parser.add_argument(
        "--force",
        action="store_true",
        help="adopt reviewed untracked unit files during initial service setup",
    )
    install_parser.add_argument("--yes", action="store_true", help="apply without prompting")
    install_parser.add_argument("--dry-run", action="store_true", help="show the plan only")
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
        help=(
            "existing non-root account that exclusively writes GPUBK state "
            "(default: the account that invoked sudo)"
        ),
    )
    init_parser.add_argument(
        "--broker-socket", type=Path, default=DEFAULT_BROKER_SOCKET
    )
    init_parser.add_argument("--gpu-count", type=int)
    init_parser.add_argument(
        "--disabled-gpus",
        help="comma-separated GPU IDs unavailable for new reservations",
    )
    init_parser.add_argument(
        "--gpu-priority",
        help="lower preference tiers as GPU=LEVEL pairs, e.g. 6=10,7=20",
    )
    init_parser.add_argument("--slot-minutes", type=int)
    init_parser.add_argument("--max-shared-users", type=int)
    memory = init_parser.add_mutually_exclusive_group()
    memory.add_argument(
        "--require-shared-memory",
        dest="require_shared_memory",
        action="store_true",
        help="require a resolved VRAM budget; auto needs complete GPU memory telemetry",
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
    transfer_parser = commands.add_parser(
        "transfer",
        help="transfer broker and monitor ownership to another local account",
    )
    transfer_parser.add_argument(
        "service_user",
        nargs="?",
        help="existing non-root account that will own GPUBK state",
    )
    transfer_parser.add_argument(
        "--config-file", type=Path, default=SYSTEM_CONFIG_FILE
    )
    transfer_parser.add_argument(
        "--recover",
        action="store_true",
        help="roll back an interrupted service-account transfer",
    )
    transfer_parser.add_argument(
        "--yes", action="store_true", help="apply without confirmation"
    )
    transfer_parser.add_argument(
        "--dry-run", action="store_true", help="show the plan only"
    )
    transfer_parser.add_argument("--json", action="store_true")
    policy_parser = commands.add_parser(
        "gpu-policy",
        help="safely update GPU, VRAM, horizon, and blackout policy",
    )
    policy_parser.add_argument(
        "--config-file", type=Path, default=SYSTEM_CONFIG_FILE
    )
    disabled = policy_parser.add_mutually_exclusive_group()
    disabled.add_argument(
        "--disabled-gpus",
        help="replace the full comma-separated disabled GPU list",
    )
    disabled.add_argument(
        "--enable-all",
        action="store_true",
        help="clear the administrator-disabled GPU list",
    )
    priority = policy_parser.add_mutually_exclusive_group()
    priority.add_argument(
        "--gpu-priority",
        help="replace preference tiers, e.g. 6=10,7=20; larger runs later",
    )
    priority.add_argument(
        "--clear-priority",
        action="store_true",
        help="restore equal administrator preference for every GPU",
    )
    shared_memory = policy_parser.add_mutually_exclusive_group()
    shared_memory.add_argument(
        "--require-shared-memory",
        dest="require_shared_memory",
        action="store_true",
        help="require every shared reservation to resolve a per-GPU VRAM budget",
    )
    shared_memory.add_argument(
        "--allow-implicit-shared-memory",
        dest="require_shared_memory",
        action="store_false",
        help="allow shared reservations to omit VRAM and retain automatic estimation",
    )
    policy_parser.set_defaults(require_shared_memory=None)
    policy_parser.add_argument(
        "--booking-horizon-days",
        type=int,
        help="maximum future booking horizon (default policy: 30 days)",
    )
    blackouts = policy_parser.add_mutually_exclusive_group()
    blackouts.add_argument(
        "--blackout",
        nargs=3,
        action="append",
        metavar=("START", "END", "REASON"),
        help="replace blackout windows; repeat for more (timezone-aware ISO times)",
    )
    blackouts.add_argument(
        "--clear-blackouts",
        action="store_true",
        help="remove all administrator blackout windows",
    )
    policy_parser.add_argument(
        "--recover",
        action="store_true",
        help="roll back an interrupted managed configuration update",
    )
    policy_parser.add_argument("--yes", action="store_true")
    policy_parser.add_argument("--dry-run", action="store_true")
    policy_parser.add_argument("--json", action="store_true")
    cancel_parser = commands.add_parser(
        "cancel",
        help="cancel any active reservation with an owner-visible reason",
    )
    cancel_parser.add_argument("reservation", help="full or unique short reservation ID")
    cancel_parser.add_argument(
        "--reason",
        required=True,
        help="reason preserved in the reservation, audit log, and user notification",
    )
    cancel_parser.add_argument(
        "--config-file", type=Path, default=SYSTEM_CONFIG_FILE
    )
    cancel_parser.add_argument("--yes", action="store_true")
    cancel_parser.add_argument("--json", action="store_true")
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
        help="remove validated GPUBK ledger and usage data",
    )
    uninstall_parser.add_argument(
        "--yes", action="store_true", help="apply without confirmation"
    )
    uninstall_parser.add_argument(
        "--dry-run", action="store_true", help="show the plan only"
    )
    uninstall_parser.add_argument("--json", action="store_true")
    data_parser = commands.add_parser(
        "data",
        help="back up, verify, clear, or restore managed server data",
        description=(
            "Maintain the complete reservation, audit, and usage data tree. "
            "Backup, clear, and restore require stopped broker/monitor services; "
            "clear always creates and verifies a backup first."
        ),
        epilog=(
            "Examples:\n"
            "  sudo bk admin data backup /var/backups/gpubk/pre-upgrade\n"
            "  sudo bk admin data verify /var/backups/gpubk/pre-upgrade\n"
            "  sudo bk admin data clear --backup-to /var/backups/gpubk/pre-clear --yes\n"
            "  sudo bk admin data restore /var/backups/gpubk/pre-clear --yes"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    data_commands = data_parser.add_subparsers(dest="data_action", required=True)
    data_backup = data_commands.add_parser(
        "backup", help="create a checksummed, immutable-style data snapshot"
    )
    data_backup.add_argument("path", nargs="?", type=Path)
    data_backup.add_argument("--config-file", type=Path, default=SYSTEM_CONFIG_FILE)
    data_backup.add_argument("--json", action="store_true")
    data_verify = data_commands.add_parser(
        "verify", help="verify a backup without changing server data"
    )
    data_verify.add_argument("path", type=Path)
    data_verify.add_argument("--json", action="store_true")
    data_clear = data_commands.add_parser(
        "clear", help="back up first, then atomically replace server data with empty state"
    )
    data_clear.add_argument("--backup-to", type=Path)
    data_clear.add_argument("--config-file", type=Path, default=SYSTEM_CONFIG_FILE)
    data_clear.add_argument("--yes", action="store_true")
    data_clear.add_argument("--json", action="store_true")
    data_restore = data_commands.add_parser(
        "restore", help="verify and restore a backup into empty managed data"
    )
    data_restore.add_argument("path", type=Path)
    data_restore.add_argument("--config-file", type=Path, default=SYSTEM_CONFIG_FILE)
    data_restore.add_argument("--yes", action="store_true")
    data_restore.add_argument("--json", action="store_true")
    services_parser = commands.add_parser(
        "services",
        help="install, inspect, or remove tracked system-level services",
    )
    service_commands = services_parser.add_subparsers(
        dest="service_action", required=True
    )
    services_install = service_commands.add_parser(
        "install",
        help="install boot-persistent broker and monitor units",
    )
    services_install.add_argument(
        "--config-file", type=Path, default=SYSTEM_CONFIG_FILE
    )
    services_install.add_argument(
        "--python-executable",
        type=Path,
        help="absolute interpreter path (default: current interpreter, then tracked path)",
    )
    services_install.add_argument(
        "--force",
        action="store_true",
        help="replace reviewed pre-existing unit files and restore them on uninstall",
    )
    services_install.add_argument("--yes", action="store_true")
    services_install.add_argument("--dry-run", action="store_true")
    services_install.add_argument("--json", action="store_true")
    services_status = service_commands.add_parser(
        "status", help="inspect tracked system-level service files"
    )
    services_status.add_argument(
        "--config-file", type=Path, default=SYSTEM_CONFIG_FILE
    )
    services_status.add_argument("--json", action="store_true")
    services_uninstall = service_commands.add_parser(
        "uninstall",
        help="restore or remove tracked system-level service files",
    )
    services_uninstall.add_argument(
        "--config-file", type=Path, default=SYSTEM_CONFIG_FILE
    )
    services_uninstall.add_argument("--yes", action="store_true")
    services_uninstall.add_argument("--dry-run", action="store_true")
    services_uninstall.add_argument("--json", action="store_true")
    login_hook_parser = commands.add_parser(
        "login-hook",
        help="install, inspect, or remove the optional login reservation notice",
    )
    login_hook_commands = login_hook_parser.add_subparsers(
        dest="login_hook_action", required=True
    )
    login_hook_install = login_hook_commands.add_parser(
        "install", help="install the bounded interactive-login notice"
    )
    login_hook_install.add_argument(
        "--executable",
        type=Path,
        default=Path("/usr/local/bin/bk"),
        help="absolute bk executable used by the hook",
    )
    login_hook_install.add_argument("--yes", action="store_true")
    login_hook_install.add_argument("--dry-run", action="store_true")
    login_hook_install.add_argument("--json", action="store_true")
    login_hook_status = login_hook_commands.add_parser(
        "status", help="inspect the optional login notice"
    )
    login_hook_status.add_argument("--json", action="store_true")
    login_hook_uninstall = login_hook_commands.add_parser(
        "uninstall", help="remove only the managed GPUBK login notice"
    )
    login_hook_uninstall.add_argument("--yes", action="store_true")
    login_hook_uninstall.add_argument("--dry-run", action="store_true")
    login_hook_uninstall.add_argument("--json", action="store_true")
    add_admin_cluster_parser(commands)
    args = parser.parse_args(list(argv))

    if args.action == "install":
        return _run_admin_install(args)
    if args.action == "data":
        return _run_admin_data(args)
    if args.action == "cluster":
        return run_admin_cluster(args)
    if args.action == "login-hook":
        return _run_admin_login_hook(args)
    if args.action == "services":
        return _run_admin_services(args)
    if args.action == "uninstall":
        return _run_admin_uninstall(args)
    if args.action == "transfer":
        return _run_admin_transfer(args)
    if args.action == "gpu-policy":
        return _run_admin_gpu_policy(args)
    if args.action == "cancel":
        return _run_admin_cancel(args)

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
            "next: install boot services with "
            "'sudo bk admin services install --yes', or test in the foreground with "
            f"'bk broker' as {plan.service.username}"
        )
    return 0


def _run_admin_cancel(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        raise BookingError("administrator cancellation must run as root; use sudo")
    previous = os.environ.get("BK_CONFIG_FILE")
    os.environ["BK_CONFIG_FILE"] = str(args.config_file)
    try:
        config = load_config()
    finally:
        if previous is None:
            os.environ.pop("BK_CONFIG_FILE", None)
        else:
            os.environ["BK_CONFIG_FILE"] = previous
    from .broker import ledger_store_for_config
    from .identity import current_actor
    from .scheduler import cancel_booking_as_admin

    store = ledger_store_for_config(config)
    ledger = store.load()
    token = str(args.reservation)
    matches = [
        item
        for item in ledger.get("reservations", [])
        if str(item.get("id", "")).startswith(token)
    ]
    if not matches:
        raise BookingError("reservation not found")
    if len(matches) > 1:
        raise BookingError("reservation ID prefix is ambiguous; provide more characters")
    target = matches[0]
    if not args.yes:
        if not sys.stdin.isatty():
            raise BookingError("pass --yes to confirm administrator cancellation")
        answer = input(
            f"Cancel {str(target['id'])[:8]} owned by {target.get('username')}? [y/N]: "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            print("No changes made.")
            return 1
    reservation = cancel_booking_as_admin(
        store,
        config,
        str(target["id"]),
        current_actor(),
        args.reason,
    )
    result = {
        "schema_version": ADMIN_SCHEMA_VERSION,
        "kind": "admin-cancel",
        "status": "cancelled",
        "reservation_id": reservation["id"],
        "owner_uid": reservation["uid"],
        "owner_username": reservation.get("username"),
        "reason": args.reason.strip(),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"cancelled: {str(reservation['id'])[:8]} owner={reservation.get('username')} "
            f"reason={args.reason.strip()}"
        )
        print("The owner can see this cancellation with `bk notifications` or `bk l --history`.")
    return 0


def _run_admin_data(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        raise BookingError("administrator data maintenance must run as root; use sudo bk admin data")
    action = args.data_action
    if action == "verify":
        result = verify_data_backup(args.path)
        return _print_admin_data_result(result, json_output=args.json)

    config_file = _absolute_path(args.config_file)
    manifest, _document = _load_admin_services_manifest(config_file, expected_owner=0)
    data_dir = _manifest_absolute_path(manifest, "data_dir")
    broker_socket = _manifest_absolute_path(manifest, "broker_socket")
    service_uid = _manifest_nonnegative_int(manifest, "service_uid")
    service_gid = _manifest_nonnegative_int(manifest, "service_gid")
    owner = (service_uid, service_gid)
    _validate_transfer_directory(data_dir, {owner}, BROKER_DIR_MODE, "data")
    _validate_transfer_directory(
        broker_socket.parent,
        {owner},
        BROKER_SOCKET_DIRECTORY_MODE,
        "broker socket",
    )
    blockers = _admin_service_blockers(
        data_dir,
        broker_socket,
        owner_pair={owner},
        socket_owner_uids={0, service_uid},
    )
    if blockers:
        raise BookingError(
            "; ".join(blockers)
            + "; stop gpubk-broker.service and gpubk-monitor.service, then retry"
        )

    backup_path = getattr(args, "path", None) or getattr(args, "backup_to", None)
    if action in {"backup", "clear"} and backup_path is None:
        backup_path = _default_admin_backup_path()
    if action in {"clear", "restore"} and not args.yes:
        if args.json or not sys.stdin.isatty():
            print(f"bk: pass --yes to {action} managed server data", file=sys.stderr)
            return 1
        prompt = (
            f"Clear all managed data after backup to {backup_path}? [y/N]: "
            if action == "clear"
            else f"Restore {args.path} into the empty managed data directory? [y/N]: "
        )
        if input(prompt).strip().lower() not in {"y", "yes"}:
            print("No changes made.")
            return 1

    with _admin_service_guard(
        data_dir,
        broker_socket,
        allowed_owner_pairs={owner},
        lock_owner=owner,
        socket_owner_uids={0, service_uid},
        operation=f"data {action}",
    ):
        if action == "backup":
            result = create_data_backup(
                data_dir,
                config_file,
                backup_path,
                service_uid=service_uid,
                service_gid=service_gid,
            )
        elif action == "clear":
            result = create_data_backup(
                data_dir,
                config_file,
                backup_path,
                service_uid=service_uid,
                service_gid=service_gid,
            )
            clear_data_directory(
                data_dir,
                service_uid=service_uid,
                service_gid=service_gid,
                directory_mode=BROKER_DIR_MODE,
            )
            result = {**result, "status": "cleared", "data_dir": str(data_dir)}
        else:
            result = restore_data_backup(
                args.path,
                data_dir,
                service_uid=service_uid,
                service_gid=service_gid,
                directory_mode=BROKER_DIR_MODE,
            )
            result = {**result, "status": "restored", "data_dir": str(data_dir)}
    return _print_admin_data_result(result, json_output=args.json)


def _default_admin_backup_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("/var/backups/gpubk") / f"gpubk-{stamp}"


def _print_admin_data_result(result: dict, *, json_output: bool) -> int:
    public = {key: value for key, value in result.items() if key != "manifest"}
    if json_output:
        print(json.dumps(public, ensure_ascii=False, sort_keys=True))
    else:
        print(f"data {public['status']}: {public['path']}")
        print(
            f"contents: {public['files']} file(s), {public['directories']} "
            f"director(ies), {public['bytes']} byte(s)"
        )
        if public.get("data_dir"):
            print(f"data directory: {public['data_dir']}")
    return 0


def _run_admin_login_hook(args: argparse.Namespace) -> int:
    from .login_hook import (
        DEFAULT_LOGIN_EXECUTABLE,
        apply_login_hook_install,
        apply_login_hook_uninstall,
        inspect_login_hook,
    )

    action = args.login_hook_action
    executable = getattr(args, "executable", DEFAULT_LOGIN_EXECUTABLE)
    inspection = inspect_login_hook(executable=executable, expected_owner=0)
    if action == "status":
        if args.json:
            print(json.dumps(inspection, ensure_ascii=False, sort_keys=True))
        else:
            print(f"GPUBK login notice: {inspection['status']}")
            print(f"  path: {inspection['path']}")
            print(f"  executable: {inspection['executable']}")
            for blocker in inspection["blockers"]:
                print(f"  blocked: {blocker}")
        return 1 if inspection["blockers"] else 0

    verb = "install" if action == "install" else "remove"
    if args.json and args.dry_run:
        print(json.dumps({**inspection, "status": "dry-run", "action": verb}, sort_keys=True))
        return 0
    if not args.json:
        print(f"GPUBK login notice: {verb}")
        print(f"  path: {inspection['path']}")
        print("  guard: interactive terminal only; 1 second timeout; errors suppressed")
        for blocker in inspection["blockers"]:
            print(f"  blocked: {blocker}")
    if inspection["blockers"]:
        raise BookingError("; ".join(inspection["blockers"]))
    if args.dry_run:
        return 0
    if not args.yes:
        if args.json or not sys.stdin.isatty():
            print(f"bk: pass --yes to {verb} the login notice", file=sys.stderr)
            return 1
        answer = input(f"{verb.capitalize()} the login notice? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("No changes made.")
            return 1
    result = (
        apply_login_hook_install(executable=executable)
        if action == "install"
        else apply_login_hook_uninstall()
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        outcome = "installed" if action == "install" else "removed"
        print(f"login notice {outcome}: {result['path']}")
    return 0


def _run_admin_install(args: argparse.Namespace) -> int:
    if os.geteuid() != 0 and not args.dry_run:
        raise BookingError("administrator install must run as root; use sudo bk admin install")
    config_file = _absolute_path(args.config_file)
    if os.path.lexists(_manifest_path(config_file)):
        return _run_existing_admin_install(args, config_file)

    interactive = sys.stdin.isatty() and not args.yes
    detected_gpu_count = _detected_gpu_count(args.gpu_count)
    default_service = _default_service_identity(args.service_user)
    plan = _build_plan(
        args,
        detected_gpu_count,
        default_service,
        interactive=interactive,
    )
    _validate_plan(plan)
    inspection = inspect_admin_init(plan, force=args.force, expected_owner=0)
    login_hook = bool(args.login_hook)
    if interactive and not args.login_hook:
        login_hook = _ask_bool("Install the interactive login reminder", True, enabled=True)

    python_executable = _absolute_path(args.python_executable or Path(sys.executable))
    command_path = _absolute_path(args.command_path or DEFAULT_COMMAND_LINK)
    command_target = _absolute_path(
        args.command_target or python_executable.with_name("bk")
    )
    command_preview = None
    if not args.no_command_link and (os.geteuid() == 0 or not args.dry_run):
        command_preview = plan_command_link_install(
            existing=None,
            destination=command_path,
            target=command_target,
            expected_owner=0,
        )
        if command_preview.blockers:
            raise BookingError("; ".join(command_preview.blockers))

    _print_plan(plan, inspection)
    print(f"  Python:  {python_executable}")
    if args.no_command_link:
        print("  command: leave system-wide bk unchanged")
    else:
        suffix = (
            f" ({command_preview.status})"
            if command_preview is not None
            else " (validate when applied as root)"
        )
        print(f"  command: {command_path} -> {command_target}{suffix}")
    print(f"  login:   {'install reminder' if login_hook else 'leave unchanged'}")
    print(f"  services: {'install only' if args.no_start else 'install, enable, and start'}")
    if args.dry_run:
        print("Dry run: no files or services changed.")
        return 0
    if not args.yes:
        answer = input("Install GPUBK with this configuration? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("No changes made.")
            return 1

    systemctl = None if args.no_start else shutil.which("systemctl")
    if not args.no_start and systemctl is None:
        raise BookingError("systemctl is unavailable; use --no-start and supervise services manually")

    apply_admin_init(plan, force=args.force)
    if not args.no_command_link:
        apply_admin_command_link_install(
            plan.config_file,
            destination=command_path,
            target=command_target,
        )
    service_plan, service_inspection = inspect_admin_system_services(
        plan.config_file,
        operation="install",
        python_executable=args.python_executable or Path(sys.executable),
        force=args.force,
        expected_owner=0,
    )
    if service_inspection["blockers"]:
        raise BookingError("; ".join(service_inspection["blockers"]))
    apply_admin_system_services_install(
        plan.config_file,
        service_plan=service_plan,
    )
    if login_hook:
        from .login_hook import apply_login_hook_install

        apply_login_hook_install(executable=command_path)
    if systemctl is not None:
        _run_systemctl(systemctl, "daemon-reload")
        _run_systemctl(
            systemctl,
            "enable",
            "--now",
            "gpubk-broker.service",
            "gpubk-monitor.service",
        )
    print("installed: GPUBK configuration and tracked system services")
    if systemctl is not None:
        print("started: gpubk-broker.service and gpubk-monitor.service")
    else:
        print("next: start the broker and monitor using your service supervisor")
    print("verify as an ordinary user: bk doctor --probe --require-monitor --strict")
    return 0


def _run_existing_admin_install(args: argparse.Namespace, config_file: Path) -> int:
    if os.geteuid() != 0:
        raise BookingError(
            "reconciling an existing installation must run as root; "
            "use sudo bk admin install"
        )
    manifest, document = _load_admin_services_manifest(
        config_file,
        expected_owner=0,
    )
    _validate_existing_install_arguments(args, document, manifest)

    python_executable = _absolute_path(args.python_executable or Path(sys.executable))
    tracked_link = manifest.get("command_link")
    tracked_destination = (
        Path(tracked_link["destination"])
        if isinstance(tracked_link, dict)
        else DEFAULT_COMMAND_LINK
    )
    tracked_target = (
        Path(tracked_link["target"])
        if isinstance(tracked_link, dict)
        else python_executable.with_name("bk")
    )
    command_path = _absolute_path(args.command_path or tracked_destination)
    command_target = _absolute_path(args.command_target or tracked_target)
    command_preview = None
    if not args.no_command_link:
        command_preview = plan_command_link_install(
            existing=tracked_link,
            destination=command_path,
            target=command_target,
            expected_owner=0,
        )
        if command_preview.blockers:
            raise BookingError("; ".join(command_preview.blockers))

    systemctl = None if args.no_start else shutil.which("systemctl")
    if not args.no_start and systemctl is None:
        raise BookingError(
            "systemctl is unavailable; use --no-start and supervise services manually"
        )
    stop_managed_services = (
        systemctl is not None and manifest.get("system_services") is not None
    )
    service_plan, service_inspection = inspect_admin_system_services(
        config_file,
        operation="install",
        python_executable=python_executable,
        force=args.force,
        expected_owner=0,
        allow_running=stop_managed_services,
    )
    if service_inspection["blockers"]:
        raise BookingError("; ".join(service_inspection["blockers"]))

    print("GPUBK managed installation")
    print(f"  config:   preserve {config_file}")
    print(f"  data:     preserve {manifest['data_dir']}")
    print(f"  Python:   {python_executable}")
    if args.no_command_link:
        print("  command:  leave system-wide bk unchanged")
    else:
        print(
            f"  command:  {command_path} -> {command_target} "
            f"({command_preview.status})"
        )
    if args.login_hook:
        print("  login:    install or refresh reminder")
    else:
        print("  login:    preserve current setting")
    if systemctl is None:
        print("  services: reconcile files; leave stopped")
    elif stop_managed_services:
        print("  services: stop, reconcile, enable, and restart")
    else:
        print("  services: install, enable, and start")
    print("  policy:   preserve all scheduling and GPU settings")
    if args.dry_run:
        print("Dry run: no files or services changed.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("bk: pass --yes to reconcile this installation", file=sys.stderr)
            return 1
        answer = input("Reconcile this managed installation? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("No changes made.")
            return 1

    stopped_services = False
    try:
        if stop_managed_services:
            stopped_services = True
            _run_systemctl(
                systemctl,
                "stop",
                "gpubk-broker.service",
                "gpubk-monitor.service",
            )

        service_plan, service_inspection = inspect_admin_system_services(
            config_file,
            operation="install",
            python_executable=python_executable,
            force=args.force,
            expected_owner=0,
        )
        if service_inspection["blockers"]:
            raise BookingError("; ".join(service_inspection["blockers"]))
        if not args.no_command_link:
            apply_admin_command_link_install(
                config_file,
                destination=command_path,
                target=command_target,
            )
        apply_admin_system_services_install(
            config_file,
            service_plan=service_plan,
        )
        if args.login_hook:
            from .login_hook import apply_login_hook_install

            apply_login_hook_install(executable=command_path)
        if systemctl is not None:
            _run_systemctl(systemctl, "daemon-reload")
            _run_systemctl(
                systemctl,
                "enable",
                "--now",
                "gpubk-broker.service",
                "gpubk-monitor.service",
            )
            stopped_services = False
    except BaseException as exc:
        if stopped_services:
            recovery_errors = []
            for action in (
                ("daemon-reload",),
                (
                    "start",
                    "gpubk-broker.service",
                    "gpubk-monitor.service",
                ),
            ):
                try:
                    _run_systemctl(systemctl, *action)
                except BaseException as recovery_exc:
                    recovery_errors.append(str(recovery_exc))
            if recovery_errors:
                raise BookingError(
                    f"installation reconciliation failed: {exc}; service recovery also "
                    f"failed: {'; '.join(recovery_errors)}"
                ) from exc
        raise
    print("reconciled: configuration and data were preserved")
    if systemctl is not None:
        print("started: gpubk-broker.service and gpubk-monitor.service")
    else:
        print("next: start the broker and monitor using your service supervisor")
    print("verify as an ordinary user: bk doctor --probe --require-monitor --strict")
    return 0


def _validate_existing_install_arguments(
    args: argparse.Namespace,
    document: dict,
    manifest: dict,
) -> None:
    mismatches = []

    def compare(flag: str, explicit: object, current: object) -> None:
        if explicit is not None and explicit != current:
            mismatches.append(flag)

    if args.data_dir is not None:
        compare(
            "--data-dir",
            str(_absolute_path(args.data_dir)),
            document.get("data_dir"),
        )
    current_access = "group" if document.get("broker_gid") is not None else "all"
    compare("--access", args.access, current_access)
    if args.group is not None:
        try:
            requested_gid = int(grp.getgrnam(args.group).gr_gid)
        except KeyError as exc:
            raise BookingError(f"Unix group does not exist: {args.group}") from exc
        compare("--group", requested_gid, document.get("broker_gid"))
    if args.service_user is not None:
        compare(
            "--service-user",
            _resolve_identity(args.service_user).uid,
            manifest.get("service_uid"),
        )
    if args.broker_socket is not None:
        compare(
            "--broker-socket",
            str(_absolute_path(args.broker_socket)),
            document.get("broker_socket"),
        )
    compare("--gpu-count", args.gpu_count, document.get("gpu_count"))
    compare("--slot-minutes", args.slot_minutes, document.get("slot_minutes"))
    compare(
        "--max-shared-users",
        args.max_shared_users,
        document.get("max_shared_users"),
    )
    compare(
        "--require-shared-memory/--allow-implicit-shared-memory",
        args.require_shared_memory,
        document.get("require_shared_memory"),
    )

    gpu_count = document.get("gpu_count")
    if isinstance(gpu_count, bool) or not isinstance(gpu_count, int):
        raise BookingError("trusted configuration gpu_count is invalid")
    try:
        current_disabled = validate_gpu_list(
            document.get("disabled_gpus"),
            gpu_count,
            "disabled_gpus",
        )
        current_priority = validate_gpu_priority(
            document.get("gpu_priority"),
            gpu_count,
        )
        if args.disabled_gpus is not None:
            compare(
                "--disabled-gpus",
                validate_gpu_list(args.disabled_gpus, gpu_count, "disabled_gpus"),
                current_disabled,
            )
        if args.gpu_priority is not None:
            compare(
                "--gpu-priority",
                validate_gpu_priority(args.gpu_priority, gpu_count),
                current_priority,
            )
    except ValueError as exc:
        raise BookingError(str(exc)) from exc

    if mismatches:
        flags = ", ".join(sorted(set(mismatches)))
        raise BookingError(
            "a managed installation already exists and install will not rewrite its "
            f"configuration; different option(s): {flags}. Use 'bk admin gpu-policy' "
            "for GPU policy, 'bk admin transfer' for the service account, or a reviewed "
            "migration for other settings"
        )


def _run_systemctl(executable: str, *arguments: str) -> None:
    result = subprocess.run(
        [executable, *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise BookingError(f"systemctl {' '.join(arguments)} failed{suffix}")


def _run_admin_gpu_policy(args: argparse.Namespace) -> int:
    config_file = _absolute_path(args.config_file)
    mutation_requested = any(
        (
            args.disabled_gpus is not None,
            args.enable_all,
            args.gpu_priority is not None,
            args.clear_priority,
            args.require_shared_memory is not None,
            args.booking_horizon_days is not None,
            args.blackout is not None,
            args.clear_blackouts,
        )
    )
    if args.recover:
        if mutation_requested:
            raise BookingError("--recover cannot be combined with policy changes")
        inspection = inspect_admin_gpu_policy_recovery(config_file)
        if not args.json:
            _print_gpu_policy_recovery(inspection)
        if args.dry_run:
            if args.json:
                print(json.dumps(inspection, ensure_ascii=False, sort_keys=True))
            return 0
        if inspection["blockers"]:
            if args.json:
                print(json.dumps(inspection, ensure_ascii=False, sort_keys=True))
            else:
                print(f"bk: {'; '.join(inspection['blockers'])}", file=sys.stderr)
            return 2
        if not args.yes:
            if args.json:
                print(json.dumps(inspection, ensure_ascii=False, sort_keys=True))
            print("bk: pass --yes to recover this configuration update", file=sys.stderr)
            return 1
        result = recover_admin_gpu_policy(config_file)
        if args.json:
            print(
                json.dumps(
                    {**result, "inspection": inspection},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print("recovered: restored the prior trusted configuration and manifest")
            _print_gpu_policy_restart_hint()
        return 0

    disabled_value: object = () if args.enable_all else args.disabled_gpus
    priority_value: object = () if args.clear_priority else args.gpu_priority
    blackout_value: object = () if args.clear_blackouts else args.blackout
    plan = inspect_admin_gpu_policy(
        config_file,
        disabled_gpus=disabled_value,
        gpu_priority=priority_value,
        require_shared_memory=args.require_shared_memory,
        booking_horizon_days=args.booking_horizon_days,
        booking_blackouts=blackout_value,
    )
    status = "blocked" if plan.blockers else ("planned" if plan.changed else "unchanged")
    public_plan = plan.public_document(status="dry-run" if args.dry_run else status)
    if not args.json:
        _print_gpu_policy_plan(plan)
    if not plan.changed or args.dry_run:
        if args.json:
            print(json.dumps(public_plan, ensure_ascii=False, sort_keys=True))
        return 0
    if plan.blockers:
        if args.json:
            print(json.dumps(public_plan, ensure_ascii=False, sort_keys=True))
        else:
            print(f"bk: {'; '.join(plan.blockers)}", file=sys.stderr)
        return 2
    if not args.yes:
        if args.json:
            print(json.dumps(public_plan, ensure_ascii=False, sort_keys=True))
        print("bk: pass --yes to apply this scheduling policy", file=sys.stderr)
        return 1
    result = apply_admin_gpu_policy(plan)
    if args.json:
        print(
            json.dumps(
                {**result, "inspection": public_plan},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(f"updated scheduling policy: {plan.config_file}")
        _print_gpu_policy_restart_hint()
    return 0


def _print_gpu_policy_plan(plan: AdminGpuPolicyPlan) -> None:
    current_disabled = ",".join(map(str, plan.current_disabled_gpus)) or "none"
    desired_disabled = ",".join(map(str, plan.desired_disabled_gpus)) or "none"
    current_priority = ",".join(
        f"{gpu}={priority}" for gpu, priority in plan.current_gpu_priority
    ) or "equal"
    desired_priority = ",".join(
        f"{gpu}={priority}" for gpu, priority in plan.desired_gpu_priority
    ) or "equal"
    print("GPUBK scheduling policy")
    print(f"  config:            {plan.config_file}")
    print(f"  disabled current:  {current_disabled}")
    print(f"  disabled desired:  {desired_disabled}")
    print(f"  priority current:  {current_priority}")
    print(f"  priority desired:  {desired_priority}")
    print(
        "  shared VRAM:       "
        f"{'required' if plan.current_require_shared_memory else 'automatic'} -> "
        f"{'required' if plan.desired_require_shared_memory else 'automatic'}"
    )
    print(
        "  booking horizon:   "
        f"{plan.current_booking_horizon_days}d -> "
        f"{plan.desired_booking_horizon_days}d"
    )
    print(
        "  blackout windows:  "
        f"{len(plan.current_booking_blackouts)} -> "
        f"{len(plan.desired_booking_blackouts)}"
    )
    for start, end, reason in plan.desired_booking_blackouts:
        print(f"    blocked:          {start} -> {end}  {reason}")
    print(f"  change:            {'yes' if plan.changed else 'no'}")
    for blocker in plan.blockers:
        print(f"  blocked:           {blocker}")
    if plan.changed and plan.blockers:
        print(
            "  stop first:        sudo systemctl stop "
            "gpubk-broker.service gpubk-monitor.service"
        )


def _print_gpu_policy_recovery(inspection: dict) -> None:
    print("GPUBK interrupted configuration update")
    print(f"  config:  {inspection['config_file']}")
    print(f"  status:  {inspection['status']}")
    for blocker in inspection["blockers"]:
        print(f"  blocked: {blocker}")


def _print_gpu_policy_restart_hint() -> None:
    print("next: restart the broker and monitor")
    print(
        "  systemd: sudo systemctl start "
        "gpubk-broker.service gpubk-monitor.service"
    )


def _run_admin_services(args: argparse.Namespace) -> int:
    config_file = _absolute_path(args.config_file)
    operation = args.service_action
    if operation == "status":
        _, inspection = inspect_admin_system_services(
            config_file,
            operation="status",
            expected_owner=0,
        )
        _print_admin_services_inspection(inspection, json_output=args.json)
        return 0 if not inspection["blockers"] else 1

    service_plan, inspection = inspect_admin_system_services(
        config_file,
        operation=operation,
        python_executable=getattr(args, "python_executable", None),
        force=bool(getattr(args, "force", False)),
        expected_owner=0,
    )
    if not args.json:
        _print_admin_services_inspection(inspection, json_output=False)
    if args.dry_run:
        if args.json:
            print(json.dumps(inspection, ensure_ascii=False, sort_keys=True))
        return 0 if not inspection["blockers"] else 1
    if inspection["blockers"]:
        raise BookingError("; ".join(inspection["blockers"]))
    if args.json and not args.yes:
        print(json.dumps(inspection, ensure_ascii=False, sort_keys=True))
    prompt = (
        "Install tracked GPUBK system services? [y/N]: "
        if operation == "install"
        else "Restore or remove tracked GPUBK system services? [y/N]: "
    )
    message = f"pass --yes to apply this system service {operation} plan"
    if not _confirm_admin_action(args, prompt, message):
        return 1
    if operation == "install":
        result = apply_admin_system_services_install(
            config_file,
            service_plan=service_plan,
        )
    else:
        result = apply_admin_system_services_uninstall(
            config_file,
            service_plan=service_plan,
        )
    if args.json:
        print(
            json.dumps(
                {"status": result["status"], "inspection": inspection, "result": result},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif operation == "install":
        print("installed: gpubk-broker.service and gpubk-monitor.service")
        print("next: sudo systemctl daemon-reload")
        print(
            "next: sudo systemctl enable --now "
            "gpubk-broker.service gpubk-monitor.service"
        )
        print("verify: bk doctor --probe --require-monitor --strict")
    else:
        print("removed: tracked GPUBK system service files")
        print("next: sudo systemctl daemon-reload")
    return 0


def apply_admin_command_link_install(
    config_file: Path,
    *,
    destination: Path = DEFAULT_COMMAND_LINK,
    target: Path,
    require_root: bool = True,
) -> dict:
    if require_root and os.geteuid() != 0:
        raise BookingError(
            "administrator command-link installation must run as root"
        )
    expected_owner = 0 if require_root else os.geteuid()
    config_file = _absolute_path(config_file)
    manifest_path = _manifest_path(config_file)
    manifest = _read_manifest(manifest_path, expected_owner=expected_owner)
    plan = plan_command_link_install(
        existing=manifest.get("command_link"),
        destination=destination,
        target=target,
        expected_owner=expected_owner,
    )
    if plan.blockers:
        raise BookingError("; ".join(plan.blockers))
    pending = {**manifest, "command_link": plan.document}
    _write_manifest(manifest_path, pending, replace=True)
    finalized = apply_command_link_install(
        plan.document,
        expected_owner=expected_owner,
    )
    _write_manifest(
        manifest_path,
        {**pending, "command_link": finalized},
        replace=True,
    )
    return {
        "status": "installed",
        "destination": finalized["destination"],
        "target": finalized["target"],
        "owned": finalized["owned"],
    }


def inspect_admin_system_services(
    config_file: Path,
    *,
    operation: str,
    python_executable: Optional[Path] = None,
    unit_directory: Path = DEFAULT_SYSTEM_UNIT_DIR,
    force: bool = False,
    expected_owner: int = 0,
    allow_running: bool = False,
) -> tuple[Optional[SystemServicesPlan], dict]:
    if operation not in {"install", "status", "uninstall"}:
        raise BookingError(f"unknown system service operation: {operation}")
    config_file = _absolute_path(config_file)
    manifest, document = _load_admin_services_manifest(
        config_file,
        expected_owner=expected_owner,
    )
    data_dir = _manifest_absolute_path(manifest, "data_dir")
    broker_socket = _manifest_absolute_path(manifest, "broker_socket")
    service_uid = _manifest_nonnegative_int(manifest, "service_uid")
    service_gid = _manifest_nonnegative_int(manifest, "service_gid")
    existing = manifest.get("system_services")

    if operation == "status":
        if existing is None:
            return None, {
                "schema_version": ADMIN_SCHEMA_VERSION,
                "kind": "admin-system-services",
                "operation": "status",
                "status": "not-installed",
                "config_file": str(config_file),
                "units": {},
                "enabled_links": [],
                "blockers": [],
            }
        services = _validated_manifest_system_services(
            manifest,
            document,
            expected_owner=expected_owner,
            require_installed=False,
        )
        statuses, blockers = inspect_system_service_files(services)
        links = enabled_unit_links(services)
        return None, {
            "schema_version": ADMIN_SCHEMA_VERSION,
            "kind": "admin-system-services",
            "operation": "status",
            "status": "blocked" if blockers else services["phase"],
            "config_file": str(config_file),
            "unit_directory": services["unit_directory"],
            "python_executable": services["python_executable"],
            "service_uid": services["service_uid"],
            "service_gid": services["service_gid"],
            "units": statuses,
            "enabled_links": [str(path) for path in links],
            "blockers": blockers,
        }

    if operation == "install":
        service_plan = plan_system_services_install(
            existing=existing,
            config_file=config_file,
            data_dir=data_dir,
            socket_directory=broker_socket.parent,
            service_uid=service_uid,
            service_gid=service_gid,
            unit_directory=unit_directory,
            python_executable=python_executable,
            expected_owner=expected_owner,
            force=force,
        )
    else:
        if existing is None:
            raise BookingError("tracked system services are not installed")
        _validated_manifest_system_services(
            manifest,
            document,
            expected_owner=expected_owner,
            require_installed=False,
        )
        service_plan = plan_system_services_uninstall(existing)

    inspection = service_plan.public_document()
    blockers = list(inspection["blockers"])
    socket_state = _broker_socket_state(broker_socket, service_uid=service_uid)
    allowed_owner_pairs = {(service_uid, service_gid)}
    usage_lock = _probe_admin_lock(data_dir / "usage.lock", allowed_owner_pairs)
    ledger_lock = _probe_admin_lock(data_dir / "ledger.lock", allowed_owner_pairs)
    running_install = operation == "install" and allow_running
    if socket_state == "active" and not running_install:
        blockers.append("broker is running; stop it before changing system services")
    if usage_lock == "active" and not running_install:
        blockers.append("monitor is running; stop it before changing system services")
    if operation == "uninstall" and ledger_lock == "active":
        blockers.append("a ledger transaction is active; retry after it finishes")
    links = enabled_unit_links(service_plan.document)
    if operation == "uninstall" and links:
        blockers.append(
            "system services are still enabled; run: sudo systemctl disable --now "
            + " ".join(system_unit_names())
        )
    inspection.update(
        {
            "config_file": str(config_file),
            "socket_state": socket_state,
            "usage_lock": usage_lock,
            "ledger_lock": ledger_lock,
            "enabled_links": [str(path) for path in links],
            "blockers": blockers,
            "status": "blocked" if blockers else "ready",
        }
    )
    return service_plan, inspection


def apply_admin_system_services_install(
    config_file: Path,
    *,
    service_plan: Optional[SystemServicesPlan] = None,
    python_executable: Optional[Path] = None,
    unit_directory: Path = DEFAULT_SYSTEM_UNIT_DIR,
    force: bool = False,
    require_root: bool = True,
) -> dict:
    if require_root and os.geteuid() != 0:
        raise BookingError(
            "system service installation must run as root; use sudo bk admin services install"
        )
    expected_owner = 0 if require_root else os.geteuid()
    config_file = _absolute_path(config_file)
    if service_plan is None:
        service_plan, inspection = inspect_admin_system_services(
            config_file,
            operation="install",
            python_executable=python_executable,
            unit_directory=unit_directory,
            force=force,
            expected_owner=expected_owner,
        )
        if inspection["blockers"]:
            raise BookingError("; ".join(inspection["blockers"]))
    if service_plan is None:
        raise BookingError("system service installation plan is missing")
    manifest_path = _manifest_path(config_file)
    manifest = _read_manifest(manifest_path, expected_owner=expected_owner)
    pending_manifest = {**manifest, "system_services": service_plan.document}
    _write_manifest(manifest_path, pending_manifest, replace=True)
    finalized = apply_system_services_install(
        service_plan.document,
        expected_owner=expected_owner,
    )
    _write_manifest(
        manifest_path,
        {**pending_manifest, "system_services": finalized},
        replace=True,
    )
    return {
        "status": "installed",
        "unit_directory": finalized["unit_directory"],
        "units": list(system_unit_names()),
        "service_uid": finalized["service_uid"],
        "service_gid": finalized["service_gid"],
        "enabled": False,
    }


def apply_admin_system_services_uninstall(
    config_file: Path,
    *,
    service_plan: Optional[SystemServicesPlan] = None,
    require_root: bool = True,
) -> dict:
    if require_root and os.geteuid() != 0:
        raise BookingError(
            "system service removal must run as root; use sudo bk admin services uninstall"
        )
    expected_owner = 0 if require_root else os.geteuid()
    config_file = _absolute_path(config_file)
    if service_plan is None:
        service_plan, inspection = inspect_admin_system_services(
            config_file,
            operation="uninstall",
            expected_owner=expected_owner,
        )
        if inspection["blockers"]:
            raise BookingError("; ".join(inspection["blockers"]))
    if service_plan is None:
        raise BookingError("system service removal plan is missing")
    manifest_path = _manifest_path(config_file)
    manifest = _read_manifest(manifest_path, expected_owner=expected_owner)
    pending_manifest = {**manifest, "system_services": service_plan.document}
    _write_manifest(manifest_path, pending_manifest, replace=True)
    apply_system_services_uninstall(
        service_plan.document,
        expected_owner=expected_owner,
    )
    final_manifest = dict(pending_manifest)
    final_manifest.pop("system_services", None)
    _write_manifest(manifest_path, final_manifest, replace=True)
    return {
        "status": "uninstalled",
        "unit_directory": service_plan.document["unit_directory"],
        "units": list(system_unit_names()),
        "restored_preexisting_units": any(
            service_plan.document["files"][name]["before"]["exists"]
            for name in system_unit_names()
        ),
    }


def _load_admin_services_manifest(
    config_file: Path,
    *,
    expected_owner: int,
) -> tuple[dict, dict]:
    _reject_pending_config_update(config_file)
    if os.path.lexists(_transfer_journal_path(config_file)):
        raise BookingError(
            "an interrupted service-account transfer requires recovery before changing services"
        )
    manifest = _read_manifest(
        _manifest_path(config_file),
        expected_owner=expected_owner,
    )
    if manifest.get("admin_uid") != expected_owner:
        raise BookingError("install manifest administrator UID does not match")
    if manifest.get("config_file") != str(config_file):
        raise BookingError("install manifest belongs to another configuration")
    payload = _read_owned_regular_payload(
        config_file,
        expected_owner,
        CONFIG_FILE_MODE,
    )
    if _sha256(payload) != manifest.get("config_sha256"):
        raise BookingError("trusted configuration does not match the install manifest")
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BookingError(f"trusted configuration is invalid JSON: {config_file}") from exc
    if not isinstance(document, dict):
        raise BookingError("trusted configuration must contain a JSON object")
    data_dir = _manifest_absolute_path(manifest, "data_dir")
    broker_socket = _manifest_absolute_path(manifest, "broker_socket")
    service_uid = _manifest_nonnegative_int(manifest, "service_uid")
    if document.get("data_dir") != str(data_dir):
        raise BookingError("trusted configuration data_dir does not match the manifest")
    if document.get("broker_socket") != str(broker_socket):
        raise BookingError("trusted broker socket does not match the manifest")
    if document.get("broker_uid") != service_uid or document.get("monitor_uid") != service_uid:
        raise BookingError("trusted service UID does not match the install manifest")
    return manifest, document


def _validated_manifest_system_services(
    manifest: dict,
    config_document: dict,
    *,
    expected_owner: int,
    require_installed: bool,
) -> dict:
    services = validate_system_services_document(manifest.get("system_services"))
    expected = {
        "config_file": manifest["config_file"],
        "data_dir": manifest["data_dir"],
        "socket_directory": str(Path(manifest["broker_socket"]).parent),
        "service_uid": manifest["service_uid"],
        "service_gid": manifest["service_gid"],
    }
    for key, value in expected.items():
        if services[key] != value:
            raise BookingError(f"tracked system service {key} does not match the install manifest")
    if config_document.get("broker_uid") != services["service_uid"]:
        raise BookingError("tracked system service UID does not match trusted configuration")
    if require_installed and services["phase"] != PHASE_INSTALLED:
        raise BookingError(
            "system service lifecycle is incomplete; finish it before this operation"
        )
    directory = Path(services["unit_directory"])
    metadata = directory.lstat()
    if metadata.st_uid != expected_owner:
        raise BookingError("system unit directory ownership drifted")
    return services


def _print_admin_services_inspection(inspection: dict, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(inspection, ensure_ascii=False, sort_keys=True))
        return
    print("GPUBK system services")
    print(f"  operation:  {inspection['operation']}")
    print(f"  status:     {inspection['status']}")
    if inspection.get("unit_directory"):
        print(f"  directory:  {inspection['unit_directory']}")
    if inspection.get("python_executable"):
        print(f"  python:     {inspection['python_executable']}")
    for name, status in inspection.get("units", {}).items():
        print(f"  unit:       {name} ({status})")
    for blocker in inspection.get("blockers", []):
        print(f"  blocked:    {blocker}")


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
        print(_python_uninstall_hint())
    return 0


def _python_uninstall_hint() -> str:
    prefix = Path(sys.prefix).resolve()
    base_prefix = Path(getattr(sys, "base_prefix", sys.prefix)).resolve()
    executable = Path(sys.executable).resolve()
    if prefix != base_prefix:
        return (
            "next: remove the isolated Python environment with "
            f"'sudo rm -rf {shlex.quote(str(prefix))}'"
        )
    return (
        "next: uninstall the Python package from this interpreter with "
        f"'{shlex.quote(str(executable))} -m pip uninstall gpubk'"
    )


def _run_admin_transfer(args: argparse.Namespace) -> int:
    config_file = _absolute_path(args.config_file)
    if args.recover:
        if args.service_user is not None:
            raise BookingError("a recovery does not accept a target service account")
        inspection = inspect_admin_transfer_recovery(config_file)
        if not args.json:
            _print_transfer_inspection(inspection)
        if args.dry_run:
            if args.json:
                print(json.dumps(inspection, ensure_ascii=False, sort_keys=True))
            return 0
        if args.json and not args.yes:
            print(json.dumps(inspection, ensure_ascii=False, sort_keys=True))
        if not _confirm_admin_action(
            args,
            "Recover the interrupted service-account transfer? [y/N]: ",
            "pass --yes to recover the interrupted transfer",
        ):
            return 1
        result = recover_admin_transfer(config_file)
    else:
        if args.service_user is None:
            raise BookingError(
                "target service account is required; use: bk admin transfer USER"
            )
        target = _resolve_identity(args.service_user)
        _, inspection = inspect_admin_transfer(config_file, target)
        if not args.json:
            _print_transfer_inspection(inspection)
        if args.dry_run or inspection["status"] == "unchanged":
            if args.json:
                print(json.dumps(inspection, ensure_ascii=False, sort_keys=True))
            return 0
        if inspection["blockers"]:
            raise BookingError("; ".join(inspection["blockers"]))
        if args.json and not args.yes:
            print(json.dumps(inspection, ensure_ascii=False, sort_keys=True))
        if not _confirm_admin_action(
            args,
            f"Transfer GPUBK to {target.username} ({target.uid})? [y/N]: ",
            "pass --yes to apply this service-account transfer",
        ):
            return 1
        result = apply_admin_transfer(config_file, target)

    if args.json:
        print(
            json.dumps(
                {"status": result["status"], "inspection": inspection, "result": result},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        if args.recover:
            print("recovered: GPUBK ownership and configuration returned to the prior account")
        else:
            print(
                f"transferred: GPUBK broker and monitor ownership now belongs to "
                f"{result['service_username']} (UID {result['service_uid']})"
            )
            print("preserved: reservations, user UIDs, audit logs, and usage history")
        if result.get("system_services_updated"):
            print("next: sudo systemctl daemon-reload")
            print(
                "next: sudo systemctl start "
                "gpubk-broker.service gpubk-monitor.service"
            )
        else:
            print(
                f"next: start 'bk broker' as {result['service_username']}, then run "
                "'bk doctor --probe --strict'"
            )
    return 0


def _confirm_admin_action(
    args: argparse.Namespace,
    prompt: str,
    noninteractive_message: str,
) -> bool:
    if args.yes:
        return True
    if args.json or not sys.stdin.isatty():
        print(f"bk: {noninteractive_message}", file=sys.stderr)
        return False
    answer = input(prompt).strip().lower()
    if answer in {"y", "yes"}:
        return True
    print("No changes made.")
    return False


def _print_transfer_inspection(inspection: dict) -> None:
    print("GPUBK service-account transfer")
    print(
        f"  from:       {inspection['from']['username']} "
        f"({inspection['from']['uid']}:{inspection['from']['gid']})"
    )
    print(
        f"  to:         {inspection['to']['username']} "
        f"({inspection['to']['uid']}:{inspection['to']['gid']})"
    )
    print(f"  data:       {inspection['data_dir']}")
    print(f"  config:     {inspection['config_file']}")
    print(f"  status:     {inspection['status']}")
    for action in inspection["actions"]:
        print(f"  action:     {action}")
    for blocker in inspection["blockers"]:
        print(f"  blocked:    {blocker}")


def inspect_admin_gpu_policy(
    config_file: Path,
    *,
    disabled_gpus: object = None,
    gpu_priority: object = None,
    require_shared_memory: Optional[bool] = None,
    booking_horizon_days: Optional[int] = None,
    booking_blackouts: object = None,
    expected_owner: int = 0,
) -> AdminGpuPolicyPlan:
    config_file = _absolute_path(config_file)
    manifest, document = _load_admin_services_manifest(
        config_file,
        expected_owner=expected_owner,
    )
    gpu_count = document.get("gpu_count")
    if isinstance(gpu_count, bool) or not isinstance(gpu_count, int):
        raise BookingError("trusted configuration gpu_count is invalid")
    current_require_shared_memory = document.get("require_shared_memory", False)
    if not isinstance(current_require_shared_memory, bool):
        raise BookingError("trusted configuration require_shared_memory is invalid")
    if require_shared_memory is not None and not isinstance(require_shared_memory, bool):
        raise BookingError("shared memory policy must be a boolean")
    desired_require_shared_memory = (
        current_require_shared_memory
        if require_shared_memory is None
        else require_shared_memory
    )
    current_booking_horizon_days = document.get("booking_horizon_days", 30)
    if (
        isinstance(current_booking_horizon_days, bool)
        or not isinstance(current_booking_horizon_days, int)
        or not 1 <= current_booking_horizon_days <= MAX_BOOKING_HORIZON_DAYS
    ):
        raise BookingError("trusted configuration booking_horizon_days is invalid")
    if booking_horizon_days is not None and (
        isinstance(booking_horizon_days, bool)
        or not isinstance(booking_horizon_days, int)
        or not 1 <= booking_horizon_days <= MAX_BOOKING_HORIZON_DAYS
    ):
        raise BookingError(
            f"booking horizon must be between 1 and {MAX_BOOKING_HORIZON_DAYS} days"
        )
    desired_booking_horizon_days = (
        current_booking_horizon_days
        if booking_horizon_days is None
        else booking_horizon_days
    )
    try:
        current_booking_blackouts = validate_booking_blackouts(
            document.get("booking_blackouts")
        )
        desired_booking_blackouts = (
            current_booking_blackouts
            if booking_blackouts is None
            else validate_booking_blackouts(booking_blackouts)
        )
        current_disabled = validate_gpu_list(
            document.get("disabled_gpus"),
            gpu_count,
            "disabled_gpus",
        )
        current_priority = validate_gpu_priority(
            document.get("gpu_priority"),
            gpu_count,
        )
        desired_disabled = (
            current_disabled
            if disabled_gpus is None
            else validate_gpu_list(disabled_gpus, gpu_count, "disabled_gpus")
        )
        desired_priority = (
            current_priority
            if gpu_priority is None
            else validate_gpu_priority(gpu_priority, gpu_count)
        )
    except ValueError as exc:
        raise BookingError(str(exc)) from exc

    desired_document = dict(document)
    if desired_disabled:
        desired_document["disabled_gpus"] = list(desired_disabled)
    else:
        desired_document.pop("disabled_gpus", None)
    if desired_priority:
        desired_document["gpu_priority"] = {
            str(gpu): priority for gpu, priority in desired_priority
        }
    else:
        desired_document.pop("gpu_priority", None)
    if require_shared_memory is not None:
        desired_document["require_shared_memory"] = desired_require_shared_memory
    if booking_horizon_days is not None:
        desired_document["booking_horizon_days"] = desired_booking_horizon_days
    if booking_blackouts is not None:
        if desired_booking_blackouts:
            desired_document["booking_blackouts"] = _public_blackouts(
                desired_booking_blackouts
            )
        else:
            desired_document.pop("booking_blackouts", None)

    data_dir = _manifest_absolute_path(manifest, "data_dir")
    broker_socket = _manifest_absolute_path(manifest, "broker_socket")
    service_uid = _manifest_nonnegative_int(manifest, "service_uid")
    service_gid = _manifest_nonnegative_int(manifest, "service_gid")
    service = AdminIdentity(
        service_uid,
        _username_for_uid(service_uid),
        service_gid,
    )
    owner_pair = {(service_uid, service_gid)}
    _validate_transfer_directory(
        data_dir,
        owner_pair,
        BROKER_DIR_MODE,
        "data",
    )
    _validate_transfer_directory(
        broker_socket.parent,
        owner_pair,
        BROKER_SOCKET_DIRECTORY_MODE,
        "broker socket",
    )
    blockers = _admin_service_blockers(
        data_dir,
        broker_socket,
        owner_pair=owner_pair,
        socket_owner_uids={expected_owner, service_uid},
    )
    return AdminGpuPolicyPlan(
        config_file=config_file,
        data_dir=data_dir,
        broker_socket=broker_socket,
        service=service,
        current_disabled_gpus=current_disabled,
        desired_disabled_gpus=desired_disabled,
        current_gpu_priority=current_priority,
        desired_gpu_priority=desired_priority,
        current_require_shared_memory=current_require_shared_memory,
        desired_require_shared_memory=desired_require_shared_memory,
        require_shared_memory_update=require_shared_memory,
        current_booking_horizon_days=current_booking_horizon_days,
        desired_booking_horizon_days=desired_booking_horizon_days,
        booking_horizon_days_update=booking_horizon_days,
        current_booking_blackouts=current_booking_blackouts,
        desired_booking_blackouts=desired_booking_blackouts,
        booking_blackouts_update=(
            desired_booking_blackouts if booking_blackouts is not None else None
        ),
        current_document=document,
        desired_document=desired_document,
        blockers=tuple(blockers),
    )


def apply_admin_gpu_policy(
    plan: AdminGpuPolicyPlan,
    *,
    require_root: bool = True,
) -> dict:
    if require_root and os.geteuid() != 0:
        raise BookingError(
            "GPU policy updates must run as root; use sudo bk admin gpu-policy"
        )
    expected_owner = 0 if require_root else os.geteuid()
    fresh = inspect_admin_gpu_policy(
        plan.config_file,
        disabled_gpus=plan.desired_disabled_gpus,
        gpu_priority=plan.desired_gpu_priority,
        require_shared_memory=plan.require_shared_memory_update,
        booking_horizon_days=plan.booking_horizon_days_update,
        booking_blackouts=plan.booking_blackouts_update,
        expected_owner=expected_owner,
    )
    if fresh.current_document != plan.current_document:
        raise BookingError("trusted configuration changed after review; inspect it again")
    if fresh.desired_document != plan.desired_document:
        raise BookingError("GPU policy plan changed after review; inspect it again")
    if fresh.blockers:
        raise BookingError("; ".join(fresh.blockers))
    if not fresh.changed:
        return {
            "schema_version": ADMIN_SCHEMA_VERSION,
            "kind": "admin-gpu-policy",
            "status": "unchanged",
            "config_file": str(plan.config_file),
        }

    owner_pair = (fresh.service.uid, fresh.service.primary_gid)
    manifest_path = _manifest_path(fresh.config_file)
    journal_path = _config_update_journal_path(fresh.config_file)
    with _admin_service_guard(
        fresh.data_dir,
        fresh.broker_socket,
        allowed_owner_pairs={owner_pair},
        lock_owner=owner_pair,
        socket_owner_uids={fresh.service.uid},
        operation="GPU policy update",
    ):
        manifest = _read_manifest(manifest_path, expected_owner=expected_owner)
        config_payload = _read_owned_regular_payload(
            fresh.config_file,
            expected_owner,
            CONFIG_FILE_MODE,
        )
        if config_payload != _config_payload(fresh.current_document):
            raise BookingError("trusted configuration changed while acquiring maintenance locks")
        if manifest.get("config_sha256") != _sha256(config_payload):
            raise BookingError("trusted configuration no longer matches the install manifest")

        desired_payload = _config_payload(fresh.desired_document)
        journal = _build_config_update_journal(
            fresh,
            config_payload=config_payload,
            manifest_payload=_read_owned_regular_payload(
                manifest_path,
                expected_owner,
                INSTALL_MANIFEST_MODE,
            ),
            expected_owner=expected_owner,
        )
        history = manifest.get("config_updates", [])
        if not isinstance(history, list):
            raise BookingError("install manifest configuration history is invalid")
        updated_manifest = {
            **manifest,
            "config_sha256": _sha256(desired_payload),
            "previous_config_sha256": None,
            "config_updates": [
                *history[-31:],
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "fields": [
                        "disabled_gpus",
                        "gpu_priority",
                        *(
                            ["require_shared_memory"]
                            if fresh.require_shared_memory_update is not None
                            else []
                        ),
                        *(
                            ["booking_horizon_days"]
                            if fresh.booking_horizon_days_update is not None
                            else []
                        ),
                        *(
                            ["booking_blackouts"]
                            if fresh.booking_blackouts_update is not None
                            else []
                        ),
                    ],
                    "from_sha256": _sha256(config_payload),
                    "to_sha256": _sha256(desired_payload),
                },
            ],
        }
        _write_config_update_journal(journal_path, journal)
        try:
            _write_new_file(
                fresh.config_file,
                desired_payload,
                CONFIG_FILE_MODE,
                replace=True,
            )
            _write_manifest(manifest_path, updated_manifest, replace=True)
            written_config = _read_owned_regular_payload(
                fresh.config_file,
                expected_owner,
                CONFIG_FILE_MODE,
            )
            written_manifest = _read_manifest(
                manifest_path,
                expected_owner=expected_owner,
            )
            if written_config != desired_payload:
                raise BookingError("GPU policy configuration verification failed")
            if written_manifest.get("config_sha256") != _sha256(written_config):
                raise BookingError("GPU policy manifest verification failed")
        except BaseException as exc:
            rollback_errors = _restore_config_update_snapshots(
                fresh.config_file,
                journal,
            )
            if rollback_errors:
                raise BookingError(
                    "GPU policy update failed and automatic rollback was incomplete "
                    f"({'; '.join(rollback_errors)}); run gpu-policy --recover"
                ) from exc
            journal_path.unlink()
            fsync_directory(journal_path.parent)
            raise
        journal_path.unlink()
        fsync_directory(journal_path.parent)

    return {
        "schema_version": ADMIN_SCHEMA_VERSION,
        "kind": "admin-gpu-policy",
        "status": "updated",
        "config_file": str(fresh.config_file),
        "disabled_gpus": list(fresh.desired_disabled_gpus),
        "gpu_priority": {
            str(gpu): priority for gpu, priority in fresh.desired_gpu_priority
        },
        "require_shared_memory": fresh.desired_require_shared_memory,
        "booking_horizon_days": fresh.desired_booking_horizon_days,
        "booking_blackouts": _public_blackouts(fresh.desired_booking_blackouts),
    }


def inspect_admin_gpu_policy_recovery(
    config_file: Path,
    *,
    expected_owner: int = 0,
) -> dict:
    config_file = _absolute_path(config_file)
    journal = _read_config_update_journal(
        _config_update_journal_path(config_file),
        config_file=config_file,
        expected_owner=expected_owner,
    )
    service_uid = int(journal["service_uid"])
    service_gid = int(journal["service_gid"])
    data_dir = _absolute_path(Path(journal["data_dir"]))
    broker_socket = _absolute_path(Path(journal["broker_socket"]))
    owner_pair = {(service_uid, service_gid)}
    _validate_transfer_directory(
        data_dir,
        owner_pair,
        BROKER_DIR_MODE,
        "data",
    )
    _validate_transfer_directory(
        broker_socket.parent,
        owner_pair,
        BROKER_SOCKET_DIRECTORY_MODE,
        "broker socket",
    )
    blockers = _admin_service_blockers(
        data_dir,
        broker_socket,
        owner_pair=owner_pair,
        socket_owner_uids={expected_owner, service_uid},
    )
    return {
        "schema_version": ADMIN_SCHEMA_VERSION,
        "kind": "admin-gpu-policy-recovery",
        "status": "blocked" if blockers else "ready",
        "config_file": str(config_file),
        "journal": str(_config_update_journal_path(config_file)),
        "blockers": blockers,
    }


def recover_admin_gpu_policy(
    config_file: Path,
    *,
    require_root: bool = True,
) -> dict:
    if require_root and os.geteuid() != 0:
        raise BookingError(
            "configuration recovery must run as root; use sudo bk admin gpu-policy --recover"
        )
    expected_owner = 0 if require_root else os.geteuid()
    inspection = inspect_admin_gpu_policy_recovery(
        config_file,
        expected_owner=expected_owner,
    )
    if inspection["blockers"]:
        raise BookingError("; ".join(inspection["blockers"]))
    config_file = _absolute_path(config_file)
    journal_path = _config_update_journal_path(config_file)
    journal = _read_config_update_journal(
        journal_path,
        config_file=config_file,
        expected_owner=expected_owner,
    )
    owner_pair = (int(journal["service_uid"]), int(journal["service_gid"]))
    with _admin_service_guard(
        _absolute_path(Path(journal["data_dir"])),
        _absolute_path(Path(journal["broker_socket"])),
        allowed_owner_pairs={owner_pair},
        lock_owner=owner_pair,
        socket_owner_uids={expected_owner, owner_pair[0]},
        operation="configuration recovery",
    ):
        errors = _restore_config_update_snapshots(config_file, journal)
        if errors:
            raise BookingError(
                "configuration recovery is incomplete: " + "; ".join(errors)
            )
        journal_path.unlink()
        fsync_directory(journal_path.parent)
    return {
        "schema_version": ADMIN_SCHEMA_VERSION,
        "kind": "admin-gpu-policy-recovery",
        "status": "recovered",
        "config_file": str(config_file),
    }


def inspect_admin_uninstall(
    config_file: Path,
    *,
    purge_data: bool,
    expected_owner: int = 0,
    login_hook_path: Optional[Path] = None,
) -> dict:
    from .login_hook import DEFAULT_LOGIN_HOOK_PATH, inspect_login_hook

    config_file = _absolute_path(config_file)
    login_hook_path = login_hook_path or DEFAULT_LOGIN_HOOK_PATH
    _reject_pending_config_update(config_file)
    journal_path = _transfer_journal_path(config_file)
    if os.path.lexists(journal_path):
        raise BookingError(
            "an interrupted service-account transfer must be recovered before "
            "uninstalling; run: "
            f"sudo bk admin transfer --recover --config-file {config_file}"
        )
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
    system_services_present = manifest.get("system_services") is not None
    if system_services_present:
        if current_config is None:
            raise BookingError("trusted configuration is missing while system services are tracked")
        try:
            current_document = json.loads(current_config)
        except json.JSONDecodeError as exc:
            raise BookingError("trusted configuration is invalid JSON") from exc
        _validated_manifest_system_services(
            manifest,
            current_document,
            expected_owner=expected_owner,
            require_installed=False,
        )
    command_link_document = manifest.get("command_link")
    command_link = None
    if command_link_document is not None:
        command_link = inspect_command_link(
            command_link_document,
            expected_owner=expected_owner,
            allow_absent=True,
            require_target=False,
        )

    data_dir = _manifest_absolute_path(manifest, "data_dir")
    broker_socket = _manifest_absolute_path(manifest, "broker_socket")
    service_uid = _manifest_nonnegative_int(manifest, "service_uid")
    service_gid = _manifest_nonnegative_int(manifest, "service_gid")
    data_nonempty = False
    usage_lock = "absent"
    ledger_lock = "absent"
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
        allowed_owner_pairs = {(service_uid, service_gid)}
        usage_lock = _probe_admin_lock(
            data_dir / "usage.lock",
            allowed_owner_pairs,
        )
        ledger_lock = _probe_admin_lock(
            data_dir / "ledger.lock",
            allowed_owner_pairs,
        )
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
        cluster_catalog = _validated_managed_cluster_catalog(
            config_file.parent / CLUSTER_CATALOG_NAME,
            expected_owner=expected_owner,
        )
        if cluster_catalog is not None:
            allowed.add(cluster_catalog.name)
        _validate_directory_entries(config_file.parent, allowed, "configuration")
    else:
        cluster_catalog = None
    if not socket_directory_before["exists"]:
        _validate_directory_entries(
            broker_socket.parent, {broker_socket.name}, "broker socket"
        )

    blockers = []
    if system_services_present:
        blockers.append(
            "tracked system services must be removed first; run: "
            "sudo bk admin services uninstall --yes"
        )
    if socket_state == "active":
        blockers.append("broker is running; stop it before uninstalling")
    if usage_lock == "active":
        blockers.append("monitor is running; stop it before uninstalling")
    if ledger_lock == "active":
        blockers.append("a ledger transaction is active; retry after it finishes")
    if data_nonempty and not purge_data:
        blockers.append("data exists; pass --purge-data to remove it")
    if command_link is not None:
        blockers.extend(command_link["blockers"])
    actions = []
    if command_link is not None:
        if command_link["owned"] and command_link["current"] != "absent":
            actions.append(f"remove managed command link {command_link['destination']}")
        elif not command_link["owned"]:
            actions.append(
                f"preserve pre-existing command link {command_link['destination']}"
            )
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
    login_hook = inspect_login_hook(
        login_hook_path,
        expected_owner=expected_owner,
    )
    login_hook_managed = login_hook["status"] in {"installed", "update-available"}
    if login_hook_managed:
        blockers.extend(login_hook["blockers"])
        actions.append(f"remove managed login notice {login_hook['path']}")
    if not socket_directory_before["exists"]:
        actions.append(f"remove socket directory {broker_socket.parent}")
    if not config_directory_before["exists"]:
        if cluster_catalog is not None:
            actions.append(f"remove validated cluster catalog {cluster_catalog}")
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
        "usage_lock": usage_lock,
        "ledger_lock": ledger_lock,
        "system_services_present": system_services_present,
        "login_hook_managed": login_hook_managed,
        "login_hook_path": login_hook["path"],
        "command_link": command_link,
        "cluster_catalog": (
            str(cluster_catalog) if cluster_catalog is not None else None
        ),
        "actions": actions,
        "blockers": blockers,
    }


def apply_admin_uninstall(
    config_file: Path,
    *,
    purge_data: bool,
    require_root: bool = True,
    login_hook_path: Optional[Path] = None,
) -> dict:
    from .login_hook import (
        DEFAULT_LOGIN_HOOK_PATH,
        apply_login_hook_uninstall,
    )

    if require_root and os.geteuid() != 0:
        raise BookingError(
            "administrator uninstall must run as root; use sudo bk admin uninstall"
        )
    expected_owner = 0 if require_root else os.geteuid()
    inspection = inspect_admin_uninstall(
        config_file,
        purge_data=purge_data,
        expected_owner=expected_owner,
        login_hook_path=login_hook_path,
    )
    if inspection["blockers"]:
        raise BookingError("; ".join(inspection["blockers"]))

    config_file = _absolute_path(config_file)
    manifest_path = _manifest_path(config_file)
    manifest = _read_manifest(manifest_path, expected_owner=expected_owner)
    data_dir = _manifest_absolute_path(manifest, "data_dir")
    broker_socket = _manifest_absolute_path(manifest, "broker_socket")
    backup_path = _validated_backup_path(manifest, config_file)
    login_hook_path = login_hook_path or DEFAULT_LOGIN_HOOK_PATH
    login_hook_removed = False
    command_link_removed = False
    cluster_catalog_removed = False

    if inspection["login_hook_managed"]:
        result = apply_login_hook_uninstall(
            login_hook_path,
            require_root=require_root,
        )
        login_hook_removed = bool(result["changed"])

    if manifest.get("command_link") is not None:
        command_link_removed = apply_command_link_uninstall(
            manifest["command_link"],
            expected_owner=expected_owner,
        )

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

    cluster_catalog_value = inspection.get("cluster_catalog")
    if cluster_catalog_value is not None:
        cluster_catalog = _validated_managed_cluster_catalog(
            config_file.parent / CLUSTER_CATALOG_NAME,
            expected_owner=expected_owner,
        )
        if cluster_catalog is None or str(cluster_catalog) != cluster_catalog_value:
            raise BookingError("managed cluster catalog changed during uninstall")
        cluster_catalog.unlink()
        fsync_directory(cluster_catalog.parent)
        cluster_catalog_removed = True

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
        "login_hook_removed": login_hook_removed,
        "command_link_removed": command_link_removed,
        "cluster_catalog_removed": cluster_catalog_removed,
        "accounts_changed": False,
    }


def inspect_admin_transfer_recovery(
    config_file: Path,
    *,
    expected_owner: int = 0,
) -> dict:
    config_file = _absolute_path(config_file)
    journal = _read_transfer_journal(
        _transfer_journal_path(config_file),
        expected_owner=expected_owner,
    )
    plan = _transfer_plan_from_journal(journal)
    allowed_pairs = {
        (plan.current_service.uid, plan.current_service.primary_gid),
        (plan.target_service.uid, plan.target_service.primary_gid),
    }
    _validate_transfer_tree(plan.data_dir, allowed_pairs)
    _validate_transfer_directory(
        plan.broker_socket.parent,
        allowed_pairs,
        BROKER_SOCKET_DIRECTORY_MODE,
        "broker socket",
    )
    blockers = []
    socket_state = _transfer_socket_state(
        plan.broker_socket,
        {0, plan.current_service.uid, plan.target_service.uid},
    )
    if socket_state == "active":
        blockers.append("broker is running; stop it before recovering the transfer")
    usage_lock = _probe_admin_lock(plan.data_dir / "usage.lock", allowed_pairs)
    if usage_lock == "active":
        blockers.append("monitor is running; stop it before recovering the transfer")
    ledger_lock = _probe_admin_lock(plan.data_dir / "ledger.lock", allowed_pairs)
    if ledger_lock == "active":
        blockers.append("a ledger transaction is active; retry after it finishes")
    status = "blocked" if blockers else "ready"
    inspection = plan.public_document(status=status, blockers=blockers)
    inspection.update(
        {
            "kind": "admin-transfer-recovery",
            "socket_state": socket_state,
            "usage_lock": usage_lock,
            "ledger_lock": ledger_lock,
            "actions": [
                "restore the prior trusted configuration and install manifest",
                "return managed data ownership to the prior service account",
                "remove the completed recovery journal",
            ],
        }
    )
    return inspection


def recover_admin_transfer(
    config_file: Path,
    *,
    require_root: bool = True,
) -> dict:
    if require_root and os.geteuid() != 0:
        raise BookingError(
            "transfer recovery must run as root; use sudo bk admin transfer --recover"
        )
    expected_owner = 0 if require_root else os.geteuid()
    inspection = inspect_admin_transfer_recovery(
        config_file,
        expected_owner=expected_owner,
    )
    if inspection["blockers"]:
        raise BookingError("; ".join(inspection["blockers"]))
    config_file = _absolute_path(config_file)
    journal_path = _transfer_journal_path(config_file)
    journal = _read_transfer_journal(journal_path, expected_owner=expected_owner)
    plan = _transfer_plan_from_journal(journal)
    old_pair = (plan.current_service.uid, plan.current_service.primary_gid)
    new_pair = (plan.target_service.uid, plan.target_service.primary_gid)
    with _admin_transfer_guard(
        plan,
        allowed_owner_pairs={old_pair, new_pair},
        lock_owner=old_pair,
        socket_owner_uids={0, old_pair[0], new_pair[0]},
    ):
        errors = _rollback_admin_transfer(
            plan,
            journal=journal,
            allowed_owner_pairs={old_pair, new_pair},
        )
        if errors:
            raise BookingError(
                "service-account transfer recovery is incomplete: "
                + "; ".join(errors)
            )
    journal_path.unlink()
    fsync_directory(journal_path.parent)
    return {
        "schema_version": ADMIN_SCHEMA_VERSION,
        "kind": "admin-transfer-recovery",
        "status": "recovered",
        "service_uid": old_pair[0],
        "service_gid": old_pair[1],
        "service_username": plan.current_service.username,
        "system_services_updated": plan.manifest.get("system_services") is not None,
        "accounts_changed": False,
    }


def _load_admin_transfer_plan(
    config_file: Path,
    target: AdminIdentity,
    *,
    expected_owner: int,
) -> AdminTransferPlan:
    _reject_pending_config_update(config_file)
    manifest_path = _manifest_path(config_file)
    manifest = _read_manifest(manifest_path, expected_owner=expected_owner)
    if manifest.get("admin_uid") != expected_owner:
        raise BookingError("install manifest administrator UID does not match")
    if manifest.get("config_file") != str(config_file):
        raise BookingError("install manifest belongs to another configuration")
    config_payload = _read_owned_regular_payload(
        config_file,
        expected_owner,
        CONFIG_FILE_MODE,
    )
    if _sha256(config_payload) != manifest.get("config_sha256"):
        raise BookingError(
            "trusted configuration does not match the install manifest; "
            "finish or roll back the prior administrator operation first"
        )
    try:
        document = json.loads(config_payload)
    except json.JSONDecodeError as exc:
        raise BookingError(f"trusted configuration is invalid JSON: {config_file}") from exc
    if not isinstance(document, dict):
        raise BookingError("trusted configuration must contain a JSON object")

    data_dir = _manifest_absolute_path(manifest, "data_dir")
    broker_socket = _manifest_absolute_path(manifest, "broker_socket")
    if _absolute_path(Path(str(document.get("data_dir", "")))) != data_dir:
        raise BookingError("trusted configuration data_dir does not match the manifest")
    if _absolute_path(Path(str(document.get("broker_socket", "")))) != broker_socket:
        raise BookingError(
            "trusted configuration broker_socket does not match the manifest"
        )
    service_uid = _manifest_nonnegative_int(manifest, "service_uid")
    service_gid = _manifest_nonnegative_int(manifest, "service_gid")
    if document.get("broker_uid") != service_uid:
        raise BookingError("trusted broker_uid does not match the install manifest")
    if document.get("monitor_uid") != service_uid:
        raise BookingError("trusted monitor_uid does not match the install manifest")
    try:
        current_record = pwd.getpwuid(service_uid)
        current_username = str(current_record.pw_name)
    except KeyError:
        current_username = str(service_uid)
    current = AdminIdentity(service_uid, current_username, service_gid)

    broker_gid = document.get("broker_gid")
    if broker_gid is not None and (
        isinstance(broker_gid, bool) or not isinstance(broker_gid, int) or broker_gid < 0
    ):
        raise BookingError("trusted broker_gid is invalid")
    socket_mode = _administrator_mode(
        document.get("broker_socket_mode"),
        "broker_socket_mode",
    )
    expected_socket_mode = (
        BROKER_GROUP_SOCKET_MODE if broker_gid is not None else BROKER_ALL_SOCKET_MODE
    )
    if socket_mode != expected_socket_mode:
        raise BookingError("trusted broker socket policy is inconsistent")
    if broker_gid is not None:
        memberships = set(os.getgrouplist(target.username, target.primary_gid))
        if broker_gid not in memberships:
            raise BookingError(
                f"target account {target.username} is not in broker group GID "
                f"{broker_gid}; add it before transferring"
            )
    if manifest.get("system_services") is not None:
        services = _validated_manifest_system_services(
            manifest,
            document,
            expected_owner=expected_owner,
            require_installed=True,
        )
        statuses, blockers = inspect_system_service_files(services)
        if blockers or any(value != "managed" for value in statuses.values()):
            raise BookingError(
                "tracked system services must be fully installed and unchanged before transfer"
            )
    return AdminTransferPlan(
        config_file=config_file,
        data_dir=data_dir,
        broker_socket=broker_socket,
        current_service=current,
        target_service=target,
        broker_gid=broker_gid,
        broker_socket_mode=socket_mode,
        config_document=document,
        manifest=manifest,
    )


def _administrator_mode(value: object, label: str) -> int:
    if isinstance(value, str):
        try:
            parsed = int(value, 8)
        except ValueError as exc:
            raise BookingError(f"trusted {label} is invalid") from exc
    elif isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    else:
        raise BookingError(f"trusted {label} is invalid")
    if parsed < 0 or parsed > 0o7777:
        raise BookingError(f"trusted {label} is invalid")
    return parsed


def _build_transfer_journal(
    plan: AdminTransferPlan,
    *,
    config_payload: bytes,
    manifest_payload: bytes,
    expected_owner: int,
) -> dict:
    return {
        "schema_version": TRANSFER_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config_file": str(plan.config_file),
        "data_dir": str(plan.data_dir),
        "broker_socket": str(plan.broker_socket),
        "from_uid": plan.current_service.uid,
        "from_gid": plan.current_service.primary_gid,
        "from_username": plan.current_service.username,
        "to_uid": plan.target_service.uid,
        "to_gid": plan.target_service.primary_gid,
        "to_username": plan.target_service.username,
        "broker_gid": plan.broker_gid,
        "broker_socket_mode": plan.broker_socket_mode,
        "config_before": _transfer_file_snapshot(
            plan.config_file,
            config_payload,
            expected_owner=expected_owner,
            expected_mode=CONFIG_FILE_MODE,
        ),
        "manifest_before": _transfer_file_snapshot(
            _manifest_path(plan.config_file),
            manifest_payload,
            expected_owner=expected_owner,
            expected_mode=INSTALL_MANIFEST_MODE,
        ),
    }


def _transfer_plan_from_journal(journal: dict) -> AdminTransferPlan:
    if not isinstance(journal, dict) or journal.get("schema_version") != TRANSFER_SCHEMA_VERSION:
        raise BookingError("unsupported or invalid service-account transfer journal")
    config_file = _absolute_path(Path(_journal_string(journal, "config_file")))
    data_dir = _absolute_path(Path(_journal_string(journal, "data_dir")))
    broker_socket = _absolute_path(Path(_journal_string(journal, "broker_socket")))
    from_uid = _journal_nonnegative_int(journal, "from_uid", allow_root=False)
    from_gid = _journal_nonnegative_int(journal, "from_gid")
    to_uid = _journal_nonnegative_int(journal, "to_uid", allow_root=False)
    to_gid = _journal_nonnegative_int(journal, "to_gid")
    from_username = _journal_string(journal, "from_username")
    to_username = _journal_string(journal, "to_username")
    broker_gid = journal.get("broker_gid")
    if broker_gid is not None:
        broker_gid = _journal_nonnegative_int(journal, "broker_gid")
    broker_socket_mode = _administrator_mode(
        journal.get("broker_socket_mode"),
        "transfer journal broker_socket_mode",
    )
    expected_socket_mode = (
        BROKER_GROUP_SOCKET_MODE if broker_gid is not None else BROKER_ALL_SOCKET_MODE
    )
    if broker_socket_mode != expected_socket_mode:
        raise BookingError("transfer journal broker socket policy is inconsistent")

    config_snapshot = _validated_transfer_snapshot(journal.get("config_before"))
    manifest_snapshot = _validated_transfer_snapshot(journal.get("manifest_before"))
    try:
        config_payload = base64.b64decode(
            config_snapshot["content_b64"], validate=True
        )
        manifest_payload = base64.b64decode(
            manifest_snapshot["content_b64"], validate=True
        )
        config_document = json.loads(config_payload)
        manifest = json.loads(manifest_payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BookingError("transfer journal contains invalid prior JSON") from exc
    if not isinstance(config_document, dict) or not isinstance(manifest, dict):
        raise BookingError("transfer journal prior files must contain JSON objects")
    if manifest.get("schema_version") != INSTALL_SCHEMA_VERSION:
        raise BookingError("transfer journal contains an invalid install manifest")
    admin_uid = _manifest_nonnegative_int(manifest, "admin_uid")
    if config_snapshot["uid"] != admin_uid or manifest_snapshot["uid"] != admin_uid:
        raise BookingError("transfer journal prior file ownership is inconsistent")
    if config_snapshot["mode"] != CONFIG_FILE_MODE:
        raise BookingError("transfer journal prior configuration mode is invalid")
    if manifest_snapshot["mode"] != INSTALL_MANIFEST_MODE:
        raise BookingError("transfer journal prior manifest mode is invalid")
    if manifest.get("config_file") != str(config_file):
        raise BookingError("transfer journal configuration path is inconsistent")
    if _manifest_absolute_path(manifest, "data_dir") != data_dir:
        raise BookingError("transfer journal data path is inconsistent")
    if _manifest_absolute_path(manifest, "broker_socket") != broker_socket:
        raise BookingError("transfer journal broker socket path is inconsistent")
    if _manifest_nonnegative_int(manifest, "service_uid") != from_uid:
        raise BookingError("transfer journal prior service UID is inconsistent")
    if _manifest_nonnegative_int(manifest, "service_gid") != from_gid:
        raise BookingError("transfer journal prior service GID is inconsistent")
    if manifest.get("config_sha256") != _sha256(config_payload):
        raise BookingError("transfer journal prior configuration checksum is inconsistent")
    if config_document.get("broker_uid") != from_uid:
        raise BookingError("transfer journal prior broker UID is inconsistent")
    if config_document.get("monitor_uid") != from_uid:
        raise BookingError("transfer journal prior monitor UID is inconsistent")
    if config_document.get("data_dir") != str(data_dir):
        raise BookingError("transfer journal prior data path is inconsistent")
    if config_document.get("broker_socket") != str(broker_socket):
        raise BookingError("transfer journal prior broker socket path is inconsistent")
    if config_document.get("broker_gid") != broker_gid:
        raise BookingError("transfer journal prior broker group is inconsistent")
    if (
        _administrator_mode(
            config_document.get("broker_socket_mode"),
            "transfer journal prior broker_socket_mode",
        )
        != broker_socket_mode
    ):
        raise BookingError("transfer journal prior broker socket mode is inconsistent")
    if manifest.get("system_services") is not None:
        _validated_manifest_system_services(
            manifest,
            config_document,
            expected_owner=admin_uid,
            require_installed=True,
        )
    return AdminTransferPlan(
        config_file=config_file,
        data_dir=data_dir,
        broker_socket=broker_socket,
        current_service=AdminIdentity(
            from_uid,
            from_username,
            from_gid,
        ),
        target_service=AdminIdentity(
            to_uid,
            to_username,
            to_gid,
        ),
        broker_gid=broker_gid,
        broker_socket_mode=broker_socket_mode,
        config_document=config_document,
        manifest=manifest,
    )


def inspect_admin_transfer(
    config_file: Path,
    target: AdminIdentity,
    *,
    expected_owner: int = 0,
) -> tuple[AdminTransferPlan, dict]:
    config_file = _absolute_path(config_file)
    journal_path = _transfer_journal_path(config_file)
    if os.path.lexists(journal_path):
        raise BookingError(
            "an interrupted service-account transfer requires recovery; run: "
            f"sudo bk admin transfer --recover --config-file {config_file}"
        )
    plan = _load_admin_transfer_plan(
        config_file,
        target,
        expected_owner=expected_owner,
    )
    current_pair = {
        (plan.current_service.uid, plan.current_service.primary_gid)
    }
    _validate_transfer_tree(plan.data_dir, current_pair)
    _validate_transfer_directory(
        plan.broker_socket.parent,
        current_pair,
        BROKER_SOCKET_DIRECTORY_MODE,
        "broker socket",
    )

    blockers = []
    socket_state = _transfer_socket_state(
        plan.broker_socket,
        {plan.current_service.uid},
    )
    if socket_state == "active":
        blockers.append("broker is running; stop it before transferring ownership")
    usage_lock = _probe_admin_lock(
        plan.data_dir / "usage.lock",
        current_pair,
    )
    if usage_lock == "active":
        blockers.append("monitor is running; stop it before transferring ownership")
    ledger_lock = _probe_admin_lock(
        plan.data_dir / "ledger.lock",
        current_pair,
    )
    if ledger_lock == "active":
        blockers.append("a ledger transaction is active; retry after it finishes")

    unchanged = (
        plan.current_service.uid == plan.target_service.uid
        and plan.current_service.primary_gid == plan.target_service.primary_gid
    )
    status = "unchanged" if unchanged else "blocked" if blockers else "ready"
    inspection = plan.public_document(status=status, blockers=blockers)
    inspection.update(
        {
            "socket_state": socket_state,
            "usage_lock": usage_lock,
            "ledger_lock": ledger_lock,
            "actions": []
            if unchanged
            else [
                "hold broker, monitor, and ledger maintenance guards",
                f"transfer managed data ownership to "
                f"{plan.target_service.uid}:{plan.target_service.primary_gid}",
                "update trusted broker_uid and monitor_uid",
                *(
                    ["update tracked systemd units to the target UID and GID"]
                    if plan.manifest.get("system_services") is not None
                    else []
                ),
                "update the root-only install manifest",
            ],
        }
    )
    return plan, inspection


def apply_admin_transfer(
    config_file: Path,
    target: AdminIdentity,
    *,
    require_root: bool = True,
) -> dict:
    if require_root and os.geteuid() != 0:
        raise BookingError(
            "service-account transfer must run as root; use sudo bk admin transfer"
        )
    expected_owner = 0 if require_root else os.geteuid()
    plan, inspection = inspect_admin_transfer(
        config_file,
        target,
        expected_owner=expected_owner,
    )
    if inspection["status"] == "unchanged":
        return {
            "status": "unchanged",
            "service_uid": target.uid,
            "service_gid": target.primary_gid,
            "service_username": target.username,
        }
    if inspection["blockers"]:
        raise BookingError("; ".join(inspection["blockers"]))

    old_pair = (plan.current_service.uid, plan.current_service.primary_gid)
    new_pair = (plan.target_service.uid, plan.target_service.primary_gid)
    config_payload = _read_owned_regular_payload(
        plan.config_file,
        expected_owner,
        CONFIG_FILE_MODE,
    )
    manifest_path = _manifest_path(plan.config_file)
    manifest_payload = _read_owned_regular_payload(
        manifest_path,
        expected_owner,
        INSTALL_MANIFEST_MODE,
    )
    new_config = dict(plan.config_document)
    new_config["broker_uid"] = target.uid
    new_config["monitor_uid"] = target.uid
    new_config_payload = _config_payload(new_config)
    old_system_services = plan.manifest.get("system_services")
    new_system_services = None
    if old_system_services is not None:
        new_system_services = retarget_system_services_document(
            old_system_services,
            service_uid=new_pair[0],
            service_gid=new_pair[1],
            expected_owner=expected_owner,
        )
    history = plan.manifest.get("service_transfers", [])
    if not isinstance(history, list):
        raise BookingError("install manifest service transfer history is invalid")
    transfer_event = {
        "at": datetime.now(timezone.utc).isoformat(),
        "from_uid": old_pair[0],
        "from_gid": old_pair[1],
        "to_uid": new_pair[0],
        "to_gid": new_pair[1],
    }
    new_manifest = {
        **plan.manifest,
        "service_uid": new_pair[0],
        "service_gid": new_pair[1],
        "config_sha256": _sha256(new_config_payload),
        "previous_config_sha256": None,
        "service_transfers": [*history[-31:], transfer_event],
    }
    if new_system_services is not None:
        new_manifest["system_services"] = new_system_services
    journal_path = _transfer_journal_path(plan.config_file)
    journal = _build_transfer_journal(
        plan,
        config_payload=config_payload,
        manifest_payload=manifest_payload,
        expected_owner=expected_owner,
    )

    rollback_completed = False
    try:
        with _admin_transfer_guard(
            plan,
            allowed_owner_pairs={old_pair},
            lock_owner=old_pair,
            socket_owner_uids={old_pair[0]},
        ):
            _write_transfer_journal(journal_path, journal, replace=False)
            try:
                _retarget_managed_tree(
                    plan.data_dir,
                    allowed_owner_pairs={old_pair},
                    target_owner=new_pair,
                )
                _retarget_transfer_path(
                    plan.broker_socket.parent,
                    allowed_owner_pairs={old_pair},
                    target_owner=new_pair,
                    expected_mode=BROKER_SOCKET_DIRECTORY_MODE,
                    require_directory=True,
                )
                if new_system_services is not None:
                    apply_installed_system_services(
                        new_system_services,
                        allowed_current=[old_system_services],
                        expected_owner=expected_owner,
                    )
                _write_new_file(
                    plan.config_file,
                    new_config_payload,
                    CONFIG_FILE_MODE,
                    replace=True,
                )
                _write_manifest(manifest_path, new_manifest, replace=True)
            except BaseException as exc:
                rollback_errors = _rollback_admin_transfer(
                    plan,
                    journal=journal,
                    allowed_owner_pairs={old_pair, new_pair},
                )
                if rollback_errors:
                    raise BookingError(
                        "service-account transfer failed and automatic rollback was "
                        f"incomplete ({'; '.join(rollback_errors)}); "
                        "run transfer --recover"
                    ) from exc
                rollback_completed = True
                raise
    except BaseException:
        if rollback_completed:
            journal_path.unlink(missing_ok=True)
            fsync_directory(journal_path.parent)
        raise
    else:
        journal_path.unlink()
        fsync_directory(journal_path.parent)

    return {
        "schema_version": ADMIN_SCHEMA_VERSION,
        "kind": "admin-transfer",
        "status": "transferred",
        "service_uid": target.uid,
        "service_gid": target.primary_gid,
        "service_username": target.username,
        "config_file": str(plan.config_file),
        "data_dir": str(plan.data_dir),
        "reservations_rewritten": False,
        "system_services_updated": new_system_services is not None,
        "accounts_changed": False,
    }


def _transfer_journal_path(config_file: Path) -> Path:
    return config_file.parent / TRANSFER_JOURNAL_NAME


def _config_update_journal_path(config_file: Path) -> Path:
    return config_file.parent / CONFIG_UPDATE_JOURNAL_NAME


def _reject_pending_config_update(config_file: Path) -> None:
    journal_path = _config_update_journal_path(config_file)
    if os.path.lexists(journal_path):
        raise BookingError(
            "an interrupted administrator configuration update must be recovered; "
            f"run: sudo bk admin gpu-policy --recover --config-file {config_file}"
        )


def _build_config_update_journal(
    plan: AdminGpuPolicyPlan,
    *,
    config_payload: bytes,
    manifest_payload: bytes,
    expected_owner: int,
) -> dict:
    return {
        "schema_version": CONFIG_UPDATE_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config_file": str(plan.config_file),
        "data_dir": str(plan.data_dir),
        "broker_socket": str(plan.broker_socket),
        "service_uid": plan.service.uid,
        "service_gid": plan.service.primary_gid,
        "config_before": _transfer_file_snapshot(
            plan.config_file,
            config_payload,
            expected_owner=expected_owner,
            expected_mode=CONFIG_FILE_MODE,
        ),
        "manifest_before": _transfer_file_snapshot(
            _manifest_path(plan.config_file),
            manifest_payload,
            expected_owner=expected_owner,
            expected_mode=INSTALL_MANIFEST_MODE,
        ),
    }


def _write_config_update_journal(path: Path, journal: dict) -> None:
    payload = (
        json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    _write_new_file(path, payload, INSTALL_MANIFEST_MODE, replace=False)


def _read_config_update_journal(
    path: Path,
    *,
    config_file: Path,
    expected_owner: int,
) -> dict:
    if not os.path.lexists(path):
        raise BookingError(f"configuration update journal is missing: {path}")
    payload = _read_owned_regular_payload(
        path,
        expected_owner,
        INSTALL_MANIFEST_MODE,
    )
    if len(payload) > 4 * 1024 * 1024:
        raise BookingError("configuration update journal is unexpectedly large")
    try:
        journal = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BookingError(f"configuration update journal is invalid JSON: {path}") from exc
    if (
        not isinstance(journal, dict)
        or journal.get("schema_version") != CONFIG_UPDATE_SCHEMA_VERSION
    ):
        raise BookingError(f"unsupported configuration update journal: {path}")
    if journal.get("config_file") != str(config_file):
        raise BookingError("configuration update journal belongs to another config file")
    for key in ("data_dir", "broker_socket"):
        value = journal.get(key)
        if not isinstance(value, str) or _absolute_path(Path(value)) != Path(value):
            raise BookingError(f"configuration update journal field {key} is invalid")
    for key in ("service_uid", "service_gid"):
        value = journal.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BookingError(f"configuration update journal field {key} is invalid")

    config_snapshot = _validated_transfer_snapshot(journal.get("config_before"))
    manifest_snapshot = _validated_transfer_snapshot(journal.get("manifest_before"))
    try:
        config_payload = base64.b64decode(
            config_snapshot["content_b64"],
            validate=True,
        )
        manifest_payload = base64.b64decode(
            manifest_snapshot["content_b64"],
            validate=True,
        )
        config_document = json.loads(config_payload)
        manifest = json.loads(manifest_payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BookingError("configuration update journal contains invalid prior JSON") from exc
    if not isinstance(config_document, dict) or not isinstance(manifest, dict):
        raise BookingError("configuration update journal prior files must be JSON objects")
    if manifest.get("schema_version") != INSTALL_SCHEMA_VERSION:
        raise BookingError("configuration update journal contains an invalid manifest")
    if manifest.get("admin_uid") != expected_owner:
        raise BookingError("configuration update journal administrator UID is invalid")
    if manifest.get("config_file") != str(config_file):
        raise BookingError("configuration update journal manifest path is inconsistent")
    if manifest.get("config_sha256") != _sha256(config_payload):
        raise BookingError("configuration update journal prior files are inconsistent")
    if manifest.get("data_dir") != journal["data_dir"]:
        raise BookingError("configuration update journal data directory is inconsistent")
    if manifest.get("broker_socket") != journal["broker_socket"]:
        raise BookingError("configuration update journal broker socket is inconsistent")
    if manifest.get("service_uid") != journal["service_uid"]:
        raise BookingError("configuration update journal service UID is inconsistent")
    if manifest.get("service_gid") != journal["service_gid"]:
        raise BookingError("configuration update journal service GID is inconsistent")
    if config_snapshot["uid"] != expected_owner or manifest_snapshot["uid"] != expected_owner:
        raise BookingError("configuration update journal prior file owner is inconsistent")
    if config_snapshot["mode"] != CONFIG_FILE_MODE:
        raise BookingError("configuration update journal prior config mode is invalid")
    if manifest_snapshot["mode"] != INSTALL_MANIFEST_MODE:
        raise BookingError("configuration update journal prior manifest mode is invalid")
    return journal


def _journal_string(document: dict, key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise BookingError(f"transfer journal field {key} must be a non-empty string")
    return value


def _journal_nonnegative_int(
    document: dict,
    key: str,
    *,
    allow_root: bool = True,
) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BookingError(
            f"transfer journal field {key} must be a non-negative integer"
        )
    if not allow_root and value == 0:
        raise BookingError(f"transfer journal field {key} must be non-root")
    return value


def _read_owned_regular_payload(path: Path, expected_owner: int, mode: int) -> bytes:
    fd = open_existing_regular(path, expected_mode=mode)
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != expected_owner:
            raise BookingError(f"managed file must be owned by UID {expected_owner}: {path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _transfer_file_snapshot(
    path: Path,
    payload: bytes,
    *,
    expected_owner: int,
    expected_mode: int,
) -> dict:
    fd = open_existing_regular(path, expected_mode=expected_mode)
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != expected_owner:
            raise BookingError(f"managed file owner drifted while preparing transfer: {path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            observed = handle.read()
    finally:
        if fd >= 0:
            os.close(fd)
    if observed != payload:
        raise BookingError(f"managed file changed while preparing transfer: {path}")
    return {
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "mode": stat.S_IMODE(metadata.st_mode),
        "sha256": _sha256(payload),
        "content_b64": base64.b64encode(payload).decode("ascii"),
    }


def _validated_transfer_snapshot(value: object) -> dict:
    if not isinstance(value, dict):
        raise BookingError("transfer journal contains an invalid file snapshot")
    for key in ("uid", "gid", "mode"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise BookingError(f"transfer journal snapshot field {key} is invalid")
    if value["mode"] > 0o7777:
        raise BookingError("transfer journal snapshot mode is invalid")
    encoded = value.get("content_b64")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError) as exc:
        raise BookingError("transfer journal snapshot content is not valid base64") from exc
    digest = value.get("sha256")
    if not isinstance(digest, str) or _sha256(payload) != digest:
        raise BookingError("transfer journal snapshot checksum does not match")
    return value


def _write_transfer_journal(path: Path, journal: dict, *, replace: bool) -> None:
    payload = (
        json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    _write_new_file(path, payload, INSTALL_MANIFEST_MODE, replace=replace)


def _read_transfer_journal(path: Path, *, expected_owner: int) -> dict:
    payload = _read_owned_regular_payload(
        path,
        expected_owner,
        INSTALL_MANIFEST_MODE,
    )
    if len(payload) > 4 * 1024 * 1024:
        raise BookingError("service-account transfer journal is unexpectedly large")
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BookingError(f"service-account transfer journal is invalid JSON: {path}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != TRANSFER_SCHEMA_VERSION:
        raise BookingError(f"unsupported or invalid service-account transfer journal: {path}")
    _transfer_plan_from_journal(document)
    return document


def _validate_transfer_directory(
    path: Path,
    allowed_owner_pairs: set[tuple[int, int]],
    expected_mode: int,
    label: str,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise BookingError(f"managed {label} directory is missing: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise BookingError(f"managed {label} path is not a real directory: {path}")
    if (metadata.st_uid, metadata.st_gid) not in allowed_owner_pairs:
        raise BookingError(f"managed {label} directory ownership drifted: {path}")
    if stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise BookingError(
            f"managed {label} directory mode must be {expected_mode:04o}: {path}"
        )
    return metadata


def _validate_transfer_tree(
    data_dir: Path,
    allowed_owner_pairs: set[tuple[int, int]],
) -> tuple[tuple[Path, bool, int], ...]:
    root_metadata = _validate_transfer_directory(
        data_dir,
        allowed_owner_pairs,
        BROKER_DIR_MODE,
        "data",
    )
    unknown = sorted(
        item.name for item in data_dir.iterdir() if item.name not in MANAGED_DATA_NAMES
    )
    if unknown:
        raise BookingError(
            "refusing service-account transfer with unknown managed-data entries: "
            + ", ".join(unknown)
        )

    entries: list[tuple[Path, bool, int]] = [
        (data_dir, True, stat.S_IMODE(root_metadata.st_mode))
    ]
    for root, directories, files in os.walk(data_dir, topdown=True, followlinks=False):
        for name in [*directories, *files]:
            path = Path(root) / name
            metadata = path.lstat()
            owner = (metadata.st_uid, metadata.st_gid)
            if owner not in allowed_owner_pairs:
                raise BookingError(f"managed data ownership drifted: {path}")
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                if mode != BROKER_DIR_MODE:
                    raise BookingError(
                        f"managed data directory mode must be {BROKER_DIR_MODE:04o}: {path}"
                    )
                entries.append((path, True, mode))
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise BookingError(f"refusing hard-linked managed data file: {path}")
                if mode not in {BROKER_FILE_MODE, INSTALL_MANIFEST_MODE}:
                    raise BookingError(
                        f"managed data file mode must be 0644 or 0600: {path}"
                    )
                entries.append((path, False, mode))
            else:
                raise BookingError(f"refusing special or symbolic file in managed data: {path}")
    return tuple(
        sorted(entries, key=lambda item: len(item[0].relative_to(data_dir).parts), reverse=True)
    )


def _transfer_socket_state(path: Path, allowed_owner_uids: set[int]) -> str:
    if not os.path.lexists(path):
        return "absent"
    metadata = path.lstat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid not in allowed_owner_uids:
        raise BookingError(f"refusing unsafe broker path during transfer: {path}")
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


def _probe_admin_lock(
    path: Path,
    allowed_owner_pairs: set[tuple[int, int]],
) -> str:
    if not os.path.lexists(path):
        return "absent"
    fd = open_existing_regular(path, os.O_RDWR, expected_mode=BROKER_FILE_MODE)
    try:
        metadata = os.fstat(fd)
        if (metadata.st_uid, metadata.st_gid) not in allowed_owner_pairs:
            raise BookingError(f"managed lock ownership drifted: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return "active"
            raise
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return "idle"
    finally:
        os.close(fd)


def _admin_service_blockers(
    data_dir: Path,
    broker_socket: Path,
    *,
    owner_pair: set[tuple[int, int]],
    socket_owner_uids: set[int],
) -> list[str]:
    blockers = []
    if _transfer_socket_state(broker_socket, socket_owner_uids) == "active":
        blockers.append("broker is running; stop it before administrator maintenance")
    if _probe_admin_lock(data_dir / "usage.lock", owner_pair) == "active":
        blockers.append("monitor is running; stop it before administrator maintenance")
    if _probe_admin_lock(data_dir / "ledger.lock", owner_pair) == "active":
        blockers.append("a ledger transaction is active; retry after it finishes")
    return blockers


@contextmanager
def _admin_file_lock(
    path: Path,
    *,
    allowed_owner_pairs: set[tuple[int, int]],
    lock_owner: tuple[int, int],
    label: str,
) -> Iterator[None]:
    flags = os.O_RDWR
    for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, name, 0)
    created = False
    try:
        fd = os.open(str(path), flags | os.O_CREAT | os.O_EXCL, BROKER_FILE_MODE)
        created = True
    except FileExistsError:
        fd = open_existing_regular(path, os.O_RDWR, expected_mode=BROKER_FILE_MODE)
    try:
        if created:
            os.fchown(fd, lock_owner[0], lock_owner[1])
            os.fchmod(fd, BROKER_FILE_MODE)
            os.fsync(fd)
            fsync_directory(path.parent)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BookingError(f"refusing unsafe {label} lock: {path}")
        if stat.S_IMODE(metadata.st_mode) != BROKER_FILE_MODE:
            raise BookingError(f"managed {label} lock mode drifted: {path}")
        if (metadata.st_uid, metadata.st_gid) not in allowed_owner_pairs:
            raise BookingError(f"managed {label} lock ownership drifted: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise BookingError(
                    f"{label} became active; stop the related process and retry"
                ) from exc
            raise
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def _admin_transfer_guard(
    plan: AdminTransferPlan,
    *,
    allowed_owner_pairs: set[tuple[int, int]],
    lock_owner: tuple[int, int],
    socket_owner_uids: set[int],
) -> Iterator[None]:
    with _admin_service_guard(
        plan.data_dir,
        plan.broker_socket,
        allowed_owner_pairs=allowed_owner_pairs,
        lock_owner=lock_owner,
        socket_owner_uids=socket_owner_uids,
        operation="transfer",
    ):
        yield


@contextmanager
def _admin_service_guard(
    data_dir: Path,
    broker_socket: Path,
    *,
    allowed_owner_pairs: set[tuple[int, int]],
    lock_owner: tuple[int, int],
    socket_owner_uids: set[int],
    operation: str,
) -> Iterator[None]:
    _validate_transfer_directory(
        broker_socket.parent,
        allowed_owner_pairs,
        BROKER_SOCKET_DIRECTORY_MODE,
        "broker socket",
    )
    socket_state = _transfer_socket_state(broker_socket, socket_owner_uids)
    if socket_state == "active":
        raise BookingError(f"broker became active; stop it and retry the {operation}")
    if socket_state == "stale":
        broker_socket.unlink()
        fsync_directory(broker_socket.parent)

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    guard_identity: Optional[tuple[int, int]] = None
    try:
        try:
            listener.bind(str(broker_socket))
            listener.listen(1)
        except OSError as exc:
            raise BookingError(
                "could not establish the broker maintenance guard; stop the broker and retry"
            ) from exc
        metadata = broker_socket.lstat()
        if not stat.S_ISSOCK(metadata.st_mode):
            raise BookingError("broker maintenance guard is not a Unix socket")
        guard_identity = (metadata.st_dev, metadata.st_ino)
        with ExitStack() as stack:
            stack.enter_context(
                _admin_file_lock(
                    data_dir / "usage.lock",
                    allowed_owner_pairs=allowed_owner_pairs,
                    lock_owner=lock_owner,
                    label="monitor",
                )
            )
            stack.enter_context(
                _admin_file_lock(
                    data_dir / "ledger.lock",
                    allowed_owner_pairs=allowed_owner_pairs,
                    lock_owner=lock_owner,
                    label="ledger",
                )
            )
            yield
    finally:
        listener.close()
        if guard_identity is not None:
            try:
                metadata = broker_socket.lstat()
            except FileNotFoundError as exc:
                raise BookingError(
                    f"broker maintenance guard disappeared during {operation}"
                ) from exc
            if (
                not stat.S_ISSOCK(metadata.st_mode)
                or (metadata.st_dev, metadata.st_ino) != guard_identity
            ):
                raise BookingError(
                    f"broker maintenance guard was replaced during {operation}"
                )
            broker_socket.unlink()
            fsync_directory(broker_socket.parent)


def _retarget_transfer_path(
    path: Path,
    *,
    allowed_owner_pairs: set[tuple[int, int]],
    target_owner: tuple[int, int],
    expected_mode: int,
    require_directory: bool,
) -> None:
    if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():
        raise BookingError(f"refusing symbolic link during service-account transfer: {path}")
    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    if require_directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(path), flags)
    try:
        metadata = os.fstat(fd)
        if require_directory:
            valid_type = stat.S_ISDIR(metadata.st_mode)
        else:
            valid_type = stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
        if not valid_type:
            raise BookingError(f"managed path type changed during transfer: {path}")
        if stat.S_IMODE(metadata.st_mode) != expected_mode:
            raise BookingError(f"managed path mode changed during transfer: {path}")
        if (metadata.st_uid, metadata.st_gid) not in allowed_owner_pairs:
            raise BookingError(f"managed path owner changed during transfer: {path}")
        if (metadata.st_uid, metadata.st_gid) != target_owner:
            os.fchown(fd, target_owner[0], target_owner[1])
            os.fchmod(fd, expected_mode)
            os.fsync(fd)
        updated = os.fstat(fd)
        if (updated.st_uid, updated.st_gid) != target_owner:
            raise BookingError(f"failed to transfer managed path ownership: {path}")
        if stat.S_IMODE(updated.st_mode) != expected_mode:
            raise BookingError(f"failed to preserve managed path mode: {path}")
    finally:
        os.close(fd)
    fsync_directory(path.parent)


def _retarget_managed_tree(
    data_dir: Path,
    *,
    allowed_owner_pairs: set[tuple[int, int]],
    target_owner: tuple[int, int],
) -> None:
    entries = _validate_transfer_tree(data_dir, allowed_owner_pairs)
    for path, is_directory, mode in entries:
        _retarget_transfer_path(
            path,
            allowed_owner_pairs=allowed_owner_pairs,
            target_owner=target_owner,
            expected_mode=mode,
            require_directory=is_directory,
        )
    _validate_transfer_tree(data_dir, {target_owner})


def _restore_transfer_snapshot(path: Path, snapshot: object) -> None:
    validated = _validated_transfer_snapshot(snapshot)
    payload = base64.b64decode(validated["content_b64"], validate=True)
    _write_new_file(path, payload, validated["mode"], replace=True)
    fd = open_existing_regular(path, expected_mode=validated["mode"])
    try:
        os.fchown(fd, validated["uid"], validated["gid"])
        os.fchmod(fd, validated["mode"])
        os.fsync(fd)
        metadata = os.fstat(fd)
        if (metadata.st_uid, metadata.st_gid) != (
            validated["uid"],
            validated["gid"],
        ):
            raise BookingError(f"failed to restore managed file ownership: {path}")
    finally:
        os.close(fd)
    fsync_directory(path.parent)


def _restore_config_update_snapshots(config_file: Path, journal: dict) -> list[str]:
    errors = []
    for path, file_snapshot, label in (
        (config_file, journal.get("config_before"), "configuration"),
        (_manifest_path(config_file), journal.get("manifest_before"), "manifest"),
    ):
        try:
            _restore_transfer_snapshot(path, file_snapshot)
        except BaseException as exc:
            errors.append(f"{label}: {exc}")
    return errors


def _rollback_admin_transfer(
    plan: AdminTransferPlan,
    *,
    journal: dict,
    allowed_owner_pairs: set[tuple[int, int]],
) -> list[str]:
    old_owner = (plan.current_service.uid, plan.current_service.primary_gid)
    errors = []
    try:
        _retarget_managed_tree(
            plan.data_dir,
            allowed_owner_pairs=allowed_owner_pairs,
            target_owner=old_owner,
        )
    except BaseException as exc:
        errors.append(f"data ownership: {exc}")
    try:
        _retarget_transfer_path(
            plan.broker_socket.parent,
            allowed_owner_pairs=allowed_owner_pairs,
            target_owner=old_owner,
            expected_mode=BROKER_SOCKET_DIRECTORY_MODE,
            require_directory=True,
        )
    except BaseException as exc:
        errors.append(f"socket ownership: {exc}")
    try:
        old_services = plan.manifest.get("system_services")
        if old_services is not None:
            expected_owner = _manifest_nonnegative_int(plan.manifest, "admin_uid")
            transferred_services = retarget_system_services_document(
                old_services,
                service_uid=plan.target_service.uid,
                service_gid=plan.target_service.primary_gid,
                expected_owner=expected_owner,
            )
            apply_installed_system_services(
                old_services,
                allowed_current=[transferred_services],
                expected_owner=expected_owner,
            )
    except BaseException as exc:
        errors.append(f"system services: {exc}")
    try:
        _restore_transfer_snapshot(plan.config_file, journal.get("config_before"))
    except BaseException as exc:
        errors.append(f"configuration: {exc}")
    try:
        _restore_transfer_snapshot(
            _manifest_path(plan.config_file),
            journal.get("manifest_before"),
        )
    except BaseException as exc:
        errors.append(f"install manifest: {exc}")
    return errors


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
    if manifest.get("command_link") is not None:
        validate_command_link_document(manifest["command_link"])
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


def _validated_managed_cluster_catalog(
    path: Path,
    *,
    expected_owner: int,
) -> Optional[Path]:
    if not os.path.lexists(path):
        return None
    fd = open_existing_regular(path, expected_mode=CONFIG_FILE_MODE)
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != expected_owner:
            raise BookingError(f"cluster catalog owner drifted: {path}")
    finally:
        os.close(fd)

    # Import lazily because admin_cluster imports this module's public entrypoint.
    from .cluster import load_cluster_config

    load_cluster_config(path, allow_legacy_pinned_user_for_repair=True)
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
    print("GPUBK administrator uninstall")
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
    _reject_pending_config_update(plan.config_file)
    if os.path.lexists(_transfer_journal_path(plan.config_file)):
        raise BookingError(
            "reserved transfer journal already exists beside the configuration; "
            "review or recover it before initialization"
        )
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
        args.broker_socket or DEFAULT_BROKER_SOCKET,
        enabled=interactive
        and args.broker_socket in {None, DEFAULT_BROKER_SOCKET},
    )

    gpu_count = _ask_int(
        "GPU count",
        args.gpu_count if args.gpu_count is not None else detected_gpu_count,
        minimum=1,
        maximum=MAX_GPU_COUNT,
        enabled=interactive and args.gpu_count is None,
    )
    disabled_value = args.disabled_gpus
    if interactive and disabled_value is None:
        disabled_value = _ask("Disabled GPUs (comma separated; blank for none)", "", enabled=True)
    priority_value = args.gpu_priority
    if interactive and priority_value is None:
        priority_value = _ask("Lower-priority GPUs (GPU=LEVEL; blank for none)", "", enabled=True)
    while True:
        try:
            disabled_gpus = validate_gpu_list(
                disabled_value or None,
                gpu_count,
                "disabled_gpus",
            )
            gpu_priority = validate_gpu_priority(priority_value or None, gpu_count)
            break
        except ValueError as exc:
            if not interactive:
                raise BookingError(str(exc)) from exc
            print(f"Invalid GPU policy: {exc}")
            disabled_value = _ask(
                "Disabled GPUs (comma separated; blank for none)",
                "",
                enabled=True,
            )
            priority_value = _ask(
                "Lower-priority GPUs (GPU=LEVEL; blank for none)",
                "",
                enabled=True,
            )
    slot_minutes = _ask_slot_minutes(
        args.slot_minutes if args.slot_minutes is not None else DEFAULT_SLOT_MINUTES,
        enabled=interactive and args.slot_minutes is None,
    )
    max_shared_users = _ask_int(
        "Maximum shared slots per GPU",
        args.max_shared_users if args.max_shared_users is not None else 2,
        minimum=1,
        maximum=MAX_SHARED_UNITS,
        enabled=interactive and args.max_shared_users is None,
    )
    require_shared_memory = (
        args.require_shared_memory
        if args.require_shared_memory is not None
        else _ask_bool(
            "Require a resolved VRAM budget for shared bookings",
            False,
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
        disabled_gpus=disabled_gpus,
        gpu_priority=gpu_priority,
    )


def _validate_plan(plan: AdminInitPlan) -> None:
    if plan.config_file.name in {
        INSTALL_MANIFEST_NAME,
        TRANSFER_JOURNAL_NAME,
        CONFIG_UPDATE_JOURNAL_NAME,
    }:
        raise BookingError(
            "trusted configuration filename conflicts with administrator metadata"
        )
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
        disabled_gpus=plan.disabled_gpus,
        gpu_priority=plan.gpu_priority,
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

    sudo_uid = os.environ.get("SUDO_UID")
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_uid is not None or sudo_user is not None:
        if not sudo_uid or not sudo_uid.isdecimal() or not sudo_user:
            raise BookingError("SUDO_UID and SUDO_USER do not identify a valid account")
        by_uid = _resolve_identity(sudo_uid)
        by_name = _resolve_identity(sudo_user)
        if by_uid.uid != by_name.uid:
            raise BookingError("SUDO_UID and SUDO_USER identify different accounts")
        return by_uid

    if os.getuid() != 0:
        return _resolve_identity(str(os.getuid()))
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


def _username_for_uid(uid: int) -> str:
    try:
        return str(pwd.getpwuid(uid).pw_name)
    except KeyError:
        return str(uid)


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
    print("GPUBK administrator setup")
    print(f"  GPUs:       {plan.gpu_count}")
    print(f"  data:       {plan.data_dir}")
    print(f"  config:     {plan.config_file}")
    print(f"  time slice: {plan.slot_minutes} minutes")
    print(f"  sharing:    max {plan.max_shared_users} slots per GPU")
    if plan.disabled_gpus:
        print(f"  disabled:   {','.join(map(str, plan.disabled_gpus))}")
    if plan.gpu_priority:
        print(
            "  priority:   "
            + ",".join(f"{gpu}={priority}" for gpu, priority in plan.gpu_priority)
        )
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
