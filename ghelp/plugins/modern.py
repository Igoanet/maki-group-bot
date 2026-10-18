"""Modern Telegram Bot API features (owner 2026-10-18).

Researched against core.telegram.org/bots/api-changelog.  Everything here is
written to DEGRADE, never to crash: each call is attempted, and on any
rejection the caller falls back to the plain version, so a bot API change can
never take a command offline.

Implemented:
  * EPHEMERAL messages (Bot API 10.2/10.3) -- a group message only one member
    can see.  Used so bot cards stop spamming the whole group.
  * MEMBER TAGS (Bot API 9.5, setChatMemberTag) -- /tag to label a member.
  * date_time entities (Bot API 9.5) -- timestamps rendered in each reader's
    own timezone by the Telegram client.
  * Message effects (Bot API 9.x, effect_id) -- celebration animation.
"""

import html
import inspect
import logging
import os

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.helpers import mention

PRE = ["/", "?", "!", "."]
logger = logging.getLogger(__name__)

EPHEMERAL = (os.getenv("MODERN_EPHEMERAL", "true") or "true").strip().lower() \
    not in ("0", "false", "off", "no")
EFFECTS = (os.getenv("MODERN_EFFECTS", "true") or "true").strip().lower() \
    not in ("0", "false", "off", "no")

# Telegram's published message-effect ids.  An id Telegram does not accept is
# simply ignored (the send falls back to a plain message), so these are a
# best-effort flourish rather than a hard dependency.
EFFECTS_BY_NAME = {
    "party": "5046509860389126442",       # celebration
    "fire": "5104841245755180586",
    "love": "5159385139981059251",
    "like": "5107584321108051014",
    "dislike": "5104858069142078462",
    "poop": "5046589136895476101",
}


def _accepts(func, name: str) -> bool:
    """Whether a library call really takes this parameter."""
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


