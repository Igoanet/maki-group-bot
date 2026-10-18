"""Join-request admission service.

This module deliberately contains the stateful part of join-request handling,
instead of a command panel.  The panel and the normal membership moderation
watchers use the public functions below.  In particular, a successful request
approval is not a blanket exemption for later joins: :func:`is_request_admission`
binds it to one BY_REQUEST membership service message.
"""

import asyncio
import html
import secrets
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager

from pyrogram import enums

from . import app
from .config import config
from .database import db
from .helpers import ikb, ikm

MODES = frozenset(("off", "verify", "instant"))
CHALLENGE_SECONDS = 5 * 60
FAILED_DM_SECONDS = 24 * 60 * 60
ADMISSION_SECONDS = 60 * 60
MAX_PENDING = 500
MAX_ADMITTED = 500
MAX_PROCESSED = 500
MAX_BULK_OPS = 200
BULK_OPERATION_SECONDS = 5 * 60


class RequestError(Exception):
    """An expected, safe-to-show join-request service error."""

    def __init__(self, text: str):
        self.text = str(text)
        super().__init__(self.text)


# A lock exists only while an approval RPC is underway.  This makes a
# membership update that arrives just before Telegram returns wait for the
# durable admission marker rather than accidentally running legacy gates.
_approval_locks: dict[tuple[int, int], asyncio.Lock] = {}


@asynccontextmanager
async def _no_lock():
    """Use when a caller already owns the per-requester lock."""
    yield


def _mode(cfg: Mapping) -> str:
    value = cfg.get("join_request_mode", "off")
    return value if value in MODES else "off"


def _revision(cfg: Mapping) -> int:
    try:
        return max(0, int(cfg.get("join_request_revision", 0)))
    except (TypeError, ValueError):
        return 0


def _pending(cfg: dict) -> dict:
    pending = cfg.get("join_request_pending")
    if not isinstance(pending, dict):
        pending = {}
        cfg["join_request_pending"] = pending
    return pending


def _admitted(cfg: dict) -> dict:
    admitted = cfg.get("join_request_admitted")
    if not isinstance(admitted, dict):
        admitted = {}
        cfg["join_request_admitted"] = admitted
    return admitted


def _bulk_ops(cfg: dict) -> dict:
    operations = cfg.get("join_request_bulk_ops")
    if not isinstance(operations, dict):
        operations = {}
        cfg["join_request_bulk_ops"] = operations
    return operations


def _processed(cfg: dict) -> dict:
    """Recent request identities, not an unbounded audit history."""
    processed = cfg.get("join_request_processed")
    if not isinstance(processed, dict):
        processed = {}
        cfg["join_request_processed"] = processed
    return processed


def _trim(cfg: dict, now: float) -> None:
    """Discard expired data and cap persisted state without a history backup."""
    pending = _pending(cfg)
    for uid, item in list(pending.items()):
        if not isinstance(item, dict) or float(item.get("keep_until", 0) or 0) <= now:
            pending.pop(uid, None)
    if len(pending) > MAX_PENDING:
        oldest = sorted(
            pending,
            key=lambda uid: float(pending[uid].get("created", 0) or 0),
        )[:len(pending) - MAX_PENDING]
        for uid in oldest:
            pending.pop(uid, None)

    admitted = _admitted(cfg)
    for uid, item in list(admitted.items()):
        if not isinstance(item, dict) or float(item.get("until", 0) or 0) <= now:
            admitted.pop(uid, None)
    if len(admitted) > MAX_ADMITTED:
        oldest = sorted(
            admitted,
            key=lambda uid: float(admitted[uid].get("created", 0) or 0),
        )[:len(admitted) - MAX_ADMITTED]
        for uid in oldest:
            admitted.pop(uid, None)

    processed = _processed(cfg)
    for identity, item in list(processed.items()):
        if not isinstance(item, dict) or float(item.get("until", 0) or 0) <= now:
            processed.pop(identity, None)
    if len(processed) > MAX_PROCESSED:
        oldest = sorted(
            processed,
            key=lambda identity: float(processed[identity].get("created", 0) or 0),
        )[:len(processed) - MAX_PROCESSED]
        for identity in oldest:
            processed.pop(identity, None)

    operations = _bulk_ops(cfg)
    for operation, item in list(operations.items()):
        if not isinstance(item, dict) or float(item.get("until", 0) or 0) <= now:
            operations.pop(operation, None)
    if len(operations) > MAX_BULK_OPS:
        oldest = sorted(
            operations,
            key=lambda operation: float(operations[operation].get("created", 0) or 0),
        )[:len(operations) - MAX_BULK_OPS]
        for operation in oldest:
            operations.pop(operation, None)


