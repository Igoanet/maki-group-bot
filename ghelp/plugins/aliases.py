# Copyright (c) 2026 Maki
"""Command aliases and typos that people actually type.

Registered as their OWN names, so nothing is shadowed.  Anything that maps
onto a real command is dispatched through the decoration-time registry, which
is the only reliable way to reach a handler (owner 2026-10-18).
"""

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.plugins.basic import HELP_SECTIONS

PRE = ["/", "?", "!", "."]

# typed name -> the command it should run
ALIASES = {
    "muter": "mute",
    "mut": "mute",
    "bann": "ban",
    "kik": "kick",
    "edior": "editor",
    "editer": "editor",
    "edit": "editor",
    "editfre": "editfree",
    "pormote": "promote",
    "fullpromte": "fullpromote",
    "unbann": "unban",
    "warnn": "warn",
    "purgee": "purge",
}


async def _run_alias(message: types.Message, real: str) -> bool:
    """Re-dispatch this message as `real`, keeping the sender's identity."""
    try:
        from ghelp import find_command_handlers
        cands = find_command_handlers(real)
    except Exception:
        return False
    if not cands:
        return False
    body = (message.text or message.caption or "").strip()
    head, _, tail = body[1:].partition(" ") if body[:1] in PRE else ("", "", "")
    args = tail.split() if tail else []
    message.command = [real] + args
    for _g, cb in cands:
        try:
            await cb(app, message)
            return True
        except Exception:
            continue
    return False


@app.on_message(filters.group & filters.command(sorted(ALIASES), prefixes=PRE))
async def alias_cmd(_, message: types.Message) -> None:
    typed = (message.command[0].lower() if getattr(message, "command", None)
             else "")
    real = ALIASES.get(typed)
    if not real:
        return
    if await _run_alias(message, real):
        return
    try:
        await message.reply_text(
            f"<code>/{typed}</code> maps to <code>/{real}</code>, but that "
            "command isn\'t available on this bot.",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        pass


@app.on_message(filters.group & filters.command(["mod"], prefixes=PRE))
async def mod_cmd(_, message: types.Message) -> None:
    """`/mod` — jump straight to the Moderation commands."""
    sec = HELP_SECTIONS.get("admin")
    if not sec:
        return
    lines = [f"<b>{sec['button']}</b> \u2014 <i>{sec['title']}</i>", "",
             sec.get("intro", "")]
    for usage, desc in sec["cmds"]:
        lines.append(f"\u2022 <code>{usage}</code> \u2014 {desc}")
    if sec.get("note"):
        lines += ["", f"<i>{sec['note']}</i>"]
    try:
        await message.reply_text(
            "\n".join(lines), parse_mode=enums.ParseMode.HTML,
            link_preview_options=types.LinkPreviewOptions(is_disabled=True)
            if hasattr(types, "LinkPreviewOptions") else None,
        )
    except Exception:
        await message.reply_text("\n".join(lines[:20]),
                                 parse_mode=enums.ParseMode.HTML)
