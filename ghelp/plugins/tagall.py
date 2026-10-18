"""Admin-only mention-everyone menu with reliable, repeatable message chunks.

Members are mentioned by EMOJI, never by a visible name link: each mention is
``<a href="tg://user?id=ID">EMOJI</a>``, so the member is notified while the
batch reads as a row of emojis instead of a wall of names.  There is no
name-link style for this command.  The admin's text is repeated at the top of
every batch.
"""

import asyncio
import html
import logging
import secrets
import time

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.helpers import has_right, require_admin

PRE = ["/", "?", "!", "."]
_last_run: dict[int, float] = {}
_pending: dict[str, dict] = {}
_running: dict[str, asyncio.Event] = {}
_active_chats: dict[int, str] = {}
_chat_locks: dict[int, asyncio.Lock] = {}
_COOLDOWN = 600
_MENU_TTL = 180
_MAX_MEMBERS = 150
_CHUNK = 7
_MAX_NOTE = 700
logger = logging.getLogger(__name__)

# Emoji-tag labels.  Assignment is derived from the user id, so a member keeps
# the same emoji run after run instead of shuffling on every tag-all.
_EMOJI_POOL = (
    "\U0001F468\U0001F3FD\u200D\U0001F33E", "\U0001F9D1\U0001F3FE\u200D\U0001F3ED",
    "\U0001F37C", "\U0001F471", "\U0001F62D", "\U0001F44F\U0001F3FF",
    "\U0001F99F", "\U0001F9D1\U0001F3FE\u200D\U0001F692", "\U0001F9D1\u200D\U0001F680",
    "\U0001F36B", "\U0001F691", "\u26F5", "\U0001F4CF", "\U0001F9D1\U0001F3FB\u200D\U0001F4BC",
    "\U0001F9D1\U0001F3FF\u200D\U0001F3EB", "\U0001F913", "\U0001F46F", "\U0001F9D1\U0001F3FC\u200D\U0001F3A4",
    "\U0001F340", "\u26D1", "\U0001F435", "\U0001F965", "\u270B", "\U0001F9D1\u200D\u2708\uFE0F",
    "\U0001F575", "\U0001F9D1\U0001F3FB\u200D\U0001F3A8", "\U0001F471\u200D\u2640\uFE0F",
    "\U0001F9D1\U0001F3FF\u200D\U0001F3A4", "\U0001F385", "\U0001F3CC", "\U0001F607",
    "\U0001F570", "\U0001FAB0", "\U0001F9D1\U0001F3FF\u200D\U0001F4BC", "\u2709",
    "\U0001F9D1\U0001F3FF\u200D\u2708\uFE0F", "\U0001F6F3", "\U0001F934", "\U0001F449",
    "\U0001F9BD", "\U0001F9D3", "\U0001F91A\U0001F3FF", "\U0001FACF", "\U0001F3C3",
    "\U0001F527", "\U0001F44C", "\U0001F642", "\U0001F44E", "\U0001F393",
    "\U0001F4D2", "\U0001F9D1\u200D\U0001F393", "\U0001F36D", "\U0001FAB8", "\U0001F442",
)

def _emoji_for(user_id: int) -> str:
    """Stable emoji label for a member (same id -> same emoji)."""
    return _EMOJI_POOL[(int(user_id) * 2654435761) % len(_EMOJI_POOL)]


