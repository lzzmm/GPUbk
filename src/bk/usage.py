from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .gpu import GpuProcessSnapshot, GpuSnapshot
from .timeparse import parse_iso, utc_now


USAGE_AUTHORIZED = "ok"
USAGE_WRONG_GPU = "wrong-gpu"
USAGE_UNRESERVED = "unreserved"
USAGE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProcessUsage:
    gpu: int
    process: GpuProcessSnapshot
    status: str
    reservation_ids: Tuple[str, ...] = ()

    @property
    def violation(self) -> bool:
        return self.status in {USAGE_WRONG_GPU, USAGE_UNRESERVED}


def classify_process_usage(
    snapshots: Sequence[GpuSnapshot],
    reservations: Sequence[dict],
    at: Optional[datetime] = None,
) -> Dict[int, List[ProcessUsage]]:
    at = at or utc_now()
    current = [
        item
        for item in reservations
        if item.get("status") == "active"
        and parse_iso(item["start_at"]) <= at
        and at < parse_iso(item["end_at"])
    ]
    result: Dict[int, List[ProcessUsage]] = {}
    for gpu in snapshots:
        rows = [_classify_process(gpu.index, process, current) for process in gpu.processes]
        result[gpu.index] = sorted(rows, key=lambda item: (not item.violation, item.process.username, item.process.pid))
    return result


def _classify_process(gpu: int, process: GpuProcessSnapshot, current: Sequence[dict]) -> ProcessUsage:
    if process.uid is None:
        return ProcessUsage(gpu, process, USAGE_UNKNOWN)
    user_reservations = [item for item in current if int(item.get("uid", -1)) == process.uid]
    matches = [item for item in user_reservations if gpu in item.get("gpus", [])]
    if matches:
        ids = tuple(str(item.get("id", "")) for item in matches)
        return ProcessUsage(gpu, process, USAGE_AUTHORIZED, ids)
    if user_reservations:
        return ProcessUsage(gpu, process, USAGE_WRONG_GPU)
    return ProcessUsage(gpu, process, USAGE_UNRESERVED)
