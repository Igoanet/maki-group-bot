"""Log channel (Rose /setlog): mirror moderation events to a private channel
so deleted-content evidence survives and admins have an audit trail."""

import html
import logging

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.database import db
from ghelp.helpers import require_admin

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]


async def log_event(chat_id: int, text: str) -> None:
    """Mirror a moderation event to the chat's log channel (if configured).

    Fire-and-forget: logging must never break the moderation action itself.
    """
    try:
        cfg = await db.get_chat(chat_id)
        target = int(cfg.get("log_chat") or 0)
        if not target:
            return
        await app.send_message(
            target,
            text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.info("logchan: %s", exc)


@app.on_message(filters.group & filters.command("setlog", prefixes=PRE))
async def set_log(_, message: types.Message):
    if not await require_admin(message, "can_change_info", "Change group info"):
        return
    if len(message.command) < 2:
        await message.reply_text(
            "Usage: <code>/setlog &lt;@channel or id&gt;</code> — add me to that channel as admin first.",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    try:
        chat = await app.get_chat(message.command[1])
    except Exception:
        await message.reply_text("Error: could not find that channel. Check the username/id.")
        return
    group_title = html.escape(message.chat.title or "this group")
    try:
        probe = await app.send_message(
            chat.id,
            f"Log channel linked to <b>{group_title}</b>.",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await message.reply_text("Error: I can't post there — make me an admin in that channel first.")
        return
    await db.update_chat(message.chat.id, log_chat=chat.id)
    await message.reply_text(
        f"Success: moderation log set to <b>{html.escape(chat.title or str(chat.id))}</b>.",
        parse_mode=enums.ParseMode.HTML,
    )
    logger.info("logchan linked: %s -> %s (probe msg %s)", message.chat.id, chat.id, probe.id)


@app.on_message(filters.group & filters.command("unsetlog", prefixes=PRE))
async def unset_log(_, message: types.Message):
    if not await require_admin(message, "can_change_info", "Change group info"):
        return
    await db.update_chat(message.chat.id, log_chat=0)
    await message.reply_text("Success: log channel unlinked.")
