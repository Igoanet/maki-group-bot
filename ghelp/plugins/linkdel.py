"""Owner-controlled link deletion and the shared three-warning mute policy."""

import html
import logging
import time
import uuid
from dataclasses import dataclass

from pyrogram import enums, filters, types
from pyrogram.errors import MessageNotModified

from ghelp import app
from ghelp.config import config
from ghelp.database import db
from ghelp.helpers import concise_bot_text, ikb, ikm, is_owner, require_owner
from ghelp.link_detection import has_links, is_whisper
from ghelp.selfdel import autodel_reply

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]
DEFAULT_TIME = 86400
TIME_CHOICES = (3600, 21600, 86400, 604800)


@dataclass
class Confirmation:
    chat_id: int
    user_id: int
    message_id: int
    created: float
    revision: int


@dataclass
class DeleteAttempt:
    """One delete/read-back attempt with only safe diagnostic metadata."""

    confirmed: bool
    stage: str
    error: str = ""


_confirmations: dict[str, Confirmation] = {}
_notices: dict[int, float] = {}


@autodel_reply
async def _reply(message, text, **kwargs):
    return await message.reply_text(concise_bot_text(text), parse_mode=enums.ParseMode.HTML, **kwargs)


def _group(message) -> bool:
    return bool(message and message.chat and message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP))


def _seconds(settings) -> int:
    try:
        value = int(settings.get("warn_time", DEFAULT_TIME))
        return value if 60 <= value <= 30 * 86400 else DEFAULT_TIME
    except (TypeError, ValueError):
        return DEFAULT_TIME


def _duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m" if seconds % 60 == 0 else f"{seconds}s"


def _revision(settings) -> int:
    try:
        return max(0, int(settings.get("linkdel_revision", 0)))
    except (TypeError, ValueError):
        return 0


def _revoke(chat_id: int) -> None:
    now = time.monotonic()
    for key, item in list(_confirmations.items()):
        if item.chat_id == chat_id or now - item.created > 300:
            _confirmations.pop(key, None)


def _panel(settings):
    enabled = bool(settings.get("linkdel_on"))
    admins = enabled and bool(settings.get("linkdel_admins"))
    text = (
        "<b>Link Deleter</b>\n\n"
        f"Members: <b>{'ON' if enabled else 'OFF'}</b>\n"
        f"Admin links: <b>{'ON' if admins else 'OFF'}</b>\n"
        f"Limit: <b>3 warnings → mute for {_duration(_seconds(settings))}</b>\n\n"
        "Covers links in text, captions, hidden links, URL buttons and edits. "
        "The old URL-lock allowlist does not bypass this rule.\n"
        "Only the group owner or bot owner can change these settings.\n\n"
        "<b>Admin limit:</b> muting requires removing editable admin rights first. "
        "The bot needs Add admins and Ban users rights. Admin rights are not restored automatically. "
        "For other admins, the bot deletes links and reports that the owner must act.\n"
        "Anonymous admin/channel posts can be deleted, but cannot receive personal warnings. "
        "Group owner and bot owner are protected.\n\n"
        "Custom mute duration: <code>/setwarntime 24h</code> (1m–30d). "
        "24h is the mute duration, not warning expiry."
    )
    keyboard = ikm([
        [ikb(f"Link deleter: {'ON' if enabled else 'OFF'}", callback_data=f"gh:ld:{'disable' if enabled else 'enable'}",
             style=enums.ButtonStyle.SUCCESS if enabled else enums.ButtonStyle.PRIMARY)],
        [ikb(f"Admin links: {'ON' if admins else 'OFF'}", callback_data=f"gh:ld:{'admins-off' if admins else 'admin'}",
             style=enums.ButtonStyle.SUCCESS if admins else enums.ButtonStyle.PRIMARY)],
        [ikb(f"Mute time: {_duration(_seconds(settings))}", callback_data="gh:ld:time", style=enums.ButtonStyle.PRIMARY)],
        [ikb("Refresh", callback_data="gh:ld:open", style=enums.ButtonStyle.PRIMARY),
         ikb("Close", callback_data="gh:ld:close", style=enums.ButtonStyle.DANGER)],
    ])
    return text, keyboard


