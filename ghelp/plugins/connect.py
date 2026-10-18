"""Connections (Rose /connect): manage your group from the bot's PM.

/connect in a group gives a deep-link button; in PM, /connect <chat id>
connects directly. /mygroup opens the management panel (view notes, rules,
blocklist and settings of the connected group)."""

import html
import logging

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.selfdel import autodel_reply
from ghelp.database import db
from ghelp.helpers import concise_bot_text, ikb, ikm, is_admin, require_admin

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]
_KEY = "connections"  # global store: uid -> {current, previous, recent}


@autodel_reply
async def _reply(m: types.Message, text: str, **kwargs):
    return await m.reply_text(
        concise_bot_text(text), parse_mode=enums.ParseMode.HTML, **kwargs)


async def _get_conn(uid: int) -> int | None:
    conns = (await db.get(_KEY)) or {}
    value = conns.get(str(uid))
    return value.get("current") if isinstance(value, dict) else value


async def _set_conn(uid: int, chat_id: int) -> None:
    conns = (await db.get(_KEY)) or {}
    old = conns.get(str(uid))
    if isinstance(old, dict):
        recent = [chat_id] + [x for x in old.get("recent", []) if x != chat_id]
        conns[str(uid)] = {"current": chat_id, "previous": old.get("current"),
                           "recent": recent[:10]}
    else:
        conns[str(uid)] = {"current": chat_id, "previous": old, "recent": [chat_id]}
    await db.set(_KEY, conns)


async def _del_conn(uid: int) -> None:
    conns = (await db.get(_KEY)) or {}
    value = conns.get(str(uid))
    if isinstance(value, dict):
        if value.get("current") is not None:
            value["previous"] = value["current"]
        value["current"] = None
        conns[str(uid)] = value
        await db.set(_KEY, conns)
    elif conns.pop(str(uid), None) is not None:
        await db.set(_KEY, conns)


async def _recent(uid: int) -> list[int]:
    value = ((await db.get(_KEY)) or {}).get(str(uid), [])
    return value.get("recent", []) if isinstance(value, dict) else ([value] if value else [])


async def _title(chat_id: int) -> str:
    try:
        chat = await app.get_chat(chat_id)
        return html.escape(chat.title or str(chat_id))
    except Exception:
        return str(chat_id)


@app.on_message(filters.group & filters.command("connect", prefixes=PRE))
async def connect_group(_, m: types.Message):
    if not await require_admin(m):
        return
    if not m.from_user:
        await _reply(m, "Anonymous admins must use the PM connect button.")
        return
    await _set_conn(m.from_user.id, m.chat.id)
    await _reply(m, f"Success: connected to <b>{await _title(m.chat.id)}</b> "
                    f"(<code>{m.chat.id}</code>).")


@app.on_message(filters.private & filters.command("connect", prefixes=PRE))
async def connect_pm(_, m: types.Message):
    if not m.from_user:
        return
    if len(m.command) < 2:
        recent = await _recent(m.from_user.id)
        if not recent:
            await _reply(m, "No recently connected chats. Use /connect &lt;chat id or username&gt;.")
        else:
            await _reply(m, "Recently connected:\n" + "\n".join(
                f"• <code>{cid}</code> — {await _title(cid)}" for cid in recent))
        return
    try:
        target = m.command[1]
        chat = await app.get_chat(int(target) if target.lstrip("-").isdigit() else target)
        chat_id = chat.id
    except Exception:
        await _reply(m, "Error: that chat could not be found.")
        return
    if not await is_admin(chat_id, m.from_user.id):
        await _reply(m, "Blocked: you're not an admin of that group.")
        return
    await _set_conn(m.from_user.id, chat_id)
    await _reply(m, f"Success: connected to <b>{await _title(chat_id)}</b> "
                    f"(<code>{chat_id}</code>).\nUse /mygroup to manage it.")


async def serve_deeplink(m: types.Message, payload: str) -> bool:
    """Handle /start connect_<chat> (from the group's connect button)."""
    if not payload.startswith("connect_"):
        return False
    try:
        chat_id = int(payload[8:])
    except ValueError:
        return True
    if not m.from_user:
        return True
    if not await is_admin(chat_id, m.from_user.id):
        await _reply(m, "Blocked: you're not an admin of that group.")
        return True
    await _set_conn(m.from_user.id, chat_id)
    await _reply(m, f"Success: connected to <b>{await _title(chat_id)}</b> "
                    f"(<code>{chat_id}</code>).\nUse /mygroup to manage it.")
    return True


@app.on_message(filters.private & filters.command("disconnect", prefixes=PRE))
async def disconnect(_, m: types.Message):
    if not m.from_user:
        return
    await _del_conn(m.from_user.id)
    await _reply(m, "Success: disconnected.")


