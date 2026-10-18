"""Group administration commands."""

import asyncio
import html
import inspect
import logging
import re
import time
from datetime import datetime, timedelta

from pyrogram import enums, filters, raw, types, utils

from ghelp import app
from ghelp.selfdel import autodel_reply
from ghelp.config import config
from ghelp.database import db
from ghelp.helpers import (
    close_kb,
    concise_bot_text,
    is_admin,
    is_owner,
    mention,
    parse_duration,
    require_admin, require_owner, require_right,
    resolve_user,
    safe_restrict,
    bot_can,
)
from ghelp.moderation_reply import (
    action_text,
    failure_text,
    send_reply,
    user_label,
)
from ghelp.plugins.gtools import _bot_can_pin

logger = logging.getLogger(__name__)


def _name(user: types.User) -> str:
    return mention(user.id, html.escape(user.first_name or "user"))


def _rpc_succeeded(result) -> bool:
    """Telegram moderation methods return a truthy result on success."""
    return bool(result)


@autodel_reply
async def _reply(message: types.Message, text: str, **kwargs):
    return await send_reply(message, text, client=app, **kwargs)


async def _link_notice(message: types.Message, text: str, **kwargs):
    """Send a link-warning notice independently of the deleted trigger."""
    return await app.send_message(
        message.chat.id,
        concise_bot_text(text),
        parse_mode=enums.ParseMode.HTML,
        **kwargs,
    )


async def _target(message: types.Message) -> types.User | None:
    user = await resolve_user(message)
    if not user:
        await _reply(message, "Warning: Reply to a user or provide a valid user.")
        return None
    try:
        me = await app.get_me()
        protected = (
            user.id == me.id
            or user.id == config.OWNER_ID
            or await is_admin(message.chat.id, user.id)
        )
    except Exception:
        await _reply(message, f"Warning: Could not check {user_label(user)}.")
        return None
    if protected:
        await _reply(message, f"Warning: I cannot act on {user_label(user)}.")
        return None
    return user


async def _prime_moderation_peers(
    message: types.Message, user: types.User
) -> tuple[object, object] | bool:
    """Resolve RPC peers before the final permission check.

    Pyrogram's convenience moderation methods resolve both peers internally.
    That lookup can otherwise happen after the last actor-right check and
    leave a demotion window before the actual request is sent.  A disconnected
    client is common in offline handler tests; the native call is already
    replaced there, so there is no peer lookup to prime.
    """
    if not getattr(app, "is_connected", False):
        return True
    try:
        chat_peer = await app.resolve_peer(message.chat.id)
        user_peer = await app.resolve_peer(user.id)
    except Exception:
        return False
    if chat_peer is None or user_peer is None:
        return False
    return chat_peer, user_peer


async def _moderation_preflight(
    message: types.Message,
    user: types.User,
    *,
    actor_privilege: str | None = "can_restrict_members",
    actor_label: str = "Ban users",
    bot_privileges: tuple[str, ...] = ("can_restrict_members",),
) -> bool:
    """Fresh bot/target checks followed by the final actor-right check.

    This helper intentionally performs the actor lookup last.  Callers must
    invoke the moderation RPC immediately after a successful return: there
    must be no reply, status lookup, or other await in between.
    """
    for privilege in bot_privileges:
        if not await bot_can(message.chat.id, privilege):
            return False
    try:
        me = await app.get_me()
        if (
            user.id == me.id
            or user.id == config.OWNER_ID
        ):
            return False
        target_member = await app.get_chat_member(message.chat.id, user.id)
        if target_member.status in (
            enums.ChatMemberStatus.OWNER,
            enums.ChatMemberStatus.ADMINISTRATOR,
        ):
            return False
    except Exception:
        return False
    if actor_privilege is None:
        return True
    return await require_right(message, actor_privilege, actor_label)


