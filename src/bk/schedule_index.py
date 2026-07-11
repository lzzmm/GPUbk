from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .models import STATUS_ACTIVE
from .timeparse import parse_iso


@dataclass(frozen=True, slots=True)
class ReservationSpan:
    record: dict
    start: datetime
    end: datetime
    gpus: Tuple[int, ...]
    mode: str


class ReservationIndex:
    """Parsed active reservations grouped by GPU for one scheduling request."""

    __slots__ = ("spans", "_by_gpu", "_starts_by_gpu")

    def __init__(self, spans: Sequence[ReservationSpan]):
        self.spans = tuple(sorted(spans, key=_span_sort_key))
        by_gpu: Dict[int, List[ReservationSpan]] = {}
        for span in self.spans:
            for gpu in span.gpus:
                by_gpu.setdefault(gpu, []).append(span)
        self._by_gpu = {gpu: tuple(items) for gpu, items in by_gpu.items()}
        self._starts_by_gpu = {
            gpu: tuple(item.start for item in items)
            for gpu, items in self._by_gpu.items()
        }

    @classmethod
    def from_ledger(
        cls,
        ledger: dict,
        active_after: datetime,
        statuses: Optional[Sequence[str]] = None,
    ) -> "ReservationIndex":
        allowed_statuses = frozenset((STATUS_ACTIVE,) if statuses is None else statuses)
        spans = []
        for record in ledger.get("reservations", []):
            if record.get("status") not in allowed_statuses:
                continue
            end = parse_iso(record["end_at"])
            if end <= active_after:
                continue
            start = parse_iso(record["start_at"])
            raw_gpus = record.get("gpus", [])
            gpus = tuple(gpu for gpu in raw_gpus if isinstance(gpu, int)) if isinstance(raw_gpus, list) else ()
            spans.append(
                ReservationSpan(
                    record=record,
                    start=start,
                    end=end,
                    gpus=gpus,
                    mode=str(record.get("mode", "")),
                )
            )
        return cls(spans)

    def records(self) -> List[dict]:
        return [span.record for span in self.spans]

    def overlapping(self, gpu: int, start: datetime, end: datetime) -> List[ReservationSpan]:
        spans = self._by_gpu.get(gpu, ())
        starts = self._starts_by_gpu.get(gpu, ())
        stop = bisect_left(starts, end)
        return [span for span in spans[:stop] if span.end > start]


def _span_sort_key(span: ReservationSpan) -> tuple:
    return span.start, span.end, str(span.record.get("id", ""))
