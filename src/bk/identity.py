from __future__ import annotations

import os
import pwd

from .models import Actor


def current_actor() -> Actor:
    """Return an identity whose display name is bound to the numeric process UID."""
    uid = os.getuid()
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        username = str(uid)
    return Actor(uid=uid, username=username)
