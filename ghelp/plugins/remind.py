"""Reminders — /remindme 10m call the client.

Reminders persist in the DB global store; the on_start loop (launched by the
runner) checks every 20s and delivers due ones, so restarts lose nothing.
"""

import asyncio
import html
import time

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.database import db
from ghelp.helpers import mention, parse_duration

PRE = ["/", "?", "!", "."]
_KEY = "reminders"
_MAX_TOTAL = 500
_MAX_PER_USER = 10
_lock = asyncio.Lock()  # serializes read-modify-write vs the scheduler loop


def _human(secs: float) -> str:
    secs = max(0, int(secs))
    if secs < 3600:
        return f"{max(1, secs // 60)}m"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"


@app.on_message(filters.command("remindme", prefixes=PRE))
async def remindme(_, m: types.Message) -> None:
    if not m.from_user:
        return
    parts = (m.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await m.reply_text(
            "Usage: <code>/remindme 10m drink water</code>",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    secs = parse_duration(parts[1])
    if not secs or not 30 <= secs <= 30 * 86400:
        await m.reply_text(
            "Time must be between 30s and 30d. Example: 45m, 2h, or 1d.",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    async with _lock:
        items = list(await db.get(_KEY, []) or [])
        mine = [r for r in items if r.get("user") == m.from_user.id]
        if len(items) >= _MAX_TOTAL or len(mine) >= _MAX_PER_USER:
            await m.reply_text(
                "Reminder limit reached. Next: delete one with /delreminder."
            )
            return
        items.append({
            "chat": m.chat.id,
            "user": m.from_user.id,
            "name": m.from_user.first_name or "user",
            "text": parts[2][:300],
            "at": time.time() + secs,
        })
        await db.set(_KEY, items)
    await m.reply_text(
        f"Reminder set for <b>{html.escape(parts[1])}</b>. "
        "Next: I'll ping you when it is due.",
        parse_mode=enums.ParseMode.HTML,
    )


@app.on_message(filters.command("reminders", prefixes=PRE))
async def my_reminders(_, m: types.Message) -> None:
    if not m.from_user:
        return
    items = await db.get(_KEY, []) or []
    mine = [(i + 1, r) for i, r in enumerate(items) if r.get("user") == m.from_user.id]
    if not mine:
        await m.reply_text("No reminders set.")
        return
    lines = []
    for idx, r in mine[:10]:
        left = float(r.get("at", 0)) - time.time()
        lines.append(
            f"<b>{idx}.</b> in {_human(left)} — {html.escape(str(r.get('text', ''))[:40])}"
        )
    await m.reply_text(
        "<b>Your reminders</b>\n\n" + "\n".join(lines),
        parse_mode=enums.ParseMode.HTML,
    )


@app.on_message(filters.command("delreminder", prefixes=PRE))
async def del_reminder(_, m: types.Message) -> None:
    if not m.from_user:
        return
    try:
        index = int(m.command[1]) - 1
    except (IndexError, ValueError):
        await m.reply_text(
            "Next: use <code>/delreminder &lt;number from /reminders&gt;</code>.",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    async with _lock:
        items = list(await db.get(_KEY, []) or [])
        if not 0 <= index < len(items) or items[index].get("user") != m.from_user.id:
            await m.reply_text("Reminder not found.")
            return
        items.pop(index)
        await db.set(_KEY, items)
    await m.reply_text("Reminder deleted.")


async def on_start(_app) -> None:
    """Deliver due reminders forever (restart-safe: they live in the DB)."""
    while True:
        await asyncio.sleep(20)
        try:
            async with _lock:
                items = list(await db.get(_KEY, []) or [])
                now = time.time()
                due_all = [r for r in items if float(r.get("at", 0)) <= now]
                if not due_all:
                    continue
                # consume ONLY the batch we are about to send — anything past
                # 20 stays queued for the next cycle instead of being dropped
                batch, later = due_all[:20], due_all[20:]
                keep = [r for r in items if float(r.get("at", 0)) > now] + later
                await db.set(_KEY, keep)  # consume first — never fire twice
            for r in batch:
                try:
                    await app.send_message(
                        int(r["chat"]),
                        f"{mention(int(r['user']), str(r.get('name') or 'user'))} "
                        f"reminder: {html.escape(str(r.get('text') or ''))}",
                        parse_mode=enums.ParseMode.HTML,
                    )
                except Exception:
                    pass
                await asyncio.sleep(0.3)
        except Exception:
            continue
