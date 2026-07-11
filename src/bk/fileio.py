from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


def open_existing_regular(path: Path, flags: int = os.O_RDONLY) -> int:
    if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():
        raise OSError(errno.ELOOP, f"refusing symbolic link: {path}")
    fd = os.open(str(path), _secure_flags(flags))
    try:
        _validate_regular_fd(fd, path)
        return fd
    except Exception:
        os.close(fd)
        raise


def open_or_create_regular(path: Path, flags: int, mode: int) -> int:
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
        _validate_regular_fd(fd, path)
        if created:
            os.fchmod(fd, mode)
        return fd
    except Exception:
        os.close(fd)
        raise


def ensure_directory(path: Path, mode: int) -> None:
    try:
        path.mkdir(parents=True, mode=mode)
    except FileExistsError:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return ensure_directory(path, mode)
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, f"refusing non-directory path: {path}", str(path))
        return
    path.chmod(mode)


def _secure_flags(flags: int) -> int:
    for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, name, 0)
    return flags


def _validate_regular_fd(fd: int, path: Path) -> int:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(errno.EINVAL, f"refusing non-regular file: {path}")
    return fd
