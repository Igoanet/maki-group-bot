"""Rose-style keyword filters.

Filters are deliberately kept in the ordinary per-chat JSON settings.  A
filter value is either a saved text string or the same native-media record
used by notes and greetings.
"""

from __future__ import annotations

import html
import re
import shlex

from pyrogram import enums, filters as pyrogram_filters, types

from ghelp import app
from ghelp.database import db
from ghelp.helpers import (
    has_right,
    close_kb,
    ikb,
    ikm,
    is_admin,
    media_of,
    require_admin,
    send_saved,
    note_controls,
    unsupported_media_of,
)

PRE = ["/", "?", "!", "."]


def _key(value: str) -> str:
    return " ".join(value.strip().strip("\"'").split()).casefold()


def _filter_args(source: str) -> tuple[str, str]:
    """Parse a quoted trigger while keeping the response readable."""
    pieces = source.split(maxsplit=1)
    rest = pieces[1] if len(pieces) > 1 else ""
    if not rest:
        return "", ""
    try:
        tokens = shlex.split(rest)
    except ValueError:
        tokens = rest.split()
    return (tokens[0], " ".join(tokens[1:])) if tokens else ("", "")


def _entries(value) -> dict:
    """Accept the current mapping and tolerate an old list-shaped export."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        result = {}
        for item in value:
            if isinstance(item, dict) and item.get("keyword") is not None:
                result[_key(str(item["keyword"]))] = item.get("response", "")
        return result
    return {}


def _entry_from_reply(message: types.Message, explicit: str):
    replied = message.reply_to_message
    if replied is None:
        return explicit.strip() or None
    kind, file_id = media_of(replied)
    unsupported = unsupported_media_of(replied)
    if unsupported:
        raise ValueError(
            "That message type is not supported. Reply to text, photo, video, "
            "animation, audio, voice, document, sticker, or video note."
        )
    body = replied.text or replied.caption
    if body is not None:
        body = getattr(body, "html", None) or str(body)
    body = explicit.strip() or (body or "")
    if kind:
        if not file_id:
            raise ValueError("That media cannot be reused. Send it again, then reply.")
        return {"type": kind, "file_id": file_id, "text": body}
    return body or None


def _matches(text: str, keyword: str) -> bool:
    # \w boundaries make "he" not match "the", while still permitting
    # punctuation immediately around a phrase.  IGNORECASE handles ordinary
    # Telegram text without changing the authored keyword.
    return re.search(
        rf"(?<!\w){re.escape(keyword)}(?!\w)", text, flags=re.IGNORECASE
    ) is not None


@app.on_message(
    pyrogram_filters.group
     & pyrogram_filters.command(["filter", "addfilter"], prefixes=PRE)
)
async def add_filter(_: object, m: types.Message) -> None:
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    source = m.text or m.caption or ""
    keyword, explicit = _filter_args(source)
    if not keyword:
        await m.reply_text(
            "Use <code>/filter keyword response</code>, or reply to a "
            "message/media with <code>/filter keyword</code>.",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    try:
        response = _entry_from_reply(m, explicit)
        if response is None:
            raise ValueError("Add a response or reply to a message.")
        normalized = _key(keyword)
        if not normalized:
            raise ValueError("The keyword cannot be empty.")
        chat = await db.get_chat(m.chat.id)
        saved = _entries(chat.get("filters"))
        saved[normalized] = response
        await db.update_chat(m.chat.id, filters=saved)
        await m.reply_text(
            f"Filter <code>{html.escape(keyword)}</code> saved.",
            parse_mode=enums.ParseMode.HTML,
        )
    except ValueError as exc:
        await m.reply_text(str(exc), parse_mode=enums.ParseMode.HTML)
    except Exception:
        await m.reply_text("Could not save that filter.", parse_mode=enums.ParseMode.HTML)


@app.on_message(
    pyrogram_filters.group
     & pyrogram_filters.command(["stop", "stopfilter"], prefixes=PRE)
)
async def stop_filter(_: object, m: types.Message) -> None:
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    keyword, _ = _filter_args(m.text or m.caption or "")
    normalized = _key(keyword)
    if not normalized:
        await m.reply_text("Use <code>/stop keyword</code>.", parse_mode=enums.ParseMode.HTML)
        return
    try:
        chat = await db.get_chat(m.chat.id)
        saved = _entries(chat.get("filters"))
        if normalized not in saved:
            await m.reply_text("That filter was not found.")
            return
        del saved[normalized]
        await db.update_chat(m.chat.id, filters=saved)
        await m.reply_text(
            f"Filter <code>{html.escape(keyword)}</code> removed.",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await m.reply_text("Could not remove that filter.")


@app.on_message(
    pyrogram_filters.group
    & pyrogram_filters.command(["stopall", "stopallfilters"], prefixes=PRE)
)
async def stop_all_filters(_: object, m: types.Message) -> None:
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    await m.reply_text(
        "Remove every automatic filter from this group? This cannot be undone.",
        reply_markup=ikm([[
            ikb("Confirm", callback_data="gh:filters:clear:yes",
                style=enums.ButtonStyle.SUCCESS),
            ikb("Cancel", callback_data="gh:filters:clear:no",
                style=enums.ButtonStyle.DANGER),
        ]]),
    )


@app.on_callback_query(pyrogram_filters.regex(r"^gh:filters:clear:(yes|no)$"))
async def stop_all_filters_callback(_: object, q: types.CallbackQuery) -> None:
    if not q.message or not q.from_user:
        return
    if not await has_right(q.message.chat.id, q.from_user.id, "can_change_info"):
        await q.answer("You need the Change group info admin right.",
                       show_alert=True)
        return
    action = (
        q.matches[0].group(1)
        if getattr(q, "matches", None)
        else str(getattr(q, "data", "")).rsplit(":", 1)[-1]
    )
    if action == "no":
        await q.message.edit_text("Saved filters were kept.")
        await q.answer()
        return
    await db.update_chat(q.message.chat.id, filters={})
    await q.message.edit_text("All saved filters were removed.")
    await q.answer()


@app.on_message(
    pyrogram_filters.group
    & pyrogram_filters.command("filters", prefixes=PRE)
)
async def list_filters(_: object, m: types.Message) -> None:
    try:
        saved = _entries((await db.get_chat(m.chat.id)).get("filters"))
        if not saved:
            text = "No filters are saved."
        else:
            names = sorted(
                (
                    str(item.get("keyword", key))
                    if isinstance(item, dict) else str(key)
                    for key, item in saved.items()
                ),
                key=str.casefold,
            )
            text = "<b>Saved filters</b>\n\n" + "\n".join(
                f"• <code>{html.escape(name)}</code>" for name in names
            )
        await m.reply_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=close_kb())
    except Exception:
        await m.reply_text("Could not load the filters.")


@app.on_message(pyrogram_filters.group & ~pyrogram_filters.service, group=13)
async def filter_watcher(_: object, m: types.Message) -> None:
    user = m.from_user
    if not user or user.is_self or user.is_bot:
        return
    content = m.text or m.caption or ""
    if not content or content.lstrip().startswith(("/", "?", "!")):
        return
    try:
        cfg = await db.get_chat(m.chat.id)
        saved = _entries(cfg.get("filters"))
        for keyword, response in saved.items():
            if _matches(content, str(keyword)):
                raw = response.get("text", "") if isinstance(response, dict) else str(response)
                _, controls = note_controls(raw)
                if controls.get("admin") and not await is_admin(
                    m.chat.id, m.from_user.id
                ):
                    return
                if controls.get("private"):
                    me = await app.get_me()
                    name = str(keyword).replace(" ", "_")
                    await m.reply_text(
                        "This filter reply is read privately — tap below.",
                        reply_markup=ikm([[
                            ikb(
                                "Open filter",
                                url=f"https://t.me/{me.username}?start=filter_{m.chat.id}_{name}",
                                style=enums.ButtonStyle.PRIMARY,
                            )
                        ]]),
                    )
                    return
                await send_saved(m, response, m)
                return
    except Exception:
        return