from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from .config import Config
from .gpu import GpuSnapshot, has_process_telemetry, has_process_utilization, snapshot
from .monitor import UsageAuditStore
from .timeparse import to_iso, utc_now
from .usage import (
    GpuLiveState,
    HistoricalGpuLoad,
    assess_gpu_live_states,
    combined_gpu_scores,
    gpu_selection_order,
    historical_gpu_loads,
)


@dataclass(frozen=True)
class GpuAdvice:
    generated_at: datetime
    snapshots: Sequence[GpuSnapshot]
    live_states: Dict[int, GpuLiveState]
    historical_loads: Dict[int, HistoricalGpuLoad]
    scores: Dict[int, float]
    order: List[int]

    @property
    def memory_capacities_mb(self) -> Dict[int, int]:
        return {
            item.index: item.memory_total_mb
            for item in self.snapshots
            if item.memory_total_mb > 0
        }

    def as_dict(self) -> dict:
        snapshots = {item.index: item for item in self.snapshots}
        return {
            "generated_at": to_iso(self.generated_at),
            "order": list(self.order),
            "gpus": [
                _gpu_advice_dict(
                    index,
                    snapshots.get(index),
                    self.live_states[index],
                    self.historical_loads[index],
                    self.scores[index],
                )
                for index in sorted(self.live_states)
            ],
        }


def build_gpu_advice(
    config: Config,
    *,
    snapshots: Optional[Sequence[GpuSnapshot]] = None,
    history: Optional[dict] = None,
    at: Optional[datetime] = None,
) -> GpuAdvice:
    generated_at = at or utc_now()
    devices = list(snapshot(config) if snapshots is None else snapshots)
    if history is None:
        history = UsageAuditStore(
            config.data_dir,
            config.lock_timeout_seconds,
            config.file_mode,
            config.dir_mode,
        ).load_load_history()
    live = assess_gpu_live_states(devices, config.gpu_count)
    historical = historical_gpu_loads(
        history,
        config.gpu_count,
        generated_at,
        window_minutes=config.usage_load_window_minutes,
    )
    scores = combined_gpu_scores(live, historical)
    return GpuAdvice(
        generated_at=generated_at,
        snapshots=devices,
        live_states=live,
        historical_loads=historical,
        scores=scores,
        order=gpu_selection_order(live, scores),
    )


def _gpu_advice_dict(
    index: int,
    snapshot_item: Optional[GpuSnapshot],
    live: GpuLiveState,
    historical: HistoricalGpuLoad,
    score: float,
) -> dict:
    total = snapshot_item.memory_total_mb if snapshot_item is not None else 0
    used = snapshot_item.memory_used_mb if snapshot_item is not None else 0
    memory_known = total > 0
    return {
        "index": index,
        "name": snapshot_item.name if snapshot_item is not None else "unknown",
        "temperature_c": snapshot_item.temperature_c if snapshot_item is not None else None,
        "score": score,
        "live": {
            "status": live.status,
            "reason": live.reason,
            "utilization_percent": live.utilization_percent,
            "memory_percent": round(live.memory_percent, 3),
            "process_count": live.process_count,
        },
        "history": {
            "predicted_percent": round(historical.predicted_percent, 3),
            "average_utilization_percent": round(historical.average_utilization_percent, 3),
            "busy_fraction": round(historical.busy_fraction, 4),
            "memory_percent": round(historical.memory_percent, 3),
            "sample_count": historical.sample_count,
        },
        "memory": {
            "used_mb": used if memory_known else None,
            "total_mb": total if memory_known else None,
            "free_mb": max(0, total - used) if memory_known else None,
        },
        "source": snapshot_item.source if snapshot_item is not None else "none",
        "capabilities": {
            "process_telemetry": bool(
                snapshot_item is not None and has_process_telemetry(snapshot_item)
            ),
            "process_utilization": bool(
                snapshot_item is not None and has_process_utilization(snapshot_item)
            ),
        },
    }
