"""Force-subscribe: members must join a channel before they can chat.

/forcesubscribe @channel — gate on (bot must be admin in the channel)
/forcesubscribe off     — gate off
/fsub                   — status

Non-members get their message deleted, are muted, and get a join button +
"Unmute me" verification callback. Approved users (approval.py) are exempt.
"""

import html
import time

from pyrogram import enums, filters, types
from pyrogram.errors import UserNotParticipant

from ghelp import app
from ghelp.approval_trust import is_exempt
from ghelp.selfdel import autodel_reply
from ghelp.database import db
from ghelp.helpers import (
    concise_bot_text, ikb, ikm, is_admin, mention, require_admin, safe_restrict,
)
from ghelp.plugins.admin import _member_permissions, _muted_permissions

PRE = ["/", "?", "!", "."]
_sub_cache: dict[tuple[int, int], tuple[bool, float]] = {}
_prompted: dict[tuple[int, int], float] = {}
_CACHE_TTL = 120
_PROMPT_GAP = 60
_MAX_TRACKED = 500


async def _track(chat_id: int, uid: int) -> None:
    """DB-persist the fact that the CURRENT mute on uid came from force-sub."""
    def _add(cfg):
        tracked = [u for u in (cfg.get("fsub_muted") or []) if u != uid]
        tracked.append(uid)
        cfg["fsub_muted"] = tracked[-_MAX_TRACKED:]

    await db.mutate_chat(chat_id, _add)


async def untrack_fsub(chat_id: int, uid: int) -> None:
    """Drop fsub's claim on a user's restriction.

    Called by every moderation mute/unmute — a later admin action supersedes
    the gate's restriction, so the unmute button must never undo it.
    """
    def _drop(cfg):
        tracked = cfg.get("fsub_muted") or []
        if uid in tracked:
            cfg["fsub_muted"] = [u for u in tracked if u != uid]

    try:
        await db.mutate_chat(chat_id, _drop)
    except Exception:
        pass


@autodel_reply
async def _reply(m: types.Message, text: str):
    return await m.reply_text(
        concise_bot_text(text), parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.group & filters.command(["forcesubscribe", "fsub"], prefixes=PRE))
async def forcesubscribe(_, m: types.Message) -> None:
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    arg = (m.command[1] if len(m.command) > 1 else "").strip()
    cfg = await db.get_chat(m.chat.id)
    if not arg:
        if cfg.get("fsub"):
            title = html.escape(str(cfg.get("fsub_title") or cfg["fsub"]))
            await _reply(
                m,
                f"Force-subscribe is <b>ON</b> → {title}\n"
                "<code>/forcesubscribe off</code> to disable.",
            )
        else:
            await _reply(
                m,
                "Force-subscribe is <b>OFF</b>.\n"
                "<code>/forcesubscribe @channel</code> — members must join "
                "that channel before they can chat.",
            )
        return
    if arg.lower() == "off":
        await db.update_chat(m.chat.id, fsub=0, fsub_link="", fsub_title="")
        await _reply(m, "Force-subscribe disabled.")
        return
    try:
        channel = await app.get_chat(int(arg) if arg.lstrip("-").isdigit() else arg.lstrip("@"))
    except Exception:
        await _reply(m, "Channel not found. Use its @username and add me there first.")
        return
    try:
        me_member = await app.get_chat_member(channel.id, (await app.get_me()).id)
        if me_member.status not in (
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        ):
            raise ValueError
    except Exception:
        await _reply(m, "Make me an <b>admin in that channel</b> first, then retry.")
        return
    link = f"https://t.me/{channel.username}" if channel.username else (channel.invite_link or "")
    if not link:
        try:
            link = await app.export_chat_invite_link(channel.id)
        except Exception:
            link = ""
    await db.update_chat(
        m.chat.id,
        fsub=channel.id,
        fsub_link=str(link),
        fsub_title=str(channel.title or arg),
    )
    await _reply(
        m,
        f"Force-subscribe ON → <b>{html.escape(str(channel.title or arg))}</b>\n"
        "Members must join it before they can chat. Approved users are exempt.",
    )


