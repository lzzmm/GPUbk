from __future__ import annotations

import base64
import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .config import MAX_GPU_COUNT
from .fileio import fsync_directory, open_existing_regular
from .models import BookingError
from .systemd import system_unit_names, system_unit_text


SYSTEM_SERVICES_SCHEMA_VERSION = "gpubk.system-services.v1"
SYSTEM_UNIT_MODE = 0o644
SYSTEM_UNIT_DIRECTORY_MODE = 0o755
MAX_SYSTEM_UNIT_BYTES = 1024 * 1024
PHASE_INSTALLING = "installing"
PHASE_INSTALLED = "installed"
PHASE_REMOVING = "removing"


@dataclass(frozen=True)
class SystemServicesPlan:
    document: dict
    operation: str
    statuses: Mapping[str, str]
    blockers: tuple[str, ...]

    def public_document(self) -> dict:
        return {
            "schema_version": SYSTEM_SERVICES_SCHEMA_VERSION,
            "kind": "admin-system-services",
            "operation": self.operation,
            "status": "blocked" if self.blockers else "ready",
            "phase": self.document["phase"],
            "unit_directory": self.document["unit_directory"],
            "python_executable": self.document["python_executable"],
            "service_uid": self.document["service_uid"],
            "service_gid": self.document["service_gid"],
            "gpu_count": self.document["gpu_count"],
            "units": dict(self.statuses),
            "blockers": list(self.blockers),
        }


def plan_system_services_install(
    *,
    existing: object,
    config_file: Path,
    data_dir: Path,
    socket_directory: Path,
    service_uid: int,
    service_gid: int,
    gpu_count: int,
    unit_directory: Path,
    python_executable: Optional[Path],
    expected_owner: int,
    force: bool,
) -> SystemServicesPlan:
    _validate_identity(service_uid, service_gid)
    _validate_gpu_count(gpu_count)
    config_file = _absolute_path(config_file, "configuration")
    data_dir = _absolute_path(data_dir, "data directory")
    socket_directory = _absolute_path(socket_directory, "socket directory")
    unit_directory = _absolute_path(unit_directory, "system unit directory")
    _validate_unit_directory(unit_directory, expected_owner)
    managed_gid = unit_directory.stat().st_gid

    previous = None
    if existing is not None:
        previous = validate_system_services_document(existing)
        if previous["phase"] == PHASE_REMOVING:
            raise BookingError(
                "system service removal is incomplete; rerun the uninstall operation"
            )
        for key, expected in (
            ("config_file", str(config_file)),
            ("data_dir", str(data_dir)),
            ("socket_directory", str(socket_directory)),
            ("unit_directory", str(unit_directory)),
        ):
            if previous[key] != expected:
                raise BookingError(
                    f"tracked system service {key} differs; uninstall it before changing paths"
                )
        executable = _absolute_path(
            python_executable
            if python_executable is not None
            else Path(previous["python_executable"]),
            "Python executable",
        )
        before_by_name = {
            name: previous["files"][name]["before"] for name in system_unit_names()
        }
    else:
        executable = _absolute_path(
            python_executable if python_executable is not None else Path(os.sys.executable),
            "Python executable",
        )
        before_by_name = {}
        for name in system_unit_names():
            before = _read_file_state(unit_directory / name)
            before_by_name[name] = before
            if before["exists"] and not force:
                raise BookingError(
                    f"untracked systemd unit already exists: {unit_directory / name}; "
                    "pass --force only after reviewing it"
                )

    desired = _render_managed_files(
        service_uid=service_uid,
        service_gid=service_gid,
        config_file=config_file,
        data_dir=data_dir,
        socket_directory=socket_directory,
        python_executable=executable,
        expected_owner=expected_owner,
        expected_gid=managed_gid,
        gpu_count=gpu_count,
    )
    files = {}
    for name in system_unit_names():
        item = {
            "before": before_by_name[name],
            "managed": desired[name],
        }
        if previous is not None:
            prior_managed = previous["files"][name]["managed"]
            if prior_managed["sha256"] != desired[name]["sha256"]:
                item["previous_managed"] = prior_managed
            elif "previous_managed" in previous["files"][name]:
                item["previous_managed"] = previous["files"][name][
                    "previous_managed"
                ]
        files[name] = item
    document = {
        "schema_version": SYSTEM_SERVICES_SCHEMA_VERSION,
        "phase": PHASE_INSTALLING,
        "unit_directory": str(unit_directory),
        "python_executable": str(executable),
        "config_file": str(config_file),
        "data_dir": str(data_dir),
        "socket_directory": str(socket_directory),
        "service_uid": service_uid,
        "service_gid": service_gid,
        "gpu_count": gpu_count,
        "files": files,
    }
    document = validate_system_services_document(document)
    statuses, blockers = inspect_system_service_files(document)
    return SystemServicesPlan(
        document=document,
        operation="install",
        statuses=statuses,
        blockers=tuple(blockers),
    )


