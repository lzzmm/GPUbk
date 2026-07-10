from __future__ import annotations

import json
import os
import pwd
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import Config


@dataclass(frozen=True)
class GpuProcessSnapshot:
    pid: int
    uid: Optional[int] = None
    username: str = "?"
    command: str = ""
    gpu_memory_mb: int = 0
    sm_utilization_percent: Optional[int] = None
    kind: str = "C"


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


@dataclass(frozen=True)
class _HostIdentity:
    uid: Optional[int]
    username: str
    command: str


_NVML_SAMPLER: Optional["_NvmlSampler"] = None
_NVML_UNAVAILABLE = False
_IDENTITY_CACHE: Dict[int, Tuple[float, Tuple[int, int], _HostIdentity]] = {}


def snapshot(config: Config) -> List[GpuSnapshot]:
    simulation_path = os.environ.get("BK_GPU_SIM_FILE")
    if simulation_path:
        try:
            return _simulation_snapshot(Path(simulation_path))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return _unknown_snapshots(config)

    sampler = _nvml_sampler()
    if sampler is not None:
        try:
            return sampler.snapshots(config.gpu_count)
        except Exception:
            pass
    try:
        return _nvidia_smi_snapshot()
    except Exception:
        return _unknown_snapshots(config)


def _unknown_snapshots(config: Config) -> List[GpuSnapshot]:
    return [GpuSnapshot(index=index, name="unknown") for index in range(config.gpu_count)]


def _nvml_sampler() -> Optional["_NvmlSampler"]:
    global _NVML_SAMPLER, _NVML_UNAVAILABLE
    if _NVML_SAMPLER is not None:
        return _NVML_SAMPLER
    if _NVML_UNAVAILABLE:
        return None
    try:
        _NVML_SAMPLER = _NvmlSampler()
    except Exception:
        _NVML_UNAVAILABLE = True
        return None
    return _NVML_SAMPLER


class _NvmlSampler:
    def __init__(self) -> None:
        import pynvml  # type: ignore

        self.nvml = pynvml
        self.nvml.nvmlInit()
        self.handles = [
            self.nvml.nvmlDeviceGetHandleByIndex(index)
            for index in range(self.nvml.nvmlDeviceGetCount())
        ]
        recent = max(0, int(time.time() * 1_000_000) - 2_000_000)
        self.last_process_sample = {index: recent for index in range(len(self.handles))}

    def snapshots(self, configured_count: int) -> List[GpuSnapshot]:
        return [self._device_snapshot(index, handle) for index, handle in enumerate(self.handles[:configured_count])]

    def _device_snapshot(self, index: int, handle) -> GpuSnapshot:
        name = self.nvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        memory = self.nvml.nvmlDeviceGetMemoryInfo(handle)
        utilization = self.nvml.nvmlDeviceGetUtilizationRates(handle)
        temperature = self._optional_temperature(handle)
        process_utilization = self._process_utilization(index, handle)
        processes = self._running_processes(handle, process_utilization)
        return GpuSnapshot(
            index=index,
            name=str(name),
            memory_used_mb=int(memory.used // (1024 * 1024)),
            memory_total_mb=int(memory.total // (1024 * 1024)),
            utilization_percent=int(utilization.gpu),
            temperature_c=temperature,
            processes=tuple(processes),
            source="nvml",
        )

    def _optional_temperature(self, handle) -> Optional[int]:
        try:
            return int(self.nvml.nvmlDeviceGetTemperature(handle, self.nvml.NVML_TEMPERATURE_GPU))
        except Exception:
            return None

    def _process_utilization(self, index: int, handle) -> Dict[int, int]:
        query = getattr(self.nvml, "nvmlDeviceGetProcessUtilization", None)
        if query is None:
            return {}
        last_seen = self.last_process_sample.get(index, 0)
        try:
            samples = query(handle, last_seen)
        except Exception:
            return {}
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
        return {pid: value for pid, (_timestamp, value) in latest.items()}

    def _running_processes(self, handle, utilization: Dict[int, int]) -> List[GpuProcessSnapshot]:
        merged: Dict[int, Dict[str, object]] = {}
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
                )
            )
        return result