def _new_challenge(chat_id: int, user_id: int, revision: int, event_id: str,
                   event_date: float, title: str = "") -> dict:
    """Create a compact, serializable arithmetic challenge."""
    left = 2 + secrets.randbelow(8)
    right = 1 + secrets.randbelow(8)
    answer = left + right
    values = [answer]
    while len(values) < 3:
        candidate = answer + secrets.randbelow(9) - 4
        if candidate >= 0 and candidate not in values:
            values.append(candidate)
    # The answer index, not the answer itself, is carried in callback data.
    for index in range(len(values) - 1, 0, -1):
        swap = secrets.randbelow(index + 1)
        values[index], values[swap] = values[swap], values[index]
    correct = values.index(answer)
    now = time.time()
    return {
        "nonce": secrets.token_hex(12),
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "revision": revision,
        "event_id": event_id,
        "event_date": event_date,
        "created": now,
        "expires": now + CHALLENGE_SECONDS,
        "keep_until": now + CHALLENGE_SECONDS,
        "question": f"{left} + {right}",
        "choices": values,
        "correct": correct,
        "attempts": 0,
        "message_id": 0,
        "delivery": "sending",
        "state": "ready",
        "title": str(title or "")[:256],
    }


def _request_identity(request, user_id: int) -> tuple[str, float]:
    """Stable identity for Telegram's one request update/replay.

    Telegram supplies a request date.  ``query_id`` further distinguishes two
    reapplications in the same date tick without ever being used for a DM.
    """
    value = getattr(request, "date", 0)
    try:
        event_date = float(value.timestamp()) if hasattr(value, "timestamp") else float(value)
    except (TypeError, ValueError, OverflowError):
        event_date = 0.0
    query = str(getattr(request, "query_id", "") or "")[:128]
    return f"{user_id}:{event_date:.6f}:{query}", event_date


def _remember_processed(cfg: dict, event_id: str, event_date: float,
                        state: str = "success") -> None:
    _processed(cfg)[event_id] = {
        "created": time.time(),
        "until": time.time() + ADMISSION_SECONDS,
        "event_date": event_date,
        "state": state,
    }


def _is_admin_status(status) -> bool:
    return status in (
        enums.ChatMemberStatus.OWNER,
        enums.ChatMemberStatus.ADMINISTRATOR,
    )


async def _bot_can_invite(chat_id: int) -> None:
    """Fail closed on bot-right checks; do not use cached authorization."""
    try:
        me = getattr(app, "me", None) or await app.get_me()
        member = await app.get_chat_member(chat_id, me.id)
        privileges = getattr(member, "privileges", None)
        if member.status == enums.ChatMemberStatus.OWNER:
            return
        if (
            member.status == enums.ChatMemberStatus.ADMINISTRATOR
            and bool(getattr(privileges, "can_invite_users", False))
        ):
            return
    except Exception:
        pass
    raise RequestError(
        "I need the Invite Users admin right before I can manage join requests."
    )


async def require_access(chat_id, actor_id, check_bot: bool = True) -> None:
    """Require a real creator/configured owner or invite-capable administrator.

    ``actor_id`` intentionally must be a personal Telegram user ID.  Callers
    with an anonymous sender have no usable ID and are rejected.
    """
    if not actor_id:
        raise RequestError("Anonymous admins cannot manage join requests.")
    if config.OWNER_ID and int(actor_id) == config.OWNER_ID:
        if check_bot:
            await _bot_can_invite(int(chat_id))
        return
    try:
        member = await app.get_chat_member(int(chat_id), int(actor_id))
        privileges = getattr(member, "privileges", None)
        allowed = member.status == enums.ChatMemberStatus.OWNER or (
            member.status == enums.ChatMemberStatus.ADMINISTRATOR
            and bool(getattr(privileges, "can_invite_users", False))
        )
    except Exception:
        allowed = False
    if not allowed:
        raise RequestError(
            "You need the Invite Users admin right to manage join requests."
        )
    if check_bot:
        await _bot_can_invite(int(chat_id))