def apply_system_services_install(
    document: object,
    *,
    expected_owner: int,
) -> dict:
    document = validate_system_services_document(document)
    if document["phase"] not in {PHASE_INSTALLING, PHASE_INSTALLED}:
        raise BookingError("system service document is not installable")
    _validate_unit_directory(Path(document["unit_directory"]), expected_owner)
    statuses, blockers = inspect_system_service_files(document)
    if blockers:
        raise BookingError("; ".join(blockers))
    for name in system_unit_names():
        if statuses[name] != "managed":
            _write_file_state(
                Path(document["unit_directory"]) / name,
                document["files"][name]["managed"],
            )
    statuses, blockers = inspect_system_service_files(document)
    if blockers or any(value != "managed" for value in statuses.values()):
        raise BookingError("system service installation did not converge")
    finalized = {
        **document,
        "phase": PHASE_INSTALLED,
        "files": {
            name: {
                "before": document["files"][name]["before"],
                "managed": document["files"][name]["managed"],
            }
            for name in system_unit_names()
        },
    }
    return validate_system_services_document(finalized)


def plan_system_services_uninstall(existing: object) -> SystemServicesPlan:
    document = validate_system_services_document(existing)
    document = {**document, "phase": PHASE_REMOVING}
    statuses, blockers = inspect_system_service_files(document)
    return SystemServicesPlan(
        document=document,
        operation="uninstall",
        statuses=statuses,
        blockers=tuple(blockers),
    )


def apply_system_services_uninstall(
    document: object,
    *,
    expected_owner: int,
) -> None:
    document = validate_system_services_document(document)
    if document["phase"] != PHASE_REMOVING:
        raise BookingError("system service document is not in removal phase")
    directory = Path(document["unit_directory"])
    _validate_unit_directory(directory, expected_owner)
    _, blockers = inspect_system_service_files(document)
    if blockers:
        raise BookingError("; ".join(blockers))
    for name in reversed(system_unit_names()):
        destination = directory / name
        before = document["files"][name]["before"]
        current = _read_file_state(destination)
        if _state_equal(current, before):
            continue
        if before["exists"]:
            _write_file_state(destination, before)
        elif current["exists"]:
            destination.unlink()
            fsync_directory(directory)
    for name in system_unit_names():
        current = _read_file_state(directory / name)
        if not _state_equal(current, document["files"][name]["before"]):
            raise BookingError(f"failed to restore prior systemd unit: {directory / name}")


def retarget_system_services_document(
    existing: object,
    *,
    service_uid: int,
    service_gid: int,
    expected_owner: int,
) -> dict:
    current = validate_system_services_document(existing)
    if current["phase"] != PHASE_INSTALLED:
        raise BookingError(
            "system service lifecycle is incomplete; finish it before transferring ownership"
        )
    _validate_identity(service_uid, service_gid)
    desired = _render_managed_files(
        service_uid=service_uid,
        service_gid=service_gid,
        config_file=Path(current["config_file"]),
        data_dir=Path(current["data_dir"]),
        socket_directory=Path(current["socket_directory"]),
        python_executable=Path(current["python_executable"]),
        expected_owner=expected_owner,
        expected_gid=Path(current["unit_directory"]).stat().st_gid,
        gpu_count=current["gpu_count"],
    )
    updated = {
        **current,
        "service_uid": service_uid,
        "service_gid": service_gid,
        "files": {
            name: {
                "before": current["files"][name]["before"],
                "managed": desired[name],
            }
            for name in system_unit_names()
        },
    }
    return validate_system_services_document(updated)


