"""Safe, truthful replies for moderation actions.

Moderation API calls and their notices deliberately live in separate failure
domains.  Telegram may accept a ban and then reject the reply (for example
when the command message was deleted), and a rejected notice must never make
the already completed action look like a failed one.
"""

import html
import logging
from html.parser import HTMLParser

from pyrogram import enums, types

from ghelp import app
from ghelp.helpers import concise_bot_text


logger = logging.getLogger(__name__)
_MAX_FIELD_CHARS = 1024
_MAX_DURATION_CHARS = 256
_MAX_NAME_CHARS = 256
_MAX_MESSAGE_CHARS = 4096

_REPLY_ONLY_KWARGS = {
    "quote",
    "reply_to",
    "reply_to_message_id",
    "reply_parameters",
}


def _escaped_clip(value, limit: int = _MAX_FIELD_CHARS) -> str:
    """Escape and clip one user-controlled field without cutting an entity."""
    if not value:
        return ""
    result: list[str] = []
    used = 0
    truncated = False
    for char in str(value):
        escaped = html.escape(char)
        if used + len(escaped) > limit:
            truncated = True
            break
        result.append(escaped)
        used += len(escaped)
    if truncated and used < limit:
        result.append("…")
    return "".join(result)


class _PlainTextParser(HTMLParser):
    """Remove tags while retaining decoded text for the plain fallback."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"br", "p", "div", "li"}:
            self.parts.append("\n")


def _plain_text(value: str) -> str:
    parser = _PlainTextParser()
    try:
        parser.feed(value)
        parser.close()
        return html.unescape("".join(parser.parts))
    except Exception:
        # Keep the final notification useful even for malformed HTML supplied
        # by another plugin.  This branch contains no network or user logging.
        return html.unescape(value.replace("<", "").replace(">", ""))


def _display_name(user: types.User | None) -> str:
    if not user:
        return "user"
    return (
        getattr(user, "first_name", None)
        or getattr(user, "username", None)
        or "user"
    )


def user_label(user: types.User | None) -> str:
    """Render a user as an escaped clickable name followed by their ID."""
    if not user or not getattr(user, "id", None):
        return "<i>unknown user</i>"
    return (
        f'<a href="tg://user?id={user.id}">'
        f"{_escaped_clip(_display_name(user), _MAX_NAME_CHARS)}</a> "
        f"(<code>{user.id}</code>)"
    )


def actor_label(message: types.Message) -> str:
    """Render the command actor, including anonymous-chat actors."""
    user = getattr(message, "from_user", None)
    if user and getattr(user, "id", None):
        return user_label(user)
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat and getattr(sender_chat, "id", None):
        title = (
            getattr(sender_chat, "title", None)
            or getattr(sender_chat, "username", None)
            or "anonymous admin"
        )
        return (
            f"<b>{_escaped_clip(title, _MAX_NAME_CHARS)}</b> "
            f"(<code>{sender_chat.id}</code>)"
        )
    return "<i>anonymous admin</i>"


def _optional_lines(*, reason: str = "", duration: str = "") -> list[str]:
    lines: list[str] = []
    if duration:
        lines.append(
            f"<b>Duration:</b> "
            f"{_escaped_clip(duration, _MAX_DURATION_CHARS)}"
        )
    if reason:
        lines.append(
            f"<b>Reason:</b> {_escaped_clip(reason, _MAX_FIELD_CHARS)}"
        )
    return lines


def action_text(
    message: types.Message,
    verb: str,
    target: types.User,
    *,
    reason: str = "",
    duration: str = "",
    detail: str = "",
) -> str:
    """Build the common success notice without interpolating raw user data."""
    text = (
        f"Done: <b>{html.escape(verb)}</b> {user_label(target)}.\n"
        f"<b>By:</b> {actor_label(message)}"
    )
    lines = _optional_lines(reason=reason, duration=duration)
    if detail:
        lines.append(_escaped_clip(detail, _MAX_FIELD_CHARS))
    if lines:
        text += "\n" + "\n".join(lines)
    return text[:_MAX_MESSAGE_CHARS]


def failure_text(
    message: types.Message,
    verb: str,
    target: types.User,
    *,
    reason: str = "",
    duration: str = "",
    detail: str = "",
) -> str:
    """Build a truthful failed/partial-action notice."""
    text = (
        f"Warning: Could not <b>{html.escape(verb)}</b> "
        f"{user_label(target)}.\n"
        f"<b>By:</b> {actor_label(message)}"
    )
    lines = _optional_lines(reason=reason, duration=duration)
    if detail:
        lines.append(_escaped_clip(detail, _MAX_FIELD_CHARS))
    if lines:
        text += "\n" + "\n".join(lines)
    return text[:_MAX_MESSAGE_CHARS]


async def send_reply(
    message: types.Message,
    text: str,
    *,
    concise: bool = True,
    client=None,
    **kwargs,
):
    """Reply once, then retry as a standalone message if the reply is gone.

    This helper intentionally swallows notification errors.  Callers invoke
    it only after an action has reached its final state, so a formatting,
    deleted-message, or network error in this path cannot alter action truth.
    The fallback strips every reply-only argument, including ``quote``.
    """
    if client is None:
        client = app
    if concise:
        text = concise_bot_text(text)
    kwargs = dict(kwargs)
    kwargs.pop("parse_mode", None)
    try:
        return await message.reply_text(
            text, parse_mode=enums.ParseMode.HTML, **kwargs
        )
    except Exception:
        fallback_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key not in _REPLY_ONLY_KWARGS
        }
        try:
            return await client.send_message(
                message.chat.id,
                text,
                parse_mode=enums.ParseMode.HTML,
                **fallback_kwargs,
            )
        except Exception:
            # A malformed pre-existing command text should not prevent a
            # standalone plain-text notice from being delivered.
            try:
                plain = _plain_text(text)
                return await client.send_message(
                    message.chat.id,
                    plain,
                    parse_mode=enums.ParseMode.DISABLED,
                    **fallback_kwargs,
                )
            except Exception as exc:
                logger.warning(
                    "reply delivery failed after HTML and plain fallbacks: %s",
                    type(exc).__name__,
                )
                return None