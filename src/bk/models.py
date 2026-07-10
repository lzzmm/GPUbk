from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


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
    op_id: Optional[str] = None
    allow_queue: bool = False


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
    count: Optional[int] = None
    allow_queue: bool = False


class BookingError(RuntimeError):
    pass