def apply_installed_system_services(
    target: object,
    *,
    allowed_current: Sequence[object] = (),
    expected_owner: int,
) -> None:
    target = validate_system_services_document(target)
    if target["phase"] != PHASE_INSTALLED:
        raise BookingError("target system service document is not installed")
    allowed = [target]
    for document in allowed_current:
        validated = validate_system_services_document(document)
        if validated["unit_directory"] != target["unit_directory"]:
            raise BookingError("system service rollback directories differ")
        allowed.append(validated)
    directory = Path(target["unit_directory"])
    _validate_unit_directory(directory, expected_owner)
    for name in system_unit_names():
        current = _read_file_state(directory / name)
        candidates = [item["files"][name]["managed"] for item in allowed]
        if not any(_state_equal(current, candidate) for candidate in candidates):
            raise BookingError(f"managed systemd unit drifted: {directory / name}")
    for name in system_unit_names():
        desired = target["files"][name]["managed"]
        destination = directory / name
        if not _state_equal(_read_file_state(destination), desired):
            _write_file_state(destination, desired)


def inspect_system_service_files(document: object) -> tuple[dict[str, str], list[str]]:
    document = validate_system_services_document(document)
    directory = Path(document["unit_directory"])
    statuses = {}
    blockers = []
    for name in system_unit_names():
        current = _read_file_state(directory / name)
        item = document["files"][name]
        if _state_equal(current, item["managed"]):
            status = "managed"
        elif "previous_managed" in item and _state_equal(
            current, item["previous_managed"]
        ):
            status = "previous-managed"
        elif _state_equal(current, item["before"]):
            status = "original"
        else:
            status = "drifted"
            blockers.append(f"managed systemd unit drifted: {directory / name}")
        statuses[name] = status
    return statuses, blockers


def validate_system_services_document(value: object) -> dict:
    if not isinstance(value, dict):
        raise BookingError("system service manifest entry is invalid")
    if value.get("schema_version") != SYSTEM_SERVICES_SCHEMA_VERSION:
        raise BookingError("unsupported system service manifest entry")
    phase = value.get("phase")
    if phase not in {PHASE_INSTALLING, PHASE_INSTALLED, PHASE_REMOVING}:
        raise BookingError("system service manifest phase is invalid")
    for key in (
        "unit_directory",
        "python_executable",
        "config_file",
        "data_dir",
        "socket_directory",
    ):
        path = value.get(key)
        if not isinstance(path, str) or str(_absolute_path(Path(path), key)) != path:
            raise BookingError(f"system service manifest {key} is invalid")
    _validate_identity(value.get("service_uid"), value.get("service_gid"))
    gpu_count = value.get("gpu_count", MAX_GPU_COUNT)
    _validate_gpu_count(gpu_count)
    files = value.get("files")
    if not isinstance(files, dict) or set(files) != set(system_unit_names()):
        raise BookingError("system service manifest file set is invalid")
    normalized_files = {}
    for name in system_unit_names():
        item = files[name]
        if not isinstance(item, dict) or not {"before", "managed"} <= set(item):
            raise BookingError(f"system service manifest item is invalid: {name}")
        unexpected = set(item) - {"before", "managed", "previous_managed"}
        if unexpected:
            raise BookingError(f"system service manifest item has unknown fields: {name}")
        normalized = {
            "before": _validate_file_state(item["before"], managed=False),
            "managed": _validate_file_state(item["managed"], managed=True),
        }
        if "previous_managed" in item:
            normalized["previous_managed"] = _validate_file_state(
                item["previous_managed"], managed=True
            )
        normalized_files[name] = normalized
    return {**value, "gpu_count": gpu_count, "files": normalized_files}


def enabled_unit_links(document: object) -> tuple[Path, ...]:
    document = validate_system_services_document(document)
    directory = Path(document["unit_directory"])
    wants = directory / "multi-user.target.wants"
    links = []
    for name in system_unit_names():
        path = wants / name
        if os.path.lexists(path):
            links.append(path)
    return tuple(links)


