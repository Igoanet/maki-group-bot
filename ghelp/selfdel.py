"""Auto-delete every bot-authored group output after one hour."""

import asyncio
import functools
import logging

from pyrogram import enums

DELETE_AFTER = 60 * 60
logger = logging.getLogger(__name__)

_GROUPS = (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)
_GROUP_VALUES = {"group", "supergroup"}
_pending: dict[tuple[int, int], asyncio.Task] = {}

_OUTPUT_METHODS = (
    "send_message",
    "send_photo",
    "send_audio",
    "send_document",
    "send_video",
    "send_animation",
    "send_voice",
    "send_video_note",
    "send_sticker",
    "send_location",
    "send_venue",
    "send_contact",
    "send_poll",
    "send_dice",
    "send_game",
    "send_cached_media",
    "send_media_group",
    "copy_message",
    "forward_messages",
    "edit_message_text",
    "edit_message_caption",
    "edit_message_media",
    "edit_message_reply_markup",
)


async def _del_later(chat_id: int, msg_id: int) -> None:
    try:
        await asyncio.sleep(DELETE_AFTER)
        from ghelp import app

        await app.delete_messages(chat_id, msg_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.info(
            "selfdel: could not delete group message chat=%s message=%s",
            chat_id,
            msg_id,
        )
    finally:
        current = asyncio.current_task()
        if _pending.get((chat_id, msg_id)) is current:
            _pending.pop((chat_id, msg_id), None)


def _is_group_message(msg) -> bool:
    chat_type = getattr(getattr(msg, "chat", None), "type", None)
    value = str(getattr(chat_type, "value", chat_type)).lower()
    return chat_type in _GROUPS or value in _GROUP_VALUES


def arm_delete(msg) -> None:
    """Schedule each returned group message exactly once."""
    try:
        if isinstance(msg, (list, tuple)):
            for item in msg:
                arm_delete(item)
            return
        if msg is None or not _is_group_message(msg):
            return
        key = (int(msg.chat.id), int(msg.id))
        current = _pending.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.get_running_loop().create_task(_del_later(*key))
        _pending[key] = task
    except Exception:
        pass


def install_client_cleanup(client) -> None:
    """Intercept all normal Pyrogram send/edit APIs on this client."""
    if getattr(client, "_maki_selfdel_installed", False):
        return
    for method_name in _OUTPUT_METHODS:
        original = getattr(client, method_name, None)
        if not callable(original):
            continue

        @functools.wraps(original)
        async def wrapped(*args, __original=original, **kwargs):
            sent = await __original(*args, **kwargs)
            arm_delete(sent)
            return sent

        setattr(client, method_name, wrapped)
    setattr(client, "_maki_selfdel_installed", True)


def autodel_reply(func):
    """Compatibility decorator; all returned group outputs now expire."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        kwargs.pop("preserve", False)
        sent = await func(*args, **kwargs)
        arm_delete(sent)
        return sent

    return wrapper
