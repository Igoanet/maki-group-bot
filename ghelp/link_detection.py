"""Shared link detection."""

import re

from pyrogram import enums

_URL = re.compile(r"(?i)(?:https?://|tg://|www\.|(?:t\.me|telegram\.me|telegram\.dog)/)[^\s<>]+")


# A username mention is NOT a posted link.  Telegram itself rewrites
# "@name" into a link, and inline mentions use tg://user?id=..., so naively
# treating every URL as a violation deleted innocent messages that merely
# mentioned someone (owner 2026-10-18: "do not delete the message with just
# username").  Real links -- hidden text links, invites, message links and
# plain URLs -- are still caught.
_PROFILE_ONLY = re.compile(
    r"(?i)^(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/[A-Za-z0-9_]{4,32}/?$"
)
_TG_MENTION = re.compile(r"(?i)^tg://(?:user\?id=\d+|resolve\?domain=[A-Za-z0-9_]+)$")


def is_username_reference(value: str) -> bool:
    """True when this "link" is only a username / inline mention."""
    v = (value or "").strip()
    if not v:
        return False
    return bool(_PROFILE_ONLY.match(v) or _TG_MENTION.match(v))


def urls(message, *, buttons: bool = False) -> list[str]:
    result = []
    for text, entities in (
        (getattr(message, "text", None) or "", getattr(message, "entities", None) or []),
        (getattr(message, "caption", None) or "", getattr(message, "caption_entities", None) or []),
    ):
        encoded = text.encode("utf-16-le")
        for entity in entities:
            if entity.type == enums.MessageEntityType.TEXT_LINK:
                value = getattr(entity, "url", None)
                if value:
                    result.append(value)
            elif entity.type == enums.MessageEntityType.URL:
                start, end = entity.offset * 2, (entity.offset + entity.length) * 2
                result.append(encoded[start:end].decode("utf-16-le", errors="replace"))
        result.extend(match.group(0) for match in _URL.finditer(text))
    if buttons:
        markup = getattr(message, "reply_markup", None)
        for row in getattr(markup, "inline_keyboard", None) or []:
            for button in row:
                url = getattr(button, "url", None)
                if url:
                    result.append(url)
    # drop username mentions: only genuine links remain, so a message that
    # merely tags someone is never treated as a link violation.
    return [value for value in result if not is_username_reference(value)]


def has_links(message, *, buttons: bool = False) -> bool:
    return bool(urls(message, buttons=buttons))


def is_whisper(message, bot_id: int, bot_username: str | None = None) -> bool:
    """The whisper feature was removed, so nothing is a whisper (2026-09-30)."""
    return False
