"""Rose-style group utility commands: admin list, invite link, pinned msg,
unpin-all, group meta (title/description/photo), zombie cleanup, @admin
reports."""

import html
import logging
import re
import secrets
from collections import OrderedDict
import time

from pyrogram import enums, filters, types
try:
    from pyrogram import StopPropagation
except Exception:  # older/newer builds that do not export it
    class StopPropagation(Exception):
        pass

from ghelp import app
from ghelp.selfdel import autodel_reply
from ghelp.database import db
from ghelp.config import config
from ghelp.helpers import (
    concise_bot_text, fill, ikb, ikm, mention, parse_buttons, require_admin,
    require_right,
)
from ghelp.plugins.logchan import log_event
import asyncio
import os

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]


async def _bot_can_pin(chat_id: int) -> bool | None:
    """Return whether this bot can pin, or None when it cannot be checked.

    Checking the actor's right is not sufficient: Telegram applies the bot's
    own administrator rights when the pin RPC is made.  Keep lookup failures
    distinct from a known missing right so callers can give an honest answer
    rather than claiming that an operation succeeded.
    """
    try:
        member = await app.get_chat_member(chat_id, "me")
    except Exception as exc:
        logger.info("pin: bot permission lookup failed: %s", exc)
        return None
    if getattr(member, "status", None) == enums.ChatMemberStatus.OWNER:
        return True
    if getattr(member, "status", None) != enums.ChatMemberStatus.ADMINISTRATOR:
        return False
    return bool(getattr(getattr(member, "privileges", None),
                        "can_pin_messages", False))


@autodel_reply
async def _reply(message: types.Message, text: str, **kwargs):
    return await message.reply_text(
        concise_bot_text(text), parse_mode=enums.ParseMode.HTML, **kwargs)


@app.on_message(filters.group & filters.command("adminlist", prefixes=PRE))
async def admin_list(_, message: types.Message):
    try:
        owners, admins = [], []
        async for mem in app.get_chat_members(
            message.chat.id, filter=enums.ChatMembersFilter.ADMINISTRATORS
        ):
            u = mem.user
            if not u or u.is_bot:
                continue
            line = mention(u.id, html.escape(u.first_name or "admin"))
            if mem.status == enums.ChatMemberStatus.OWNER:
                owners.append(f"Owner: {line}")
            else:
                admins.append(f"Admin: {line}")
        lines = owners + admins
        if not lines:
            await _reply(message, "Error: could not fetch the admin list.")
            return
        await _reply(message, "<b>Admins here:</b>\n" + "\n".join(lines[:50]))
    except Exception as exc:
        logger.info("adminlist: %s", exc)
        await _reply(message, "Error: could not fetch the admin list.")


@app.on_message(filters.group & filters.command("pinned", prefixes=PRE))
async def pinned_message(_, message: types.Message):
    try:
        chat = await app.get_chat(message.chat.id)
        pm = getattr(chat, "pinned_message", None)
        if pm and getattr(pm, "link", None):
            await _reply(message, f"Pinned message:\n{pm.link}")
        elif pm:
            await _reply(message, "There is a pinned message (scroll up).")
        else:
            await _reply(message, "Info: nothing is pinned here.")
    except Exception as exc:
        logger.info("pinned: %s", exc)
        await _reply(message, "Error: could not fetch the pinned message.")


@app.on_message(filters.group & filters.command("unpinall", prefixes=PRE))
async def unpin_all(_, message: types.Message):
    if not await require_right(message, "can_pin_messages", "Pin messages"):
        return
    bot_can_pin = await _bot_can_pin(message.chat.id)
    if bot_can_pin is None:
        await _reply(message, "Error: could not verify the bot's Pin Messages right.")
        return
    if not bot_can_pin:
        await _reply(message, "Error: the bot needs the Pin Messages right.")
        return
    try:
        result = await app.unpin_all_chat_messages(message.chat.id)
        if result is False:
            await _reply(message, "Error: could not unpin (Pin Messages failed).")
            return
        await _reply(message, "Success: all pinned messages unpinned.")
    except Exception as exc:
        logger.info("unpinall: %s", exc)
        await _reply(message, "Error: could not unpin (need 'Pin Messages' right).")


@app.on_message(filters.group & filters.command("permapin", prefixes=PRE))
async def permanent_pin(_, message: types.Message):
    """Send and pin a bot-authored message, retaining its formatting/buttons."""
    if not await require_right(message, "can_pin_messages", "Pin messages"):
        return
    bot_can_pin = await _bot_can_pin(message.chat.id)
    if bot_can_pin is not True:
        await _reply(message, "Error: the bot needs the Pin Messages right.")
        return
    raw = (message.text or message.caption or "").split(maxsplit=1)
    text = raw[1].strip() if len(raw) > 1 else ""
    if not text and message.reply_to_message:
        text = message.reply_to_message.text or message.reply_to_message.caption or ""
    if not text:
        await _reply(message, "Usage: <code>/permapin text</code>.")
        return
    try:
        body, markup = parse_buttons(fill(text, message))
        sent = await message.reply_text(
            body, parse_mode=enums.ParseMode.HTML, reply_markup=markup
        )
        result = await app.pin_chat_message(
            message.chat.id, sent.id, disable_notification=True
        )
        if result is False:
            await _reply(message, "Error: message sent but could not be pinned.")
            return
        await _reply(message, "Done: custom message pinned.")
    except Exception as exc:
        logger.info("permapin: %s", exc)
        await _reply(message, "Error: could not create or pin that message.")


@app.on_message(filters.group & filters.command("antichannelpin", prefixes=PRE))
async def antichannel_pin(_, message: types.Message):
    if not await require_right(message, "can_pin_messages", "Pin messages"):
        return
    arg = message.command[1].lower() if len(message.command) > 1 else ""
    if arg not in ("yes", "no", "on", "off"):
        cfg = await db.get_chat(message.chat.id)
        state = "ON" if cfg.get("antichannelpin") else "OFF"
        await _reply(message, f"Anti-channel pin: <b>{state}</b>.")
        return
    await db.update_chat(message.chat.id, antichannelpin=arg in ("yes", "on"))
    await _reply(message, f"Anti-channel pin <b>{arg.upper()}</b>.")