async def settings(chat_id) -> dict:
    """Return the current per-chat join-request settings."""
    return await db.get_chat(int(chat_id))


async def set_mode(chat_id, actor_id, mode) -> dict:
    """Set ``off``, ``verify`` or ``instant`` and revoke old challenges."""
    mode = str(mode or "").lower()
    if mode not in MODES:
        raise RequestError("Join-request mode must be off, verify, or instant.")
    chat_id = int(chat_id)
    await require_access(chat_id, actor_id, check_bot=True)

    def change(cfg):
        _trim(cfg, time.time())
        cfg["join_request_mode"] = mode
        cfg["join_request_revision"] = _revision(cfg) + 1
        # Challenges are tied to their revision.  Do not retain a historical
        # backup that could become valid after a later settings change.
        cfg["join_request_pending"] = {}

    return await db.mutate_chat(chat_id, change)


def _challenge_markup(chat_id: int, record: dict):
    nonce = record["nonce"]
    buttons = [
        ikb(
            str(value),
            callback_data=f"gh:jr:v:{chat_id}:{nonce}:{index}",
            style=enums.ButtonStyle.PRIMARY,
        )
        for index, value in enumerate(record["choices"])
    ]
    return ikm([buttons])


async def _send_challenge(chat_id: int, user_id: int, record: dict) -> bool:
    """Send a prepared challenge and publish its message binding atomically."""
    group = html.escape(record.get("title") or "this group")
    text = (
        f"Join request for <b>{group}</b>\n\n"
        f"Please answer: <b>{html.escape(record['question'])}</b>"
    )
    try:
        sent = await app.send_message(
            user_id,
            text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_challenge_markup(chat_id, record),
        )
        message_id = int(getattr(sent, "id", 0) or getattr(sent, "message_id", 0))
        if not message_id:
            raise RuntimeError("Telegram did not return a message id")
    except Exception:
        def failed(cfg):
            _trim(cfg, time.time())
            item = _pending(cfg).get(str(user_id))
            if (
                item and item.get("nonce") == record["nonce"]
                and item.get("event_id") == record["event_id"]
            ):
                # A private-chat block is recoverable with /verifyrequests,
                # but it never permits an approval fallback.
                item["delivery"] = "failed"
                item["expires"] = 0
                item["keep_until"] = time.time() + FAILED_DM_SECONDS
        await db.mutate_chat(chat_id, failed)
        return False

    published = False
    def delivered(cfg):
        nonlocal published
        _trim(cfg, time.time())
        item = _pending(cfg).get(str(user_id))
        if (
            item
            and item.get("nonce") == record["nonce"]
            and item.get("event_id") == record["event_id"]
            and _mode(cfg) == "verify"
            and _revision(cfg) == record["revision"]
        ):
            item["message_id"] = message_id
            item["delivery"] = "sent"
            published = True
        elif (
            item and item.get("nonce") == record["nonce"]
            and item.get("event_id") == record["event_id"]
        ):
            _pending(cfg).pop(str(user_id), None)
    await db.mutate_chat(chat_id, delivered)
    return published


async def _start_verification(chat_id: int, user, revision: int, event_id: str,
                              event_date: float, title: str) -> None:
    user_id = int(user.id)
    record = _new_challenge(chat_id, user_id, revision, event_id, event_date, title)
    reserved = False

    def reserve(cfg):
        nonlocal reserved
        _trim(cfg, time.time())
        if _mode(cfg) != "verify" or _revision(cfg) != revision:
            return
        if event_id in _processed(cfg):
            return  # completed/rejected request replay after a restart
        existing = _pending(cfg).get(str(user_id))
        if existing:
            if existing.get("event_id") == event_id:
                return  # same request delivery: never send another DM
            try:
                existing_date = float(existing.get("event_date", 0) or 0)
            except (TypeError, ValueError):
                existing_date = 0
            # An older update cannot revoke a newer request.  An in-flight
            # approval also wins; ingress waits for its lock where possible.
            if event_date < existing_date or existing.get("state") in ("approving", "dispatched"):
                return
        _pending(cfg)[str(user_id)] = record
        reserved = True
    result = await db.mutate_chat(chat_id, reserve)
    item = result.get("join_request_pending", {}).get(str(user_id))
    if not reserved or not item or item.get("nonce") != record["nonce"]:
        return
    await _send_challenge(chat_id, user_id, record)


