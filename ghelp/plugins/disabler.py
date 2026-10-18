"""Per-chat command disabling for non-admins (Rose /disable).

The gate is a single early handler (group=-2) that stops propagation before
any plugin's command handler runs — no plugin needs its own check.
"""

import logging

from pyrogram import StopPropagation, enums, filters, types

from ghelp import app
from ghelp.database import db
from ghelp.helpers import is_admin_soft, require_admin

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]

# commands that must never be disabled (or the chat locks itself out)
_PROTECTED = {"disable", "enable", "disabled", "disableable", "disabledel",
              "disableadmin", "help", "start", "report"}


def _cmd_of(message: types.Message) -> str:
    """Command name without prefix or @BotName suffix, lowercased."""
    text = message.text or ""
    first = text.split(maxsplit=1)[0] if text else ""
    return first.lstrip("/?!").split("@", 1)[0].lower()


@app.on_message(filters.group & filters.text & filters.regex(r"^[/!?]"), group=-2)
async def _disabled_gate(_, message: types.Message):
    cmd = _cmd_of(message)
    if not cmd:
        return
    cfg = await db.get_chat(message.chat.id)
    if cmd not in cfg["disabled_cmds"]:
        return
    sender = message.from_user
    if sender and await is_admin_soft(message.chat.id, sender.id) and not cfg.get("disabled_admin"):
        return  # admins always bypass (non-destructive gate -> soft cache ok)
    if cfg.get("disabled_del"):
        try:
            await message.delete()
        except Exception:
            pass
    raise StopPropagation


@app.on_message(filters.group & filters.command("disable", prefixes=PRE))
async def disable_cmd(_, message: types.Message):
    if not await require_admin(message, "can_change_info", "Change group info"):
        return
    if len(message.command) < 2:
        await message.reply_text("Usage: <code>/disable &lt;command&gt;</code>",
                                 parse_mode=enums.ParseMode.HTML)
        return
    cmd = message.command[1].strip().lstrip("/?!").split("@", 1)[0].lower()
    if cmd == "all":
        names = sorted(_disableable())
        await db.update_chat(message.chat.id, disabled_cmds=names)
        await message.reply_text(f"Disabled {len(names)} commands.")
        return
    if not cmd or cmd in _PROTECTED:
        await message.reply_text("That command can't be disabled.")
        return
    outcome: dict = {}

    def _apply(cfg):
        if cmd not in cfg["disabled_cmds"]:
            cfg["disabled_cmds"].append(cmd)
            outcome["changed"] = True

    await db.mutate_chat(message.chat.id, _apply)
    if outcome.get("changed"):
        await message.reply_text(f"<code>/{cmd}</code> disabled for non-admins.",
                                 parse_mode=enums.ParseMode.HTML)
    else:
        await message.reply_text("Info: Already disabled.")


@app.on_message(filters.group & filters.command("enable", prefixes=PRE))
async def enable_cmd(_, message: types.Message):
    if not await require_admin(message, "can_change_info", "Change group info"):
        return
    if len(message.command) < 2:
        await message.reply_text("Usage: <code>/enable &lt;command&gt;</code>")
        return
    cmd = message.command[1].strip().lstrip("/?!").split("@", 1)[0].lower()
    outcome: dict = {}

    def _apply(cfg):
        if cmd in cfg["disabled_cmds"]:
            cfg["disabled_cmds"].remove(cmd)
            outcome["changed"] = True

    await db.mutate_chat(message.chat.id, _apply)
    if outcome.get("changed"):
        await message.reply_text(f"<code>/{cmd}</code> enabled again.",
                                 parse_mode=enums.ParseMode.HTML)
    else:
        await message.reply_text("Info: That command was not disabled.")


@app.on_message(filters.group & filters.command("disabled", prefixes=PRE))
async def list_disabled(_, message: types.Message):
    if not await require_admin(message, "can_change_info", "Change group info"):
        return
    cfg = await db.get_chat(message.chat.id)
    cmds = cfg["disabled_cmds"]
    if not cmds:
        await message.reply_text("Info: No commands are disabled here.")
        return
    listing = ", ".join(f"<code>/{c}</code>" for c in cmds)
    await message.reply_text(
        f"Disabled for non-admins: {listing}",
        parse_mode=enums.ParseMode.HTML,
    )


def _disableable() -> set[str]:
    return {"connect", "connection", "captcha", "captchamode", "captcharules",
            "notes", "save", "get", "info", "id", "warn", "report", "rules"}


@app.on_message(filters.group & filters.command("disableable", prefixes=PRE))
async def list_disableable(_, message: types.Message):
    if not await require_admin(message, "can_change_info", "Change group info"):
        return
    await message.reply_text("Disableable commands:\n" + ", ".join(
        f"<code>/{x}</code>" for x in sorted(_disableable())),
        parse_mode=enums.ParseMode.HTML)


async def _toggle(message, key: str, label: str):
    if not await require_admin(message, "can_change_info", "Change group info"):
        return
    arg = message.command[1].lower() if len(message.command) > 1 else ""
    if arg not in {"on", "off", "yes", "no"}:
        await message.reply_text(f"Usage: <code>/{label} on|off</code>",
                                 parse_mode=enums.ParseMode.HTML)
        return
    value = arg in {"on", "yes"}
    await db.update_chat(message.chat.id, **{key: value})
    await message.reply_text(f"{label} is now <b>{'enabled' if value else 'disabled'}</b>.",
                             parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.group & filters.command("disabledel", prefixes=PRE))
async def disabledel(_, message): await _toggle(message, "disabled_del", "disabledel")


@app.on_message(filters.group & filters.command("disableadmin", prefixes=PRE))
async def disableadmin(_, message): await _toggle(message, "disabled_admin", "disableadmin")
