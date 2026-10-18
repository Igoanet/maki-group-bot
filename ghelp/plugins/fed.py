"""Federations (Rose-style): ban a user once and they're banned in every
group of the federation. Only the fed's owner can fban/unfban; any group
admin can join their group to a fed if they know its id."""

import html
import logging
import uuid

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.selfdel import autodel_reply
from ghelp.database import db
from ghelp.helpers import concise_bot_text, mention, require_admin, resolve_user
from ghelp.plugins.logchan import log_event

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]


@autodel_reply
async def _reply(message: types.Message, text: str, **kwargs):
    return await message.reply_text(
        concise_bot_text(text), parse_mode=enums.ParseMode.HTML, **kwargs)


@app.on_message(filters.group & filters.command("newfed", prefixes=PRE))
async def new_fed(_, message: types.Message):
    if not await require_admin(message, "can_restrict_members", "Ban users"):
        return
    name = " ".join(message.command[1:]).strip() if len(message.command) > 1 else ""
    if not name:
        await _reply(message, "Usage: <code>/newfed &lt;name&gt;</code>")
        return
    fid = uuid.uuid4().hex[:8]
    owner = message.from_user.id

    # detach from any previous fed first — stale membership would let the
    # OLD fed's owner keep enforcing bans here (code-review 2026-09-06)
    cfg = await db.get_chat(message.chat.id)
    old = str(cfg.get("fed_id") or "")
    if old:
        def _detach(f):
            if message.chat.id in f["chats"]:
                f["chats"].remove(message.chat.id)
        await db.mutate_fed(old, _detach)

    def _create(fed):
        fed["name"] = name
        fed["owner"] = owner
        fed["chats"] = [message.chat.id]
        fed["banned"] = []

    await db.mutate_fed(fid, _create)
    await db.update_chat(message.chat.id, fed_id=fid)
    await _reply(
        message,
        f"Federation <b>{html.escape(name)}</b> created and this group joined it.\n"
        f"Fed ID: <code>{fid}</code>\n\n"
        "Other groups: <code>/joinfed " + fid + "</code>\n"
        "Ban everywhere: reply <code>/fban</code> · Undo: <code>/unfban</code>",
    )


@app.on_message(filters.group & filters.command("joinfed", prefixes=PRE))
async def join_fed(_, message: types.Message):
    if not await require_admin(message, "can_restrict_members", "Ban users"):
        return
    if len(message.command) < 2:
        await _reply(message, "Usage: <code>/joinfed &lt;fed id&gt;</code>")
        return
    fid = message.command[1].strip().lower()
    feds = await db.get_feds()
    fed = feds.get(fid)
    if not fed:
        await _reply(message, "No federation with that id.")
        return
    # only the fed OWNER may attach a group — a bare fed id must not let a
    # random group admin hand the fed owner ban-power over that group
    if not message.from_user or message.from_user.id != int(fed.get("owner", 0)):
        await _reply(message, "Only the federation owner can add groups to this fed.")
        return
    # detach from any previous fed first (same stale-membership fix as /newfed)
    cfg = await db.get_chat(message.chat.id)
    old = str(cfg.get("fed_id") or "")
    if old and old != fid:
        def _detach(f):
            if message.chat.id in f["chats"]:
                f["chats"].remove(message.chat.id)
        await db.mutate_fed(old, _detach)
    outcome: dict = {}

    def _join(f):
        if message.chat.id not in f["chats"]:
            f["chats"].append(message.chat.id)
            outcome["changed"] = True

    await db.mutate_fed(fid, _join)
    await db.update_chat(message.chat.id, fed_id=fid)
    await _reply(
        message,
        f"This group joined <b>{html.escape(fed['name'])}</b> "
        f"({len(fed['chats'])} groups).",
    )


@app.on_message(filters.group & filters.command("leavefed", prefixes=PRE))
async def leave_fed(_, message: types.Message):
    if not await require_admin(message, "can_restrict_members", "Ban users"):
        return
    cfg = await db.get_chat(message.chat.id)
    fid = str(cfg.get("fed_id") or "")
    if not fid:
        await _reply(message, "Info: This group is not in any federation.")
        return

    def _leave(f):
        if message.chat.id in f["chats"]:
            f["chats"].remove(message.chat.id)

    await db.mutate_fed(fid, _leave)
    await db.update_chat(message.chat.id, fed_id="")
    await _reply(message, "Done: This group left the federation.")


async def _fed_for_action(message: types.Message):
    """(fed_id, fed) if the chat is in a fed AND the caller owns it."""
    cfg = await db.get_chat(message.chat.id)
    fid = str(cfg.get("fed_id") or "")
    if not fid:
        await _reply(message, "This group is not in a federation. Use /newfed or /joinfed.")
        return None, None
    feds = await db.get_feds()
    fed = feds.get(fid)
    if not fed:
        await _reply(message, "The federation this group points to no longer exists.")
        return None, None
    allowed = {int(fed.get("owner", 0))} | {int(x) for x in fed.get("fadmins", [])}
    if not message.from_user or message.from_user.id not in allowed:
        await _reply(message, "Only the federation owner or fed-admins can do this.")
        return None, None
    return fid, fed


