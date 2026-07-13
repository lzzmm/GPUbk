from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional


MODE_SHARED = "shared"
MODE_EXCLUSIVE = "exclusive"

STATUS_ACTIVE = "active"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"

JOB_PENDING = "pending"
JOB_CLAIMED = "claimed"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_MISSED = "missed"
JOB_TIMED_OUT = "timed-out"
JOB_INTERRUPTED = "interrupted"
JOB_UNCERTAIN = "uncertain"


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
    job_spec_id: Optional[str] = None
    job_digest: Optional[str] = None
    job_summary: Optional[str] = None
    job_digest_aliases: Optional[List[str]] = None
    expected_memory_mb: Optional[int] = None
    gpu_memory_capacity_mb: Optional[Dict[int, int]] = None
    share_units: Optional[int] = None
    excluded_gpus: Optional[List[int]] = None


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
    op_id: Optional[str] = None
    start_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    mode: Optional[str] = None
    preferred_gpus: Optional[List[int]] = None
    gpu_order: Optional[List[int]] = None
    gpu_scores: Optional[Dict[int, float]] = None
    count: Optional[int] = None
    allow_queue: bool = False
    expected_memory_mb: Optional[int] = None
    update_expected_memory: bool = False
    gpu_memory_capacity_mb: Optional[Dict[int, int]] = None
    share_units: Optional[int] = None
    update_share_units: bool = False
    excluded_gpus: Optional[List[int]] = None


class BookingError(RuntimeError):
    pass
