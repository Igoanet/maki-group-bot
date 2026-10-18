"""Group activity rankings for Maki.

The normal leaderboard is an in-memory Pillow image.  Text cards and their
larger callback surface are retained only so cards sent by older Maki
instances continue to work; a new card has exactly the three period buttons.
"""

from __future__ import annotations

import asyncio
import html
import inspect
import io
import logging
import re
import time
import unicodedata
import weakref
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.activity_store import (
    IST,
    MEDIA_KINDS,
    MEDIA_LABELS,
    MAX_PAGE,
    PERIODS,
    activity,
    utc,
)
from ghelp.chatfight_image import DEFAULT_TITLE_WIDTH, render_leaderboard
from ghelp.database import db
from ghelp.helpers import has_right, ikb, ikm, is_admin
from ghelp.selfdel import autodel_reply

logger = logging.getLogger(__name__)
GROUPS = {enums.ChatType.GROUP, enums.ChatType.SUPERGROUP}

# Callback data is always parsed and allow-listed below.  The shape is kept
# bounded so stale/hostile callbacks cannot become SQL fragments or huge page
# values.  ``member`` callbacks from the old text cards include a numeric ID.
CALLBACK = re.compile(r"^gh:cf:(-\d+):([a-z]+(?::[a-z0-9_-]+){0,4})$")
ALIASES = {
    "daily": "day",
    "today": "day",
    "weekly": "week",
    "overall": "all",
    "alltime": "all",
}
MEDIA_PAGE_KINDS = (*MEDIA_KINDS, "unknown")
COMMAND_DEFAULTS = {
    "chatfight": "day",
    "top": "all",
    "weektop": "week",
    "alltop": "all",
}
PERIOD_BUTTONS = {"day": "Today", "week": "Week", "all": "All time"}

# Avatar hydration is deliberately request-scoped.  Importing this plugin
# never asks Telegram for users or files; these limits only apply when a
# leaderboard image is requested.
_AVATAR_CACHE_TTL = 1800
_AVATAR_CACHE_MAX = 128
_AVATAR_MAX_BYTES = 2_000_000
_AVATAR_CONCURRENCY = 4
_AVATAR_TOTAL_TIMEOUT = 20
_AVATAR_LOOKUP_TIMEOUT = 6
_AVATAR_DOWNLOAD_TIMEOUT = 10
_avatar_cache = OrderedDict()
_avatar_inflight = {}
_avatar_gates = {}


@dataclass
class _CardState:
    """Generation and edit serialization for one visible leaderboard card."""

    generation: int = 0
    last_period: str = ""
    last_edit: str = ""
    edit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# A state is kept only while a callback task still references it.  This avoids
# a permanent lock per chat/message while still making rapid taps on one card
# share a generation and a final-edit lock.
_CARD_STATES = weakref.WeakValueDictionary()


def _card_key(message):
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(message, "id", None)
    if message_id is None:
        # Lightweight offline fakes may not have an id.  A real Telegram
        # message always does, and object identity is safe for the fallback.
        message_id = id(message)
    return chat_id, message_id


def _card_state_for(message):
    """The per-card state, creating it without bumping the generation."""
    key = _card_key(message)
    state = _CARD_STATES.get(key)
    if state is None:
        state = _CardState()
        _CARD_STATES[key] = state
    return state


def _claim_card_generation(message, period):
    state = _card_state_for(message)
    state.generation += 1
    state.last_period = period
    return state, state.generation


def _signature(*parts) -> str:
    """A stable fingerprint of one rendered card."""
    import hashlib

    digest = hashlib.sha256()
    for part in parts:
        digest.update(
            part
            if isinstance(part, (bytes, bytearray))
            else str(part).encode("utf-8", "replace")
        )
        digest.update(b"\x00")
    return digest.hexdigest()


# The edit fingerprint needs a STRONG reference: _CARD_STATES is a
# WeakValueDictionary, so a state stored there is collected as soon as the
# callback task drops it — which silently defeated de-duplication across
# separate taps.  This map is bounded instead.
_LAST_EDIT_MAX = 512
_last_edit: "OrderedDict[tuple, str]" = OrderedDict()


def _card_shows(message, payload: str) -> bool:
    """True when this card already displays exactly this payload.

    A repeated tap used to re-send an identical edit.  Telegram answers
    MessageNotModified and counts the attempt against the per-chat edit flood
    limit, so skipping the call is what actually stops FLOOD_WAIT_X.
    """
    return _last_edit.get(_card_key(message)) == payload


def _card_shown(message, payload: str) -> None:
    key = _card_key(message)
    _last_edit[key] = payload
    _last_edit.move_to_end(key)
    while len(_last_edit) > _LAST_EDIT_MAX:
        _last_edit.popitem(last=False)


_FLOOD_MAX_SLEEP = 120  # never freeze a callback handler longer than this


async def _flood_safe(call, *args, **kwargs):
    """Await one Telegram call, sleeping through a FloodWait once.

    Telegram states the exact number of seconds to wait in the error; ignoring
    it and retrying immediately is what keeps the same chat flooded.
    """
    from pyrogram.errors import FloodWait

    try:
        return await call(*args, **kwargs)
    except FloodWait as exc:
        wait = min(float(getattr(exc, "value", 0) or 0) + 1.0, _FLOOD_MAX_SLEEP)
        logger.info("Chat Fight: FloodWait %.0fs — sleeping, then one retry", wait)
        await asyncio.sleep(wait)
        return await call(*args, **kwargs)


