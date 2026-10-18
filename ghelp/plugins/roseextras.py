"""Rose parity extras: /tmute, /demote, /admincache, /echo, /say, /broadcast,
warn aliases (/warnings /rmwarn /setwarnlimit /setwarnmode /setwarntime
/warntime), /adminerror, /anonadmin, /privacy."""

import asyncio
import html
import logging
import time

from pyrogram import enums, filters, types
from pyrogram.errors import FloodWait

from ghelp import app
from ghelp.selfdel import autodel_reply
from ghelp.config import config
from ghelp.database import db
from ghelp.helpers import (admin_cache_clear_chat, is_admin,
                           is_owner, mention, parse_duration, require_admin,
                           require_owner, require_right, resolve_user)
from ghelp.plugins.admin import (
    _muted_permissions,
    _warn_target,
    require_warn_reset,
    warn_reset_denied,
    warn_lock,
)
from ghelp.plugins.gtools import _bot_can_pin
from ghelp.moderation_reply import action_text, failure_text, send_reply
from ghelp.sudo import is_sudo

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]


@autodel_reply
async def _reply(m: types.Message, text: str, **kwargs):
    preserve_text = kwargs.pop("preserve_text", False)
    return await send_reply(
        m, text, concise=not preserve_text, client=app, **kwargs
    )


@app.on_message(filters.group & filters.command("tmute", prefixes=PRE))
async def tmute(_, m: types.Message):
    if not await require_right(m, "can_restrict_members", "Ban users"):
        return
    if m.reply_to_message and m.reply_to_message.from_user:
        user = m.reply_to_message.from_user
        dur_raw = m.command[1] if len(m.command) > 1 else ""
        reason = " ".join(m.command[2:])
    else:
        if len(m.command) < 3:
            await _reply(m, "Usage: reply <code>/tmute 2h</code> or "
                            "<code>/tmute @user 2h [reason]</code>")
            return
        user = await resolve_user(m)
        dur_raw = m.command[2]
        reason = " ".join(m.command[3:])
    if not user:
        await _reply(m, "User not found. Next: reply to the user or provide an @username/id.")
        return
    secs = parse_duration(dur_raw)
    if not secs:
        if not dur_raw:
            await _reply(m, "Usage: reply <code>/tmute 2h</code> or "
                            "<code>/tmute @user 2h [reason]</code>")
        else:
            await _reply(m, "Invalid duration. Example: 10m, 2h, or 1d.")
        return
    if user.is_bot or await is_admin(m.chat.id, user.id):
        await _reply(m, "Cannot mute admins or bots.")
        return
    try:
        result = await app.restrict_chat_member(
            m.chat.id, user.id, _muted_permissions(),
            until_date=int(time.time()) + secs,
        )
        succeeded = result is not False
    except Exception as exc:
        logger.info("tmute: %s", exc)
        succeeded = False
    if not succeeded:
        await _reply(
            m,
            failure_text(
                m,
                "mute",
                user,
                duration=dur_raw,
                reason=reason,
                detail="Restrict Members permission may be missing.",
            ),
        )
        return
    # A moderation mute supersedes any force-sub restriction on this user.
    try:
        from ghelp.plugins.forcesub import untrack_fsub
        await untrack_fsub(m.chat.id, user.id)
    except Exception:
        pass
    await _reply(
        m,
        action_text(m, "Muted", user, duration=dur_raw, reason=reason),
    )


@app.on_message(filters.group & filters.command("demote", prefixes=PRE))
async def demote(_, m: types.Message):
    if not await require_right(m, "can_promote_members", "Add admins"):
        return
    user = await resolve_user(m)
    if not user:
        await _reply(m, "Reply to an admin or give an @username/id.")
        return
    try:
        # empty privileges = full demotion in the Bot API
        await app.promote_chat_member(m.chat.id, user.id,
                                      privileges=types.ChatPrivileges())
    except Exception as exc:
        logger.info("demote: %s", exc)
        await _reply(m, "Could not demote. Only admins I promoted can be demoted.")
        return
    who = mention(user.id, html.escape(user.first_name or "user"))
    await _reply(m, f"Demoted: {who} is now a regular member.")


@app.on_message(filters.group & filters.command("admincache", prefixes=PRE))
async def admincache(_, m: types.Message):
    if not await require_admin(m, "can_promote_members", "Add admins"):
        return
    admin_cache_clear_chat(m.chat.id)
    await _reply(m, "Admin cache refreshed.")


