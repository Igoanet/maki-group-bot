"""Opt-in cleanup for only the current join/leave service message.

``autodel`` intentionally watches ordinary (non-service) messages.  This is a
separate, default-off switch because Telegram service messages have different
semantics and Telegram refuses some of them (notably chat-creation posts).
There is no history scan here: the only message ID ever considered is the ID
of the service update currently being handled.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from pyrogram import enums, errors, filters, raw, types

from ghelp import app
from ghelp.bulk_delete import _is_group_scoped, _prepare_delete_request, delete_right
from ghelp.database import db
from ghelp.helpers import ikb, ikm

logger = logging.getLogger(__name__)

_GROUP_TYPES = (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)
_CALLBACK_RE = re.compile(r"^gh:cs:(on|off):([0-9a-f]{8})$")
_TOKEN_RE = re.compile(r"^[0-9a-f]{8}$")
_MENU_TTL = 600
_SETTING = "cleanservice_on"
SERVICE_TYPES = ("all", "join", "leave", "other", "photo", "pin", "title", "videochat")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _group(message) -> bool:
    chat = getattr(message, "chat", None)
    return bool(chat and getattr(chat, "type", None) in _GROUP_TYPES)


def _service_kind(message) -> str | None:
    """Return the one service kind this plugin is allowed to process."""

    service = getattr(message, "service", None)
    value = getattr(service, "value", service)
    if value == enums.MessageServiceType.NEW_CHAT_MEMBERS.value:
        return "join"
    if value == enums.MessageServiceType.LEFT_CHAT_MEMBER.value:
        return "leave"
    if getattr(message, "new_chat_members", None) is not None:
        return "join"
    if getattr(message, "left_chat_member", None) is not None:
        return "leave"
    name = str(getattr(service, "name", service)).upper()
    if "PINNED" in name:
        return "pin"
    if "PHOTO" in name or "BACKGROUND" in name:
        return "photo"
    if "TITLE" in name:
        return "title"
    if "VIDEO_CHAT" in name or "CALL" in name:
        return "videochat"
    if service is not None:
        return "other"
    return None


def _enabled(cfg: dict, kind: str) -> bool:
    selected = cfg.get("cleanservice_types")
    if isinstance(selected, (list, tuple, set)):
        return "all" in selected or kind in selected
    # Existing installations used a join/leave-only switch.  Preserve its
    # deliberately narrow behavior; new category settings are explicit.
    return bool(cfg.get(_SETTING, False)) and kind in ("join", "leave")


def _migration_or_creation(message) -> bool:
    """Reject migration markers even if a malformed update has join fields."""

    if any(
        getattr(message, name, None) is not None
        for name in ("migrate_from_chat_id", "migrate_to_chat_id")
    ):
        return True
    # These values are intentionally listed for readability and for SDK
    # compatibility.  _service_kind already rejects all of them.
    service = getattr(message, "service", None)
    value = getattr(service, "value", service)
    return value in {
        enums.MessageServiceType.GROUP_CHAT_CREATED.value,
        enums.MessageServiceType.SUPERGROUP_CHAT_CREATED.value,
        enums.MessageServiceType.CHANNEL_CHAT_CREATED.value,
        enums.MessageServiceType.MIGRATE_FROM_CHAT_ID.value,
        enums.MessageServiceType.MIGRATE_TO_CHAT_ID.value,
    }


def _member_ids(message, kind: str) -> tuple[int, ...]:
    if kind == "join":
        members = getattr(message, "new_chat_members", None) or ()
        return tuple(
            user.id for user in members
            if _is_int(getattr(user, "id", None))
        )
    user = getattr(message, "left_chat_member", None)
    user_id = getattr(user, "id", None)
    return (user_id,) if _is_int(user_id) else ()


def _status_text(enabled: bool) -> str:
    return (
        f"<b>Service-message cleanup</b>: <b>{'ON' if enabled else 'OFF'}</b>.\n"
        "Only new join/leave messages."
    )


def _keyboard(token: str, enabled: bool):
    return ikm(
        [[
            ikb(
                "ON",
                callback_data=f"gh:cs:on:{token}",
                style=enums.ButtonStyle.SUCCESS,
            ),
            ikb(
                "OFF",
                callback_data=f"gh:cs:off:{token}",
                style=enums.ButtonStyle.PRIMARY,
            ),
        ]]
    )


@dataclass
class MenuState:
    token: str
    chat_id: int
    actor_id: int
    card_id: int = 0
    created: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


_menus: dict[str, MenuState] = {}


def _cleanup_menus() -> None:
    now = time.monotonic()
    for token, item in list(_menus.items()):
        if now - item.created > _MENU_TTL:
            _menus.pop(token, None)


async def _member_has_delete_right(chat_id: int, user_id: int) -> bool:
    """Check the command actor's live personal Delete messages right."""

    try:
        member = await app.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    if member.status == enums.ChatMemberStatus.OWNER:
        return True
    return bool(
        member.status == enums.ChatMemberStatus.ADMINISTRATOR
        and getattr(getattr(member, "privileges", None), "can_delete_messages", False)
    )