def _expire_pending(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for token, job in list(_pending.items()):
        if token not in _running and now - job["created"] > _MENU_TTL:
            _pending.pop(token, None)


async def _claim_start(job: dict, token: str,
                       actor_id: int | None = None) -> tuple[bool, str]:
    """Atomically authorize and reserve one Tag All run for this chat.

    The right is checked on whoever is pressing Start, not on whoever opened
    the menu: any admin holding Pin messages may drive this feature.
    """
    chat_id = job["chat_id"]
    user_id = job["user_id"] if actor_id is None else actor_id
    lock = _chat_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        now = time.time()
        if now - job["created"] > _MENU_TTL:
            _pending.pop(token, None)
            return False, "This Tag All button expired."
        if _pending.get(token) is not job:
            # Another admin cancelled this menu while the right was being
            # checked; a cancelled run must never still start.
            return False, "This Tag All button expired."
        if not await has_right(chat_id, user_id, "can_pin_messages"):
            # losing Pin messages between opening the menu and pressing
            # Start must not still fire a mass mention
            _pending.pop(token, None)
            return False, "You no longer have the Pin messages admin right."
        if chat_id in _active_chats:
            return False, "Tag All is already running in this group."
        wait = int(_COOLDOWN - (now - _last_run.get(chat_id, 0)))
        if wait > 0:
            return False, f"Cooldown active for {wait}s."
        # Reserve before releasing the lock so two menus cannot start together.
        _active_chats[chat_id] = token
        _last_run[chat_id] = now
        job["claimed_at"] = now
        return True, ""


async def _release_claim(job: dict, token: str, *, rollback: bool = False) -> None:
    chat_id = job["chat_id"]
    lock = _chat_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        if _active_chats.get(chat_id) == token:
            _active_chats.pop(chat_id, None)
        if rollback and _last_run.get(chat_id) == job.get("claimed_at"):
            _last_run.pop(chat_id, None)


def _keyboard(token: str, *, running: bool = False) -> types.InlineKeyboardMarkup:
    if running:
        rows = [[types.InlineKeyboardButton(
            "Stop Tag All",
            callback_data=f"gh:tagall:stop:{token}",
            style=enums.ButtonStyle.DANGER,
        )]]
    else:
        rows = [[
            types.InlineKeyboardButton(
                "Start Tag All",
                callback_data=f"gh:tagall:start:{token}",
                style=enums.ButtonStyle.SUCCESS,
            ),
            types.InlineKeyboardButton(
                "Cancel",
                callback_data=f"gh:tagall:cancel:{token}",
                style=enums.ButtonStyle.DANGER,
            ),
        ]]
    return types.InlineKeyboardMarkup(rows)


def _reply_payload(message) -> str:
    if not message:
        return ""
    text = str(
        getattr(message, "text", None)
        or getattr(message, "caption", None)
        or ""
    ).strip()
    link = str(getattr(message, "link", None) or "").strip()
    if link and link not in text:
        text = f"{text}\n{link}".strip()
    return text[:_MAX_NOTE]


async def _collect_tags(chat_id: int) -> list[str]:
    """One emoji-mention anchor per member.

    The anchor text is an emoji, so nothing of the member's name is published
    in the group; Telegram still delivers the mention notification because the
    href carries their user id.
    """
    tags: list[str] = []
    async for member in app.get_chat_members(chat_id):
        user = member.user
        if not user or user.is_bot or user.is_deleted:
            continue
        tags.append(f'<a href="tg://user?id={user.id}">{_emoji_for(user.id)}</a>')
        if len(tags) >= _MAX_MEMBERS:
            break
    return tags


async def _execute(job: dict, stop: asyncio.Event) -> tuple[int, int, bool]:
    """Send mention chunks; repeat the admin's exact message/link in every one."""
    tags = await _collect_tags(job["chat_id"])
    if not tags:
        return 0, 0, False
    head = html.escape(job["note"])
    sent = failed = 0
    for index in range(0, len(tags), _CHUNK):
        if stop.is_set():
            break
        chunk = "".join(tags[index:index + _CHUNK])
        try:
            await app.send_message(
                job["chat_id"],
                f"{head}\n{chunk}",
                parse_mode=enums.ParseMode.HTML,
                # Every chunk points to the same replied message, not only the first.
                reply_to_message_id=job["reply_id"],
            )
            sent += 1
        except Exception as exc:
            failed += 1
            logger.info("Tag All chunk failed in %s: %s", job["chat_id"], exc)
        if index + _CHUNK < len(tags):
            await asyncio.sleep(1)
    return sent, failed, stop.is_set()


@app.on_message(
    filters.group
    & filters.command(["tagall", "mentionall", "all"], prefixes=PRE)
)
async def tagall(_, m: types.Message) -> None:
    if not await require_admin(m, "can_pin_messages", "Pin messages"):
        return
    _expire_pending()
    parts = (m.text or "").split(maxsplit=1)
    note = (parts[1].strip() if len(parts) > 1 else "")[:_MAX_NOTE]
    if not note:
        note = _reply_payload(m.reply_to_message)
    if not note:
        await m.reply_text(
            "<b>Tag All</b>\n\n"
            "Send <code>/tagall your message</code>, or reply to any message "
            "with <code>/tagall</code>.\n\n"
            "Members are tagged by emoji — the mention is hidden behind it, "
            "so no names are posted in the group.\n\n"
            "Your message is repeated at the top of every batch.",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    now = time.time()
    wait = int(_COOLDOWN - (now - _last_run.get(m.chat.id, 0)))
    if wait > 0:
        await m.reply_text(f"Cooldown active. Next: try again in {wait}s.")
        return
    reply_id = getattr(getattr(m, "reply_to_message", None), "id", None)
    token = secrets.token_urlsafe(6)
    _pending[token] = {
        "chat_id": m.chat.id,
        "user_id": getattr(getattr(m, "from_user", None), "id", 0),
        "note": note,
        "reply_id": reply_id,
        "created": now,
    }
    if len(_pending) > 500:
        oldest = min(_pending, key=lambda key: _pending[key]["created"])
        _pending.pop(oldest, None)
    await m.reply_text(
        "<b>Tag All ready</b>\n\n"
        f"<b>Message in every batch:</b>\n{html.escape(note)}\n\n"
        f"Members are mentioned by emoji (names hidden).\n"
        f"Up to {_MAX_MEMBERS} members • {_CHUNK} mentions per batch",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=_keyboard(token),
    )


@app.on_callback_query(
    filters.regex(r"^gh:tagall:(start|cancel|stop):([A-Za-z0-9_-]+)$")
)
async def tagall_callback(_, q: types.CallbackQuery) -> None:
    action, token = q.data.rsplit(":", 2)[-2:]
    _expire_pending()
    job = _pending.get(token)
    # Powers decide, not who pressed first: every admin holding the same
    # right this command required may start, cancel or stop the run.
    if action == "stop":
        stop = _running.get(token)
        if not stop:
            await q.answer("Tag All is not running.", show_alert=True)
            return
        if not job:
            await q.answer("This Tag All button expired.", show_alert=True)
            return
        if not await has_right(job["chat_id"], q.from_user.id,
                               "can_pin_messages"):
            await q.answer(
                "You need the Pin messages admin right to stop this.",
                show_alert=True)
            return
        stop.set()
        await q.answer("Stopping…")
        return
    if not job:
        await q.answer("This Tag All button expired.", show_alert=True)
        return
    if not await has_right(job["chat_id"], q.from_user.id, "can_pin_messages"):
        await q.answer(
            "You need the Pin messages admin right to use this.",
            show_alert=True)
        return
    if action == "cancel":
        # Cancel and Start compete for the same token, so the removal happens
        # under the chat lock that Start also claims through.
        lock = _chat_locks.setdefault(job["chat_id"], asyncio.Lock())
        async with lock:
            if _active_chats.get(job["chat_id"]) == token:
                # Start already claimed this token; the job must stay pending
                # so Stop and the final cleanup still work.
                claimed = True
                cancelled = False
            else:
                claimed = False
                cancelled = _pending.pop(token, None) is job
        if claimed:
            await q.answer(
                "Tag All already started — use Stop Tag All.", show_alert=True)
            return
        if not cancelled:
            await q.answer("This Tag All button expired.", show_alert=True)
            return
        await q.message.edit_text("Tag All cancelled.")
        await q.answer()
        return
    if token in _running:
        await q.answer("Tag All is already running.", show_alert=True)
        return

    claimed, error = await _claim_start(job, token, q.from_user.id)
    if not claimed:
        await q.answer(error, show_alert=True)
        return
    stop = asyncio.Event()
    _running[token] = stop
    await q.answer("Tagging started.")
    await q.message.edit_text(
        "<b>Tag All running…</b>\n"
        "Your message/link is being repeated in every batch.",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=_keyboard(token, running=True),
    )
    try:
        sent, failed, stopped = await _execute(job, stop)
    except Exception:
        logger.exception("Tag All member fetch failed in %s", job["chat_id"])
        await _release_claim(job, token, rollback=True)
        await q.message.edit_text(
            "Could not fetch the member list. Next: check that I am an admin."
        )
        return
    finally:
        _running.pop(token, None)
        _pending.pop(token, None)
    await _release_claim(job, token, rollback=not sent)
    if sent:
        if len(_last_run) > 2000:
            _last_run.clear()
    if not sent:
        result = "Tag All failed: no mention batch was sent."
    elif stopped:
        result = f"Tag All stopped. Sent: {sent} • Failed: {failed}"
    else:
        result = f"Tag All complete. Sent: {sent} • Failed: {failed}"
    await q.message.edit_text(result)
