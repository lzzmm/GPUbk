from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

from .fileio import fsync_directory
from .models import MODE_EXCLUSIVE, MODE_SHARED
from .storage import FileLock
from .timeparse import parse_iso, to_iso, utc_now
from .userdirs import xdg_user_directory


PRESET_SCHEMA_VERSION = "gpubk.presets.v1"
PRESET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
MAX_PRESETS = 128
MAX_HISTORY_RECORDS = 100
SUGGESTION_THRESHOLD = 3
PRESET_LOCK_TIMEOUT_SECONDS = 5.0


def preset_path() -> Path:
    return xdg_user_directory("XDG_CONFIG_HOME", ".config") / "gpubk" / "presets.json"


def load_preset_document(path: Optional[Path] = None) -> dict:
    target = path or preset_path()
    if not target.exists():
        return _empty_document()
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"preset file is not a regular file: {target}")
    metadata = target.stat()
    if metadata.st_uid != os.geteuid():
        raise ValueError(f"preset file is not owned by the current UID: {target}")
    if metadata.st_nlink != 1:
        raise ValueError(f"preset file must not have hard links: {target}")
    if metadata.st_mode & 0o077:
        raise ValueError(f"preset file permissions must be 0600 or stricter: {target}")
    if metadata.st_size > 1024 * 1024:
        raise ValueError("preset file exceeds 1 MiB")
    document = json.loads(target.read_text(encoding="utf-8"))
    return validate_preset_document(document)


def save_preset(preset: dict, path: Optional[Path] = None) -> dict:
    target = path or preset_path()
    _prepare_directory(target.parent)
    with FileLock(_lock_path(target), PRESET_LOCK_TIMEOUT_SECONDS):
        document = load_preset_document(target)
        normalized = validate_preset(preset)
        presets = [
            item
            for item in document["presets"]
            if item["name"] != normalized["name"]
        ]
        if len(presets) >= MAX_PRESETS:
            raise ValueError(f"at most {MAX_PRESETS} presets may be stored")
        now = to_iso(utc_now())
        previous = next(
            (
                item
                for item in document["presets"]
                if item["name"] == normalized["name"]
            ),
            None,
        )
        normalized = {
            **normalized,
            "created_at": previous.get("created_at", now) if previous else now,
            "updated_at": now,
        }
        document["presets"] = sorted(
            [*presets, normalized], key=lambda item: item["name"]
        )
        _write_document(target, document)
        return normalized


def delete_preset(name: str, path: Optional[Path] = None) -> bool:
    name = validate_preset_name(name)
    target = path or preset_path()
    _prepare_directory(target.parent)
    with FileLock(_lock_path(target), PRESET_LOCK_TIMEOUT_SECONDS):
        document = load_preset_document(target)
        remaining = [item for item in document["presets"] if item["name"] != name]
        if len(remaining) == len(document["presets"]):
            return False
        document["presets"] = remaining
        _write_document(target, document)
        return True


def get_preset(name: str, path: Optional[Path] = None) -> dict:
    name = validate_preset_name(name)
    for preset in load_preset_document(path)["presets"]:
        if preset["name"] == name:
            return preset
    raise ValueError(f"preset not found: {name}")


def learned_profile(ledger: dict, uid: int) -> Optional[dict]:
    records = []
    for reservation in ledger.get("reservations", []):
        try:
            if int(reservation.get("uid", -1)) != uid:
                continue
            profile = profile_from_reservation(reservation)
            created = parse_iso(reservation.get("created_at", reservation["start_at"]))
        except (KeyError, TypeError, ValueError):
            continue
        records.append((created, profile))
    records.sort(key=lambda item: item[0], reverse=True)
    records = records[:MAX_HISTORY_RECORDS]
    if not records:
        return None
    signatures = [profile_signature(profile) for _created, profile in records]
    counts = Counter(signatures)
    winner, count = max(
        counts.items(),
        key=lambda item: (
            item[1],
            -signatures.index(item[0]),
        ),
    )
    if count < SUGGESTION_THRESHOLD:
        return None
    profile = next(profile for _created, profile in records if profile_signature(profile) == winner)
    return {**profile, "observations": count, "signature": winner}


def preset_suggestion(ledger: dict, uid: int, presets: Iterable[dict]) -> Optional[dict]:
    learned = learned_profile(ledger, uid)
    if learned is None:
        return None
    signatures = {learned_profile_signature(item) for item in presets}
    if learned["signature"] in signatures:
        return None
    return {**learned, "name": suggested_name(learned)}


def learned_profile_signature(profile: dict) -> str:
    normalized = validate_profile(profile)
    learned_fields = {
        **normalized,
        "preferred_gpus": None,
        "excluded_gpus": [],
    }
    return profile_signature(learned_fields)