async def _actor_can_manage(chat_id: int, user_id: int) -> bool:
    if not _is_int(user_id):
        return False
    return await _member_has_delete_right(chat_id, user_id)


async def _bot_can_manage(chat_id: int) -> bool:
    """Maki's own right is checked live; this is never inferred from config."""

    try:
        return await delete_right(app, chat_id)
    except Exception:
        return False


async def _send_denial(message) -> None:
    try:
        await message.reply_text(
            "The admin and Maki both need the live "
            "<b>Delete messages</b> right.",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        pass


async def _show_menu(message, *, enabled: bool | None = None) -> None:
    _cleanup_menus()
    actor = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    if actor is None or chat is None or not _is_int(getattr(chat, "id", None)):
        return
    cfg = await db.get_chat(chat.id)
    if enabled is None:
        enabled = bool(cfg.get(_SETTING, False))
    item = MenuState(
        token=uuid.uuid4().hex[:8],
        chat_id=chat.id,
        actor_id=actor.id,
        created=time.monotonic(),
    )
    _menus[item.token] = item
    try:
        card = await message.reply_text(
            _status_text(enabled),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_keyboard(item.token, enabled),
        )
        if _menus.get(item.token) is item:
            item.card_id = getattr(card, "id", 0)
            return
    except Exception:
        _menus.pop(item.token, None)
        raise


async def _set_enabled(chat_id: int, enabled: bool) -> bool:
    """Persist one toggle, rolling it back if live authorization disappears."""

    before = bool((await db.get_chat(chat_id)).get(_SETTING, False))
    await db.update_chat(chat_id, **{_SETTING: enabled})
    # A demotion/revocation during the database await must not leave a stale
    # callback or command as the apparent actor of a setting change.  The
    # caller performs the actor and bot checks again around this operation.
    return before


async def _set_for_actor(chat_id: int, actor_id: int, enabled: bool) -> bool:
    if not await _actor_can_manage(chat_id, actor_id):
        return False
    if not await _bot_can_manage(chat_id):
        return False
    before = await _set_enabled(chat_id, enabled)
    # The bot lookup comes first.  It can itself yield to a live membership
    # update in an offline/fake client; the actor check must be the last
    # awaited authorization immediately before this write is accepted.
    try:
        bot_ok = await _bot_can_manage(chat_id)
        actor_ok = await _actor_can_manage(chat_id, actor_id)
    except Exception:
        bot_ok = actor_ok = False
    if bot_ok and actor_ok:
        return True
    # Revoke a setting changed during a permission await.  This is a
    # non-destructive setting, so fail closed rather than keeping an
    # unauthorized write.
    try:
        await db.update_chat(chat_id, **{_SETTING: before})
    except Exception:
        logger.warning(
            "cleanservice setting rollback failed chat_id=%s",
            chat_id,
        )
    return False


async def cleanservice_command(_, message: types.Message) -> None:
    if not _group(message):
        return
    actor = getattr(message, "from_user", None)
    actor_id = getattr(actor, "id", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    if not _is_int(chat_id) or not await _actor_can_manage(chat_id, actor_id):
        await _send_denial(message)
        return
    if not await _bot_can_manage(chat_id):
        await _send_denial(message)
        return

    args = list((getattr(message, "command", None) or [])[1:])
    args = [str(value).lower() for value in args]
    enabled = None
    if len(args) == 1 and args[0] in ("on", "off"):
        enabled = args[0] == "on"
        if not await _set_for_actor(chat_id, actor_id, enabled):
            await _send_denial(message)
            return
    elif args:
        selected = {"all" if x in ("yes", "on") else x for x in args}
        if selected <= set(SERVICE_TYPES):
            current = set() if "off" in args or "no" in args else selected
            await db.update_chat(
                chat_id,
                cleanservice_types=sorted(current),
                cleanservice_on=bool(current),
            )
            enabled = bool(current)
        else:
            enabled = None
        # Keep the command nondestructive and useful for typos: show the
        # current state rather than changing anything.
        enabled = None
    await _show_menu(message, enabled=enabled)


cleanservice_command = app.on_message(
    filters.command("cleanservice", prefixes=["/", "?", "!", "."]) & filters.group,
    group=15,
)(cleanservice_command)


async def _keepservice_command(_, message: types.Message) -> None:
    if not _group(message):
        return
    actor = getattr(getattr(message, "from_user", None), "id", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    if not _is_int(chat_id) or not await _actor_can_manage(chat_id, actor):
        await _send_denial(message)
        return
    args = [str(x).lower() for x in (getattr(message, "command", None) or [])[1:]]
    cfg = await db.get_chat(chat_id)
    current = set(cfg.get("cleanservice_types", []))
    if "all" in args:
        current.clear()
    else:
        current -= set(args).intersection(SERVICE_TYPES)
    await db.update_chat(chat_id, cleanservice_types=sorted(current), cleanservice_on=bool(current))
    await message.reply_text("Service cleanup: " + (", ".join(sorted(current)) if current else "off"))


keepservice_command = app.on_message(
    filters.command(["keepservice", "nocleanservice"], prefixes=["/", "?", "!", "."]) & filters.group,
    group=15,
)(_keepservice_command)


async def _cleanservicetypes_command(_, message: types.Message) -> None:
    if _group(message):
        await message.reply_text(
            "Service types: all, join, leave, other, photo, pin, title, videochat"
        )


cleanservicetypes_command = app.on_message(
    filters.command("cleanservicetypes", prefixes=["/", "?", "!", "."]) & filters.group,
    group=15,
)(_cleanservicetypes_command)


def _callback_item(query, action: str, token: str) -> MenuState | None:
    _cleanup_menus()
    item = _menus.get(token)
    message = getattr(query, "message", None)
    actor = getattr(query, "from_user", None)
    chat = getattr(message, "chat", None)
    if (
        item is None
        or not _TOKEN_RE.fullmatch(token)
        or action not in ("on", "off")
        or message is None
        or actor is None
        or getattr(actor, "id", None) != item.actor_id
        or getattr(message, "id", None) != item.card_id
        or getattr(chat, "id", None) != item.chat_id
        or getattr(chat, "type", None) not in _GROUP_TYPES
    ):
        return None
    return item


async def _answer(query, text: str | None = None, *, alert: bool = False) -> None:
    try:
        await query.answer(text or "", show_alert=alert)
    except Exception:
        pass


@app.on_callback_query(filters.regex(_CALLBACK_RE.pattern))
async def cleanservice_callback(_, query: types.CallbackQuery) -> None:
    data = getattr(query, "data", "") or ""
    match = _CALLBACK_RE.fullmatch(data)
    if not match:
        await _answer(query, "Stale button.", alert=True)
        return
    action, token = match.groups()
    item = _callback_item(query, action, token)
    if item is None:
        await _answer(query, "This button has expired.", alert=True)
        return
    async with item.lock:
        if not await _actor_can_manage(item.chat_id, item.actor_id):
            await _answer(query, "The admin no longer has this right.", alert=True)
            return
        if not await _bot_can_manage(item.chat_id):
            await _answer(query, "Maki no longer has Delete messages permission.", alert=True)
            return
        enabled = action == "on"
        before = bool((await db.get_chat(item.chat_id)).get(_SETTING, False))
        if not await _set_for_actor(item.chat_id, item.actor_id, enabled):
            await _answer(query, "Authorization changed. Open the menu again.", alert=True)
            return
        cfg = await db.get_chat(item.chat_id)
        # The setting write already had a final bot-then-actor check.  These
        # extra checks protect the callback's status edit after the DB read;
        # if either live check fails, restore the pre-callback value before
        # denying the callback.  Never deny after a committed setting write
        # while leaving that write in place.
        try:
            bot_ok = await _bot_can_manage(item.chat_id)
            actor_ok = await _actor_can_manage(item.chat_id, item.actor_id)
        except Exception:
            bot_ok = actor_ok = False
        if not bot_ok or not actor_ok:
            try:
                await db.update_chat(item.chat_id, **{_SETTING: before})
            except Exception:
                logger.warning(
                    "cleanservice setting rollback failed chat_id=%s",
                    item.chat_id,
                )
            await _answer(query, "Live authorization changed.", alert=True)
            return
        try:
            await query.message.edit_text(
                _status_text(bool(cfg.get(_SETTING, False))),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=_keyboard(item.token, bool(cfg.get(_SETTING, False))),
            )
        except Exception:
            await _answer(query, "Could not update the status.", alert=True)
            return
    await _answer(query)


async def _bot_id() -> int | None:
    me = getattr(app, "me", None)
    if me is not None and _is_int(getattr(me, "id", None)):
        return me.id
    try:
        me = await app.get_me()
    except Exception:
        return None
    return getattr(me, "id", None) if _is_int(getattr(me, "id", None)) else None


def _maki_added(message, kind: str, bot_id: int) -> bool:
    ids = _member_ids(message, kind)
    if bot_id in ids:
        return True
    # A join service message authored by Maki means Maki added the member;
    # leave events are not excluded merely because Maki performed moderation.
    actor = getattr(message, "from_user", None)
    return kind == "join" and getattr(actor, "id", None) == bot_id


async def _exact_service(message, kind: str, bot_id: int):
    """Fetch exactly the update's message ID and re-check its event shape."""

    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(message, "id", None)
    if (
        not _is_int(chat_id)
        or not _is_int(message_id)
        or _migration_or_creation(message)
        or _maki_added(message, kind, bot_id)
    ):
        return None
    try:
        fetched = await app.get_messages(chat_id, [message_id], replies=0)
    except TypeError:
        fetched = await app.get_messages(chat_id, [message_id])
    if not isinstance(fetched, (list, tuple)) or len(fetched) != 1:
        return None
    current = fetched[0]
    if (
        current is None
        or getattr(current, "empty", False)
        or getattr(current, "id", None) != message_id
        or getattr(getattr(current, "chat", None), "id", None) != chat_id
        or getattr(getattr(current, "chat", None), "type", None) not in _GROUP_TYPES
        or _service_kind(current) != kind
        or _migration_or_creation(current)
        or _maki_added(current, kind, bot_id)
        or not _member_ids(current, kind)
    ):
        return None
    return current


async def _delete_exact(
    chat_id: int,
    message_id: int,
    kind: str,
    initial_cfg: dict,
    expected_chat_type,
):
    """Delete one verified service message, with no retry or background task."""

    cfg = initial_cfg
    if not _enabled(cfg, kind):
        return
    # This read follows the exact-message await and closes the setting
    # revocation window before any destructive call.
    cfg = await db.get_chat(chat_id)
    if not _enabled(cfg, kind):
        return
    # Resolve the exact peer/ID before final authorization.  The raw invoke
    # below deliberately disables the SDK's default retry/sleep loop; a
    # retry after FloodWait must never bypass a fresh policy check.
    try:
        request = await _prepare_delete_request(app, chat_id, [message_id])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "cleanservice refusal chat_id=%s message_id=%s service=%s error=%s",
            chat_id,
            message_id,
            kind,
            type(exc).__name__,
        )
        return
    # A channel-scoped request carries the only safe namespace for a
    # supergroup. Never invoke it unless the resolved channel is exactly the
    # event's chat. Conversely, a bare messages.DeleteMessages request is
    # valid for a basic group, but must never be sent for a supergroup: IDs
    # are account-wide there and a stale/wrong peer could erase another
    # chat's message with the same ID.
    channel_request = isinstance(request, raw.functions.channels.DeleteMessages)
    if channel_request:
        request_is_safe = (
            expected_chat_type == enums.ChatType.SUPERGROUP
            and _is_group_scoped(request, chat_id)
        )
    else:
        request_is_safe = expected_chat_type == enums.ChatType.GROUP
    if not request_is_safe:
        logger.warning(
            "cleanservice refusal chat_id=%s message_id=%s service=%s error=wrong_request_transport",
            chat_id,
            message_id,
            kind,
        )
        return
    if not await _bot_can_manage(chat_id):
        logger.info(
            "cleanservice refusal chat_id=%s message_id=%s service=%s error=no_delete_right",
            chat_id,
            message_id,
            kind,
        )
        return
    # Re-read policy after the live-right await; only the delete await follows.
    cfg = await db.get_chat(chat_id)
    if not _enabled(cfg, kind):
        return
    try:
        await app.invoke(
            request,
            retries=1,
            sleep_threshold=0,
            retry_delay=0,
        )
    except asyncio.CancelledError:
        logger.info(
            "cleanservice cancellation chat_id=%s message_id=%s service=%s",
            chat_id,
            message_id,
            kind,
        )
        raise
    except errors.FloodWait:
        logger.warning(
            "cleanservice refusal chat_id=%s message_id=%s service=%s error=FloodWait",
            chat_id,
            message_id,
            kind,
        )
    except Exception as exc:
        logger.warning(
            "cleanservice refusal chat_id=%s message_id=%s service=%s error=%s",
            chat_id,
            message_id,
            kind,
            type(exc).__name__,
        )
        return

    # A successful RPC is not used as a deletion claim. Telegram may accept a
    # mixed/protected service update while leaving this exact ID present, so
    # verify the same ID and peer once; an unreadable result stays unknown.
    try:
        fetched = await app.get_messages(chat_id, [message_id], replies=0)
        confirmed = (
            isinstance(fetched, (list, tuple))
            and len(fetched) == 1
            and getattr(fetched[0], "id", None) == message_id
            and getattr(getattr(fetched[0], "chat", None), "id", None) == chat_id
            and bool(getattr(fetched[0], "empty", False))
        )
    except asyncio.CancelledError:
        logger.info(
            "cleanservice cancellation chat_id=%s message_id=%s service=%s",
            chat_id,
            message_id,
            kind,
        )
        raise
    except Exception as exc:
        logger.warning(
            "cleanservice refusal chat_id=%s message_id=%s service=%s error=%s",
            chat_id,
            message_id,
            kind,
            type(exc).__name__,
        )
        return
    if confirmed:
        logger.info(
            "cleanservice removed chat_id=%s message_id=%s service=%s",
            chat_id,
            message_id,
            kind,
        )
    else:
        logger.warning(
            "cleanservice refusal chat_id=%s message_id=%s service=%s error=unverified",
            chat_id,
            message_id,
            kind,
        )


@app.on_message(
    filters.group & filters.service,
    group=15,
)
async def cleanservice_watcher(_, message: types.Message) -> None:
    kind = _service_kind(message)
    if kind is None or not _group(message):
        return
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    if not _is_int(chat_id):
        return
    try:
        cfg = await db.get_chat(chat_id)
        if not _enabled(cfg, kind):
            return
        bot_id = await _bot_id()
        if bot_id is None:
            return
        # This await is followed by a fresh policy read in _delete_exact.
        current = await _exact_service(message, kind, bot_id)
        if current is None:
            return
        await _delete_exact(
            chat_id,
            current.id,
            kind,
            cfg,
            getattr(getattr(current, "chat", None), "type", None),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "cleanservice refusal chat_id=%s message_id=%s service=%s error=%s",
            chat_id,
            getattr(message, "id", None),
            kind,
            type(exc).__name__,
        )