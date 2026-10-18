"""Edit Guardian.

Edit Guardian accepts only an explicit Telegram ``edit_date`` event.  This keeps
reaction-only updates from being treated as content edits while still covering
messages edited days after they were originally posted.

The short lived jobs in this module are intentionally in memory only.  This
means that a process restart cannot resume a destructive action from an old
process.  Every job also repeats the policy, identity, permission, and
deadline checks immediately before the delete RPC.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from pyrogram import enums, filters, raw, types, utils

from ghelp import app
from ghelp.database import db

logger = logging.getLogger(__name__)

_MIN_WINDOW, _MAX_WINDOW = 5, 86400
_DEFAULT_WINDOW = 60
_NOTICE_TTL = 5
_CONSENT_TTL = 300
_RPC_TIMEOUT = 15
_OVERDUE_GRACE = 5
_PRE = ["/", "?", "!", "."]
_GROUP_TYPES = (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)


@dataclass
class _Consent:
    chat_id: int
    user_id: int
    message_id: int
    created: float
    window: int
    nonce: str


@dataclass
class _Verification:
    chat_id: int
    message_id: int
    created: float
    args: tuple[str, ...]
    nonce: str


@dataclass
class _Pending:
    chat_id: int
    message_id: int
    token: int
    delay: int
    deadline: float
    settings_revision: int
    fingerprint: tuple[str, str]
    message: Any
    identity: tuple[str, int]
    notice: Any = None
    delete_task: asyncio.Task | None = None
    notice_task: asyncio.Task | None = None


# A prompt is not persisted.  In addition to making prompts harmless after a
# restart, this keeps the callback binding small and makes cancellation
# synchronous with settings changes.
_consents: dict[str, _Consent] = {}
_verifications: dict[str, _Verification] = {}

# One pending delete per edited message.  _generations prevents a task that is
# just waking up from deleting after a newer edit won the race.
_pending: dict[tuple[int, int], _Pending] = {}
_generations: dict[tuple[int, int], int] = {}
_settings_revisions: dict[int, int] = {}
_chat_tasks: dict[int, set[asyncio.Task]] = {}
_notice_tasks: set[asyncio.Task] = set()
_CONTENT_CACHE_LIMIT = 20_000
_content_cache: OrderedDict[tuple[int, int], str] = OrderedDict()

# Compatibility/readability aliases useful to offline callers inspecting the
# runtime state.  They are aliases, not additional state.
_pending_deletes = _pending
_confirmations = _consents


def _group(message: Any) -> bool:
    chat = getattr(message, "chat", None)
    return bool(chat and getattr(chat, "type", None) in _GROUP_TYPES)


def _event_timestamp(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    stamp = getattr(value, "timestamp", None)
    try:
        return float(stamp() if callable(stamp) else value)
    except (TypeError, ValueError, OverflowError):
        return None


def _actual_content_edit(message: Any) -> bool:
    """Reject reaction/update wrappers that do not carry an edit time."""
    return _event_timestamp(getattr(message, "edit_date", None)) is not None


def _content_hash(message: Any) -> str:
    return _edit_fingerprint(message)[1]


def _media_kind(message: Any) -> str:
    for field in _MEDIA_FIELDS:
        if getattr(message, field, None) is not None:
            return field
    return ""


def _media_unique(message: Any) -> str:
    for field in _MEDIA_FIELDS:
        value = getattr(message, field, None)
        if value is None:
            continue
        return str(getattr(value, "file_unique_id", None)
                   or getattr(value, "id", "") or "")
    return ""


def _content_key(message: Any) -> str:
    """Coarse identity of what was posted — media kind, media id, text,
    caption.  A reaction/pin/view change alters none of these, so this gate
    cannot be defeated by SDK serialisation drift (2026-09-29)."""
    return "\x1f".join((
        _media_kind(message),
        _media_unique(message),
        str(getattr(message, "text", None) or ""),
        str(getattr(message, "caption", None) or ""),
    ))


_content_keys: "OrderedDict[tuple, str]" = OrderedDict()

def _remember_content(message: Any) -> None:
    if not _group(message):
        return
    key = (message.chat.id, message.id)
    _content_cache[key] = _content_hash(message)
    _content_keys[key] = _content_key(message)
    _content_keys.move_to_end(key)
    _content_cache.move_to_end(key)
    while len(_content_cache) > _CONTENT_CACHE_LIMIT:
        _content_cache.popitem(last=False)


async def _rpc(awaitable, timeout: float | None = None):
    """Bound one standalone Telegram RPC and cancel it on timeout."""
    return await asyncio.wait_for(
        awaitable,
        _RPC_TIMEOUT if timeout is None else timeout,
    )


def _stable_value(value: Any):
    """Reduce SDK objects to identity fields, never message body text."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _stable_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    return {
        name: _stable_value(getattr(value, name, None))
        for name in (
            "type",
            "offset",
            "length",
            "url",
            "user",
            "language",
            "custom_emoji_id",
            "file_unique_id",
            "file_id",
            "id",
            "dc_id",
        )
        if hasattr(value, name)
    }


