"""Quotly-style quote stickers, rendered by the REAL reference renderer:
LyoSU/quote-api runs locally (bots/quote_api, started by run_all.sh on
127.0.0.1:4889) and this plugin just builds its /generate payload. Output is
pixel-identical to genuine QuotLy stickers — fonts, colors, tag placement,
premium emoji (Bot API thumbs), Unicode emoji (bundled brand images),
avatars, reply context, timestamps — all handled by the reference code.

/q [custom name] — quote the replied message (optional fake name)
/fq @username|user_id <text> — fully fake quote: their identity, YOUR text

If the local renderer is unreachable the user gets an explicit error — no
silent fallback to a lookalike renderer.
"""

import asyncio
import base64
import datetime
import html
import io
import json
import os
import re
import time
import urllib.request
import zlib
from collections import OrderedDict
from dataclasses import dataclass

from pyrogram import enums, filters

from ghelp import app, logger
from ghelp.config import config
from ghelp.plugins.restrict import fq_allowed

QUOTE_API = "http://127.0.0.1:4889/generate"

# abuse protection: at most 3 sticker jobs at once, 10s cooldown per user.
# _inflight is checked and incremented with NO await in between, so admission
# is atomic in the event loop — excess requests are rejected immediately,
# never queued.
_inflight = 0
_MAX_INFLIGHT = 3
_cooldown: dict[tuple[int, int], float] = {}
_COOLDOWN_SEC = 10

# Both quote paths use the bot's authenticated MTProto session to fetch the
# target profile image. Keep that image in memory only and keep the request
# small enough for quote-api's JSON body limit. If a photo is unavailable
# (private, deleted, or simply absent), the renderer's normal initials avatar
# is the correct fallback.
_AVATAR_DOWNLOAD_TIMEOUT = 12
_AVATAR_MAX_BYTES = 512 * 1024
_AVATAR_DOWNLOAD_RETRIES = 1
_AVATAR_CACHE_TTL = 300
_AVATAR_CACHE_MAX = 64


@dataclass(frozen=True)
class _AvatarFetch:
    """The authenticated avatar result without negative-cache ambiguity.

    ``no_photo`` means Telegram did not expose a profile-photo file id after a
    fresh user lookup. ``transient`` means it did expose one, but a bounded
    media transfer did not finish.  Neither outcome is cached: a later quote
    must be able to recover when a private-photo setting changes or Telegram's
    media DC has a short outage.
    """

    url: str | None
    no_photo: bool = False
    transient: bool = False


# Successful, authenticated profile images only. Values are data URLs, not
# paths; this stays in process RAM and expires promptly. The cache key includes
# the photo's stable/file id, so an update that carries a new photo never uses
# a previous image for the same account.
_avatar_cache: OrderedDict[tuple[int, int, str], tuple[float, str]] = OrderedDict()

# entity types the reference renderer understands
_ENTITY_TYPES = {
    "bold", "italic", "underline", "strikethrough", "code", "pre",
    "text_link", "custom_emoji",
}


def _entities_payload(m) -> list[dict]:
    """Telegram entities -> quote-api format (UTF-16 offsets passed through;
    the renderer expects them exactly as Telegram sends them)."""
    ents = m.entities or m.caption_entities or []
    out = []
    for e in ents:
        t = getattr(e.type, "value", str(e.type))
        if t not in _ENTITY_TYPES:
            continue
        d = {"type": t, "offset": e.offset, "length": e.length}
        if t == "custom_emoji" and getattr(e, "custom_emoji_id", None):
            d["custom_emoji_id"] = str(e.custom_emoji_id)
        elif t == "text_link" and getattr(e, "url", None):
            d["url"] = e.url
        out.append(d)
    return out


def _message_text(m) -> str:
    if m.text:
        return m.text
    if m.caption:
        return m.caption
    for attr, label in (("photo", "🖼 Photo"), ("video", "🎬 Video"),
                        ("sticker", "✨ Sticker"), ("animation", "🎞 GIF"),
                        ("voice", "🎤 Voice message"), ("audio", "🎵 Audio"),
                        ("document", "📎 Document"),
                        ("video_note", "📹 Video message"), ("poll", "📊 Poll")):
        if getattr(m, attr, None):
            return f"[{label}]"
    if m.service:
        return "[Service message]"
    return ""


def _raw_text(m) -> str:
    """Raw text/caption only — no synthesized media labels. Detects
    content-stripped refetches: a retained photo with a stripped caption must
    still trigger the fallback, which _message_text would mask behind a
    synthetic '[🖼 Photo]' label."""
    return (getattr(m, "text", None) or getattr(m, "caption", None) or "")


