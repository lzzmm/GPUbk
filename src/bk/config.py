from __future__ import annotations

import difflib
import json
import math
import os
import shlex
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .fileio import open_existing_regular_at
from .granularity import DEFAULT_SLOT_MINUTES, slot_seconds, validate_slot_minutes
from .userdirs import xdg_user_directory


DEFAULT_PRIVATE_FILE_MODE = 0o600
DEFAULT_PRIVATE_DIR_MODE = 0o700
BROKER_FILE_MODE = 0o644
BROKER_DIR_MODE = 0o755
BROKER_ALL_SOCKET_MODE = 0o666
BROKER_GROUP_SOCKET_MODE = 0o660
MAX_CONFIG_FILE_BYTES = 1024 * 1024
CONFIG_VERSION = 1
SYSTEM_CONFIG_FILE = Path("/etc/gpubk/config.json")
CONFIG_UPDATE_JOURNAL_NAME = "config-update.json"
MAX_GPU_COUNT = 1024
MAX_SHARED_UNITS = 10_000
MAX_QUEUE_SEARCH_HOURS = 10 * 365 * 24
MAX_BOOKING_HORIZON_DAYS = 10 * 365
MAX_BOOKING_BLACKOUTS = 512
MAX_BLACKOUT_REASON_LENGTH = 512
MAX_RETENTION_DAYS = 100 * 365
MAX_USAGE_LOAD_WINDOW_MINUTES = 365 * 24 * 60
MAX_LOCK_TIMEOUT_SECONDS = 60 * 60
MAX_BACKUP_KEEP = 10_000
MAX_TIMELINE_HOURS = 365 * 24
MAX_MEMORY_MB = 16 * 1024 * 1024
MAX_WORKER_POLL_SECONDS = 60 * 60
DEFAULT_WORKER_MAX_PARALLEL = 64
MAX_WORKER_MAX_PARALLEL = 4096
DEFAULT_WORKER_TERMINATION_GRACE_SECONDS = 5.0
MIN_WORKER_TERMINATION_GRACE_SECONDS = 0.1
MAX_WORKER_TERMINATION_GRACE_SECONDS = 60.0
MAX_WORKER_CLAIM_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
MAX_WORKER_RECOVERY_GRACE_SECONDS = 24 * 60 * 60
MAX_ALLOCATOR_TIMEOUT_SECONDS = 5 * 60
MAX_ALLOCATOR_WEIGHT = 1_000_000
MAX_GPU_PRIORITY = 1_000_000
MIN_MONITOR_INTERVAL_SECONDS = 0.2
MAX_MONITOR_INTERVAL_SECONDS = 60 * 60
MAX_MONITOR_ROLLUP_SECONDS = 24 * 60 * 60
MIN_TUI_REFRESH_SECONDS = 0.1
MAX_TUI_REFRESH_SECONDS = 60.0
MAX_UID = 2**32 - 2

CONFIG_ENV_MAP = {
    "gpu_count": "BK_GPU_COUNT",
    "slot_minutes": "BK_SLOT_MINUTES",
    "max_shared_users": "BK_MAX_SHARED_USERS",
    "queue_search_hours": "BK_QUEUE_SEARCH_HOURS",
    "booking_horizon_days": "BK_BOOKING_HORIZON_DAYS",
    "booking_blackouts": "BK_BOOKING_BLACKOUTS",
    "ledger_retention_days": "BK_LEDGER_RETENTION_DAYS",
    "usage_load_window_minutes": "BK_USAGE_LOAD_WINDOW_MINUTES",
    "usage_minute_retention_days": "BK_USAGE_MINUTE_RETENTION_DAYS",
    "usage_five_minute_retention_days": "BK_USAGE_FIVE_MINUTE_RETENTION_DAYS",
    "usage_ten_minute_retention_days": "BK_USAGE_TEN_MINUTE_RETENTION_DAYS",
    "usage_hourly_retention_days": "BK_USAGE_HOURLY_RETENTION_DAYS",
    "usage_daily_retention_days": "BK_USAGE_DAILY_RETENTION_DAYS",
    "usage_event_retention_days": "BK_USAGE_EVENT_RETENTION_DAYS",
    "lock_timeout_seconds": "BK_LOCK_TIMEOUT_SECONDS",
    "backup_keep": "BK_BACKUP_KEEP",
    "timeline_hours": "BK_TIMELINE_HOURS",
    "require_shared_memory": "BK_REQUIRE_SHARED_MEMORY",
    "shared_memory_reserve_mb": "BK_SHARED_MEMORY_RESERVE_MB",
    "job_log_retention_days": "BK_JOB_LOG_RETENTION_DAYS",
    "job_log_max_mb": "BK_JOB_LOG_MAX_MB",
    "job_log_total_max_mb": "BK_JOB_LOG_TOTAL_MAX_MB",
    "worker_poll_seconds": "BK_WORKER_POLL_SECONDS",
    "worker_max_parallel": "BK_WORKER_MAX_PARALLEL",
    "worker_termination_grace_seconds": "BK_WORKER_TERMINATION_GRACE_SECONDS",
    "worker_claim_timeout_seconds": "BK_WORKER_CLAIM_TIMEOUT_SECONDS",
    "worker_recovery_grace_seconds": "BK_WORKER_RECOVERY_GRACE_SECONDS",
    "worker_live_guard": "BK_WORKER_LIVE_GUARD",
    "monitor_interval_seconds": "BK_MONITOR_INTERVAL_SECONDS",
    "monitor_rollup_seconds": "BK_MONITOR_ROLLUP_SECONDS",
    "tui_refresh_seconds": "BK_TUI_REFRESH_SECONDS",
    "file_mode": "BK_FILE_MODE",
    "dir_mode": "BK_DIR_MODE",
    "disabled_gpus": "BK_DISABLED_GPUS",
    "gpu_priority": "BK_GPU_PRIORITY",
}
CONFIG_FILE_KEYS = frozenset(
    {
        "config_version",
        "data_dir",
        *CONFIG_ENV_MAP,
        "job_log_dir",
        "allocator_command",
        "allocator_timeout_seconds",
        "allocator_weight",
        "monitor_uid",
        "storage_gid",
        "broker_socket",
        "broker_uid",
        "broker_gid",
        "broker_socket_mode",
    }
)