def _render_managed_files(
    *,
    service_uid: int,
    service_gid: int,
    config_file: Path,
    data_dir: Path,
    socket_directory: Path,
    python_executable: Path,
    expected_owner: int,
    expected_gid: int,
    gpu_count: int,
) -> dict[str, dict]:
    rendered = {}
    for kind, name in (("broker", system_unit_names()[0]), ("monitor", system_unit_names()[1])):
        payload = system_unit_text(
            kind,
            service_uid=service_uid,
            service_gid=service_gid,
            config_file=config_file,
            data_dir=data_dir,
            socket_directory=socket_directory,
            gpu_count=gpu_count,
            python_executable=python_executable,
        ).encode()
        rendered[name] = _payload_state(
            payload,
            uid=expected_owner,
            gid=expected_gid,
            mode=SYSTEM_UNIT_MODE,
        )
    return rendered


def _validate_unit_directory(path: Path, expected_owner: int) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise BookingError(f"system unit directory does not exist: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise BookingError(f"system unit path is not a real directory: {path}")
    if metadata.st_uid != expected_owner:
        raise BookingError(f"system unit directory must be owned by UID {expected_owner}: {path}")
    if stat.S_IMODE(metadata.st_mode) != SYSTEM_UNIT_DIRECTORY_MODE:
        raise BookingError(f"system unit directory must use mode 0755: {path}")


def _validate_identity(uid: object, gid: object) -> None:
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise BookingError("system service UID must be a positive integer")
    if isinstance(gid, bool) or not isinstance(gid, int) or gid < 0:
        raise BookingError("system service GID must be a non-negative integer")


def _validate_gpu_count(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_GPU_COUNT
    ):
        raise BookingError(f"system service GPU count must be between 1 and {MAX_GPU_COUNT}")


def _absolute_path(path: Path, label: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise BookingError(f"{label} must be an absolute path")
    text = os.fspath(expanded)
    if any(character in text for character in ("\x00", "\n", "\r")):
        raise BookingError(f"{label} contains an invalid character")
    return Path(os.path.abspath(text))


def _read_file_state(path: Path) -> dict:
    if not os.path.lexists(path):
        return {"exists": False}
    fd = open_existing_regular(path, expected_mode=SYSTEM_UNIT_MODE)
    try:
        metadata = os.fstat(fd)
        if metadata.st_nlink != 1:
            raise BookingError(f"refusing hard-linked systemd unit: {path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            payload = handle.read(MAX_SYSTEM_UNIT_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(payload) > MAX_SYSTEM_UNIT_BYTES:
        raise BookingError(f"systemd unit is unexpectedly large: {path}")
    return _payload_state(
        payload,
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        mode=stat.S_IMODE(metadata.st_mode),
    )


def _payload_state(payload: bytes, *, uid: int, gid: int, mode: int) -> dict:
    return {
        "exists": True,
        "uid": uid,
        "gid": gid,
        "mode": mode,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "content_b64": base64.b64encode(payload).decode("ascii"),
    }


def _validate_file_state(value: object, *, managed: bool) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("exists"), bool):
        raise BookingError("system service file snapshot is invalid")
    if not value["exists"]:
        if managed or set(value) != {"exists"}:
            raise BookingError("system service absent-file snapshot is invalid")
        return value
    for key in ("uid", "gid", "mode"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise BookingError(f"system service snapshot {key} is invalid")
    if value["mode"] != SYSTEM_UNIT_MODE:
        raise BookingError("system service snapshots must use mode 0644")
    try:
        payload = base64.b64decode(value.get("content_b64"), validate=True)
    except (TypeError, ValueError) as exc:
        raise BookingError("system service snapshot content is invalid") from exc
    if len(payload) > MAX_SYSTEM_UNIT_BYTES:
        raise BookingError("system service snapshot is unexpectedly large")
    digest = value.get("sha256")
    if not isinstance(digest, str) or hashlib.sha256(payload).hexdigest() != digest:
        raise BookingError("system service snapshot checksum does not match")
    return value


def _state_equal(left: object, right: object) -> bool:
    return _validate_file_state(left, managed=False) == _validate_file_state(
        right, managed=False
    )


def _write_file_state(path: Path, state: object) -> None:
    state = _validate_file_state(state, managed=True)
    payload = base64.b64decode(state["content_b64"], validate=True)
    directory = path.parent
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(directory)
    )
    temporary = Path(temporary_name)
    try:
        os.fchown(fd, state["uid"], state["gid"])
        os.fchmod(fd, state["mode"])
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)
