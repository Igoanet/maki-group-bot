"""Edit Guardian trust: who is exempt, and who may hand it out (2026-10-18).

The group OWNER can exempt a member from Edit Guardian, so that member's
edits are never deleted.  The owner can also hand that power to someone else
("grant"), who may then exempt further members themselves -- without ever
being able to change who holds the power.

    /exempt add     [reply | @username | id]   exempt a member
    /exempt remove  [reply | @username | id]   drop an exemption
    /exempt list                               who is exempt / who may grant
    /exempt allow   [reply | @username | id]   let a member grant too (owner)
    /exempt deny    [reply | @username | id]   take that power back (owner)
"""

import html
import logging

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.database import db
from ghelp.helpers import mention

PRE = ["/", "?", "!", "."]
logger = logging.getLogger(__name__)

EXEMPT_KEY = "editguard_exempt"
GRANT_KEY = "editguard_granters"


def _ids(value) -> list[int]:
    out = []
    for item in value or []:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def exempt_ids(settings: dict | None) -> list[int]:
    return _ids((settings or {}).get(EXEMPT_KEY))


def granter_ids(settings: dict | None) -> list[int]:
    return _ids((settings or {}).get(GRANT_KEY))


def is_exempt(settings: dict | None, user_id) -> bool:
    """True when this member must be left alone by Edit Guardian."""
    try:
        return int(user_id) in exempt_ids(settings)
    except (TypeError, ValueError):
        return False


async def _is_owner(chat_id: int, user_id: int) -> bool:
    try:
        member = await app.get_chat_member(chat_id, user_id)
        return getattr(member, "status", None) == enums.ChatMemberStatus.OWNER
    except Exception:
        return False


async def _target(message):
    """The member this command refers to: replied-to, @username or numeric id."""
    reply = getattr(message, "reply_to_message", None)
    user = getattr(reply, "from_user", None) if reply is not None else None
    if user is not None and not getattr(user, "is_bot", False):
        return user
    parts = (message.text or message.caption or "").split()
    if len(parts) < 3:
        return None
    token = parts[2].lstrip("@")
    try:
        return await app.get_users(int(token)) if token.lstrip("-").isdigit() \
            else await app.get_users(token)
    except Exception:
        return None


async def _save(chat_id: int, key: str, values) -> bool:
    try:
        await db.update_chat(chat_id, **{key: sorted({int(v) for v in values})})
        return True
    except Exception as exc:
        logger.warning("exempt: could not save %s: %s", key, type(exc).__name__)
        return False


def _people(ids, fallback: str) -> str:
    if not ids:
        return fallback
    return ", ".join(f"<code>{i}</code>" for i in ids)


_USAGE = (
    "<b>\U0001f513 Edit Guardian \u2014 edit-free</b>\n"
    "An edit-free member's edits are never deleted. An <b>editor</b> can "
    "make other members edit-free too.\n\n"
    "<b>Commands</b>\n"
    "<code>/editfree</code> \u2014 reply to a member to make them edit-free\n"
    "<code>/editfree off</code> \u2014 remove it\n"
    "<code>/editfree list</code> \u2014 who is edit-free, and who are editors\n"
    "<code>/editor</code> \u2014 reply to a member to make them an editor\n"
    "<code>/editor off</code> \u2014 take that back"
)