def _get_member_api(chat_id: int, user_id: int) -> tuple:
    """Blocking Bot API getChatMember. Always the latest schema (member 'tag'
    field included) and takes raw user ids — no peer cache, no TL layer
    lottery. Returns (title, is_admin)."""
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{os.getenv('BOT_TOKEN')}/getChatMember",
        data=json.dumps({"chat_id": chat_id, "user_id": user_id}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        resp = json.load(r)
    if not resp.get("ok"):
        raise RuntimeError(str(resp.get("description"))[:120])
    m = resp["result"]
    status = m.get("status")
    is_admin = status in ("administrator", "creator")
    # Telegram shows a default role tag when an admin has no custom title
    # ("admin" / "owner"); regular members can carry the newer 'tag' field.
    title = m.get("custom_title") or None
    if not title and is_admin:
        title = "owner" if status == "creator" else "admin"
    if not title:
        title = m.get("tag") or None
    return (title, is_admin)


async def _admin_title(chat_id: int, user_id: int) -> tuple:
    """Use the live bot session, not a possibly rotated Bot API credential."""
    if not chat_id or chat_id > 0 or user_id <= 0:
        return (None, None)
    try:
        member = await asyncio.wait_for(app.get_chat_member(chat_id, user_id), 5)
        status = getattr(member.status, "value", member.status)
        is_admin = status in ("administrator", "owner", "creator")
        title = getattr(member, "custom_title", None) or getattr(member, "tag", None)
        if not title and is_admin:
            title = "owner" if status in ("owner", "creator") else "admin"
        return (title, is_admin)
    except Exception as exc:
        logger.info("quotly: authenticated tag lookup failed: %s", type(exc).__name__)
        return (None, None)


async def _user_extras(user_id: int | None) -> tuple:
    """(emoji_status custom_emoji_id, accent_color_id) for a user. Bot API
    message updates carry neither, so re-fetch the full user. accent_color_id
    is what modern clients use for the exact name color (id%7 is only a
    legacy fallback), so the sticker name matches the group exactly."""
    if not user_id or user_id <= 0:
        return (None, None)
    status_id = accent_id = None
    try:
        full = await app.get_users(user_id)
        status_id = _emoji_status_id(getattr(full, "emoji_status", None))
        accent_id = _accent_color_id(full)
    except Exception:
        pass
    if accent_id is None or status_id is None:
        # Parsed User may not expose the color or the emoji status (kurigram
        # 2.2.25's parser drops fields it doesn't know — same reason
        # custom_title was empty) — go raw and fill whichever is missing.
        try:
            from pyrogram.raw.functions.users import GetFullUser
            peer = await app.resolve_peer(user_id)
            fu = await app.invoke(GetFullUser(id=peer))
            # users.getFullUser returns a users.Users result.  Its matching
            # User is in the vector; there is no reliable fu.user field.
            users = getattr(fu, "users", None) or []
            user = next(
                (candidate for candidate in users
                 if getattr(candidate, "id", None) == user_id),
                None,
            )
            if user is None:
                return (status_id, accent_id
                        if isinstance(accent_id, int) else None)
            if accent_id is None:
                accent_id = _accent_color_id(user)
            if status_id is None:
                status_id = _emoji_status_id(
                    getattr(user, "emoji_status", None)
                )
        except Exception:
            pass
    return (status_id, accent_id if isinstance(accent_id, int) else None)


def _emoji_status_id(status) -> str | None:
    """Extract a live custom-emoji document id from parsed or raw status.

    Raw MTProto statuses use ``document_id`` for both ordinary and
    collectible statuses.  ``collectible_id`` is the gift/collectible record,
    not the image document the quote renderer needs.  Expired statuses are
    intentionally treated as absent.
    """
    if status is None:
        return None
    until = getattr(status, "until", None)
    if until is None:
        until = getattr(status, "expires", None)
    if until is not None:
        try:
            expiry = until.timestamp() if hasattr(until, "timestamp") \
                else float(until)
            if expiry <= time.time():
                return None
        except (TypeError, ValueError, AttributeError):
            pass
    eid = getattr(status, "custom_emoji_id", None)
    if eid is None:
        eid = getattr(status, "document_id", None)
    if eid is None:
        return None
    try:
        eid = int(eid)
    except (TypeError, ValueError):
        return None
    return str(eid) if eid > 0 else None


def _accent_color_id(user) -> int | None:
    """Read accent color while preserving a valid color id of zero."""
    for source in (
        user,
        getattr(user, "color", None),
        getattr(user, "profile_color", None),
    ):
        if source is None:
            continue
        for attr in ("accent_color_id", "color"):
            value = getattr(source, attr, None)
            if isinstance(value, int):
                return value
    return None


def _avatar_data_url(data: bytes) -> str | None:
    """Return a renderer-safe data URL for an in-memory profile image."""
    if not data or len(data) > _AVATAR_MAX_BYTES:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith((b"RIFF",)) and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        # Telegram profile photos are normally JPEGs.  Canvas can still
        # validate the payload, and this keeps the data URL independent of
        # any filename returned by the download helper.
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _avatar_file_ids(user) -> list[tuple[str, str]]:
    """Get de-duplicated download ids with a stable cache identity."""
    photo = getattr(user, "photo", None)
    if photo is None:
        return []
    candidates = []
    for size in ("big", "small"):
        file_id = getattr(photo, f"{size}_file_id", None)
        if not file_id:
            continue
        # file_unique_id survives a file-reference refresh.  Fall back to the
        # current id if an older parsed User does not expose the unique id.
        identity = getattr(photo, f"{size}_file_unique_id", None) or file_id
        pair = (str(file_id), str(identity))
        if pair not in candidates:
            candidates.append(pair)
    return candidates


def _avatar_cache_key(user, photo_identity: str) -> tuple[int, int, str] | None:
    user_id = getattr(user, "id", None)
    if not isinstance(user_id, int) or user_id <= 0:
        return None
    return (id(app), user_id, photo_identity)


def _cached_avatar(user, photo_identity: str) -> str | None:
    now = time.monotonic()
    for key, (expiry, _url) in list(_avatar_cache.items()):
        if expiry <= now:
            _avatar_cache.pop(key, None)
    key = _avatar_cache_key(user, photo_identity)
    entry = _avatar_cache.get(key) if key else None
    if entry is None:
        return None
    _avatar_cache.move_to_end(key)
    return entry[1]


def _cache_avatar(user, photo_identity: str, url: str) -> None:
    key = _avatar_cache_key(user, photo_identity)
    if key is None:
        return
    now = time.monotonic()
    _avatar_cache[key] = (now + _AVATAR_CACHE_TTL, url)
    _avatar_cache.move_to_end(key)
    while len(_avatar_cache) > _AVATAR_CACHE_MAX:
        _avatar_cache.popitem(last=False)


async def _fetch_avatar(user) -> _AvatarFetch:
    """Fetch an avatar through the authenticated bot session.

    A missing file id after the direct and fresh-user views is a genuine
    no/private-photo result. A failed transfer after Telegram supplied a file
    id is deliberately tracked as transient instead. Both fall back to
    initials for this one render, but only validated successful images enter
    the RAM cache, allowing a later /q to recover rather than permanently
    preserving an outage as a fake "no photo" state.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _AVATAR_DOWNLOAD_TIMEOUT
    source = user
    file_ids = _avatar_file_ids(source)
    user_id = getattr(source, "id", 0)
    lookup_failed = False
    if not file_ids and isinstance(user_id, int) and user_id > 0:
        # Message updates often contain a min User without profile-photo
        # fields.  Refresh that user through the same authenticated session;
        # this is not a public lookup and is still covered by the deadline.
        remaining = deadline - loop.time()
        if remaining > 0:
            try:
                full = await asyncio.wait_for(
                    app.get_users(user_id), timeout=remaining,
                )
                if full is not None:
                    source = full
                    file_ids = _avatar_file_ids(source)
            except Exception as exc:
                lookup_failed = True
                logger.info("quotly avatar lookup unavailable (%s)",
                            type(exc).__name__)
    if not file_ids:
        # A min update contains no photo fields. If its authenticated refresh
        # failed, that is an outage, not evidence that the account has no
        # photo; leave it uncached so the next quote can recover.
        return _AvatarFetch(None, no_photo=not lookup_failed,
                            transient=lookup_failed)

    for file_id, photo_identity in file_ids:
        cached = _cached_avatar(source, photo_identity)
        if cached:
            return _AvatarFetch(cached)
        for attempt in range(_AVATAR_DOWNLOAD_RETRIES + 1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return _AvatarFetch(None, transient=True)
            try:
                downloaded = await asyncio.wait_for(
                    app.download_media(file_id, in_memory=True),
                    timeout=remaining,
                )
                if downloaded is None:
                    break
                if isinstance(downloaded, (bytes, bytearray)):
                    raw = bytes(downloaded)
                else:
                    # Pyrogram returns BytesIO for in_memory=True.  Do not
                    # trust seek/tell metadata from alternate clients.
                    raw = downloaded.getvalue() if hasattr(downloaded, "getvalue") \
                        else downloaded.read()
                result = _avatar_data_url(bytes(raw)) \
                    if isinstance(raw, (bytes, bytearray)) else None
                if result:
                    _cache_avatar(source, photo_identity, result)
                    return _AvatarFetch(result)
                # A returned non-image cannot become valid by retrying the
                # same file reference; continue to the smaller photo.
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt < _AVATAR_DOWNLOAD_RETRIES:
                    logger.info("quotly avatar retry (%s)", type(exc).__name__)
                else:
                    logger.info("quotly avatar transfer unavailable (%s)",
                                type(exc).__name__)
    return _AvatarFetch(None, transient=True)


async def _download_avatar_data_url(user) -> str | None:
    """Compatibility wrapper for callers/tests that only need the data URL."""
    return (await _fetch_avatar(user)).url


async def _none():
    return None


def _other_groups(exclude: int | None) -> list[int]:
    """Supergroups this bot is in, from kurigram's session peer table."""
    try:
        rows = app.storage.conn.execute(
            "SELECT id FROM peers WHERE type IN ('supergroup', 'forum') LIMIT 25").fetchall()
    except Exception as exc:  # storage layout is kurigram-internal; degrade quietly
        logger.info("quotly: peer table not readable (%s)", type(exc).__name__)
        return []
    return [cid for (cid,) in rows if cid != exclude]


async def _find_member(uid: int, chat_id: int | None, everywhere: bool = False):
    """channels.getParticipant accepts access_hash=0 from a bot for ANY member of
    a supergroup the bot is in — including members who never spoke, whom
    users.getUsers with hash 0 does not return. Ordinary callers only get
    the chat the command was used in: /fq is open to every non-restricted
    user, so searching the bot's other groups would let anyone in PM pull
    identities out of communities they are not part of. `everywhere` (owner
    / fq allow-list) also tries the other groups. Raw User or None."""
    from pyrogram.errors import FloodWait
    from pyrogram.raw.functions.channels import GetParticipant
    from pyrogram.raw.types import InputChannel, InputPeerChannel, InputPeerUser
    from pyrogram.raw.types import User as RawUser

    chats = [chat_id] if chat_id is not None and chat_id < 0 else []
    if everywhere:
        chats += _other_groups(chat_id)
    for cid in chats:
        try:
            peer = await app.resolve_peer(cid)
        except Exception:
            continue
        if not isinstance(peer, InputPeerChannel):
            continue
        try:
            res = await app.invoke(GetParticipant(
                channel=InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash),
                participant=InputPeerUser(user_id=uid, access_hash=0)))
        except FloodWait:
            break  # never hammer Telegram over a sticker
        except Exception as exc:  # PARTICIPANT_ID_INVALID / USER_NOT_PARTICIPANT / CHAT_ADMIN_REQUIRED
            logger.info("quotly: getParticipant(%s, %s) -> %s", cid, uid, type(exc).__name__)
            continue
        for u in res.users:
            if isinstance(u, RawUser) and u.id == uid:
                return u
    return None