async def _safe_edit(editor, *args, **kwargs):
    """Edit a card, treating "nothing changed" as success.

    Returns True when Telegram applied the edit and False when the card
    already showed that content.  MessageNotModified must never reach the
    legacy fallback that deletes and re-sends the card — that churn was the
    real source of the edit flood.
    """
    from pyrogram.errors import FloodWait, MessageNotModified

    try:
        await _flood_safe(editor, *args, **kwargs)
        return True
    except MessageNotModified:
        return False
    except FloodWait:
        # Still limited after the retry: leave the card as it is.  Re-posting
        # it would delete the card and feed the same flood.
        logger.warning("Chat Fight: edit still flood-limited; card left unchanged")
        return False


def _card_is_current(state, generation, period):
    return state.generation == generation and state.last_period == period


@autodel_reply
async def _reply(message, text, **kwargs):
    return await message.reply_text(text, parse_mode=enums.ParseMode.HTML, **kwargs)


def _cb(chat_id, action):
    return f"gh:cf:{chat_id}:{action}"


def _main_keyboard(chat_id, period):
    """The only keyboard placed on a new leaderboard card."""

    return ikm([[
        ikb(
            PERIOD_BUTTONS[key],
            _cb(chat_id, key),
            style=enums.ButtonStyle.SUCCESS if key == period else enums.ButtonStyle.PRIMARY,
        )
        for key in PERIODS
    ]])


# Kept as a source-compatible name for integrations that imported the old
# helper.  New cards never add details, rank, media, pager, or pause buttons.
def _keyboard(chat_id, period, enabled=True, page=0, pages=1, entries=None):
    return _main_keyboard(chat_id, period)


def _member_action(period, media_kind, user_id):
    return f"member:{period}:{media_kind or 'all'}:{user_id}"


def _member_buttons(button, period, media_kind, entries):
    """Legacy text-card details buttons.

    This helper is intentionally not called by ``_keyboard``.  It remains
    available for callbacks and for text media analytics opened explicitly by
    ``/chatfight media``.
    """

    rows = []
    for row in entries or ():
        label = " ".join(str(row.get("name") or row["user_id"]).split())[:28]
        rows.append([
            button(f"Details: {label}", _member_action(period, media_kind, row["user_id"]))
        ])
    return rows


def _user_id(value):
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if 0 < value <= 2**63 - 1 else None


def _avatar_client_key(client):
    # Only the authenticated Maki client is ever passed to these helpers.
    return id(client)


def _prune_avatar_cache(now=None):
    now = time.monotonic() if now is None else now
    for key, (expires, _profile) in list(_avatar_cache.items()):
        if expires <= now:
            _avatar_cache.pop(key, None)
    while len(_avatar_cache) > _AVATAR_CACHE_MAX:
        _avatar_cache.popitem(last=False)


def _cached_avatar_profile(client, user_id):
    now = time.monotonic()
    _prune_avatar_cache(now)
    key = (_avatar_client_key(client), user_id)
    cached = _avatar_cache.get(key)
    if cached is None:
        return None
    _avatar_cache.move_to_end(key)
    # Profile values are immutable scalars/bytes, but return a fresh mapping
    # so a caller cannot mutate the bounded shared cache.
    return dict(cached[1])


def _cache_avatar_profile(client, user_id, profile):
    now = time.monotonic()
    _prune_avatar_cache(now)
    key = (_avatar_client_key(client), user_id)
    _avatar_cache[key] = (now + _AVATAR_CACHE_TTL, dict(profile))
    _avatar_cache.move_to_end(key)
    _prune_avatar_cache(now)


def _avatar_gate(client):
    loop = asyncio.get_running_loop()
    key = (_avatar_client_key(client), id(loop))
    gate = _avatar_gates.get(key)
    if gate is None:
        gate = asyncio.Semaphore(_AVATAR_CONCURRENCY)
        _avatar_gates[key] = gate
    return gate


async def _maybe_await(value, timeout):
    if inspect.isawaitable(value):
        return await asyncio.wait_for(value, timeout=timeout)
    return value


def _stream_download(downloaded):
    if isinstance(downloaded, (bytes, bytearray, memoryview)):
        return bytes(downloaded)
    getter = getattr(downloaded, "getvalue", None)
    if callable(getter):
        raw = getter()
    else:
        reader = getattr(downloaded, "read", None)
        if not callable(reader):
            return None
        raw = reader()
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(raw)
    return raw if len(raw) <= _AVATAR_MAX_BYTES else None


def _valid_avatar(raw):
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return False
    raw = bytes(raw)
    if not raw or len(raw) > _AVATAR_MAX_BYTES:
        return False
    return (
        raw.startswith(b"\x89PNG\r\n\x1a\n")
        or raw.startswith(b"\xff\xd8\xff")
        or (raw.startswith(b"RIFF") and len(raw) >= 12 and raw[8:12] == b"WEBP")
    )


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _username(user):
    value = _field(user, "username")
    if not isinstance(value, str):
        return None
    value = value.strip().lstrip("@")
    # Telegram usernames are ASCII and this allow-list also prevents an
    # attribute from becoming HTML/URL syntax in a caption.
    return value if re.fullmatch(r"[A-Za-z0-9_]{5,32}", value) else None


def _display_user(user):
    parts = [
        _field(user, "first_name"),
        _field(user, "last_name"),
    ]
    value = " ".join(str(part).strip() for part in parts if part)
    return value or None


def _photo_candidates(user):
    """Return preferred real profile file IDs, largest first."""

    candidates = []
    for photo_name in ("photo", "personal_photo", "public_photo"):
        photo = _field(user, photo_name)
        if photo is None:
            continue
        # The card draws an 82px circle, so the small thumbnail is already
        # more than enough and downloads far quicker than the full-size
        # photo — which is what used to time out and leave a blank initial.
        for field_name in ("small_file_id", "file_id", "big_file_id"):
            file_id = _field(photo, field_name)
            if not file_id or any(existing[0] == file_id for existing in candidates):
                continue
            # Kurigram's ChatPhoto does not expose sizes, so the actual
            # in-memory download remains the authority.  Test doubles and
            # newer SDKs may expose per-variant sizes; do not let an
            # oversized *big* declaration suppress a legitimate small thumb.
            size_field = {
                "big_file_id": "big_file_size",
                "small_file_id": "small_file_size",
                "file_id": "file_size",
            }[field_name]
            declared = _field(photo, size_field)
            if declared is None and field_name == "file_id":
                declared = _field(photo, "file_size")
            try:
                declared = int(declared or 0)
            except (TypeError, ValueError, OverflowError):
                declared = 0
            candidates.append((file_id, declared))
    return candidates


