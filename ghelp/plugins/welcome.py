"""Welcome and goodbye messages."""

import asyncio
import weakref
from types import SimpleNamespace

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.database import db
from ghelp.helpers import (
    fill, is_admin, require_admin, send_saved,
    media_of, unsupported_media_of, parse_buttons,
    rose_markdown_to_html,
)

DEFAULT_WELCOME = "Welcome {mention} to <b>{chat}</b>!"
DEFAULT_GOODBYE = "{fullname} left the group."
_greet_locks = weakref.WeakValueDictionary()


def _greet_lock(chat_id):
    lock = _greet_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _greet_locks[chat_id] = lock
    return lock


def _greeting_entry(message):
    """Explicit command text wins; replied media keeps its native type."""
    parts = str(message.text or message.caption or "").split(maxsplit=1)
    text = parts[1].strip() if len(parts) > 1 else ""
    replied = message.reply_to_message
    if replied:
        if unsupported_media_of(replied):
            raise ValueError("Reply to text, a photo, video, GIF, audio, voice, file or sticker.")
        kind, file_id = media_of(replied)
        body = replied.text or replied.caption
        body = (getattr(body, "html", None) or str(body)) if body is not None else ""
        text = text or body
        if kind:
            if not file_id:
                raise ValueError("That media cannot be reused. Send it again, then reply.")
            return {"type": kind, "file_id": file_id, "text": text}
    if not text:
        raise ValueError("Add text, or reply to a message or media.")
    return text

def _safe_context(chat, user: types.User) -> tuple[SimpleNamespace, SimpleNamespace]:
    """Supply the member context; fill() escapes profile fields exactly once."""
    safe_user = SimpleNamespace(
        id=user.id,
        first_name=user.first_name or "",
        last_name=user.last_name or "",
        username=user.username or "",
    )
    safe_chat = SimpleNamespace(title=chat.title or "")
    return SimpleNamespace(chat=safe_chat, from_user=safe_user), safe_user


def _render(text: str, chat, user: types.User) -> tuple[str, types.InlineKeyboardMarkup | None]:
    context, safe_user = _safe_context(chat, user)
    # Keep this legacy helper in lock-step with send_saved.  In particular,
    # buttonurl :same and all supported buttonurl schemes must behave the same
    # in a preview and in a live greeting.
    body, markup = parse_buttons(fill(text, context, safe_user))
    return rose_markdown_to_html(body), markup


async def greet(chat_id: int, user: types.User) -> None:
    async with _greet_lock(chat_id):
        await _greet(chat_id, user)


async def _greet(chat_id: int, user: types.User) -> None:
    """Send a configured welcome, and optionally remove the previous one."""
    try:
        settings = await db.get_chat(chat_id)
        if not settings.get("welcome_on", False):
            return
        chat = await app.get_chat(chat_id)
        configured = settings.get("welcome_text") or DEFAULT_WELCOME
        context, _ = _safe_context(chat, user)
        current = await db.get_chat(chat_id)
        if not current.get("welcome_on") or current.get("welcome_text") != settings.get("welcome_text"):
            return
        delivered = []
        sent = await send_saved(
            app, configured, context, chat_id=chat_id, sent_messages=delivered,
        )
        ids = [item.id for item in delivered if getattr(item, "id", None)]
        previous = list(settings.get("last_welcome_ids") or [])
        if not previous and settings.get("last_welcome_id"):
            previous = [settings["last_welcome_id"]]
        if sent is None:
            # Keep the old welcome when replacement is incomplete. Remember
            # known partial sends so the next successful cleanup includes them.
            if ids:
                await db.update_chat(chat_id, last_welcome_ids=list(dict.fromkeys(previous + ids)))
            return
        current = await db.get_chat(chat_id)
        remaining = previous if settings.get("clean_welcome") else []
        if current.get("clean_welcome") and current.get("welcome_on") and previous:
            try:
                result = await app.delete_messages(chat_id, previous)
                if type(result) is int and result >= len(previous):
                    remaining = []
            except Exception:
                pass
        if ids:
            await db.update_chat(
                chat_id, last_welcome_id=ids[-1],
                last_welcome_ids=list(dict.fromkeys(remaining + ids)),
            )
    except Exception:
        # A welcome must never break member processing.
        return