@app.on_message(filters.group & filters.command("fban", prefixes=PRE))
async def fed_ban(_, message: types.Message):
    fid, fed = await _fed_for_action(message)
    if not fid:
        return
    user = await resolve_user(message)
    if not user:
        await _reply(message, "Warning: Reply to a user or give an @username/id.")
        return
    if user.is_bot or user.id == int(fed.get("owner", 0)):
        await _reply(message, "Cannot fed-ban this user.")
        return
    reason = " ".join(
        message.command[1:] if message.reply_to_message else message.command[2:]
    ).strip() or str(fed.get("reason") or "")  # /fedreason default

    def _ban(f):
        if user.id not in f["banned"]:
            f["banned"].append(user.id)

    fed = await db.mutate_fed(fid, _ban)
    done = 0
    for cid in fed["chats"]:
        # defense in depth: only enforce where the group STILL points at this
        # fed — stale list entries must never grant ban power (review 09-06)
        try:
            target_cfg = await db.get_chat(cid)
            if str(target_cfg.get("fed_id") or "") != fid:
                continue
            await app.ban_chat_member(cid, user.id)
            done += 1
        except Exception:
            pass  # bot not admin there / user not there — fine
    who = mention(user.id, html.escape(user.first_name or "user"))
    reason_line = f"\n<b>Reason:</b> {html.escape(reason)}" if reason else ""
    if fed.get("notif", True):  # /fednotif off silences announcements
        await _reply(
            message,
            f"Fed-banned {who} in <b>{done}/{len(fed['chats'])}</b> groups "
            f"of <b>{html.escape(fed['name'])}</b>.{reason_line}",
        )
    await log_event(message.chat.id, f"Fed-ban: {who} ({done} groups){reason_line}")
    fed_log = int(fed.get("log_chat") or 0)  # /setfedlog
    if fed_log:
        try:
            await app.send_message(
                fed_log,
                f"Fed-ban in <b>{html.escape(fed['name'])}</b>: {who} "
                f"({done} groups){reason_line}",
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception as exc:
            logger.info("fed log send: %s", exc)


@app.on_message(filters.group & filters.command("unfban", prefixes=PRE))
async def fed_unban(_, message: types.Message):
    fid, fed = await _fed_for_action(message)
    if not fid:
        return
    user = await resolve_user(message)
    if not user:
        await _reply(message, "Warning: Reply to a user or give an @username/id.")
        return

    def _unban(f):
        if user.id in f["banned"]:
            f["banned"].remove(user.id)

    fed = await db.mutate_fed(fid, _unban)
    done = 0
    for cid in fed["chats"]:
        try:
            target_cfg = await db.get_chat(cid)
            if str(target_cfg.get("fed_id") or "") != fid:
                continue
            await app.unban_chat_member(cid, user.id)
            done += 1
        except Exception:
            pass
    who = mention(user.id, html.escape(user.first_name or "user"))
    await _reply(message, f"Done: Fed-unbanned {who} in <b>{done}</b> groups.")


@app.on_message(filters.group & filters.command("fedinfo", prefixes=PRE))
async def fed_info(_, message: types.Message):
    cfg = await db.get_chat(message.chat.id)
    fid = str(cfg.get("fed_id") or "")
    if not fid:
        await _reply(message, "Info: This group is not in any federation.")
        return
    feds = await db.get_feds()
    fed = feds.get(fid)
    if not fed:
        await _reply(message, "Federation data missing.")
        return
    await _reply(
        message,
        f"<b>{html.escape(fed['name'])}</b>\n"
        f"ID: <code>{fid}</code>\n"
        f"Groups: <b>{len(fed['chats'])}</b>\n"
        f"Fed-banned users: <b>{len(fed['banned'])}</b>",
    )


@app.on_message(filters.new_chat_members, group=6)
async def fed_guard(_, message: types.Message):
    """A fed-banned user joining ANY fed group gets banned on sight."""
    try:
        cfg = await db.get_chat(message.chat.id)
        fid = str(cfg.get("fed_id") or "")
        if not fid:
            return
        feds = await db.get_feds()
        fed = feds.get(fid)
        if not fed:
            return
        banned = set(fed.get("banned", []))
        for sub_id in fed.get("subs", []):  # /subfed — subscribed feds share bans
            sub = feds.get(sub_id)
            if sub:
                banned.update(sub.get("banned", []))
        if not banned:
            return
        for u in message.new_chat_members or []:
            if u.id not in banned:
                continue
            try:
                await app.ban_chat_member(message.chat.id, u.id)
                if fed.get("quiet"):
                    continue  # /quietfed on — ban silently
                who = mention(u.id, html.escape(u.first_name or "user"))
                await app.send_message(
                    message.chat.id,
                    f"{who} is fed-banned in <b>{html.escape(fed['name'])}</b> — removed.",
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception as exc:
                logger.info("fed guard: %s", exc)
    except Exception as exc:
        logger.info("fed guard: %s", exc)