async def _fetch_avatar_profile(client, user_id):
    key = (_avatar_client_key(client), user_id)
    task = asyncio.current_task()
    deadline = asyncio.get_running_loop().time() + _AVATAR_TOTAL_TIMEOUT
    profile = {"username": None, "displayUser": None, "avatar": None}
    acquired = False
    try:
        lookup = getattr(client, "get_users", None)
        download = getattr(client, "download_media", None)
        if not callable(lookup):
            return profile
        gate = _avatar_gate(client)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return profile
        await asyncio.wait_for(gate.acquire(), timeout=remaining)
        acquired = True
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return profile

        # ``get_users`` accepts a numeric ID in Pyrogram/Kurigram.  It is
        # intentionally the only profile lookup used here.
        result = lookup(user_id)
        user = await _maybe_await(result, min(_AVATAR_LOOKUP_TIMEOUT, remaining))
        if isinstance(user, (list, tuple)):
            user = user[0] if user else None
        if user is None:
            _cache_avatar_profile(client, user_id, profile)
            return profile
        profile["username"] = _username(user)
        profile["displayUser"] = _display_user(user)

        candidates = _photo_candidates(user)
        avatar_download_failed = False
        if callable(download):
            for file_id, declared_size in candidates:
                if declared_size and declared_size > _AVATAR_MAX_BYTES:
                    continue
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    downloaded = download(file_id, in_memory=True)
                    downloaded = await _maybe_await(
                        downloaded, min(_AVATAR_DOWNLOAD_TIMEOUT, remaining)
                    )
                    raw = _stream_download(downloaded)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    avatar_download_failed = True
                    logger.info(
                        "Chat Fight avatar unavailable for user %s: %s",
                        user_id,
                        type(exc).__name__,
                    )
                    continue
                if _valid_avatar(raw):
                    profile["avatar"] = bytes(raw)
                    break
                avatar_download_failed = True
        # Retry a real DP on the next request after transient download errors.
        if profile["avatar"] is not None or not candidates or not avatar_download_failed:
            _cache_avatar_profile(client, user_id, profile)
        return dict(profile)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.info(
            "Chat Fight profile lookup unavailable for user %s: %s",
            user_id,
            type(exc).__name__,
        )
        # Do not cache a temporary Telegram lookup failure as "no DP".
        return dict(profile)
    finally:
        if acquired:
            gate.release()
        if _avatar_inflight.get(key) is task:
            _avatar_inflight.pop(key, None)


async def _avatar_profile(client, user_id):
    cached = _cached_avatar_profile(client, user_id)
    if cached is not None:
        return cached
    key = (_avatar_client_key(client), user_id)
    task = _avatar_inflight.get(key)
    if task is None:
        task = asyncio.create_task(_fetch_avatar_profile(client, user_id))
        _avatar_inflight[key] = task
    try:
        # A cancelled callback must not cancel a shared cold profile request.
        return dict(await asyncio.shield(task))
    except asyncio.CancelledError:
        raise
    except Exception:
        return {"username": None, "displayUser": None, "avatar": None}


