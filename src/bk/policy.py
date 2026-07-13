from __future__ import annotations

from typing import Optional, Tuple

from .config import Config
from .granularity import DEFAULT_SLOT_MINUTES
from .models import BookingError


# Backward-compatible default; runtime scheduling uses Config.slot_seconds.
BOOKING_GRANULARITY_SECONDS = DEFAULT_SLOT_MINUTES * 60
LEDGER_POLICY_VERSION = 1
LEDGER_POLICY_KEY = "policy"
STORAGE_GID_POLICY_KEY = "storage_gid"


def policy_for_config(config: Config) -> dict:
    policy = {
        "version": LEDGER_POLICY_VERSION,
        "gpu_count": config.gpu_count,
        "max_shared_reservations_per_gpu": config.max_shared_users,
        "granularity_seconds": config.slot_seconds,
        "require_shared_memory": config.require_shared_memory,
        "shared_memory_reserve_mb": config.shared_memory_reserve_mb,
        "file_mode": f"{config.file_mode:04o}",
        "dir_mode": f"{config.dir_mode:04o}",
    }
    if config.storage_gid is not None:
        policy[STORAGE_GID_POLICY_KEY] = config.storage_gid
    return policy


def bind_ledger_policy(ledger: dict, config: Config) -> bool:
    if ledger.get(LEDGER_POLICY_KEY) is None:
        ledger[LEDGER_POLICY_KEY] = policy_for_config(config)
        return True
    validate_ledger_policy(ledger, config)
    current = ledger[LEDGER_POLICY_KEY]
    if config.storage_gid is not None and STORAGE_GID_POLICY_KEY not in current:
        current[STORAGE_GID_POLICY_KEY] = config.storage_gid
        return True
    return False


def validate_ledger_policy(ledger: dict, config: Config) -> None:
    current = ledger.get(LEDGER_POLICY_KEY)
    if current is None:
        return
    if not isinstance(current, dict):
        raise BookingError("ledger policy must be a JSON object")

    expected = policy_for_config(config)
    mismatches = [
        f"{key}: ledger={current.get(key)!r} local={value!r}"
        for key, value in expected.items()
        if key != STORAGE_GID_POLICY_KEY and current.get(key) != value
    ]
    if (
        STORAGE_GID_POLICY_KEY in current
        and current[STORAGE_GID_POLICY_KEY] != config.storage_gid
    ):
        mismatches.append(
            f"{STORAGE_GID_POLICY_KEY}: ledger={current[STORAGE_GID_POLICY_KEY]!r} "
            f"local={config.storage_gid!r}"
        )
    if mismatches:
        raise BookingError("local configuration does not match ledger policy: " + "; ".join(mismatches))


def ledger_storage_modes(ledger: dict) -> Optional[Tuple[str, str]]:
    current = ledger.get(LEDGER_POLICY_KEY)
    if current is None:
        return None
    if not isinstance(current, dict):
        raise BookingError("ledger policy must be a JSON object")
    file_mode = current.get("file_mode")
    dir_mode = current.get("dir_mode")
    if not isinstance(file_mode, str) or not isinstance(dir_mode, str):
        raise BookingError("ledger policy storage modes are invalid")
    return file_mode, dir_mode
