"""Configurable deletion of command messages (without suppressing handlers)."""
from __future__ import annotations

from pyrogram import enums, filters

from ghelp import app
from ghelp.database import db
from ghelp.helpers import has_right, is_admin

COMMAND_TYPES = ("all", "admin", "user", "other")

# Rose's distinction is based on the command, not who sent it.  Keep this
# inventory explicit: adding a command to another plugin only requires adding
# it here, and unknown commands remain safely classed as ``other``.
ADMIN_COMMANDS = {
    "ban", "dban", "sban", "tban", "unban", "mute", "dmute", "smute", "tmute",
    "unmute", "kick", "dkick", "skick", "warn", "dwarn", "swarn", "setwarnlimit",
    "setwarnmode", "setwarntime", "lock", "unlock", "setrules", "resetrules",
    "setlog", "unsetlog", "promote", "fullpromote", "demote", "setwelcome",
    "resetwelcome", "setgoodbye", "resetgoodbye", "cleanwelcome", "flood",
    "setflood", "setfloodtimer", "setfloodmode", "clearflood", "antiraid",
    "raidtime", "raidactiontime", "autoantiraid", "cleanservice", "keepservice",
    "nocleanservice", "cleanservicetypes", "cleancommand", "keepcommand",
    "cleancommandtypes", "shield", "antiforward", "slowmode", "settitle",
    "setdescription", "setgpic", "unpinall", "permapin", "night", "setnight",
}
USER_COMMANDS = {
    "id", "info", "get", "notes", "saved", "rules", "help", "start", "ping",
    "kickme", "approval", "approved", "filters", "stats", "adminlist", "pinned",
    "q", "quote", "fq", "tq", "remindme", "reminders", "delreminder",
    "w", "markdownhelp", "clear", "clearall",
}


def _kind(message) -> str:
    command = getattr(message, "command", None) or ()
    name = str(command[0] if command else "").lower().lstrip("/").split("@", 1)[0]
    if name in ADMIN_COMMANDS:
        return "admin"
    if name in USER_COMMANDS:
        return "user"
    return "other"


def _matches(cfg: dict, kind: str) -> bool:
    selected = cfg.get("cleancommand_types", [])
    if isinstance(selected, dict):
        return bool(selected.get("all") or selected.get(kind))
    return isinstance(selected, (list, tuple, set)) and (
        "all" in selected or kind in selected
    )


async def _configure(message, keep: bool) -> None:
    if getattr(getattr(message, "chat", None), "type", None) not in (
        enums.ChatType.GROUP, enums.ChatType.SUPERGROUP
    ):
        return
    uid, chat_id = getattr(getattr(message, "from_user", None), "id", None), message.chat.id
    if not isinstance(uid, int) or not await has_right(
        chat_id, uid, "can_delete_messages"
    ):
        # this setting makes the bot delete members' command messages
        return
    args = [str(x).lower() for x in (getattr(message, "command", None) or [])[1:]]
    if not args:
        await message.reply_text("Usage: /cleancommand all|admin|user|other")
        return
    chosen = set()
    for arg in args:
        if arg in ("yes", "on"):
            arg = "all"
        if arg not in COMMAND_TYPES:
            await message.reply_text("Types: all, admin, user, other")
            return
        chosen.add(arg)
    cfg = await db.get_chat(chat_id)
    current = set(cfg.get("cleancommand_types", []))
    if "all" in chosen:
        current = set() if keep else {"all"}
    elif keep:
        current -= chosen
    else:
        current |= chosen
    await db.update_chat(chat_id, cleancommand_types=sorted(current))
    await message.reply_text("Command cleanup: " + (", ".join(sorted(current)) if current else "off"))


@app.on_message(filters.group & filters.command("cleancommand", prefixes=["/", "?", "!", "."]), group=15)
async def cleancommand_command(_, message):
    await _configure(message, False)


@app.on_message(filters.group & filters.command("keepcommand", prefixes=["/", "?", "!", "."]), group=15)
async def keepcommand_command(_, message):
    await _configure(message, True)


@app.on_message(filters.group & filters.command("cleancommandtypes", prefixes=["/", "?", "!", "."]), group=15)
async def cleancommandtypes_command(_, message):
    await message.reply_text(
        "<b>Command cleanup types</b>\nall — every command\nadmin — admin/settings commands\n"
        "user — recognised user commands\nother — unrecognised commands",
        parse_mode=enums.ParseMode.HTML,
    )


@app.on_message(filters.group & ~filters.service, group=15)
async def cleancommand_watcher(_, message) -> None:
    if not getattr(message, "command", None):
        return
    cfg = await db.get_chat(message.chat.id)
    if not _matches(cfg, _kind(message)):
        return
    try:
        await app.delete_messages(message.chat.id, message.id)
    except Exception:
        pass