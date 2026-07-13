from __future__ import annotations

import os
import shutil
import stat
import uuid
from importlib import resources
from pathlib import Path
from typing import Optional

from .models import BookingError
from .userdirs import xdg_user_directory


SKILL_NAME = "gpubk"


def default_skill_path() -> Path:
    codex_home = xdg_user_directory("CODEX_HOME", ".codex")
    return codex_home / "skills" / SKILL_NAME


def skill_text() -> str:
    return _skill_resource().joinpath("SKILL.md").read_text(encoding="utf-8")


def install_skill(target: Optional[Path] = None, *, force: bool = False) -> Path:
    destination = _absolute_skill_path(target or default_skill_path())
    source = _skill_resource()
    replacing = os.path.lexists(destination)
    if replacing:
        if not force:
            raise BookingError(f"skill already exists: {destination}; pass --force to replace it")
        _verify_existing_skill(destination)
        _refuse_active_working_tree(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    temporary = destination.parent / f".{SKILL_NAME}.{token}.tmp"
    backup = destination.parent / f".{SKILL_NAME}.{token}.backup"
    try:
        _copy_resource_tree(source, temporary)
        if replacing:
            os.replace(destination, backup)
        try:
            os.replace(temporary, destination)
        except BaseException as install_error:
            _restore_replaced_skill(destination, backup, install_error)
            raise
        if replacing:
            try:
                _remove_staged_path(backup)
            except OSError as exc:
                raise BookingError(
                    f"skill installed but the previous copy could not be removed: {backup}"
                ) from exc
    finally:
        _remove_staged_path(temporary)
    return destination


def _skill_resource():
    return resources.files("bk").joinpath("data", "codex-skill", SKILL_NAME)


def _copy_resource_tree(source, destination: Path) -> None:
    destination.mkdir(mode=0o755)
    for item in source.iterdir():
        target = destination / item.name
        if item.is_dir():
            _copy_resource_tree(item, target)
        else:
            target.write_bytes(item.read_bytes())
            target.chmod(0o644)


def _absolute_skill_path(path: Path) -> Path:
    try:
        expanded = Path(path).expanduser()
        raw = os.fspath(expanded)
        if "\x00" in raw:
            raise ValueError("embedded null byte")
        return Path(os.path.abspath(raw))
    except (OSError, RuntimeError, ValueError) as exc:
        raise BookingError(f"invalid skill destination: {path}") from exc


def _refuse_active_working_tree(destination: Path) -> None:
    try:
        current = Path.cwd().resolve(strict=True)
        resolved = destination.resolve(strict=True)
    except OSError as exc:
        raise BookingError("cannot verify the active working directory before replacement") from exc
    if resolved == current or resolved in current.parents:
        raise BookingError(
            "refusing to replace the current working directory or one of its ancestors; "
            "run the command from outside the installed skill"
        )


def _restore_replaced_skill(
    destination: Path,
    backup: Path,
    install_error: BaseException,
) -> None:
    if not os.path.lexists(backup):
        return
    if os.path.lexists(destination):
        raise BookingError(
            f"skill replacement failed; the previous copy is retained at {backup}"
        ) from install_error
    try:
        os.replace(backup, destination)
    except BaseException as rollback_error:
        raise BookingError(
            f"skill replacement and rollback failed ({rollback_error}); "
            f"the previous copy may remain at {backup}"
        ) from install_error


def _remove_staged_path(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(metadata.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _verify_existing_skill(destination: Path) -> None:
    try:
        metadata = destination.lstat()
    except OSError as exc:
        raise BookingError(f"cannot inspect existing skill: {destination}") from exc
    marker = destination / "SKILL.md"
    try:
        marker_metadata = marker.lstat()
    except OSError:
        marker_metadata = None
    if (
        destination.name != SKILL_NAME
        or not stat.S_ISDIR(metadata.st_mode)
        or marker_metadata is None
        or not stat.S_ISREG(marker_metadata.st_mode)
    ):
        raise BookingError(f"refusing to replace an unrecognized directory: {destination}")
    header = marker.read_text(encoding="utf-8", errors="replace")[:512]
    if f"name: {SKILL_NAME}" not in header:
        raise BookingError(f"refusing to replace an unrecognized skill: {destination}")
