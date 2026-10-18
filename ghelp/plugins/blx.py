"""Blocklist (Rose naming): /addblocklist /blocklist /rmblocklist
/rmblocklistall /blocklistdelete /setblocklistreason /resetblocklistreason —
aliases & extras over blacklist.py's storage."""

import html
import re
import shlex

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.selfdel import autodel_reply
from ghelp.database import db
from ghelp.helpers import concise_bot_text, parse_duration, require_admin

PRE = ["/", "?", "!", "."]


# tags that make the blocklist watcher restrict the member
_PUNITIVE_ACTIONS = ("warn", "kick", "mute", "ban", "tmute")


def _parse_command(m: types.Message) -> list[tuple[str, dict]]:
    """Parse quoted phrases, comma alternatives, and Rose action tags."""
    raw = (m.text or m.caption or "").split(maxsplit=1)
    args = raw[1] if len(raw) > 1 else ""
    tags = {}
    for tag in re.findall(r"\{([^{}]+)\}", args):
        value = tag.strip().lower()
        if value == "nodel":
            tags["delete"] = False
        elif value in ("del", "warn", "ban", "kick", "mute"):
            tags["action"] = value
        elif value.startswith("tmute"):
            tags["action"] = value
            duration = value[5:].strip()
            if duration:
                tags["duration"] = parse_duration(duration)
        elif value.startswith("reason "):
            tags["reason"] = tag[7:].strip()
    args = re.sub(r"\s*\{[^{}]+\}", "", args).strip()
    reason = ""
    # Rose's final free text is a reply reason; preserve it, but the first
    # token(s) are the trigger. Quotes/brackets make multi-entry syntax clear.
    if args.startswith("(") and ")" in args:
        end = args.index(")")
        keys = [x.strip() for x in args[1:end].split(",") if x.strip()]
        reason = args[end + 1:].strip()
    else:
        try:
            tokens = shlex.split(args)
        except ValueError:
            tokens = args.split()
        keys = [tokens[0]] if tokens else []
        if len(tokens) > 1:
            # A quoted phrase is one token; unquoted forms retain the first
            # word as the trigger and the rest as the authored reason.
            reason = " ".join(tokens[1:])
    return [(key.casefold(), {**tags, "reason": tags.get("reason") or reason})
            for key in keys]


@autodel_reply
async def _reply(m: types.Message, text: str, **kwargs):
    return await m.reply_text(
        concise_bot_text(text), parse_mode=enums.ParseMode.HTML, **kwargs)


@app.on_message(filters.group & filters.command("addblocklist", prefixes=PRE))
async def addblocklist(_, m: types.Message):
    if not await require_admin(m, "can_delete_messages", "Delete messages"):
        return
    parsed = _parse_command(m)
    # An entry without a tag inherits the group's blocklist mode, so the
    # effective action decides whether Ban users is required.
    try:
        mode = str((await db.get_chat(m.chat.id)).get("bl_mode") or "del")
    except Exception:
        mode = "del"
    effective = [str(meta.get("action") or mode) for _, meta in parsed] or [mode]
    if any(action.startswith(_PUNITIVE_ACTIONS) for action in effective):
        if not await require_admin(m, "can_restrict_members", "Ban users"):
            return
    if not parsed and m.reply_to_message:
        reply = m.reply_to_message
        sticker = getattr(reply, "sticker", None)
        pack = getattr(sticker, "set_name", None) if sticker else None
        parsed = [(f"stickerpack:{pack}", {"reason": ""})] if pack else []
    if not parsed:
        await _reply(m, "Usage: <code>/addblocklist &lt;word or phrase&gt;</code>")
        return
    outcome: dict = {}

    def _add(cfg):
        entries = cfg.setdefault("bl_entries", {})
        for phrase, meta in parsed:
            if phrase not in cfg["blacklist"] and len(cfg["blacklist"]) < 200:
                cfg["blacklist"].append(phrase)
                entries[phrase] = meta
                if meta.get("reason"):
                    cfg.setdefault("bl_reasons", {})[phrase] = meta["reason"]
                outcome["added"] = True

    await db.mutate_chat(m.chat.id, _add)
    if outcome.get("added"):
        await _reply(m, "Done: Blocklisted " + ", ".join(
            f"<code>{html.escape(p)}</code>" for p, _ in parsed))
    else:
        await _reply(m, "Info: Already blocklisted (or the 200-entry cap is reached).")


