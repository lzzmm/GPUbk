from __future__ import annotations

import csv
import json
import os
import pwd
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .config import MAX_GPU_COUNT, MAX_UID, Config


@dataclass(frozen=True)
class GpuProcessSnapshot:
    pid: int
    uid: Optional[int] = None
    username: str = "?"
    command: str = ""
    gpu_memory_mb: int = 0
    sm_utilization_percent: Optional[int] = None
    kind: str = "C"
    host_start_id: str = ""


@dataclass(frozen=True)
class GpuSnapshot:
    index: int
    name: str
    memory_used_mb: int = 0
    memory_total_mb: int = 0
    utilization_percent: Optional[int] = None
    temperature_c: Optional[int] = None
    processes: Tuple[GpuProcessSnapshot, ...] = ()
    source: str = "none"
    process_telemetry_available: Optional[bool] = None
    process_utilization_available: Optional[bool] = None
    device_uuid: str = ""


@dataclass(frozen=True)
class _HostIdentity:
    uid: Optional[int]
    username: str
    command: str
    start_id: str


_NVML_SAMPLER: Optional["_NvmlSampler"] = None
_NVML_UNAVAILABLE = False
_NVML_RETRY_AT = 0.0
_IDENTITY_CACHE: Dict[int, Tuple[float, Tuple[int, int], _HostIdentity]] = {}

NVML_RETRY_SECONDS = 30.0
MAX_PROCESS_COMMAND_BYTES = 4096
MAX_IDENTITY_CACHE_ENTRIES = 4096
MAX_SIMULATION_FILE_BYTES = 4 * 1024 * 1024
MAX_SIMULATION_PROCESSES_PER_GPU = 100_000
MAX_CUDA_DEVICE_IDENTIFIER_BYTES = 256


def snapshot(config: Config) -> List[GpuSnapshot]:
    simulation_path = os.environ.get("BK_GPU_SIM_FILE")
    if simulation_path:
        try:
            return _simulation_snapshot(Path(simulation_path))[: config.gpu_count]
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return _unknown_snapshots(config)

    sampler = _nvml_sampler()
    if sampler is not None:
        try:
            return sampler.snapshots(config.gpu_count)
        except Exception:
            _invalidate_nvml_sampler()
    try:
        return _nvidia_smi_snapshot()[: config.gpu_count]
    except Exception:
        return _unknown_snapshots(config)


def detect_gpu_count() -> int:
    simulation_path = os.environ.get("BK_GPU_SIM_FILE")
    if simulation_path:
        try:
            return max(1, len(_simulation_snapshot(Path(simulation_path))))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 1

    procfs_count = _procfs_gpu_count()
    if procfs_count:
        return procfs_count

    sampler = _nvml_sampler()
    if sampler is not None:
        return max(1, len(sampler.handles))
    try:
        return max(1, len(_nvidia_smi_snapshot()))
    except Exception:
        return 1


def _procfs_gpu_count(path: Path = Path("/proc/driver/nvidia/gpus")) -> int:
    try:
        return sum(1 for item in path.iterdir() if item.is_dir())
    except OSError:
        return 0


def _unknown_snapshots(config: Config) -> List[GpuSnapshot]:
    return [GpuSnapshot(index=index, name="unknown") for index in range(config.gpu_count)]


def has_process_telemetry(device: GpuSnapshot) -> bool:
    if device.process_telemetry_available is not None:
        return device.process_telemetry_available
    return device.source in {"nvml", "simulation"}


def has_process_utilization(device: GpuSnapshot) -> bool:
    if device.process_utilization_available is not None:
        return device.process_utilization_available
    return device.source in {"nvml", "simulation"}


def has_stable_device_identifier(device: GpuSnapshot) -> bool:
    try:
        return _cuda_device_identifier(device.device_uuid) == device.device_uuid
    except ValueError:
        return False