async def _requester_already_joined(chat_id: int, user_id: int) -> bool:
    try:
        member = await app.get_chat_member(chat_id, user_id)
        return member.status in (
            enums.ChatMemberStatus.OWNER,
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.MEMBER,
            enums.ChatMemberStatus.RESTRICTED,
        )
    except Exception:
        return False


async def _approve_one(chat_id: int, user_id: int, revision: int, mode: str,
                       token: str, event_id: str, event_date: float,
                       *, lock_held: bool = False) -> str:
    """Approve a reserved request while holding its per-user admission lock."""
    key = (chat_id, user_id)
    lock = _approval_locks.setdefault(key, asyncio.Lock())
    async with (_no_lock() if lock_held else lock):
        if await _requester_already_joined(chat_id, user_id):
            def joined(cfg):
                item = _pending(cfg).get(str(user_id))
                if (
                    item and item.get("nonce") == token
                    and item.get("event_id") == event_id
                ):
                    _pending(cfg).pop(str(user_id), None)
                    _remember_processed(cfg, event_id, event_date, "already_joined")
            await db.mutate_chat(chat_id, joined)
            return "joined"

        try:
            await _bot_can_invite(chat_id)
        except RequestError:
            return "rights"
        # Read policy after the final permission lookup.  Disabling/changing
        # mode while that await was in flight therefore wins before the RPC.
        current = await db.get_chat(chat_id)
        if _mode(current) != mode or _revision(current) != revision:
            return "stale"
        # State has already been reserved atomically by the caller.  From
        # this point the RPC is dispatched; a later /off cannot undo an
        # approval Telegram has accepted, so success is recorded truthfully.
        try:
            result = await app.approve_chat_join_request(chat_id, user_id)
        except Exception:
            result = False
        if result is not True:
            def reviewable(cfg):
                item = _pending(cfg).get(str(user_id))
                if (
                    item and item.get("nonce") == token
                    and item.get("event_id") == event_id
                ):
                    item["state"] = "approval_failed"
                    item["keep_until"] = time.time() + FAILED_DM_SECONDS
            await db.mutate_chat(chat_id, reviewable)
            return "failed"

        def complete(cfg):
            _trim(cfg, time.time())
            item = _pending(cfg).get(str(user_id))
            # A successful, already-dispatched Telegram RPC is a fact even
            # if /off changed the revision while it was in flight.  Never
            # erase a newer reapplication, however.
            _remember_processed(cfg, event_id, event_date, "success")
            if item and item.get("nonce") == token and item.get("event_id") == event_id:
                _pending(cfg).pop(str(user_id), None)
            _admitted(cfg)[str(user_id)] = {
                "created": time.time(),
                "until": time.time() + ADMISSION_SECONDS,
                "message_id": 0,
                "event_id": event_id,
            }
        await db.mutate_chat(chat_id, complete)
        return "approved"