async def _is_subscribed(channel_id: int, user_id: int) -> bool:
    key = (channel_id, user_id)
    hit = _sub_cache.get(key)
    if hit and time.time() - hit[1] < _CACHE_TTL:
        return hit[0]
    try:
        member = await app.get_chat_member(channel_id, user_id)
        joined = member.status not in (
            enums.ChatMemberStatus.LEFT,
            enums.ChatMemberStatus.BANNED,
        )
    except UserNotParticipant:
        joined = False
    except Exception:
        return True  # API hiccup — fail open, never punish on errors
    if len(_sub_cache) > 5000:
        _sub_cache.clear()
    _sub_cache[key] = (joined, time.time())
    return joined


@app.on_message(filters.group & ~filters.service, group=12)
async def fsub_gate(_, m: types.Message) -> None:
    if not m.from_user or m.from_user.is_self or m.from_user.is_bot:
        return
    cfg = await db.get_chat(m.chat.id)
    channel_id = cfg.get("fsub")
    if not channel_id:
        return
    uid = m.from_user.id
    if is_exempt(cfg, uid, "force_subscribe"):
        return
    if await _is_subscribed(int(channel_id), uid):
        return
    if await is_admin(m.chat.id, uid):  # live check — this gates a mute
        return
    try:
        await m.delete()
    except Exception:
        pass
    m._fsub_enforced = True  # later-group watchers (afk) must skip this one
    if await safe_restrict(m.chat.id, uid, _muted_permissions()):
        await _track(m.chat.id, uid)
    key = (m.chat.id, uid)
    if time.time() - _prompted.get(key, 0) < _PROMPT_GAP:
        return  # already has a prompt message with buttons
    _prompted[key] = time.time()
    if len(_prompted) > 5000:
        _prompted.clear()
    rows = []
    link = str(cfg.get("fsub_link") or "")
    if link:
        rows.append([ikb(
            f"Join {str(cfg.get('fsub_title') or 'channel')[:20]}",
            url=link,
            style=enums.ButtonStyle.PRIMARY,
        )])
    rows.append([ikb(
        "Joined — Unmute Me",
        callback_data=f"fsub:{uid}",
        style=enums.ButtonStyle.SUCCESS,
    )])
    try:
        await m.reply_text(
            f"{mention(uid, m.from_user.first_name or 'user')} — join our "
            "channel first, then tap the button to chat.",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=ikm(rows),
        )
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^fsub:(-?\d+)$"))
async def fsub_unmute(_, q: types.CallbackQuery) -> None:
    uid = int(q.matches[0].group(1))
    clicker = q.from_user.id if q.from_user else 0
    if clicker != uid:
        await q.answer("This button isn't for you.", show_alert=True)
        return
    cfg = await db.get_chat(q.message.chat.id)
    channel_id = cfg.get("fsub")
    if not channel_id:
        await q.answer("Force-subscribe is off now.")
        return
    _sub_cache.pop((int(channel_id), uid), None)  # force a fresh check
    if not await _is_subscribed(int(channel_id), uid):
        await q.answer("You haven't joined the channel yet!", show_alert=True)
        return
    chat_id = q.message.chat.id
    if uid in (cfg.get("fsub_muted") or []):
        # DB-persisted; any moderation mute/unmute since untracks the entry,
        # so lifting here only ever undoes OUR OWN force-sub mute.
        if not await safe_restrict(chat_id, uid, _member_permissions()):
            # keep the record AND the button so the user can retry once the
            # bot's Restrict right is back — never destroy the retry path
            await q.answer(
                "Couldn't unmute right now (missing Restrict right?) — "
                "try again or ask an admin.",
                show_alert=True,
            )
            return
        await untrack_fsub(chat_id, uid)
        await q.answer("Verified — you can chat now!")
    else:
        await q.answer(
            "Verified! (Still muted? An admin muted you — ask them to /unmute.)"
        )
    try:
        await q.message.edit_text("Verified — welcome! You can chat now.")
    except Exception:
        pass