async def _ban_rpc(
    message: types.Message,
    user: types.User,
    *,
    seconds: int | None = None,
    actor_privilege: str | None = "can_restrict_members",
) -> bool:
    """Run one ban RPC only after a fresh final moderation preflight."""
    peers = await _prime_moderation_peers(message, user)
    if not peers:
        return False
    if (
        seconds is not None
        and isinstance(peers, tuple)
        and isinstance(peers[0], raw.types.InputPeerChat)
    ):
        # Basic groups expose only DeleteChatUser, which cannot carry an
        # until_date and would silently become a permanent ban.
        return False
    if not await _moderation_preflight(
        message, user, actor_privilege=actor_privilege
    ):
        return False
    # Construct the deadline only after the final preflight.  In particular,
    # do not turn a short delay between authorization and the RPC into
    # Telegram's permanent-ban range.
    kwargs = {}
    if seconds is not None:
        kwargs["until_date"] = datetime.now() + timedelta(seconds=seconds)
    try:
        if isinstance(peers, tuple):
            chat_peer, user_peer = peers
            banned_rights = raw.types.ChatBannedRights(
                until_date=utils.datetime_to_timestamp(
                    kwargs.get("until_date", utils.zero_datetime())
                ),
                view_messages=True,
                send_messages=True,
                send_media=True,
                send_stickers=True,
                send_gifs=True,
                send_games=True,
                send_inline=True,
                embed_links=True,
            )
            if isinstance(chat_peer, raw.types.InputPeerChannel):
                # A bound raw request avoids Pyrogram's implicit peer
                # resolution after the final authorization check.  The raw
                # channel method takes InputChannel, not InputPeerChannel.
                channel = raw.types.InputChannel(
                    channel_id=chat_peer.channel_id,
                    access_hash=chat_peer.access_hash,
                )
                result = await app.invoke(
                    raw.functions.channels.EditBanned(
                        channel=channel,
                        participant=user_peer,
                        banned_rights=banned_rights,
                    ),
                    retries=1,
                    sleep_threshold=0,
                )
            else:
                result = await app.invoke(
                    raw.functions.messages.DeleteChatUser(
                        chat_id=abs(message.chat.id),
                        user_id=user_peer,
                        revoke_history=None,
                    ),
                    retries=1,
                    sleep_threshold=0,
                )
        else:
            result = await app.ban_chat_member(
                message.chat.id, user.id, **kwargs
            )
    except Exception:
        return False
    return _rpc_succeeded(result)


async def _unban_rpc(
    message: types.Message,
    user: types.User,
    *,
    actor_privilege: str | None = "can_restrict_members",
) -> bool:
    """Run one unban RPC only after a fresh final moderation preflight."""
    peers = await _prime_moderation_peers(message, user)
    if not peers:
        return False
    if not await _moderation_preflight(
        message, user, actor_privilege=actor_privilege
    ):
        return False
    if isinstance(peers, tuple) and isinstance(
        peers[0], raw.types.InputPeerChannel
    ):
        try:
            result = await app.invoke(
                raw.functions.channels.EditBanned(
                    channel=raw.types.InputChannel(
                        channel_id=peers[0].channel_id,
                        access_hash=peers[0].access_hash,
                    ),
                    participant=peers[1],
                    banned_rights=raw.types.ChatBannedRights(until_date=0),
                ),
                retries=1,
                sleep_threshold=0,
            )
            if _rpc_succeeded(result):
                return True
            logger.warning(
                "unban raw RPC returned a false result chat=%s user=%s",
                message.chat.id,
                user.id,
            )
        except Exception as exc:
            # A bound raw request can fail when the cached channel/user access
            # hash is stale. Keep the authenticated fallback below: Pyrogram
            # resolves fresh peers for the convenience method.
            logger.warning(
                "unban raw RPC failed chat=%s user=%s error=%s: %s",
                message.chat.id,
                user.id,
                type(exc).__name__,
                exc,
            )
        # Re-check the actor immediately before the fallback. The first RPC
        # may have waited on Telegram, so never let a demoted actor use the
        # second transport path.
        if not await _moderation_preflight(
            message, user, actor_privilege=actor_privilege
        ):
            return False
        try:
            result = await app.unban_chat_member(message.chat.id, user.id)
            if _rpc_succeeded(result):
                logger.info(
                    "unban fallback succeeded chat=%s user=%s",
                    message.chat.id,
                    user.id,
                )
                return True
            logger.warning(
                "unban fallback returned a false result chat=%s user=%s",
                message.chat.id,
                user.id,
            )
        except Exception as exc:
            logger.warning(
                "unban fallback failed chat=%s user=%s error=%s: %s",
                message.chat.id,
                user.id,
                type(exc).__name__,
                exc,
            )
        return False
    try:
        result = await app.unban_chat_member(message.chat.id, user.id)
    except Exception as exc:
        logger.warning(
            "unban convenience RPC failed chat=%s user=%s error=%s: %s",
            message.chat.id,
            user.id,
            type(exc).__name__,
            exc,
        )
        return False
    return _rpc_succeeded(result)


async def _delete_reply_rpc(
    message: types.Message,
    user: types.User,
    reply: types.Message,
) -> bool:
    """Delete an exact, still-current reply after fresh cleanup checks."""
    current = getattr(message, "reply_to_message", None)
    reply_chat = getattr(getattr(reply, "chat", None), "id", None)
    sender = getattr(reply, "from_user", None)
    if (
        current is not reply
        or reply_chat != message.chat.id
        or sender is None
        or sender.id != user.id
    ):
        return False
    # The moderation RPC may already have removed the target from the group.
    # Re-running the target membership lookup here would reject the cleanup
    # with UserNotParticipant even though the exact replied-to message is
    # still deletable. Re-check only the permissions that can change while
    # the ban RPC is in flight.
    if (
        not await bot_can(message.chat.id, "can_delete_messages")
        or not await require_right(
            message, "can_delete_messages", "Delete messages"
        )
    ):
        return False
    # Keep the identity and ownership check immediately adjacent to the
    # deletion.  The final actor check above is the last await before it.
    current = getattr(message, "reply_to_message", None)
    if (
        current is not reply
        or getattr(getattr(reply, "chat", None), "id", None)
        != message.chat.id
        or getattr(getattr(reply, "from_user", None), "id", None)
        != user.id
    ):
        return False
    try:
        result = await reply.delete()
    except Exception as exc:
        logger.warning(
            "post-moderation reply deletion failed chat=%s message=%s "
            "user=%s error=%s: %s",
            message.chat.id,
            getattr(reply, "id", None),
            user.id,
            type(exc).__name__,
            exc,
        )
        return False
    return _rpc_succeeded(result)


