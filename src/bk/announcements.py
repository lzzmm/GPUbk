from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import MAX_ANNOUNCEMENT_HISTORY, Config
from .models import Actor, BookingError
from .timeparse import parse_iso, to_iso, utc_now


ANNOUNCEMENT_LEVELS = ("info", "warning", "critical")
def active_announcements(ledger: dict, *, now: Optional[datetime] = None) -> list[dict]:
    current = (now or utc_now()).astimezone(timezone.utc).replace(microsecond=0)
    visible = []
    for item in ledger.get("announcements", []):
        if not isinstance(item, dict):
            continue
        if item.get("archived_at") is not None:
            continue
        try:
            starts = parse_iso(str(item["starts_at"]))
            expires = parse_iso(str(item["expires_at"]))
        except (KeyError, TypeError, ValueError):
            continue
        if starts <= current < expires:
            visible.append(item)
    priority = {"critical": 0, "warning": 1, "info": 2}
    return sorted(
        visible,
        key=lambda item: (
            priority.get(str(item.get("level")), 3),
            str(item.get("created_at", "")),
        ),
    )


def publish_announcement(
    store,
    config: Config,
    actor: Actor,
    message: str,
    level: str,
    expires_in_seconds: int,
    *,
    starts_at: Optional[datetime] = None,
) -> dict:
    broker_publish = getattr(store, "broker_publish_announcement", None)
    if callable(broker_publish):
        return broker_publish(actor, message, level, expires_in_seconds, starts_at)
    _require_administrator(config, actor)
    message = _message(message)
    level = _level(level)
    if not 60 <= expires_in_seconds <= 365 * 86400:
        raise BookingError("announcement expiry must be between 1 minute and 365 days")
    now = utc_now()
    starts = (starts_at or now).astimezone(timezone.utc).replace(microsecond=0)
    expires = starts + timedelta(seconds=expires_in_seconds)
    announcement = {
        "id": str(uuid.uuid4()),
        "level": level,
        "message": message,
        "created_at": to_iso(now),
        "starts_at": to_iso(starts),
        "expires_at": to_iso(expires),
        "actor_uid": actor.uid,
        "actor_username": actor.username,
    }

    def mutate(ledger: dict):
        existing = ledger.get("announcements", [])
        if not isinstance(existing, list):
            raise BookingError("invalid announcement history")
        if len(existing) >= MAX_ANNOUNCEMENT_HISTORY:
            raise BookingError(
                "announcement archive is full; back up server data before migrating history"
            )
        ledger["announcements"] = [*existing, announcement]
        return ledger, announcement, [_audit_log("announcement.publish", actor, announcement)], True

    return store.transaction(mutate)


def archive_announcement(store, config: Config, actor: Actor, token: str) -> dict:
    broker_archive = getattr(store, "broker_archive_announcement", None)
    if callable(broker_archive):
        return broker_archive(actor, token)
    broker_remove = getattr(store, "broker_remove_announcement", None)
    if callable(broker_remove):
        return broker_remove(actor, token)
    _require_administrator(config, actor)

    def mutate(ledger: dict):
        matches = [
            item
            for item in ledger.get("announcements", [])
            if str(item.get("id", "")).startswith(token)
        ]
        if not matches:
            raise BookingError("announcement not found")
        if len(matches) > 1:
            raise BookingError("announcement ID prefix is ambiguous")
        target = matches[0]
        if target.get("archived_at") is not None:
            return ledger, target, [], False
        target["archived_at"] = to_iso(utc_now())
        target["archived_by_uid"] = actor.uid
        target["archived_by_username"] = actor.username
        return ledger, target, [_audit_log("announcement.archive", actor, target)], True

    return store.transaction(mutate)


def remove_announcement(store, config: Config, actor: Actor, token: str) -> dict:
    """Backward-compatible alias; announcements are archived, never deleted."""
    return archive_announcement(store, config, actor, token)


def edit_announcement(
    store,
    config: Config,
    actor: Actor,
    token: str,
    *,
    message: Optional[str] = None,
    level: Optional[str] = None,
    expires_in_seconds: Optional[int] = None,
    starts_at: Optional[datetime] = None,
    expires_at: Optional[datetime] = None,
) -> dict:
    broker_edit = getattr(store, "broker_edit_announcement", None)
    if callable(broker_edit):
        return broker_edit(
            actor,
            token,
            message,
            level,
            expires_in_seconds,
            starts_at,
            expires_at,
        )
    _require_administrator(config, actor)
    if message is not None:
        message = _message(message)
    if level is not None:
        level = _level(level)
    if expires_in_seconds is not None and not 60 <= expires_in_seconds <= 365 * 86400:
        raise BookingError("announcement expiry must be between 1 minute and 365 days")
    normalized_start = (
        starts_at.astimezone(timezone.utc).replace(microsecond=0)
        if starts_at is not None
        else None
    )
    normalized_expiry = (
        expires_at.astimezone(timezone.utc).replace(microsecond=0)
        if expires_at is not None
        else None
    )

    def mutate(ledger: dict):
        matches = [
            item
            for item in ledger.get("announcements", [])
            if str(item.get("id", "")).startswith(token)
        ]
        if not matches:
            raise BookingError("announcement not found")
        if len(matches) > 1:
            raise BookingError("announcement ID prefix is ambiguous")
        target = matches[0]
        if target.get("archived_at") is not None:
            raise BookingError("archived announcement cannot be edited")
        if message is not None:
            target["message"] = message
        if level is not None:
            target["level"] = level
        if normalized_start is not None:
            target["starts_at"] = to_iso(normalized_start)
        effective_start = parse_iso(str(target["starts_at"]))
        if normalized_expiry is not None:
            target["expires_at"] = to_iso(normalized_expiry)
        if expires_in_seconds is not None:
            target["expires_at"] = to_iso(
                utc_now() + timedelta(seconds=expires_in_seconds)
            )
        effective_expiry = parse_iso(str(target["expires_at"]))
        if effective_expiry <= effective_start:
            raise BookingError("announcement deadline must be after its start time")
        if effective_expiry - effective_start > timedelta(days=365):
            raise BookingError("announcement window must not exceed 365 days")
        target["updated_at"] = to_iso(utc_now())
        return ledger, target, [_audit_log("announcement.edit", actor, target)], True

    return store.transaction(mutate)


def _require_administrator(config: Config, actor: Actor) -> None:
    del config
    if actor.uid != 0:
        raise BookingError(
            "permission denied: administrator announcement requires sudo"
        )


def _level(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized not in ANNOUNCEMENT_LEVELS:
        raise BookingError("announcement level must be info, warning, or critical")
    return normalized


def _message(value: str) -> str:
    normalized = str(value).strip()
    if not 1 <= len(normalized) <= 1024:
        raise BookingError("announcement message must contain 1-1024 characters")
    if any(ord(char) < 32 and char not in "\t" for char in normalized):
        raise BookingError("announcement message contains control characters")
    return normalized


def _audit_log(action: str, actor: Actor, announcement: dict) -> dict:
    return {
        "timestamp": to_iso(utc_now()),
        "action": action,
        "uid": actor.uid,
        "username": actor.username,
        "announcement_id": announcement["id"],
        "announcement": dict(announcement),
        "result": "ok",
    }
