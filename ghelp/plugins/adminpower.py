"""Owner power panel — which groups the bot is in, and exactly what it can do.

Bots cannot call get_dialogs (Telegram refuses: BOT_METHOD_INVALID), so the
group list is enumerated through the ASSISTANT user account, which can.  For
each group the panel shows the bot's own admin rights, a X/8 score, and the
rights it is missing — so the owner can see where the bot is powerless.
"""

import html
import json
import pathlib

from pyrogram import Client, enums, filters, types

from ghelp import app
from ghelp.config import config
from ghelp.database import db

_PRE = ["/", "!", "?", "."]

# right flag -> short label
RIGHTS = [
    ("can_delete_messages", "Delete msgs"),
    ("can_restrict_members", "Ban users"),
    ("can_invite_users", "Invite users"),
    ("can_pin_messages", "Pin"),
    ("can_change_info", "Change info"),
    ("can_promote_members", "Promote"),
    ("can_manage_video_chats", "Video chats"),
    ("can_manage_topics", "Topics"),
]
_TOTAL = len(RIGHTS)

# where the assistant's user session lives (music bot owns it)
_SESSION_FILES = (
    "/root/bobby_music/data/sessions.json",
    "/root/group_help/data/assistant_sessions.json",
)


def _assistant_session() -> tuple:
    """(user_id, session_string) for the VC assistant, or (None, None)."""
    for path in _SESSION_FILES:
        try:
            raw = json.loads(pathlib.Path(path).read_text())
        except Exception:
            continue
        # {uid: "string"} or {uid: {"session": "string", ...}}
        for uid, val in raw.items():
            if isinstance(val, str) and len(val) > 60:
                return int(uid), val
            if isinstance(val, dict):
                for key in ("session", "session_string", "string", "value"):
                    s = val.get(key)
                    if isinstance(s, str) and len(s) > 60:
                        return int(uid), s
    return None, None


async def _scan() -> tuple:
    """(rows, error).  rows = list of dicts, one per group the bot is in."""
    uid, sess = _assistant_session()
    if not sess:
        return [], "assistant session not found"

    asst = Client("power_scan", api_id=config.API_ID, api_hash=config.API_HASH,
                  session_string=sess, in_memory=True)
    await asst.start()
    rows = []
    try:
        # a bot token's numeric prefix IS the bot's user id — no API
        # call needed, so this works outside the running client too.
        bot_id = int(config.BOT_TOKEN.split(":")[0])
        async for d in asst.get_dialogs():
            ch = d.chat
            if ch.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
                continue
            row = {"id": ch.id, "title": ch.title or str(ch.id),
                   "status": "not a member", "admin": False,
                   "have": [], "missing": [lbl for _, lbl in RIGHTS]}
            try:
                mem = await app.get_chat_member(ch.id, bot_id)
                st = getattr(mem.status, "value", str(mem.status))
                row["status"] = st
                pr = getattr(mem, "privileges", None)
                have = [lbl for flag, lbl in RIGHTS if pr and getattr(pr, flag, False)]
                row["have"] = have
                row["missing"] = [lbl for _, lbl in RIGHTS if lbl not in have]
                row["admin"] = st in ("administrator", "creator", "owner")
            except Exception as exc:
                row["status"] = "unreachable (%s)" % type(exc).__name__
            rows.append(row)
    finally:
        try:
            await asst.stop()
        except Exception:
            pass
    return rows, None


def _render(rows) -> str:
    admins = [r for r in rows if r["admin"]]
    others = [r for r in rows if not r["admin"]]
    out = [f"<b>Bot power across {len(rows)} groups</b>",
           f"Admin in <b>{len(admins)}</b> · not admin in <b>{len(others)}</b>", ""]
    if admins:
        out.append("<b>— ADMIN —</b>")
        for i, r in enumerate(sorted(admins, key=lambda x: -len(x["have"])), 1):
            miss = ", ".join(r["missing"]) if r["missing"] else "none — full power"
            out.append(f"{i}. <b>{html.escape(r['title'])}</b>\n"
                       f"    <code>{r['id']}</code> · <b>{len(r['have'])}/{_TOTAL}</b>\n"
                       f"    has: {', '.join(r['have']) or '—'}\n"
                       f"    missing: {miss}")
    if others:
        out.append("\n<b>— NOT ADMIN —</b>")
        for i, r in enumerate(sorted(others, key=lambda x: x["title"]), 1):
            out.append(f"{i}. {html.escape(r['title'])} · <code>{r['id']}</code> "
                       f"· {html.escape(r['status'])}")
    return "\n".join(out)


async def _save(rows):
    """Persist every group so /groups and /massban can see them too."""
    recs = {}
    for r in rows:
        recs[str(r["id"])] = {"title": r["title"], "admin": r["admin"],
                              "status": r["status"],
                              "can_ban": "Ban users" in r["have"]}
    await db.set("known_chats", recs)
    return len(recs)


def _kb():
    return types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton("Refresh", callback_data="ap:scan")
    ]])


@app.on_message(filters.command(["power", "adminpower", "mygroups"], prefixes=_PRE))
async def power_cmd(_, m: types.Message):
    if not m.from_user or m.from_user.id != config.OWNER_ID:
        return
    if m.chat and m.chat.type != enums.ChatType.PRIVATE:
        try:
            await m.delete()
        except Exception:
            pass
    note = await app.send_message(m.from_user.id, "Scanning groups via the assistant…")
    rows, err = await _scan()
    if err:
        return await note.edit_text(f"Error: <code>{html.escape(err)}</code>")
    if not rows:
        return await note.edit_text("No groups found for the assistant account.")
    n = await _save(rows)
    await note.edit_text(_render(rows) + f"\n\n<i>{n} groups saved.</i>",
                         parse_mode=enums.ParseMode.HTML, reply_markup=_kb())


@app.on_callback_query(filters.regex(r"^ap:scan$"))
async def power_cb(_, q: types.CallbackQuery):
    if not q.from_user or q.from_user.id != config.OWNER_ID:
        return await q.answer("Owner only.", show_alert=True)
    await q.answer("Scanning…")
    rows, err = await _scan()
    if err:
        return await q.message.edit_text(f"Error: <code>{html.escape(err)}</code>")
    await _save(rows)
    await q.message.edit_text(_render(rows), parse_mode=enums.ParseMode.HTML,
                              reply_markup=_kb())
