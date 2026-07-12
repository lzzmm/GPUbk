from __future__ import annotations

import difflib
import json
import math
import os
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .fileio import open_existing_regular_at
from .granularity import DEFAULT_SLOT_MINUTES, slot_seconds, validate_slot_minutes


DEFAULT_PRIVATE_FILE_MODE = 0o600
DEFAULT_PRIVATE_DIR_MODE = 0o700
MAX_CONFIG_FILE_BYTES = 1024 * 1024
CONFIG_VERSION = 1
MAX_GPU_COUNT = 1024
MAX_SHARED_UNITS = 10_000
MAX_QUEUE_SEARCH_HOURS = 10 * 365 * 24
MAX_RETENTION_DAYS = 100 * 365
MAX_USAGE_LOAD_WINDOW_MINUTES = 365 * 24 * 60
MAX_LOCK_TIMEOUT_SECONDS = 60 * 60
MAX_BACKUP_KEEP = 10_000
MAX_TIMELINE_HOURS = 365 * 24
MAX_MEMORY_MB = 16 * 1024 * 1024
MAX_WORKER_POLL_SECONDS = 60 * 60
MAX_WORKER_CLAIM_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
MAX_WORKER_RECOVERY_GRACE_SECONDS = 24 * 60 * 60
MAX_ALLOCATOR_TIMEOUT_SECONDS = 5 * 60
MAX_ALLOCATOR_WEIGHT = 1_000_000
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
    "worker_claim_timeout_seconds": "BK_WORKER_CLAIM_TIMEOUT_SECONDS",
    "worker_recovery_grace_seconds": "BK_WORKER_RECOVERY_GRACE_SECONDS",
    "worker_live_guard": "BK_WORKER_LIVE_GUARD",
    "monitor_interval_seconds": "BK_MONITOR_INTERVAL_SECONDS",
    "monitor_rollup_seconds": "BK_MONITOR_ROLLUP_SECONDS",
    "tui_refresh_seconds": "BK_TUI_REFRESH_SECONDS",
    "file_mode": "BK_FILE_MODE",
    "dir_mode": "BK_DIR_MODE",
}
CONFIG_FILE_KEYS = frozenset(
    {
        "config_version",
        *CONFIG_ENV_MAP,
        "job_log_dir",
        "allocator_command",
        "allocator_timeout_seconds",
        "allocator_weight",
        "monitor_uid",
    }
)


@dataclass(frozen=True)
class Config:
    data_dir: Path
    gpu_count: int = 1
    max_shared_users: int = 2
    queue_search_hours: int = 168
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "slot_minutes", validate_slot_minutes(self.slot_minutes))
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
            "monitor_uid",
            validate_optional_uid(self.monitor_uid, "monitor_uid"),
        )
        object.__setattr__(
            self,
            "config_owner_uid",
            validate_optional_uid(self.config_owner_uid, "config_owner_uid"),
        )

    @property
    def slot_seconds(self) -> int:
        return slot_seconds(self.slot_minutes)

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


def _read_config_file(
    path: Path,
    *,
    required: bool = False,
) -> Tuple[Dict[str, Any], Optional[int]]:
    if not os.path.lexists(path):
        if required:
            raise FileNotFoundError(f"configured BK_CONFIG_FILE does not exist: {path}")
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
            f"{path}: unsupported config_version {version}; this GPUbk supports {CONFIG_VERSION}"
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
    data_home = _path_value(
        os.environ.get("XDG_DATA_HOME", "~/.local/share"),
        "XDG_DATA_HOME",
    )
    return data_home / "bk"


def _auto_gpu_count() -> int:
    from .gpu import detect_gpu_count

    return max(1, detect_gpu_count())


def load_config() -> Config:
    data_dir = (
        _path_value(os.environ["BK_DATA_DIR"], "BK_DATA_DIR")
        if "BK_DATA_DIR" in os.environ
        else _default_data_dir()
    )
    explicit_config_file = "BK_CONFIG_FILE" in os.environ
    config_file = (
        _canonical_config_file(_path_value(os.environ["BK_CONFIG_FILE"], "BK_CONFIG_FILE"))
        if explicit_config_file
        else data_dir / "config.json"
    )
    raw, config_owner_uid = _read_config_file(config_file, required=explicit_config_file)

    for key, env_name in CONFIG_ENV_MAP.items():
        if env_name in os.environ:
            raw[key] = os.environ[env_name]

    job_log_raw = os.environ.get("BK_JOB_LOG_DIR", raw.get("job_log_dir"))
    if job_log_raw is not None and job_log_raw != "":
        job_log_dir = _path_value(job_log_raw, "job_log_dir")
    else:
        state_home = _path_value(
            os.environ.get("XDG_STATE_HOME", "~/.local/state"),
            "XDG_STATE_HOME",
        )
        job_log_dir = state_home / "bk" / "jobs"
    allocator_raw = os.environ.get("BK_ALLOCATOR_COMMAND", raw.get("allocator_command"))
    allocator_command = _command_value(allocator_raw)
    gpu_count = (
        _int_value(raw, "gpu_count", 1, maximum=MAX_GPU_COUNT)
        if "gpu_count" in raw
        else _bounded_detected_gpu_count(_auto_gpu_count())
    )

    return Config(
        data_dir=data_dir,
        config_file=config_file if explicit_config_file else None,
        gpu_count=gpu_count,
        slot_minutes=validate_slot_minutes(raw.get("slot_minutes", DEFAULT_SLOT_MINUTES)),
        max_shared_users=_int_value(raw, "max_shared_users", 2, maximum=MAX_SHARED_UNITS),
        queue_search_hours=_int_value(
            raw, "queue_search_hours", 168, maximum=MAX_QUEUE_SEARCH_HOURS
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
