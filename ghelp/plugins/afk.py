"""AFK mode — auto-reply when someone tags or replies to an away user."""

import html
import time

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.database import db

PRE = ["/", "?", "!", "."]
_KEY = "afk"
_MAX = 2000


def _human(secs: float) -> str:
    secs = max(0, int(secs))
    if secs < 3600:
        return f"{max(1, secs // 60)}m"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"


@app.on_message(filters.command("afk", prefixes=PRE) & (filters.group | filters.private))
async def go_afk(_, m: types.Message) -> None:
    if not m.from_user:
        return
    parts = (m.text or "").split(maxsplit=1)
    reason = parts[1].strip()[:200] if len(parts) > 1 else ""
    afk = dict(await db.get(_KEY, {}) or {})
    if len(afk) >= _MAX and str(m.from_user.id) not in afk:
        await m.reply_text("Error: AFK list is full right now, try later.")
        return
    afk[str(m.from_user.id)] = {
        "reason": reason,
        "since": time.time(),
        "name": m.from_user.first_name or "user",
    }
    await db.set(_KEY, afk)
    await m.reply_text(
        f"{html.escape(m.from_user.first_name or 'You')} — AFK set"
        + (f": <i>{html.escape(reason)}</i>" if reason else "."),
        parse_mode=enums.ParseMode.HTML,
    )


# group 13, not 12: Pyrogram runs only the FIRST matching handler per group,
# so enforcement (fsub_gate, group 12) must never share a group with this
# reply-only watcher or it would silently disable force-subscribe.
@app.on_message(
    filters.group & ~filters.service & ~filters.regex(r"^\s*[/!?]"), group=13
)
async def afk_watcher(_, m: types.Message) -> None:
    if not m.from_user or m.from_user.is_self or m.from_user.is_bot:
        return
    if getattr(m, "_fsub_enforced", False):
        return  # fsub gate (group 12) already deleted/punished this message
    afk = await db.get(_KEY, {}) or {}
    uid = str(m.from_user.id)
    if uid in afk:
        # the AFK user is back — any normal message clears the status
        gone = time.time() - float(afk[uid].get("since", time.time()))
        afk = dict(afk)
        afk.pop(uid, None)
        await db.set(_KEY, afk)
        await m.reply_text(
            f"Welcome back {html.escape(m.from_user.first_name or '')}! "
            f"(away {_human(gone)})",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    targets: set[str] = set()
    if m.reply_to_message and m.reply_to_message.from_user:
        targets.add(str(m.reply_to_message.from_user.id))
    for ent in m.entities or m.caption_entities or []:
        if ent.type == enums.MessageEntityType.TEXT_MENTION and ent.user:
            targets.add(str(ent.user.id))
    for tid in targets:
        hit = afk.get(tid)
        if hit:
            name = html.escape(str(hit.get("name") or "user"))
            line = (
                f"<b>{name}</b> is AFK "
                f"({_human(time.time() - float(hit.get('since', 0)))} ago)"
            )
            if hit.get("reason"):
                line += f":\n<i>{html.escape(str(hit['reason']))}</i>"
            await m.reply_text(line, parse_mode=enums.ParseMode.HTML)
            return