def _muted_permissions() -> types.ChatPermissions:
    return types.ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )


def _member_permissions() -> types.ChatPermissions:
    return types.ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )


def _command_reason(message: types.Message) -> str:
    """Return text after the target, whether target came from a reply or arg."""
    by_reply = bool(
        message.reply_to_message and message.reply_to_message.from_user
    )
    parts = message.command[1:] if by_reply else message.command[2:]
    return " ".join(parts).strip()


# ``/delete all`` is a native full-history command owned by bulk_purge.  Keep
# every spelling of its reserved prefix out of the ordinary single-message
# route, including malformed forms that the native handler intentionally
# rejects.  Otherwise a reply-bearing ``/delete all ...`` could fall through
# here and delete the replied-to message.
_RESERVED_DELETE_ALL_RE = re.compile(
    r"^(?:[/?!])delete(?:@[A-Za-z0-9_]+)?\s+all"
    r"(?:@[A-Za-z0-9_]+)?(?:\s|$)",
    re.IGNORECASE,
)
_ORDINARY_DELETE_FILTER = (
    filters.group
    & filters.command(["del", "delete"], prefixes=["/", "?", "!", "."])
    & ~filters.regex(_RESERVED_DELETE_ALL_RE)
)


@app.on_message(filters.group & filters.command("ban", prefixes=["/", "?", "!", "."]))
async def ban_user(_, message: types.Message):
    if not await require_right(message, "can_restrict_members", "Ban users"):
        return
    user = await _target(message)
    if not user:
        return
    reason = _command_reason(message)
    succeeded = await _ban_rpc(message, user)
    if succeeded:
        text = action_text(message, "Banned", user, reason=reason)
    else:
        text = failure_text(message, "ban", user, reason=reason)
    await _reply(message, text)


@app.on_message(filters.group & filters.command("unban", prefixes=["/", "?", "!", "."]))
async def unban_user(_, message: types.Message):
    if not await require_right(message, "can_restrict_members", "Ban users"):
        return
    user = await _target(message)
    if not user:
        return
    reason = _command_reason(message)
    succeeded = await _unban_rpc(message, user)
    if succeeded:
        text = action_text(message, "Unbanned", user, reason=reason)
    else:
        text = failure_text(message, "unban", user, reason=reason)
    await _reply(message, text)


@app.on_message(filters.group & filters.command("kick", prefixes=["/", "?", "!", "."]))
async def kick_user(_, message: types.Message):
    if not await require_right(message, "can_restrict_members", "Ban users"):
        return
    user = await _target(message)
    if not user:
        return
    reason = _command_reason(message)
    ban_succeeded = await _ban_rpc(message, user)
    if not ban_succeeded:
        await _reply(message, failure_text(message, "kick", user, reason=reason))
        return
    unban_succeeded = await _unban_rpc(message, user)
    if unban_succeeded:
        text = action_text(message, "Kicked", user, reason=reason)
    else:
        text = failure_text(
            message,
            "kick",
            user,
            reason=reason,
            detail="Ban was applied, but unban failed; the target remains banned.",
        )
    await _reply(message, text)


@app.on_message(filters.group & filters.command("mute", prefixes=["/", "?", "!", "."]))
async def mute_user(_, message: types.Message):
    if not await require_right(message, "can_restrict_members", "Ban users"):
        return
    user = await _target(message)
    if not user:
        return
    # /mute [10m|2h|1d] [reason]  (reply)  or  /mute @user [10m|2h|1d] [reason]
    # The first extra word is a duration only if it parses as one; anything
    # else (e.g. "/mute spam") is treated as the reason of a permanent mute.
    # same predicate as helpers.resolve_user: a reply without from_user
    # (channel post) means the target came from the first argument instead
    by_reply = bool(message.reply_to_message and message.reply_to_message.from_user)
    extra = message.command[1:] if by_reply else message.command[2:]
    duration_arg = None
    seconds = None
    if extra:
        seconds = parse_duration(extra[0])
        if seconds:
            duration_arg = extra[0]
            extra = extra[1:]
    reason = " ".join(extra).strip()
    until = datetime.now() + timedelta(seconds=seconds) if seconds else None
    try:
        ok = (
            await safe_restrict(message.chat.id, user.id, _muted_permissions())
            if until is None
            else await _restrict_until(message.chat.id, user.id, until)
        )
    except Exception:
        ok = False
    if ok:
        # _restrict_until/safe_restrict already make this best effort.  Keep
        # it outside the action result so bookkeeping cannot rewrite success.
        try:
            await _untrack_fsub(message.chat.id, user.id)
        except Exception:
            pass
        await _reply(
            message,
            action_text(
                message,
                "Muted",
                user,
                reason=reason,
                duration=duration_arg or "",
            ),
        )
    else:
        await _reply(
            message,
            failure_text(
                message,
                "mute",
                user,
                reason=reason,
                duration=duration_arg or "",
            ),
        )


