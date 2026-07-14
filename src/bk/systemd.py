from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from importlib import resources
from pathlib import Path
from typing import Mapping, Optional

from .config import CONFIG_ENV_MAP, Config
from .fileio import ensure_directory, fsync_directory, open_existing_regular
from .models import BookingError
from .userdirs import xdg_user_directory


UNITS = {
    "monitor": "bk-monitor.service",
    "worker": "bk-worker.service",
}
MANAGED_UNIT_MARKER = "# Managed by GPUBK; remove with `bk service uninstall`.\n"
SYSTEM_UNITS = {
    "broker": "gpubk-broker.service",
    "monitor": "gpubk-monitor.service",
}
SYSTEM_MANAGED_UNIT_MARKER = (
    "# Managed by GPUBK; use `bk admin services` for lifecycle changes.\n"
)
DEFAULT_SYSTEM_UNIT_DIR = Path("/etc/systemd/system")

_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TEMPLATE_MARKER = re.compile(r"@[A-Z][A-Z0-9_]*@")


def default_user_unit_dir() -> Path:
    config_home = xdg_user_directory("XDG_CONFIG_HOME", ".config")
    return config_home / "systemd" / "user"


def unit_text(
    kind: str,
    python_executable: Optional[Path] = None,
    *,
    environment: Optional[Mapping[str, str]] = None,
) -> str:
    filename = _unit_filename(kind)
    executable = Path(python_executable or sys.executable)
    if not executable.is_absolute():
        raise BookingError("Python executable for systemd must be an absolute path")
    template = resources.files("bk").joinpath("data", "systemd", filename).read_text(encoding="utf-8")
    environment_text = "\n".join(
        _systemd_environment_line(name, value)
        for name, value in sorted((environment or {}).items())
    )
    rendered = template.replace(
        "@PYTHON_EXECUTABLE@", _quote_systemd_argument(str(executable))
    ).replace("@SERVICE_ENVIRONMENT@", environment_text)
    return MANAGED_UNIT_MARKER + rendered


def system_unit_text(
    kind: str,
    *,
    service_uid: int,
    service_gid: int,
    config_file: Path,
    data_dir: Path,
    socket_directory: Path,
    python_executable: Optional[Path] = None,
) -> str:
    filename = _system_unit_filename(kind)
    if (
        isinstance(service_uid, bool)
        or not isinstance(service_uid, int)
        or service_uid <= 0
    ):
        raise BookingError("system service UID must be a positive integer")
    if (
        isinstance(service_gid, bool)
        or not isinstance(service_gid, int)
        or service_gid < 0
    ):
        raise BookingError("system service GID must be a non-negative integer")
    executable = _absolute_required_path(
        Path(python_executable or sys.executable), "Python executable"
    )
    trusted_config = _absolute_required_path(config_file, "configuration")
    state_directory = _absolute_required_path(data_dir, "data directory")
    runtime_directory = _absolute_required_path(
        socket_directory, "broker socket directory"
    )
    runtime_directives = ""
    try:
        runtime_relative = runtime_directory.relative_to("/run")
    except ValueError:
        pass
    else:
        if not runtime_relative.parts:
            raise BookingError("broker socket directory cannot be /run itself")
        runtime_name = "/".join(runtime_relative.parts)
        runtime_directives = (
            f"RuntimeDirectory={runtime_name}\n"
            "RuntimeDirectoryMode=0755\n"
            "RuntimeDirectoryPreserve=yes"
        )
    template = (
        resources.files("bk")
        .joinpath("data", "systemd", "system", filename)
        .read_text(encoding="utf-8")
    )
    replacements = {
        "@PYTHON_EXECUTABLE@": _quote_systemd_argument(str(executable)),
        "@SERVICE_UID@": str(service_uid),
        "@SERVICE_GID@": str(service_gid),
        "@CONFIG_ENVIRONMENT@": _systemd_environment_line(
            "BK_CONFIG_FILE", str(trusted_config)
        ),
        "@DATA_DIRECTORY@": _escape_systemd_path(str(state_directory)),
        "@SOCKET_DIRECTORY@": _escape_systemd_path(str(runtime_directory)),
        "@RUNTIME_DIRECTORY@": runtime_directives,
    }
    rendered = template
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    if _TEMPLATE_MARKER.search(rendered):
        raise BookingError(f"unresolved systemd template marker in {filename}")
    return SYSTEM_MANAGED_UNIT_MARKER + rendered