async def _permissions(chat_id: int) -> None:
    """Check the actual bot's deletion/mute rights, not the owner's rights."""
    me = getattr(app, "me", None) or await app.get_me()
    member = await app.get_chat_member(chat_id, me.id)
    privileges = getattr(member, "privileges", None)
    missing = [label for name, label in (
        ("can_delete_messages", "Delete messages"),
        ("can_restrict_members", "Ban users"),
    ) if not getattr(privileges, name, False)]
    if member.status != enums.ChatMemberStatus.ADMINISTRATOR or missing:
        raise PermissionError("Give the bot these admin rights first: " + ", ".join(missing or ["Delete messages", "Ban users"]) + ".")


async def _set_enabled(chat_id: int, enabled: bool, user_id: int) -> None:
    expected_revision = _revision(await db.get_chat(chat_id))
    if enabled:
        await _permissions(chat_id)
    if not await is_owner(chat_id, user_id):
        raise PermissionError("Owner access could not be verified. No settings were changed.")
    _revoke(chat_id)

    def change(settings):
        if enabled and _revision(settings) != expected_revision:
            raise ValueError("Settings changed while enabling link deletion")
        if enabled and not settings.get("linkdel_on"):
            settings["linkdel_admins"] = False
        settings["linkdel_on"] = enabled
        if enabled:
            settings["warn_limit"] = 3
            settings["warn_action"] = "mute"
        else:
            settings["linkdel_admins"] = False
        settings["linkdel_revision"] = _revision(settings) + 1
    await db.mutate_chat(chat_id, change)


async def _admins_off(chat_id: int) -> None:
    _revoke(chat_id)

    def change(settings):
        settings["linkdel_admins"] = False
        settings["linkdel_revision"] = _revision(settings) + 1
    await db.mutate_chat(chat_id, change)


async def _show(message, *, edit=False) -> None:
    text, keyboard = _panel(await db.get_chat(message.chat.id))
    if edit:
        await message.edit_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)
    else:
        await _reply(message, text, reply_markup=keyboard)


async def _ask_admin(message, user_id: int, *, edit=False) -> None:
    revision = _revision(await db.get_chat(message.chat.id))
    await _permissions(message.chat.id)
    if _revision(await db.get_chat(message.chat.id)) != revision:
        return await _reply(message, "Settings changed while opening confirmation. Open /linkdel again.", quote=False)
    _revoke(message.chat.id)
    # Bound the transient confirmation cache; none of these values authorize
    # a caller without a fresh owner check in the callback.
    if len(_confirmations) >= 512:
        _confirmations.pop(next(iter(_confirmations)))
    key = uuid.uuid4().hex[:24]
    text = (
        "<b>Confirm admin link enforcement</b>\n\n"
        "This enables link deletion and warnings for members and admins.\n"
        "On the third warning, the bot will <b>remove an editable admin's admin rights</b> "
        "and mute them for the configured duration (default 24h).\n\n"
        "<b>Admin rights will NOT return automatically.</b> The owner must restore them manually.\n"
        "Telegram must allow the bot to edit that admin, and the bot needs Add admins and Ban users. "
        "Otherwise, links are still deleted and the owner is told to demote/mute them manually.\n"
        "Anonymous admins cannot be personally warned or muted. Group owner and bot owner are protected."
    )
    keyboard = ikm([
        [ikb("Confirm admin enforcement", callback_data=f"gh:ld:confirm:{key}", style=enums.ButtonStyle.SUCCESS)],
        [ikb("Cancel", callback_data="gh:ld:open", style=enums.ButtonStyle.DANGER)],
    ])
    pending = Confirmation(message.chat.id, user_id, message.id if edit else 0, time.monotonic(), revision)
    _confirmations[key] = pending
    try:
        if edit:
            await message.edit_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)
        else:
            sent = await _reply(message, text, reply_markup=keyboard)
            if _confirmations.get(key) is not pending:
                await sent.delete()
                return
            pending.message_id = sent.id
    except BaseException:
        if _confirmations.get(key) is pending:
            _confirmations.pop(key, None)
        raise