async def handle_request(request) -> None:
    """Handle a ChatJoinRequest according to the current mode."""
    chat = getattr(request, "chat", None)
    user = getattr(request, "from_user", None)
    if not chat or not user or not getattr(user, "id", None):
        return
    chat_id, user_id = int(chat.id), int(user.id)
    event_id, event_date = _request_identity(request, user_id)
    # An ingress replay arriving while a request RPC is outstanding waits for
    # its final receipt.  It then sees processed identity rather than sending
    # a second DM or replacing the just-approved request.
    lock = _approval_locks.get((chat_id, user_id))
    if lock and lock.locked():
        async with lock:
            pass
    cfg = await db.get_chat(chat_id)
    mode, revision = _mode(cfg), _revision(cfg)
    if mode == "off":
        return
    if mode == "verify":
        await _start_verification(
            chat_id, user, revision, event_id, event_date, getattr(chat, "title", "")
        )
        return

    # Instant requests still receive a durable reservation so duplicate update
    # deliveries cannot call Telegram twice and failures remain reviewable.
    token = secrets.token_hex(12)
    now = time.time()
    record = {
        "nonce": token, "chat_id": chat_id, "user_id": user_id,
        "revision": revision, "created": now, "expires": now + FAILED_DM_SECONDS,
        "keep_until": now + FAILED_DM_SECONDS, "state": "approving",
        "delivery": "none", "event_id": event_id, "event_date": event_date,
    }
    reserved = False
    def reserve(cfg):
        nonlocal reserved
        _trim(cfg, time.time())
        if _mode(cfg) != "instant" or _revision(cfg) != revision:
            return
        if event_id in _processed(cfg):
            return
        existing = _pending(cfg).get(str(user_id))
        if existing:
            if existing.get("event_id") == event_id:
                return
            try:
                existing_date = float(existing.get("event_date", 0) or 0)
            except (TypeError, ValueError):
                existing_date = 0
            if event_date < existing_date or existing.get("state") in ("approving", "dispatched"):
                return
        _pending(cfg)[str(user_id)] = record
        reserved = True
    await db.mutate_chat(chat_id, reserve)
    if reserved:
        await _approve_one(chat_id, user_id, revision, "instant", token, event_id, event_date)


async def _answer(callback, text: str, *, alert: bool = False) -> None:
    try:
        await callback.answer(text, show_alert=alert)
    except Exception:
        pass


def _parse_callback(data: str):
    parts = (data or "").split(":")
    if len(parts) != 6 or parts[:3] != ["gh", "jr", "v"]:
        return None
    try:
        chat_id, index = int(parts[3]), int(parts[5])
    except (TypeError, ValueError):
        return None
    nonce = parts[4]
    if len(nonce) != 24 or any(c not in "0123456789abcdef" for c in nonce):
        return None
    return chat_id, nonce, index


async def handle_verification(callback) -> None:
    """Consume a bound verification callback; all user-visible failures are safe."""
    parsed = _parse_callback(getattr(callback, "data", ""))
    message, user = getattr(callback, "message", None), getattr(callback, "from_user", None)
    if not parsed or not message or not user or not getattr(user, "id", None):
        await _answer(callback, "This verification is invalid or expired.", alert=True)
        return
    chat_id, nonce, index = parsed
    user_id = int(user.id)
    message_id = int(getattr(message, "id", 0) or getattr(message, "message_id", 0))
    if getattr(getattr(message, "chat", None), "id", None) != user_id:
        await _answer(callback, "This verification belongs in your private chat.", alert=True)
        return

    snapshot = await db.get_chat(chat_id)
    item = snapshot.get("join_request_pending", {}).get(str(user_id))
    now = time.time()
    if not isinstance(item, dict) or (
        item.get("nonce") != nonce
        or item.get("message_id") != message_id
        or item.get("delivery") != "sent"
        or item.get("state", "ready") != "ready"
        or item.get("revision") != _revision(snapshot)
        or _mode(snapshot) != "verify"
        or float(item.get("expires", 0) or 0) <= now
    ):
        await _answer(callback, "This verification expired. Send /verifyrequests for a new one.", alert=True)
        return

    if index != item.get("correct"):
        attempts_left = 0
        def wrong(cfg):
            nonlocal attempts_left
            _trim(cfg, time.time())
            current = _pending(cfg).get(str(user_id))
            if (
                current and current.get("nonce") == nonce
                and current.get("message_id") == message_id
                and current.get("state", "ready") == "ready"
                and current.get("revision") == _revision(cfg)
                and _mode(cfg) == "verify"
            ):
                current["attempts"] = int(current.get("attempts", 0)) + 1
                attempts_left = max(0, 3 - current["attempts"])
                if not attempts_left:
                    _pending(cfg).pop(str(user_id), None)
        await db.mutate_chat(chat_id, wrong)
        await _answer(
            callback,
            "That answer is not correct." if attempts_left else "Too many incorrect answers. Send /verifyrequests to try again.",
            alert=True,
        )
        return

    # Reservation/consumption happens atomically before any await that can
    # lead to an approval.  A restarted or duplicate callback sees state !=
    # sent and cannot approve twice.
    reserved = False
    revision = _revision(snapshot)
    def reserve(cfg):
        nonlocal reserved
        _trim(cfg, time.time())
        current = _pending(cfg).get(str(user_id))
        if (
            current and current.get("nonce") == nonce
            and current.get("message_id") == message_id
            and current.get("delivery") == "sent"
            and current.get("state", "ready") == "ready"
            and current.get("revision") == revision
            and _mode(cfg) == "verify"
            and float(current.get("expires", 0) or 0) > time.time()
        ):
            current["state"] = "approving"
            reserved = True
    # Own the ingress lock before publishing "approving".  Thus a newer
    # reapplication that arrives during the approval RPC waits, then can
    # replace only an older completed/failed request; a same-event replay is
    # recognized from the durable processed receipt.
    lock = _approval_locks.setdefault((chat_id, user_id), asyncio.Lock())
    await lock.acquire()
    try:
        await db.mutate_chat(chat_id, reserve)
        if not reserved:
            await _answer(callback, "This verification was already used or expired.", alert=True)
            return
        outcome = await _approve_one(
            chat_id, user_id, revision, "verify", nonce,
            item["event_id"], float(item.get("event_date", 0) or 0),
            lock_held=True,
        )
    finally:
        lock.release()
    texts = {
        "approved": "Request approved. You can now join the group.",
        "joined": "You are already in the group; no second approval was made.",
        "rights": "I could not verify my Invite Users right. Your request remains pending.",
        "failed": "Telegram could not approve the request. It remains pending for review.",
        "stale": "Join-request settings changed; this verification was not used.",
    }
    await _answer(callback, texts[outcome], alert=outcome != "approved")