async def _hydrate_leaderboard(data):
    """Attach request-time usernames/photos while preserving store values."""

    hydrated = dict(data)
    entries = []
    for row in list(data.get("entries") or ())[:10]:
        if isinstance(row, Mapping):
            entries.append(dict(row))
        else:
            entries.append({})
    hydrated["entries"] = entries

    if not callable(getattr(app, "get_users", None)):
        return hydrated
    jobs = []
    for row in entries:
        user_id = _user_id(row.get("user_id"))
        jobs.append(_avatar_profile(app, user_id) if user_id is not None else None)
    if not jobs:
        return hydrated
    try:
        profiles = await asyncio.wait_for(
            asyncio.gather(
                *(job if job is not None else asyncio.sleep(0, result=None)
                  for job in jobs),
                return_exceptions=True,
            ),
            timeout=_AVATAR_TOTAL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        profiles = [None] * len(entries)
    for row, profile in zip(entries, profiles):
        if not isinstance(profile, dict):
            continue
        if profile.get("avatar") is not None:
            row["avatar"] = profile["avatar"]
        if profile.get("username"):
            row["username"] = profile["username"]
        if profile.get("displayUser"):
            row["displayUser"] = profile["displayUser"]
    return hydrated


def _legacy_keyboard(chat_id, period, enabled, page=0, pages=1, entries=None):
    """Recreate enough of the old text card to honour old callbacks."""

    def button(text, action, style=enums.ButtonStyle.PRIMARY):
        return ikb(text, _cb(chat_id, action), style=style)

    rows = [[
        button(label, key, enums.ButtonStyle.SUCCESS if key == period else enums.ButtonStyle.PRIMARY)
        for key, label in PERIODS.items()
    ]]
    rows.append([button("Media types", f"media:{period}"),
                 button("My rank", "rank" + period), button("Refresh", period)])
    if pages > 1:
        pager = []
        if page:
            pager.append(button("Previous", f"main:{period}:{page - 1}"))
        pager.append(button(f"Page {page + 1}/{pages}", f"main:{period}:{page}"))
        if page + 1 < pages:
            pager.append(button("Next", f"main:{period}:{page + 1}"))
        rows.append(pager)
    rows.extend(_member_buttons(button, period, None, entries))
    return ikm(rows)


def _media_keyboard(chat_id, period, enabled, kind=None, page=0, pages=1, entries=None):
    """Keyboard for explicitly requested typed-media analytics.

    It is separate from the new image card.  Counting controls are deliberately
    absent here: pause/resume is an explicit ``/chatfight on|off`` command.
    """

    def button(text, action, style=enums.ButtonStyle.PRIMARY):
        return ikb(text, _cb(chat_id, action), style=style)

    rows = [[
        button(label, f"kind:{key}:{kind}:0" if kind else f"media:{key}",
               enums.ButtonStyle.SUCCESS if key == period else enums.ButtonStyle.PRIMARY)
        for key, label in PERIODS.items()
    ]]
    if kind is None:
        rows.append([button("Messages", f"main:{period}:0")])
    else:
        rows.append([button("Media types", f"media:{period}")])
        if pages > 1:
            pager = []
            if page:
                pager.append(button("Previous", f"kind:{period}:{kind}:{page - 1}"))
            pager.append(button(f"Page {page + 1}/{pages}", f"kind:{period}:{kind}:{page}"))
            if page + 1 < pages:
                pager.append(button("Next", f"kind:{period}:{kind}:{page + 1}"))
            rows.append(pager)
        rows.extend(_member_buttons(button, period, kind, entries))
    rows.append([button("My rank", f"rank:{period}:{kind}" if kind else f"rank{period}")])
    return ikm(rows)


def _media_menu_keyboard(chat_id, period, counts):
    rows = [[
        ikb(
            label,
            _cb(chat_id, f"media:{key}"),
            style=enums.ButtonStyle.SUCCESS if key == period else enums.ButtonStyle.PRIMARY,
        )
        for key, label in PERIODS.items()
    ]]
    available = [kind for kind in MEDIA_PAGE_KINDS if counts.get(kind, 0)]
    if not available:
        available = ["text", "photo", "video", "document", "voice", "unknown"]
    for index in range(0, len(available), 2):
        rows.append([
            ikb(
                f"{MEDIA_LABELS[kind]} ({counts.get(kind, 0)})",
                _cb(chat_id, f"kind:{period}:{kind}:0"),
                style=enums.ButtonStyle.PRIMARY,
            )
            for kind in available[index:index + 2]
        ])
    rows.append([ikb(
        "Messages",
        _cb(chat_id, f"main:{period}:0"),
        style=enums.ButtonStyle.PRIMARY,
    )])
    return ikm(rows)


def _text(data):
    """Legacy HTML text card and offline/test fallback only."""

    title = PERIODS[data["period"]]
    lines = [
        f"<b>Chat Fight · {title}</b>",
        f"{data['messages']} messages · {data['people']} members",
        "",
    ]
    if data.get("pages", 1) > 1:
        lines[1] += f" · page {data['page'] + 1}/{data['pages']}"
    for row in data["entries"]:
        name = html.escape(str(row.get("name") or row["user_id"])[:40])
        lines.append(
            f"{row['rank']}. <a href=\"tg://user?id={row['user_id']}\">"
            f"{name}</a> — <b>{row['count']}</b>"
        )
    if not data["entries"]:
        lines.append("No counted messages in this period yet.")
    since = datetime.fromisoformat(data["started_at"]).astimezone(IST).strftime("%d %b %Y")
    lines.extend([
        "",
        f"Tracking since {since}. New member messages only.",
        "Bots, commands and edits excluded. Days/weeks: IST; week starts Monday.",
    ])
    if not data["enabled"]:
        lines.append("<b>Counting paused.</b> Existing scores are saved.")
    return "\n".join(lines)


def _media_text(data):
    title = PERIODS[data["period"]]
    label = MEDIA_LABELS[data["media_kind"]]
    lines = [
        f"<b>Chat Fight · {title} · {html.escape(label)}</b>",
        f"{data['messages']} {html.escape(label.lower())} messages · {data['people']} members",
        "",
    ]
    if data.get("pages", 1) > 1:
        lines[1] += f" · page {data['page'] + 1}/{data['pages']}"
    for row in data["entries"]:
        name = html.escape(str(row.get("name") or row["user_id"])[:40])
        lines.append(
            f"{row['rank']}. <a href=\"tg://user?id={row['user_id']}\">"
            f"{name}</a> — <b>{row['count']}</b>"
        )
    if not data["entries"]:
        lines.append(f"No counted {html.escape(label.lower())} messages in this period yet.")
    since = datetime.fromisoformat(data["started_at"]).astimezone(IST).strftime("%d %b %Y")
    lines.extend([
        "",
        f"Tracking since {since}. New member messages only.",
        "Bots, commands and edits excluded. Days/weeks: IST; week starts Monday.",
        "Older activity whose type was not recorded remains unclassified.",
    ])
    if not data["enabled"]:
        lines.append("<b>Counting paused.</b> Existing scores are saved.")
    return "\n".join(lines)


def _member_text(data, media_kind=None):
    title = PERIODS[data["period"]]
    name = html.escape(str(data["name"])[:40])
    lines = [
        f"<b>Chat Fight · {title} · Member details</b>",
        f'<a href="tg://user?id={data["user_id"]}">{name}</a>',
        f"Total messages: <b>{data['total']}</b>",
        "",
    ]
    if media_kind:
        label = html.escape(MEDIA_LABELS[media_kind])
        lines += [f"{label} in this period: <b>{data['counts'].get(media_kind, 0)}</b>", ""]
    for kind in MEDIA_PAGE_KINDS:
        count = data["counts"].get(kind, 0)
        if count:
            lines.append(f"{html.escape(MEDIA_LABELS[kind])}: <b>{count}</b>")
    if not any(data["counts"].get(kind, 0) for kind in MEDIA_PAGE_KINDS):
        lines.append("No counted messages in this period yet.")
    since = datetime.fromisoformat(data["started_at"]).astimezone(IST).strftime("%d %b %Y")
    lines.extend([
        "",
        f"Tracking since {since}. New member messages only.",
        "Older activity whose type was not recorded remains unclassified.",
    ])
    if not data["enabled"]:
        lines.append("<b>Counting paused.</b> Existing scores are saved.")
    return "\n".join(lines)


def _member_keyboard(chat_id, period, enabled, media_kind, user_id):
    def button(text, action, style=enums.ButtonStyle.PRIMARY):
        return ikb(text, _cb(chat_id, action), style=style)

    token = media_kind or "all"
    rows = [[
        button(
            label, f"member:{key}:{token}:{user_id}",
            enums.ButtonStyle.SUCCESS if key == period else enums.ButtonStyle.PRIMARY,
        )
        for key, label in PERIODS.items()
    ]]
    back = f"kind:{period}:{media_kind}:0" if media_kind else f"main:{period}:0"
    rows.append([button("Back to rankings", back)])
    rows.append([button("My rank", f"rank:{period}:{media_kind}" if media_kind else f"rank{period}")])
    return ikm(rows)


def _parse_action(action):
    """Parse only allow-listed legacy and new callback actions."""

    if action in {"pause", "enable"}:
        return {"view": "toggle", "action": action}
    if action in PERIODS:
        return {"view": "main", "period": action, "page": 0}
    if action.startswith("rank") and action[4:] in PERIODS:
        return {"view": "rank", "period": action[4:], "media_kind": None}
    parts = action.split(":")
    if parts[0] == "media" and len(parts) == 2 and parts[1] in PERIODS:
        return {"view": "media_menu", "period": parts[1]}
    if parts[0] == "main" and len(parts) == 3 and parts[1] in PERIODS:
        if parts[2].isdigit() and int(parts[2]) <= MAX_PAGE:
            return {"view": "main", "period": parts[1], "page": int(parts[2])}
        return None
    if parts[0] == "kind" and len(parts) == 4 and parts[1] in PERIODS:
        if parts[2] not in MEDIA_PAGE_KINDS or not parts[3].isdigit():
            return None
        if int(parts[3]) > MAX_PAGE:
            return None
        return {"view": "media", "period": parts[1], "media_kind": parts[2],
                "page": int(parts[3])}
    if parts[0] == "rank" and len(parts) == 3 and parts[1] in PERIODS:
        if parts[2] not in MEDIA_PAGE_KINDS:
            return None
        return {"view": "rank", "period": parts[1], "media_kind": parts[2]}
    if parts[0] == "member" and len(parts) == 4 and parts[1] in PERIODS:
        if parts[2] == "all":
            media_kind = None
        elif parts[2] in MEDIA_PAGE_KINDS:
            media_kind = parts[2]
        else:
            return None
        if not parts[3].isdigit() or not 0 < int(parts[3]) <= 2**63 - 1:
            return None
        return {"view": "member", "period": parts[1], "media_kind": media_kind,
                "user_id": int(parts[3])}
    return None


def _media_menu_text(summary):
    title = PERIODS[summary["period"]]
    lines = [
        f"<b>Chat Fight · {title} · Media types</b>",
        "Choose a type to see named member rankings.",
        "",
    ]
    for kind in MEDIA_PAGE_KINDS:
        count = summary["counts"].get(kind, 0)
        if count:
            lines.append(f"{html.escape(MEDIA_LABELS[kind])}: <b>{count}</b>")
    if not any(summary["counts"].get(kind, 0) for kind in MEDIA_PAGE_KINDS):
        lines.append("No media-type counts in this period yet.")
    lines.extend([
        "",
        "Older activity whose type was not recorded remains unclassified.",
        "Days/weeks: IST; week starts Monday.",
    ])
    return "\n".join(lines)


def _caption(data):
    """Build the image's compact, linked top-ten Telegram text fallback.

    Telegram's limit is measured in UTF-16 code units, not Python code
    points.  Names are clipped before escaping and links are never clipped,
    so Unicode names and every retained total remain parseable HTML.
    """

    period = data.get("period", "day")
    title = PERIODS.get(period, str(period).title())

    def count(value):
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def units(value):
        return len(value.encode("utf-16-le", "surrogatepass")) // 2

    def clip_units(value, limit):
        if limit <= 0:
            return ""
        result = ""
        for char in str(value):
            candidate = result + char
            if units(candidate) > limit:
                break
            result = candidate
        return result

    def safe_name(row):
        candidates = (
            row.get("name"),
            row.get("display_name"),
            row.get("displayUser"),
            row.get("user_id"),
            "Unknown member",
        )
        # Keep Unicode letters/emoji but prevent control characters from
        # making a caption visually empty or changing Telegram's parser.
        for candidate in candidates:
            if candidate is None:
                continue
            value = "".join(
                char for char in str(candidate)
                if unicodedata.category(char) not in {"Cc", "Cf"}
            ).strip()
            if value:
                return value
        return "Unknown member"

    def link(row, name):
        username = row.get("username")
        if isinstance(username, str):
            username = username.strip().lstrip("@")
        if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            href = f"https://t.me/{username}"
        else:
            user_id = _user_id(row.get("user_id"))
            if user_id is None:
                return html.escape(name, quote=False)
            href = f"tg://user?id={user_id}"
        return f'<a href="{html.escape(href, quote=True)}">{html.escape(name)}</a>'

    entries = [row for row in (data.get("entries") or ()) if isinstance(row, Mapping)][:10]
    # Keep this line compact enough that all ten links have ample room even
    # with long IDs and emoji names.
    def rows_text(caps):
        return [
            f"{_user_id(row.get('rank')) or index + 1}. "
            f"{link(row, clip_units(safe_name(row), cap))}"
            f" — <b>{count(row.get('count')):,}</b>"
            for index, (row, cap) in enumerate(zip(entries, caps))
        ]

    header = (
        f"<b>Chat Fight · {html.escape(str(title))}</b>\n"
        f"<b>{count(data.get('messages')):,} messages total</b> · "
        f"{count(data.get('people')):,} members"
    )
    footer = "Today: /chatfight · Week: /weektop · All time: /top"
    if not data.get("enabled", True):
        footer += "\n<b>Counting is paused; existing scores are saved.</b>"

    caps = [64] * len(entries)
    body = rows_text(caps)
    caption = "\n".join([header, *body, footer])
    # Shrink only names until all ranking links fit.  In normal data this loop
    # runs zero times; it protects against adversarial Unicode/HTML names.
    while units(caption) > 1024 and any(cap > 1 for cap in caps):
        largest = max(range(len(caps)), key=lambda index: caps[index])
        caps[largest] = max(1, caps[largest] - 8)
        body = rows_text(caps)
        caption = "\n".join([header, *body, footer])

    if units(caption) > 1024:
        # Link/total overhead itself is bounded for Telegram IDs.  As a last
        # line of defence, retain complete HTML lines only; never raw-slice a
        # closing tag and silently turn a valid caption into broken markup.
        kept = [header]
        for line in body:
            candidate = "\n".join([*kept, line, footer])
            if units(candidate) <= 1024:
                kept.append(line)
        with_footer = "\n".join([*kept, footer])
        caption = with_footer if units(with_footer) <= 1024 else "\n".join(kept)
    return caption


async def _image(data, group_name):
    # Pillow is CPU work and must never block Pyrogram's update loop.
    raw = await asyncio.to_thread(render_leaderboard, data, group_name)
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise RuntimeError("Chat Fight renderer returned no image")
    photo = io.BytesIO(bytes(raw))
    photo.name = "chatfight.png"
    return photo


async def _image_for_chat(message, data):
    settings = await db.get_chat(message.chat.id)
    styled = {
        **data,
        "title_width_percent": settings.get("chatfight_title_width", DEFAULT_TITLE_WIDTH),
    }
    return await _image(styled, getattr(message.chat, "title", None) or "Group")


async def _send_main(message, data):
    """Send a new image card, with a test-only text path for old fake messages."""

    if not callable(getattr(message, "reply_photo", None)):
        # Real Kurigram Message objects always have reply_photo.  This branch
        # keeps old offline callers that only model reply_text deterministic;
        # it is never a production image fallback.
        return await _reply(message, _text(data), reply_markup=_main_keyboard(
            message.chat.id, data["period"]))
    data = await _hydrate_leaderboard(data)
    photo = await _image_for_chat(message, data)
    return await message.reply_photo(
        photo=photo,
        caption=_caption(data),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=_main_keyboard(message.chat.id, data["period"]),
    )


async def _replace_main(message, data, *, photo=None):
    """Turn a legacy text card into a photo without deleting it first."""

    keyboard = _main_keyboard(message.chat.id, data["period"])
    caption = _caption(data)
    if photo is None:
        photo = await _image_for_chat(message, data)
    # The rendered bytes are part of the identity: two taps can share a
    # caption but produce a different avatar, and that must still refresh.
    payload = _signature("photo", caption, bytes(photo.getvalue()))
    if _card_shows(message, payload):
        return
    media = types.InputMediaPhoto(
        media=photo,
        caption=caption,
        parse_mode=enums.ParseMode.HTML,
    )
    editor = getattr(message, "edit_media", None)
    if callable(editor):
        try:
            # ``Message.edit_media`` accepts the InputMedia object as its
            # first positional argument in native Kurigram.
            applied = await _safe_edit(editor, media, reply_markup=keyboard)
        except Exception:
            # A genuine refusal (Telegram will not put media on a text
            # message) falls through to the safe replacement below.
            # MessageNotModified never reaches here: _safe_edit absorbs it,
            # because "already correct" is not a failure.
            logger.info("Chat Fight card needs legacy replacement", exc_info=True)
        else:
            if applied:
                _card_shown(message, payload)
            return

    sender = getattr(app, "send_photo", None)
    chat = getattr(message, "chat", None)
    if callable(sender) and chat is not None:
        photo.seek(0)
        # Native Kurigram signature: send_photo(chat_id, photo, caption=...).
        await _flood_safe(
            sender,
            chat.id,
            photo,
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=keyboard,
        )
        _card_shown(message, payload)
        deleter = getattr(message, "delete", None)
        if callable(deleter):
            try:
                await deleter()
            except Exception:
                logger.warning("Could not delete replaced Chat Fight text card", exc_info=True)
        return

    # Only minimal fake messages used by the pre-image offline suite arrive
    # here.  Do not use this for a real Telegram Message (which always has
    # edit_media and app.send_photo).
    editor = getattr(message, "edit_text", None)
    if callable(editor):
        await editor(_text(data), parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)
        return
    raise RuntimeError("Cannot replace Chat Fight message with an image")


async def _update_main_card(query, message, data, state, generation, notice=None):
    """Render and edit one card while making the newest tap win.

    Rendering happens outside ``edit_lock`` so a slow Pillow job does not
    serialize other taps.  The generation checks before and after rendering
    discard stale work; only the short API edit is serialized.  Consequently,
    an older edit already in flight can finish, but the newer edit always runs
    after it and is the final Telegram state.
    """

    period = data["period"]
    if not _card_is_current(state, generation, period):
        await _safe_answer(query, "Updated elsewhere.")
        return
    data = await _hydrate_leaderboard(data)
    if not _card_is_current(state, generation, period):
        await _safe_answer(query, "Updated elsewhere.")
        return
    photo = await _image_for_chat(message, data)
    if not _card_is_current(state, generation, period):
        await _safe_answer(query, "Updated elsewhere.")
        return
    async with state.edit_lock:
        if not _card_is_current(state, generation, period):
            await _safe_answer(query, "Updated elsewhere.")
            return
        await _replace_main(message, data, photo=photo)
        current = _card_is_current(state, generation, period)
    await _safe_answer(query, notice or ("Updated." if current else "Updated elsewhere."))


async def _safe_answer(query, text="", *, alert=False):
    try:
        await query.answer(text, show_alert=alert)
    except Exception:
        logger.debug("Could not answer Chat Fight callback", exc_info=True)


def _command_parts(message):
    values = getattr(message, "command", None)
    if values:
        return [str(value) for value in values]
    return (getattr(message, "text", None) or "").split()


async def _title_setting(message, args):
    usage = (
        "Use <code>/chatfight title 35</code> (20–60% width), "
        "or <code>/chatfight title reset</code>."
    )
    if len(args) not in (2, 3):
        return await _reply(message, usage)
    if len(args) == 3:
        value = args[2].lower()
        if value == "reset":
            percent = DEFAULT_TITLE_WIDTH
        elif re.fullmatch(r"[0-9]{1,2}%?", value):
            percent = int(value.rstrip("%"))
        else:
            return await _reply(message, usage)
        if not 20 <= percent <= 60:
            return await _reply(message, usage)
    user = getattr(message, "from_user", None)
    if not user or not await has_right(message.chat.id, user.id,
                                       "can_change_info"):
        return await _reply(
            message,
            "You need the <b>Change group info</b> admin right to change the "
            "leaderboard title size.",
        )
    try:
        if len(args) == 2:
            settings = await db.get_chat(message.chat.id)
            percent = settings.get("chatfight_title_width", DEFAULT_TITLE_WIDTH)
            return await _reply(message, f"Title width: <b>{percent}%</b>.\n{usage}")
        await db.update_chat(message.chat.id, chatfight_title_width=percent)
    except Exception:
        logger.exception("Chat Fight title setting failed")
        return await _reply(message, "Title size could not be saved. Please try again.")
    await _reply(
        message,
        f"Title width saved: <b>{percent}%</b>. "
        "Open <code>/chatfight</code> or tap a period button to see it.",
    )


@app.on_message(filters.command(["chatfight", "top", "weektop", "alltop"]))
async def chatfight(_, message):
    if getattr(getattr(message, "chat", None), "type", None) not in GROUPS:
        return await _reply(message, "Use <code>/chatfight</code> inside your group.")
    args = _command_parts(message)
    command = args[0].lower().split("@", 1)[0].lstrip("/!?") if args else "chatfight"
    default = COMMAND_DEFAULTS.get(command, "day")
    requested = args[1].lower() if len(args) > 1 else ""
    if requested == "title" and command == "chatfight":
        return await _title_setting(message, args)
    if requested in {"on", "off"} and command == "chatfight":
        if len(args) != 2:
            return await _reply(message, "Use <code>/chatfight on</code> or <code>/chatfight off</code>.")
        user = getattr(message, "from_user", None)
        if not user or not await has_right(message.chat.id, user.id,
                                           "can_change_info"):
            return await _reply(
                message,
                "You need the <b>Change group info</b> admin right to change "
                "Chat Fight counting.",
            )
        await activity.set_enabled(message.chat.id, requested == "on")
        requested = ""
    elif requested == "media" and command == "chatfight":
        media_period = args[2].lower() if len(args) > 2 else "day"
        media_period = ALIASES.get(media_period, media_period)
        if media_period not in PERIODS or len(args) > 3:
            return await _reply(message, "Use <code>/chatfight media [day|week|all]</code>.")
        try:
            summary = await activity.media_summary(message.chat.id, media_period)
            return await _reply(
                message,
                _media_menu_text(summary),
                reply_markup=_media_menu_keyboard(
                    message.chat.id, media_period, summary["counts"]),
            )
        except Exception:
            logger.exception("Chat Fight media menu failed")
            return await _reply(message, "Chat Fight media analytics is unavailable right now.")

    period = ALIASES.get(requested, requested) if requested else default
    if period not in PERIODS or (len(args) > 2 and not (
        command == "chatfight" and args[1].lower() in {"on", "off"}
    )):
        return await _reply(
            message,
            "Use <code>/chatfight</code>, <code>/weektop</code> or <code>/top</code>; "
            "add <code>day</code>, <code>week</code> or <code>all</code>.",
        )
    status = None
    try:
        try:
            status = await message.reply_text("Sending Chat Fight…")
        except Exception:
            # A temporary status must never block the actual leaderboard.
            status = None
        data = await activity.snapshot(message.chat.id, period, page=0)
        await _send_main(message, data)
    except Exception:
        logger.exception("Chat Fight image failed")
        # This is intentionally explicit: image generation failures must not
        # masquerade as a successful text leaderboard.
        await _reply(message, "Chat Fight image could not be generated. Please try again.")
    finally:
        if status is not None and hasattr(status, "delete"):
            try:
                await status.delete()
            except Exception:
                pass


@app.on_callback_query(filters.regex(CALLBACK.pattern))
async def chatfight_callback(_, query):
    raw = query.data if isinstance(getattr(query, "data", None), str) else ""
    match = CALLBACK.fullmatch(raw)
    message = getattr(query, "message", None)
    chat = getattr(message, "chat", None)
    user = getattr(query, "from_user", None)
    action = _parse_action(match[2]) if match else None
    if not match or not action or not user or not chat or chat.type not in GROUPS or chat.id != int(match[1]):
        return await _safe_answer(query, "Open Chat Fight in the original group.", alert=True)
    chat_id = chat.id
    card_state = card_generation = None
    # Claim the generation before the membership RPC.  Two taps are then
    # ordered by callback arrival rather than by whichever auth lookup happens
    # to return first.
    if action["view"] == "main":
        card_state, card_generation = _claim_card_generation(message, action["period"])
    try:
        member = await app.get_chat_member(chat_id, user.id)
        valid_statuses = {
            enums.ChatMemberStatus.OWNER,
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.MEMBER,
        }
        if member.status not in valid_statuses and not (
            member.status == enums.ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False)
        ):
            return await _safe_answer(query, "Join this group to view its rankings.", alert=True)

        toggle_notice = None
        if action["view"] == "toggle":
            # Legacy callbacks remain admin-gated.  New cards do not expose
            # these buttons; the supported surface is /chatfight on|off.
            if not await has_right(chat_id, user.id, "can_change_info"):
                return await _safe_answer(
                    query,
                    "You need the Change group info admin right to change "
                    "counting.",
                    alert=True,
                )
            await activity.set_enabled(chat_id, action["action"] == "enable")
            toggle_notice = "Counting paused." if action["action"] == "pause" else "Counting enabled."
            action = {"view": "main", "period": "day", "page": 0}
            card_state, card_generation = _claim_card_generation(message, action["period"])

        period = action["period"]
        if action["view"] == "member":
            data = await activity.member_breakdown(chat_id, action["user_id"], period)
            await _safe_answer(query, "Member details.")
            member_text = _member_text(data, action["media_kind"])
            payload = _signature("text", member_text)
            if not _card_shows(message, payload):
                if await _safe_edit(
                    message.edit_text,
                    member_text,
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=_member_keyboard(
                        chat_id, period, data["enabled"], action["media_kind"], action["user_id"]
                    ),
                ):
                    _card_shown(message, payload)
            return

        if action["view"] == "rank":
            data = await activity.snapshot(
                chat_id, period, user.id, media_kind=action["media_kind"]
            )
            mine = data["mine"]
            label = MEDIA_LABELS[action["media_kind"]] if action["media_kind"] else "messages"
            text = (
                f"{PERIODS[period]} {label}: rank #{mine['rank']} · {mine['count']} messages"
                if mine else f"{PERIODS[period]} {label}: no counted messages yet."
            )
            return await _safe_answer(query, text)

        if action["view"] == "media_menu":
            summary = await activity.media_summary(chat_id, period)
            await _safe_answer(query, "Choose a media type.")
            menu_text = _media_menu_text(summary)
            payload = _signature("text", menu_text)
            if not _card_shows(message, payload):
                if await _safe_edit(
                    message.edit_text,
                    menu_text,
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=_media_menu_keyboard(chat_id, period, summary["counts"]),
                ):
                    _card_shown(message, payload)
            return

        media_kind = action.get("media_kind")
        page = action.get("page", 0)
        data = await activity.snapshot(chat_id, period, user.id, media_kind=media_kind, page=page)
        if media_kind is None:
            # A period button on either a current photo or an old text card
            # always reopens the real image.
            await _update_main_card(
                query, message, data, card_state, card_generation, toggle_notice
            )
            return
        else:
            media_text = _media_text(data)
            payload = _signature("text", media_text)
            if not _card_shows(message, payload):
                if await _safe_edit(
                    message.edit_text,
                    media_text,
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=_media_keyboard(
                        chat_id, period, data["enabled"], media_kind, page,
                        data["pages"], data.get("entries"),
                    ),
                ):
                    _card_shown(message, payload)
        await _safe_answer(query, toggle_notice or "Updated.")
    except Exception:
        logger.exception("Chat Fight callback failed")
        if (
            card_state is not None
            and card_generation is not None
            and not _card_is_current(card_state, card_generation, action.get("period"))
        ):
            await _safe_answer(query, "Updated elsewhere.")
        else:
            await _safe_answer(query, "Could not load Chat Fight. Try again.", alert=True)