@app.on_message(filters.command("welcome", prefixes=["/", "?", "!", "."]) & filters.group)
async def welcome_command(_, m: types.Message) -> None:
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    try:
        arg = m.command[1].lower() if len(m.command) > 1 else ""
        if arg in ("on", "off"):
            enabled = arg == "on"
            await db.update_chat(m.chat.id, welcome_on=enabled)
            await m.reply_text(
                f"Welcome messages are now <b>{'enabled' if enabled else 'disabled'}</b>.",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        settings = await db.get_chat(m.chat.id)
        if arg == "preview":
            context, _ = _safe_context(m.chat, m.from_user)
            await send_saved(m, settings.get("welcome_text") or DEFAULT_WELCOME, context)
            return
        state = "enabled" if settings.get("welcome_on") else "disabled"
        await m.reply_text(
            f"Welcome messages are <b>{state}</b>.\n"
            "<code>/welcome on</code> or <code>/welcome off</code>\n"
            "<code>/setwelcome &lt;text&gt;</code> or reply to media.\n"
            "<code>/welcome preview</code>\n"
            "<code>/resetwelcome</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await m.reply_text("Could not update welcome settings.", parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command("setwelcome", prefixes=["/", "?", "!", "."]) & filters.group)
async def setwelcome_command(_, m: types.Message) -> None:
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    try:
        entry = _greeting_entry(m)
        if not await require_admin(m, "can_change_info", "Change group info"):
            return
        await db.update_chat(m.chat.id, welcome_text=entry)
        await m.reply_text("Welcome message saved.", parse_mode=enums.ParseMode.HTML)
    except ValueError as exc:
        await m.reply_text(str(exc), parse_mode=enums.ParseMode.DISABLED)
    except Exception:
        await m.reply_text("Could not save the welcome message.", parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command("resetwelcome", prefixes=["/", "?", "!", "."]) & filters.group)
async def resetwelcome_command(_, m: types.Message) -> None:
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    try:
        await db.update_chat(m.chat.id, welcome_text=DEFAULT_WELCOME)
        await m.reply_text("Welcome message reset.", parse_mode=enums.ParseMode.HTML)
    except Exception:
        await m.reply_text("Could not reset the welcome message.", parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command("goodbye", prefixes=["/", "?", "!", "."]) & filters.group)
async def goodbye_command(_, m: types.Message) -> None:
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg == "preview":
        settings = await db.get_chat(m.chat.id)
        context, _ = _safe_context(m.chat, m.from_user)
        await send_saved(m, settings.get("goodbye_text") or DEFAULT_GOODBYE, context)
        return
    if arg not in ("on", "off"):
        await m.reply_text(
            "Use <code>/goodbye on|off</code> or <code>/goodbye preview</code>.",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    try:
        enabled = arg == "on"
        await db.update_chat(m.chat.id, goodbye_on=enabled)
        await m.reply_text(
            f"Goodbye messages are now <b>{'enabled' if enabled else 'disabled'}</b>.",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await m.reply_text("Could not update goodbye settings.", parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command("setgoodbye", prefixes=["/", "?", "!", "."]) & filters.group)
async def setgoodbye_command(_, m: types.Message) -> None:
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    try:
        entry = _greeting_entry(m)
        if not await require_admin(m, "can_change_info", "Change group info"):
            return
        await db.update_chat(m.chat.id, goodbye_text=entry)
        await m.reply_text("Goodbye message saved.", parse_mode=enums.ParseMode.HTML)
    except ValueError as exc:
        await m.reply_text(str(exc), parse_mode=enums.ParseMode.DISABLED)
    except Exception:
        await m.reply_text("Could not save the goodbye message.", parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command("cleanwelcome", prefixes=["/", "?", "!", "."]) & filters.group)
async def cleanwelcome_command(_, m: types.Message) -> None:
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg not in ("on", "off"):
        await m.reply_text(
            "Usage: <code>/cleanwelcome on</code> or <code>/cleanwelcome off</code>",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    try:
        enabled = arg == "on"
        await db.update_chat(m.chat.id, clean_welcome=enabled)
        await m.reply_text(
            f"Clean welcome is now <b>{'enabled' if enabled else 'disabled'}</b>.",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await m.reply_text("Could not update clean welcome.", parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.new_chat_members & filters.group, group=2)
async def welcome_new_members(_, m: types.Message) -> None:
    try:
        settings = await db.get_chat(m.chat.id)
        if settings.get("captcha_on"):
            return
        me = await app.get_me()
        for user in m.new_chat_members or []:
            if user.is_bot or user.id == me.id or await is_admin(m.chat.id, user.id):
                continue
            await greet(m.chat.id, user)
    except Exception:
        return


@app.on_message(filters.left_chat_member & filters.group, group=2)
async def goodbye_member(_, m: types.Message) -> None:
    user = m.left_chat_member
    if not user:
        return
    try:
        me = await app.get_me()
        if user.id == me.id or user.is_bot or await is_admin(m.chat.id, user.id):
            return
        settings = await db.get_chat(m.chat.id)
        if not settings.get("goodbye_on"):
            return
        context, _ = _safe_context(m.chat, user)
        await send_saved(m, settings.get("goodbye_text") or DEFAULT_GOODBYE, context)
    except Exception:
        return