async def send_ephemeral(chat_id: int, text: str, user_id: int, **kwargs):
    """A group message only `user_id` (and the bot) can see.

    Returns the sent message, or None when Telegram refuses -- the caller then
    sends a normal reply instead.  Never raises.
    """
    if not EPHEMERAL or not user_id:
        return None
    sender = getattr(app, "send_message", None)
    if sender is None or not _accepts(sender, "ephemeral_message_parameters"):
        return None
    try:
        params = types.EphemeralMessageParameters(receiver_user_id=int(user_id))
    except Exception as exc:  # noqa: BLE001
        logger.info("ephemeral unavailable: %s", type(exc).__name__)
        return None
    kwargs.setdefault("parse_mode", enums.ParseMode.HTML)
    try:
        return await app.send_message(
            chat_id, text, ephemeral_message_parameters=params, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.info("ephemeral send refused (%s); using a normal reply",
                    type(exc).__name__)
        return None


async def reply_ephemeral(message, text: str, **kwargs):
    """Ephemeral when possible, ordinary reply otherwise.  Returns the reply."""
    user = getattr(message, "from_user", None)
    if user is not None:
        sent = await send_ephemeral(
            message.chat.id, text, user.id,
            reply_parameters=types.ReplyParameters(message_id=message.id)
            if _accepts(app.send_message, "reply_parameters") else None,
            **{k: v for k, v in kwargs.items() if v is not None},
        )
        if sent is not None:
            return sent
    return await message.reply_text(text, **kwargs)


async def send_effect(chat_id: int, text: str, effect: str = "party", **kwargs):
    """Send with a celebration effect; falls back to a plain send."""
    kwargs.setdefault("parse_mode", enums.ParseMode.HTML)
    if EFFECTS and _accepts(app.send_message, "effect_id"):
        effect_id = EFFECTS_BY_NAME.get(effect)
        if effect_id:
            try:
                return await app.send_message(chat_id, text, effect_id=effect_id,
                                              **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.info("effect %s refused (%s); plain send",
                            effect, type(exc).__name__)
    return await app.send_message(chat_id, text, **kwargs)


def local_time_entity(offset: int, length: int, unix_ts: int, style: str = "t"):
    """A date_time entity: Telegram renders it in the READER's timezone.

    style: 't' = short time (9:20 PM), 'D' = date, 'd' = date, 'T' = seconds,
           'f' = full date+time, 'F' = full with weekday, 'R' = relative.
    """
    try:
        return types.MessageEntity(
            type=enums.MessageEntityType.DATE_TIME,
            offset=offset, length=length,
            unix_time=unix_ts, date_time_format=style,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("date_time entity unavailable: %s", type(exc).__name__)
        return None


async def _admins(chat_id: int) -> list:
    out = []
    async for mem in app.get_chat_members(
            chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
        u = getattr(mem, "user", None)
        if u and not u.is_bot:
            out.append(u)
    return out


async def _is_staff(chat_id: int, user_id: int) -> bool:
    try:
        member = await app.get_chat_member(chat_id, user_id)
        return getattr(member, "status", None) in (
            enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False


_USAGE = (
    "<b>Member tags &amp; modern features</b>\n"
    "<code>/tag label</code> \u2014 reply to a member to give them a tag\n"
    "<code>/tag clear</code> \u2014 remove their tag\n"
    "<code>/tag list</code> \u2014 see who has one\n"
    "<code>/modern</code> \u2014 which new Telegram features are active\n\n"
    "<i>Tags need me to hold the &quot;manage tags&quot; admin right.</i>"
)


@app.on_message(filters.group & filters.command(["tag"], prefixes=PRE))
async def tag_cmd(_, message: types.Message) -> None:
    """Member tags (Bot API 9.5): a short label shown next to a member."""
    user = getattr(message, "from_user", None)
    chat_id = message.chat.id
    if user is None or not await _is_staff(chat_id, user.id):
        return await reply_ephemeral(
            message, "Admins only \u2014 reply to a member with "
                     "<code>/tag label</code>.", parse_mode=enums.ParseMode.HTML)
    setter = getattr(app, "set_chat_member_tag", None)
    if setter is None :
        return await reply_ephemeral(
            message, "This server's library has no member-tag support yet.",
            parse_mode=enums.ParseMode.HTML)

    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if arg.lower() == "list":
        rows = []
        try:
            async for mem in app.get_chat_members(chat_id):
                tag = getattr(mem, "tag", None)
                if tag:
                    u = getattr(mem, "user", None)
                    if u:
                        rows.append(f"\u2022 {mention(u.id, u.first_name or 'user')} "
                                    f"\u2014 <b>{html.escape(str(tag))}</b>")
        except Exception as exc:  # noqa: BLE001
            logger.info("tag list failed: %s", type(exc).__name__)
        return await reply_ephemeral(
            message,
            "<b>Member tags</b>\n" + ("\n".join(rows) if rows else "nobody has one yet."),
            parse_mode=enums.ParseMode.HTML)

    reply = getattr(message, "reply_to_message", None)
    target = getattr(reply, "from_user", None) if reply is not None else None
    if target is None and len(parts) > 1 and parts[1].startswith("@"):
        try:
            target = await app.get_users(parts[1][1:].split()[0])
        except Exception:
            target = None
    if target is None:
        return await reply_ephemeral(
            message, "Reply to the member you want to tag, then send "
                     "<code>/tag label</code>.", parse_mode=enums.ParseMode.HTML)

    tag = "" if not arg or arg.lower() in ("clear", "off", "none") else arg[:16]
    # pass only the parameters the installed library actually exposes
    kwargs = {}
    for key, value in (("chat_id", chat_id), ("user_id", target.id), ("tag", tag)):
        kwargs[key] = value
    try:
        await setter(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.info("set_chat_member_tag failed: %s", type(exc).__name__)
        return await reply_ephemeral(
            message,
            "I couldn't set that tag \u2014 I need the <b>manage tags</b> "
            "admin right in this group.", parse_mode=enums.ParseMode.HTML)
    who = mention(target.id, target.first_name or "user")
    return await reply_ephemeral(
        message,
        (f"\u2705 {who}'s tag is now <b>{html.escape(tag)}</b>." if tag else
         f"\u2705 {who}'s tag was removed."),
        parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command(["modern"], prefixes=PRE))
async def modern_cmd(_, message: types.Message) -> None:
    """Which of the newer Telegram features this server can actually use."""
    checks = [
        ("Ephemeral messages", "ephemeral_message_parameters"),
        ("Message effects", "effect_id"),
        ("Reply quoting", "quote_text"),
        ("Thread topics", "direct_messages_topic_id"),
        ("Protected content", "protect_content"),
        ("Scheduled messages", "schedule_date"),
        ("Repeating messages", "repeat_period"),
        ("Paid broadcasts", "allow_paid_broadcast"),
        ("Suggested posts", "suggested_post_parameters"),
    ]
    mark = lambda ok: "\u2705" if ok else "\u274c"
    lines = ["<b>Modern Telegram features</b>", ""]
    for label, param in checks:
        lines.append("%s %s" % (mark(_accepts(app.send_message, param)), label))
    lines += [
        "",
        "%s Member tags (<code>/tag</code>)" % mark(hasattr(app, "set_chat_member_tag")),
        "%s Local-time stamps" % mark(bool(getattr(enums.MessageEntityType, "DATE_TIME", None))),
        "",
        f"Ephemeral replies are {'on' if EPHEMERAL else 'off'}; "
        f"effects are {'on' if EFFECTS else 'off'}.",
    ]
    await message.reply_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)