@app.on_message(filters.group & filters.command("linkdel", prefixes=PRE))
async def linkdel(_, message: types.Message):
    if not await require_owner(message):
        return
    arg = message.command[1].lower() if len(message.command) == 2 else ""
    if len(message.command) > 1 and arg not in ("on", "off"):
        return await _reply(message, "Usage: <code>/linkdel on|off</code>, or <code>/linkdel</code> for settings.")
    try:
        if arg:
            await _set_enabled(message.chat.id, arg == "on", message.from_user.id)
        await _show(message)
    except PermissionError as exc:
        await _reply(message, html.escape(str(exc)))
    except Exception as exc:
        logger.warning("Link settings failed: %s", type(exc).__name__)
        await _reply(message, "Could not update link settings. Check the bot's admin rights and try again.")


@app.on_message(filters.group & filters.command("linkdeladmins", prefixes=PRE))
async def linkdeladmins(_, message: types.Message):
    if not await require_owner(message):
        return
    arg = message.command[1].lower() if len(message.command) == 2 else ""
    try:
        if arg == "on":
            return await _ask_admin(message, message.from_user.id)
        if arg == "off":
            await _admins_off(message.chat.id)
            return await _show(message)
        if len(message.command) == 1:
            return await _show(message)
        await _reply(message, "Usage: <code>/linkdeladmins on|off</code>. Enabling requires owner confirmation.")
    except PermissionError as exc:
        await _reply(message, html.escape(str(exc)))
    except Exception as exc:
        logger.warning("Admin link settings failed: %s", type(exc).__name__)
        await _reply(message, "Could not update admin link settings. Try again.")


@app.on_message(filters.private & filters.command(["linkdel", "linkdeladmins"], prefixes=PRE))
async def linkdel_private(_, message: types.Message):
    await _reply(
        message,
        "Open <code>/linkdel</code> inside the group you want to manage. "
        "The bot needs admin rights there. Only the group owner or bot owner can change its link settings.",
    )


@app.on_callback_query(filters.regex(r"^gh:ld:"))
async def linkdel_callback(_, callback: types.CallbackQuery):
    message, user = callback.message, callback.from_user
    if not _group(message):
        return await callback.answer("Open /linkdel inside your group to change its settings.", show_alert=True)
    if not user or not await is_owner(message.chat.id, user.id):
        return await callback.answer("Only the group owner or bot owner can change this.", show_alert=True)
    action = callback.data.removeprefix("gh:ld:")
    # Acknowledge once; errors after acknowledgement are visible messages.
    await callback.answer()
    try:
        if action == "close":
            _revoke(message.chat.id)
            return await message.delete()
        if action == "admin":
            return await _ask_admin(message, user.id, edit=True)
        if action == "time":
            _revoke(message.chat.id)
            return await message.edit_text(
                "<b>Warn-mute duration</b>\nChoose a duration or send <code>/setwarntime 24h</code> "
                "for a custom value (1m–30d). Warnings do not expire after this time.",
                parse_mode=enums.ParseMode.HTML,
                reply_markup=ikm([
                    [ikb(_duration(s), callback_data=f"gh:ld:settime:{s}", style=enums.ButtonStyle.PRIMARY) for s in TIME_CHOICES],
                    [ikb("Back", callback_data="gh:ld:open", style=enums.ButtonStyle.PRIMARY)],
                ]),
            )
        if action.startswith("confirm:"):
            key = action.removeprefix("confirm:")
            item = _confirmations.get(key)
            if not item or time.monotonic() - item.created > 300 or (
                item.chat_id, item.user_id, item.message_id
            ) != (message.chat.id, user.id, message.id):
                return await _reply(message, "This confirmation expired or belongs to another settings message. Open /linkdel again.", quote=False)
            await _permissions(message.chat.id)
            if not await is_owner(message.chat.id, user.id):
                return await _reply(message, "Owner access could not be verified. No settings were changed.", quote=False)

            def enable_admins(settings):
                # Check and consume consent INSIDE the database mutation.
                # Cancel/off during a Telegram permission lookup wins.
                if _confirmations.get(key) is not item or time.monotonic() - item.created > 300:
                    raise ValueError("Admin confirmation was cancelled or expired")
                if _revision(settings) != item.revision:
                    raise ValueError("Settings changed since admin confirmation was opened")
                _confirmations.pop(key, None)
                settings.update(linkdel_on=True, linkdel_admins=True, warn_limit=3, warn_action="mute")
                settings["linkdel_revision"] = _revision(settings) + 1
            await db.mutate_chat(message.chat.id, enable_admins)
        elif action == "enable":
            await _set_enabled(message.chat.id, True, user.id)
        elif action == "disable":
            await _set_enabled(message.chat.id, False, user.id)
        elif action == "admins-off":
            await _admins_off(message.chat.id)
        elif action.startswith("settime:") and action.removeprefix("settime:") in {str(s) for s in TIME_CHOICES}:
            await db.update_chat(message.chat.id, warn_time=int(action.removeprefix("settime:")))
        elif action == "open":
            _revoke(message.chat.id)
        else:
            return await _reply(message, "Unknown link-settings action.", quote=False)
        await _show(message, edit=True)
    except MessageNotModified:
        pass  # Refreshing an already-current settings panel is successful.
    except PermissionError as exc:
        await _reply(message, html.escape(str(exc)), quote=False)
    except Exception as exc:
        logger.warning("Link settings callback failed: %s", type(exc).__name__)
        await _reply(message, "Could not complete that change. Open /linkdel to check the current settings.", quote=False)