@app.on_message(filters.private & filters.command("reconnect", prefixes=PRE))
async def reconnect(_, m: types.Message):
    if not m.from_user:
        return
    value = ((await db.get(_KEY)) or {}).get(str(m.from_user.id))
    previous = value.get("previous") if isinstance(value, dict) else value
    if not previous:
        await _reply(m, "Info: no previously connected chat.")
        return
    if not await is_admin(previous, m.from_user.id):
        await _reply(m, "Blocked: you're no longer an admin of that group.")
        return
    await _set_conn(m.from_user.id, previous)
    await _reply(m, f"Success: reconnected to <b>{await _title(previous)}</b>.")


@app.on_message(filters.private & filters.command("connection", prefixes=PRE))
async def connection(_, m: types.Message):
    if not m.from_user:
        return
    chat_id = await _get_conn(m.from_user.id)
    if not chat_id:
        await _reply(m, "Info: no active connection. Use /connect first.")
        return
    await _reply(m, f"Connected to <b>{await _title(chat_id)}</b> "
                    f"(<code>{chat_id}</code>).")


def _panel_kb() -> types.InlineKeyboardMarkup:
    return ikm([
        [ikb("Notes", callback_data="gh:cx:notes", style=enums.ButtonStyle.PRIMARY),
         ikb("Rules", callback_data="gh:cx:rules", style=enums.ButtonStyle.PRIMARY)],
        [ikb("Blocklist", callback_data="gh:cx:blacklist", style=enums.ButtonStyle.PRIMARY)],
        [ikb("Settings", callback_data="gh:cx:settings", style=enums.ButtonStyle.PRIMARY)],
        [ikb("Close", callback_data="gh:close", style=enums.ButtonStyle.DANGER)],
    ])


@app.on_message(filters.private & filters.command("mygroup", prefixes=PRE))
async def mygroup(_, m: types.Message):
    if not m.from_user:
        return
    chat_id = await _get_conn(m.from_user.id)
    if not chat_id:
        await _reply(m, "Info: no active connection. Use /connect first.")
        return
    await _reply(m, f"<b>{await _title(chat_id)}</b>\nChoose what to view:",
                 reply_markup=_panel_kb())


@app.on_callback_query(filters.regex(r"^gh:cx:"))
async def cx_cb(_, q: types.CallbackQuery):
    try:
        if not q.from_user or not q.message:
            return
        uid = q.from_user.id
        chat_id = await _get_conn(uid)
        if not chat_id:
            await q.answer("No active connection — /connect first.", show_alert=True)
            return
        if not await is_admin(chat_id, uid):
            await q.answer("You're no longer an admin of that group.",
                           show_alert=True)
            return
        what = q.data.removeprefix("gh:cx:")
        cfg = await db.get_chat(chat_id)
        title = await _title(chat_id)
        back = ikm([[ikb("Back", callback_data="gh:cx:main",
                         style=enums.ButtonStyle.PRIMARY)]])
        if what == "notes":
            names = sorted((cfg.get("notes") or {}).keys())
            text = ("<b>Notes:</b> " + ", ".join(f"<code>#{html.escape(n)}</code>"
                    for n in names)) if names else "Info: no notes saved."
        elif what == "rules":
            rules = str(cfg.get("rules") or "")
            text = f"<b>Rules:</b>\n\n{rules}" if rules else "Info: no rules set."
        elif what == "blacklist":
            words = cfg.get("blacklist") or []
            text = ("<b>Blocklist:</b> " + ", ".join(f"<code>{html.escape(w)}</code>"
                    for w in words[:50])) if words else "Info: blocklist is empty."
        elif what == "settings":
            def onoff(v):
                return "ON" if v else "OFF"
            text = (
                f"<b>Settings:</b>\n"
                f"• Captcha: <b>{onoff(cfg.get('captcha_on'))}</b>\n"
                f"• Approval: <b>{onoff(cfg.get('approval_on'))}</b>\n"
                f"• Antiflood: <b>{onoff(cfg.get('flood_on'))}</b> "
                f"(limit {cfg.get('flood_limit', 5)})\n"
                f"• Antiraid auto: <b>{onoff(cfg.get('raid_on', True))}</b>\n"
                f"• Anti-forward: <b>{onoff(cfg.get('antiforward'))}</b>\n"
                f"• Blocklist mode: <b>{cfg.get('bl_mode', 'del')}</b>\n"
                f"• Warns: limit <b>{cfg.get('warn_limit', 3)}</b> → "
                f"<b>{cfg.get('warn_action', 'mute')}</b>\n"
                f"• Private rules/notes: <b>{onoff(cfg.get('privaterules'))}</b>/"
                f"<b>{onoff(cfg.get('privatenotes'))}</b>"
            )
        else:
            await q.message.edit_text(
                f"<b>{title}</b>\nChoose what to view:",
                parse_mode=enums.ParseMode.HTML,
                reply_markup=_panel_kb(),
            )
            await q.answer()
            return
        await q.message.edit_text(f"<b>{title}</b>\n\n{text}",
                                  parse_mode=enums.ParseMode.HTML,
                                  reply_markup=back)
        await q.answer()
    except Exception as exc:
        logger.info("connect cb: %s", exc)
        try:
            await q.answer("Could not load that.", show_alert=True)
        except Exception:
            pass
