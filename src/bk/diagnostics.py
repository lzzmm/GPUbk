from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Iterable

from .config import Config
from .fileio import (
    ensure_directory,
    fsync_directory,
    open_existing_regular,
    open_or_create_regular,
)
from .gpu import (
    has_process_telemetry,
    has_process_utilization,
    has_stable_device_identifier,
    snapshot,
)
from .models import BookingError
from .storage import FileLock


DOCTOR_SCHEMA_VERSION = "gpubk.doctor.v1"
MIN_FREE_BYTES = 100 * 1024 * 1024
MIN_FREE_RATIO = 0.01
MAX_PROC_IDENTITY_CANDIDATES = 100_000

_CHILD_LOCK_PROBE = """
import fcntl
import os
import stat
import sys

path = sys.argv[1]
flags = os.O_RDWR
flags |= getattr(os, "O_CLOEXEC", 0)
flags |= getattr(os, "O_NOFOLLOW", 0)
fd = os.open(path, flags)
try:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise SystemExit(12)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(11)
    raise SystemExit(0)
finally:
    os.close(fd)
"""


def run_deployment_probes(config: Config) -> list[dict]:
    directory = _probe_data_directory(config)
    checks = [directory]
    if directory["status"] == "pass":
        if config.storage_transport == "broker" and os.getuid() != config.broker_uid:
            checks.append(_probe_broker(config))
        else:
            checks.extend((_probe_atomic_replace(config), _probe_process_lock(config)))
        checks.append(_probe_disk_space(config))
    else:
        names = (
            ("broker-connectivity", "disk-space")
            if config.storage_transport == "broker" and os.getuid() != config.broker_uid
            else ("atomic-replace", "process-lock", "disk-space")
        )
        for name in names:
            checks.append(_result(name, "fail", "data directory is not ready"))
    checks.append(_probe_process_identity(config))
    checks.append(_probe_gpu(config))
    return checks


def probes_ready(checks: Iterable[dict]) -> bool:
    return all(item.get("status") == "pass" for item in checks)


def _probe_data_directory(config: Config) -> dict:
    try:
        ensure_directory(config.data_dir, config.dir_mode)
        metadata = config.data_dir.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            return _result("data-directory", "fail", "path is not a directory")
        actual = stat.S_IMODE(metadata.st_mode)
        if actual != config.dir_mode:
            return _result(
                "data-directory",
                "fail",
                "directory mode does not match configuration",
                expected_mode=f"{config.dir_mode:04o}",
                actual_mode=f"{actual:04o}",
            )
        if config.storage_gid is not None and metadata.st_gid != config.storage_gid:
            return _result(
                "data-directory",
                "fail",
                "directory GID does not match configured storage_gid",
                expected_gid=config.storage_gid,
                actual_gid=metadata.st_gid,
            )
        required_access = os.R_OK | os.X_OK
        if config.storage_transport != "broker" or os.getuid() == config.broker_uid:
            required_access |= os.W_OK
        if not os.access(config.data_dir, required_access):
            expectation = (
                "read and traverse"
                if config.storage_transport == "broker"
                and os.getuid() != config.broker_uid
                else "read, write, and traverse"
            )
            return _result(
                "data-directory", "fail", f"current UID cannot {expectation}"
            )
        return _result(
            "data-directory",
            "pass",
            (
                "service-owned directory is readable; writes use the broker"
                if config.storage_transport == "broker"
                and os.getuid() != config.broker_uid
                else "directory is accessible with the configured mode"
            ),
            path=str(config.data_dir),
            mode=f"{actual:04o}",
            owner_uid=metadata.st_uid,
            owner_gid=metadata.st_gid,
        )
    except OSError as exc:
        return _result("data-directory", "fail", str(exc), path=str(config.data_dir))


def _probe_broker(config: Config) -> dict:
    try:
        from .broker import BrokerClient

        response = BrokerClient(config).call("ping", {})
        if (
            not isinstance(response, dict)
            or response.get("service_uid") != config.broker_uid
        ):
            raise OSError(
                "broker identity response does not match trusted configuration"
            )
        if response.get("actor_uid") != os.getuid():
            raise OSError("broker did not report the kernel-authenticated caller UID")
        return _result(
            "broker-connectivity",
            "pass",
            "broker accepted the kernel-authenticated local connection",
            socket=str(config.broker_socket),
            service_uid=config.broker_uid,
            actor_uid=response.get("actor_uid"),
        )
    except (BookingError, OSError, ValueError) as exc:
        return _result("broker-connectivity", "fail", str(exc))


