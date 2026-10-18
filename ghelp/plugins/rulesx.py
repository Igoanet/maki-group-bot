"""Rules extras (Rose parity): /privaterules /resetrules /setrulesbutton
/resetrulesbutton — plus the /start deep-link handler for private rules and
private notes."""

import html
import logging

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.selfdel import autodel_reply
from ghelp.database import db
from ghelp.helpers import concise_bot_text, require_admin, send_saved

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]


@autodel_reply
async def _reply(m: types.Message, text: str, **kwargs):
    return await m.reply_text(
        concise_bot_text(text), parse_mode=enums.ParseMode.HTML, **kwargs)


@app.on_message(filters.group & filters.command("privaterules", prefixes=PRE))
async def privaterules(_, m: types.Message):
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg not in ("on", "off"):
        cfg = await db.get_chat(m.chat.id)
        state = "ON" if cfg.get("privaterules") else "OFF"
        await _reply(m, f"Private rules: <b>{state}</b>.\n"
                        "Next: <code>/privaterules on|off</code>.")
        return
    await db.update_chat(m.chat.id, privaterules=(arg == "on"))
    await _reply(m, f"Private rules <b>{arg.upper()}</b>. "
                    + ("/rules now sends members to PM." if arg == "on"
                       else "/rules now shows them in the group."))


@app.on_message(filters.group & filters.command("privatenotes", prefixes=PRE))
async def privatenotes(_, m: types.Message):
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg not in ("on", "off"):
        await _reply(m, "Usage: <code>/privatenotes on|off</code> — notes open "
                        "in my PM instead of the group.")
        return
    await db.update_chat(m.chat.id, privatenotes=(arg == "on"))
    note = ("\nAnyone who taps the button can read the note in PM; "
            "notes are not secret.") if arg == "on" else ""
    await _reply(m, f"Private notes <b>{arg.upper()}</b>.{note}")


@app.on_message(filters.group & filters.command("resetrules", prefixes=PRE))
async def resetrules(_, m: types.Message):
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    await db.update_chat(m.chat.id, rules="")
    await _reply(m, "Group rules cleared.")


@app.on_message(filters.group & filters.command("setrulesbutton", prefixes=PRE))
async def setrulesbutton(_, m: types.Message):
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    label = " ".join(m.command[1:]).strip()
    if not label or len(label) > 30:
        await _reply(m, "Usage: <code>/setrulesbutton &lt;label, max 30 chars&gt;</code>")
        return
    await db.update_chat(m.chat.id, rules_btn=label)
    await _reply(m, f"Rules button label set to <b>{html.escape(label)}</b>.")


@app.on_message(filters.group & filters.command("resetrulesbutton", prefixes=PRE))
async def resetrulesbutton(_, m: types.Message):
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    await db.update_chat(m.chat.id, rules_btn="")
    await _reply(m, "Rules button label reset to default.")


async def serve_deeplink(m: types.Message, payload: str) -> bool:
    """Handle /start rules_<chat> and note_<chat>_<name> (PM). True = handled."""
    if payload.startswith("rules_"):
        try:
            chat_id = int(payload[6:])
        except ValueError:
            return True
        cfg = await db.get_chat(chat_id)
        rules = str(cfg.get("rules") or "")
        if not rules:
            await _reply(m, "That group has no rules set.")
            return True
        await send_saved(m, f"<b>Group rules:</b>\n\n{rules}", m)
        return True
    if payload.startswith("note_"):
        rest = payload[5:]
        chat_s, _, name = rest.partition("_")
        try:
            chat_id = int(chat_s)
        except ValueError:
            return True
        if not name:
            return True
        cfg = await db.get_chat(chat_id)
        text = (cfg.get("notes") or {}).get(name.lower())
        if not text:
            await _reply(m, "Note not found.")
            return True
        # Reuse the normal renderer so private notes preserve media, buttons,
        # fillings, control tags, and random variants instead of stringifying
        # media dictionaries into Python repr text.
        me = await app.get_me()
        await send_saved(
            m, text, m,
            note_url=f"https://t.me/{me.username}?start=note_{chat_id}_{name}",
        )
        return True
    if payload.startswith("filter_"):
        rest = payload[7:]
        chat_s, _, keyword = rest.partition("_")
        try:
            chat_id = int(chat_s)
        except ValueError:
            return True
        key = keyword.replace("_", " ").casefold()
        cfg = await db.get_chat(chat_id)
        entry = (cfg.get("filters") or {}).get(key)
        if not entry:
            await _reply(m, "Filter reply not found.")
            return True
        await send_saved(m, entry, m)
        return True
    return False
