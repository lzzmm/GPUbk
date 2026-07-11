from __future__ import annotations

import os
import shutil
import uuid
from importlib import resources
from pathlib import Path
from typing import Optional

from .models import BookingError


SKILL_NAME = "bk-gpu-scheduler"


def default_skill_path() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    return codex_home / "skills" / SKILL_NAME


def skill_text() -> str:
    return _skill_resource().joinpath("SKILL.md").read_text(encoding="utf-8")


def install_skill(target: Optional[Path] = None, *, force: bool = False) -> Path:
    destination = (target or default_skill_path()).expanduser()
    source = _skill_resource()
    if destination.exists():
        if not force:
            raise BookingError(f"skill already exists: {destination}; pass --force to replace it")
        _verify_existing_skill(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{SKILL_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        _copy_resource_tree(source, temporary)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
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


def _verify_existing_skill(destination: Path) -> None:
    marker = destination / "SKILL.md"
    if destination.name != SKILL_NAME or not marker.is_file():
        raise BookingError(f"refusing to replace an unrecognized directory: {destination}")
    header = marker.read_text(encoding="utf-8", errors="replace")[:512]
    if f"name: {SKILL_NAME}" not in header:
        raise BookingError(f"refusing to replace an unrecognized skill: {destination}")