async def _optional_allowed(check) -> bool:
    """Evaluate the UI's ephemeral consent closure without persisting it."""
    if check is None:
        return True
    try:
        value = check()
        if hasattr(value, "__await__"):
            value = await value
        return bool(value)
    except Exception:
        return False


async def approve_all(chat_id, actor_id, *, expected_revision=None,
                      still_authorized=None, operation_id=None) -> None:
    """Use Telegram's native bulk endpoint, including requests we never saw.

    ``still_authorized`` is intentionally an in-memory closure supplied by a
    settings confirmation UI.  It is evaluated after all permission/network
    checks and immediately before the native RPC; it is never stored.  A
    short-lived ``operation_id`` consumes a button/message action even when
    Telegram rejects its RPC, preventing a delayed duplicate callback from
    approving requests that arrived later.
    """
    chat_id = int(chat_id)
    if operation_id is not None:
        operation_id = str(operation_id)
        if not operation_id or len(operation_id) > 128:
            raise RequestError("This bulk-approval action is invalid.")
    try:
        expected = None if expected_revision is None else int(expected_revision)
    except (TypeError, ValueError):
        raise RequestError("This bulk-approval action is invalid.") from None
    await require_access(chat_id, actor_id, check_bot=True)

    first = await db.get_chat(chat_id)
    if expected is not None and _revision(first) != expected:
        raise RequestError("Join-request settings changed. Open the panel again.")

    reserved = True
    duplicate_state = "unknown"
    def reserve(cfg):
        nonlocal reserved, duplicate_state
        _trim(cfg, time.time())
        if expected is not None and _revision(cfg) != expected:
            raise RequestError("Join-request settings changed. Open the panel again.")
        if operation_id is not None:
            operations = _bulk_ops(cfg)
            if operation_id in operations:
                reserved = False
                duplicate_state = str(operations[operation_id].get("state") or "unknown")
                return
            operations[operation_id] = {
                "created": time.time(),
                "until": time.time() + BULK_OPERATION_SECONDS,
                "revision": _revision(cfg),
                # An interruption after this mutation is deliberately
                # uncertain, and is never replayed by the old button.
                "state": "unknown",
            }
    await db.mutate_chat(chat_id, reserve)
    if not reserved:
        if duplicate_state == "success":
            raise RequestError(
                "This bulk operation already completed; no new requests were approved."
            )
        raise RequestError(
            "The previous bulk attempt failed or is uncertain. Send a fresh command to retry."
        )

    async def mark_operation(state: str) -> None:
        if operation_id is None:
            return
        def mark(cfg):
            item = _bulk_ops(cfg).get(operation_id)
            if item:
                item["state"] = state
        await db.mutate_chat(chat_id, mark)

    # These final checks intentionally occur *after* the reservation.  A UI
    # cancellation or rights/mode change while the DB mutation was waiting
    # therefore wins.  There is no await between the final consent result and
    # the RPC invocation below.
    try:
        await require_access(chat_id, actor_id, check_bot=True)
        current = await db.get_chat(chat_id)
        if expected is not None and _revision(current) != expected:
            raise RequestError("Join-request settings changed. Open the panel again.")
        if not await _optional_allowed(still_authorized):
            raise RequestError("This bulk-approval confirmation expired or was cancelled.")
    except RequestError:
        await mark_operation("failed")
        raise

    try:
        result = await app.approve_all_chat_join_requests(chat_id)
    except Exception:
        result = False
    if result is not True:
        await mark_operation("failed")
        raise RequestError("Telegram could not approve all pending requests. Try again.")

    def clear(cfg):
        _trim(cfg, time.time())
        # This only clears our old challenges after Telegram confirmed native
        # bulk approval.  It does not invent a count or mark future joins safe.
        cfg["join_request_pending"] = {}
        if operation_id is not None:
            item = _bulk_ops(cfg).get(operation_id)
            if item:
                item["state"] = "success"
    await db.mutate_chat(chat_id, clear)


