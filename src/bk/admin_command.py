from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .fileio import fsync_directory
from .models import BookingError


COMMAND_LINK_SCHEMA_VERSION = "gpubk.command-link.v1"
PHASE_INSTALLING = "installing"
PHASE_INSTALLED = "installed"


@dataclass(frozen=True)
class CommandLinkPlan:
    document: dict
    status: str
    blockers: tuple[str, ...]

    def public_document(self) -> dict:
        return {
            "schema_version": COMMAND_LINK_SCHEMA_VERSION,
            "kind": "admin-command-link",
            "status": "blocked" if self.blockers else "ready",
            "phase": self.document["phase"],
            "destination": self.document["destination"],
            "target": self.document["target"],
            "owned": self.document["owned"],
            "current": self.status,
            "blockers": list(self.blockers),
        }


def plan_command_link_install(
    *,
    existing: object,
    destination: Path,
    target: Path,
    expected_owner: int,
) -> CommandLinkPlan:
    destination = _absolute_path(destination, "command link")
    target = _absolute_path(target, "command target")
    _validate_parent(destination.parent, expected_owner)
    _validate_target(target, expected_owner)

    if existing is None:
        link_state, detail = _link_state(destination, target)
        if link_state == "absent":
            owned = True
            blockers: tuple[str, ...] = ()
        elif link_state == "target":
            owned = False
            blockers = ()
        else:
            owned = False
            blockers = (detail,)
        document = {
            "schema_version": COMMAND_LINK_SCHEMA_VERSION,
            "phase": PHASE_INSTALLING,
            "destination": str(destination),
            "target": str(target),
            "owned": owned,
        }
    else:
        previous = validate_command_link_document(existing)
        if previous["destination"] != str(destination):
            raise BookingError(
                "tracked command link destination differs; uninstall before changing it"
            )
        if previous["target"] != str(target):
            raise BookingError(
                "tracked command target differs; keep a stable installation path or uninstall first"
            )
        document = {**previous, "phase": PHASE_INSTALLING}
        link_state, detail = _link_state(destination, target)
        blockers = _install_blockers(document, link_state, detail)

    return CommandLinkPlan(
        document=validate_command_link_document(document),
        status=_public_status(link_state, document["owned"]),
        blockers=blockers,
    )


def apply_command_link_install(document: object, *, expected_owner: int) -> dict:
    document = validate_command_link_document(document)
    if document["phase"] not in {PHASE_INSTALLING, PHASE_INSTALLED}:
        raise BookingError("command link document is not installable")
    destination = Path(document["destination"])
    target = Path(document["target"])
    _validate_parent(destination.parent, expected_owner)
    _validate_target(target, expected_owner)
    status, detail = _link_state(destination, target)
    blockers = _install_blockers(document, status, detail)
    if blockers:
        raise BookingError("; ".join(blockers))
    if status == "absent":
        try:
            os.symlink(target, destination)
        except FileExistsError as exc:
            raise BookingError(
                f"command link changed while being installed: {destination}"
            ) from exc
        fsync_directory(destination.parent)
    status, detail = _link_state(destination, target)
    if status != "target":
        raise BookingError(detail or f"command link installation did not converge: {destination}")
    return validate_command_link_document({**document, "phase": PHASE_INSTALLED})


def inspect_command_link(
    document: object,
    *,
    expected_owner: int,
    allow_absent: bool = False,
    require_target: bool = True,
) -> dict:
    document = validate_command_link_document(document)
    destination = Path(document["destination"])
    target = Path(document["target"])
    blockers = []
    status, detail = _link_state(destination, target)
    try:
        if status != "absent" or not allow_absent:
            _validate_parent(destination.parent, expected_owner)
        if require_target:
            _validate_target(target, expected_owner)
    except BookingError as exc:
        blockers.append(str(exc))
    if status == "other":
        blockers.append(detail)
    elif status == "absent" and not allow_absent:
        kind = "managed" if document["owned"] else "pre-existing"
        blockers.append(f"{kind} command link is missing: {destination}")
    return {
        "schema_version": COMMAND_LINK_SCHEMA_VERSION,
        "kind": "admin-command-link",
        "phase": document["phase"],
        "destination": str(destination),
        "target": str(target),
        "owned": document["owned"],
        "current": _public_status(status, document["owned"]),
        "blockers": blockers,
    }


