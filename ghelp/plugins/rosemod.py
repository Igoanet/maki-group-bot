"""Rose-style moderation extras: timed bans, silent & delete variants,
warn configuration, self-kick."""

import logging
import re
from datetime import datetime, timedelta

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.database import db
from ghelp.helpers import (
    is_admin,
    is_owner,
    require_admin,
    require_owner,
    require_right,
    safe_restrict,
)
from ghelp.plugins.admin import (
    _ban_rpc,
    _delete_reply_rpc,
    _muted_permissions,
    _reply,
    _restrict_until,
    _unban_rpc,
    _warn_target,
    _target,
    apply_warn,
    require_warn_reset,
    warn_reset_denied,
    warn_lock,
)
from ghelp.moderation_reply import action_text, failure_text

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]
_MIN_TIMED_SECONDS = 60
_MAX_TIMED_SECONDS = 366 * 86400
_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)


def _duration_value(raw: str | None) -> int | None:
    """Parse Rose's documented s/m/h/d/w duration syntax.

    ``helpers.parse_duration`` intentionally serves the older mute parser and
    does not know about weeks.  Keep this parser local so the shared target
    resolver does not acquire moderation-specific rules.
    """
    match = _DURATION_RE.fullmatch(raw or "")
    if not match:
        return None
    multiplier = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 7 * 86400,
    }[match.group(2).lower()]
    return int(match.group(1)) * multiplier


def _valid_timed_seconds(seconds: int | None) -> bool:
    """Telegram treats dates outside this range as a permanent ban."""
    return (
        seconds is not None
        and _MIN_TIMED_SECONDS <= seconds <= _MAX_TIMED_SECONDS
    )


def _timed_bans_supported(message: types.Message) -> bool:
    """Basic groups have no timed-ban RPC (only DeleteChatUser)."""
    return getattr(message.chat, "type", None) != enums.ChatType.GROUP


def _duration_usage(command: str, *, required: bool = True) -> str:
    if required:
        return (
            f"Give a duration from <code>{_MIN_TIMED_SECONDS}s</code> to "
            f"<code>366d</code>, e.g. <code>/{command} 2h</code>."
        )
    return (
        f"Duration must be between <code>{_MIN_TIMED_SECONDS}s</code> and "
        "<code>366d</code>."
    )


async def _duration_arg(message: types.Message):
    """Return the optional duration after a reply/explicit target."""
    raw = None
    if message.reply_to_message and len(message.command) > 1:
        raw = message.command[1]
    elif len(message.command) > 2:
        raw = message.command[2]
    if raw is None:
        return None, None
    return raw, _duration_value(raw)


def _reason_after_target(message: types.Message) -> str:
    by_reply = bool(
        message.reply_to_message and message.reply_to_message.from_user
    )
    parts = message.command[1:] if by_reply else message.command[2:]
    return " ".join(parts).strip()


def _reason_after_duration(message: types.Message) -> str:
    by_reply = bool(
        message.reply_to_message and message.reply_to_message.from_user
    )
    parts = message.command[2:] if by_reply else message.command[3:]
    return " ".join(parts).strip()


def _rpc_ok(result) -> bool:
    """Treat only truthy Telegram results as a completed mutation."""
    return bool(result)


async def _ban(message: types.Message, user: types.User, seconds: int | None = None):
    return await _ban_rpc(message, user, seconds=seconds)


async def _unban(message: types.Message, user: types.User):
    return await _unban_rpc(message, user)


async def _delete_message(message) -> bool:
    try:
        result = await message.delete()
    except Exception:
        return False
    return _rpc_ok(result)


async def _require_moderation_rights(message: types.Message) -> bool:
    if not await require_right(message, "can_restrict_members", "Ban users"):
        return False
    return await require_right(
        message, "can_delete_messages", "Delete messages"
    )


@app.on_message(
    filters.group & filters.command(["tban", "timeban"], prefixes=PRE)
)
async def tban_user(_, message: types.Message):
    if not await require_right(message, "can_restrict_members", "Ban users"):
        return
    user = await _target(message)
    if not user:
        return
    raw, seconds = await _duration_arg(message)
    if not _valid_timed_seconds(seconds):
        await _reply(message, _duration_usage("tban"))
        return
    if not _timed_bans_supported(message):
        await _reply(
            message,
            "Timed bans are not supported in basic groups; use /ban or "
            "/kick instead.",
        )
        return
    by_reply = bool(
        message.reply_to_message and message.reply_to_message.from_user
    )
    reason_parts = message.command[2:] if by_reply else message.command[3:]
    reason = " ".join(reason_parts).strip()
    succeeded = await _ban(message, user, seconds)
    if succeeded:
        text = action_text(
            message, "Banned", user, duration=raw, reason=reason
        )
    else:
        text = failure_text(
            message, "ban", user, duration=raw, reason=reason
        )
    await _reply(message, text)