@app.on_message(filters.group & filters.command("rmwarn", prefixes=PRE))
async def rmwarn(_, m: types.Message):
    if not await require_warn_reset(m):
        return
    user = await _warn_target(m)
    if not user:
        await _reply(m, "Reply to a user or give an @username/id.")
        return
    outcome: dict = {}

    async with warn_lock(m.chat.id, user.id):
        caller_is_owner = bool(
            m.from_user
            and await is_owner(m.chat.id, m.from_user.id)
        )

        def _rm(cfg):
            if cfg.get("linkdel_on", False) and not caller_is_owner:
                outcome["denied"] = True
                return
            w = dict(cfg.get("warns") or {})
            key = str(user.id) if str(user.id) in w else (
                user.id if user.id in w else None
            )
            if key is not None and int(w.get(key) or 0) > 0:
                w[key] = int(w[key]) - 1
                outcome["n"] = w[key]
            cfg["warns"] = w

        await db.mutate_chat(m.chat.id, _rm)
    if outcome.get("denied"):
        await warn_reset_denied(m)
        return
    who = mention(user.id, html.escape(user.first_name or "user"))
    if "n" in outcome:
        await _reply(m, f"Removed one warn from {who}; now <b>{outcome['n']}</b>.")
    else:
        await _reply(m, f"{who} has no warns.")


@app.on_message(filters.group & filters.command("resetwarn", prefixes=PRE))
async def resetwarn(_, m: types.Message):
    """Reset every warning for one user."""
    if not await require_warn_reset(m):
        return
    user = await _warn_target(m)
    if not user:
        await _reply(m, "Reply to a user or give an @username/id.")
        return
    outcome: dict = {}
    async with warn_lock(m.chat.id, user.id):
        caller_is_owner = bool(
            m.from_user and await is_owner(m.chat.id, m.from_user.id)
        )

        def _reset(cfg):
            if cfg.get("linkdel_on", False) and not caller_is_owner:
                outcome["denied"] = True
                return
            warns = dict(cfg.get("warns") or {})
            for key in (str(user.id), user.id):
                if key in warns:
                    warns.pop(key, None)
                    outcome["reset"] = True
            cfg["warns"] = warns

        await db.mutate_chat(m.chat.id, _reset)
    if outcome.get("denied"):
        await warn_reset_denied(m)
        return
    who = mention(user.id, html.escape(user.first_name or "user"))
    await _reply(
        m,
        f"Reset all warns for {who}."
        if outcome.get("reset") else f"{who} has no warns.",
    )


@app.on_message(filters.group & filters.command("setwarnlimit", prefixes=PRE))
async def setwarnlimit(_, m: types.Message):
    if not await require_owner(m):
        return
    try:
        n = int(m.command[1])
        if not 0 <= n <= 20:
            raise ValueError
    except (IndexError, ValueError):
        await _reply(m, "Usage: <code>/setwarnlimit &lt;0-20&gt;</code>")
        return
    cfg = await db.get_chat(m.chat.id)
    if cfg.get("linkdel_on") and n != 3:
        await _reply(
            m,
            "Link warnings are enabled: the warning limit must remain <b>3</b>.",
        )
        return
    await db.update_chat(m.chat.id, warn_limit=n)
    await _reply(m, f"Warn limit set to <b>{n}</b>.")


@app.on_message(filters.group & filters.command(["setwarnmode", "warnmode"], prefixes=PRE))
async def setwarnmode(_, m: types.Message):
    if not await require_owner(m):
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg not in ("mute", "kick", "ban"):
        await _reply(m, "Usage: <code>/setwarnmode mute|kick|ban</code>")
        return
    cfg = await db.get_chat(m.chat.id)
    if cfg.get("linkdel_on") and arg != "mute":
        await _reply(
            m,
            "Link warnings are enabled: the warning action must remain <b>mute</b>.",
        )
        return
    await db.update_chat(m.chat.id, warn_action=arg)
    await _reply(m, f"Warn-limit action set to <b>{arg}</b>.")


@app.on_message(filters.group & filters.command("setwarntime", prefixes=PRE))
async def setwarntime(_, m: types.Message):
    if not await require_owner(m):
        return
    secs = parse_duration(m.command[1]) if len(m.command) > 1 else None
    if not secs or not 60 <= secs <= 30 * 86400:
        await _reply(m, "Usage: <code>/setwarntime &lt;duration&gt;</code> "
                        "(1m to 30d — mute length when the warn limit is hit)")
        return
    await db.update_chat(m.chat.id, warn_time=secs)
    await _reply(m, f"Warn-mute duration set to <b>{html.escape(m.command[1])}</b>.")


