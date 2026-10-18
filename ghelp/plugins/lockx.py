"""Lock extras (Rose parity): /locktypes, /lockwarns, url /allowlist."""

import html

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.selfdel import autodel_reply
from ghelp.database import db
from ghelp.helpers import concise_bot_text, require_admin
from ghelp.plugins.locks import LOCK_TYPES

PRE = ["/", "?", "!", "."]


@autodel_reply
async def _reply(m: types.Message, text: str, **kwargs):
    return await m.reply_text(
        concise_bot_text(text), parse_mode=enums.ParseMode.HTML, **kwargs)


@app.on_message(filters.group & filters.command("locktypes", prefixes=PRE))
async def locktypes(_, m: types.Message):
    await _reply(
        m,
        "<b>Available lock types:</b>\n"
        + "\n".join(f"• <code>{t}</code>" for t in LOCK_TYPES)
        + "\n\nUse <code>/lock &lt;type&gt;</code> / <code>/unlock &lt;type&gt;</code>.",
    )


@app.on_message(filters.group & filters.command("lockwarns", prefixes=PRE))
async def lockwarns(_, m: types.Message):
    if not await require_admin(m, "can_delete_messages", "Delete messages"):
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg in ("yes", "true"):
        arg = "on"
    elif arg in ("no", "false"):
        arg = "off"
    if arg not in ("on", "off"):
        cfg = await db.get_chat(m.chat.id)
        state = "ON" if cfg.get("lockwarns", True) else "OFF"
        await _reply(m, f"Lock notices are <b>{state}</b>.\n"
                        "<b>Usage:</b> <code>/lockwarns on|off</code>")
        return
    await db.update_chat(m.chat.id, lockwarns=(arg == "on"))
    await _reply(m, f"Lock notices <b>{arg.upper()}</b>.")


@app.on_message(filters.group & filters.command("allowlist", prefixes=PRE))
async def allowlist(_, m: types.Message):
    if not await require_admin(m, "can_delete_messages", "Delete messages"):
        return
    if len(m.command) < 2:
        cfg = await db.get_chat(m.chat.id)
        items = cfg.get("allowlist") or []
        if not items:
            await _reply(m, "Info: Allowlist is empty. Add domains with "
                            "<code>/allowlist example.com</code> — these stay "
                            "postable even when the url lock is on.")
            return
        await _reply(m, "<b>Allowlisted domains:</b>\n"
                        + "\n".join(f"• <code>{html.escape(d)}</code>" for d in items))
        return
    domains = []
    for value in m.command[1:]:
        domain = value.strip().lower().removeprefix("http://").removeprefix(
            "https://").split("/")[0]
        if domain:
            domains.append(domain)
    if not domains or any(" " in domain for domain in domains):
        await _reply(m, "Give a domain, e.g. <code>/allowlist youtube.com</code>")
        return
    outcome: dict = {}

    def _add(cfg):
        al = cfg["allowlist"]
        for domain in domains:
            if domain not in al and len(al) < 50:
                al.append(domain)
                outcome["added"] = True

    await db.mutate_chat(m.chat.id, _add)
    if outcome.get("added"):
        await _reply(m, f"<code>{html.escape(', '.join(domains))}</code> allowlisted.")
    else:
        await _reply(m, "Info: Already allowlisted (or the 50-domain cap is reached).")


@app.on_message(filters.group & filters.command("rmallowlist", prefixes=PRE))
async def rmallowlist(_, m: types.Message):
    if not await require_admin(m, "can_delete_messages", "Delete messages"):
        return
    if len(m.command) < 2:
        await _reply(m, "Usage: <code>/rmallowlist &lt;domain&gt;</code>")
        return
    domains = [value.strip().lower() for value in m.command[1:] if value.strip()]

    def _rm(cfg):
        for domain in domains:
            if domain in cfg["allowlist"]:
                cfg["allowlist"].remove(domain)

    await db.mutate_chat(m.chat.id, _rm)
    await _reply(m, f"<code>{html.escape(', '.join(domains))}</code> removed from the allowlist.")


@app.on_message(filters.group & filters.command("rmallowlistall", prefixes=PRE))
async def rmallowlistall(_, m: types.Message):
    if not await require_admin(m, "can_delete_messages", "Delete messages"):
        return

    def _clear(cfg):
        cfg["allowlist"] = []

    await db.mutate_chat(m.chat.id, _clear)
    await _reply(m, "Allowlist cleared.")