def profile_from_reservation(reservation: dict) -> dict:
    start = parse_iso(reservation["start_at"])
    end = parse_iso(reservation["end_at"])
    duration = int((end - start).total_seconds())
    mode = str(reservation.get("mode", MODE_SHARED))
    profile = {
        "mode": mode,
        "count": len(reservation.get("gpus", [])),
        "duration_seconds": duration,
        "expected_memory_mb": reservation.get("expected_memory_mb"),
        "share_units": reservation.get("share_units", 1) if mode == MODE_SHARED else None,
        "preferred_gpus": None,
        "excluded_gpus": [],
    }
    return validate_profile(profile)


def profile_signature(profile: dict) -> str:
    normalized = validate_profile(profile)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def suggested_name(profile: dict) -> str:
    mode = "x" if profile["mode"] == MODE_EXCLUSIVE else "s"
    minutes = profile["duration_seconds"] // 60
    duration = f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"
    return f"{mode}{profile['count']}x-{duration}"[:32]


def validate_preset_document(value: object) -> dict:
    if not isinstance(value, dict) or value.get("schema_version") != PRESET_SCHEMA_VERSION:
        raise ValueError("unsupported preset file schema")
    raw_presets = value.get("presets")
    if not isinstance(raw_presets, list) or len(raw_presets) > MAX_PRESETS:
        raise ValueError("preset file contains an invalid preset list")
    presets = [validate_preset(item) for item in raw_presets]
    names = [item["name"] for item in presets]
    if len(names) != len(set(names)):
        raise ValueError("preset names must be unique")
    return {"schema_version": PRESET_SCHEMA_VERSION, "presets": presets}


def validate_preset(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("preset must be an object")
    result = {"name": validate_preset_name(value.get("name")), **validate_profile(value)}
    for field in ("created_at", "updated_at"):
        raw = value.get(field)
        if raw is not None:
            result[field] = to_iso(parse_iso(str(raw)))
    return result


def validate_profile(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("preset profile must be an object")
    mode = value.get("mode", MODE_SHARED)
    if mode not in {MODE_SHARED, MODE_EXCLUSIVE}:
        raise ValueError("preset mode must be shared or exclusive")
    count = _positive_int(value.get("count"), "preset count", maximum=1024)
    duration = _positive_int(
        value.get("duration_seconds"), "preset duration_seconds", maximum=10 * 365 * 86400
    )
    memory = value.get("expected_memory_mb")
    if memory is not None:
        memory = _positive_int(memory, "preset expected_memory_mb", maximum=16 * 1024 * 1024)
    share = value.get("share_units")
    if mode == MODE_SHARED:
        share = _positive_int(share or 1, "preset share_units", maximum=10_000)
    elif share is not None:
        raise ValueError("exclusive presets cannot set share_units")
    preferred = _gpu_list(value.get("preferred_gpus"), "preferred_gpus", allow_none=True)
    excluded = _gpu_list(value.get("excluded_gpus"), "excluded_gpus", allow_none=False)
    if preferred is not None and len(preferred) != count:
        raise ValueError("preset preferred_gpus must match preset count")
    if preferred is not None and set(preferred) & set(excluded):
        raise ValueError("preset GPUs cannot be both preferred and excluded")
    return {
        "mode": mode,
        "count": count,
        "duration_seconds": duration,
        "expected_memory_mb": memory,
        "share_units": share,
        "preferred_gpus": preferred,
        "excluded_gpus": excluded,
    }


def validate_preset_name(value: object) -> str:
    if not isinstance(value, str) or PRESET_NAME.fullmatch(value) is None:
        raise ValueError("preset name must be 1-32 letters, digits, dots, dashes, or underscores")
    return value


def _gpu_list(value: object, key: str, *, allow_none: bool) -> Optional[list[int]]:
    if value is None and allow_none:
        return None
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"preset {key} must be an array")
    result = []
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw >= 1024:
            raise ValueError(f"preset {key} must contain GPU indexes")
        result.append(raw)
    if len(result) != len(set(result)):
        raise ValueError(f"preset {key} contains duplicates")
    return sorted(result)


def _positive_int(value: object, key: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
        raise ValueError(f"{key} must be between 1 and {maximum}")
    return value


def _empty_document() -> dict:
    return {"schema_version": PRESET_SCHEMA_VERSION, "presets": []}


def _write_document(path: Path, document: dict) -> None:
    validated = validate_preset_document(document)
    _prepare_directory(path.parent)
    if path.exists() and path.is_symlink():
        raise ValueError(f"preset file must not be a symlink: {path}")
    payload = (json.dumps(validated, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".presets.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _prepare_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"preset directory must not be a symlink: {path}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"preset directory is not a regular directory: {path}")
    if path.stat().st_uid != os.geteuid():
        raise ValueError(f"preset directory is not owned by the current UID: {path}")
    path.chmod(0o700)


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")
