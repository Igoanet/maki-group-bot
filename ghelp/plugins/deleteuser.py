"""Bounded, bot-only deletion of one person's visible messages.

``/deleteuser`` and ``/purgeuser`` deliberately do not use Telegram's
participant-history methods.  They inspect a fixed range of message IDs with
the bot session, delete only messages whose returned object proves both the
same chat and the exact target ``from_user.id``, and report hidden/absent IDs
separately.  The command message is the immutable upper boundary.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.bulk_delete import _prepare_delete_request
from ghelp.config import config
from ghelp.helpers import ikb, ikm, is_admin
from ghelp.message_scope import SCOPES, matches_scope

logger = logging.getLogger(__name__)

_GROUP_TYPES = (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)
_MAX_SCAN = 5000
_FETCH_BATCH = 100
_DELETE_BATCH = 100
_TTL = 300
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class PendingDelete:
    token: str
    chat_id: int
    actor_id: int
    target_id: int
    target_name: str
    boundary: int
    lower_bound: int
    created: float
    card_id: int = 0
    card: object | None = None
    scope: str | None = None
    frozen_scope: str | None = None
    stop: asyncio.Event | None = None
    task: asyncio.Task | None = None
    scanned: int = 0
    identified: int = 0
    accepted: int = 0
    hidden: int = 0
    outside: int = 0
    unrelated: int = 0
    refused: int = 0
    finished: bool = False
    reason: str = ""
    admitting: bool = False
    cancelled: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


_pending: dict[str, PendingDelete] = {}
_running: dict[int, PendingDelete] = {}
_admission_locks: dict[int, asyncio.Lock] = {}


def _group(message) -> bool:
    chat = getattr(message, "chat", None)
    return bool(chat and getattr(chat, "type", None) in _GROUP_TYPES)


def _token() -> str:
    return uuid.uuid4().hex


def _cb(item: PendingDelete, action: str, value: str | None = None) -> str:
    pieces = ["gh", "du", action]
    if value is not None:
        pieces.append(value)
    pieces.append(item.token)
    return ":".join(pieces)


def _cleanup() -> None:
    now = time.monotonic()
    for token, item in list(_pending.items()):
        if item.chat_id not in _running and now - item.created > _TTL:
            _pending.pop(token, None)


def _label(item: PendingDelete) -> str:
    return (
        f'<a href="tg://user?id={item.target_id}">'
        f"{html.escape(item.target_name or str(item.target_id))}</a>"
    )


def _scope_keyboard(item: PendingDelete):
    rows = [
        [
            ikb(
                label,
                callback_data=_cb(item, "type", value),
                style=enums.ButtonStyle.PRIMARY,
            )
            for value, label in (
                ("all", "All"),
                ("text", "Text"),
                ("media", "Media"),
            )
        ],
        [
            ikb(
                label,
                callback_data=_cb(item, "type", value),
                style=enums.ButtonStyle.PRIMARY,
            )
            for value, label in (
                ("stickers", "Stickers"),
                ("gifs", "GIFs"),
                ("forwards", "Forwards"),
            )
        ],
    ]
    if item.scope:
        rows.append(
            [
                ikb(
                    "Delete selected messages",
                    callback_data=_cb(item, "confirm"),
                    style=enums.ButtonStyle.DANGER,
                )
            ]
        )
    rows.append(
        [
            ikb(
                "Cancel",
                callback_data=_cb(item, "cancel"),
                style=enums.ButtonStyle.PRIMARY,
            )
        ]
    )
    return ikm(rows)


def _selection_text(item: PendingDelete) -> str:
    selected = (
        f"\nSelected type: <b>{html.escape(item.scope)}</b>"
        if item.scope
        else "\nSelect exactly one message type below."
    )
    return (
        f"<b>Delete messages from {_label(item)}?</b>\n"
        f"Bot-visible IDs: <code>{item.lower_bound}–{item.boundary - 1}</code> "
        f"(at most {_MAX_SCAN}).\n"
        "Only messages returned from this same group with this exact sender "
        "are eligible. Hidden or absent IDs are never guessed."
        f"{selected}\n\n"
        "The command message and anything sent after it are outside the "
        "fixed boundary."
    )


def _progress_text(item: PendingDelete, *, final: bool = False) -> str:
    display_scope = item.frozen_scope or item.scope
    if final:
        heading = (
            "<b>Person-message deletion complete.</b>"
            if item.finished
            else "<b>Person-message deletion stopped.</b>"
        )
    else:
        heading = "<b>Person-message deletion is running…</b>"
    lines = [
        heading,
        f"Target: {_label(item)}",
        f"Type: <b>{html.escape(display_scope or 'unknown')}</b>",
        f"Exact messages identified: <b>{item.identified}</b>",
        f"Deletion requests accepted: <b>{item.accepted}</b>",
        f"Hidden / absent IDs: <b>{item.hidden}</b>",
        f"Returned outside this chat: <b>{item.outside}</b>",
        f"Other visible messages skipped: <b>{item.unrelated}</b>",
        f"Telegram refused: <b>{item.refused}</b>",
        f"Message IDs checked: <b>{item.scanned}</b>/{item.boundary - item.lower_bound}",
        "Counts are bot-visible IDs and accepted API requests, not a promise "
        "that Telegram's history is empty.",
    ]
    if item.reason:
        lines.append(f"Stopped because: {html.escape(item.reason)}")
    return "\n".join(lines)


async def _resolve_target(message, arg: str | None = None):
    reply = getattr(message, "reply_to_message", None)
    user = getattr(reply, "from_user", None) if reply else None
    if user is not None:
        return user
    if not arg:
        return None
    arg = str(arg).lstrip("@")
    try:
        return await app.get_users(int(arg) if arg.lstrip("-").isdigit() else arg)
    except Exception:
        return None


def _member_can_delete(member) -> bool:
    status = getattr(member, "status", None)
    if status == enums.ChatMemberStatus.OWNER:
        return True
    if status != enums.ChatMemberStatus.ADMINISTRATOR:
        return False
    return bool(getattr(getattr(member, "privileges", None), "can_delete_messages", False))


async def _live_mutation_checks(chat_id: int, actor_id: int) -> tuple[bool, str]:
    """Check caller and Maki immediately before every delete request."""

    try:
        actor = await app.get_chat_member(chat_id, actor_id)
        if not _member_can_delete(actor):
            return False, "The requester is no longer a group creator/admin with Delete messages."
        me = await app.get_me()
        bot = await app.get_chat_member(chat_id, me.id)
        if not _member_can_delete(bot):
            return False, "Maki no longer has the group's Delete messages right."
    except Exception:
        return False, "Could not verify live group/admin or Maki permissions."
    return True, ""


async def _target_is_protected(chat_id: int, target_id: int) -> tuple[bool, str]:
    try:
        me = await app.get_me()
        if target_id == me.id:
            return True, "Maki is protected."
        if config.OWNER_ID and target_id == config.OWNER_ID:
            return True, "The configured bot owner is protected."
        member = await app.get_chat_member(chat_id, target_id)
    except Exception:
        return True, "Could not verify the target's live group role."
    status = getattr(member, "status", None)
    if status == enums.ChatMemberStatus.OWNER:
        return True, "The group creator is protected."
    if status == enums.ChatMemberStatus.ADMINISTRATOR:
        return True, "Group admins are protected."
    return False, ""


async def _initial_checks(message, target) -> str | None:
    actor = getattr(message, "from_user", None)
    if actor is None:
        return "Only a visible group admin can start this cleanup."
    ok, reason = await _live_mutation_checks(message.chat.id, actor.id)
    if not ok:
        return reason
    protected, reason = await _target_is_protected(message.chat.id, target.id)
    return reason if protected else None


def _new_item(message, target) -> PendingDelete | None:
    boundary = getattr(message, "id", None)
    if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 2:
        return None
    upper = boundary - 1
    lower = max(1, upper - _MAX_SCAN + 1)
    return PendingDelete(
        token=_token(),
        chat_id=message.chat.id,
        actor_id=message.from_user.id,
        target_id=target.id,
        target_name=getattr(target, "first_name", "") or "user",
        boundary=boundary,
        lower_bound=lower,
        created=time.monotonic(),
    )


async def _open(message, target) -> None:
    _cleanup()
    problem = await _initial_checks(message, target)
    if problem:
        await message.reply_text(problem)
        return
    item = _new_item(message, target)
    if item is None:
        await message.reply_text("This command has no valid fixed message boundary.")
        return
    _pending[item.token] = item
    try:
        sent = await message.reply_text(
            _selection_text(item),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_scope_keyboard(item),
        )
        if _pending.get(item.token) is not item:
            return
        item.card = sent
        item.card_id = getattr(sent, "id", 0)
    except Exception:
        _pending.pop(item.token, None)
        try:
            await message.reply_text("Could not open this deletion confirmation.")
        except Exception:
            pass


@app.on_message(
    filters.group
    & filters.command(["deleteuser", "purgeuser"], prefixes=["/", "?", "!", "."])
)
async def deleteuser_command(_, message: types.Message):
    command = getattr(message, "command", None) or []
    target = await _resolve_target(
        message,
        command[1] if len(command) > 1 else None,
    )
    if target is None:
        await message.reply_text(
            "Reply to a person's message or provide @username / numeric ID."
        )
        return
    await _open(message, target)


def _bound(query, token: str) -> PendingDelete | None:
    item = _pending.get(token)
    message = getattr(query, "message", None)
    user = getattr(query, "from_user", None)
    if (
        not item
        or not _TOKEN_RE.fullmatch(token)
        or not message
        or getattr(getattr(message, "chat", None), "id", None) != item.chat_id
        or getattr(message, "id", None) != item.card_id
        or not user
        or user.id != item.actor_id
    ):
        return None
    if item.chat_id not in _running and time.monotonic() - item.created > _TTL:
        _pending.pop(token, None)
        return None
    return item


def admits_stop_callback(query) -> bool:
    """Synchronously admit only a bound Stop for a currently running job.

    The rateguard can call this without awaiting Telegram or database state.
    Every identity component is checked against the job registered for the
    callback's chat, so unrelated callbacks cannot consume the priority lane.
    """

    data = str(getattr(query, "data", "") or "")
    parts = data.split(":")
    if len(parts) != 4 or parts[:3] != ["gh", "du", "stop"]:
        return False
    token = parts[3]
    if not _TOKEN_RE.fullmatch(token):
        return False
    item = _pending.get(token)
    if item is None or _running.get(item.chat_id) is not item:
        return False
    if item.stop is None or item.finished or item.cancelled:
        return False
    message = getattr(query, "message", None)
    user = getattr(query, "from_user", None)
    return bool(
        message
        and getattr(getattr(message, "chat", None), "id", None) == item.chat_id
        and getattr(message, "id", None) == item.card_id
        and user
        and getattr(user, "id", None) == item.actor_id
    )


def _admission_valid(query, token: str, item: PendingDelete) -> bool:
    if not item.admitting or item.cancelled:
        return False
    if _running.get(item.chat_id) is not None:
        return False
    return _bound(query, token) is item


async def _answer(query, text: str, *, alert: bool = False) -> None:
    try:
        await query.answer(text, show_alert=alert)
    except Exception:
        pass


async def _edit(item: PendingDelete, text: str, markup=None) -> None:
    if item.card is None:
        return
    await item.card.edit_text(
        text,
        parse_mode=enums.ParseMode.HTML,
        reply_markup=markup,
    )


async def _confirm_delete(query, token: str, item: PendingDelete) -> None:
    """Admit one confirmation per chat, with cancellation-safe admission."""

    response = None
    async with item.lock:
        if _bound(query, token) is not item:
            response = "This deletion card expired or belongs elsewhere."
        elif item.admitting:
            response = "Deletion confirmation is already being checked."
        elif not item.scope:
            response = "Choose a message type first."
        elif item.chat_id in _running:
            response = "Deletion is already running."
        else:
            item.admitting = True
            item.cancelled = False
    if response is not None:
        await _answer(query, response, alert=True)
        return

    # A chat-wide admission lock covers different cards for the same chat.
    # The item lock is intentionally released while Telegram lookups await so
    # Cancel can invalidate this admission before the final registration.
    admission_lock = _admission_locks.setdefault(
        item.chat_id, asyncio.Lock()
    )
    async with admission_lock:
        if not _admission_valid(query, token, item):
            async with item.lock:
                item.admitting = False
            await _answer(query, "This confirmation was cancelled or expired.", alert=True)
            return
        ok, reason = await _live_mutation_checks(item.chat_id, item.actor_id)
        if not ok:
            async with item.lock:
                item.admitting = False
            await _answer(query, reason, alert=True)
            return
        protected, reason = await _target_is_protected(
            item.chat_id, item.target_id
        )
        if protected:
            async with item.lock:
                item.admitting = False
            await _answer(query, reason, alert=True)
            return

        # This is the last synchronous gate before registering _running.
        # Recheck pending-card identity, TTL, cancellation, and scope after
        # all awaited live lookups.
        async with item.lock:
            if not _admission_valid(query, token, item):
                item.admitting = False
                stale = True
            else:
                item.frozen_scope = item.scope
                frozen_scope = item.frozen_scope
                item.stop = asyncio.Event()
                _running[item.chat_id] = item
                item.admitting = False
                stale = False
        if stale:
            await _answer(query, "This confirmation was cancelled or expired.", alert=True)
            return

    try:
        await _edit(
            item,
            _progress_text(item),
            ikm(
                [
                    [
                        ikb(
                            "Stop",
                            callback_data=_cb(item, "stop"),
                            style=enums.ButtonStyle.DANGER,
                        )
                    ]
                ]
            ),
        )
    except asyncio.CancelledError:
        # A callback timeout/shutdown before task creation must not leave an
        # untracked, task-less registration that permanently blocks the chat.
        item.cancelled = True
        item.stop.set()
        if _running.get(item.chat_id) is item:
            _running.pop(item.chat_id, None)
        if _pending.get(item.token) is item:
            _pending.pop(item.token, None)
        raise
    except Exception:
        pass
    item.task = asyncio.create_task(_scan(item, frozen_scope))
    await _answer(query, "Deletion started.")


def _returned_messages(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _same_message(message, chat_id: int, message_id: int, target_id: int) -> bool:
    if message is None or getattr(message, "empty", False):
        return False
    if getattr(message, "id", None) != message_id:
        return False
    chat = getattr(message, "chat", None)
    if chat is None or getattr(chat, "id", None) != chat_id:
        return False
    sender = getattr(message, "from_user", None)
    return sender is not None and getattr(sender, "id", None) == target_id


def _same_visible_chat(message, chat_id: int, message_id: int) -> bool:
    """Prove a returned object is a visible message in this exact chat."""

    if message is None or getattr(message, "empty", False):
        return False
    return (
        getattr(message, "id", None) == message_id
        and getattr(getattr(message, "chat", None), "id", None) == chat_id
    )


async def _scan(item: PendingDelete, scope: str | None = None) -> None:
    # ``scope`` is copied at the confirmation boundary.  The menu's mutable
    # selection must never change a running destructive job.
    scope = scope or item.frozen_scope or item.scope
    assert scope in SCOPES
    if item.stop is None:
        item.stop = asyncio.Event()
    try:
        cursor = item.boundary - 1
        while cursor >= item.lower_bound:
            if item.stop.is_set():
                item.reason = "Stopped by the group creator/admin."
                break
            start = max(item.lower_bound, cursor - _FETCH_BATCH + 1)
            ids = list(range(cursor, start - 1, -1))
            try:
                returned = _returned_messages(
                    await app.get_messages(item.chat_id, ids)
                )
            except Exception as exc:
                item.reason = f"Message read failed ({type(exc).__name__})."
                break
            by_id = {
                getattr(message, "id", None): message
                for message in returned
                if getattr(message, "id", None) in ids
            }
            matches = []
            for message_id in ids:
                item.scanned += 1
                message = by_id.get(message_id)
                if not _same_visible_chat(message, item.chat_id, message_id):
                    if (
                        message is not None
                        and not getattr(message, "empty", False)
                        and getattr(message, "id", None) == message_id
                        and getattr(getattr(message, "chat", None), "id", None)
                        != item.chat_id
                    ):
                        item.outside += 1
                    else:
                        item.hidden += 1
                    continue
                if not _same_message(
                    message, item.chat_id, message_id, item.target_id
                ):
                    item.unrelated += 1
                    continue
                if matches_scope(message, scope):
                    matches.append(message_id)
                    item.identified += 1
            for offset in range(0, len(matches), _DELETE_BATCH):
                if item.stop.is_set():
                    item.reason = "Stopped by the group creator/admin."
                    break
                batch = matches[offset : offset + _DELETE_BATCH]
                # Resolve/build the raw request before live checks so no
                # peer-resolution await occurs after permission is granted.
                try:
                    request = await _prepare_delete_request(
                        app, item.chat_id, batch
                    )
                except Exception as exc:
                    item.refused += len(batch)
                    item.reason = (
                        "Could not prepare a scoped deletion request "
                        f"({type(exc).__name__})."
                    )
                    break
                protected, reason = await _target_is_protected(
                    item.chat_id, item.target_id
                )
                if protected:
                    item.reason = reason
                    break
                # Target protection precedes the final actor/bot-right check.
                # That check is the last permission await before invoke.
                ok, reason = await _live_mutation_checks(
                    item.chat_id, item.actor_id
                )
                if not ok:
                    item.reason = reason
                    break
                try:
                    result = await app.invoke(
                        request,
                        retries=1,
                        sleep_threshold=0,
                        retry_delay=0,
                    )
                    if result is False:
                        raise RuntimeError("Telegram refused the deletion request")
                    item.accepted += len(batch)
                except Exception as exc:
                    item.refused += len(batch)
                    item.reason = f"Telegram refused a deletion request ({type(exc).__name__})."
                    break
            if item.reason or item.stop.is_set():
                break
            cursor = start - 1
            try:
                await _edit(item, _progress_text(item))
            except Exception:
                # A deleted/uneditable progress card must not turn a successful
                # bot request into an invented failure.
                pass
        item.finished = not bool(item.reason)
        if item.stop.is_set() and not item.reason:
            item.reason = "Stopped by the group creator/admin."
            item.finished = False
    except asyncio.CancelledError:
        item.reason = "Cleanup task cancelled."
        item.finished = False
        raise
    finally:
        if _running.get(item.chat_id) is item:
            _running.pop(item.chat_id, None)
        try:
            await _edit(item, _progress_text(item, final=True))
        except Exception:
            pass
        if _pending.get(item.token) is item:
            _pending.pop(item.token, None)


@app.on_callback_query(filters.regex(r"^gh:du:"))
async def deleteuser_callback(_, query: types.CallbackQuery):
    data = str(getattr(query, "data", "") or "")
    parts = data.split(":")
    if len(parts) < 4:
        await _answer(query, "That deletion button is invalid.", alert=True)
        return
    token = parts[-1]
    item = _bound(query, token)
    if item is None:
        await _answer(query, "This deletion card expired or belongs elsewhere.", alert=True)
        return
    action = parts[2]
    value = parts[3] if len(parts) == 5 else None
    if item.chat_id in _running and action != "stop":
        await _answer(query, "Deletion is already running; use Stop.", alert=True)
        return
    if action == "cancel":
        async with item.lock:
            if item.chat_id in _running:
                running = True
            else:
                running = False
                item.cancelled = True
                item.admitting = False
                if _pending.get(token) is item:
                    _pending.pop(token, None)
        if running:
            await _answer(query, "Use Stop while deletion is running.", alert=True)
            return
        try:
            await query.message.edit_text("Deletion cancelled — nothing was deleted.")
        except Exception:
            pass
        await _answer(query, "Cancelled.")
        return
    if action == "stop":
        running = _running.get(item.chat_id)
        if running is not item or item.stop is None:
            await _answer(query, "That deletion is no longer running.", alert=True)
            return
        ok, reason = await _live_mutation_checks(item.chat_id, item.actor_id)
        if not ok:
            await _answer(query, reason, alert=True)
            return
        item.stop.set()
        await _answer(query, "Stopping after the current safe request…")
        return
    if action == "type":
        if value not in SCOPES:
            await _answer(query, "That message type is unavailable.", alert=True)
            return
        response = None
        async with item.lock:
            if item.admitting:
                response = "Deletion confirmation is already being checked."
        if response is not None:
            await _answer(query, response, alert=True)
            return
        ok, reason = await _live_mutation_checks(item.chat_id, item.actor_id)
        if not ok:
            await _answer(query, reason, alert=True)
            return
        response = None
        async with item.lock:
            if item.admitting or item.cancelled or _pending.get(token) is not item:
                response = "That deletion card is no longer current."
            else:
                item.scope = value
        if response is not None:
            await _answer(query, response, alert=True)
            return
        try:
            await _edit(item, _selection_text(item), _scope_keyboard(item))
        except Exception:
            pass
        await _answer(query, f"Selected {value}.")
        return
    if action == "confirm":
        await _confirm_delete(query, token, item)
        return
    await _answer(query, "That deletion action is not available.", alert=True)