async def resend_for(user_id) -> int:
    """Retry failed verification DMs for a requester; return messages sent."""
    user_id = int(user_id)
    sent_count = 0
    try:
        # Some deployments expose a public list_chats method; the local JSON
        # database currently calls it all_chats.  Neither path writes a DB
        # record merely to enumerate candidates.
        listing = getattr(db, "list_chats", None)
        chats = await listing() if callable(listing) else await db.all_chats()
    except Exception:
        return 0
    for raw_chat_id in list(chats):
        try:
            chat_id = int(raw_chat_id)
            cfg = await db.get_chat(chat_id)
            old = cfg.get("join_request_pending", {}).get(str(user_id))
            if (
                not isinstance(old, dict)
                or old.get("delivery") != "failed"
                or _mode(cfg) != "verify"
                or float(old.get("keep_until", 0) or 0) <= time.time()
            ):
                continue
            record = _new_challenge(
                chat_id, user_id, _revision(cfg),
                str(old.get("event_id") or f"{user_id}:0.000000:"),
                float(old.get("event_date", 0) or 0), old.get("title", ""),
            )
            replaced = False
            def replace(state):
                nonlocal replaced
                _trim(state, time.time())
                current = _pending(state).get(str(user_id))
                if (
                    current and current.get("nonce") == old.get("nonce")
                    and current.get("event_id") == old.get("event_id")
                    and current.get("delivery") == "failed"
                    and _mode(state) == "verify"
                    and _revision(state) == record["revision"]
                ):
                    _pending(state)[str(user_id)] = record
                    replaced = True
            await db.mutate_chat(chat_id, replace)
            if replaced and await _send_challenge(chat_id, user_id, record):
                sent_count += 1
        except Exception:
            # One inaccessible or deleted group must not prevent the requester
            # from receiving retries for other pending groups.
            continue
    return sent_count


async def is_request_admission(message, user_id) -> bool:
    """Return true only for the one BY_REQUEST service message we approved."""
    chat = getattr(message, "chat", None)
    if not chat or not user_id:
        return False
    if getattr(message, "chat_join_type", None) != enums.ChatJoinType.BY_REQUEST:
        return False
    members = getattr(message, "new_chat_members", None) or ()
    if not any(getattr(member, "id", None) == int(user_id) for member in members):
        return False
    key = (int(chat.id), int(user_id))
    lock = _approval_locks.get(key)
    if lock and lock.locked():
        async with lock:
            pass
    allowed = False
    message_id = int(getattr(message, "id", 0) or getattr(message, "message_id", 0))
    def bind(cfg):
        nonlocal allowed
        _trim(cfg, time.time())
        item = _admitted(cfg).get(str(user_id))
        if not item:
            return
        bound = int(item.get("message_id", 0) or 0)
        if bound == 0 and message_id:
            item["message_id"] = message_id
            allowed = True
        elif bound == message_id and message_id:
            allowed = True
    await db.mutate_chat(int(chat.id), bind)
    return allowed