async def _delete_variant(
    message: types.Message,
    action: str,
    *,
    timed: bool = False,
) -> None:
    if not await _require_moderation_rights(message):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await _reply(
            message,
            "Reply to the user's message to delete it.",
        )
        return
    user = await _target(message)
    if not user:
        return
    raw, seconds = await _duration_arg(message)
    if timed and not _valid_timed_seconds(seconds):
        await _reply(message, _duration_usage("dtban"))
        return
    if timed and not _timed_bans_supported(message):
        await _reply(
            message,
            "Timed bans are not supported in basic groups; use /dban or "
            "/dkick instead.",
        )
        return
    reason = (
        _reason_after_duration(message)
        if timed
        else _reason_after_target(message)
    )
    # Apply the ban before touching the replied-to message.  A target can
    # become protected while the target lookup is in flight; deleting first
    # would lose that message even when the final ban preflight rejects the
    # mutation.
    ban_succeeded = await _ban(message, user, seconds if timed else None)
    if not ban_succeeded:
        await _reply(
            message,
            failure_text(
                message,
                action,
                user,
                reason=reason,
                duration=raw if timed else "",
                detail="The target message was not deleted.",
            ),
        )
        return
    if action == "kick":
        unban_succeeded = await _unban(message, user)
        if not unban_succeeded:
            await _reply(
                message,
                failure_text(
                    message,
                    action,
                    user,
                    reason=reason,
                    duration=raw if timed else "",
                    detail=(
                        "Ban was applied, but unban failed; the target remains "
                        "banned. The target message was not deleted."
                    ),
                ),
            )
            return
    deleted = await _delete_reply_rpc(
        message, user, message.reply_to_message
    )
    word = "Kicked" if action == "kick" else "Banned"
    detail = (
        "The target message was deleted."
        if deleted
        else "The moderation action succeeded, but the target message "
        "could not be deleted."
    )
    await _reply(
        message,
        action_text(
            message,
            word,
            user,
            reason=reason,
            duration=raw if timed else "",
            detail=detail,
        ),
    )


@app.on_message(
    filters.group & filters.command(["dban", "deepban"], prefixes=PRE)
)
async def dban_user(_, message: types.Message):
    """Reply-delete + ban; ``deepban`` is not a history/global-ban command."""
    await _delete_variant(message, "ban")


@app.on_message(filters.group & filters.command("dtban", prefixes=PRE))
async def dtban_user(_, message: types.Message):
    await _delete_variant(message, "ban", timed=True)


@app.on_message(filters.group & filters.command("dkick", prefixes=PRE))
async def dkick_user(_, message: types.Message):
    await _delete_variant(message, "kick")


async def _silent(
    message: types.Message,
    action: str,
    *,
    timed: bool = False,
) -> None:
    """Apply a silent action; announce denials and partial failures only."""
    if not await _require_moderation_rights(message):
        return
    user = await _target(message)
    if not user:
        return
    raw, seconds = await _duration_arg(message)
    if timed and not _valid_timed_seconds(seconds):
        await _reply(message, _duration_usage("stban"))
        return
    if timed and not _timed_bans_supported(message):
        await _reply(
            message,
            "Timed bans are not supported in basic groups; use /sban or "
            "/skick instead.",
        )
        return
    if (
        not timed
        and action == "ban"
        and seconds is not None
        and not _valid_timed_seconds(seconds)
    ):
        await _reply(message, _duration_usage(action, required=False))
        return
    reply = message.reply_to_message
    target_deleted = True
    # Mute retains Rose's historical cleanup order.  Ban/kick variants must
    # apply the restriction first and only then attempt optional cleanup.
    if action == "mute" and reply:
        target_deleted = await _delete_message(reply)

    action_ok = False
    partial_detail = ""
    if action == "ban":
        action_ok = await _ban(message, user, seconds)
    elif action == "kick":
        action_ok = await _ban(message, user)
        if action_ok and not await _unban(message, user):
            partial_detail = (
                "Ban was applied, but unban failed; the target remains banned."
            )
            action_ok = False
    else:
        mute_seconds = seconds or None
        until = (
            datetime.now() + timedelta(seconds=mute_seconds)
            if mute_seconds is not None
            else None
        )
        if until is not None:
            action_ok = await _restrict_until(message.chat.id, user.id, until)
        else:
            action_ok = await safe_restrict(
                message.chat.id, user.id, _muted_permissions()
            )

    if not action_ok:
        detail = partial_detail or "The moderation request was rejected."
        if reply and not target_deleted:
            detail += " The target message could not be deleted."
        await _reply(
            message,
            failure_text(
                message,
                action,
                user,
                duration=raw if seconds is not None else "",
                detail=detail,
            ),
        )
        return

    if action in ("ban", "kick") and reply:
        target_deleted = await _delete_reply_rpc(message, user, reply)
    command_deleted = await _delete_message(message)
    cleanup_failed = (
        (reply and not target_deleted)
        or not command_deleted
    )
    logger.info("silent %s: %s in %s", action, user.id, message.chat.id)
    if cleanup_failed:
        detail = "The moderation action succeeded, but "
        if reply and not target_deleted:
            detail += "the target message could not be deleted."
        else:
            detail += "the command message could not be deleted."
        await _reply(
            message,
            action_text(
                message,
                action.title(),
                user,
                duration=raw if seconds is not None else "",
                detail=detail,
            ),
        )