def _gpu_memory_mb(value, nvml) -> int:
    unavailable = getattr(nvml, "NVML_VALUE_NOT_AVAILABLE", None)
    if value is None or value == unavailable:
        return 0
    try:
        return max(0, int(value) // (1024 * 1024))
    except (TypeError, ValueError, OverflowError):
        return 0


def _host_identity(pid: int) -> _HostIdentity:
    now = time.monotonic()
    proc_dir = Path("/proc") / str(pid)
    try:
        proc_stat = proc_dir.stat()
    except OSError:
        _IDENTITY_CACHE.pop(pid, None)
        return _HostIdentity(uid=None, username="?", command="")
    process_token = (int(proc_stat.st_ino), int(proc_stat.st_ctime_ns))
    cached = _IDENTITY_CACHE.get(pid)
    if cached is not None and cached[0] > now and cached[1] == process_token:
        return cached[2]

    identity = _HostIdentity(uid=None, username="?", command="")
    try:
        uid = proc_stat.st_uid
        try:
            username = pwd.getpwuid(uid).pw_name
        except KeyError:
            username = str(uid)
        raw_command = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ").strip()
        if raw_command:
            command = raw_command.decode("utf-8", errors="replace")
        else:
            command = (proc_dir / "comm").read_text(encoding="utf-8", errors="replace").strip()
        identity = _HostIdentity(uid=uid, username=username, command=command)
    except OSError:
        pass

    _IDENTITY_CACHE[pid] = (now + (5.0 if identity.uid is not None else 1.0), process_token, identity)
    if len(_IDENTITY_CACHE) > 1024:
        expired = [key for key, (expires, _token, _value) in _IDENTITY_CACHE.items() if expires <= now]
        for key in expired:
            _IDENTITY_CACHE.pop(key, None)
    return identity


def _simulation_snapshot(path: Path) -> List[GpuSnapshot]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_gpus = payload.get("gpus") if isinstance(payload, dict) else payload
    if not isinstance(raw_gpus, list):
        raise ValueError("simulation file must contain a gpus list")
    snapshots = []
    for raw_gpu in raw_gpus:
        if not isinstance(raw_gpu, dict):
            raise ValueError("simulation GPU must be an object")
        raw_processes = raw_gpu.get("processes", [])
        if not isinstance(raw_processes, list):
            raise ValueError("simulation processes must be a list")
        processes = tuple(_simulation_process(item) for item in raw_processes)
        snapshots.append(
            GpuSnapshot(
                index=int(raw_gpu["index"]),
                name=str(raw_gpu.get("name", "simulated")),
                memory_used_mb=int(raw_gpu.get("memory_used_mb", 0)),
                memory_total_mb=int(raw_gpu.get("memory_total_mb", 0)),
                utilization_percent=_optional_int(raw_gpu.get("utilization_percent")),
                temperature_c=_optional_int(raw_gpu.get("temperature_c")),
                processes=processes,
                source="simulation",
            )
        )
    return sorted(snapshots, key=lambda item: item.index)


def _simulation_process(raw) -> GpuProcessSnapshot:
    if not isinstance(raw, dict):
        raise ValueError("simulation process must be an object")
    return GpuProcessSnapshot(
        pid=int(raw["pid"]),
        uid=_optional_int(raw.get("uid")),
        username=str(raw.get("username", "?")),
        command=str(raw.get("command", "")),
        gpu_memory_mb=int(raw.get("gpu_memory_mb", 0)),
        sm_utilization_percent=_optional_int(raw.get("sm_utilization_percent")),
        kind=str(raw.get("kind", "C")),
    )


def _optional_int(value) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _nvidia_smi_snapshot() -> List[GpuSnapshot]:
    cmd = [
        "nvidia-smi",
        "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    output = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=2)
    items = []
    for line in output.splitlines():
        if not line.strip():
            continue
        index, name, used, total, utilization, temperature = [part.strip() for part in line.split(",", 5)]
        items.append(
            GpuSnapshot(
                index=int(index),
                name=name,
                memory_used_mb=int(used),
                memory_total_mb=int(total),
                utilization_percent=int(utilization),
                temperature_c=int(temperature),
                source="nvidia-smi",
            )
        )
    return items