async def _restrict_until(chat_id: int, user_id: int, until: datetime) -> bool:
    try:
        result = await app.restrict_chat_member(
            chat_id, user_id, _muted_permissions(), until_date=until
        )
        if result is False:
            return False
        await _untrack_fsub(chat_id, user_id)
        return True
    except Exception:
        return False


async def _untrack_fsub(chat_id: int, user_id: int) -> None:
    """A moderation mute/unmute supersedes any force-sub restriction."""
    try:
        from ghelp.plugins.forcesub import untrack_fsub
        await untrack_fsub(chat_id, user_id)
    except Exception:
        pass


_WARN_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}
_WARN_SEEN_MAX = 4096
_WARN_SEEN_TTL = 7 * 86400


def warn_lock(chat_id: int, user_id: int) -> asyncio.Lock:
    """Return the serialisation lock for one user's warning state."""
    key = (chat_id, user_id)
    lock = _WARN_LOCKS.get(key)
    if lock is None:
        lock = _WARN_LOCKS[key] = asyncio.Lock()
    return lock


async def _warning_member(chat_id: int, user_id: int):
    """Fetch a target's live member record, or None on any lookup failure."""
    try:
        return await app.get_chat_member(chat_id, user_id)
    except Exception:
        return None


async def _warning_target(
    message: types.Message,
    user: types.User,
    settings: dict,
    *,
    link_violation: bool = False,
) -> tuple[bool, bool]:
    """Check target identity/protection and return (allowed, is_admin).

    Warning paths intentionally do not trust the User object alone: Telegram
    role and bot identity are looked up live.  Admin targets are only
    eligible for owner-authorized warning mode, or for the link-deleter's
    explicitly enabled named-admin mode.
    """
    if not user or not getattr(user, "id", None) or getattr(user, "is_bot", False):
        return False, False
    try:
        me = await app.get_me()
    except Exception:
        return False, False
    if user.id == getattr(me, "id", None):
        return False, False
    if config.OWNER_ID and user.id == config.OWNER_ID:
        return False, False
    member = await _warning_member(message.chat.id, user.id)
    if member is None:
        return False, False
    status = getattr(member, "status", None)
    if status == enums.ChatMemberStatus.OWNER:
        return False, False
    if status == enums.ChatMemberStatus.ADMINISTRATOR:
        if link_violation:
            return bool(settings.get("linkdel_admins")), True
        caller_owner = bool(
            message.from_user
            and await is_owner(message.chat.id, message.from_user.id)
        )
        return bool(settings.get("linkdel_admins") and caller_owner), True
    # Only known, non-privileged membership records are actionable.  A
    # missing/unknown status is treated as a failed identity lookup.
    known = {
        enums.ChatMemberStatus.MEMBER,
        enums.ChatMemberStatus.RESTRICTED,
        enums.ChatMemberStatus.LEFT,
        enums.ChatMemberStatus.BANNED,
    }
    if status not in known:
        return False, False
    return True, False


async def _warn_target(message: types.Message) -> types.User | None:
    """Resolve a warning target while allowing admins only in owner mode."""
    user = await resolve_user(message)
    if not user:
        await _reply(message, "Warning: Reply to a user or provide a valid user.")
        return None
    try:
        settings = await db.get_chat(message.chat.id)
    except Exception:
        settings = {}
    allowed, _ = await _warning_target(message, user, settings)
    if not allowed:
        await _reply(message, f"Warning: I cannot warn {_name(user)}.")
        return None
    return user


async def require_warn_reset(message: types.Message) -> bool:
    """Use owner authorization for resets while link counting is enabled."""
    try:
        settings = await db.get_chat(message.chat.id)
    except Exception:
        # A failed read must not fall back to the weaker admin reset gate:
        # link mode may be enabled in the durable record we could not read.
        return await require_owner(message)
    if settings.get("linkdel_on", False):
        return await require_owner(message)
    return await require_admin(message, "can_restrict_members", "Ban users")


async def warn_reset_denied(message: types.Message) -> None:
    """Plain-text denial used when link mode changes during a reset."""
    try:
        await message.reply_text(
            concise_bot_text(
                "This command is for the bot owner or group owner only."
            )
        )
    except Exception:
        pass