@dataclass(frozen=True)
class Config:
    data_dir: Path
    gpu_count: int = 1
    max_shared_users: int = 2
    queue_search_hours: int = 168
    booking_horizon_days: int = 30
    booking_blackouts: Tuple[Tuple[str, str, str], ...] = ()
    ledger_retention_days: int = 90
    usage_load_window_minutes: int = 120
    usage_minute_retention_days: int = 30
    usage_five_minute_retention_days: int = 365
    usage_ten_minute_retention_days: int = 1095
    usage_hourly_retention_days: int = 1500
    usage_daily_retention_days: int = 0
    usage_event_retention_days: int = 365
    lock_timeout_seconds: float = 10.0
    backup_keep: int = 10
    timeline_hours: int = 2
    require_shared_memory: bool = False
    shared_memory_reserve_mb: int = 512
    job_log_dir: Optional[Path] = None
    job_log_retention_days: int = 30
    job_log_max_mb: int = 64
    job_log_total_max_mb: int = 4096
    worker_poll_seconds: float = 1.0
    worker_claim_timeout_seconds: float = 30.0
    worker_recovery_grace_seconds: float = 5.0
    worker_live_guard: bool = True
    file_mode: int = DEFAULT_PRIVATE_FILE_MODE
    dir_mode: int = DEFAULT_PRIVATE_DIR_MODE
    allocator_command: Optional[Tuple[str, ...]] = None
    allocator_timeout_seconds: float = 3.0
    allocator_weight: float = 5.0
    slot_minutes: int = DEFAULT_SLOT_MINUTES
    config_file: Optional[Path] = None
    monitor_interval_seconds: float = 2.0
    monitor_rollup_seconds: int = 60
    tui_refresh_seconds: float = 1.0
    monitor_uid: Optional[int] = None
    config_owner_uid: Optional[int] = None
    worker_max_parallel: int = DEFAULT_WORKER_MAX_PARALLEL
    worker_termination_grace_seconds: float = DEFAULT_WORKER_TERMINATION_GRACE_SECONDS
    storage_gid: Optional[int] = None
    broker_socket: Optional[Path] = None
    broker_uid: Optional[int] = None
    broker_gid: Optional[int] = None
    broker_socket_mode: int = 0o600
    disabled_gpus: Tuple[int, ...] = ()
    gpu_priority: Tuple[Tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.booking_horizon_days, bool)
            or not isinstance(self.booking_horizon_days, int)
            or self.booking_horizon_days < 1
            or self.booking_horizon_days > MAX_BOOKING_HORIZON_DAYS
        ):
            raise ValueError(
                "booking_horizon_days must be between 1 and "
                f"{MAX_BOOKING_HORIZON_DAYS}"
            )
        object.__setattr__(
            self, "slot_minutes", validate_slot_minutes(self.slot_minutes)
        )
        interval, rollup = validate_monitor_timing(
            self.monitor_interval_seconds,
            self.monitor_rollup_seconds,
        )
        object.__setattr__(self, "monitor_interval_seconds", interval)
        object.__setattr__(self, "monitor_rollup_seconds", rollup)
        object.__setattr__(
            self,
            "tui_refresh_seconds",
            validate_tui_refresh_seconds(self.tui_refresh_seconds),
        )
        object.__setattr__(
            self,
            "disabled_gpus",
            validate_gpu_list(self.disabled_gpus, self.gpu_count, "disabled_gpus"),
        )
        object.__setattr__(
            self,
            "gpu_priority",
            validate_gpu_priority(self.gpu_priority, self.gpu_count),
        )
        object.__setattr__(
            self,
            "booking_blackouts",
            validate_booking_blackouts(self.booking_blackouts),
        )
        object.__setattr__(
            self,
            "monitor_uid",
            validate_optional_uid(self.monitor_uid, "monitor_uid"),
        )
        object.__setattr__(
            self,
            "storage_gid",
            validate_optional_gid(self.storage_gid, "storage_gid"),
        )
        if self.storage_gid is not None and not self.dir_mode & stat.S_ISGID:
            raise ValueError("storage_gid requires a setgid dir_mode such as 2770")
        object.__setattr__(
            self,
            "config_owner_uid",
            validate_optional_uid(self.config_owner_uid, "config_owner_uid"),
        )
        object.__setattr__(
            self,
            "broker_uid",
            validate_optional_uid(self.broker_uid, "broker_uid"),
        )
        object.__setattr__(
            self,
            "broker_gid",
            validate_optional_gid(self.broker_gid, "broker_gid"),
        )
        if self.broker_socket is not None:
            socket_path = Path(self.broker_socket).expanduser()
            if not socket_path.is_absolute():
                raise ValueError("broker_socket must be an absolute path")
            object.__setattr__(
                self,
                "broker_socket",
                Path(os.path.abspath(os.fspath(socket_path))),
            )
            if self.broker_uid is None:
                raise ValueError("broker_socket requires broker_uid")
            if self.file_mode != BROKER_FILE_MODE or self.dir_mode != BROKER_DIR_MODE:
                raise ValueError(
                    "broker storage must use file_mode 0644 and dir_mode 0755"
                )
            expected_socket_mode = (
                BROKER_GROUP_SOCKET_MODE
                if self.broker_gid is not None
                else BROKER_ALL_SOCKET_MODE
            )
            if self.broker_socket_mode != expected_socket_mode:
                raise ValueError(
                    "broker_socket_mode must be 0660 with broker_gid or 0666 without it"
                )
            if self.storage_gid is not None:
                raise ValueError("broker storage must not use storage_gid")
        elif self.broker_uid is not None or self.broker_gid is not None:
            raise ValueError("broker_uid and broker_gid require broker_socket")
        object.__setattr__(
            self,
            "worker_max_parallel",
            validate_worker_max_parallel(self.worker_max_parallel),
        )
        object.__setattr__(
            self,
            "worker_termination_grace_seconds",
            validate_worker_termination_grace_seconds(
                self.worker_termination_grace_seconds
            ),
        )

    @property
    def slot_seconds(self) -> int:
        return slot_seconds(self.slot_minutes)

    @property
    def effective_worker_max_parallel(self) -> int:
        scheduling_capacity = len(self.enabled_gpus) * self.max_shared_users
        return max(1, min(self.worker_max_parallel, scheduling_capacity))

    @property
    def enabled_gpus(self) -> Tuple[int, ...]:
        disabled = set(self.disabled_gpus)
        return tuple(gpu for gpu in range(self.gpu_count) if gpu not in disabled)

    @property
    def gpu_priority_map(self) -> Dict[int, int]:
        return dict(self.gpu_priority)

    @property
    def access_mode(self) -> str:
        if self.broker_socket is not None:
            return "group" if self.broker_gid is not None else "all"
        if self.dir_mode & stat.S_IWOTH:
            return "all"
        if self.dir_mode & stat.S_IWGRP:
            return "group"
        return "private"

    @property
    def storage_transport(self) -> str:
        return "broker" if self.broker_socket is not None else "direct"

    @property
    def config_path(self) -> Path:
        return self.config_file or self.data_dir / "config.json"