def _entity_identity(entity: Any):
    if entity is None:
        return None
    return _stable_value(entity)


_MEDIA_FIELDS = (
    "media",
    "photo",
    "video",
    "document",
    "animation",
    "audio",
    "voice",
    "video_note",
    "sticker",
    "contact",
    "location",
    "venue",
    "poll",
    "dice",
    "web_page",
    "game",
    "story",
    "paid_media",
    "giveaway",
    "giveaway_winners",
    "invoice",
)


def _edit_fingerprint(message: Any) -> tuple[str, str]:
    """Return an edit revision without retaining plaintext message content."""
    edit_date = getattr(message, "edit_date", None)
    if hasattr(edit_date, "isoformat"):
        edit_date = edit_date.isoformat()
    elif edit_date is not None:
        edit_date = str(edit_date)
    payload = {
        "text": getattr(message, "text", None),
        "caption": getattr(message, "caption", None),
        "entities": [
            _entity_identity(item)
            for item in (getattr(message, "entities", None) or [])
        ],
        "caption_entities": [
            _entity_identity(item)
            for item in (getattr(message, "caption_entities", None) or [])
        ],
        "media": [
            (field, _stable_value(getattr(message, field, None)))
            for field in _MEDIA_FIELDS
            if getattr(message, field, None) is not None
        ],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return str(edit_date), hashlib.sha256(encoded).hexdigest()


def _window(settings: dict) -> int:
    try:
        value = int(settings.get("editguard_window", _DEFAULT_WINDOW))
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW
    return value if _MIN_WINDOW <= value <= _MAX_WINDOW else _DEFAULT_WINDOW


def _configured_window(settings: dict) -> int | None:
    """Return None for malformed persisted configuration (fail closed)."""
    try:
        value = int(settings.get("editguard_window", _DEFAULT_WINDOW))
    except (TypeError, ValueError):
        return None
    return value if _MIN_WINDOW <= value <= _MAX_WINDOW else None


def _state_text(settings: dict) -> str:
    enabled = bool(settings.get("editguard_on", False))
    admins = bool(settings.get("editguard_admin", False))
    return (
        "<b>Edit Guardian</b>\n"
        f"Status: <b>{'ON' if enabled else 'OFF'}</b> · "
        f"admins: <b>{'yes' if admins else 'no'}</b> · "
        f"delay: <b>{_window(settings)}s</b>\n\n"
        "<code>/editguard on</code> — enable (choose admin coverage)\n"
        "<code>/editguard off</code> — disable\n"
        "<code>/editguard admin on|off</code> — admin coverage\n"
        f"<code>/editguard time {_MIN_WINDOW}-{_MAX_WINDOW}</code> — delay"
    )


def _usage() -> str:
    return (
        "Usage: <code>/editguard</code>, "
        "<code>/editguard on|off</code>, "
        "<code>/editguard admin on|off</code>, or "
        f"<code>/editguard time {_MIN_WINDOW}-{_MAX_WINDOW}</code>."
    )


def _message_command(message: Any) -> list[str]:
    command = getattr(message, "command", None)
    if command:
        return [str(item) for item in command]
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    if not text.startswith(tuple(_PRE)):
        return []
    return text.split()


async def _reply(message: Any, text: str, **kwargs: Any) -> Any:
    """Reply without ever including the edited message's body."""
    try:
        return await _rpc(message.reply_text(text, **kwargs))
    except TypeError:
        # A few small offline Message fakes do not accept quote/parse options.
        kwargs.pop("quote", None)
        kwargs.pop("parse_mode", None)
        return await _rpc(message.reply_text(text, **kwargs))


async def _group_owner(chat_id: int, user_id: int | None) -> bool:
    """Check the actual Telegram group owner, with no configured-owner bypass."""
    if not user_id:
        return False
    direct_status = None
    try:
        member = await _rpc(app.get_chat_member(chat_id, user_id))
        direct_status = getattr(member, "status", None)
        if _owner_status(direct_status):
            return True
    except Exception:
        pass

    # Some Telegram/Kurigram sessions fail a direct peer lookup even though
    # the bot can enumerate current administrators. Verify the exact caller
    # ID against that live list; never grant access merely for being admin.
    listing = getattr(app, "get_chat_members", None)
    if not callable(listing):
        return False

    async def find_live_owner() -> bool:
        async for current in listing(
            chat_id,
            filter=enums.ChatMembersFilter.ADMINISTRATORS,
        ):
            user = getattr(current, "user", None)
            if (
                getattr(user, "id", None) == user_id
                and _owner_status(getattr(current, "status", None))
            ):
                return True
        return False

    try:
        return bool(await _rpc(find_live_owner(), timeout=8))
    except Exception:
        return False


def _owner_status(status: Any) -> bool:
    """Accept owner status shapes returned by Pyrogram/Kurigram versions."""
    if status == enums.ChatMemberStatus.OWNER:
        return True
    value = getattr(status, "value", status)
    name = getattr(status, "name", "")
    normalized = str(value or "").lower().rsplit(".", 1)[-1]
    return normalized in {"owner", "creator"} or str(name).lower() in {
        "owner",
        "creator",
    }


async def _require_group_owner(message: Any) -> bool:
    user = getattr(message, "from_user", None)
    if _group(message) and user and await _group_owner(message.chat.id, user.id):
        return True
    try:
        if _group(message) and user is None:
            text = (
                "I can't identify you while anonymous admin mode is enabled. "
                "Send this command as your personal account; only the live "
                "Telegram group owner can change Edit Guardian."
            )
        else:
            text = "Only the live Telegram group owner can change Edit Guardian."
        await _reply(message, text)
    except Exception:
        pass
    return False


async def _bot_can_delete(chat_id: int) -> tuple[bool, str]:
    """Return a user-facing reason, without leaking RPC details."""
    try:
        me = getattr(app, "me", None) or await _rpc(app.get_me())
        member = await _rpc(app.get_chat_member(chat_id, me.id))
    except Exception:
        return False, "I couldn't verify my Delete messages permission. Try again."
    if getattr(member, "status", None) != enums.ChatMemberStatus.ADMINISTRATOR:
        return False, "I need to be an administrator with Delete messages permission."
    privileges = getattr(member, "privileges", None)
    if not getattr(privileges, "can_delete_messages", False):
        return False, "Give me the Telegram Delete messages permission before enabling Edit Guardian."
    return True, ""


async def _mutate_chat(chat_id: int, fn) -> dict:
    """Use the real atomic store, while remaining friendly to tiny test DBs."""
    mutate = getattr(db, "mutate_chat", None)
    if mutate is not None:
        return await mutate(chat_id, fn)
    settings = await db.get_chat(chat_id)
    fn(settings)
    return await db.update_chat(chat_id, **settings)


def _discard_done(task: asyncio.Task) -> None:
    for tasks in (_chat_tasks,):
        for chat_id, pending in list(tasks.items()):
            pending.discard(task)
            if not pending:
                tasks.pop(chat_id, None)
    _notice_tasks.discard(task)


def _track(task: asyncio.Task, chat_id: int, *, notice: bool = False) -> asyncio.Task:
    _chat_tasks.setdefault(chat_id, set()).add(task)
    if notice:
        _notice_tasks.add(task)
    task.add_done_callback(_discard_done)
    return task


def _next_token(key: tuple[int, int]) -> int:
    token = _generations.get(key, 0) + 1
    _generations[key] = token
    # This is a process-local guard only; it must not grow without bound if a
    # busy group edits one message forever.
    if token > 2**60:
        _generations[key] = 1
        return 1
    return token


def _cancel_pending(chat_id: int, message_id: int | None = None) -> None:
    keys = (
        [(chat_id, message_id)]
        if message_id is not None
        else [key for key in _pending if key[0] == chat_id]
    )
    for key in keys:
        _next_token(key)
        item = _pending.pop(key, None)
        if item and item.delete_task and not item.delete_task.done():
            item.delete_task.cancel()
            tasks = _chat_tasks.get(chat_id)
            if tasks is not None:
                tasks.discard(item.delete_task)
                if not tasks:
                    _chat_tasks.pop(chat_id, None)
        # The notice task is deliberately not cancelled: notice cleanup has a
        # five-second lifetime independent of the original-delete job.


def _settings_changed(chat_id: int) -> None:
    """Invalidate both queued jobs and watcher work currently in flight."""
    _settings_revisions[chat_id] = _settings_revisions.get(chat_id, 0) + 1
    _cancel_pending(chat_id)


def _invalidate_consents(chat_id: int) -> None:
    now = time.monotonic()
    for nonce, item in list(_consents.items()):
        if item.chat_id == chat_id or now - item.created > _CONSENT_TTL:
            _consents.pop(nonce, None)
    for nonce, item in list(_verifications.items()):
        if item.chat_id == chat_id or now - item.created > _CONSENT_TTL:
            _verifications.pop(nonce, None)


async def _open_anonymous_verification(message: Any, args: list[str]) -> None:
    """Let the hidden sender prove ownership by tapping as a personal account."""
    _invalidate_consents(message.chat.id)
    nonce = uuid.uuid4().hex[:20]
    item = _Verification(
        chat_id=message.chat.id,
        message_id=0,
        created=time.monotonic(),
        args=tuple(args),
        nonce=nonce,
    )
    _verifications[nonce] = item
    keyboard = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(
            "Verify Admin",
            callback_data=f"gh:eg:verify:{nonce}",
            style=enums.ButtonStyle.SUCCESS,
        )
    ]])
    try:
        card = await _reply(
            message,
            "<b>Anonymous admin detected.</b>\n"
            "Tap <b>Verify Admin</b> from your personal account. "
            "Only the live Telegram group owner can continue.",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=keyboard,
        )
        item.message_id = getattr(card, "id", 0)
    except Exception:
        _verifications.pop(nonce, None)
        await _reply(message, "I couldn't open admin verification.")


