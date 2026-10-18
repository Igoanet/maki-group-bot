"""Auto-Post: scheduled repeating announcements per chat (text only).

The scheduler starts lazily on the first bot activity (plugins import before
the event loop runs, so a task cannot be created at import time).
"""

import asyncio
import html
import logging
import time
import uuid

from pyrogram import enums, filters, types
from pyrogram.errors import FloodWait

from ghelp import app
from ghelp.database import db
from ghelp.helpers import parse_duration, require_admin

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]
_TICK = 30            # scheduler granularity, seconds
_MIN_IVL = 60         # 1 minute
_MAX_IVL = 7 * 86400  # 1 week

_task: asyncio.Task | None = None


def _ensure_task() -> None:
    global _task
    if _task is not None and not _task.done():
        try:
            # a task bound to a dead/different loop never fires — rebind
            if _task.get_loop() is asyncio.get_running_loop():
                return
        except RuntimeError:
            return
    _task = asyncio.create_task(_scheduler())


async def on_start(_app) -> None:
    """Called by the runner right after app.start() — the scheduler must run
    even for groups with zero traffic (their saved posts are still due)."""
    _ensure_task()


async def _scheduler() -> None:
    while True:
        await asyncio.sleep(_TICK)
        try:
            await _tick()
        except Exception as exc:
            logger.info("autopost tick: %s", exc)


async def _tick() -> None:
    chats = await db.all_chats()
    now = time.time()
    for cid_s, stored in chats.items():
        posts = stored.get("autoposts") or []
        due = [p for p in posts if float(p.get("next", 0)) <= now]
        if not due:
            continue
        cid = int(cid_s)
        for p in due:
            pid = p.get("id")
            try:
                await app.send_message(cid, p.get("text", ""),
                                       disable_web_page_preview=True)
            except FloodWait as fw:
                await asyncio.sleep(min(int(fw.value), 30))  # retry next tick
                continue
            except Exception as exc:
                # bot removed / no rights — drop the post instead of looping
                logger.info("autopost %s dropped in %s: %s", pid, cid, exc)

                def _drop(cfg, _pid=pid):
                    cfg["autoposts"] = [q for q in cfg["autoposts"]
                                        if q.get("id") != _pid]

                await db.mutate_chat(cid, _drop)
                continue

            def _bump(cfg, _pid=pid, _now=now):
                for q in cfg["autoposts"]:
                    if q.get("id") == _pid:
                        q["next"] = _now + int(q.get("interval", 3600))

            await db.mutate_chat(cid, _bump)


@app.on_message(filters.group & filters.command("addpost", prefixes=PRE))
async def add_post(_, message: types.Message):
    if not await require_admin(message, "can_change_info", "Change group info"):
        return
    if len(message.command) < 2:
        await message.reply_text(
            "Usage: <code>/addpost 2h &lt;text&gt;</code> — or reply to a message with <code>/addpost 2h</code>.\n"
            "Interval: 1m to 7d.",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    interval = parse_duration(message.command[1])
    if not interval or not _MIN_IVL <= interval <= _MAX_IVL:
        await message.reply_text("Error: interval must be between 1m and 7d (e.g. 30m, 2h, 1d).")
        return
    text = " ".join(message.command[2:]).strip()
    if not text and message.reply_to_message:
        text = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
    if not text:
        await message.reply_text("Error: give me the announcement text (or reply to a message).")
        return
    pid = uuid.uuid4().hex[:6]
    post = {"id": pid, "text": text, "interval": interval,
            "next": time.time() + interval}
    outcome: dict = {}

    def _add(cfg):
        # cap INSIDE the atomic mutation — concurrent /addpost calls can't
        # race past it (code-review 2026-09-06)
        if len(cfg["autoposts"]) >= 10:
            outcome["full"] = True
            return
        cfg["autoposts"].append(post)
        outcome["changed"] = True

    await db.mutate_chat(message.chat.id, _add)
    if outcome.get("full"):
        await message.reply_text(
            "Error: max <b>10</b> auto-posts per group — delete one first with /delpost.",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    _ensure_task()
    await message.reply_text(
        f"Auto-post scheduled every <b>{html.escape(message.command[1])}</b> "
        f"(id <code>{pid}</code>). First post goes out in one interval.",
        parse_mode=enums.ParseMode.HTML,
    )


@app.on_message(filters.group & filters.command("delpost", prefixes=PRE))
async def del_post(_, message: types.Message):
    if not await require_admin(message, "can_change_info", "Change group info"):
        return
    if len(message.command) < 2:
        await message.reply_text("Usage: <code>/delpost &lt;id&gt;</code> (see /posts).")
        return
    pid = message.command[1].strip()
    outcome: dict = {}

    def _del(cfg):
        before = len(cfg["autoposts"])
        cfg["autoposts"] = [q for q in cfg["autoposts"] if q.get("id") != pid]
        outcome["changed"] = len(cfg["autoposts"]) != before

    await db.mutate_chat(message.chat.id, _del)
    if outcome.get("changed"):
        await message.reply_text(f"Success: auto-post <code>{html.escape(pid)}</code> deleted.",
                                 parse_mode=enums.ParseMode.HTML)
    else:
        await message.reply_text("Info: no auto-post with that id.")


@app.on_message(filters.group & filters.command("posts", prefixes=PRE))
async def list_posts(_, message: types.Message):
    if not await require_admin(message, "can_change_info", "Change group info"):
        return
    cfg = await db.get_chat(message.chat.id)
    posts = cfg["autoposts"]
    if not posts:
        await message.reply_text("Info: no auto-posts scheduled here.")
        return
    lines = []
    now = time.time()
    for p in posts:
        mins = max(0, int((float(p.get("next", 0)) - now) / 60))
        preview = html.escape(str(p.get("text", ""))[:40])
        lines.append(
            f"• <code>{p.get('id')}</code> — every {int(p.get('interval', 0)) // 60}m, "
            f"next in ~{mins}m: {preview}…"
        )
    await message.reply_text("<b>Auto-posts:</b>\n" + "\n".join(lines[:20]),
                             parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.group & ~filters.service, group=11)
async def _wakeup(_, message: types.Message):
    """Lazy-start the scheduler on any group activity."""
    _ensure_task()
