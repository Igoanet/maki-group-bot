"""Owner-managed list of Telegram users who may not use the bots at all.

ONE list for every bot in this project: the module lives in bots/shared/ and is symlinked into
each bot package, and the JSON store sits next to it (bots/shared/data/), so `/restrict` in the
music bot also applies in the group-help bot and the other way round. Each process re-reads the
file when its modification time changes; the hot-path check is a dict lookup plus, at most once
a second, one stat() call.

Standard library only — the module is shared by bots with different dependency sets.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("restrictions")

# resolve(): the real file, not the symlink. RESTRICTIONS_DATA_DIR is for offline tests only.
DATA_DIR = Path(os.environ.get("RESTRICTIONS_DATA_DIR") or Path(__file__).resolve().parent / "data")
STORE = DATA_DIR / "restricted_users.json"
LOCK = DATA_DIR / ".restricted_users.lock"
CHECK_EVERY = 1.0  # seconds between stat() calls on the hot path

_cache: dict[int, dict] = {}
_stamp: tuple[int, int, int] | None = None  # (mtime_ns, size, inode) the cache was read from
_checked = 0.0
_bad_stamp: tuple[int, int, int] | None = None  # last unreadable version — logged once, retried


def _stat() -> tuple[int, int, int] | None:
    try:
        st = STORE.stat()
    except FileNotFoundError:
        return None
    except OSError as e:  # permissions, I/O — keep the last good list, don't break the bot
        logger.error("restrictions: cannot stat %s (%r)", STORE, e)
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino  # every write is a fresh inode (os.replace)


def _read(quiet: bool = False) -> dict[int, dict] | None:
    """The store's users, or None when the file is unreadable (the caller keeps what it has)."""
    log = logger.debug if quiet else logger.error
    try:
        with STORE.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log("restrictions: %s is unreadable (%r) — keeping the last good list", STORE, e)
        return None
    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, dict):
        log("restrictions: %s has no 'users' object — keeping the last good list", STORE)
        return None
    out: dict[int, dict] = {}
    for key, rec in users.items():
        try:
            out[int(key)] = rec if isinstance(rec, dict) else {}
        except (TypeError, ValueError):
            continue
    return out


def _refresh(force: bool = False) -> None:
    global _cache, _stamp, _checked, _bad_stamp
    now = time.monotonic()
    if not force and now - _checked < CHECK_EVERY:
        return
    _checked = now
    stamp = _stat()
    if stamp == _stamp:
        return
    users = _read(quiet=stamp == _bad_stamp)
    if users is None:
        _bad_stamp = stamp  # logged once per broken version; retried every CHECK_EVERY
        return
    _cache, _stamp, _bad_stamp = users, stamp, None


def is_restricted(user_id: int | None) -> bool:
    if not user_id:
        return False
    _refresh()
    return user_id in _cache


def restricted() -> dict[int, dict]:
    """user id -> {"name", "by", "at"} — fresh from disk."""
    _refresh(force=True)
    return dict(_cache)


def _write(users: dict[int, dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"users": {str(k): v for k, v in sorted(users.items())}}
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix="restricted_users_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1, ensure_ascii=False)
        os.replace(tmp, STORE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _locked_update(change) -> bool:
    """Read-modify-write under a file lock: two bots may edit the same list."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            users = _read()
            if users is None:
                raise OSError(f"{STORE} is unreadable — fix or remove it first")
            if not change(users):
                return False
            _write(users)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    _refresh(force=True)
    return True


def restrict(user_id: int, *, by: int, name: str = "") -> bool:
    """Add a user; False when already restricted."""

    def change(users: dict[int, dict]) -> bool:
        if user_id in users:
            return False
        users[user_id] = {"name": name[:64], "by": by, "at": int(time.time())}
        return True

    return _locked_update(change)


def unrestrict(user_id: int) -> bool:
    """Remove a user; False when they weren't restricted."""

    def change(users: dict[int, dict]) -> bool:
        return users.pop(user_id, None) is not None

    return _locked_update(change)


def _ensure_store() -> None:
    """Create an empty store once so the hot path never has to handle a missing file."""
    if STORE.exists():
        return
    try:
        _locked_update(lambda users: True)  # writes {"users": {}} unless someone else just did
    except OSError as e:
        logger.warning("restrictions: could not create %s (%r)", STORE, e)


_ensure_store()
