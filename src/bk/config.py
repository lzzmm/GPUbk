from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


DEFAULT_DATA_DIR = "/data2/shared/bk"


@dataclass(frozen=True)
class Config:
    data_dir: Path
    gpu_count: int = 1
    max_shared_users: int = 2
    queue_search_hours: int = 168
    lock_timeout_seconds: float = 10.0
    backup_keep: int = 10
    timeline_hours: int = 24


def _read_config_file(data_dir: Path) -> Dict[str, Any]:
    path = data_dir / "config.json"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return raw


def _int_value(raw: Dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if parsed < 1:
        raise ValueError(f"{key} must be >= 1")
    return parsed


def _float_value(raw: Dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{key} must be > 0")
    return parsed


def load_config() -> Config:
    data_dir = Path(os.environ.get("BK_DATA_DIR", DEFAULT_DATA_DIR)).expanduser()
    raw = _read_config_file(data_dir)

    env_map = {
        "gpu_count": "BK_GPU_COUNT",
        "max_shared_users": "BK_MAX_SHARED_USERS",
        "queue_search_hours": "BK_QUEUE_SEARCH_HOURS",
        "lock_timeout_seconds": "BK_LOCK_TIMEOUT_SECONDS",
        "backup_keep": "BK_BACKUP_KEEP",
        "timeline_hours": "BK_TIMELINE_HOURS",
    }
    for key, env_name in env_map.items():
        if env_name in os.environ:
            raw[key] = os.environ[env_name]

    return Config(
        data_dir=data_dir,
        gpu_count=_int_value(raw, "gpu_count", 1),
        max_shared_users=_int_value(raw, "max_shared_users", 2),
        queue_search_hours=_int_value(raw, "queue_search_hours", 168),
        lock_timeout_seconds=_float_value(raw, "lock_timeout_seconds", 10.0),
        backup_keep=_int_value(raw, "backup_keep", 10),
        timeline_hours=_int_value(raw, "timeline_hours", 24),
    )