def _identity(message: Any) -> tuple[str, int] | None:
    sender = getattr(message, "from_user", None)
    sender_chat = getattr(message, "sender_chat", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    # Linked channel posts are never an eligible group edit.  The group itself
    # in sender_chat is Telegram's anonymous group-administrator identity.
    if sender_chat is not None and getattr(sender_chat, "id", None) != chat_id:
        return None
    if sender is None:
        return ("anonymous", chat_id) if sender_chat is not None and chat_id is not None else None
    if getattr(sender, "is_bot", False) or getattr(sender, "is_self", False):
        return None
    try:
        return ("user", int(sender.id))
    except (TypeError, ValueError, AttributeError):
        return None


async def _member_status(chat_id: int, user_id: int):
    """None means Telegram membership could not be verified (fail closed)."""
    try:
        member = await _rpc(app.get_chat_member(chat_id, user_id))
    except Exception:
        return None
    return getattr(member, "status", None)


async def _eligible(message: Any, include_admins: bool) -> tuple[bool, tuple[str, int] | None]:
    ident = _identity(message)
    if ident is None:
        return False, None
    if ident[0] == "anonymous":
        # Only sender_chat == this group is an anonymous group admin.  A
        # linked channel was rejected by _identity above.
        return bool(include_admins), ident

    # The group owner may exempt a member from Edit Guardian entirely: an
    # exempt member's edits are never deleted, even when admin coverage is on
    # (owner 2026-10-18).  Checked BEFORE the admin test so the exemption wins.
    try:
        from ghelp.plugins.exempt import is_exempt
        if is_exempt(await db.get_chat(message.chat.id), ident[1]):
            logger.info("editguard: %s is exempt in %s", ident[1], message.chat.id)
            return False, ident
    except Exception as exc:  # noqa: BLE001
        logger.warning("editguard: exemption check failed: %s", type(exc).__name__)

    # Do not rely only on Message.from_user.is_self.  Small SDK versions and
    # offline fakes do not always populate that flag, while the bot id is an
    # unambiguous exclusion.
    try:
        getter = getattr(app, "get_me", None)
        me = getattr(app, "me", None) or (
            await _rpc(getter()) if getter else None
        )
        if me is not None and ident[1] == getattr(me, "id", None):
            return False, ident
    except Exception:
        # Telegram already labels bot/self messages in normal updates.  A
        # temporary failure to resolve Maki's id must not make every ordinary
        # member edit disappear from enforcement; membership below remains
        # fail-closed for destructive decisions.
        pass

    status = await _member_status(message.chat.id, ident[1])
    if status is None:
        return False, ident
    if status in (
        enums.ChatMemberStatus.OWNER,
        enums.ChatMemberStatus.ADMINISTRATOR,
    ):
        return bool(include_admins), ident
    # Do not act on a departed/banned/unknown identity even when a malformed
    # SDK status happens to compare unequal to the two admin statuses.
    known_member = {
        enums.ChatMemberStatus.MEMBER,
        enums.ChatMemberStatus.RESTRICTED,
    }
    return status in known_member, ident


async def _fetch_message(chat_id: int, message_id: int):
    try:
        getter = getattr(app, "get_messages")
        try:
            current = await _rpc(getter(chat_id, message_id, replies=0))
        except TypeError:
            current = await _rpc(getter(chat_id, message_id))
    except Exception:
        return None
    if isinstance(current, (list, tuple)):
        if len(current) != 1:
            return None
        current = current[0]
    return current


def _same_identity(message: Any, identity: tuple[str, int], chat_id: int) -> bool:
    if getattr(getattr(message, "chat", None), "id", chat_id) != chat_id:
        return False
    actual = _identity(message)
    return actual == identity


async def _notice_cleanup(notice: Any) -> None:
    try:
        await asyncio.sleep(_NOTICE_TTL)
        await _rpc(notice.delete())
    except asyncio.CancelledError:
        raise
    except Exception:
        # Cleanup is best effort.  It must never cancel the original delete.
        logger.info("editguard: notice cleanup failed")


def _schedule_notice_cleanup(notice: Any, chat_id: int) -> asyncio.Task | None:
    if notice is None or not hasattr(notice, "delete"):
        return None
    try:
        task = asyncio.create_task(_notice_cleanup(notice))
    except Exception:
        return None
    return _track(task, chat_id, notice=True)


def _safe_user_mention(message: Any) -> str:
    user = getattr(message, "from_user", None)
    if user is None:
        return "Anonymous group admin"
    try:
        user_id = int(user.id)
    except (TypeError, ValueError, AttributeError):
        return "The sender"
    name = (
        getattr(user, "first_name", None)
        or getattr(user, "username", None)
        or "user"
    )
    return f'<a href="tg://user?id={user_id}">{html.escape(str(name))}</a>'


async def _send_notice(message: Any, text: str) -> Any:
    try:
        return await _reply(
            message,
            f"{_safe_user_mention(message)}, {text}",
            parse_mode=enums.ParseMode.HTML,
            # This both notifies the sender and makes the notice a reply to
            # the edited message.  The mention is escaped above.
            quote=True,
        )
    except Exception:
        # Do not log text or captions: group messages can contain private
        # material and Edit Guardian must not turn it into a log record.
        logger.info(
            "editguard: could not send notice in chat %s",
            getattr(getattr(message, "chat", None), "id", "?"),
        )
        return None


async def _report_failure(message: Any) -> None:
    notice = await _send_notice(message, "I couldn't delete that edited message.")
    if notice is not None:
        _schedule_notice_cleanup(notice, getattr(message.chat, "id", 0))


async def _delete_pending(item: _Pending) -> None:
    key = (item.chat_id, item.message_id)

    def current_job() -> bool:
        now = time.monotonic()
        return (
            _pending.get(key) is item
            and _settings_revisions.get(item.chat_id, 0) == item.settings_revision
            and item.deadline <= now <= item.deadline + _OVERDUE_GRACE
        )

    def same_revision(message: Any) -> bool:
        return (
            _same_identity(message, item.identity, item.chat_id)
            and _edit_fingerprint(message) == item.fingerprint
        )

    try:
        await asyncio.sleep(item.delay)
        if not current_job():
            return

        # The first read is deliberately after the delay.  It proves the
        # message still has the same chat and sender identity and avoids
        # deleting a replacement message with a reused SDK object.
        settings = await db.get_chat(item.chat_id)
        if not current_job() or not settings.get("editguard_on") or _configured_window(settings) != item.delay:
            return
        current = await _fetch_message(item.chat_id, item.message_id)
        if not current_job():
            return
        if current is None or getattr(current, "empty", False) or not same_revision(current):
            return
        allowed, _ = await _eligible(current, bool(settings.get("editguard_admin")))
        if not current_job() or not allowed:
            return

        # Re-read policy and deadline after the identity lookup.  Promotion,
        # OFF, or a changed delay during that await wins over the old event.
        if not current_job():
            return
        settings = await db.get_chat(item.chat_id)
        if not current_job() or not settings.get("editguard_on") or _configured_window(settings) != item.delay:
            return

        # Bot rights are checked before the final Telegram read.  The final
        # user-policy lookup comes after this await, so a promotion that
        # happens while Telegram checks bot rights cannot be missed.
        bot_ok, _ = await _bot_can_delete(item.chat_id)
        if not current_job():
            return
        if not bot_ok:
            await _report_failure(current)
            return

        # Final revision and live user-policy checks intentionally happen
        # after the bot-rights RPC and immediately before deletion.
        current = await _fetch_message(item.chat_id, item.message_id)
        if not current_job():
            return
        if current is None or getattr(current, "empty", False) or not same_revision(current):
            return
        settings = await db.get_chat(item.chat_id)
        if not current_job() or not settings.get("editguard_on") or _configured_window(settings) != item.delay:
            return
        allowed, _ = await _eligible(current, bool(settings.get("editguard_admin")))
        if not current_job() or not allowed:
            return
        try:
            await _rpc(current.delete())
        except Exception:
            logger.info(
                "editguard: delete failed in chat %s message %s",
                item.chat_id,
                item.message_id,
            )
            await _report_failure(current)
    except asyncio.CancelledError:
        raise
    except Exception:
        # The task is isolated from Pyrogram's dispatcher.  No message body or
        # exception string is logged because either can contain user content.
        logger.info(
            "editguard: delete job failed in chat %s message %s",
            item.chat_id,
            item.message_id,
        )
    finally:
        if _pending.get(key) is item:
            _pending.pop(key, None)


async def _watch_edit(
    message: Any,
    settings: dict,
    identity: tuple[str, int],
    token: int,
    settings_revision: int,
    fingerprint: tuple[str, str],
) -> None:
    chat_id, message_id = message.chat.id, message.id
    key = (chat_id, message_id)
    delay = _window(settings)
    if (
        _generations.get(key, 0) != token
        or _settings_revisions.get(chat_id, 0) != settings_revision
        or not settings.get("editguard_on")
        or _window(settings) != delay
    ):
        return
    item = _Pending(
        chat_id=chat_id,
        message_id=message_id,
        token=token,
        delay=delay,
        deadline=time.monotonic() + delay,
        settings_revision=settings_revision,
        fingerprint=fingerprint,
        message=message,
        identity=identity,
    )
    _pending[key] = item

    notice = await _send_notice(
        message,
        f"You edited this message. It will be deleted at "
        f"{time.strftime('%H:%M:%S UTC', time.gmtime(item.deadline))} "
        f"(in {delay} seconds). "
        "This notice will disappear in 5 seconds.",
    )
    item.notice = notice
    if notice is not None:
        item.notice_task = _schedule_notice_cleanup(notice, chat_id)

    # The setting may have been changed while Telegram was sending the
    # warning.  Do not leave a destructive job behind that race.
    current = await db.get_chat(chat_id)
    if (
        _pending.get(key) is not item
        or _generations.get(key, 0) != token
        or _settings_revisions.get(chat_id, 0) != item.settings_revision
        or _edit_fingerprint(message) != fingerprint
        or not current.get("editguard_on")
        or _window(current) != delay
    ):
        _pending.pop(key, None)
        return
    try:
        task = asyncio.create_task(_delete_pending(item))
    except Exception:
        _pending.pop(key, None)
        return
    item.delete_task = _track(task, chat_id)


@app.on_message(filters.group & filters.command("editguard", prefixes=_PRE))
async def editguard_command(_, message: types.Message) -> None:
    """Show state to callers; only the real group owner changes settings."""
    if not _group(message):
        await _reply(message, "Open /editguard inside a group.")
        return
    command = _message_command(message)
    args = [item.lower() for item in command[1:]]
    if not args:
        try:
            await _reply(
                message,
                _state_text(await db.get_chat(message.chat.id)),
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            await _reply(message, "I couldn't read Edit Guardian settings.")
        return
    if getattr(message, "from_user", None) is None:
        await _open_anonymous_verification(message, args)
        return
    if not await _require_group_owner(message):
        return

    await _run_owner_action(message, message.from_user.id, args)


async def _run_owner_action(message: Any, owner_id: int, args: list[str] | tuple[str, ...]) -> None:
    """Apply a parsed command after live owner identity has been verified."""
    chat_id = message.chat.id
    action = args[0]
    if action == "on" and len(args) == 1:
        _invalidate_consents(chat_id)
        ok, reason = await _bot_can_delete(chat_id)
        if not ok:
            await _reply(message, reason)
            return
        nonce = uuid.uuid4().hex[:20]
        consent = _Consent(
            chat_id=chat_id,
            user_id=owner_id,
            message_id=0,
            created=time.monotonic(),
            window=_window(await db.get_chat(chat_id)),
            nonce=nonce,
        )
        _consents[nonce] = consent
        keyboard = types.InlineKeyboardMarkup(
            [[
                types.InlineKeyboardButton(
                    "Apply to admins: Yes",
                    callback_data=f"gh:eg:yes:{nonce}",
                    style=enums.ButtonStyle.SUCCESS,
                ),
                types.InlineKeyboardButton(
                    "No",
                    callback_data=f"gh:eg:no:{nonce}",
                    style=enums.ButtonStyle.PRIMARY,
                ),
            ]]
        )
        try:
            card = await _reply(
                message,
                "<b>Enable Edit Guardian?</b>\n"
                "Edited messages will be deleted after the configured delay.\n"
                "Should this also apply to admins?",
                parse_mode=enums.ParseMode.HTML,
                reply_markup=keyboard,
            )
            consent.message_id = getattr(card, "id", 0)
        except Exception:
            _consents.pop(nonce, None)
            await _reply(message, "I couldn't open the enable confirmation.")
        return
    if action == "off" and len(args) == 1:
        _invalidate_consents(chat_id)
        _settings_changed(chat_id)
        try:
            await db.update_chat(chat_id, editguard_on=False)
            await _reply(message, "<b>Edit Guardian disabled.</b>", parse_mode=enums.ParseMode.HTML)
        except Exception:
            await _reply(message, "I couldn't disable Edit Guardian.")
        return
    if action == "admin" and len(args) == 2 and args[1] in ("on", "off"):
        _invalidate_consents(chat_id)
        _settings_changed(chat_id)
        try:
            await db.update_chat(chat_id, editguard_admin=args[1] == "on")
            await _reply(
                message,
                f"Admin coverage {'enabled' if args[1] == 'on' else 'disabled'}.",
            )
        except Exception:
            await _reply(message, "I couldn't update admin coverage.")
        return
    if action == "time" and len(args) == 2:
        try:
            value = int(args[1])
            if not _MIN_WINDOW <= value <= _MAX_WINDOW:
                raise ValueError
        except (TypeError, ValueError):
            await _reply(
                message,
                f"Use <code>/editguard time {_MIN_WINDOW}-{_MAX_WINDOW}</code> seconds.",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        _invalidate_consents(chat_id)
        _settings_changed(chat_id)
        try:
            await db.update_chat(chat_id, editguard_window=value)
            await _reply(message, f"Edit Guardian delay set to {value}s.")
        except Exception:
            await _reply(message, "I couldn't update the Edit Guardian delay.")
        return
    await _reply(message, _usage(), parse_mode=enums.ParseMode.HTML)


@app.on_callback_query(filters.regex(r"^gh:eg:"))
async def editguard_callback(_, callback: types.CallbackQuery) -> None:
    message = getattr(callback, "message", None)
    user = getattr(callback, "from_user", None)
    if not _group(message):
        await _rpc(callback.answer("This confirmation only works in the group.", show_alert=True))
        return
    if not user or not await _group_owner(message.chat.id, user.id):
        await _rpc(callback.answer("Only the live group owner can confirm this.", show_alert=True))
        return
    data = str(getattr(callback, "data", ""))
    parts = data.split(":")
    if len(parts) == 4 and parts[:3] == ["gh", "eg", "verify"]:
        nonce = parts[3]
        item = _verifications.get(nonce)
        if (
            item is None
            or time.monotonic() - item.created > _CONSENT_TTL
            or item.chat_id != message.chat.id
            or item.message_id != getattr(message, "id", None)
        ):
            if item is not None and time.monotonic() - item.created > _CONSENT_TTL:
                _verifications.pop(nonce, None)
            await _rpc(callback.answer("This verification expired or is no longer valid.", show_alert=True))
            return
        _verifications.pop(nonce, None)
        await _rpc(callback.answer("Owner verified."))
        # Recheck after acknowledging: demotion during the network await must
        # invalidate the verification before any setting can change.
        if not await _group_owner(message.chat.id, user.id):
            await _reply(message, "Owner access could not be verified. No settings were changed.")
            return
        await _run_owner_action(message, user.id, item.args)
        return
    if len(parts) != 4 or parts[:2] != ["gh", "eg"] or parts[2] not in ("yes", "no"):
        await _rpc(callback.answer("Unknown Edit Guardian confirmation.", show_alert=True))
        return
    nonce, include_admins = parts[3], parts[2] == "yes"
    item = _consents.get(nonce)
    if (
        item is None
        or time.monotonic() - item.created > _CONSENT_TTL
        or item.chat_id != message.chat.id
        or item.user_id != user.id
        or item.message_id != getattr(message, "id", None)
    ):
        if item is not None and time.monotonic() - item.created > _CONSENT_TTL:
            _consents.pop(nonce, None)
        await _rpc(callback.answer("This confirmation expired or is no longer valid.", show_alert=True))
        return

    # Acknowledging before network checks prevents a Telegram spinner from
    # hiding an honest rights error.  The callback remains usable if checking
    # rights fails; consent is consumed only by the atomic mutation below.
    await _rpc(callback.answer())
    ok, reason = await _bot_can_delete(message.chat.id)
    if not ok:
        await _reply(message, reason)
        return
    if not await _group_owner(message.chat.id, user.id):
        _consents.pop(nonce, None)
        await _reply(message, "Owner access could not be verified. No settings were changed.")
        return
    _settings_changed(message.chat.id)
    try:
        def enable(settings: dict) -> None:
            current = _consents.get(nonce)
            if current is not item or time.monotonic() - item.created > _CONSENT_TTL:
                raise ValueError("stale confirmation")
            if _window(settings) != item.window:
                raise ValueError("settings changed")
            _consents.pop(nonce, None)
            settings["editguard_on"] = True
            settings["editguard_admin"] = include_admins

        await _mutate_chat(message.chat.id, enable)
        try:
            state = _state_text(await db.get_chat(message.chat.id))
        except Exception:
            state = "Edit Guardian enabled."
        try:
            await _rpc(message.edit_text(
                state,
                parse_mode=enums.ParseMode.HTML,
                reply_markup=None,
            ))
        except Exception:
            # The setting has already been committed.  Never report that it
            # was not changed merely because Telegram rejected a UI refresh.
            try:
                await _reply(message, "Edit Guardian enabled.")
            except Exception:
                logger.info("editguard: enabled but could not refresh card in chat %s", message.chat.id)
    except ValueError:
        _consents.pop(nonce, None)
        await _reply(message, "This confirmation expired or settings changed. No settings were changed.")
    except Exception:
        logger.info("editguard: confirmation failed in chat %s", message.chat.id)
        await _reply(message, "I couldn't enable Edit Guardian. No settings were changed.")


@app.on_message(filters.group & ~filters.service, group=-2)
async def remember_original_content(_, message: types.Message) -> None:
    """Remember original content so metadata/reaction updates can be ignored."""
    _remember_content(message)


async def editguard_watcher(_, message: types.Message) -> None:
    """Warn and schedule one deletion for the latest eligible edit."""
    if not _group(message):
        return
    chat_id, message_id = message.chat.id, message.id
    key = (chat_id, message_id)
    fingerprint = _edit_fingerprint(message)
    content_hash = fingerprint[1]

    # Telegram re-sends a message through the edit dispatcher whenever its
    # metadata changes — a reaction added or removed (kurigram surfaces those
    # as edits, and the final reaction removal arrives with NO reaction
    # payload at all), a pin, a view counter.  The CONTENT is identical in
    # every one of those cases, so a stored baseline that still matches means
    # nothing was edited: no notice, no timer, no deletion.  This is the
    # decisive gate and it runs before anything that can arm a delete
    # (2026-09-19).
    previous_key = _content_keys.get(key)
    if previous_key is not None and previous_key == _content_key(message):
        return
    previous_hash = _content_cache.get(key)
    if previous_hash is not None and previous_hash == content_hash:
        return

    # No baseline (restart, or a post older than the cache) and the update
    # still carries reactions: a reaction is metadata, never an edit of what
    # the member wrote — fail closed and remember the content instead.
    if previous_hash is None and getattr(message, "reactions", None) is not None:
        _remember_content(message)
        return

    current = _pending.get((chat_id, message_id))
    # The same edit delivered twice (raw dispatcher + edited-message parser)
    # keeps one pending delete.
    if current is not None and current.fingerprint[1] == content_hash:
        return
    if not _actual_content_edit(message):
        return
    # Cancel before any await so a re-edit/off update cannot leave the former
    # deadline alive while this event checks membership.
    _cancel_pending(chat_id, message_id)
    # These are captured synchronously.  An older watcher invocation that is
    # paused in a Telegram membership RPC must never adopt a newer edit's
    # generation or revision when it resumes.
    token = _generations.get((chat_id, message_id), 0)
    settings_revision = _settings_revisions.get(chat_id, 0)
    try:
        settings = await db.get_chat(chat_id)
        if (
            _generations.get((chat_id, message_id), 0) != token
            or _settings_revisions.get(chat_id, 0) != settings_revision
            or not settings.get("editguard_on")
        ):
            return
        eligible, identity = await _eligible(
            message,
            bool(settings.get("editguard_admin")),
        )
        if (
            _generations.get((chat_id, message_id), 0) != token
            or _settings_revisions.get(chat_id, 0) != settings_revision
            or not eligible
            or identity is None
        ):
            return
        _remember_content(message)
        await _watch_edit(
            message,
            settings,
            identity,
            token,
            settings_revision,
            fingerprint,
        )
    except Exception:
        logger.info(
            "editguard: watcher failed in chat %s message %s",
            chat_id,
            message_id,
        )


@app.on_raw_update(group=1)
async def editguard_raw_update(_, update: Any, users: dict, chats: dict) -> None:
    """Dispatch only Telegram's two genuine message-edit update types."""
    if not isinstance(
        update,
        (raw.types.UpdateEditMessage, raw.types.UpdateEditChannelMessage),
    ):
        return
    raw_message = getattr(update, "message", None)
    peer = getattr(raw_message, "peer_id", None)
    message_id = getattr(raw_message, "id", None)
    if peer is None or message_id is None:
        return
    try:
        chat_id = utils.get_peer_id(peer)
        message = await _fetch_message(chat_id, message_id)
        if message is None or getattr(message, "empty", False):
            return
        await editguard_watcher(_, message)
    except Exception:
        logger.info(
            "editguard: raw edit dispatch failed for message %s",
            message_id,
        )