def _warning_count(warns: dict, user_id: int) -> int:
    raw = warns.get(str(user_id), warns.get(user_id, 0))
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _revision(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _warn_time(settings: dict) -> int:
    try:
        seconds = int(settings.get("warn_time", 86400))
    except (TypeError, ValueError):
        seconds = 86400
    return seconds if 60 <= seconds <= 30 * 86400 else 86400


async def _current_link_policy(
    chat_id: int,
    committed_revision: int,
    *,
    require_admin_mode: bool = False,
) -> dict | None:
    """Read link policy after an awaited Telegram operation."""
    try:
        current = await db.get_chat(chat_id)
    except Exception:
        return None
    if not current.get("linkdel_on", False):
        return None
    if _revision(current.get("linkdel_revision", 0)) != committed_revision:
        return None
    if require_admin_mode and not current.get("linkdel_admins", False):
        return None
    return current


def _migrate_seen(raw, now: float) -> dict[str, float]:
    """Prune/migrate the durable message ledger to bounded timestamps."""
    if not isinstance(raw, dict):
        raw = {}
    seen: dict[str, float] = {}
    cutoff = now - _WARN_SEEN_TTL
    for key, value in raw.items():
        # Older builds stored True.  Treat those entries as just-seen during
        # migration so a restart cannot immediately double-warn a message.
        if isinstance(value, bool):
            timestamp = now
        else:
            try:
                timestamp = float(value)
            except (TypeError, ValueError):
                continue
        if timestamp >= cutoff:
            seen[str(key)] = timestamp
    while len(seen) > _WARN_SEEN_MAX:
        seen.pop(next(iter(seen)))
    return seen


def _empty_privileges() -> types.ChatPrivileges:
    """Build an explicit all-False privilege set for admin demotion."""
    params = inspect.signature(types.ChatPrivileges).parameters
    kwargs = {
        name: False
        for name, parameter in params.items()
        if name != "self"
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return types.ChatPrivileges(**kwargs)


async def _demote_for_warning(
    chat_id: int,
    user_id: int,
    *,
    committed_revision: int | None = None,
) -> tuple[bool, str]:
    """Demote an editable admin only when every live permission check passes."""
    member = await _warning_member(chat_id, user_id)
    if member is None:
        return False, "lookup"
    if getattr(member, "status", None) != enums.ChatMemberStatus.ADMINISTRATOR:
        return True, "not_admin"
    if not getattr(member, "can_be_edited", False):
        return False, "not_editable"
    if not await bot_can(chat_id, "can_promote_members"):
        return False, "missing_promote"
    if not await bot_can(chat_id, "can_restrict_members"):
        return False, "missing_restrict"
    if committed_revision is not None:
        # Permission/role checks above are awaited calls.  Re-read policy
        # immediately before EditAdmin so owner revocation wins the race.
        if await _current_link_policy(
            chat_id, committed_revision, require_admin_mode=True
        ) is None:
            return False, "policy_revoked"
    try:
        result = await app.promote_chat_member(
            chat_id, user_id, privileges=_empty_privileges()
        )
        if result is False:
            return False, "demote_failed"
    except Exception:
        return False, "demote_failed"
    # Telegram can accept a demotion request while the role update is still
    # propagating; do not mute until a fresh lookup confirms it is no longer
    # an administrator.
    member = await _warning_member(chat_id, user_id)
    if member is None:
        return False, "lookup_after_demote"
    if getattr(member, "status", None) in (
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.OWNER,
    ):
        return False, "still_admin"
    return True, "demoted"


async def _punish_warning(
    message: types.Message,
    user: types.User,
    settings: dict,
    *,
    action: str,
    link_violation: bool,
    committed_revision: int | None,
    mute_seconds: int,
) -> tuple[bool, str]:
    """Perform the threshold action and report enough detail for the reply."""
    target = await _warning_member(message.chat.id, user.id)
    if target is None:
        return False, "lookup"
    status = getattr(target, "status", None)
    if status == enums.ChatMemberStatus.OWNER:
        return False, "protected"
    if status == enums.ChatMemberStatus.ADMINISTRATOR:
        if not settings.get("linkdel_admins"):
            return False, "admin_not_enabled"
        demoted, detail = await _demote_for_warning(
            message.chat.id,
            user.id,
            committed_revision=committed_revision if link_violation else None,
        )
        if not demoted:
            return False, detail if detail == "policy_revoked" else "admin_uneditable"
        if detail == "demoted":
            demotion_note = "demoted"
        else:
            demotion_note = ""
    elif status not in (
        enums.ChatMemberStatus.MEMBER,
        enums.ChatMemberStatus.RESTRICTED,
        enums.ChatMemberStatus.LEFT,
        enums.ChatMemberStatus.BANNED,
    ):
        return False, "lookup"
    else:
        demotion_note = ""

    if link_violation:
        current = await _current_link_policy(
            message.chat.id,
            committed_revision if committed_revision is not None else 0,
            require_admin_mode=bool(demotion_note),
        )
        if current is None:
            return (
                False,
                "demoted_policy_revoked" if demotion_note else "policy_revoked",
            )
        mute_seconds = _warn_time(current)

    try:
        if action == "ban":
            result = await app.ban_chat_member(message.chat.id, user.id)
            if result is False:
                raise RuntimeError
        elif action == "kick":
            result = await app.ban_chat_member(message.chat.id, user.id)
            if result is False:
                raise RuntimeError
            result = await app.unban_chat_member(message.chat.id, user.id)
            if result is False:
                raise RuntimeError
        else:
            until = datetime.now() + timedelta(seconds=mute_seconds)
            if not await _restrict_until(message.chat.id, user.id, until):
                raise RuntimeError
            action = "mute"
    except Exception:
        return False, "demoted_mute_failed" if demotion_note else "action_failed"
    return True, "demoted" if demotion_note else action


@app.on_message(filters.group & filters.command("unmute", prefixes=["/", "?", "!", "."]))
async def unmute_user(_, message: types.Message):
    if not await require_right(message, "can_restrict_members", "Ban users"):
        return
    user = await _target(message)
    if not user:
        return
    reason = _command_reason(message)
    try:
        ok = await safe_restrict(
            message.chat.id, user.id, _member_permissions()
        )
    except Exception:
        ok = False
    if ok:
        try:
            await _untrack_fsub(message.chat.id, user.id)
        except Exception:
            pass
        await _reply(
            message,
            action_text(message, "Unmuted", user, reason=reason),
        )
    else:
        await _reply(
            message,
            failure_text(message, "unmute", user, reason=reason),
        )


async def apply_warn(
    message: types.Message,
    user: types.User,
    reason: str = "",
    quiet: bool = False,
    *,
    link_violation: bool = False,
) -> bool:
    """Record one warning and perform the configured threshold action.

    Link violations use a durable chat/message-id ledger and the fixed
    3-warning mute policy.  Every mutation is serialized both per target and
    through ``db.mutate_chat``; a failed punishment leaves the threshold
    count in place rather than claiming success.
    """
    chat_id = message.chat.id
    message_id = getattr(message, "id", None)
    try:
        settings = await db.get_chat(chat_id)
    except Exception:
        settings = None
    if settings is None:
        if not quiet:
            await (_link_notice if link_violation else _reply)(
                message, f"Warning: Could not warn {_name(user)}."
            )
        return False
    if link_violation and not settings.get("linkdel_on", False):
        return False
    allowed, _ = await _warning_target(
        message, user, settings, link_violation=link_violation
    )
    if not allowed:
        if not quiet and not link_violation:
            await _reply(message, f"Warning: I cannot warn {_name(user)}.")
        return False
    # A missing message id cannot be safely deduplicated.  This is only
    # relevant to the link watcher; ordinary /warn calls have no such need.
    if link_violation and message_id is None:
        return False

    reason_line = f"\n<b>Reason:</b> {html.escape(reason)}" if reason else ""
    lock = warn_lock(chat_id, user.id)
    outcome: dict = {}
    async with lock:
        def record(cfg):
            if link_violation and not cfg.get("linkdel_on", False):
                outcome["disabled"] = True
                return
            if link_violation:
                seen = _migrate_seen(cfg.get("warn_link_seen"), time.time())
                event_key = str(message_id)
                if event_key in seen:
                    cfg["warn_link_seen"] = seen
                    outcome["duplicate"] = True
                    return
                seen[event_key] = time.time()
                while len(seen) > _WARN_SEEN_MAX:
                    seen.pop(next(iter(seen)))
                cfg["warn_link_seen"] = seen
            warns = dict(cfg.get("warns") or {})
            previous = _warning_count(warns, user.id)
            try:
                limit = 3 if link_violation else int(cfg.get("warn_limit", 3))
            except (TypeError, ValueError):
                limit = 3
            action = "mute" if link_violation else str(
                cfg.get("warn_action", "mute")
            ).lower()
            if action not in ("mute", "kick", "ban"):
                action = "mute"
            if limit > 0:
                count = min(previous + 1, limit)
            else:
                count = previous + 1
            warns[str(user.id)] = count
            cfg["warns"] = warns
            outcome["count"] = count
            outcome["limit"] = limit
            outcome["action"] = action
            outcome["mute_seconds"] = _warn_time(cfg)
            outcome["revision"] = _revision(cfg.get("linkdel_revision", 0))

        try:
            updated = await db.mutate_chat(chat_id, record)
        except Exception:
            if not quiet:
                await (_link_notice if link_violation else _reply)(
                    message, f"Warning: Could not warn {_name(user)}."
                )
            return False
        if outcome.get("duplicate"):
            return True
        if outcome.get("disabled"):
            return False
        if isinstance(updated, dict):
            settings.update({
                key: updated[key]
                for key in (
                    "warn_time", "warn_action", "linkdel_admins",
                    "linkdel_on", "linkdel_revision",
                )
                if key in updated
            })
        count = int(outcome["count"])
        limit = int(outcome["limit"])
        action = str(outcome["action"])
        mute_seconds = int(outcome["mute_seconds"])
        committed_revision = int(outcome["revision"])
        if limit <= 0 or count < limit:
            if not quiet:
                if link_violation:
                    await _link_notice(
                        message,
                        f"Link deleted, <b>{count}/3</b> warnings.",
                        reply_markup=close_kb(),
                    )
                else:
                    await _reply(
                        message,
                        f"Warning: {_name(user)} warned — "
                        f"<b>{count}/{'unlimited' if limit <= 0 else limit}</b>."
                        f"{reason_line}",
                        reply_markup=close_kb(),
                    )
            return True

        # Show the committed warning before slower role/demotion/mute RPCs.
        # This is sent only after Link Deleter has confirmed deletion. It must
        # never claim the mute succeeded before Telegram confirms it.
        progress_notice = None
        if link_violation and not quiet:
            try:
                progress_notice = await _link_notice(
                    message,
                    f"Link deleted. {_name(user)} — <b>{count}/3</b> warnings. "
                    "Checking permissions and applying the mute…",
                    reply_markup=close_kb(),
                )
            except Exception:
                # Notification failure must not cancel the moderation action.
                pass

        async def link_result(trigger, text, **kwargs):
            if progress_notice is not None:
                try:
                    result = await progress_notice.edit_text(
                        concise_bot_text(text), parse_mode=enums.ParseMode.HTML, **kwargs
                    )
                    if result is False:
                        raise RuntimeError("Telegram did not update the warning")
                    return result
                except Exception:
                    pass
            return await _link_notice(trigger, text, **kwargs)

        # Fetch role/permissions again at the threshold.  The target may have
        # been promoted after the first identity check.
        success, detail = await _punish_warning(
            message,
            user,
            settings,
            action=action,
            link_violation=link_violation,
            committed_revision=committed_revision if link_violation else None,
            mute_seconds=mute_seconds,
        )
        if not success:
            if not quiet:
                if link_violation and detail == "admin_uneditable":
                    await link_result(
                        message,
                        "link deleted,3/3 warnings; cannot mute admin, "
                        "owner must demote/mute",
                        reply_markup=close_kb(),
                    )
                elif link_violation and detail == "demoted_mute_failed":
                    await link_result(
                        message,
                        "link deleted,3/3 warnings; rights removed but "
                        "mute failed",
                        reply_markup=close_kb(),
                    )
                elif link_violation and detail == "demoted_policy_revoked":
                    await link_result(
                        message,
                        "link deleted,3/3 warnings; rights removed, "
                        "owner revoked link warnings before mute",
                        reply_markup=close_kb(),
                    )
                elif link_violation and detail == "policy_revoked":
                    await link_result(
                        message,
                        "link deleted,3/3 warnings; link warning policy "
                        "changed, mute not applied",
                        reply_markup=close_kb(),
                    )
                else:
                    await (link_result if link_violation else _reply)(
                        message,
                        (
                            f"Link deleted, <b>{count}/3</b> warnings; "
                            "could not apply the warning action."
                            if link_violation
                            else f"Warning: Could not warn {_name(user)}."
                        ),
                        reply_markup=close_kb() if link_violation else None,
                    )
            return False
        try:
            def reset(cfg):
                warns = dict(cfg.get("warns") or {})
                warns[str(user.id)] = 0
                cfg["warns"] = warns

            await db.mutate_chat(chat_id, reset)
        except Exception:
            if not quiet:
                if link_violation:
                    await link_result(
                        message,
                        "Link deleted, 3/3 warnings; the mute was applied, "
                        "but the warning counter could not be reset.",
                        reply_markup=close_kb(),
                    )
                else:
                    await _reply(message, f"Warning: Could not record {_name(user)}.")
            return False
        if not quiet:
            if link_violation:
                if detail == "demoted":
                    text = "link deleted,3/3 warnings; rights removed and user muted"
                else:
                    text = "link deleted,3/3 warnings; user muted"
                await link_result(message, text, reply_markup=close_kb())
            else:
                action_word = {
                    "ban": "banned",
                    "kick": "kicked",
                    "mute": "muted",
                }[action]
                await _reply(
                    message,
                    f"Done: {_name(user)} reached <b>{limit}/{limit}</b> "
                    f"warnings and was <b>{action_word}</b>.{reason_line}",
                    reply_markup=close_kb(),
                )
        return True


@app.on_message(filters.group & filters.command("warn", prefixes=["/", "?", "!", "."]))
async def warn_user(_, message: types.Message):
    if not await require_admin(message, "can_restrict_members", "Ban users"):
        return
    user = await _warn_target(message)
    if not user:
        return
    reason_parts = (
        message.command[1:]
        if message.reply_to_message
        else message.command[2:]
    )
    await apply_warn(message, user, " ".join(reason_parts).strip())


@app.on_message(filters.group & filters.command("unwarn", prefixes=["/", "?", "!", "."]))
async def unwarn_user(_, message: types.Message):
    if not await require_warn_reset(message):
        return
    user = await _warn_target(message)
    if not user:
        return
    try:
        async with warn_lock(message.chat.id, user.id):
            caller_is_owner = bool(
                message.from_user
                and await is_owner(message.chat.id, message.from_user.id)
            )
            outcome = {}

            def reset(cfg):
                if cfg.get("linkdel_on", False) and not caller_is_owner:
                    outcome["denied"] = True
                    return
                warns = dict(cfg.get("warns") or {})
                warns[str(user.id)] = 0
                cfg["warns"] = warns

            await db.mutate_chat(message.chat.id, reset)
        if outcome.get("denied"):
            await warn_reset_denied(message)
            return
        await _reply(message, f"Done: Reset warnings for {_name(user)}.")
    except Exception:
        await _reply(message, f"Warning: Could not reset warnings for {_name(user)}.")


@app.on_message(filters.group & filters.command("warns", prefixes=["/", "?", "!", "."]))
async def show_warns(_, message: types.Message):
    if not await require_admin(message, "can_restrict_members", "Ban users"):
        return
    user = await _warn_target(message)
    if not user:
        return
    try:
        settings = await db.get_chat(message.chat.id)
        count = int(settings.get("warns", {}).get(str(user.id), 0))
        limit = int(settings.get("warn_limit", 3))
        shown_limit = str(limit) if limit > 0 else "unlimited"
        await _reply(
            message,
            f"Warning: {_name(user)} has <b>{count}/{shown_limit}</b> warnings.",
            reply_markup=close_kb(),
        )
    except Exception:
        await _reply(message, f"Warning: Could not read warnings for {_name(user)}.")


@app.on_message(_ORDINARY_DELETE_FILTER)
async def delete_message(_, message: types.Message):
    # Keep the defensive check in the router as well as in its registration
    # filter: direct/local invocation must not turn malformed reserved native
    # commands into ordinary deletion.
    if _RESERVED_DELETE_ALL_RE.match(getattr(message, "text", "") or ""):
        return
    if not await require_right(message, "can_delete_messages", "Delete messages"):
        return
    if not message.reply_to_message:
        await _reply(message, "Warning: Reply to the message to delete.")
        return
    try:
        await app.delete_messages(
            message.chat.id, [message.reply_to_message.id, message.id]
        )
    except Exception:
        await _reply(message, "Warning: Could not delete that message.")


@app.on_message(filters.group & filters.command("pin", prefixes=["/", "?", "!", "."]))
async def pin_message(_, message: types.Message):
    if not await require_right(message, "can_pin_messages", "Pin messages"):
        return
    if not message.reply_to_message:
        await _reply(message, "Warning: Reply to the message to pin.")
        return
    bot_can_pin = await _bot_can_pin(message.chat.id)
    if bot_can_pin is None:
        await _reply(message, "Warning: Could not verify the bot's Pin Messages right.")
        return
    if not bot_can_pin:
        await _reply(message, "Warning: The bot needs the Pin Messages right.")
        return
    try:
        result = await app.pin_chat_message(
            message.chat.id,
            message.reply_to_message.id,
            disable_notification=True,
        )
        if result is False:
            await _reply(message, "Warning: Could not pin that message.")
            return
        await _reply(message, "Done: Message pinned.")
    except Exception:
        await _reply(message, "Warning: Could not pin that message.")


@app.on_message(filters.group & filters.command("unpin", prefixes=["/", "?", "!", "."]))
async def unpin_message(_, message: types.Message):
    if not await require_right(message, "can_pin_messages", "Pin messages"):
        return
    bot_can_pin = await _bot_can_pin(message.chat.id)
    if bot_can_pin is None:
        await _reply(message, "Warning: Could not verify the bot's Pin Messages right.")
        return
    if not bot_can_pin:
        await _reply(message, "Warning: The bot needs the Pin Messages right.")
        return
    try:
        if message.reply_to_message:
            result = await app.unpin_chat_message(
                message.chat.id, message.reply_to_message.id
            )
        else:
            result = await app.unpin_chat_message(message.chat.id)
        if result is False:
            await _reply(message, "Warning: Could not unpin the message.")
            return
        await _reply(message, "Done: Message unpinned.")
    except Exception:
        await _reply(message, "Warning: Could not unpin the message.")


@app.on_message(filters.group & filters.command("purge", prefixes=["/", "?", "!", "."]))
async def purge_messages(_, message: types.Message):
    if not await require_right(message, "can_delete_messages", "Delete messages"):
        return
    if not message.reply_to_message:
        await _reply(message, "Warning: Reply to the first message to purge.")
        return
    first = message.reply_to_message.id
    if first > message.id or message.id - first + 1 > 500:
        await _reply(message, "Warning: Purge range must contain at most 500 messages.")
        return
    ids = list(range(first, message.id + 1))
    deleted = 0
    try:
        for offset in range(0, len(ids), 100):
            result = await app.delete_messages(
                message.chat.id, ids[offset:offset + 100]
            )
            deleted += int(result or 0)
        report = await app.send_message(
            message.chat.id,
            f"Done: Purged <b>{deleted}</b> messages.",
            parse_mode=enums.ParseMode.HTML,
        )
        await asyncio.sleep(4)
        await report.delete()
    except Exception:
        try:
            await app.send_message(
                message.chat.id,
                "Warning: Could not complete the purge.",
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            pass