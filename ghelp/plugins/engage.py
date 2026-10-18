"""Engagement & pacing: Telegram-native slow mode, channel-forward blocker,
and a per-chat activity leaderboard."""

import asyncio
import html
import logging
import time
from collections import defaultdict

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.database import db
from ghelp.helpers import is_admin, mention, parse_duration, require_admin, require_right

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]


# ---------------- slow mode (native Telegram per-user delay) ----------------

@app.on_message(filters.group & filters.command("slowmode", prefixes=PRE))
async def slowmode_cmd(_, message: types.Message):
    if not await require_right(message, "can_change_info", "Change group info"):
        return
    arg = message.command[1].lower() if len(message.command) > 1 else ""
    if not arg:
        await message.reply_text(
            "Usage: <code>/slowmode 30s</code>, <code>5m</code>, <code>1h</code> or <code>off</code>\n"
            "Each member can then send only one message per interval.",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    if arg in ("off", "0"):
        seconds = 0
    else:
        seconds = parse_duration(arg)
        if not seconds or not 5 <= seconds <= 3600:
            await message.reply_text("Error: choose between 5s and 1h (or off).")
            return
    try:
        await app.set_slow_mode(message.chat.id, seconds)
    except Exception as exc:
        logger.info("slowmode: %s", exc)
        await message.reply_text("Error: could not set slow mode (need admin rights).")
        return
    if seconds:
        await message.reply_text(
            f"Slow mode <b>ON</b> — one message per <b>{html.escape(arg)}</b> per member.",
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await message.reply_text("Slow mode <b>OFF</b>.", parse_mode=enums.ParseMode.HTML)


# ---------------- anti-forward (channel forwards / post-as-channel) --------

@app.on_message(filters.group & filters.command("antiforward", prefixes=PRE))
async def antiforward_cmd(_, message: types.Message):
    if not await require_admin(message, "can_delete_messages", "Delete messages"):
        return
    arg = message.command[1].lower() if len(message.command) > 1 else ""
    if arg not in ("on", "off"):
        cfg = await db.get_chat(message.chat.id)
        state = "ON" if cfg["antiforward"] else "OFF"
        await message.reply_text(
            f"Anti-forward is <b>{state}</b> — deletes messages forwarded from "
            "channels and posts sent as a channel (attackers use these to plant "
            "illegal content). The linked discussion channel is exempt.\n"
            "<b>Usage:</b> <code>/antiforward on|off</code>",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    await db.update_chat(message.chat.id, antiforward=(arg == "on"))
    await message.reply_text(f"Anti-forward <b>{arg.upper()}</b>.",
                             parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.group & ~filters.service, group=9)
async def antiforward_watcher(_, message: types.Message):
    try:
        cfg = await db.get_chat(message.chat.id)
        if not cfg["antiforward"]:
            return
        # linked-channel auto-forwards and admins posting as the group are legit
        if getattr(message, "automatic_forward", False):
            return
        # kurigram 2.2.x exposes forwards only via message.forward_origin
        # (MessageOriginChannel.chat / MessageOriginChat.sender_chat);
        # the old forward_from_chat attribute no longer exists.
        origin = getattr(message, "forward_origin", None)
        fwd_chat = (
            getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        ) if origin else None
        sender_chat = getattr(message, "sender_chat", None)
        from_channel = bool(
            fwd_chat and getattr(fwd_chat, "type", None) == enums.ChatType.CHANNEL
        )
        as_channel = bool(
            sender_chat
            and sender_chat.id != message.chat.id
            and getattr(sender_chat, "type", None) == enums.ChatType.CHANNEL
        )
        if not (from_channel or as_channel):
            return
        sender = message.from_user
        if sender and await is_admin(message.chat.id, sender.id):
            return  # live check — deletion path
        try:
            await message.delete()
        except Exception:
            return
        note = await app.send_message(
            message.chat.id,
            "Channel-forwarded/anonymous-channel messages are not allowed here.",
        )
        await asyncio.sleep(8)
        try:
            await note.delete()
        except Exception:
            pass
    except Exception as exc:
        logger.info("antiforward: %s", exc)


# ---------------- activity stats (RAM, resets daily) ----------------

_COUNT: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
_DAY: dict[int, str] = {}
_SEEN: dict[int, float] = {}
_MAX_TRACKED = 500    # users per chat per day — overflow drops the lowest
_IDLE_TTL = 2 * 86400  # chats idle this long lose their counters entirely


def _bump(chat_id: int, user_id: int) -> None:
    today = time.strftime("%Y-%m-%d")
    now = time.time()
    if _DAY.get(chat_id) != today:
        _COUNT[chat_id] = defaultdict(int)
        _DAY[chat_id] = today
    _SEEN[chat_id] = now
    counts = _COUNT[chat_id]
    if user_id not in counts and len(counts) >= _MAX_TRACKED:
        del counts[min(counts, key=counts.get)]  # leaderboard stays meaningful
    counts[user_id] += 1
    # prune fully-idle chats (review: dead chats' maps lived forever)
    for cid, seen in list(_SEEN.items()):
        if cid != chat_id and now - seen > _IDLE_TTL:
            _SEEN.pop(cid, None)
            _DAY.pop(cid, None)
            _COUNT.pop(cid, None)


@app.on_message(filters.group & ~filters.service, group=10)
async def stats_watcher(_, message: types.Message):
    sender = message.from_user
    if sender and not sender.is_bot:
        _bump(message.chat.id, sender.id)


@app.on_message(filters.group & filters.command("stats", prefixes=PRE))
async def stats_cmd(_, message: types.Message):
    if len(message.command) > 1 and message.command[1].lower() == "reset":
        if not await require_admin(message, "can_change_info",
                                   "Change group info"):
            return
        _COUNT.pop(message.chat.id, None)
        await message.reply_text("Success: today's stats reset.")
        return
    counts = _COUNT.get(message.chat.id) or {}
    if not counts:
        await message.reply_text("Info: no activity counted yet today.")
        return
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
    names: dict[int, str] = {}
    try:
        for u in await app.get_users([uid for uid, _ in top]):
            names[u.id] = html.escape(u.first_name or "user")
    except Exception as exc:
        logger.info("stats names: %s", exc)
    total = sum(counts.values())
    lines = [
        f"{i}. {mention(uid, names.get(uid, 'user'))} — <b>{n}</b>"
        for i, (uid, n) in enumerate(top, 1)
    ]
    await message.reply_text(
        f"<b>Today's leaderboard</b> ({total} messages):\n" + "\n".join(lines)
        + "\n\n<i>Counts reset daily and on bot restarts.</i>",
        parse_mode=enums.ParseMode.HTML,
    )