@app.on_message(filters.group & filters.command("cleanlinked", prefixes=PRE))
async def clean_linked(_, message: types.Message):
    if not await require_right(message, "can_delete_messages", "Delete messages"):
        return
    arg = message.command[1].lower() if len(message.command) > 1 else ""
    if arg not in ("yes", "no", "on", "off"):
        cfg = await db.get_chat(message.chat.id)
        state = "ON" if cfg.get("cleanlinked") else "OFF"
        await _reply(message, f"Clean linked channel: <b>{state}</b>.")
        return
    await db.update_chat(message.chat.id, cleanlinked=arg in ("yes", "on"))
    await _reply(message, f"Clean linked channel <b>{arg.upper()}</b>.")


@app.on_message(filters.group & ~filters.service, group=15)
async def linked_channel_cleanup(_, message: types.Message):
    cfg = await db.get_chat(message.chat.id)
    sender_chat = getattr(message, "sender_chat", None)
    if not sender_chat or not getattr(message, "is_automatic_forward", False):
        return
    if cfg.get("cleanlinked"):
        try:
            await message.delete()
        except Exception:
            pass


@app.on_message(filters.group & filters.command("settitle", prefixes=PRE))
async def set_title(_, message: types.Message):
    if not await require_right(message, "can_change_info", "Change group info"):
        return
    title = " ".join(message.command[1:]).strip() if len(message.command) > 1 else ""
    if not title:
        await _reply(message, "Usage: <code>/settitle &lt;new title&gt;</code>")
        return
    try:
        await app.set_chat_title(message.chat.id, title)
        await _reply(message, f"Success: group title set to <b>{html.escape(title)}</b>.")
    except Exception as exc:
        logger.info("settitle: %s", exc)
        await _reply(message, "Error: could not set the title (need 'Change group info' right).")


@app.on_message(filters.group & filters.command("setdescription", prefixes=PRE))
async def set_description(_, message: types.Message):
    if not await require_right(message, "can_change_info", "Change group info"):
        return
    text = " ".join(message.command[1:]).strip() if len(message.command) > 1 else ""
    if not text:
        await _reply(message, "Usage: <code>/setdescription &lt;text&gt;</code>")
        return
    try:
        await app.set_chat_description(message.chat.id, text)
        await _reply(message, "Success: group description updated.")
    except Exception as exc:
        logger.info("setdescription: %s", exc)
        await _reply(message, "Error: could not set the description (need 'Change group info' right).")


@app.on_message(filters.group & filters.command("setgpic", prefixes=PRE))
async def set_group_photo(_, message: types.Message):
    if not await require_right(message, "can_change_info", "Change group info"):
        return
    rt = message.reply_to_message
    if not rt or not rt.photo:
        await _reply(message, "Usage: reply to a photo to set it as the group picture.")
        return
    try:
        # in_memory: the photo is streamed straight to Telegram and never
        # written to the VPS disk (2026-09-19).
        photo = await app.download_media(rt, in_memory=True)
        if photo is None:
            raise RuntimeError("download failed")
        photo.name = "grouppic.jpg"
        await app.set_chat_photo(message.chat.id, photo=photo)
        await _reply(message, "Success: group photo updated.")
    except Exception as exc:
        logger.info("setgpic: %s", exc)
        await _reply(message, "Error: could not set the photo (need 'Change group info' right).")


@app.on_message(filters.group & filters.command("zombies", prefixes=PRE))
async def zombies(_, message: types.Message):
    """/zombies -> count deleted accounts; /zombies clean -> remove them."""
    if not await require_admin(message, "can_restrict_members", "Ban users"):
        return
    clean = len(message.command) > 1 and message.command[1].lower() == "clean"
    status = await _reply(message, "Scanning members…")
    verified, suspects, removed = [], [], 0
    try:
        async for mem in app.get_chat_members(message.chat.id):
            u = mem.user
            if not u:
                continue
            # code-review 2026-09-06: a blank first name is NOT proof of a
            # deleted account — only the explicit flag may trigger a ban;
            # blank-name profiles are reported for manual review instead
            if getattr(u, "is_deleted", False):
                verified.append(u)
            elif not (u.first_name or "").strip():
                suspects.append(u)
        if clean:
            for u in verified:
                try:
                    await app.ban_chat_member(message.chat.id, u.id)
                    removed += 1
                except Exception:
                    pass
        if clean:
            text = (f"Removed <b>{removed}</b> confirmed deleted accounts.\n"
                    f"({len(suspects)} blank-name profiles were <i>not</i> "
                    "touched — check them manually.)")
        else:
            text = (f"Confirmed deleted accounts: <b>{len(verified)}</b>\n"
                    f"Suspicious blank-name profiles: <b>{len(suspects)}</b> "
                    "(manual review)\n"
                    "Use <code>/zombies clean</code> to remove confirmed ones.")
        await status.edit_text(text, parse_mode=enums.ParseMode.HTML)
    except Exception as exc:
        logger.info("zombies: %s", exc)
        await status.edit_text("Error: scan failed (too many members / rights missing?).")


# standalone token only — 'user@admin.com' or '@adminX' must not page admins
_ADMIN_TAG = re.compile(r"(?<![\w.])@admin(?![\w.])", re.I)
_TAG_COOLDOWN = 60  # seconds between @admin pages per chat
_last_tag: dict[int, float] = {}

# (dm_chat_id, dm_message_id) -> where that report came from, so an admin's
# reply in DM can be routed back into the group (2026-09-30).
_report_relay: "OrderedDict[tuple[int, int], dict]" = OrderedDict()
_REPORT_RELAY_MAX = 500


def _relay_remember(dm_chat_id: int, dm_message_id: int, **info) -> None:
    key = (dm_chat_id, dm_message_id)
    _report_relay[key] = info
    _report_relay.move_to_end(key)
    while len(_report_relay) > _REPORT_RELAY_MAX:
        _report_relay.popitem(last=False)