@app.on_message(filters.group & filters.command("sban", prefixes=PRE))
async def sban_user(_, message: types.Message):
    await _silent(message, "ban")


@app.on_message(filters.group & filters.command("stban", prefixes=PRE))
async def stban_user(_, message: types.Message):
    await _silent(message, "ban", timed=True)


@app.on_message(filters.group & filters.command("skick", prefixes=PRE))
async def skick_user(_, message: types.Message):
    await _silent(message, "kick")


@app.on_message(filters.group & filters.command("smute", prefixes=PRE))
async def smute_user(_, message: types.Message):
    await _silent(message, "mute")


async def _delete_mute(message: types.Message) -> None:
    """Mute a replied-to user, then remove the triggering message.

    The restriction is deliberately applied before deleting either message.
    That way a lost delete permission never turns a requested moderation action
    into a silent no-op.
    """
    if not await _require_moderation_rights(message):
        return
    reply = message.reply_to_message
    if not reply or not reply.from_user:
        await _reply(message, "Reply to the user's message to mute and delete it.")
        return
    user = await _target(message)
    if not user:
        return
    raw, seconds = await _duration_arg(message)
    if raw is not None and seconds is None:
        # /dmute reason is a valid permanent mute; only reject a value that
        # looks like a duration but is outside Telegram's safe timed range.
        if re.fullmatch(r"\d+\s*[smhdw]", raw, re.IGNORECASE):
            await _reply(message, _duration_usage("dmute", required=False))
            return
        raw = None
    if seconds is not None and not _valid_timed_seconds(seconds):
        await _reply(message, _duration_usage("dmute", required=False))
        return
    reason = (
        _reason_after_duration(message)
        if seconds is not None
        else _reason_after_target(message)
    )
    try:
        muted = (
            await _restrict_until(
                message.chat.id,
                user.id,
                datetime.now() + timedelta(seconds=seconds),
            )
            if seconds is not None
            else await safe_restrict(
                message.chat.id, user.id, _muted_permissions()
            )
        )
    except Exception:
        muted = False
    if not muted:
        await _reply(
            message,
            failure_text(
                message, "mute", user, reason=reason,
                duration=raw or "",
            ),
        )
        return
    deleted = await _delete_reply_rpc(message, user, reply)
    detail = (
        "The target message was deleted."
        if deleted
        else "The mute succeeded, but the target message could not be deleted."
    )
    await _reply(
        message,
        action_text(
            message, "Muted", user, reason=reason,
            duration=raw or "", detail=detail,
        ),
    )


@app.on_message(filters.group & filters.command("dmute", prefixes=PRE))
async def dmute_user(_, message: types.Message):
    await _delete_mute(message)


@app.on_message(filters.group & filters.command("tmute", prefixes=PRE))
async def tmute_user(_, message: types.Message):
    if not await require_right(message, "can_restrict_members", "Ban users"):
        return
    user = await _target(message)
    if not user:
        return
    raw, seconds = await _duration_arg(message)
    if not _valid_timed_seconds(seconds):
        await _reply(message, _duration_usage("tmute"))
        return
    reason = _reason_after_duration(message)
    try:
        muted = await _restrict_until(
            message.chat.id,
            user.id,
            datetime.now() + timedelta(seconds=seconds),
        )
    except Exception:
        muted = False
    if muted:
        await _reply(
            message,
            action_text(
                message, "Muted", user,
                reason=reason, duration=raw or "",
            ),
        )
    else:
        await _reply(
            message,
            failure_text(
                message, "mute", user,
                reason=reason, duration=raw or "",
            ),
        )


@app.on_message(filters.group & filters.command("dwarn", prefixes=PRE))
async def dwarn_user(_, message: types.Message):
    if not await require_admin(message, "can_restrict_members", "Ban users"):
        return
    if not message.reply_to_message:
        await _reply(message, "Reply to the user's message to delete and warn.")
        return
    user = await _warn_target(message)
    if not user:
        return
    try:
        await message.reply_to_message.delete()
    except Exception:
        pass
    await apply_warn(message, user, " ".join(message.command[1:]).strip())


