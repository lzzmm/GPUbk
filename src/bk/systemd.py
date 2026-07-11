from __future__ import annotations

import os
import sys
from importlib import resources
from pathlib import Path
from typing import Optional

from .models import BookingError


UNITS = {
    "monitor": "bk-monitor.service",
    "worker": "bk-worker.service",
}


def default_user_unit_dir() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return config_home / "systemd" / "user"


def unit_text(kind: str, python_executable: Optional[Path] = None) -> str:
    filename = _unit_filename(kind)
    executable = Path(python_executable or sys.executable)
    if not executable.is_absolute():
        raise BookingError("Python executable for systemd must be an absolute path")
    template = resources.files("bk").joinpath("data", "systemd", filename).read_text(encoding="utf-8")
    return template.replace("@PYTHON_EXECUTABLE@", _quote_systemd_argument(str(executable)))


def install_user_unit(kind: str, target_dir: Optional[Path] = None, *, force: bool = False) -> Path:
    filename = _unit_filename(kind)
    directory = (target_dir or default_user_unit_dir()).expanduser()
    destination = directory / filename
    if destination.exists() and not force:
        raise BookingError(f"systemd unit already exists: {destination}; pass --force to replace it")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{filename}.{os.getpid()}.tmp"
    try:
        temporary.write_text(unit_text(kind), encoding="utf-8")
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _unit_filename(kind: str) -> str:
    try:
        return UNITS[kind]
    except KeyError as exc:
        raise BookingError(f"unknown service kind: {kind}") from exc


def _quote_systemd_argument(value: str) -> str:
    if not value or any(character in value for character in ("\x00", "\n", "\r")):
        raise BookingError("invalid Python executable path for systemd")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'