async def _channel_username_title(ident):
    """If @username belongs to a channel/group, return its title, else None.

    Bots cannot resolve usernames through get_users, but
    contacts.ResolveUsername is permitted and its peer type tells us whether
    the target is a person or a channel (2026-09-27).
    """
    username = str(ident or "").lstrip("@")
    if not username or username.isdigit():
        return None
    try:
        from pyrogram.raw.functions.contacts import ResolveUsername
        from pyrogram.raw.types import PeerChannel, PeerChat

        res = await app.invoke(ResolveUsername(username=username))
        if isinstance(res.peer, (PeerChannel, PeerChat)):
            for ch in (res.chats or []):
                title = getattr(ch, "title", None)
                if title:
                    return title
            return "a channel or group"
    except Exception as exc:
        logger.info("quotly: channel probe for %s failed: %s",
                    username, type(exc).__name__)
    return None


async def _resolve_user(ident: str, chat_id: int | None = None, everywhere: bool = False):
    """@username or numeric id -> User. kurigram's get_users only accepts ids
    already in its local peer cache (PEER_ID_INVALID is raised before any
    network call), so numeric ids climb a ladder of raw calls with
    access_hash=0 — the same thing TDLib / the Bot API server does for ids
    it does not know: users.getUsers (people the bot has crossed paths with),
    then channels.getParticipant in the current supergroup — every other
    group too for `everywhere` callers — (any member, even silent ones).
    The peer is cached afterwards so the
    status/color lookups that follow resolve too. Returns None only when
    Telegram itself will not hand the user to this bot."""
    from pyrogram.raw.functions.users import GetUsers
    from pyrogram.raw.types import InputUser, User as RawUser
    from pyrogram.types import User

    if not ident.isdigit():
        # The high-level helper is unusable for bots: get_users(<username>)
        # yields None and pyrogram then raises AttributeError internally — it
        # fails even for @telegram.  contacts.ResolveUsername IS allowed for
        # bots, so it does the resolution (2026-09-27).
        try:
            return await app.get_users(ident)
        except Exception:
            pass
        username = ident.lstrip("@")
        try:
            from pyrogram.raw.functions.contacts import ResolveUsername
            from pyrogram.raw.types import User as RawUser
            from pyrogram.types import User

            res = await app.invoke(ResolveUsername(username=username))
            raw = next(
                (u for u in (res.users or []) if isinstance(u, RawUser)), None
            )
            if raw is not None and getattr(raw, "id", None):
                await app.fetch_peers([raw])
                return await User._parse(app, raw)
            logger.info("quotly: ResolveUsername(%s) returned no user", username)
        except Exception as exc:
            logger.info("quotly: ResolveUsername(%s) failed: %s",
                        username, type(exc).__name__)
        return None
    uid = int(ident)
    try:
        return await app.get_users(uid)
    except Exception as exc:
        logger.info("quotly: cached lookup of %s failed (%s) — raw hash-0 fallback",
                    uid, type(exc).__name__)
    found = await app.invoke(GetUsers(id=[InputUser(user_id=uid, access_hash=0)]))
    raw = next((u for u in found if isinstance(u, RawUser) and u.id == uid), None)
    if raw is None:
        raw = await _find_member(uid, chat_id, everywhere)
    if raw is None:
        return None
    await app.fetch_peers([raw])  # skips min users; then get_users works for the rest
    return await User._parse(app, raw)