# -- visible report tags (owner 2026-10-17) --------------------------------
TAG_CHUNK = 45           # Telegram caps ~50 text mentions per message
REPORT_TAG_EVERYONE = (
    (os.getenv("REPORT_TAG_EVERYONE", "true") or "true").strip().lower()
    in ("1", "true", "yes", "on")
)
REPORT_TAG_CAP = int(os.getenv("REPORT_TAG_CAP", "150"))


def _tag(user) -> str:
    """A visible, clickable mention of one account."""
    name = (getattr(user, "first_name", None) or "user").strip() or "user"
    return ('<a href="tg://user?id=%s">%s</a>'
            % (getattr(user, "id", 0), html.escape(name)))


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]




async def _notify_admins(message: types.Message, reason: str = "") -> None:
    """Report flow (owner 2026-10-17, revised 2026-10-18).

    In the group: the reporter's name, "Report sent" in italic, and then
    EVERYONE in the group tagged -- admins included -- so the whole group is
    notified, not just the admin team.

    In the DM: an ATTENTION card carrying the quoted message and ONE button
    (Solved).  The old "Reply to the group" button was removed on request.
    """
    try:
        cfg = await db.get_chat(message.chat.id)
        if not cfg.get("reports", True):
            await _reply(message, "Reports are turned off in this group.")
            return
        sender = message.from_user
        rt = getattr(message, "reply_to_message", None)
        target = getattr(rt, "from_user", None)
        if (
            target
            and not target.is_bot
            and await _is_group_admin(message.chat.id, target.id)
        ):
            await _reply(message, "You can't report an admin.")
            return

        admin_users: list = []
        async for mem in app.get_chat_members(
            message.chat.id, filter=enums.ChatMembersFilter.ADMINISTRATORS
        ):
            u = mem.user
            if u and not u.is_bot and not getattr(mem, "is_anonymous", False):
                admin_users.append(u)
        admin_ids = [u.id for u in admin_users]
        if not admin_ids:
            await _reply(message, "No admins found to notify.")
            return

        anonymous_report = (
            sender is None and getattr(message, "sender_chat", None) is not None
        )
        verified_uid = anon_verified_admin(message.chat.id) if anonymous_report else None
        if sender:
            reporter = mention(sender.id, sender.first_name or "user")
        elif verified_uid:
            reporter = mention(verified_uid, "Anonymous admin (verified)")
        elif anonymous_report:
            reporter = "Anonymous admin (unverified)"
        else:
            reporter = "Someone"

        # ---- group notice: ONE clean line, no wall of names --------------
        # (owner 2026-10-18) The group should read only "Report sent".  The
        # admins still get a notification: their mentions are ZERO-WIDTH, so
        # Telegram pings each of them while the message itself stays a single
        # short line with nothing to scroll past.
        hidden = "".join(
            '<a href="tg://user?id=%s">\u200b</a>' % u.id
            for u in admin_users[:50]          # Telegram caps ~50 per message
        )
        confirm = await _reply(
            message,
            "\u2705 <b>Report sent</b>" + hidden,
        )
        await log_event(message.chat.id, f"Report by {reporter}")

        # ---- DM card -------------------------------------------------------
        group_name = html.escape(message.chat.title or str(message.chat.id))
        link = getattr(rt, "link", None) or getattr(message, "link", None)
        quoted = (rt.text or rt.caption or "").strip() if rt is not None else ""
        if not quoted and rt is not None and rt.media:
            quoted = "[" + str(rt.media.value) + "]"
        lines = [
            "\u26a0\ufe0f <b>ATTENTION!</b>",
            f"{reporter} has required an admin action in the group",
            f"\u201c<b>{group_name}</b>\u201d",
        ]
        if target is not None and (sender is None or target.id != sender.id):
            lines.append(f"Reported: {mention(target.id, target.first_name or 'user')}")
        if reason:
            lines.append(f"Reason: {html.escape(reason)}")
        if quoted:
            lines.append("<b>Message:</b>")
            lines.append(f"<blockquote>{html.escape(quoted[:400])}</blockquote>")
        if link:
            lines.append(f'\U0001f449 <a href="{link}">Go to message</a>')
        dm_text = "\n".join(lines)
        markup = ikm([[ikb("\u2705 Solved", f"rsv:{message.chat.id}")]])

        targets = set(admin_ids)
        if sender:
            targets.discard(sender.id)
        sent = 0
        queued: list[int] = []
        for uid in targets:
            try:
                sent_msg = await app.send_message(
                    uid, dm_text, parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=True, reply_markup=markup,
                )
                _relay_remember(
                    uid, sent_msg.id, chat_id=message.chat.id,
                    message_id=(rt.id if rt is not None else message.id),
                    reporter_id=(sender.id if sender else 0),
                )
                if rt is not None:
                    await _forward_reported(uid, message.chat.id, rt.id)
                sent += 1
            except Exception as exc:
                queued.append(uid)
                logger.info("report DM to %s queued: %s", uid, type(exc).__name__)
        if queued:
            await _queue_reports(queued, dm_text, message.chat.id,
                                 (rt.id if rt is not None else message.id))
        logger.info(
            "report in %s: %d admins tagged, DM %d/%d, queued %d",
            message.chat.id, len(admin_ids), sent, len(targets), len(queued),
        )
    except Exception as exc:
        logger.warning("report failed: %s: %s", type(exc).__name__, exc)
        try:
            await _reply(message, "Could not send the report. Please try again.")
        except Exception:
            pass


async def _forward_reported(uid: int, chat_id: int, message_id: int) -> None:
    """Put the ACTUAL reported message in the admin's DM (owner 2026-10-05):
    the alert must carry the message itself, not only a text snippet."""
    try:
        await app.forward_messages(uid, chat_id, message_id)
        return
    except Exception:
        pass
    try:
        await app.copy_message(uid, chat_id, message_id)
    except Exception as exc:
        logger.info("report forward skipped: %s", type(exc).__name__)


_PENDING_KEY = "pending_reports"
_PENDING_MAX = 10          # per admin
_PENDING_TTL = 3 * 86400   # seconds