def cuda_visible_device_tokens(
    selected_gpus: Sequence[int],
    devices: Sequence[GpuSnapshot],
) -> Tuple[str, ...]:
    indices = tuple(int(item) for item in selected_gpus)
    by_index = {item.index: item for item in devices}
    stable = []
    for index in indices:
        device = by_index.get(index)
        if device is None or not has_stable_device_identifier(device):
            return tuple(str(item) for item in indices)
        stable.append(device.device_uuid)
    return tuple(stable)


def _nvml_sampler() -> Optional["_NvmlSampler"]:
    global _NVML_SAMPLER, _NVML_UNAVAILABLE, _NVML_RETRY_AT
    if _NVML_SAMPLER is not None:
        return _NVML_SAMPLER
    now = time.monotonic()
    if _NVML_UNAVAILABLE and now < _NVML_RETRY_AT:
        return None
    try:
        _NVML_SAMPLER = _NvmlSampler()
    except Exception:
        _NVML_UNAVAILABLE = True
        _NVML_RETRY_AT = now + NVML_RETRY_SECONDS
        return None
    _NVML_UNAVAILABLE = False
    _NVML_RETRY_AT = 0.0
    return _NVML_SAMPLER


def _invalidate_nvml_sampler() -> None:
    global _NVML_SAMPLER, _NVML_UNAVAILABLE, _NVML_RETRY_AT
    sampler = _NVML_SAMPLER
    _NVML_SAMPLER = None
    _NVML_UNAVAILABLE = True
    _NVML_RETRY_AT = time.monotonic() + NVML_RETRY_SECONDS
    if sampler is not None:
        sampler.close()


