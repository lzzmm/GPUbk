from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional


MODE_SHARED = "shared"
MODE_EXCLUSIVE = "exclusive"

STATUS_ACTIVE = "active"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"


@dataclass(frozen=True)
class Actor:
    uid: int
    username: str


@dataclass(frozen=True)
class BookingRequest:
    actor: Actor
    count: int
    duration_seconds: int
    start_at: datetime
    mode: str = MODE_SHARED
    preferred_gpus: Optional[List[int]] = None
    gpu_order: Optional[List[int]] = None
    gpu_scores: Optional[Dict[int, float]] = None
    op_id: Optional[str] = None
    allow_queue: bool = False
    command_argv: Optional[List[str]] = None
    working_directory: Optional[str] = None
    expected_memory_mb: Optional[int] = None
    gpu_memory_capacity_mb: Optional[Dict[int, int]] = None


@dataclass(frozen=True)
class BookingResult:
    reservation: dict
    created: bool
    message: str
    queued: bool = False


@dataclass(frozen=True)
class EditRequest:
    actor: Actor
    reservation_id: str
    start_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    mode: Optional[str] = None
    preferred_gpus: Optional[List[int]] = None
    gpu_order: Optional[List[int]] = None
    gpu_scores: Optional[Dict[int, float]] = None
    count: Optional[int] = None
    allow_queue: bool = False
    expected_memory_mb: Optional[int] = None
    gpu_memory_capacity_mb: Optional[Dict[int, int]] = None


class BookingError(RuntimeError):
    pass