def _probe_atomic_replace(config: Config) -> dict:
    token = uuid.uuid4().hex
    source = config.data_dir / f".gpubk-probe-{token}.tmp"
    destination = config.data_dir / f".gpubk-probe-{token}.done"
    payload = b"gpubk atomic replace probe\n"
    fd = -1
    status = "pass"
    message = "same-directory replace, file fsync, and directory fsync succeeded"
    details = {}
    try:
        directory_metadata = config.data_dir.lstat()
        expected_gid = (
            directory_metadata.st_gid
            if config.dir_mode & stat.S_ISGID
            else None
        )
        fd = open_or_create_regular(source, os.O_WRONLY, config.file_mode)
        if os.write(fd, payload) != len(payload):
            raise OSError("short write during atomic replace probe")
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(source, destination)
        fsync_directory(config.data_dir)
        fd = open_existing_regular(destination)
        metadata = os.fstat(fd)
        observed = os.read(fd, len(payload) + 1)
        actual_mode = stat.S_IMODE(metadata.st_mode)
        if observed != payload:
            raise OSError("atomic replace probe content mismatch")
        if actual_mode != config.file_mode:
            raise OSError(
                f"probe file mode {actual_mode:04o} does not match configured {config.file_mode:04o}"
            )
        if expected_gid is not None and metadata.st_gid != expected_gid:
            raise OSError(
                f"probe file GID {metadata.st_gid} did not inherit setgid data-directory "
                f"GID {expected_gid}"
            )
        details["mode"] = f"{actual_mode:04o}"
        details["directory_gid"] = directory_metadata.st_gid
        details["file_gid"] = metadata.st_gid
        details["setgid_inheritance_checked"] = expected_gid is not None
    except OSError as exc:
        status = "fail"
        message = str(exc)
    finally:
        if fd >= 0:
            os.close(fd)
        cleanup = _cleanup_paths((source, destination))
        if cleanup is not None:
            status = "fail"
            message = cleanup
    return _result("atomic-replace", status, message, **details)


def _probe_process_lock(config: Config) -> dict:
    path = config.data_dir / f".gpubk-probe-{uuid.uuid4().hex}.lock"
    status = "pass"
    message = "a second process was excluded and acquired the lock after release"
    details = {"scope": "same-host-processes"}
    try:
        with FileLock(path, 0, config.file_mode, config.dir_mode):
            blocked = _run_child_lock_probe(path)
        released = _run_child_lock_probe(path)
        if blocked != 11:
            raise OSError(f"second process was not excluded (probe exit {blocked})")
        if released != 0:
            raise OSError(f"lock was not acquirable after release (probe exit {released})")
    except (OSError, subprocess.SubprocessError) as exc:
        status = "fail"
        message = str(exc)
    finally:
        cleanup = _cleanup_paths((path,))
        if cleanup is not None:
            status = "fail"
            message = cleanup
    return _result("process-lock", status, message, **details)


def _run_child_lock_probe(path: Path) -> int:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_LOCK_PROBE, str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        check=False,
        timeout=3,
    )
    return result.returncode


def _probe_disk_space(config: Config) -> dict:
    try:
        usage = shutil.disk_usage(config.data_dir)
    except OSError as exc:
        return _result("disk-space", "fail", str(exc))
    free_ratio = usage.free / usage.total if usage.total else 0.0
    status = "pass" if usage.free >= MIN_FREE_BYTES and free_ratio >= MIN_FREE_RATIO else "warn"
    message = "sufficient free space" if status == "pass" else "free space is below the safety threshold"
    return _result(
        "disk-space",
        status,
        message,
        free_bytes=usage.free,
        total_bytes=usage.total,
        free_ratio=round(free_ratio, 6),
    )


