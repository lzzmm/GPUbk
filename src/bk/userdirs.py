from __future__ import annotations

import os
import pwd
from pathlib import Path
from typing import Mapping, Optional


MAX_USER_DIRECTORY_BYTES = 4096


def xdg_user_directory(
    variable: str,
    home_relative_default: str,
    *,
    environment: Optional[Mapping[str, str]] = None,
) -> Path:
    """Return an absolute XDG directory, ignoring empty or relative XDG values."""
    source = os.environ if environment is None else environment
    raw = source.get(variable)
    if raw is not None and not isinstance(raw, str):
        raise ValueError(f"{variable} must be a filesystem path")
    if raw:
        if Path(raw).is_absolute():
            return _environment_path(raw, variable)
    return _absolute_home(source) / home_relative_default


def _absolute_home(environment: Mapping[str, str]) -> Path:
    raw = environment.get("HOME")
    if raw is not None and not isinstance(raw, str):
        raise ValueError("HOME must be a filesystem path")
    if raw:
        if Path(raw).is_absolute():
            return _environment_path(raw, "HOME")
    try:
        candidate = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError) as exc:
        raise ValueError("cannot determine an absolute user home directory") from exc
    if not candidate.is_absolute():
        raise ValueError("account home directory must be absolute")
    return candidate


def _environment_path(raw: object, variable: str) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError(f"{variable} must be a non-empty filesystem path")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{variable} must be valid UTF-8 text") from exc
    if len(encoded) > MAX_USER_DIRECTORY_BYTES:
        raise ValueError(f"{variable} must not exceed {MAX_USER_DIRECTORY_BYTES} bytes")
    return Path(raw)