def _quote_tz():
    """Timezone the typed clock times are read in (owner 2026-10-11)."""
    name = os.getenv("QUOTE_TZ", "Asia/Kolkata")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return datetime.timezone.utc


_TIME_RE = re.compile(
    r"^(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*([apAP]\.?[mM]\.?)?$"
)


def _parse_quote_time(token: str) -> int | None:
    """A clock time the user typed -> today's epoch seconds, else None.

    Accepts 10:30, 22:45, 7:05:30, 10:30pm, 10 pm.  A bare number is NOT a
    time (it could be a user id), so a colon or an am/pm marker is required.
    """
    m = _TIME_RE.match((token or "").strip())
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ss = int(m.group(3) or 0)
    ampm = (m.group(4) or "").lower().replace(".", "")
    if m.group(2) is None and not ampm:
        return None
    if ampm:
        if not 1 <= hh <= 12:
            return None
        if ampm == "pm" and hh != 12:
            hh += 12
        elif ampm == "am" and hh == 12:
            hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        return None
    now = datetime.datetime.now(_quote_tz())
    return int(now.replace(hour=hh, minute=mm, second=ss, microsecond=0).timestamp())


def _time_from_command(m) -> int | None:
    """The clock time typed anywhere in the command, e.g. /q 10:30."""
    raw = (getattr(m, "text", None) or getattr(m, "caption", None) or "").strip()
    if not raw:
        return None
    for token in raw.split()[1:]:              # skip the /q|/fq word itself
        ts = _parse_quote_time(token)
        if ts is not None:
            return ts
    return None


def _verification_badge(user) -> str | None:
    """SCAM / FAKE badge, as Telegram paints it next to the name."""
    if user is None:
        return None
    vs = getattr(user, "verification_status", None)
    scam = getattr(vs, "is_scam", None) if vs is not None else None
    fake = getattr(vs, "is_fake", None) if vs is not None else None
    if scam is None:
        scam = getattr(user, "is_scam", None)
    if fake is None:
        fake = getattr(user, "is_fake", None)
    if fake:
        return "FAKE"
    if scam:
        return "SCAM"
    return None