def _message_kind(message):
    """Return a concrete Telegram field, preserving unknown as unclassified."""

    for kind in MEDIA_KINDS:
        if kind == "text":
            continue
        if getattr(message, kind, None):
            return kind
    media = getattr(message, "media", None)
    if isinstance(media, str) and media in MEDIA_KINDS and media != "text":
        return media
    if getattr(message, "text", None) is not None or getattr(message, "caption", None) is not None:
        return "text"
    return None


@app.on_message(filters.group & ~filters.service, group=14)
async def count_activity(_, message):
    user = getattr(message, "from_user", None)
    if (
        not user
        or getattr(user, "is_bot", False)
        or getattr(message, "sender_chat", None)
        or getattr(message, "edit_date", None)
        or getattr(message, "service", None)
        or (
            getattr(message, "text", None) or getattr(message, "caption", None) or ""
        ).lstrip().startswith("/")
    ):
        return
    try:
        name = " ".join(filter(None, [
            getattr(user, "first_name", None),
            getattr(user, "last_name", None),
        ]))
        reached = await activity.record(
            message.chat.id,
            message.id,
            user.id,
            name or str(user.id),
            getattr(message, "date", None),
            media_kind=_message_kind(message),
            milestone=True,
        )
    except Exception:
        logger.exception("Chat Fight count could not be persisted")
        return
    if reached:
        sent_at = getattr(message, "date", None)
        now = utc()
        # Do not post historical milestones from delayed/history updates.
        if sent_at is None:
            return
        sent_at = utc(sent_at)
        if (sent_at.astimezone(IST).date() != now.astimezone(IST).date()
                or not -10 <= (now - sent_at).total_seconds() <= 120):
            return
        try:
            await _announce_milestone(
                message.chat.id, reached, sent_at.astimezone(IST).strftime("%H:%M")
            )
        except Exception:
            logger.exception("Chat Fight milestone announcement could not be sent")


@autodel_reply
async def _announce_milestone(chat_id, total, clock):
    return await app.send_message(
        chat_id, f"💪 {total} messages reached today! ({clock})",
        parse_mode=enums.ParseMode.DISABLED,
    )