def system_unit_names() -> tuple[str, ...]:
    return tuple(SYSTEM_UNITS.values())


def install_user_unit(
    kind: str,
    target_dir: Optional[Path] = None,
    *,
    environment: Mapping[str, str],
    force: bool = False,
) -> Path:
    filename = _unit_filename(kind)
    directory = (target_dir or default_user_unit_dir()).expanduser()
    destination = directory / filename
    if os.path.lexists(destination) and not force:
        raise BookingError(f"systemd unit already exists: {destination}; pass --force to replace it")
    text = unit_text(kind, environment=environment)
    ensure_directory(directory, 0o755)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".tmp", dir=str(directory))
    temporary = Path(tmp_name)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, destination)
        fsync_directory(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)
    return destination


def uninstall_user_unit(kind: str, target_dir: Optional[Path] = None) -> Path:
    filename = _unit_filename(kind)
    directory = (target_dir or default_user_unit_dir()).expanduser()
    destination = directory / filename
    fd = open_existing_regular(destination, expected_mode=0o644)
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != os.geteuid():
            raise BookingError(
                f"systemd unit must be owned by the current UID: {destination}"
            )
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            text = handle.read(1024 * 1024 + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(text) > 1024 * 1024 or not text.startswith(MANAGED_UNIT_MARKER):
        raise BookingError(
            f"refusing to remove an unrecognized systemd unit: {destination}"
        )
    current = destination.lstat()
    if (
        not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
        or current.st_uid != metadata.st_uid
        or stat.S_IMODE(current.st_mode) != 0o644
    ):
        raise BookingError(f"systemd unit changed while being removed: {destination}")
    destination.unlink()
    fsync_directory(directory)
    return destination


def service_environment(
    config: Config,
    kind: str,
    *,
    process_environment: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    _unit_filename(kind)
    source_environment = os.environ if process_environment is None else process_environment
    environment = {
        "BK_DATA_DIR": str(_absolute_path(config.data_dir)),
        "PYTHONUNBUFFERED": "1",
    }
    if config.config_file is not None:
        environment["BK_CONFIG_FILE"] = str(_absolute_path(config.config_file))
    if kind == "worker" and config.job_log_dir is not None:
        environment["BK_JOB_LOG_DIR"] = str(_absolute_path(config.job_log_dir))
    for key, name in CONFIG_ENV_MAP.items():
        if name in source_environment:
            environment[name] = _config_environment_value(key, getattr(config, key))
    return environment


def _unit_filename(kind: str) -> str:
    try:
        return UNITS[kind]
    except KeyError as exc:
        raise BookingError(f"unknown service kind: {kind}") from exc


def _system_unit_filename(kind: str) -> str:
    try:
        return SYSTEM_UNITS[kind]
    except KeyError as exc:
        raise BookingError(f"unknown system service kind: {kind}") from exc


def _absolute_required_path(path: Path, label: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise BookingError(f"{label} path for systemd must be absolute")
    text = os.fspath(expanded)
    if any(character in text for character in ("\x00", "\n", "\r")):
        raise BookingError(f"invalid {label} path for systemd")
    return Path(os.path.abspath(text))


def _quote_systemd_argument(value: str) -> str:
    if not value or any(character in value for character in ("\x00", "\n", "\r")):
        raise BookingError("invalid Python executable path for systemd")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def _escape_systemd_path(value: str) -> str:
    if not value or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise BookingError("invalid path for systemd")
    escaped = []
    for character in value:
        if character == "%":
            escaped.append("%%")
        elif character in {" ", "\\", '"', "'"}:
            escaped.extend(f"\\x{byte:02x}" for byte in character.encode("utf-8"))
        else:
            escaped.append(character)
    return "".join(escaped)


def _systemd_environment_line(name: str, value: str) -> str:
    if not _ENVIRONMENT_NAME.fullmatch(name):
        raise BookingError(f"invalid systemd environment name: {name}")
    text = str(value)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        raise BookingError(f"invalid control character in systemd environment value: {name}")
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{name}={escaped}"'


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _config_environment_value(key: str, value: object) -> str:
    if key in {"file_mode", "dir_mode"}:
        return f"{int(value):04o}"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format(value, ".15g")
    return str(value)