@app.on_message(filters.group & filters.command(["editfree", "trust", "exempt"], prefixes=PRE))
async def exempt_cmd(_, message: types.Message) -> None:
    user = getattr(message, "from_user", None)
    if user is None:
        # An anonymous admin has no verifiable identity here.
        return await message.reply_text(
            "Send this from your personal account \u2014 I can't verify an "
            "anonymous admin for trust."
        )
    chat_id = message.chat.id
    cfg = await db.get_chat(chat_id)
    owner = await _is_owner(chat_id, user.id)
    may_grant = owner or user.id in granter_ids(cfg)
    if not may_grant:
        return await message.reply_text(
            "Only the group owner \u2014 or someone the owner trusted \u2014 "
            "can change Edit Guardian trust."
        )

    parts = (message.text or "").split()
    action = parts[1].lower() if len(parts) > 1 else ""

    if action in ("list", "show", ""):
        await message.reply_text(
            "<b>Edit Guardian \u2014 edit-free</b>\n"
            f"Edit-free: {_people(exempt_ids(cfg), 'nobody')}\n"
            f"Editors: {_people(granter_ids(cfg), 'owner only')}\n\n"
            + _USAGE,
            parse_mode=enums.ParseMode.HTML,
        )
        return

    # Giving or taking the power itself stays with the owner.
    if action in ("allow", "deny", "revoke"):
        if not owner:
            await message.reply_text(
                "Only the group owner can give or take this power."
            )
            return
        target = await _target(message)
        if target is None:
            await message.reply_text(
                "Reply to that person, or send <code>/trust allow @name</code>.",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        grants = granter_ids(cfg)
        grants = (sorted(set(grants) | {target.id}) if action == "allow"
                  else [i for i in grants if i != target.id])
        if await _save(chat_id, GRANT_KEY, grants):
            who = mention(target.id, target.first_name or "user")
            await message.reply_text(
                (f"\u2705 {who} is now an editor."
                 if action == "allow" else
                 f"\u2705 {who} is no longer an editor."),
                parse_mode=enums.ParseMode.HTML,
            )
        return

    if action in ("add", "remove", "del", "rm"):
        target = await _target(message)
        if target is None:
            await message.reply_text(
                "Reply to that person, or send <code>/trust @name</code>.",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        ids = exempt_ids(cfg)
        ids = (sorted(set(ids) | {target.id}) if action == "add"
               else [i for i in ids if i != target.id])
        if await _save(chat_id, EXEMPT_KEY, ids):
            who = mention(target.id, target.first_name or "user")
            await message.reply_text(
                (f"\u2705 {who} is now edit-free \u2014 Edit Guardian will leave their "
                 "edits alone." if action == "add" else
                 f"\u2705 {who} is no longer edit-free."),
                parse_mode=enums.ParseMode.HTML,
            )
        return

    await message.reply_text(_USAGE, parse_mode=enums.ParseMode.HTML)


def is_editor(settings, user_id) -> bool:
    """True when this member may hand out edit-free to others."""
    try:
        return int(user_id) in granter_ids(settings)
    except (TypeError, ValueError):
        return False


async def grant_editor(chat_id: int, user_id: int) -> bool:
    """Make a member an editor, so they can use /editfree as well."""
    cfg = await db.get_chat(chat_id)
    return await _save(chat_id, GRANT_KEY,
                       sorted(set(granter_ids(cfg)) | {int(user_id)}))


async def revoke_editor(chat_id: int, user_id: int) -> bool:
    """Take the editor role back."""
    cfg = await db.get_chat(chat_id)
    return await _save(chat_id, GRANT_KEY,
                       [i for i in granter_ids(cfg) if i != int(user_id)])

@app.on_message(filters.group & filters.command(["editor"], prefixes=PRE))
async def editor_cmd(_, message: types.Message) -> None:
    """Make a member an EDITOR, or list/remove editors.

    An editor may use /editfree on other members.  This is the direct name for
    it (owner 2026-10-18); /promote editor still works as well.
    """
    user = getattr(message, "from_user", None)
    if user is None:
        return await message.reply_text(
            "Send this from your personal account \u2014 I can't verify an "
            "anonymous admin for this."
        )
    chat_id = message.chat.id
    cfg = await db.get_chat(chat_id)
    owner = await _is_owner(chat_id, user.id)
    if not (owner or is_editor(cfg, user.id)):
        return await message.reply_text(
            "Only the group owner \u2014 or an existing editor \u2014 can add "
            "editors."
        )

    parts = (message.text or "").split()
    action = parts[1].lower() if len(parts) > 1 else ""

    if action in ("list", "show", ""):
        return await message.reply_text(
            "<b>Edit Guardian</b>\nEdit-free: "
            + _people(exempt_ids(cfg), "nobody")
            + "\nEditors: "
            + _people(granter_ids(cfg), "owner only")
            + "\n\n" + _USAGE,
            parse_mode=enums.ParseMode.HTML,
        )

    if action in ("off", "remove", "del", "rm", "deny", "revoke"):
        target = await _target(message)
        if target is None:
            return await message.reply_text(
                "Reply to that person, or send <code>/editor off @name</code>.",
                parse_mode=enums.ParseMode.HTML,
            )
        if await revoke_editor(chat_id, target.id):
            who = mention(target.id, target.first_name or "user")
            await message.reply_text(
                "\u2705 " + who + " is no longer an editor.",
                parse_mode=enums.ParseMode.HTML,
            )
        return

    if action in ("add", "allow", "on"):
        target = await _target(message)
        if target is None:
            return await message.reply_text(
                "Reply to the member you want as an editor, then send "
                "<code>/editor</code>.",
                parse_mode=enums.ParseMode.HTML,
            )
        if await grant_editor(chat_id, target.id):
            who = mention(target.id, target.first_name or "user")
            await message.reply_text(
                "\u2705 " + who + " is now an editor \u2014 they can use /editfree.",
                parse_mode=enums.ParseMode.HTML,
            )
        return

    await message.reply_text(_USAGE, parse_mode=enums.ParseMode.HTML)
