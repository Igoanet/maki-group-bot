"""Small, bot-only message type predicates shared by cleanup plugins.

Telegram exposes several different attributes for forwarded and media
messages across Pyrogram releases.  Keep the compatibility checks in one
place so a cleanup command never has to infer a sender from a message that
was not actually returned by Telegram.
"""

from __future__ import annotations


SCOPES = ("all", "text", "media", "stickers", "gifs", "forwards")


def is_forward(message) -> bool:
    """Return whether Telegram marked *message* as forwarded."""

    return any(
        getattr(message, name, None) is not None
        for name in (
            "forward_origin",
            "forward_from",
            "forward_from_chat",
            "forward_sender_name",
        )
    )


def is_sticker(message) -> bool:
    return getattr(message, "sticker", None) is not None


def is_gif(message) -> bool:
    if getattr(message, "animation", None) is not None:
        return True
    document = getattr(message, "document", None)
    return str(getattr(document, "mime_type", "") or "").lower() == "image/gif"


def is_media(message) -> bool:
    """Return true for ordinary media, excluding stickers and GIFs.

    Forwarding is a separate user-selectable scope.  A forwarded photo is
    therefore only selected by ``forwards`` (or ``all``), rather than being
    unexpectedly removed by the ordinary ``media`` choice.
    """

    if is_sticker(message) or is_gif(message) or is_forward(message):
        return False
    return any(
        getattr(message, name, None) is not None
        for name in (
            "photo",
            "video",
            "document",
            "audio",
            "voice",
            "video_note",
            "contact",
            "location",
            "venue",
            "poll",
        )
    )


def is_text(message) -> bool:
    """Only plain text messages, not captions attached to media."""

    return bool(getattr(message, "text", None)) and not is_forward(message) and not any(
        getattr(message, name, None) is not None
        for name in (
            "photo",
            "video",
            "document",
            "audio",
            "voice",
            "video_note",
            "sticker",
            "animation",
            "contact",
            "location",
            "venue",
            "poll",
        )
    )


def matches_scope(message, scope: str | None) -> bool:
    """Match only a known scope; malformed persisted values fail closed."""

    if scope == "all":
        return True
    if scope == "text":
        return is_text(message)
    if scope == "media":
        return is_media(message)
    if scope == "stickers":
        return is_sticker(message)
    if scope == "gifs":
        return is_gif(message)
    if scope == "forwards":
        return is_forward(message)
    return False