def _sticker_height(text: str, extra_rows: int = 0) -> int:
    """Height sized to the message so the sticker has no dead space and the
    text still wraps comfortably (owner 2026-10-11)."""
    chars_per_line = 34
    lines = max(1, -(-len(text or "") // chars_per_line))
    return max(260, min(int(190 + lines * 34 + extra_rows * 34), 1280))


def _build_payload(name: str, uid: int, username: str | None, text: str,
                   entities: list[dict], tag: str | None, reply,
                   status_id: str | None, accent_id: int | None,
                   avatar: bool,
                   date: int | None, reply_count: int | None,
                   reactions: list[dict] | None, tag_admin: bool = False,
                   avatar_url: str | None = None,
                   avatar_authenticated: bool = False,
                   badge: str | None = None,
                   extra_rows: int = 0) -> dict:
    msg = {
        "avatar": avatar,
        "from": {"id": uid, "first_name": name},
        "text": text,
        "entities": entities,
    }
    if username:
        msg["from"]["username"] = username
    if avatar_url:
        # quote-api accepts data URLs directly.  This is only populated by
        # /fq; leaving it absent preserves the established /q path exactly.
        msg["from"]["photo"] = {"url": avatar_url}
    elif avatar_authenticated:
        # Tell quote-api that this is the authenticated /fq path even when
        # Telegram did not make a profile photo available.  Without this
        # marker the renderer could try its legacy public username mirror.
        msg["from"]["photo"] = {"no_lookup": True}
    if badge:
        # SCAM / FAKE outranks an admin title or member tag, exactly as the
        # real client shows it (owner 2026-10-11).
        msg["senderTag"] = badge
        msg["senderTagBadge"] = True
    elif tag:
        msg["senderTag"] = tag
        if tag_admin:
            msg["senderTagAdmin"] = True
    if status_id:
        msg["from"]["emoji_status"] = status_id
    if accent_id is not None:
        msg["from"]["accent_color_id"] = accent_id
    if date:
        msg["date"] = date
    if reply_count:
        msg["replyCount"] = reply_count
    if reactions:
        msg["reactions"] = reactions
    if reply:
        rname, rtext, ruid, raccent, rstatus = reply
        msg["replyMessage"] = {"name": rname, "text": rtext,
                               "chatId": ruid}
        if raccent is not None:
            msg["replyMessage"]["accent_color_id"] = raccent
        if rstatus:
            msg["replyMessage"]["emoji_status"] = rstatus
    return {
        "type": "quote",
        "format": "webp",
        "backgroundColor": "#182533",  # Telegram dark bubble color
        "width": 512,
        "height": _sticker_height(text, extra_rows),
        "scale": 2,
        "emojiBrand": "apple",
        "messages": [msg],
    }


def _trim_transparent(data: bytes, pad: int = 2) -> bytes:
    """Crop the fully-transparent margin so the sticker hugs the bubble.

    The renderer always rasterises a 512-wide canvas, so a one-line quote came
    out as a wide sticker with empty space on both sides.  A real Telegram
    quote sticker is exactly as wide as the bubble it draws.  Cropping to the
    alpha bounds gives that breadth (and tightens the length too), with a 2px
    pad so the rounded edge is never clipped.  Any failure returns the
    original bytes -- trimming must never lose a sticker.
    """
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if im.mode != "RGBA":
            im = im.convert("RGBA")
        bbox = im.getchannel("A").getbbox()
        if not bbox:
            return data
        l, t, r, b = bbox
        box = (max(0, l - pad), max(0, t - pad),
               min(im.width, r + pad), min(im.height, b + pad))
        if (box[2] - box[0], box[3] - box[1]) == im.size:
            return data
        out = io.BytesIO()
        im.crop(box).save(out, format="WEBP", quality=95, method=6)
        return out.getvalue()
    except Exception:
        return data


def _post_generate(payload: dict) -> bytes:
    """Synchronous POST to the local quote-api (run in a thread)."""
    req = urllib.request.Request(
        QUOTE_API, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.load(r)
    if not resp.get("ok"):
        raise RuntimeError(f"quote-api error: {str(resp.get('error'))[:200]}")
    return _trim_transparent(base64.b64decode(resp["result"]["image"]))


def _throttled(m) -> bool:
    """Per-(chat,user) cooldown — silent True when throttled."""
    key = (m.chat.id, m.from_user.id if m.from_user else 0)
    if time.monotonic() - _cooldown.get(key, 0.0) < _COOLDOWN_SEC:
        return True
    _cooldown[key] = time.monotonic()
    return False


def _admit(m) -> bool:
    """Atomic in-flight admission (no await between check and increment)."""
    global _inflight
    if _inflight >= _MAX_INFLIGHT:
        return False
    _inflight += 1
    return True


async def _serve_quote(m, src, name: str, tag: str | None, uid: int,
                       author, avatar: bool = True,
                       admitted: bool = False,
                       text_override: str | None = None,
                       date_override: int | None = None):
    """Shared pipeline for /q and /fq: spam guards -> reference render ->
    sticker. author: the User/Chat the sticker impersonates (status source).
    admitted=True means the caller already passed cooldown + admission (and
    we own releasing the slot). text_override: render a fully synthetic
    message (src may be None) — custom text, no entities/reply/reactions."""
    global _inflight
    # A clock time typed in the command becomes the sticker's timestamp; with
    # none typed, the behaviour is unchanged (the message's own date, or now
    # for a synthetic /fq) (owner 2026-10-11).
    if date_override is None:
        date_override = _time_from_command(m)
    _badge = _verification_badge(author)
    if not admitted:
        if _throttled(m):
            return
        if not _admit(m):
            return await m.reply("Sticker queue is busy. Try again in a few seconds.")
    try:
        status = await m.reply("Making sticker…")
        try:
            if text_override is not None:
                # fully synthetic message: custom text, no source message,
                # no entities/reply/reactions. Identity (name/tag/accent/
                # avatar) still comes from the real target account. The fake
                # message gets "now" as its timestamp — real Telegram bubbles
                # always show the time, so a missing one looks wrong.
                title_res, (status_id, accent_id), avatar_result = await asyncio.gather(
                    _admin_title(m.chat.id, uid)
                    if avatar and uid > 0 else _none(),
                    _user_extras(uid if uid > 0 else None),
                    _fetch_avatar(author) if avatar else _none(),
                )
                avatar_url = avatar_result.url if avatar_result else None
                title, title_is_admin = title_res or (None, None)
                tag = title or tag  # admin custom title outranks @username
                tag_admin = bool(title and title_is_admin and tag == title)
                payload = _build_payload(
                    name, uid, getattr(author, "username", None),
                    text_override, [], tag, None,
                    status_id, accent_id, avatar,
                    date_override or int(time.time()), None,
                    None, tag_admin, avatar_url, True, badge=_badge)
                logger.info(
                    "quotly resolve: entities=%d status=%s tag=%r name=%r",
                    len(payload["messages"][0]["entities"]),
                    bool(status_id), tag, name)
                from ghelp.quote_emoji import hydrate_emoji
                await hydrate_emoji(payload, app)
                if status_id and str(status_id) not in payload["messages"][0]["customEmojiAssets"]:
                    await status.edit_text("Couldn't load this account's emoji. Please try again.")
                    return
                webp = await asyncio.to_thread(_post_generate, payload)
                buf = io.BytesIO(webp)
                buf.name = "quote.webp"
                await m.reply_sticker(buf)
                await status.delete()
                return
            # Bot API embeds the replied-to message but strips ITS reply
            # info (no nesting) — refetch it fresh so reply context shows.
            # BUT a bot session's refetch of ANOTHER bot's message comes back
            # content-stripped (bots can't read other bots' messages) — keep
            # the update's own text/entities/date as fallback, else quoting a
            # bot renders its name over an empty bubble (2026-09-06).
            orig_text = _message_text(src)
            orig_entities = _entities_payload(src)
            orig_date = date_override or getattr(src, "date", None)
            orig_rt_id = getattr(src, "reply_to_message_id", None)
            try:
                fresh = await app.get_messages(m.chat.id, src.id)
                if fresh and getattr(fresh, "id", None):
                    src = fresh
            except Exception as exc:
                logger.info("quotly refetch failed: %s", exc)
            body_text = _message_text(src)
            body_entities = _entities_payload(src)
            # Judge stripping by RAW text/caption: a stripped photo+caption
            # shell still synthesizes a '[🖼 Photo]' label via _message_text,
            # which would mask the lost caption behind the label.
            if not _raw_text(src).strip() and orig_text.strip():
                body_text, body_entities = orig_text, orig_entities
            # replied-to context for the sticker's quote block
            reply = None
            rt = getattr(src, "reply_to_message", None)
            # A bot-authored src refetches as a stripped shell that can also
            # lose its parsed reply object — but the reply_to MESSAGE ID
            # survives in the header (and on the update's embedded copy).
            # Fetch that target directly: a replied-to USER message comes back
            # with full content, so the sticker's reply bar shows (2026-09-06).
            rt_id = getattr(src, "reply_to_message_id", None) or orig_rt_id
            # Fetch only when the rt we have can't render a reply bar at all
            # (None, or a hollow shell with no sender/label) — media-only and
            # service replies already render via _message_text's labels, and
            # re-fetching those wastes a get_messages call on the hot /q path
            # (code-review 2026-09-06).
            ru0 = rt and (getattr(rt, "from_user", None)
                          or getattr(rt, "sender_chat", None))
            if rt_id and not (ru0 and _message_text(rt).strip()):
                try:
                    frt = await app.get_messages(m.chat.id, rt_id)
                    if frt and getattr(frt, "id", None) \
                            and (rt is None or _raw_text(frt).strip()):
                        rt = frt
                except Exception as exc:
                    logger.info("quotly reply-target fetch failed: %s", exc)
            ru = rt and (getattr(rt, "from_user", None)
                         or getattr(rt, "sender_chat", None))
            if ru:
                rname = getattr(ru, "first_name", None) \
                    or getattr(ru, "title", None) or "?"
                rtext = _message_text(rt).strip()
                if not rtext:
                    # Media-only replies have no caption — Telegram's reply
                    # preview still shows a label ("Photo", "Sticker", …).
                    # Without this the renderer drops the reply block whole.
                    for attr, label in (
                        ("photo", "Photo"), ("video", "Video"),
                        ("animation", "GIF"), ("sticker", "Sticker"),
                        ("video_note", "Video message"),
                        ("voice", "Voice message"), ("audio", "Audio"),
                        ("document", "Document"),
                    ):
                        if getattr(rt, attr, None):
                            rtext = label
                            break
                if rtext:
                    reply = (rname, rtext, ru.id)
            title_res, (status_id, accent_id), (reply_status, reply_accent), avatar_result = \
                await asyncio.gather(
                    # admin custom title outranks the @username tag — only for
                    # real members (skip for fake-name renders)
                    _admin_title(m.chat.id, uid) if avatar else _none(),
                    _user_extras(author.id if getattr(author, "id", None)
                                 else None),
                    _user_extras(ru.id if ru else 0),
                    # A normal /q must use this same authenticated path as
                    # /fq. quote-api's Bot API credential is not authoritative
                    # for Maki and its legacy username mirror can neither see
                    # private photos nor safely decide that there is none.
                    _fetch_avatar(author) if avatar else _none(),
                )
            title, title_is_admin = title_res or (None, None)
            if reply:
                reply = (*reply, reply_accent, reply_status)
            # tag precedence (reference: sender_tag ?? author_signature)
            tag = getattr(src, "sender_tag", None) \
                or getattr(src, "author_signature", None) or title or tag
            # green chip only when the shown tag IS an admin's custom title
            tag_admin = bool(title and title_is_admin and tag == title)
            # footer meta: message date + reply count (best-effort — Bot API
            # rarely exposes the count)
            src_date = (date_override or getattr(src, "date", None)
                        or orig_date)
            date = int(src_date.timestamp()) if src_date else None
            replies = getattr(src, "replies", None)
            reply_count = getattr(replies, "replies", None) \
                or getattr(src, "reply_count", None) or None
            # reactions (kurigram: Message.reactions -> MessageReactions or
            # list; each entry has .emoji or .type.emoji and .count)
            reacts = []
            mr = getattr(src, "reactions", None)
            rlist = getattr(mr, "reactions", None) \
                or (mr if isinstance(mr, list) else [])
            for r in rlist or []:
                emoji = getattr(r, "emoji", None)
                rtype = getattr(r, "type", None)
                if not emoji and rtype is not None:
                    emoji = getattr(rtype, "emoji", None)
                if emoji:
                    reacts.append({"emoji": emoji,
                                   "count": getattr(r, "count", 1) or 1})
            payload = _build_payload(
                name, uid, getattr(author, "username", None),
                body_text, body_entities, tag, reply,
                status_id, accent_id, avatar, date, reply_count,
                reacts or None, tag_admin,
                avatar_result.url if avatar_result else None,
                avatar_authenticated=avatar, badge=_badge)
            logger.info(
                "quotly resolve: entities=%d status=%s tag=%r name=%r",
                len(payload["messages"][0]["entities"]),
                bool(status_id), tag, name)
            from ghelp.quote_emoji import hydrate_emoji
            await hydrate_emoji(payload, app)
            webp = await asyncio.to_thread(_post_generate, payload)
            buf = io.BytesIO(webp)
            buf.name = "quote.webp"
            await m.reply_sticker(buf)
            await status.delete()
        except Exception as exc:
            logger.warning("quotly failed: %s", exc)
            await status.edit_text("Couldn't make the sticker. Try again.")
    finally:
        _inflight -= 1


def _quote_usage() -> str:
    return ("Reply to a message with <code>/q</code> — or, in a private chat, "
            "just send <code>/q</code> to quote your own last message.\n"
            "Add a time to stamp the sticker, e.g. <code>/q 10:30pm</code>.")


# Bots cannot call get_chat_history (BotMethodInvalid), so a private /q can
# only quote a message the bot actually received.  Remember the last
# non-command message of each DM user — in memory only (2026-09-19).
_private_last: OrderedDict[int, object] = OrderedDict()
_PRIVATE_LAST_MAX = 200


@app.on_message(filters.private & ~filters.service, group=-1)
async def _remember_private_message(_, m) -> None:
    """Keep the last thing a DM user wrote so /q has something to quote.

    Group -1 runs before the /q command handler, and command messages are
    skipped so the quote target is always real content.
    """
    if not m.from_user:
        return
    text = m.text or m.caption or ""
    if text.startswith(("/", "!", "?")):
        return
    _private_last[m.from_user.id] = m
    _private_last.move_to_end(m.from_user.id)
    while len(_private_last) > _PRIVATE_LAST_MAX:
        _private_last.popitem(last=False)


def _private_last_message(m):
    """A private chat has no reply to quote and no readable history, so /q
    falls back to the user's own last message seen in this DM."""
    if not m.from_user:
        return None
    return _private_last.get(m.from_user.id)


def _quote_target(m, src=None):
    """Validate the quoted message; returns (src, error)."""
    if src is None:
        src = getattr(m, "reply_to_message", None)
    if src is None:
        return None, _quote_usage()
    u = getattr(src, "from_user", None) or getattr(src, "sender_chat", None)
    if not u:
        return None, "Reply to a user's message to quote it."
    if not _message_text(src).strip():
        return None, "That message has nothing to quote."
    return src, None


def _name_tag(u) -> tuple[str, str | None]:
    name = getattr(u, "first_name", None) or getattr(u, "title", None) or "?"
    # No @username fallback: real Telegram shows NO tag unless the group
    # assigned one (admin custom_title / member tag) — an invented @username
    # tag on every sticker was pure noise.
    return name, None


@app.on_message(filters.command(["q", "quote"], prefixes=["/", "?", "!", "."])
                & (filters.reply | filters.private))
async def quote_cmd(_, m):
    """/q — quote the replied-to message.  In a private chat, /q without a
    reply quotes the user's own last message; anywhere else it answers with
    the usage instead of staying silent (2026-09-19)."""
    src = getattr(m, "reply_to_message", None)
    if src is None and m.chat.type == enums.ChatType.PRIVATE:
        src = _private_last_message(m)
    src, err = _quote_target(m, src)
    if err:
        return await m.reply(err, parse_mode=enums.ParseMode.HTML)
    u = getattr(src, "from_user", None) or getattr(src, "sender_chat", None)
    name, tag = _name_tag(u)
    uid, avatar = u.id, True
    # /q @username keeps the replied message text but renders it as that real
    # account (name, avatar, status and group title). Any other argument keeps
    # the established custom-name mode with a generated initial avatar.
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        requested = parts[1].strip()
        if (requested.startswith("@") and " " not in requested) or requested.isdigit():
            try:
                target = await _resolve_user(
                    requested[1:] if requested.startswith("@") else requested,
                    m.chat.id,
                )
            except Exception:
                target = None
            if (
                target is None
                or getattr(target, "is_bot", False)
                or getattr(target, "is_deleted", False)
            ):
                return await m.reply("I couldn't find that personal account.")
            u = target
            name, tag = _name_tag(u)
            uid, avatar = u.id, True
        else:
            name, tag = requested[:40], None
            uid, avatar = zlib.crc32(name.encode()), False
    await _serve_quote(m, src, name, tag, uid, u, avatar=avatar)



def _utf16_span(text, offset, length=0):
    """Slice `text` with Telegram entity offsets (UTF-16 code units, not
    Python indices). length=0 returns everything from `offset` onward."""
    u16 = text.encode("utf-16-le")
    end = (offset + length) * 2 if length else len(u16)
    return u16[offset * 2:end].decode("utf-16-le", "surrogatepass")


@app.on_message(filters.command(["fq", "tq"], prefixes=["/", "?", "!", "."]))
async def fake_text_cmd(_, m):
    """/fq (alias /tq) @username|user_id <text> — a quote sticker written BY
    that account but with YOUR text. Tagging via the mention picker works too
    (text_mention entity — no public username needed). On a reply,
    '/fq <text>' fakes the replied-to sender as the author (a leading
    @username/user_id/mention overrides)."""
    global _inflight
    args = (m.text or "").split(maxsplit=1)
    cmd = args[0] if args else "/fq"
    usage = (f"Usage: <code>{cmd} @username your text</code> — or tag "
             f"someone (works even without a username), or reply to their "
             f"message with <code>{cmd} your text</code>")
    rest = args[1].strip() if len(args) > 1 else ""
    head, _, tail = rest.partition(" ")
    head_is_ident = head.startswith("@") or head.isdigit()
    rt = getattr(m, "reply_to_message", None)
    target = text = ident = None
    if not head_is_ident and rest:
        # A mention-picker tag arrives as an entity over the display name —
        # invisible in the raw text. Accept one in head position (only
        # whitespace may sit between the command and the tag).
        raw, cmd_len = m.text or "", len(args[0]) if args else 0
        for ent in (m.entities or []):
            if ent.type not in (enums.MessageEntityType.TEXT_MENTION,
                                enums.MessageEntityType.MENTION):
                continue
            if ent.offset < cmd_len \
                    or _utf16_span(raw, cmd_len, ent.offset - cmd_len).strip():
                continue  # not the head token
            if ent.type is enums.MessageEntityType.TEXT_MENTION:
                target = getattr(ent, "user", None)
            else:
                ident = _utf16_span(raw, ent.offset, ent.length).lstrip("@")
            text = _utf16_span(raw, ent.offset + ent.length).strip()
            break
    if head_is_ident and tail.strip():
        ident, text = head.lstrip("@"), tail.strip()
    elif target is not None or ident is not None:
        pass  # identity already resolved from a mention entity above
    elif rt is not None and not head_is_ident:
        target = getattr(rt, "from_user", None) \
            or getattr(rt, "sender_chat", None)
        if target is None or not getattr(target, "id", None):
            return await m.reply(usage, parse_mode=enums.ParseMode.HTML)
        text = rest
    if not text:
        return await m.reply(usage, parse_mode=enums.ParseMode.HTML)
    # throttle + reserve a slot BEFORE the external identity lookup (as /fq)
    if _throttled(m):
        return
    if not _admit(m):
        return await m.reply("Sticker queue is busy. Try again in a few seconds.")
    if target is None:
        everywhere = False
        try:
            # owner / fq allow-list may pull members out of the bot's other
            # groups.  A private chat has no member list of its own, so a DM
            # lookup of a numeric id climbs the same ladder — deliberately
            # only for an exact id the user already knows, never for a bare
            # username in someone else's community (2026-09-19).
            everywhere = bool(
                ident
                and str(ident).isdigit()
                and m.chat.type == enums.ChatType.PRIVATE
            ) or await fq_allowed(m.from_user)
            target = await _resolve_user(ident, m.chat.id, everywhere)
        except Exception as exc:
            logger.info("quotly: lookup of %r failed: %s", ident, exc)
            target = None
        if target is None:
            _inflight -= 1  # release the reservation, nothing else to clean up
            shown_ident = html.escape(str(ident or "that user"))
            what = await _channel_username_title(ident)
            if what:
                return await m.reply(
                    f"<code>{shown_ident}</code> is not a person — it is "
                    f"<b>{html.escape(str(what))}</b>, a channel or group.\n"
                    f"/fq can only make fake quotes of <b>users</b>."
                )
            return await m.reply(
                f"No accessible user found for <code>{shown_ident}</code>.")
    if target.id == config.OWNER_ID:
        try:
            allowed = await fq_allowed(m.from_user)
        except Exception:
            _inflight -= 1  # release the reservation whatever happens
            raise
        if not allowed:
            _inflight -= 1  # release the reservation — nothing was rendered
            return await m.reply("Fake quotes of the bot owner are off.")
    name, tag = _name_tag(target)
    await _serve_quote(m, None, name, tag, target.id, target,
                       admitted=True, text_override=text)
