from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


def open_existing_regular(
    path: Path,
    flags: int = os.O_RDONLY,
    *,
    expected_mode: int | None = None,
    expected_gid: int | None = None,
) -> int:
    if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():
        raise OSError(errno.ELOOP, f"refusing symbolic link: {path}")
    fd = os.open(str(path), _secure_flags(flags))
    try:
        _validate_regular_fd(
            fd,
            path,
            expected_mode=expected_mode,
            expected_gid=expected_gid,
        )
        return fd
    except Exception:
        os.close(fd)
        raise


def open_existing_regular_at(
    directory_fd: int,
    name: str,
    display_path: Path,
    flags: int = os.O_RDONLY,
    *,
    expected_mode: int | None = None,
    expected_gid: int | None = None,
) -> int:
    if not name or name in {".", ".."} or os.sep in name or (os.altsep and os.altsep in name):
        raise ValueError("name must be a single path component")
    if not hasattr(os, "O_NOFOLLOW"):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError(errno.ELOOP, f"refusing symbolic link: {display_path}")
    fd = os.open(name, _secure_flags(flags), dir_fd=directory_fd)
    try:
        _validate_regular_fd(
            fd,
            display_path,
            expected_mode=expected_mode,
            expected_gid=expected_gid,
        )
        return fd
    except Exception:
        os.close(fd)
        raise


def open_or_create_regular(
    path: Path,
    flags: int,
    mode: int,
    *,
    expected_gid: int | None = None,
) -> int:
    secure_flags = _secure_flags(flags)
    created = False
    try:
        fd = os.open(str(path), secure_flags | os.O_CREAT | os.O_EXCL, mode)
        created = True
    except FileExistsError:
        if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():
            raise OSError(errno.ELOOP, f"refusing symbolic link: {path}")
        fd = os.open(str(path), secure_flags)
    try:
        if created:
            _validate_regular_fd(fd, path)
            os.fchmod(fd, mode)
        _validate_regular_fd(fd, path, expected_mode=mode, expected_gid=expected_gid)
        return fd
    except Exception:
        os.close(fd)
        raise


def ensure_directory(
    path: Path,
    mode: int,
    *,
    require_mode: bool = False,
    expected_gid: int | None = None,
) -> None:
    """Create a real directory tree and optionally enforce the leaf mode.

    Newly created intermediate directories receive the requested mode too. Existing
    ancestors outside the requested leaf are only type-checked.
    """
    missing = []
    cursor = path
    while True:
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise
            cursor = parent
            continue
        _validate_directory_metadata(metadata, cursor)
        break

    for directory in reversed(missing):
        created = False
        try:
            os.mkdir(directory, mode)
            created = True
        except FileExistsError:
            pass
        fd = _open_directory(directory)
        try:
            if created:
                os.fchmod(fd, mode)
            _validate_directory_fd(
                fd,
                directory,
                expected_mode=mode,
                expected_gid=expected_gid,
            )
        finally:
            os.close(fd)

    if not missing and (require_mode or expected_gid is not None):
        fd = _open_directory(path)
        try:
            _validate_directory_fd(
                fd,
                path,
                expected_mode=mode if require_mode else None,
                expected_gid=expected_gid,
            )
        finally:
            os.close(fd)


def setgid_directory_gid(
    path: Path,
    mode: int,
    *,
    expected_gid: int | None = None,
) -> int | None:
    if not mode & stat.S_ISGID:
        return None
    fd = _open_directory(path)
    try:
        metadata = os.fstat(fd)
        _validate_directory_fd(fd, path, expected_mode=mode)
        if expected_gid is not None and metadata.st_gid != expected_gid:
            raise PermissionError(
                errno.EPERM,
                f"refusing data directory with GID {metadata.st_gid}; "
                f"expected configured storage_gid {expected_gid}: {path}",
            )
        return metadata.st_gid
    finally:
        os.close(fd)


def fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():
        raise OSError(errno.ELOOP, f"refusing symbolic link: {path}")
    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    fd = os.open(str(path), flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, f"refusing non-directory path: {path}")
        os.fsync(fd)
    finally:
        os.close(fd)


def file_type_name(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symbolic-link"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular-file"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "character-device"
    if stat.S_ISBLK(mode):
        return "block-device"
    return "other"


def _secure_flags(flags: int) -> int:
    for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, name, 0)
    return flags


def _validate_regular_fd(
    fd: int,
    path: Path,
    *,
    expected_mode: int | None = None,
    expected_gid: int | None = None,
) -> int:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(errno.EINVAL, f"refusing non-regular file: {path}")
    if metadata.st_nlink != 1:
        raise OSError(
            errno.EMLINK,
            f"refusing regular file with {metadata.st_nlink} hard links: {path}",
        )
    if expected_mode is not None:
        actual_mode = stat.S_IMODE(metadata.st_mode)
        if actual_mode != expected_mode:
            raise PermissionError(
                errno.EPERM,
                f"refusing file with mode {actual_mode:04o}; expected {expected_mode:04o}: {path}",
            )
    if expected_gid is not None and metadata.st_gid != expected_gid:
        raise PermissionError(
            errno.EPERM,
            f"refusing file with GID {metadata.st_gid}; expected {expected_gid}: {path}",
        )
    return fd


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():
        raise OSError(errno.ELOOP, f"refusing symbolic link: {path}")
    return os.open(str(path), flags)


def _validate_directory_metadata(metadata: os.stat_result, path: Path) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(
            errno.ENOTDIR,
            f"refusing non-directory path: {path}",
            str(path),
        )


def _validate_directory_fd(
    fd: int,
    path: Path,
    *,
    expected_mode: int | None = None,
    expected_gid: int | None = None,
) -> None:
    metadata = os.fstat(fd)
    _validate_directory_metadata(metadata, path)
    if expected_mode is not None:
        actual_mode = stat.S_IMODE(metadata.st_mode)
        if actual_mode != expected_mode:
            raise PermissionError(
                errno.EPERM,
                f"refusing directory with mode {actual_mode:04o}; expected {expected_mode:04o}: {path}",
            )
    if expected_gid is not None and metadata.st_gid != expected_gid:
        raise PermissionError(
            errno.EPERM,
            f"refusing directory with GID {metadata.st_gid}; expected {expected_gid}: {path}",
        )