@app.on_message(filters.group & filters.command("warntime", prefixes=PRE))
async def warntime(_, m: types.Message):
    if not await require_owner(m):
        return
    if len(m.command) > 1:
        raw = m.command[1].lower()
        if raw == "off":
            await _reply(m, "Warning expiry cannot be disabled in this build; use 30d maximum.")
            return
        seconds = parse_duration(raw)
        if not seconds or not 60 <= seconds <= 30 * 86400:
            await _reply(m, "Usage: <code>/warntime 4m|3h|6d</code> (up to 30d).")
            return
        await db.update_chat(m.chat.id, warn_time=seconds)
        await _reply(m, f"Warn-mute duration set to <b>{html.escape(raw)}</b>.")
        return
    cfg = await db.get_chat(m.chat.id)
    secs = int(cfg.get("warn_time", 86400))
    await _reply(m, f"Warn-mute duration: <b>{secs // 3600}h "
                    f"{(secs % 3600) // 60}m</b>.")


@app.on_message(filters.group & filters.command("warnings", prefixes=PRE))
async def warning_settings(_, m: types.Message):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    cfg = await db.get_chat(m.chat.id)
    await _reply(
        m,
        f"Warnings: limit <b>{int(cfg.get('warn_limit', 3))}</b>, "
        f"action <b>{html.escape(str(cfg.get('warn_action', 'mute')))}</b>, "
        f"mute time <b>{int(cfg.get('warn_time', 86400)) // 3600}h</b>.",
    )


@app.on_message(filters.group & filters.command(["adminerror", "anonadmin"], prefixes=PRE))
async def admin_toggles(_, m: types.Message):
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg not in ("on", "off"):
        await _reply(m, f"Usage: <code>/{m.command[0]} on|off</code>")
        return
    key = m.command[0].lower()
    await db.update_chat(m.chat.id, **{key: arg == "on"})
    what = ("Replies to non-admins using admin commands" if key == "adminerror"
            else "Anonymous group admins can use admin commands")
    await _reply(m, f"{what}: <b>{arg.upper()}</b>.")


@app.on_message(filters.command(["echo", "say"], prefixes=PRE))
async def echo(_, m: types.Message):
    in_group = m.chat.type != enums.ChatType.PRIVATE
    if in_group:
        if not await require_admin(m, "can_change_info",
                                   "Change group info"):
            return
    elif not m.from_user or m.from_user.id != config.OWNER_ID:
        return
    text = " ".join(m.command[1:]).strip()
    if not text and m.reply_to_message:
        text = (m.reply_to_message.text or m.reply_to_message.caption or "").strip()
    if not text:
        await _reply(m, "No text provided. Next: add text or reply to a message.")
        return
    if m.command[0].lower() == "say":
        try:
            await m.delete()
        except Exception:
            pass
        try:
            await app.send_message(m.chat.id, text, parse_mode=enums.ParseMode.HTML)
        except Exception:
            await app.send_message(m.chat.id, text)  # bad HTML -> plain
    else:
        try:
            await _reply(m, text, preserve_text=True)
        except Exception:
            await m.reply_text(text)


async def _pin_broadcast(chat_id: int, sent) -> bool:
    """Pin a delivered broadcast where the bot itself may pin.

    Telegram applies the BOT's own rights to the pin call, so a group where
    it was never given Pin messages simply keeps the plain message instead of
    failing the broadcast.  Pinning stays silent: the broadcast already
    notified everyone.
    """
    message_id = getattr(sent, "id", None)
    if not isinstance(message_id, int):
        return False
    try:
        if not await _bot_can_pin(chat_id):
            return False
        return await app.pin_chat_message(
            chat_id, message_id, disable_notification=True) is not False
    except Exception:
        return False


@app.on_message(filters.command("broadcast", prefixes=PRE))
async def broadcast(_, m: types.Message):
    if not m.from_user or not await is_sudo(m.from_user.id):
        return
    text = " ".join(m.command[1:]).strip()
    if not text and m.reply_to_message:
        text = (m.reply_to_message.text or m.reply_to_message.caption or "").strip()
    if not text:
        await _reply(m, "Usage: <code>/broadcast &lt;text&gt;</code> or reply to a message.")
        return
    chats = await db.all_chats()
    status = await _reply(m, f"Broadcasting to {len(chats)} groups…")
    ok = fail = pinned = 0
    for cid_s in chats:
        chat_id = int(cid_s)
        try:
            sent = await app.send_message(chat_id, text,
                                          parse_mode=enums.ParseMode.HTML)
        except FloodWait as fw:
            await asyncio.sleep(min(int(fw.value), 30))
            try:
                sent = await app.send_message(chat_id, text,
                                              parse_mode=enums.ParseMode.HTML)
            except Exception:
                sent = None
        except Exception:
            sent = None
        if sent is None:
            fail += 1
        else:
            ok += 1
            if await _pin_broadcast(chat_id, sent):
                pinned += 1
        await asyncio.sleep(0.1)  # stay well under flood limits
    await status.edit_text(f"Broadcast done — <b>{ok}</b> delivered, "
                           f"<b>{fail}</b> failed, <b>{pinned}</b> pinned.",
                           parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command("privacy", prefixes=PRE))