def _probe_process_identity(config: Config) -> dict:
    if not sys.platform.startswith("linux"):
        return _result(
            "process-identity",
            "pass",
            "Linux procfs identity visibility is not applicable on this host",
            applicable=False,
            platform=sys.platform,
        )
    current_uid = os.getuid()
    if config.monitor_uid is not None and current_uid != config.monitor_uid:
        if config.storage_transport == "broker":
            return _result(
                "process-identity",
                "pass",
                "cross-user process attribution is assigned to the service monitor UID",
                applicable=False,
                current_uid=current_uid,
                monitor_uid=config.monitor_uid,
            )
        return _result(
            "process-identity",
            "fail",
            "run the deployment probe as the configured monitor UID",
            current_uid=current_uid,
            monitor_uid=config.monitor_uid,
        )

    candidates = 0
    examined = 0
    inaccessible = 0
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                candidates += 1
                if candidates > MAX_PROC_IDENTITY_CANDIDATES:
                    break
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except PermissionError:
                    inaccessible += 1
                    continue
                except OSError:
                    continue
                if not stat.S_ISDIR(metadata.st_mode):
                    continue
                examined += 1
                if metadata.st_uid != current_uid:
                    return _result(
                        "process-identity",
                        "pass",
                        "numeric ownership of another UID's process is visible",
                        current_uid=current_uid,
                        sample_pid=int(entry.name),
                        sample_uid=metadata.st_uid,
                        candidate_processes=candidates,
                        examined_processes=examined,
                    )
    except OSError as exc:
        return _result(
            "process-identity",
            "fail",
            f"cannot inspect Linux procfs: {exc}",
            current_uid=current_uid,
        )

    if inaccessible:
        return _result(
            "process-identity",
            "fail",
            "procfs denied process metadata needed for numeric UID attribution",
            current_uid=current_uid,
            candidate_processes=candidates,
            examined_processes=examined,
            inaccessible_processes=inaccessible,
        )
    return _result(
        "process-identity",
        "warn",
        "no process owned by another UID was visible; cross-user attribution is unproven",
        current_uid=current_uid,
        candidate_processes=candidates,
        examined_processes=examined,
    )


def _probe_gpu(config: Config) -> dict:
    try:
        devices = snapshot(config)
    except Exception as exc:
        return _result("gpu-telemetry", "fail", f"GPU probe failed: {exc}")
    indices = [device.index for device in devices]
    sources = sorted({device.source for device in devices})
    details = {
        "device_count": len(devices),
        "configured_device_count": config.gpu_count,
        "indices": indices,
        "sources": sources,
        "stable_device_identifiers": [
            has_stable_device_identifier(device) for device in devices
        ],
    }
    if not devices or sources == ["none"]:
        return _result("gpu-telemetry", "fail", "no usable GPU telemetry source", **details)
    expected_indices = list(range(config.gpu_count))
    if indices != expected_indices:
        return _result(
            "gpu-telemetry",
            "fail",
            "configured GPU topology does not match detected devices",
            expected_indices=expected_indices,
            **details,
        )
    if len(sources) != 1:
        return _result(
            "gpu-telemetry",
            "fail",
            "GPU telemetry is incomplete or uses mixed sources",
            **details,
        )
    source = sources[0]
    if source == "nvml":
        invalid_memory = [device.index for device in devices if device.memory_total_mb <= 0]
        if invalid_memory:
            return _result(
                "gpu-telemetry",
                "fail",
                "NVML did not report usable memory capacity for every configured GPU",
                invalid_memory_indices=invalid_memory,
                **details,
            )
        process_gap = [
            device.index for device in devices if not has_process_telemetry(device)
        ]
        if process_gap:
            return _result(
                "gpu-telemetry",
                "fail",
                "NVML process telemetry is unavailable for configured GPUs",
                process_telemetry_unavailable_indices=process_gap,
                **details,
            )
        identifier_gap = [
            device.index for device in devices if not has_stable_device_identifier(device)
        ]
        if identifier_gap:
            return _result(
                "gpu-telemetry",
                "fail",
                "NVML stable GPU identifiers are unavailable for scheduled command binding",
                stable_identifier_unavailable_indices=identifier_gap,
                **details,
            )
        utilization_gap = [
            device.index for device in devices if not has_process_utilization(device)
        ]
        if utilization_gap:
            return _result(
                "gpu-telemetry",
                "warn",
                "NVML works, but per-process utilization is unavailable",
                process_utilization_unavailable_indices=utilization_gap,
                **details,
            )
        return _result(
            "gpu-telemetry",
            "pass",
            "NVML device, process, and per-process utilization telemetry is available",
            **details,
        )
    if source == "nvidia-smi":
        return _result(
            "gpu-telemetry",
            "warn",
            "nvidia-smi fallback works, but process utilization detail is reduced",
            **details,
        )
    if source == "simulation":
        return _result("gpu-telemetry", "warn", "simulation is active; real GPU telemetry was not tested", **details)
    return _result("gpu-telemetry", "fail", "unexpected GPU telemetry source", **details)


def _cleanup_paths(paths: Iterable[Path]) -> str | None:
    errors = []
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    if not errors:
        return None
    return "probe cleanup failed: " + "; ".join(errors)


def _result(name: str, status: str, message: str, **details) -> dict:
    return {"name": name, "status": status, "message": message, **details}
