from __future__ import annotations

import os
import pwd
from dataclasses import dataclass
from typing import Optional

from .config import Config


ADMIN_INFO_SCHEMA_VERSION = "gpubk.administrator.v1"
MAX_GECOS_FIELD_LENGTH = 256


@dataclass(frozen=True)
class AdministratorInfo:
    uid: int
    username: str
    full_name: Optional[str] = None
    room: Optional[str] = None
    work_phone: Optional[str] = None
    home_phone: Optional[str] = None
    other: Optional[str] = None
    account_resolved: bool = True

    def as_dict(self) -> dict:
        return {
            "schema_version": ADMIN_INFO_SCHEMA_VERSION,
            "kind": "administrator",
            "source": "linux-account-gecos",
            "account_resolved": self.account_resolved,
            "account": {"uid": self.uid, "username": self.username},
            "full_name": self.full_name,
            "room": self.room,
            "contact": {
                "work_phone": self.work_phone,
                "home_phone": self.home_phone,
                "other": self.other,
            },
        }


def administrator_info(config: Config) -> AdministratorInfo:
    uid = _administrator_uid(config)
    try:
        account = pwd.getpwuid(uid)
    except KeyError:
        return AdministratorInfo(
            uid=uid,
            username=str(uid),
            account_resolved=False,
        )
    full_name, room, work_phone, home_phone, other = _gecos_fields(
        getattr(account, "pw_gecos", "")
    )
    return AdministratorInfo(
        uid=uid,
        username=_safe_field(getattr(account, "pw_name", "")) or str(uid),
        full_name=full_name,
        room=room,
        work_phone=work_phone,
        home_phone=home_phone,
        other=other,
    )


def administrator_display_lines(info: AdministratorInfo) -> tuple[str, ...]:
    identity = info.username
    if info.full_name:
        identity = f"{info.full_name} ({info.username})"
    lines = [f"Administrator: {identity}", f"Linux UID: {info.uid}"]
    if info.room:
        lines.append(f"Room: {info.room}")
    contacts = []
    if info.work_phone:
        contacts.append(f"work {info.work_phone}")
    if info.home_phone:
        contacts.append(f"home {info.home_phone}")
    if info.other:
        contacts.append(f"other {info.other}")
    lines.append(
        "Contact: " + ("; ".join(contacts) if contacts else "not provided in the Linux account")
    )
    if not info.account_resolved:
        lines.append("Account lookup is currently unavailable")
    return tuple(lines)


def _administrator_uid(config: Config) -> int:
    if config.broker_uid is not None:
        return config.broker_uid
    if config.monitor_uid is not None:
        return config.monitor_uid
    return os.getuid()


def _gecos_fields(value: object) -> tuple[Optional[str], ...]:
    parts = str(value or "").split(",", 4)
    parts.extend("" for _ in range(5 - len(parts)))
    return tuple(_safe_field(part) for part in parts)


def _safe_field(value: object) -> Optional[str]:
    cleaned = " ".join(
        "".join(
            character if character.isprintable() else " " for character in str(value)
        ).split()
    )
    return cleaned[:MAX_GECOS_FIELD_LENGTH] or None