async def _queue_reports(uids, dm_text: str, chat_id: int, message_id: int) -> None:
    now = time.time()

    def add(store):
        for uid in uids:
            items = [i for i in store.get(str(uid), []) if now - i.get("t", 0) < _PENDING_TTL]
            items.append({"t": now, "text": dm_text, "chat_id": chat_id, "message_id": message_id})
            store[str(uid)] = items[-_PENDING_MAX:]
        return True

    try:
        await db.mutate(_PENDING_KEY, add, {})
    except Exception as exc:
        logger.info("report queue failed: %s", type(exc).__name__)


async def _take_reports(uid: int) -> list:
    taken: list = []

    def pop(store):
        items = store.pop(str(uid), None)
        if not items:
            return False
        taken.extend(items)
        return True

    try:
        await db.mutate(_PENDING_KEY, pop, {})
    except Exception as exc:
        logger.info("report queue read failed: %s", type(exc).__name__)
    now = time.time()
    return [i for i in taken if now - i.get("t", 0) < _PENDING_TTL]


@app.on_message(filters.private & filters.command("start", prefixes=PRE), group=-5)
async def deliver_pending_reports(_, m: types.Message):
    """Runs alongside the normal /start (different handler group)."""
    if not m.from_user:
        return
    items = await _take_reports(m.from_user.id)
    payload = m.command[1] if len(m.command) > 1 else ""
    if payload == "reports" or items:
        try:
            await m.reply_text(
                "\U0001f514 <b>Report DMs are on.</b>\n"
                "When someone writes <code>@admin</code> in your group, the "
                "report comes here."
                + (f"\n\n{len(items)} report(s) were waiting for you:" if items else ""),
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            pass
    for it in items:
        try:
            if not await _is_group_admin(it["chat_id"], m.from_user.id):
                continue  # no longer an admin of that group
            sent_msg = await app.send_message(
                m.from_user.id, it["text"], parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=ikm([[ikb("\u21a9\ufe0f Reply to the group",
                                       f"rrep:{it['chat_id']}")]]),
            )
            _relay_remember(m.from_user.id, sent_msg.id, chat_id=it["chat_id"],
                            message_id=it.get("message_id"), reporter_id=0)
        except Exception as exc:
            logger.info("pending report delivery failed: %s", type(exc).__name__)


async def _is_group_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await app.get_chat_member(chat_id, user_id)
        return member.status in (
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        )
    except Exception:
        return False


@app.on_message(filters.group & filters.command(["report", "admin"], prefixes=PRE))
async def report_cmd(_, message: types.Message):
    reason = " ".join(message.command[1:]).strip() if len(message.command) > 1 else ""
    now = time.time()
    if now - _last_tag.get(message.chat.id, 0) < _TAG_COOLDOWN:
        await _reply(message, "\u2705 Admins were already alerted a moment ago.")
        return
    _last_tag[message.chat.id] = now
    await _notify_admins(message, reason)


@app.on_message(filters.group & filters.command("reports", prefixes=PRE))
async def reports_toggle(_, message: types.Message):
    if not await require_admin(message, "can_change_info", "Change group info"):
        return
    arg = message.command[1].lower() if len(message.command) > 1 else ""
    if arg not in {"on", "off", "yes", "no"}:
        cfg = await db.get_chat(message.chat.id)
        await _reply(message, f"User reports are <b>{'ON' if cfg.get('reports', True) else 'OFF'}</b>.")
        return
    enabled = arg in {"on", "yes"}
    await db.update_chat(message.chat.id, reports=enabled)
    await _reply(message, f"User reports <b>{'enabled' if enabled else 'disabled'}</b>.")


@app.on_message(filters.group & filters.text & filters.regex(_ADMIN_TAG), group=8)
async def report_tag(_, message: types.Message):
    # per-chat cooldown — a spammer must not mass-page the admin team
    if (message.text or "").lstrip()[:1] in ("/", "!", "?"):
        return  # a command (e.g. /report @admin) is handled by report_cmd
    if not (await db.get_chat(message.chat.id)).get("reports", True):
        return
    now = time.time()
    if now - _last_tag.get(message.chat.id, 0) < _TAG_COOLDOWN:
        await _reply(message, "\u2705 Admins were already alerted a moment ago.")
        return
    _last_tag[message.chat.id] = now
    text = message.text or ""
    reason = _ADMIN_TAG.sub("", text).strip()
    await _notify_admins(message, reason[:200])


# ── admin replies in DM are posted back into the group ─────────────────────
# pending "next message goes to the group" per admin, set by the button
_relay_armed: dict[int, tuple[int, int]] = {}


@app.on_callback_query(filters.regex(r"^rrep:(-?\d+)$") & filters.private)
async def report_relay_arm(_, q: types.CallbackQuery):
    """The Reply button arms the admin's next DM message for the group."""
    if not q.from_user:
        return
    cid = int(q.data.split(":")[1])
    if not (await _is_group_admin(cid, q.from_user.id)
            or q.from_user.id == config.OWNER_ID):
        return await q.answer("Only admins of that group can reply.", show_alert=True)
    info = _report_relay.get((q.message.chat.id, q.message.id)) or {}
    _relay_armed[q.from_user.id] = (cid, info.get("message_id") or 0)
    await q.answer("Send your reply now — it goes to the group.", show_alert=True)


@app.on_message(filters.private & ~filters.service, group=-4)
async def report_relay(_, m: types.Message):
    """Post an admin's DM back into the group.

    Triggered either by replying to the bot's report message, or by pressing
    the Reply button and sending the next message.
    """
    if not m.from_user:
        return
    uid = m.from_user.id

    target = None
    rt = getattr(m, "reply_to_message", None)
    if rt is not None:
        target = _report_relay.get((m.chat.id, rt.id))
    if target is None and uid in _relay_armed:
        cid, mid = _relay_armed[uid]
        target = {"chat_id": cid, "message_id": mid}
    if target is None:
        return

    cid = int(target["chat_id"])
    if not (await _is_group_admin(cid, uid) or uid == config.OWNER_ID):
        return await m.reply_text(
            "Only admins of that group can reply there.",
            parse_mode=enums.ParseMode.HTML,
        )

    text = (m.text or m.caption or "").strip()
    who = m.from_user.first_name or "admin"
    body = f"\U0001f464 <b>Admin reply — {html.escape(who)}</b>\n\n{html.escape(text)}"
    reply_to = target.get("message_id") or None
    try:
        if text:
            await app.send_message(
                cid, body, parse_mode=enums.ParseMode.HTML,
                reply_to_message_id=reply_to,
            )
        else:
            # media: relay it with the same header
            await app.copy_message(
                cid, m.chat.id, m.id,
                caption=body,
                parse_mode=enums.ParseMode.HTML,
                reply_to_message_id=reply_to,
            )
    except Exception as exc:
        return await m.reply_text(
            f"Could not post that: <code>{type(exc).__name__}</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    _relay_armed.pop(uid, None)
    try:
        await m.reply_text("✅ Sent to the group.")
    except Exception:
        pass


# ── anonymous-admin identity verification ──────────────────────────────────
# An anonymous admin posts with sender_chat == the group and no from_user, so
# Maki cannot tell who they are.  Whenever such a sender speaks, Maki offers a
# one-tap verification: the admin taps from their personal account and the bot
# records who is behind the anonymous mask for that chat (owner 2026-10-05).
_ANON_KEY = "anon_verified"
_ANON_TTL = 7 * 86400
_ANON_PROMPT_FAST = 60      # cooldown when the message is a command / report
_ANON_PROMPT_SLOW = 600     # cooldown for any other anonymous message
_ANON_PENDING_TTL = 600     # how long a Verify button stays valid
_anon_last_prompt: dict[int, float] = {}
_anon_prompt_msg: dict[int, int] = {}  # chat -> verify card id
_anon_pending: dict[str, tuple[int, float]] = {}
_anon_verified: dict[int, dict] = {}


_TG_FAKE_SENDER = 777000   # Telegram's service account id


def _is_anonymous_sender(message: types.Message) -> bool:
    """True for Telegram's anonymous group-admin identity.

    A linked channel post has ``sender_chat.id != chat.id`` and is NOT an
    anonymous admin.  For a genuine anonymous admin Telegram reports the
    group in ``sender_chat``; depending on the API version and client it may
    report the sender either as **no user at all** or as its **fake service
    user 777000**.  Both forms must be recognised, or the gate silently
    treats a hidden admin as an ordinary member (owner 2026-10-18).
    """
    sender_chat = getattr(message, "sender_chat", None)
    chat = getattr(message, "chat", None)
    if sender_chat is None or chat is None:
        return False
    if getattr(sender_chat, "id", None) != getattr(chat, "id", None):
        return False                       # linked channel post
    from_user = getattr(message, "from_user", None)
    if from_user is None:
        return True                        # classic anonymous admin
    try:
        return int(getattr(from_user, "id", 0) or 0) == _TG_FAKE_SENDER
    except (TypeError, ValueError):
        return False


def _anon_fingerprint(message: types.Message) -> str:
    """A one-line dump of everything Telegram told us about this message.

    Ground truth for diagnosing anonymous sends: the real field values decide
    which branch of the gate applies, and guessing them has cost this project
    several wrong fixes (owner 2026-10-18).
    """
    fu = getattr(message, "from_user", None)
    sc = getattr(message, "sender_chat", None)
    ch = getattr(message, "chat", None)
    return ("from_user=%s(id=%s) sender_chat=%s(id=%s) chat=%s(id=%s) "
            "is_ephemeral=%s receiver_user=%s") % (
        type(fu).__name__ if fu is not None else None,
        getattr(fu, "id", None),
        type(sc).__name__ if sc is not None else None,
        getattr(sc, "id", None),
        type(ch).__name__ if ch is not None else None,
        getattr(ch, "id", None),
        getattr(message, "is_ephemeral", None),
        getattr(getattr(message, "receiver_user", None), "id", None),
    )


def anon_verified_admin(chat_id: int) -> int | None:
    """The user id that proved it is the anonymous admin here, or None."""
    entry = _anon_verified.get(int(chat_id))
    if entry and time.time() - entry.get("t", 0) < _ANON_TTL:
        return entry.get("uid")
    return None


async def _anon_store(chat_id: int, user_id: int, name: str) -> None:
    entry = {"uid": int(user_id), "name": name, "t": time.time()}
    _anon_verified[int(chat_id)] = entry

    def add(store):
        store[str(chat_id)] = entry
        return True

    try:
        await db.mutate(_ANON_KEY, add, {})
    except Exception as exc:
        logger.info("anon verify store failed: %s", type(exc).__name__)


# Commands that are plainly not admin "power": a member-side or informational
# command.  Anything else a hidden admin sends is treated as using admin power.
# Only these are exempt; EVERYTHING else an anonymous admin types must be
# verified first (owner 2026-10-18).
_HARMLESS_ANON_CMDS = {
    "help", "start", "id", "ping", "alive", "source", "lang", "language",
    "report", "admin", "reports",
}


def _is_anon_power_use(message: types.Message) -> bool:
    """True when a hidden admin ran ANY command and is not yet identified.

    The owner wants verification for every command, not a chosen subset
    (owner 2026-10-18).  Only trivially safe informational commands are
    exempt, because they change nothing and hiding them would just be
    annoying."""
    body = (message.text or message.caption or "").strip()
    if body[:1] not in ("/", "!", "?"):
        return False
    head = (body[1:].split(maxsplit=1)[0] if len(body) > 1 else "")
    head = head.split("@", 1)[0].strip().lower()
    if not head or head.isdigit():
        return False
    return head not in _HARMLESS_ANON_CMDS


_prompted_msgs: dict = {}      # (chat_id, message_id) -> when prompted


async def anon_gate_prompt(message) -> None:
    """Ask an anonymous admin to verify, ONCE per message.

    rateguard wraps every message handler, so the same update reaches this
    many times; without dedupe the group would get one card per wrapper.
    Called from rateguard because that wrapper is the one place guaranteed to
    run for every update (owner 2026-10-18).
    """
    try:
        chat_id = message.chat.id
        key = (chat_id, getattr(message, "id", 0))
        now = time.time()
        for k, seen in list(_prompted_msgs.items()):
            if now - seen > 300:
                _prompted_msgs.pop(k, None)
        if key in _prompted_msgs:
            return                       # already asked for this message
        _prompted_msgs[key] = now
        await _send_anon_verify(chat_id, message, fast=True, pending=message)
    except Exception as exc:
        logger.warning("anon gate prompt failed: %s: %s",
                       type(exc).__name__, exc)


def anon_should_block(message) -> bool:
    """True when this message must NOT be processed yet: an anonymous admin is
    using a command and has not been identified.

    Exposed for rateguard, which wraps EVERY message handler.  Relying on
    StopPropagation alone left a path where a downstream handler still ran
    (owner 2026-10-18); this lets the wrapper drop the update as well.
    """
    try:
        if getattr(message, "_anon_verified", False):
            return False
        if not _is_anonymous_sender(message):
            return False
        if not _is_anon_power_use(message):
            return False
        # NO chat-level bypass here (owner 2026-10-18): a previous verification
        # used to switch the gate off for the rest of the process, so every
        # later command ran without asking.  Verification is now PER COMMAND -
        # only the exact message we replayed carries the marker.
        return True
    except Exception:
        return False


async def send_ephemeral(chat_id: int, text: str, user_id: int,
                         markup=None) -> bool:
    """Send a group message only ONE member can see (Bot API 10.2).

    This is Telegram's own "private reply inside a group": the message lives
    in the chat but nobody else can read it.  It is the correct home for the
    verification card, which the owner does not want on screen for everyone
    (owner 2026-10-18).  Returns False when Telegram refuses, so the caller
    falls back instead of losing the prompt.
    """
    if not user_id:
        return False
    try:
        params = types.EphemeralMessageParameters(receiver_user_id=int(user_id))
    except Exception as exc:  # noqa: BLE001
        logger.info("ephemeral unavailable: %s", type(exc).__name__)
        return False
    try:
        await app.send_message(
            chat_id, text, parse_mode=enums.ParseMode.HTML,
            reply_markup=markup, ephemeral_message_parameters=params,
        )
        logger.info("ephemeral card sent to %s in %s", user_id, chat_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("ephemeral refused (%s) - will use a fallback",
                    type(exc).__name__)
        return False


async def _send_anon_verify(chat_id: int, message: types.Message,
                            fast: bool = False, pending=None) -> None:
    """Ask a hidden admin to prove their identity — IN THE GROUP.

    The card belongs in the group, as a reply to the command (owner
    2026-10-18).  An anonymous admin carries no user id, so a private card
    cannot be addressed to them; the group reply is the one place the person
    who typed the command is actually looking.

    When Telegram DOES tell us who sent it (an ephemeral command, or the
    fake-sender form), the same card is sent as an **ephemeral** message: it
    still sits in the group, but only that member can read it, so nobody else
    sees the prompt.
    """
    now = time.time()
    for stale, item in list(_anon_pending.items()):
        if now - item[1] > _ANON_PENDING_TTL:
            _anon_pending.pop(stale, None)
    nonce = secrets.token_hex(10)
    _anon_pending[nonce] = (chat_id, now, pending)
    name = _cmd_name(message) if pending is not None else ""
    what = f"<code>/{html.escape(name)}</code>" if name else "a command"
    text = ("\U0001f575\ufe0f <b>Verify first.</b>\n"
            f"Tap below from your own account to run {what}.")
    markup = ikm([[
        ikb("\u2705 Verify", f"gh:av:{nonce}",
            style=enums.ButtonStyle.SUCCESS)
    ]])

    # Ephemeral only when the sender is actually known: same group, private
    # to that member, so the rest of the chat sees nothing.
    receiver = 0
    _fu = getattr(message, "from_user", None)
    if _fu is not None and int(getattr(_fu, "id", 0) or 0) != _TG_FAKE_SENDER:
        receiver = int(_fu.id)
    elif getattr(message, "receiver_user", None) is not None:
        receiver = int(getattr(message.receiver_user, "id", 0) or 0)
    if receiver and await send_ephemeral(chat_id, text, receiver, markup):
        _anon_last_prompt[chat_id] = now
        return

    # ---- the group card: one reply, reused rather than repeated ----------
    # Always a FRESH card (owner 2026-10-18): editing the old one meant the
    # prompt could pass unnoticed, and a card whose button was already used
    # cannot be re-used.  The previous card is removed first so the group
    # never collects a stack of them.
    prev = _anon_prompt_msg.pop(chat_id, None)
    if prev:
        try:
            await app.delete_messages(chat_id, prev)
        except Exception:
            pass
    try:
        sent = await message.reply_text(
            text, parse_mode=enums.ParseMode.HTML, reply_markup=markup,
        )
        _anon_prompt_msg[chat_id] = getattr(sent, "id", None)
        _anon_last_prompt[chat_id] = now
        logger.info("anon verify: card posted in %s for /%s", chat_id, name or "?")
        asyncio.create_task(
            _expire_anon_prompt(chat_id, getattr(sent, "id", 0), after=600))
    except Exception as exc:
        logger.warning("anon prompt failed: %s: %s", type(exc).__name__, exc)




def _cmd_name(message: types.Message) -> str:
    body = (message.text or message.caption or "").strip()
    head = (body[1:].split(maxsplit=1)[0] if len(body) > 1 else "")
    return head.split("@", 1)[0].strip().lower()


_SKIP_ANON_CB = {"anonymous_admin_gate", "anonymous_admin_prompt"}


def _impersonate(message: types.Message, user) -> bool:
    """Give the withheld message the verified admin's identity.

    Every command decorator (language, admin checks, mention rendering) reads
    ``from_user``.  An anonymous message carries ``from_user = None``, so the
    re-dispatched command died inside the first decorator: the admin saw
    "Verified" while the command silently did nothing (owner 2026-10-17).
    """
    try:
        message.from_user = user
        message.sender_chat = None
    except Exception as exc:
        logger.warning("anon impersonate failed: %s", type(exc).__name__)
        return False
    # The replayed command needs its ARGUMENTS.  Pyrogram derives ``command``
    # at parse time and leaves it None for an anonymous admin message, so
    # handlers doing ``m.command[1]`` crashed with 'NoneType' not
    # subscriptable -- which surfaced as "Could not promote" (owner
    # 2026-10-18).  Rebuilt from the message text, which is always present.
    try:
        _had = getattr(message, "command", None)
        if _had:
            logger.warning("anon impersonate: command already present %r",
                           list(_had)[:4])
        if not _had:
            body = (message.text or message.caption or "").strip()
            if body[:1] in ("/", "!", "?", "."):
                parts = body[1:].split()
                if parts:
                    head = parts[0].split("@", 1)[0].strip()
                    if head:
                        message.command = [head] + parts[1:]
                        logger.warning("anon impersonate: command restored as %r",
                                       message.command[:4])
    except Exception as exc:
        logger.warning("anon impersonate: command rebuild failed: %s",
                       type(exc).__name__)
    try:
        message._anon_verified = True
    except Exception:
        pass
    return getattr(message, "from_user", None) is not None


def _filter_commands(flt) -> set:
    """Every command name a handler's filter tree matches.

    Kurigram builds a tree -- ``filters.group & filters.command(["ban"])``
    becomes AndFilter(base=<group filter>, other=<CommandFilter>) -- and its
    nodes are a mix of plain functions and callable filter objects, so
    nothing may be skipped for being callable; the walk is bounded by a
    counter instead.  The command filter can sit on either side.
    """
    out, stack, guard = set(), [flt], 0
    while stack and guard < 500:
        guard += 1
        node = stack.pop()
        if node is None or isinstance(node, (str, bytes, int, float, bool)):
            continue
        if isinstance(node, (list, tuple, set, frozenset, dict)):
            try:
                stack.extend(list(node))
            except TypeError:
                pass
            continue
        cmds = getattr(node, "commands", None)
        if cmds:
            try:
                for c in cmds:
                    out.add(str(c).lower())
            except TypeError:
                out.add(str(cmds).lower())
        for attr in ("base", "other", "left", "right", "filters"):
            stack.append(getattr(node, attr, None))
    return out


async def _redispatch(chat_id: int, message: types.Message, user) -> bool:
    """Run the withheld command as the verified admin.

    Selection, most precise first:
      1. a handler that DECLARES this command name  -> run it;
      2. if none declares it, a handler whose filter ACCEPTS the message and
         which declares at least one command (never a catch-all, which
         declares nothing).
    The command name comes from the message's own text, so a declaring
    handler is by definition the right one.  Re-testing through
    ``h.check()`` was what originally let a catch-all win and leave the work
    undone (2026-10-18).

    The replay carries the verified admin's identity, so the handler's own
    permission checks evaluate that admin's REAL position and rights in the
    group -- verifying identity is what makes those checks meaningful.
    """
    if message is None:
        logger.warning("anon redispatch: nothing to replay")
        return False
    if not _impersonate(message, user):
        logger.warning("anon redispatch: could not set the verified identity")
        return False
    want = _cmd_name(message)
    if not want:
        logger.warning("anon redispatch: no command in %r", (message.text or "")[:40])
        return False
    # ---- 1. registry (decoration time) - the reliable path ----------------
    try:
        from ghelp import find_command_handlers
        cands = find_command_handlers(want)
        logger.info("anon redispatch: /%s -> registry has %d handler(s)",
                    want, len(cands))
        for gnum, cb in cands:
            name = getattr(cb, "__name__", "?")
            try:
                await cb(app, message)
            except StopPropagation:
                raise
            except Exception as exc:
                logger.warning("anon redispatch: /%s via %s raised %s",
                               want, name, type(exc).__name__)
                logger.exception("anon redispatch detail for /%s", want)
                continue
            logger.info("anon redispatch: /%s ran via %s (registry, group %s)",
                        want, name, gnum)
            return True
        if cands:
            logger.warning("anon redispatch: %d handler(s) for /%s all raised",
                           len(cands), want)
            return False
    except Exception as exc:
        logger.warning("anon redispatch: registry lookup failed: %s",
                       type(exc).__name__)

    try:
        groups = sorted(app.dispatcher.groups.keys())
    except Exception as exc:
        logger.warning("anon redispatch: no dispatcher: %s", type(exc).__name__)
        return False

    declared, accepting = [], []
    for g in groups:
        for h in list(app.dispatcher.groups.get(g) or []):
            cb = getattr(h, "callback", None)
            name = getattr(cb, "__name__", "?")
            if not callable(cb) or name in _SKIP_ANON_CB:
                continue                       # never re-enter the gate itself
            cmds = _filter_commands(getattr(h, "filters", None))
            if not cmds:
                continue                       # catch-all: never a command
            if want in cmds:
                declared.append((g, h, cb, name))
                continue
            try:
                if await h.check(app, message):
                    accepting.append((g, h, cb, name))
            except Exception:
                continue

    logger.info("anon redispatch: /%s -> %d declaring, %d accepting handler(s)",
                want, len(declared), len(accepting))

    for bucket in (declared, accepting):
        for g, h, cb, name in bucket:
            try:
                await cb(app, message)
            except StopPropagation:
                raise
            except Exception as exc:
                logger.warning("anon redispatch: /%s via %s raised %s",
                               want, name, type(exc).__name__)
                continue                       # try the next candidate
            logger.info("anon redispatch: /%s ran via %s in %s", want, name, chat_id)
            return True

    logger.warning("anon redispatch: nothing ran /%s in %s (declared=%d accepting=%d)",
                   want, chat_id, len(declared), len(accepting))
    return False

async def anonymous_admin_gate(_, message: types.Message):
    """Verify BEFORE acting: a hidden admin's command is held back and only
    runs once they prove their identity.  Ordinary chatter and reports pass."""
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    try:
        if getattr(message, "_anon_verified", False):
            logger.info("anon gate[%s]: already verified - passing", chat_id)
            return          # our own re-dispatch after a successful verify
        if not _is_anonymous_sender(message):
            return          # ordinary member: nothing to do
        if not _is_anon_power_use(message):
            logger.info("anon gate[%s]: anonymous but not a power use - passing",
                        chat_id)
            return
        logger.warning("anon gate[%s]: anonymous power use /%s - BLOCKING the "
                       "command and asking for verification",
                       chat_id, _cmd_name(message))
        await _send_anon_verify(chat_id, message, fast=True, pending=message)
    except Exception as exc:
        logger.warning("anon gate[%s]: %s: %s", chat_id, type(exc).__name__, exc)
    raise StopPropagation


@app.on_callback_query(filters.regex(r"^gh:av:([0-9a-f]+)$"))
async def anonymous_admin_verify(_, q: types.CallbackQuery):
    """Identity proven -> run the withheld command with the REAL rights.

    Order matters (owner 2026-10-18): verify the caller's live position FIRST,
    run the command, and only then clean up the card.  A crash between those
    steps used to leave the admin verified with nothing done -- which is
    exactly the "work isn't done" symptom.
    """
    nonce = q.data.split(":", 2)[2]
    item = _anon_pending.get(nonce)
    if not item or time.time() - item[1] > _ANON_PENDING_TTL:
        return await q.answer(
            "This verification expired. Send the command again.",
            show_alert=True,
        )
    user = q.from_user
    if user is None:
        return await q.answer("I couldn't read your account.", show_alert=True)
    chat_id = item[0]

    # ---- 1. live position and power check -------------------------------
    try:
        member = await app.get_chat_member(chat_id, user.id)
        status = getattr(member, "status", None)
        is_admin = status in (
            enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER,
        )
    except Exception as exc:
        logger.warning("anon verify: member lookup failed: %s",
                       type(exc).__name__)
        is_admin = False
    if not is_admin:
        return await q.answer(
            "You are not an admin in this group, so the command stays blocked.",
            show_alert=True,
        )

    name = user.first_name or "admin"
    # Stored under the user's own key so the *identity* survives for report
    # labels.  It is NOT used to bypass the gate any more (see above).
    await _anon_store(chat_id, user.id, name)
    logger.warning("anon verify: %s verified in %s - running /%s "
                   "(the next command will ask again)",
                   user.id, chat_id, _cmd_name(item[2]) if len(item) > 2 and item[2] else "?")
    _anon_pending.pop(nonce, None)
    _anon_prompt_msg.pop(chat_id, None)

    # ---- 2. run the command the anonymous admin asked for ---------------
    pending = item[2] if len(item) > 2 else None
    cmd = _cmd_name(pending) if pending is not None else ""
    ran = False
    if pending is not None:
        try:
            ran = await _redispatch(chat_id, pending, user)
        except Exception as exc:
            logger.warning("anon verify: /%s dispatch failed: %s",
                           cmd, type(exc).__name__)
            ran = False

    # ---- 3. take the prompt card down (the command's own reply stands) ---
    try:
        await q.message.delete()
    except Exception:
        try:
            await q.message.edit_reply_markup(None)
        except Exception:
            pass

    # ---- 4. SCREEN POP-UP, not a chat message --------------------------
    if not ran and cmd:
        try:
            from ghelp import COMMAND_HANDLERS
            known = cmd.lower() in COMMAND_HANDLERS
        except Exception:
            known = True
        if not known:
            await q.answer(
                f"\u26a0\ufe0f /{cmd} is not a command of this bot.",
                show_alert=True,
            )
            logger.warning("anon verify: /%s is not registered in this bot", cmd)
            return
    if ran:
        await q.answer(f"\u2705 Verified as {name} \u2014 /{cmd} ran.",
                       show_alert=True)
    else:
        await q.answer(
            f"\u2705 Verified as {name}.\nSend /{cmd} again to run it.",
            show_alert=True,
        )
        try:
            await app.send_message(
                chat_id,
                "\u2705 Verified: " + mention(user.id, html.escape(name))
                + f"\nSend <code>/{html.escape(cmd)}</code> again.",
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            pass
    logger.info("anon verify: %s verified in %s | /%s ran=%s",
                user.id, chat_id, cmd or "?", ran)


@app.on_callback_query(filters.regex(r"^rsv:(-?[0-9]+)$"))
async def report_solved(_, q: types.CallbackQuery):
    """Mark a report handled: the alert is rewritten, buttons removed.

    Anyone in that group's admin team (or the owner) may close it; a random
    member tapping the button gets a quiet refusal (owner 2026-10-17).
    """
    try:
        chat_id = int(q.data.split(":", 1)[1])
    except (TypeError, ValueError):
        return await q.answer("Invalid action.", show_alert=True)
    user = q.from_user
    owner = int(getattr(config, "OWNER_ID", 0) or 0)
    allowed = bool(user) and user.id == owner
    if not allowed and user is not None:
        try:
            allowed = await _is_group_admin(chat_id, user.id)
        except Exception:
            allowed = False
    if not allowed:
        return await q.answer("Only an admin of that group can close this.",
                              show_alert=True)
    name = (user.first_name if user else "admin") or "admin"
    try:
        await q.message.edit_text(
            "\u2705 <b>Solved</b>\n"
            f"Handled by {mention(user.id, html.escape(name))}.",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        pass
    await q.answer("Marked solved")


async def _expire_anon_prompt(chat_id: int, message_id: int,
                              after: int = 90) -> None:
    """Delete an unanswered verification card after `after` seconds.

    An anonymous admin often walks away from the prompt; leaving it in the
    group forever is exactly the on-screen clutter the owner objected to
    (2026-10-18).  A verified or replaced card is left alone.
    """
    try:
        await asyncio.sleep(after)
    except asyncio.CancelledError:
        return
    if not message_id or _anon_prompt_msg.get(chat_id) != message_id:
        return                      # already verified or replaced
    try:
        await app.delete_messages(chat_id, message_id)
        if _anon_prompt_msg.get(chat_id) == message_id:
            _anon_prompt_msg.pop(chat_id, None)
        logger.info("anon prompt %s expired in %s", message_id, chat_id)
    except Exception as exc:
        logger.info("anon prompt expiry failed: %s", type(exc).__name__)