@app.on_message(filters.group & filters.command("swarn", prefixes=PRE))
async def swarn_user(_, message: types.Message):
    if not await require_admin(message, "can_restrict_members", "Ban users"):
        return
    user = await _warn_target(message)
    if not user:
        return
    reason_parts = (
        message.command[1:] if message.reply_to_message else message.command[2:]
    )
    await apply_warn(message, user, " ".join(reason_parts).strip(), quiet=True)
    try:
        await message.delete()
    except Exception:
        pass


@app.on_message(filters.group & filters.command("warnlimit", prefixes=PRE))
async def set_warn_limit(_, message: types.Message):
    if not await require_owner(message):
        return
    raw = message.command[1] if len(message.command) > 1 else ""
    if not raw.isdigit() or not 0 <= int(raw) <= 20:
        await _reply(
            message,
            "Usage: <code>/warnlimit 0-20</code>. Next: use 0 to disable auto-action.",
        )
        return
    n = int(raw)
    cfg = await db.get_chat(message.chat.id)
    if cfg.get("linkdel_on") and n != 3:
        await _reply(
            message,
            "Warn limit must stay <b>3</b> while link warnings are enabled.",
        )
        return
    await db.update_chat(message.chat.id, warn_limit=n)
    shown = str(n) if n else "unlimited"
    await _reply(message, f"Warn limit set to <b>{shown}</b>.")


@app.on_message(filters.group & filters.command(["warnaction", "strongwarn"], prefixes=PRE))
async def set_warn_action(_, message: types.Message):
    if not await require_owner(message):
        return
    cmd = message.command[0].lower()
    arg = message.command[1].lower() if len(message.command) > 1 else ""
    if cmd == "strongwarn":  # Rose naming: on = ban, off = kick
        action = {"on": "ban", "off": "kick"}.get(arg)
        if not action:
            await _reply(message, "Usage: <code>/strongwarn on|off</code> (on = ban, off = kick).")
            return
    else:
        action = arg if arg in ("mute", "kick", "ban") else None
        if not action:
            await _reply(message, "Usage: <code>/warnaction mute|kick|ban</code>.")
            return
    cfg = await db.get_chat(message.chat.id)
    if cfg.get("linkdel_on") and action != "mute":
        await _reply(
            message,
            "Warn action must stay <b>mute</b> while link warnings are enabled.",
        )
        return
    await db.update_chat(message.chat.id, warn_action=action)
    word = {"mute": "muted", "kick": "kicked", "ban": "banned"}[action]
    await _reply(message, f"Users hitting the warn limit will be <b>{word}</b>.")


@app.on_message(filters.group & filters.command("resetallwarns", prefixes=PRE))
async def reset_all_warns(_, message: types.Message):
    if not await require_warn_reset(message):
        return
    try:
        cfg = await db.get_chat(message.chat.id)
        ids = sorted({
            int(uid) for uid in (cfg.get("warns") or {})
            if str(uid).lstrip("-").isdigit()
        })
        locks = [warn_lock(message.chat.id, uid) for uid in ids]
        acquired = []
        outcome = {}
        try:
            for lock in locks:
                await lock.acquire()
                acquired.append(lock)
            caller_is_owner = bool(
                message.from_user
                and await is_owner(message.chat.id, message.from_user.id)
            )

            def reset(current):
                if current.get("linkdel_on", False) and not caller_is_owner:
                    outcome["denied"] = True
                    return
                current["warns"] = {}

            await db.mutate_chat(message.chat.id, reset)
        finally:
            for lock in reversed(acquired):
                lock.release()
    except Exception:
        await _reply(message, "Could not reset warnings.")
        return
    if outcome.get("denied"):
        await warn_reset_denied(message)
        return
    await _reply(message, "All warnings in this chat were reset.")


@app.on_message(filters.group & filters.command("kickme", prefixes=PRE))
async def kickme(_, message: types.Message):
    user = message.from_user
    if not user:
        return
    if await is_admin(message.chat.id, user.id):
        await _reply(message, "Admins can't kick themselves — demote first!")
        return
    ban_succeeded = await _ban_rpc(
        message, user, actor_privilege=None
    )
    if not ban_succeeded:
        await _reply(
            message,
            failure_text(message, "kick", user, detail="Rights may be missing."),
        )
        return
    unban_succeeded = await _unban_rpc(
        message, user, actor_privilege=None
    )
    if unban_succeeded:
        await _reply(message, action_text(message, "Kicked", user))
    else:
        await _reply(
            message,
            failure_text(
                message,
                "kick",
                user,
                detail="Ban was applied, but unban failed; you remain banned.",
            ),
        )
