"""Sudo users — the owner's trusted staff.

Sudo users get the bot-level operational commands (/broadcast, /groups,
/ginfo, /restrict …); owner-personal commands (/clearme, /fqallow, /massban,
/gsay, /gban …) stay owner-only. The list lives in the db "sudo_users"
global and is cached in memory; sudo_add/sudo_remove keep both in sync.
The owner is always sudo and can never be removed.
"""

from .config import config
from .database import db

SUDO_KEY = "sudo_users"  # db global: [user ids]

_users: set[int] = set()
_loaded = False


async def sudo_users() -> set[int]:
    """The live sudo set (the owner is NOT in it — is_sudo adds them)."""
    global _loaded
    if not _loaded:
        rows = await db.get(SUDO_KEY, []) or []
        _users.update(int(u) for u in rows)
        _loaded = True
    return _users


async def is_sudo(user_id: int) -> bool:
    """Owner or an appointed sudo user."""
    return bool(user_id) and (
        user_id == config.OWNER_ID or user_id in await sudo_users()
    )


async def sudo_add(user_id: int) -> bool:
    """Add to the sudo set + db. False when already sudo. The db is written
    first — the in-memory cache only changes after the write succeeds, so a
    failed save never grants access."""
    users = await sudo_users()
    if user_id in users:
        return False
    changed = []

    def _add(rows: list) -> bool:
        if user_id in rows:
            return False
        rows.append(user_id)
        changed.append(True)
        return True

    await db.mutate(SUDO_KEY, _add, [])
    users.add(user_id)  # cache follows the committed db state
    return bool(changed)


async def sudo_remove(user_id: int) -> bool:
    """Remove from the sudo set + db. The owner can never lose sudo. The db
    is written first — a failed save keeps the user's access (fail-safe)."""
    if user_id == config.OWNER_ID:
        return False
    users = await sudo_users()
    if user_id not in users:
        return False
    changed = []

    def _drop(rows: list) -> bool:
        if user_id not in rows:
            return False
        rows.remove(user_id)
        changed.append(True)
        return True

    await db.mutate(SUDO_KEY, _drop, [])
    users.discard(user_id)  # cache follows the committed db state
    return bool(changed)
