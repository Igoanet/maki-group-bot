"""Scheduled night-time group closing and reopening."""

import asyncio
import re
from datetime import datetime, time as clock_time
from zoneinfo import ZoneInfo

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.database import db
from ghelp.helpers import require_admin


_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_ZONE = ZoneInfo("Asia/Kolkata")


def _open_permissions() -> types.ChatPermissions:
    """A fully open member permission set.

    ``can_send_media_messages`` is the LEGACY aggregate field: modern clients
    ignore it and enforce the granular flags instead.  Restoring only the
    legacy field (as this used to) reopened the group with photos, videos,
    documents, audio and video notes still blocked — the group looked closed
    for media even though "night mode" had ended (2026-09-19).
    """
    return types.ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )


def _inside_window(now: clock_time, start: clock_time, end: clock_time) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= now < end
    return now >= start or now < end


def _parse_time(value: str) -> clock_time:
    return datetime.strptime(value, "%H:%M").time()


@app.on_message(filters.group & filters.command("nightmode", prefixes=["/", "?", "!", "."]))
async def nightmode_command(_, m: types.Message) -> None:
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    try:
        if len(m.command) != 2 or m.command[1].lower() not in ("on", "off"):
            await m.reply_text(
                "<b>Usage:</b> <code>/nightmode on|off</code>",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        enabled = m.command[1].lower() == "on"
        settings = await db.get_chat(m.chat.id)
        if not enabled and settings.get("night_closed", False):
            await app.set_chat_permissions(m.chat.id, _open_permissions())
            await db.update_chat(m.chat.id, night_on=False, night_closed=False)
        else:
            await db.update_chat(m.chat.id, night_on=enabled)
        await m.reply_text(
            f"<b>Night mode {'enabled' if enabled else 'disabled'}.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await m.reply_text(
            "<b>Error: could not update night mode.</b>",
            parse_mode=enums.ParseMode.HTML,
        )


@app.on_message(filters.group & filters.command("setnighttime", prefixes=["/", "?", "!", "."]))
async def setnighttime_command(_, m: types.Message) -> None:
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    try:
        if (
            len(m.command) != 3
            or not _TIME_RE.fullmatch(m.command[1])
            or not _TIME_RE.fullmatch(m.command[2])
        ):
            await m.reply_text(
                "<b>Usage:</b> <code>/setnighttime HH:MM HH:MM</code>",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        start, end = m.command[1], m.command[2]
        await db.update_chat(m.chat.id, night_start=start, night_end=end)
        await m.reply_text(
            f"<b>Night hours set:</b> <code>{start}</code>–<code>{end}</code> "
            "(Asia/Kolkata).",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await m.reply_text(
            "<b>Error: could not set night hours.</b>", parse_mode=enums.ParseMode.HTML
        )


async def on_start(app) -> None:
    """Continuously apply scheduled permissions to configured groups."""
    while True:
        try:
            chats = await db.all_chats()
            now = datetime.now(_ZONE).time().replace(second=0, microsecond=0)
            for raw_chat_id, settings in chats.items():
                if not settings.get("night_on", False):
                    continue
                try:
                    chat_id = int(raw_chat_id)
                    start_text = settings.get("night_start", "23:00")
                    end_text = settings.get("night_end", "06:00")
                    if not _TIME_RE.fullmatch(start_text) or not _TIME_RE.fullmatch(end_text):
                        continue
                    closed_now = _inside_window(
                        now, _parse_time(start_text), _parse_time(end_text)
                    )
                    was_closed = bool(settings.get("night_closed", False))
                    if closed_now and not was_closed:
                        await app.set_chat_permissions(chat_id, types.ChatPermissions())
                        await app.send_message(
                            chat_id,
                            f"<b>Night mode:</b> the group is closed until "
                            f"<code>{end_text}</code>.",
                            parse_mode=enums.ParseMode.HTML,
                        )
                        await db.update_chat(chat_id, night_closed=True)
                    elif not closed_now and was_closed:
                        await app.set_chat_permissions(chat_id, _open_permissions())
                        await app.send_message(
                            chat_id,
                            "<b>Good morning!</b> The group is open again.",
                            parse_mode=enums.ParseMode.HTML,
                        )
                        await db.update_chat(chat_id, night_closed=False)
                except Exception:
                    continue
        except Exception:
            pass
        await asyncio.sleep(30)