def apply_command_link_uninstall(document: object, *, expected_owner: int) -> bool:
    document = validate_command_link_document(document)
    inspection = inspect_command_link(
        document,
        expected_owner=expected_owner,
        allow_absent=True,
        require_target=False,
    )
    if inspection["blockers"]:
        raise BookingError("; ".join(inspection["blockers"]))
    if not document["owned"] or inspection["current"] == "absent":
        return False
    destination = Path(document["destination"])
    target = Path(document["target"])
    status, detail = _link_state(destination, target)
    if status != "target":
        raise BookingError(detail or f"command link changed before removal: {destination}")
    destination.unlink()
    fsync_directory(destination.parent)
    return True


def validate_command_link_document(value: object) -> dict:
    if not isinstance(value, dict):
        raise BookingError("install manifest command_link must be an object")
    expected = {"schema_version", "phase", "destination", "target", "owned"}
    if set(value) != expected:
        raise BookingError("install manifest command_link fields are invalid")
    if value.get("schema_version") != COMMAND_LINK_SCHEMA_VERSION:
        raise BookingError("install manifest command_link schema is unsupported")
    if value.get("phase") not in {PHASE_INSTALLING, PHASE_INSTALLED}:
        raise BookingError("install manifest command_link phase is invalid")
    destination = value.get("destination")
    target = value.get("target")
    if not isinstance(destination, str) or not Path(destination).is_absolute():
        raise BookingError("install manifest command_link destination is invalid")
    if not isinstance(target, str) or not Path(target).is_absolute():
        raise BookingError("install manifest command_link target is invalid")
    if any(ord(character) < 0x20 for character in destination + target):
        raise BookingError("install manifest command_link path contains control characters")
    if not isinstance(value.get("owned"), bool):
        raise BookingError("install manifest command_link owned flag is invalid")
    return dict(value)


def _install_blockers(document: dict, status: str, detail: str) -> tuple[str, ...]:
    if status == "other":
        return (detail,)
    if status == "absent" and not document["owned"]:
        return (f"pre-existing command link is missing: {document['destination']}",)
    if status == "absent" and document["phase"] == PHASE_INSTALLED:
        return (f"managed command link is missing: {document['destination']}",)
    return ()


def _public_status(status: str, owned: bool) -> str:
    if status == "absent":
        return "absent"
    if status == "other":
        return "drifted"
    return "managed" if owned else "preexisting"


def _link_state(destination: Path, target: Path) -> tuple[str, str]:
    if not os.path.lexists(destination):
        return "absent", ""
    metadata = destination.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        linked = os.readlink(destination)
        if linked == str(target):
            return "target", ""
        return (
            "other",
            f"refusing to replace command link {destination}: it points to {linked!r}, "
            f"not {str(target)!r}",
        )
    return (
        "other",
        f"refusing to replace non-symlink command path: {destination}",
    )


def _validate_parent(path: Path, expected_owner: int) -> None:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise BookingError(f"command link parent is not a real directory: {path}")
    if metadata.st_uid != expected_owner:
        raise BookingError(f"command link parent must be owned by UID {expected_owner}: {path}")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise BookingError(f"command link parent must not be group/other writable: {path}")


def _validate_target(path: Path, expected_owner: int) -> None:
    if not os.path.lexists(path):
        raise BookingError(f"command target does not exist: {path}")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise BookingError(f"command target must be a regular file: {path}")
    if metadata.st_uid != expected_owner:
        raise BookingError(f"command target must be owned by UID {expected_owner}: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022:
        raise BookingError(f"command target must not be group/other writable: {path}")
    if mode & 0o111 == 0:
        raise BookingError(f"command target is not executable: {path}")


def _absolute_path(path: Path, label: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise BookingError(f"{label} must be an absolute path")
    text = os.fspath(expanded)
    if any(ord(character) < 0x20 for character in text):
        raise BookingError(f"{label} contains control characters")
    return Path(os.path.abspath(text))