class _NvmlSampler:
    def __init__(self) -> None:
        import pynvml  # type: ignore

        self.nvml = pynvml
        self._initialized = False
        try:
            self.nvml.nvmlInit()
            self._initialized = True
            device_count = int(self.nvml.nvmlDeviceGetCount())
            if device_count < 1 or device_count > MAX_GPU_COUNT:
                raise ValueError(f"NVML reported invalid device count: {device_count}")
            self.handles = [
                self.nvml.nvmlDeviceGetHandleByIndex(index)
                for index in range(device_count)
            ]
            self.device_uuids = [""] * len(self.handles)
        except Exception:
            self.close()
            raise
        recent = max(0, int(time.time() * 1_000_000) - 2_000_000)
        self.last_process_sample = {index: recent for index in range(len(self.handles))}

    def snapshots(self, configured_count: int) -> List[GpuSnapshot]:
        return [self._device_snapshot(index, handle) for index, handle in enumerate(self.handles[:configured_count])]

    def close(self) -> None:
        if not self._initialized:
            return
        self._initialized = False
        try:
            self.nvml.nvmlShutdown()
        except Exception:
            pass

    def _device_snapshot(self, index: int, handle) -> GpuSnapshot:
        name = self.nvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        memory = self.nvml.nvmlDeviceGetMemoryInfo(handle)
        utilization = self.nvml.nvmlDeviceGetUtilizationRates(handle)
        temperature = self._optional_temperature(handle)
        process_utilization, utilization_available = self._process_utilization(index, handle)
        processes, process_telemetry_available = self._running_processes(
            handle, process_utilization
        )
        return GpuSnapshot(
            index=index,
            name=str(name),
            memory_used_mb=int(memory.used // (1024 * 1024)),
            memory_total_mb=int(memory.total // (1024 * 1024)),
            utilization_percent=int(utilization.gpu),
            temperature_c=temperature,
            processes=tuple(processes),
            source="nvml",
            process_telemetry_available=process_telemetry_available,
            process_utilization_available=utilization_available,
            device_uuid=self._device_uuid(index, handle),
        )

    def _device_uuid(self, index: int, handle) -> str:
        cached = self.device_uuids[index]
        if cached:
            return cached
        try:
            value = _cuda_device_identifier(self.nvml.nvmlDeviceGetUUID(handle))
        except Exception:
            return ""
        self.device_uuids[index] = value
        return value

    def _optional_temperature(self, handle) -> Optional[int]:
        try:
            return int(self.nvml.nvmlDeviceGetTemperature(handle, self.nvml.NVML_TEMPERATURE_GPU))
        except Exception:
            return None

    def _process_utilization(self, index: int, handle) -> Tuple[Dict[int, int], bool]:
        query = getattr(self.nvml, "nvmlDeviceGetProcessUtilization", None)
        if query is None:
            return {}, False
        last_seen = self.last_process_sample.get(index, 0)
        try:
            samples = query(handle, last_seen)
        except Exception:
            return {}, False
        latest: Dict[int, Tuple[int, int]] = {}
        newest_timestamp = last_seen
        for sample in samples:
            pid = int(sample.pid)
            timestamp = int(sample.timeStamp)
            sm_util = int(sample.smUtil)
            if pid not in latest or timestamp >= latest[pid][0]:
                latest[pid] = (timestamp, sm_util)
            newest_timestamp = max(newest_timestamp, timestamp)
        self.last_process_sample[index] = newest_timestamp
        return {pid: value for pid, (_timestamp, value) in latest.items()}, True

    def _running_processes(
        self, handle, utilization: Dict[int, int]
    ) -> Tuple[List[GpuProcessSnapshot], bool]:
        merged: Dict[int, Dict[str, object]] = {}
        compute_query_succeeded = False
        queries = (
            ("nvmlDeviceGetComputeRunningProcesses", "C"),
            ("nvmlDeviceGetGraphicsRunningProcesses", "G"),
        )
        for function_name, kind in queries:
            query = getattr(self.nvml, function_name, None)
            if query is None:
                continue
            try:
                raw_processes = query(handle)
            except Exception:
                continue
            if kind == "C":
                compute_query_succeeded = True
            for process in raw_processes:
                pid = int(process.pid)
                used_memory = _gpu_memory_mb(getattr(process, "usedGpuMemory", 0), self.nvml)
                item = merged.setdefault(pid, {"memory": 0, "kinds": set()})
                item["memory"] = max(int(item["memory"]), used_memory)
                kinds = item["kinds"]
                if isinstance(kinds, set):
                    kinds.add(kind)

        result = []
        for pid, item in sorted(merged.items()):
            identity = _host_identity(pid)
            kinds = item["kinds"] if isinstance(item["kinds"], set) else {"C"}
            result.append(
                GpuProcessSnapshot(
                    pid=pid,
                    uid=identity.uid,
                    username=identity.username,
                    command=identity.command,
                    gpu_memory_mb=int(item["memory"]),
                    sm_utilization_percent=utilization.get(pid),
                    kind="+".join(sorted(kinds)),
                    host_start_id=identity.start_id,
                )
            )
        return result, compute_query_succeeded


def _gpu_memory_mb(value, nvml) -> int:
    unavailable = getattr(nvml, "NVML_VALUE_NOT_AVAILABLE", None)
    if value is None or value == unavailable:
        return 0
    try:
        return max(0, int(value) // (1024 * 1024))
    except (TypeError, ValueError, OverflowError):
        return 0


def _host_identity(pid: int) -> _HostIdentity:
    if pid <= 0:
        return _HostIdentity(uid=None, username="?", command="", start_id="")
    now = time.monotonic()
    proc_dir = Path("/proc") / str(pid)
    try:
        proc_stat = proc_dir.stat()
    except OSError:
        _IDENTITY_CACHE.pop(pid, None)
        return _HostIdentity(uid=None, username="?", command="", start_id="")
    process_token = (int(proc_stat.st_ino), int(proc_stat.st_ctime_ns))
    cached = _IDENTITY_CACHE.get(pid)
    if cached is not None and cached[0] > now and cached[1] == process_token:
        return cached[2]

    uid = proc_stat.st_uid
    try:
        username = pwd.getpwuid(uid).pw_name
    except (KeyError, OSError):
        username = str(uid)
    identity = _HostIdentity(
        uid=uid,
        username=username,
        command=_read_process_command(proc_dir),
        start_id=f"{process_token[0]}:{process_token[1]}",
    )

    _IDENTITY_CACHE[pid] = (now + 5.0, process_token, identity)
    _prune_identity_cache(now)
    return identity


def _read_process_command(proc_dir: Path) -> str:
    try:
        with (proc_dir / "cmdline").open("rb") as fh:
            raw_command = fh.read(MAX_PROCESS_COMMAND_BYTES + 1)
    except OSError:
        raw_command = b""
    raw_command = raw_command[:MAX_PROCESS_COMMAND_BYTES].replace(b"\0", b" ").strip()
    if raw_command:
        return raw_command.decode("utf-8", errors="replace")
    try:
        with (proc_dir / "comm").open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(MAX_PROCESS_COMMAND_BYTES).strip()
    except OSError:
        return ""


def _prune_identity_cache(now: float) -> None:
    if len(_IDENTITY_CACHE) <= MAX_IDENTITY_CACHE_ENTRIES:
        return
    expired = [
        key for key, (expires, _token, _value) in _IDENTITY_CACHE.items() if expires <= now
    ]
    for key in expired:
        _IDENTITY_CACHE.pop(key, None)
    excess = len(_IDENTITY_CACHE) - MAX_IDENTITY_CACHE_ENTRIES
    if excess <= 0:
        return
    oldest = sorted(_IDENTITY_CACHE, key=lambda key: _IDENTITY_CACHE[key][0])[:excess]
    for key in oldest:
        _IDENTITY_CACHE.pop(key, None)


def _simulation_snapshot(path: Path) -> List[GpuSnapshot]:
    with path.open("rb") as fh:
        raw_payload = fh.read(MAX_SIMULATION_FILE_BYTES + 1)
    if len(raw_payload) > MAX_SIMULATION_FILE_BYTES:
        raise ValueError("simulation file exceeds the 4 MiB limit")
    payload = json.loads(raw_payload)
    raw_gpus = payload.get("gpus") if isinstance(payload, dict) else payload
    if not isinstance(raw_gpus, list):
        raise ValueError("simulation file must contain a gpus list")
    if not raw_gpus:
        raise ValueError("simulation file must contain at least one GPU")
    if len(raw_gpus) > MAX_GPU_COUNT:
        raise ValueError(f"simulation file exceeds the {MAX_GPU_COUNT} GPU limit")
    snapshots = []
    for raw_gpu in raw_gpus:
        if not isinstance(raw_gpu, dict):
            raise ValueError("simulation GPU must be an object")
        raw_processes = raw_gpu.get("processes", [])
        if not isinstance(raw_processes, list):
            raise ValueError("simulation processes must be a list")
        if len(raw_processes) > MAX_SIMULATION_PROCESSES_PER_GPU:
            raise ValueError("simulation GPU has too many processes")
        processes = tuple(_simulation_process(item) for item in raw_processes)
        memory_used_mb = _bounded_int(raw_gpu.get("memory_used_mb", 0), "memory_used_mb", 0)
        memory_total_mb = _bounded_int(raw_gpu.get("memory_total_mb", 0), "memory_total_mb", 0)
        if memory_used_mb > memory_total_mb:
            raise ValueError("simulation memory_used_mb exceeds memory_total_mb")
        snapshots.append(
            GpuSnapshot(
                index=_bounded_int(raw_gpu["index"], "GPU index", 0, MAX_GPU_COUNT - 1),
                name=str(raw_gpu.get("name", "simulated")),
                memory_used_mb=memory_used_mb,
                memory_total_mb=memory_total_mb,
                utilization_percent=_optional_bounded_int(
                    raw_gpu.get("utilization_percent"), "utilization_percent", 0, 100
                ),
                temperature_c=_optional_bounded_int(
                    raw_gpu.get("temperature_c"), "temperature_c", -100, 250
                ),
                processes=processes,
                source="simulation",
                process_telemetry_available=True,
                process_utilization_available=True,
                device_uuid=_optional_cuda_device_identifier(raw_gpu.get("uuid")),
            )
        )
    snapshots.sort(key=lambda item: item.index)
    indices = [item.index for item in snapshots]
    if indices != list(range(len(snapshots))):
        raise ValueError("simulation GPU indices must be unique and contiguous from 0")
    return snapshots


def _simulation_process(raw) -> GpuProcessSnapshot:
    if not isinstance(raw, dict):
        raise ValueError("simulation process must be an object")
    return GpuProcessSnapshot(
        pid=_bounded_int(raw["pid"], "process pid", 1, 2**31 - 1),
        uid=_optional_bounded_int(raw.get("uid"), "process uid", 0, MAX_UID),
        username=str(raw.get("username", "?")),
        command=str(raw.get("command", "")),
        gpu_memory_mb=_bounded_int(raw.get("gpu_memory_mb", 0), "gpu_memory_mb", 0),
        sm_utilization_percent=_optional_bounded_int(
            raw.get("sm_utilization_percent"), "sm_utilization_percent", 0, 100
        ),
        kind=str(raw.get("kind", "C")),
        host_start_id=str(raw.get("host_start_id", "")),
    )


def _optional_bounded_int(
    value: object, label: str, minimum: int, maximum: Optional[int] = None
) -> Optional[int]:
    if value is None:
        return None
    return _bounded_int(value, label, minimum, maximum)


def _bounded_int(
    value: object, label: str, minimum: int, maximum: Optional[int] = None
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{label} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{label} must be <= {maximum}")
    return parsed


def _nvidia_smi_snapshot() -> List[GpuSnapshot]:
    cmd = [
        "nvidia-smi",
        "--query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    output = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=2)
    items = []
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if not row:
            continue
        if len(row) != 7:
            raise ValueError(f"unexpected nvidia-smi CSV field count: {len(row)}")
        index, device_uuid, name, used, total, utilization, temperature = [
            part.strip() for part in row
        ]
        items.append(
            GpuSnapshot(
                index=_bounded_int(index, "nvidia-smi GPU index", 0, MAX_GPU_COUNT - 1),
                name=name,
                memory_used_mb=_nvidia_smi_optional_int(used, minimum=0) or 0,
                memory_total_mb=_nvidia_smi_optional_int(total, minimum=0) or 0,
                utilization_percent=_nvidia_smi_optional_int(
                    utilization, minimum=0, maximum=100
                ),
                temperature_c=_nvidia_smi_optional_int(
                    temperature, minimum=-100, maximum=250
                ),
                source="nvidia-smi",
                device_uuid=_optional_cuda_device_identifier(device_uuid),
            )
        )
    items.sort(key=lambda item: item.index)
    if not items:
        raise ValueError("nvidia-smi returned no GPU rows")
    indices = [item.index for item in items]
    if indices != list(range(len(items))):
        raise ValueError("nvidia-smi GPU indices must be unique and contiguous from 0")
    return items


def _nvidia_smi_optional_int(
    value: str, *, minimum: int, maximum: Optional[int] = None
) -> Optional[int]:
    normalized = value.strip().lower()
    if normalized in {"", "n/a", "[n/a]", "not supported", "[not supported]"}:
        return None
    return _bounded_int(value, "nvidia-smi value", minimum, maximum)


def _optional_cuda_device_identifier(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str) and value.strip().lower() in {
        "",
        "n/a",
        "[n/a]",
        "not supported",
        "[not supported]",
    }:
        return ""
    return _cuda_device_identifier(value)


def _cuda_device_identifier(value: object) -> str:
    if isinstance(value, bytes):
        try:
            text = value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("GPU UUID must be ASCII") from exc
    elif isinstance(value, str):
        text = value
    else:
        raise ValueError("GPU UUID must be text")
    if not text.startswith(("GPU-", "MIG-")):
        raise ValueError("GPU UUID must start with GPU- or MIG-")
    try:
        encoded = text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("GPU UUID must be ASCII") from exc
    if len(encoded) > MAX_CUDA_DEVICE_IDENTIFIER_BYTES:
        raise ValueError("GPU UUID is too long")
    if any(
        not (character.isascii() and (character.isalnum() or character in "-_/.:"))
        for character in text
    ):
        raise ValueError("GPU UUID contains an unsafe character")
    return text