async def _notice(message, text: str) -> None:
    now = time.monotonic()
    if now - _notices.get(message.chat.id, -60) < 60:
        return
    if len(_notices) > 4096:
        _notices.clear()
    _notices[message.chat.id] = now
    await _reply(message, text, quote=False)


async def _delete_confirmed(message: types.Message) -> DeleteAttempt:
    """Delete one message and prove that it is no longer readable.

    In the installed Kurigram SDK, ``Message.delete()`` reduces MTProto's
    ``pts_count`` to a bool.  An acknowledged RPC is not evidence that this
    particular message is gone, so it must not start a warning or a
    ``Link deleted`` notice by itself.  Kurigram represents this client's
    missing single-message read-back as ``Message(empty=True, id=<asked-id>)``.
    Any other result, a mismatched id/chat, or a failed read-back is
    deliberately unconfirmed.
    """
    try:
        result = await message.delete()
    except Exception as exc:
        return DeleteAttempt(False, "delete_rpc", type(exc).__name__)
    try:
        remaining = await app.get_messages(
            message.chat.id, message.id, replies=0
        )
    except Exception as exc:
        return DeleteAttempt(False, "readback_rpc", type(exc).__name__)
    empty = (
        getattr(remaining, "empty", False) is True
        and getattr(remaining, "id", None) == message.id
        and (
            getattr(remaining, "chat", None) is None
            or getattr(remaining.chat, "id", None) == message.chat.id
        )
    )
    if empty:
        return DeleteAttempt(
            True,
            "confirmed" if result is True else "confirmed_after_zero_ack",
        )
    return DeleteAttempt(
        False,
        "readback_present" if result is True else "delete_result",
    )


async def _retry_delete(
    message: types.Message,
    expected_revision: int,
    me,
    user,
    sender_chat,
) -> DeleteAttempt | None:
    """Make one immediate retry only for the same still-actionable post.

    A retry is useful for a transient zero affected-count/read-after-write
    race, but must never become a second policy decision for a changed post.
    There have been no warning mutations at this point.
    """
    try:
        current = await db.get_chat(message.chat.id)
        if (
            not current.get("linkdel_on")
            or _revision(current) != expected_revision
        ):
            return None
        fresh = await app.get_messages(message.chat.id, message.id, replies=0)
        if fresh is None:
            return DeleteAttempt(False, "retry_readback_shape")
        if (
            getattr(fresh, "id", None) != message.id
            or getattr(getattr(fresh, "chat", None), "id", None) != message.chat.id
            or not has_links(fresh, buttons=True)
            or is_whisper(fresh, me.id, getattr(me, "username", None))
        ):
            return None
        fresh_sender_chat = getattr(fresh, "sender_chat", None)
        fresh_user = getattr(fresh, "from_user", None)
        if sender_chat:
            if (
                not fresh_sender_chat
                or fresh_sender_chat.id != sender_chat.id
                or (
                    fresh_sender_chat.id == message.chat.id
                    and not current.get("linkdel_admins")
                )
            ):
                return None
        elif user:
            if not fresh_user or fresh_user.id != user.id:
                return None
            member = await app.get_chat_member(message.chat.id, user.id)
            if (
                member.status == enums.ChatMemberStatus.OWNER
                or (
                    member.status == enums.ChatMemberStatus.ADMINISTRATOR
                    and not current.get("linkdel_admins")
                )
            ):
                return None
        else:
            return None
        # The role check above yielded; policy changes win before retrying.
        latest = await db.get_chat(message.chat.id)
        if (
            not latest.get("linkdel_on")
            or _revision(latest) != expected_revision
            or (
                fresh_sender_chat
                and fresh_sender_chat.id == message.chat.id
                and not latest.get("linkdel_admins")
            )
        ):
            return None
        return await _delete_confirmed(fresh)
    except Exception as exc:
        return DeleteAttempt(False, "retry_check", type(exc).__name__)