def validate_monitor_timing(
    interval_seconds: object,
    rollup_seconds: object,
) -> Tuple[float, int]:
    if isinstance(interval_seconds, bool):
        raise ValueError("monitor_interval_seconds must be a number")
    try:
        interval = float(interval_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("monitor_interval_seconds must be a number") from exc
    if not math.isfinite(interval):
        raise ValueError("monitor_interval_seconds must be finite")
    if interval < MIN_MONITOR_INTERVAL_SECONDS:
        raise ValueError(
            f"monitor_interval_seconds must be >= {MIN_MONITOR_INTERVAL_SECONDS:g}"
        )
    if interval > MAX_MONITOR_INTERVAL_SECONDS:
        raise ValueError(
            f"monitor_interval_seconds must be <= {MAX_MONITOR_INTERVAL_SECONDS}"
        )

    if isinstance(rollup_seconds, bool) or not isinstance(rollup_seconds, (int, str)):
        raise ValueError("monitor_rollup_seconds must be an integer")
    try:
        rollup = int(rollup_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("monitor_rollup_seconds must be an integer") from exc
    if rollup < 1:
        raise ValueError("monitor_rollup_seconds must be >= 1")
    if rollup > MAX_MONITOR_ROLLUP_SECONDS:
        raise ValueError(
            f"monitor_rollup_seconds must be <= {MAX_MONITOR_ROLLUP_SECONDS}"
        )
    if rollup < interval:
        raise ValueError("monitor_rollup_seconds must be >= monitor_interval_seconds")
    samples_per_rollup = rollup / interval
    if not math.isclose(
        samples_per_rollup,
        round(samples_per_rollup),
        rel_tol=1e-9,
        abs_tol=1e-9,
    ):
        raise ValueError(
            "monitor_rollup_seconds must be an integer multiple of monitor_interval_seconds"
        )
    return interval, rollup


def validate_tui_refresh_seconds(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("tui_refresh_seconds must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("tui_refresh_seconds must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError("tui_refresh_seconds must be finite")
    if parsed < MIN_TUI_REFRESH_SECONDS:
        raise ValueError(f"tui_refresh_seconds must be >= {MIN_TUI_REFRESH_SECONDS:g}")
    if parsed > MAX_TUI_REFRESH_SECONDS:
        raise ValueError(f"tui_refresh_seconds must be <= {MAX_TUI_REFRESH_SECONDS:g}")
    return parsed


def validate_worker_max_parallel(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("worker_max_parallel must be an integer")
    if value < 1:
        raise ValueError("worker_max_parallel must be >= 1")
    if value > MAX_WORKER_MAX_PARALLEL:
        raise ValueError(f"worker_max_parallel must be <= {MAX_WORKER_MAX_PARALLEL}")
    return value


def validate_worker_termination_grace_seconds(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("worker_termination_grace_seconds must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("worker_termination_grace_seconds must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError("worker_termination_grace_seconds must be finite")
    if parsed < MIN_WORKER_TERMINATION_GRACE_SECONDS:
        raise ValueError(
            "worker_termination_grace_seconds must be >= "
            f"{MIN_WORKER_TERMINATION_GRACE_SECONDS:g}"
        )
    if parsed > MAX_WORKER_TERMINATION_GRACE_SECONDS:
        raise ValueError(
            "worker_termination_grace_seconds must be <= "
            f"{MAX_WORKER_TERMINATION_GRACE_SECONDS:g}"
        )
    return parsed


def validate_optional_uid(value: object, key: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{key} must be an integer UID or null")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer UID or null") from exc
    if parsed < 0 or parsed > MAX_UID:
        raise ValueError(f"{key} must be between 0 and {MAX_UID}")
    return parsed


def validate_optional_gid(value: object, key: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{key} must be an integer GID or null")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer GID or null") from exc
    if parsed < 0 or parsed > MAX_UID:
        raise ValueError(f"{key} must be between 0 and {MAX_UID}")
    return parsed


def validate_gpu_list(value: object, gpu_count: int, key: str) -> Tuple[int, ...]:
    if value is None or value == "":
        return ()
    parsed: object = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{key} must be comma-separated GPU IDs or a JSON array") from exc
        else:
            parsed = [part.strip() for part in text.split(",") if part.strip()]
    if not isinstance(parsed, (list, tuple, set, frozenset)):
        raise ValueError(f"{key} must be an array of GPU IDs")
    result = []
    seen = set()
    for raw_gpu in parsed:
        if isinstance(raw_gpu, bool):
            raise ValueError(f"{key} must contain integer GPU IDs")
        try:
            gpu = int(raw_gpu)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must contain integer GPU IDs") from exc
        if gpu < 0 or gpu >= gpu_count:
            raise ValueError(f"{key} GPU index out of range: {gpu}")
        if gpu in seen:
            raise ValueError(f"{key} contains duplicate GPU index: {gpu}")
        seen.add(gpu)
        result.append(gpu)
    return tuple(sorted(result))


def validate_gpu_priority(value: object, gpu_count: int) -> Tuple[Tuple[int, int], ...]:
    if value is None or value == "" or value == ():
        return ()
    parsed: object = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "gpu_priority must look like 6=10,7=20 or a JSON object"
                ) from exc
        else:
            entries = []
            for item in text.split(","):
                item = item.strip()
                separator = "=" if "=" in item else ":"
                if not item or separator not in item:
                    raise ValueError("gpu_priority must look like 6=10,7=20")
                gpu, priority = item.split(separator, 1)
                entries.append((gpu.strip(), priority.strip()))
            parsed = entries
    if isinstance(parsed, Mapping):
        entries: Sequence[tuple[object, object]] = list(parsed.items())
    elif isinstance(parsed, (list, tuple)):
        entries = list(parsed)
    else:
        raise ValueError("gpu_priority must map GPU IDs to integer priority levels")

    result = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise ValueError("gpu_priority entries must be GPU/priority pairs")
        raw_gpu, raw_priority = entry
        if isinstance(raw_gpu, bool) or isinstance(raw_priority, bool):
            raise ValueError("gpu_priority entries must contain integers")
        try:
            gpu = int(raw_gpu)
            priority = int(raw_priority)
        except (TypeError, ValueError) as exc:
            raise ValueError("gpu_priority entries must contain integers") from exc
        if gpu < 0 or gpu >= gpu_count:
            raise ValueError(f"gpu_priority GPU index out of range: {gpu}")
        if priority < 0 or priority > MAX_GPU_PRIORITY:
            raise ValueError(f"GPU priority must be between 0 and {MAX_GPU_PRIORITY}")
        if gpu in seen:
            raise ValueError(f"gpu_priority contains duplicate GPU index: {gpu}")
        seen.add(gpu)
        if priority:
            result.append((gpu, priority))
    return tuple(sorted(result))


def validate_booking_blackouts(value: object) -> Tuple[Tuple[str, str, str], ...]:
    if value is None or value == "" or value == ():
        return ()
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("booking_blackouts must be a JSON array") from exc
    if not isinstance(parsed, (list, tuple)):
        raise ValueError("booking_blackouts must be an array")
    if len(parsed) > MAX_BOOKING_BLACKOUTS:
        raise ValueError(f"booking_blackouts must contain at most {MAX_BOOKING_BLACKOUTS} windows")
    result = []
    for index, item in enumerate(parsed):
        if isinstance(item, dict):
            start_raw = item.get("start_at")
            end_raw = item.get("end_at")
            reason_raw = item.get("reason", "administrator maintenance")
        elif isinstance(item, (list, tuple)) and len(item) == 3:
            start_raw, end_raw, reason_raw = item
        else:
            raise ValueError(
                f"booking_blackouts[{index}] must contain start_at, end_at, and reason"
            )
        start = _blackout_timestamp(start_raw, f"booking_blackouts[{index}].start_at")
        end = _blackout_timestamp(end_raw, f"booking_blackouts[{index}].end_at")
        if start >= end:
            raise ValueError(f"booking_blackouts[{index}] end_at must be after start_at")
        if not isinstance(reason_raw, str):
            raise ValueError(f"booking_blackouts[{index}].reason must be text")
        reason = reason_raw.strip()
        if not reason or len(reason) > MAX_BLACKOUT_REASON_LENGTH:
            raise ValueError(
                f"booking_blackouts[{index}].reason must contain 1-{MAX_BLACKOUT_REASON_LENGTH} characters"
            )
        if any(ord(character) < 32 for character in reason):
            raise ValueError(f"booking_blackouts[{index}].reason contains control characters")
        result.append((_config_iso(start), _config_iso(end), reason))
    result.sort(key=lambda item: (item[0], item[1], item[2]))
    return tuple(result)


def _blackout_timestamp(value: object, key: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError(f"{key} must be an ISO 8601 timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{key} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{key} must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _config_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _read_config_file(
    path: Path,
    *,
    required: bool = False,
    missing_label: str = "BK_CONFIG_FILE",
) -> Tuple[Dict[str, Any], Optional[int]]:
    if not os.path.lexists(path):
        if required:
            raise FileNotFoundError(f"configured {missing_label} does not exist: {path}")
        return {}, None
    parent_fd = _open_trusted_config_parent(path.parent)
    try:
        fd = open_existing_regular_at(parent_fd, path.name, path)
    finally:
        os.close(parent_fd)
    try:
        metadata = os.fstat(fd)
        config_owner_uid = metadata.st_uid
        if config_owner_uid not in {0, os.getuid()}:
            raise PermissionError(f"{path} must be owned by root or UID {os.getuid()}")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise PermissionError(f"{path} must not be writable by group or other users")
        if metadata.st_size > MAX_CONFIG_FILE_BYTES:
            raise ValueError(f"{path} exceeds the 1 MiB configuration limit")
        fh = os.fdopen(fd, "r", encoding="utf-8")
        fd = -1
        with fh:
            raw = json.load(fh)
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    _validate_config_document(raw, path)
    return raw, config_owner_uid


def _validate_config_document(raw: Dict[str, Any], path: Path) -> None:
    version = raw.get("config_version", CONFIG_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"{path}: config_version must be the integer {CONFIG_VERSION}")
    if version != CONFIG_VERSION:
        raise ValueError(
            f"{path}: unsupported config_version {version}; this GPUBK supports {CONFIG_VERSION}"
        )
    unknown = sorted(set(raw) - CONFIG_FILE_KEYS)
    if not unknown:
        return
    key = unknown[0]
    suggestion = difflib.get_close_matches(key, CONFIG_FILE_KEYS, n=1, cutoff=0.65)
    hint = f"; did you mean {suggestion[0]!r}?" if suggestion else ""
    raise ValueError(f"{path}: unknown config key {key!r}{hint}")


def _int_value(
    raw: Dict[str, Any],
    key: str,
    default: int,
    *,
    maximum: Optional[int] = None,
) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if parsed < 1:
        raise ValueError(f"{key} must be >= 1")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{key} must be <= {maximum}")
    return parsed


def _float_value(
    raw: Dict[str, Any],
    key: str,
    default: float,
    *,
    maximum: Optional[float] = None,
) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{key} must be finite")
    if parsed <= 0:
        raise ValueError(f"{key} must be > 0")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{key} must be <= {maximum}")
    return parsed


def _bool_value(raw: Dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise ValueError(f"{key} must be a boolean")


def _nonnegative_int_value(
    raw: Dict[str, Any],
    key: str,
    default: int,
    *,
    maximum: Optional[int] = None,
) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{key} must be >= 0")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{key} must be <= {maximum}")
    return parsed


def _mode_value(raw: Dict[str, Any], key: str, default: int, *, directory: bool) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{key} must be an octal mode such as 0600 or 2770")
    try:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized.startswith("0o"):
                normalized = normalized[2:]
            parsed = int(normalized, 8)
        else:
            parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an octal mode such as 0600 or 2770") from exc
    allowed = 0o7777 if directory else 0o777
    if parsed < 0 or parsed & ~allowed:
        raise ValueError(f"invalid {key}")
    if not directory and parsed & 0o111:
        raise ValueError("file_mode must not contain executable bits")
    return parsed


def _default_data_dir() -> Path:
    data_home = xdg_user_directory("XDG_DATA_HOME", ".local/share")
    return data_home / "bk"


def _auto_gpu_count() -> int:
    from .gpu import detect_gpu_count

    return max(1, detect_gpu_count())


def load_config() -> Config:
    explicit_data_dir = "BK_DATA_DIR" in os.environ
    data_dir = (
        _path_value(os.environ["BK_DATA_DIR"], "BK_DATA_DIR")
        if explicit_data_dir
        else _default_data_dir()
    )
    explicit_config_file = "BK_CONFIG_FILE" in os.environ
    system_config_file = (
        not explicit_config_file
        and not explicit_data_dir
        and os.path.lexists(SYSTEM_CONFIG_FILE)
    )
    if explicit_config_file:
        config_file = _canonical_config_file(
            _path_value(os.environ["BK_CONFIG_FILE"], "BK_CONFIG_FILE")
        )
    elif system_config_file:
        config_file = _canonical_config_file(SYSTEM_CONFIG_FILE)
    else:
        config_file = data_dir / "config.json"
    external_config = explicit_config_file or system_config_file
    update_journal = config_file.parent / CONFIG_UPDATE_JOURNAL_NAME
    if external_config and os.path.lexists(update_journal):
        raise ValueError(
            "an interrupted administrator configuration update must be recovered; "
            f"run: sudo bk admin gpu-policy --recover --config-file {config_file}"
        )
    raw, config_owner_uid = _read_config_file(
        config_file,
        required=external_config,
        missing_label=(
            "BK_CONFIG_FILE" if explicit_config_file else "system configuration file"
        ),
    )
    if "data_dir" in raw:
        if not external_config:
            raise ValueError(
                f"{config_file}: data_dir is only allowed in BK_CONFIG_FILE "
                f"or {SYSTEM_CONFIG_FILE}"
            )
        if not explicit_data_dir:
            data_dir = _absolute_path_value(raw["data_dir"], "data_dir")
    elif external_config and not explicit_data_dir:
        raise ValueError(
            f"{config_file}: external or system configuration must define data_dir "
            "when BK_DATA_DIR is unset"
        )

    for key, env_name in CONFIG_ENV_MAP.items():
        if env_name in os.environ:
            raw[key] = os.environ[env_name]

    job_log_raw = os.environ.get("BK_JOB_LOG_DIR", raw.get("job_log_dir"))
    if job_log_raw is not None and job_log_raw != "":
        job_log_key = "BK_JOB_LOG_DIR" if "BK_JOB_LOG_DIR" in os.environ else "job_log_dir"
        job_log_dir = _absolute_user_path_value(job_log_raw, job_log_key)
    else:
        state_home = xdg_user_directory("XDG_STATE_HOME", ".local/state")
        job_log_dir = state_home / "bk" / "jobs"
    allocator_raw = os.environ.get("BK_ALLOCATOR_COMMAND", raw.get("allocator_command"))
    allocator_command = _command_value(allocator_raw)
    broker_socket_raw = raw.get("broker_socket")
    if broker_socket_raw is not None:
        if not external_config:
            raise ValueError(
                f"{config_file}: broker_socket is only allowed in a trusted external "
                "or system configuration"
            )
        broker_socket = _absolute_path_value(broker_socket_raw, "broker_socket")
    else:
        broker_socket = None
    gpu_count = (
        _int_value(raw, "gpu_count", 1, maximum=MAX_GPU_COUNT)
        if "gpu_count" in raw
        else _bounded_detected_gpu_count(_auto_gpu_count())
    )

    return Config(
        data_dir=data_dir,
        config_file=config_file if external_config else None,
        gpu_count=gpu_count,
        slot_minutes=validate_slot_minutes(raw.get("slot_minutes", DEFAULT_SLOT_MINUTES)),
        max_shared_users=_int_value(raw, "max_shared_users", 2, maximum=MAX_SHARED_UNITS),
        queue_search_hours=_int_value(
            raw, "queue_search_hours", 168, maximum=MAX_QUEUE_SEARCH_HOURS
        ),
        booking_horizon_days=_int_value(
            raw,
            "booking_horizon_days",
            30,
            maximum=MAX_BOOKING_HORIZON_DAYS,
        ),
        booking_blackouts=validate_booking_blackouts(
            raw.get("booking_blackouts")
        ),
        ledger_retention_days=_nonnegative_int_value(
            raw, "ledger_retention_days", 90, maximum=MAX_RETENTION_DAYS
        ),
        usage_load_window_minutes=_int_value(
            raw,
            "usage_load_window_minutes",
            120,
            maximum=MAX_USAGE_LOAD_WINDOW_MINUTES,
        ),
        usage_minute_retention_days=_nonnegative_int_value(
            raw, "usage_minute_retention_days", 30, maximum=MAX_RETENTION_DAYS
        ),
        usage_five_minute_retention_days=_nonnegative_int_value(
            raw, "usage_five_minute_retention_days", 365, maximum=MAX_RETENTION_DAYS
        ),
        usage_ten_minute_retention_days=_nonnegative_int_value(
            raw, "usage_ten_minute_retention_days", 1095, maximum=MAX_RETENTION_DAYS
        ),
        usage_hourly_retention_days=_nonnegative_int_value(
            raw, "usage_hourly_retention_days", 1500, maximum=MAX_RETENTION_DAYS
        ),
        usage_daily_retention_days=_nonnegative_int_value(
            raw, "usage_daily_retention_days", 0, maximum=MAX_RETENTION_DAYS
        ),
        usage_event_retention_days=_nonnegative_int_value(
            raw, "usage_event_retention_days", 365, maximum=MAX_RETENTION_DAYS
        ),
        lock_timeout_seconds=_float_value(
            raw, "lock_timeout_seconds", 10.0, maximum=MAX_LOCK_TIMEOUT_SECONDS
        ),
        backup_keep=_int_value(raw, "backup_keep", 10, maximum=MAX_BACKUP_KEEP),
        timeline_hours=_int_value(raw, "timeline_hours", 2, maximum=MAX_TIMELINE_HOURS),
        require_shared_memory=_bool_value(raw, "require_shared_memory", False),
        shared_memory_reserve_mb=_nonnegative_int_value(
            raw, "shared_memory_reserve_mb", 512, maximum=MAX_MEMORY_MB
        ),
        job_log_dir=job_log_dir,
        job_log_retention_days=_nonnegative_int_value(
            raw, "job_log_retention_days", 30, maximum=MAX_RETENTION_DAYS
        ),
        job_log_max_mb=_nonnegative_int_value(
            raw, "job_log_max_mb", 64, maximum=MAX_MEMORY_MB
        ),
        job_log_total_max_mb=_nonnegative_int_value(
            raw, "job_log_total_max_mb", 4096, maximum=MAX_MEMORY_MB
        ),
        worker_poll_seconds=_float_value(
            raw, "worker_poll_seconds", 1.0, maximum=MAX_WORKER_POLL_SECONDS
        ),
        worker_max_parallel=_int_value(
            raw,
            "worker_max_parallel",
            DEFAULT_WORKER_MAX_PARALLEL,
            maximum=MAX_WORKER_MAX_PARALLEL,
        ),
        worker_termination_grace_seconds=_float_value(
            raw,
            "worker_termination_grace_seconds",
            DEFAULT_WORKER_TERMINATION_GRACE_SECONDS,
            maximum=MAX_WORKER_TERMINATION_GRACE_SECONDS,
        ),
        worker_claim_timeout_seconds=_float_value(
            raw,
            "worker_claim_timeout_seconds",
            30.0,
            maximum=MAX_WORKER_CLAIM_TIMEOUT_SECONDS,
        ),
        worker_recovery_grace_seconds=_float_value(
            raw,
            "worker_recovery_grace_seconds",
            5.0,
            maximum=MAX_WORKER_RECOVERY_GRACE_SECONDS,
        ),
        worker_live_guard=_bool_value(raw, "worker_live_guard", True),
        monitor_interval_seconds=_float_value(
            raw,
            "monitor_interval_seconds",
            2.0,
            maximum=MAX_MONITOR_INTERVAL_SECONDS,
        ),
        monitor_rollup_seconds=_int_value(
            raw,
            "monitor_rollup_seconds",
            60,
            maximum=MAX_MONITOR_ROLLUP_SECONDS,
        ),
        tui_refresh_seconds=_float_value(
            raw,
            "tui_refresh_seconds",
            1.0,
            maximum=MAX_TUI_REFRESH_SECONDS,
        ),
        monitor_uid=validate_optional_uid(raw.get("monitor_uid"), "monitor_uid"),
        storage_gid=validate_optional_gid(raw.get("storage_gid"), "storage_gid"),
        config_owner_uid=config_owner_uid,
        file_mode=_mode_value(raw, "file_mode", DEFAULT_PRIVATE_FILE_MODE, directory=False),
        dir_mode=_mode_value(raw, "dir_mode", DEFAULT_PRIVATE_DIR_MODE, directory=True),
        allocator_command=allocator_command,
        allocator_timeout_seconds=_float_value(
            {
                **raw,
                "allocator_timeout_seconds": os.environ.get(
                    "BK_ALLOCATOR_TIMEOUT_SECONDS",
                    raw.get("allocator_timeout_seconds", 3.0),
                ),
            },
            "allocator_timeout_seconds",
            3.0,
            maximum=MAX_ALLOCATOR_TIMEOUT_SECONDS,
        ),
        allocator_weight=_float_value(
            {
                **raw,
                "allocator_weight": os.environ.get(
                    "BK_ALLOCATOR_WEIGHT",
                    raw.get("allocator_weight", 5.0),
                ),
            },
            "allocator_weight",
            5.0,
            maximum=MAX_ALLOCATOR_WEIGHT,
        ),
        broker_socket=broker_socket,
        broker_uid=validate_optional_uid(raw.get("broker_uid"), "broker_uid"),
        broker_gid=validate_optional_gid(raw.get("broker_gid"), "broker_gid"),
        broker_socket_mode=_mode_value(
            raw,
            "broker_socket_mode",
            0o600,
            directory=False,
        ),
        disabled_gpus=validate_gpu_list(
            raw.get("disabled_gpus"), gpu_count, "disabled_gpus"
        ),
        gpu_priority=validate_gpu_priority(raw.get("gpu_priority"), gpu_count),
    )


def _bounded_detected_gpu_count(value: int) -> int:
    if value < 1 or value > MAX_GPU_COUNT:
        raise ValueError(f"detected gpu_count must be between 1 and {MAX_GPU_COUNT}")
    return value


def _open_trusted_config_parent(path: Path) -> int:
    absolute = Path(os.path.realpath(os.path.abspath(os.fspath(path))))
    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    anchor = Path(absolute.anchor)
    fd = os.open(str(anchor), flags)
    try:
        _validate_trusted_config_directory(fd, anchor)
        cursor = anchor
        for component in absolute.parts[1:]:
            if not hasattr(os, "O_NOFOLLOW"):
                metadata = os.stat(component, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise OSError(f"refusing symbolic-link configuration directory: {cursor / component}")
            child_fd = os.open(component, flags, dir_fd=fd)
            try:
                cursor /= component
                _validate_trusted_config_directory(child_fd, cursor)
            except Exception:
                os.close(child_fd)
                raise
            os.close(fd)
            fd = child_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _validate_trusted_config_directory(fd: int, path: Path) -> None:
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"configuration path component is not a directory: {path}")
    if metadata.st_uid not in {0, os.getuid()}:
        raise PermissionError(
            f"configuration directory {path} must be owned by root or UID {os.getuid()}"
        )
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022 and not mode & stat.S_ISVTX:
        raise PermissionError(
            f"configuration directory {path} must not be writable by group or other users; "
            "use BK_CONFIG_FILE outside the shared data directory"
        )


def _canonical_config_file(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    return Path(os.path.realpath(absolute.parent)) / absolute.name


def _path_value(value: Any, key: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError(f"{key} must be a filesystem path")
    text = os.fspath(value)
    if not isinstance(text, str):
        raise ValueError(f"{key} must be a text filesystem path")
    if not text or "\x00" in text or len(text) > 4096:
        raise ValueError(f"{key} must be a non-empty path of at most 4096 characters")
    return Path(text).expanduser()


def _absolute_path_value(value: Any, key: str) -> Path:
    path = _path_value(value, key)
    if not Path(os.fspath(value)).is_absolute():
        raise ValueError(f"{key} must be an absolute filesystem path")
    return Path(os.path.abspath(os.fspath(path)))


def _absolute_user_path_value(value: Any, key: str) -> Path:
    path = _path_value(value, key)
    if not path.is_absolute():
        raise ValueError(f"{key} must be an absolute filesystem path")
    return Path(os.path.abspath(os.fspath(path)))


def _command_value(value: Any) -> Optional[Tuple[str, ...]]:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        argv = shlex.split(value)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        argv = list(value)
    else:
        raise ValueError("allocator_command must be a command string or string array")
    if not argv or not argv[0] or len(argv) > 64:
        raise ValueError("allocator_command must contain 1-64 arguments")
    if any("\x00" in item for item in argv):
        raise ValueError("allocator_command contains a NUL byte")
    if any(len(item.encode("utf-8")) > 4096 for item in argv):
        raise ValueError("allocator_command arguments must not exceed 4096 bytes")
    if sum(len(item.encode("utf-8")) + 1 for item in argv) > 64 * 1024:
        raise ValueError("allocator_command must not exceed 64 KiB")
    return tuple(argv)