async def privacy(_, m: types.Message):
    if m.chat.type != enums.ChatType.PRIVATE:
        await _reply(m, "For privacy and data tools, message me in a private chat.")
        return
    action = m.command[1].lower() if len(m.command) > 1 else ""
    if action in {"delete", "remove"}:
        await _reply(
            m,
            "<b>Delete your data?</b>\nThis removes personal records held by this "
            "bot. This cannot be undone.",
            reply_markup=types.InlineKeyboardMarkup([[
                types.InlineKeyboardButton("Confirm delete", callback_data=f"gh:privacy:delete:{m.from_user.id}"),
                types.InlineKeyboardButton("Cancel", callback_data=f"gh:privacy:cancel:{m.from_user.id}"),
            ]]),
        )
        return
    if action in {"view", "data"}:
        await _reply(m, f"<b>Your stored data</b>\nTelegram user ID: <code>{m.from_user.id}</code>\n"
                        "This instance stores no private message history or profile database. "
                        "Group settings are owned by each group and cannot be viewed or deleted from PM.")
        return
    await _reply(
        m,
        "<b>Privacy tools</b>\n"
        "We store group configuration (including notes, filters and moderation "
        "settings) so the bot can operate. Private message history is not stored "
        "or shared.\n\n"
        "Use <code>/privacy view</code> to inspect data associated with your "
        "account, or <code>/privacy delete</code> to request deletion. "
        "Group administrators can remove a group's configuration by removing the bot.",
    )


@app.on_callback_query(filters.regex(r"^gh:privacy:(?:delete|cancel):\d+$"))
async def privacy_callback(_, query: types.CallbackQuery):
    parts = query.data.split(":")
    if not query.from_user or int(parts[-1]) != query.from_user.id:
        await query.answer("This privacy action belongs to another user.", show_alert=True)
        return
    if parts[2] == "cancel":
        await query.message.edit_text("Privacy deletion cancelled.")
        await query.answer("Cancelled.")
        return
    await query.message.edit_text(
        "Deletion complete. This instance had no private message history or "
        "personal profile record to remove."
    )
    await query.answer("Your data request was completed.")


@app.on_message(filters.private & filters.command("markdownhelp", prefixes=PRE))
async def markdown_help(_, m: types.Message):
    await _reply(m, "<b>Markdown and formatting</b>\n"
                    "<code>`code`</code> <i>_italic_</i> <b>*bold*</b> "
                    "<u>__underline__</u> <s>~strike~</s> "
                    "<tg-spoiler>||spoiler||</tg-spoiler>\n"
                    "Blocks: <code>```lang\\ncode```</code>, "
                    "<code>&gt; quote</code>, <code>**&gt; expandable||</code>\n"
                    "Links: <code>[label](https://example.com)</code>\n"
                    "Buttons: <code>[Label](buttonurl://https://example.com)</code> "
                    "or <code>buttonurl#primary|danger|success</code>\n"
                    "Fillings: <code>{first}</code> <code>{fullname}</code> "
                    "<code>{username}</code> <code>{mention}</code> <code>{id}</code>.")


@app.on_message(filters.command("donate", prefixes=PRE))
async def donate(_, m: types.Message):
    await _reply(m, "Help keep this bot running by supporting its maintainer: "
                    f"{html.escape(config.OWNER_LINK)}")


@app.on_message(filters.command("runs", prefixes=PRE))
async def runs(_, m: types.Message):
    await _reply(m, f"<b>{html.escape(config.BOT_NAME)}</b> is online and serving "
                    "this chat through its bot session.")


@app.on_message(filters.command("limits", prefixes=PRE))
async def limits(_, m: types.Message):
    await _reply(m, "Current limits: purges up to 1000 messages per command; "
                    "reports are cooldown-protected; Tag All and saved content "
                    "limits are shown in Help.")


@app.on_message(filters.command("info", prefixes=PRE))
async def info(_, m: types.Message):
    try:
        chat = await app.get_chat(m.chat.id)
        title = html.escape(getattr(chat, "title", None) or getattr(chat, "first_name", None) or "Chat")
        kind = html.escape(str(getattr(chat, "type", "unknown")))
        await _reply(m, f"<b>{title}</b>\nType: <code>{kind}</code>\nID: <code>{chat.id}</code>")
    except Exception:
        await _reply(m, f"Chat ID: <code>{m.chat.id}</code>")