def _delete_failure_text(message: types.Message, attempt: DeleteAttempt) -> str:
    """Actionable, rate-limited group diagnostic without user content."""
    detail = (
        f"stage={attempt.stage}; error={attempt.error or 'none'}; "
        f"chat={message.chat.id}; message={message.id}"
    )
    return (
        "<b>Link Deleter could not confirm removal.</b> No link warning was "
        "recorded; the post may still be visible. Group owner: inspect the "
        "diagnostic and Maki's current admin rights, then retry. "
        f"<code>{html.escape(detail)}</code>"
    )


async def enforce(message, settings: dict) -> bool:
    """True only after deletion; caller then stops further moderation of it."""
    if not settings.get("linkdel_on") or not has_links(message, buttons=True):
        return False
    attempt = DeleteAttempt(False, "identity")
    try:
        me = getattr(app, "me", None) or await app.get_me()
        user = message.from_user
        if is_whisper(message, me.id, getattr(me, "username", None)) or (user and user.id in (me.id, config.OWNER_ID)):
            return False
        sender_chat = getattr(message, "sender_chat", None)
        if sender_chat:
            anonymous = sender_chat.id == message.chat.id
            if anonymous and not settings.get("linkdel_admins"):
                return False
            member = None
        elif user:
            member = await app.get_chat_member(message.chat.id, user.id)
            if member.status == enums.ChatMemberStatus.OWNER:
                return False
            if member.status == enums.ChatMemberStatus.ADMINISTRATOR and not settings.get("linkdel_admins"):
                return False
        else:
            return False
        # Re-read after role lookup so an owner disabling the policy while
        # Telegram responds prevents the destructive operation.
        current = await db.get_chat(message.chat.id)
        if not current.get("linkdel_on"):
            return False
        if ((sender_chat and sender_chat.id == message.chat.id) or
            (member and member.status == enums.ChatMemberStatus.ADMINISTRATOR)) and not current.get("linkdel_admins"):
            return False
        expected_revision = _revision(current)
        attempt = await _delete_confirmed(message)
        if not attempt.confirmed and attempt.stage in (
            "delete_result", "readback_present",
        ):
            # One bounded retry is still before any warning/notice work.
            retry = await _retry_delete(
                message, expected_revision, me, user, sender_chat
            )
            if retry is not None:
                attempt = retry
        if not attempt.confirmed:
            logger.warning(
                "Link deletion unconfirmed stage=%s error=%s chat_id=%s message_id=%s",
                attempt.stage, attempt.error or "none", message.chat.id, message.id,
            )
            await _notice(message, _delete_failure_text(message, attempt))
            return False
    except Exception as exc:
        attempt = DeleteAttempt(False, "identity_or_policy", type(exc).__name__)
        logger.warning(
            "Link deletion unconfirmed stage=%s error=%s chat_id=%s message_id=%s",
            attempt.stage, attempt.error, message.chat.id, message.id,
        )
        await _notice(message, _delete_failure_text(message, attempt))
        return False
    try:
        if sender_chat:
            await _notice(message, "Link deleted. Anonymous admin/channel posts cannot be assigned to a personal account, so no user was warned or muted.")
        else:
            from ghelp.plugins.admin import apply_warn
            await apply_warn(message, user, "Link posted while Link Deleter is enabled.", link_violation=True)
    except Exception as exc:
        logger.warning("Link was deleted; warning/notice failed: %s", type(exc).__name__)
        try:
            await _notice(message, "Link deleted, but the warning action could not be completed. Owner: check the bot's permissions and the user's /warns.")
        except Exception as notice_error:
            logger.warning("Link failure notice could not be sent: %s", type(notice_error).__name__)
    # Deletion remains handled even if persistence/notification failed. Do
    # not let later moderation handlers issue a second warning for this post.
    return True