@app.on_message(filters.group & filters.command("rmblocklist", prefixes=PRE))
async def rmblocklist(_, m: types.Message):
    if not await require_admin(m, "can_delete_messages", "Delete messages"):
        return
    phrase = " ".join(m.command[1:]).strip().lower()
    if not phrase:
        await _reply(m, "Usage: <code>/rmblocklist &lt;word&gt;</code>")
        return

    def _rm(cfg):
        if phrase in cfg["blacklist"]:
            cfg["blacklist"].remove(phrase)
        cfg.setdefault("bl_reasons", {}).pop(phrase, None)
        cfg.setdefault("bl_entries", {}).pop(phrase, None)

    await db.mutate_chat(m.chat.id, _rm)
    await _reply(m, f"Done: Removed <code>{html.escape(phrase)}</code> from the blocklist.")


@app.on_message(filters.group & filters.command(["rmblocklistall", "unblocklistall"], prefixes=PRE))
async def rmblocklistall(_, m: types.Message):
    if not await require_admin(m, "can_delete_messages", "Delete messages"):
        return

    def _clear(cfg):
        cfg["blacklist"] = []
        cfg["bl_reasons"] = {}
        cfg["bl_entries"] = {}

    await db.mutate_chat(m.chat.id, _clear)
    await _reply(m, "Done: Blocklist cleared.")


@app.on_message(filters.group & filters.command("blocklist", prefixes=PRE))
async def blocklist_view(_, m: types.Message):
    if not await require_admin(m, "can_delete_messages", "Delete messages"):
        return
    cfg = await db.get_chat(m.chat.id)
    words = cfg["blacklist"]
    if not words:
        await _reply(m, "Info: Blocklist is empty.")
        return
    reason = str(cfg.get("bl_reason") or "")
    reason_line = f"\n<b>Reason:</b> {html.escape(reason)}" if reason else ""
    await _reply(
        m,
        f"<b>Blocklist ({len(words)}):</b> "
        + ", ".join(f"<code>{html.escape(w)}</code>" for w in words[:50])
        + reason_line
        + f"\nMode: <b>{cfg.get('bl_mode', 'del')}</b> · "
          f"Delete: <b>{'on' if cfg.get('bl_del', True) else 'off'}</b>",
    )


@app.on_message(filters.group & filters.command("blocklistdelete", prefixes=PRE))
async def blocklistdelete(_, m: types.Message):
    if not await require_admin(m, "can_delete_messages", "Delete messages"):
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg not in ("on", "off"):
        await _reply(m, "Usage: <code>/blocklistdelete on|off</code> — when off, "
                        "blocklisted messages stay and the user is warned instead.")
        return
    await db.update_chat(m.chat.id, bl_del=(arg == "on"))
    await _reply(m, f"Blocklist message deletion <b>{arg.upper()}</b>.")


@app.on_message(filters.group & filters.command(["setblocklistreason", "resetblocklistreason"], prefixes=PRE))
async def blocklist_reason(_, m: types.Message):
    if not await require_admin(m, "can_delete_messages", "Delete messages"):
        return
    if m.command[0].lower() == "resetblocklistreason":
        await db.update_chat(m.chat.id, bl_reason="")
        await _reply(m, "Done: Blocklist reason cleared.")
        return
    reason = " ".join(m.command[1:]).strip()
    if not reason:
        await _reply(m, "Usage: <code>/setblocklistreason &lt;text&gt;</code>")
        return
    await db.update_chat(m.chat.id, bl_reason=reason)
    await _reply(m, f"Blocklist reason set: <i>{html.escape(reason)}</i>")
