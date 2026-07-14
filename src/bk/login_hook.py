from __future__ import annotations

import hashlib
import os
import shlex
import stat
import tempfile
from pathlib import Path

from .fileio import fsync_directory, open_existing_regular
from .models import BookingError


DEFAULT_LOGIN_HOOK_PATH = Path("/etc/profile.d/gpubk.sh")
DEFAULT_LOGIN_EXECUTABLE = Path("/usr/local/bin/bk")
LOGIN_HOOK_MODE = 0o644
LOGIN_HOOK_MARKER = "# Managed by GPUBK login notice."
MAX_LOGIN_HOOK_BYTES = 16 * 1024


def render_login_hook(executable: Path = DEFAULT_LOGIN_EXECUTABLE) -> bytes:
    executable = _absolute_executable(executable)
    command = shlex.quote(str(executable))
    return (
        f"{LOGIN_HOOK_MARKER}\n"
        "# Remove with: sudo bk admin login-hook uninstall --yes\n"
        "# Never delay or block an interactive login.\n"
        "if [ -z \"${GPUBK_LOGIN_NOTICE_SHOWN:-}\" ] && [ -t 1 ] "
        f"&& [ -x {command} ] && command -v timeout >/dev/null 2>&1; then\n"
        "    GPUBK_LOGIN_NOTICE_SHOWN=1\n"
        "    export GPUBK_LOGIN_NOTICE_SHOWN\n"
        f"    timeout -k 0.2s 1s {command} login --hook 2>/dev/null || :\n"
        "fi\n"
    ).encode("utf-8")


def inspect_login_hook(
    path: Path = DEFAULT_LOGIN_HOOK_PATH,
    *,
    executable: Path = DEFAULT_LOGIN_EXECUTABLE,
    expected_owner: int = 0,
) -> dict:
    path = _absolute_path(path)
    desired = render_login_hook(executable)
    state = _read_state(path, expected_owner=expected_owner)
    status = "absent"
    if state["exists"]:
        if state.get("error"):
            status = "unsafe"
        elif not state["managed"]:
            status = "unmanaged"
        elif state["content"] == desired:
            status = "installed"
        else:
            status = "update-available"
    blockers = []
    try:
        _validate_parent(path.parent, expected_owner=expected_owner)
    except BookingError as exc:
        blockers.append(str(exc))
    if status == "unsafe":
        blockers.append(str(state["error"]))
    elif status == "unmanaged":
        blockers.append(f"refusing to replace an unmanaged login file: {path}")
    return {
        "kind": "admin-login-hook",
        "path": str(path),
        "executable": str(_absolute_executable(executable)),
        "status": status,
        "changed": status not in {"installed"},
        "managed": bool(state.get("managed")),
        "blockers": blockers,
    }


def apply_login_hook_install(
    path: Path = DEFAULT_LOGIN_HOOK_PATH,
    *,
    executable: Path = DEFAULT_LOGIN_EXECUTABLE,
    require_root: bool = True,
) -> dict:
    expected_owner = _required_owner(require_root, "install")
    path = _absolute_path(path)
    inspection = inspect_login_hook(
        path,
        executable=executable,
        expected_owner=expected_owner,
    )
    if inspection["blockers"]:
        raise BookingError("; ".join(inspection["blockers"]))
    if inspection["status"] == "installed":
        return {**inspection, "changed": False}

    payload = render_login_hook(executable)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, LOGIN_HOOK_MODE)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        current = inspect_login_hook(
            path,
            executable=executable,
            expected_owner=expected_owner,
        )
        if current["status"] != inspection["status"]:
            raise BookingError("login hook changed during installation; retry")
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)
    return {
        **inspect_login_hook(
            path,
            executable=executable,
            expected_owner=expected_owner,
        ),
        "changed": True,
    }


def apply_login_hook_uninstall(
    path: Path = DEFAULT_LOGIN_HOOK_PATH,
    *,
    require_root: bool = True,
) -> dict:
    expected_owner = _required_owner(require_root, "uninstall")
    path = _absolute_path(path)
    inspection = inspect_login_hook(path, expected_owner=expected_owner)
    if inspection["status"] == "absent":
        return {**inspection, "changed": False}
    if inspection["blockers"] or not inspection["managed"]:
        blockers = inspection["blockers"] or [f"refusing to remove unmanaged file: {path}"]
        raise BookingError("; ".join(blockers))

    fd = open_existing_regular(path, expected_mode=LOGIN_HOOK_MODE)
    try:
        metadata = os.fstat(fd)
        payload = _read_fd(fd)
        if metadata.st_uid != expected_owner or metadata.st_nlink != 1:
            raise BookingError(f"login hook ownership or link count is unsafe: {path}")
        if not payload.startswith((LOGIN_HOOK_MARKER + "\n").encode("utf-8")):
            raise BookingError(f"refusing to remove unmanaged file: {path}")
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise BookingError("login hook changed during removal; retry")
        path.unlink()
        fsync_directory(path.parent)
    finally:
        os.close(fd)
    return {**inspection, "status": "absent", "changed": True}


def _read_state(path: Path, *, expected_owner: int) -> dict:
    if not os.path.lexists(path):
        return {"exists": False, "managed": False, "content": b""}
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise BookingError(f"login hook must be a regular file, not a link: {path}")
        if metadata.st_uid != expected_owner:
            raise BookingError(f"login hook must be owned by UID {expected_owner}: {path}")
        if metadata.st_nlink != 1:
            raise BookingError(f"login hook must have exactly one hard link: {path}")
        if stat.S_IMODE(metadata.st_mode) != LOGIN_HOOK_MODE:
            raise BookingError(f"login hook mode must be {LOGIN_HOOK_MODE:04o}: {path}")
        if metadata.st_size > MAX_LOGIN_HOOK_BYTES:
            raise BookingError(f"login hook is unexpectedly large: {path}")
        fd = open_existing_regular(path, expected_mode=LOGIN_HOOK_MODE)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise BookingError("login hook changed while being inspected; retry")
            content = _read_fd(fd)
        finally:
            os.close(fd)
        managed = content.startswith((LOGIN_HOOK_MARKER + "\n").encode("utf-8"))
        return {
            "exists": True,
            "managed": managed,
            "content": content,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    except (BookingError, OSError) as exc:
        return {"exists": True, "managed": False, "content": b"", "error": str(exc)}


def _read_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    remaining = MAX_LOGIN_HOOK_BYTES + 1
    while remaining:
        chunk = os.read(fd, min(8192, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > MAX_LOGIN_HOOK_BYTES:
        raise BookingError("login hook is unexpectedly large")
    return payload


def _required_owner(require_root: bool, action: str) -> int:
    if require_root and os.geteuid() != 0:
        raise BookingError(
            f"login hook {action} must run as root; use sudo bk admin login-hook {action}"
        )
    return 0 if require_root else os.geteuid()


def _validate_parent(path: Path, *, expected_owner: int) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise BookingError(f"login hook directory does not exist: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise BookingError(f"login hook parent must not be a symbolic link: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise BookingError(f"login hook parent is not a real directory: {path}")
    if metadata.st_uid != expected_owner:
        raise BookingError(f"login hook directory must be owned by UID {expected_owner}: {path}")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise BookingError(f"login hook directory must not be group/world writable: {path}")


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise BookingError("login hook path must be absolute")
    return expanded


def _absolute_executable(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or "\n" in str(expanded) or "\r" in str(expanded):
        raise BookingError("login hook executable must be an absolute path")
    return expanded
