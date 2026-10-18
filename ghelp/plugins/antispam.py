"""Flood protection and automatic anti-raid lockdown."""

import asyncio
import html
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.approval_trust import is_exempt
from ghelp.config import config
from ghelp.database import db
from ghelp.helpers import is_admin, mention, require_admin, safe_restrict


_flood: dict[tuple[int, int], deque[float]] = defaultdict(deque)
_flood_notice: dict[int, float] = {}
_joins: dict[int, deque[float]] = defaultdict(deque)
_tasks: set[asyncio.Task] = set()


def _remember_task(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _closed_permissions() -> types.ChatPermissions:
    return types.ChatPermissions()


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


@app.on_message(filters.group & filters.command("antiflood", prefixes=["/", "?", "!", "."]))
async def antiflood_command(_, m: types.Message) -> None:
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    try:
        if len(m.command) != 2 or m.command[1].lower() not in ("on", "off"):
            await m.reply_text(
                "Usage: <code>/antiflood on|off</code>",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        enabled = m.command[1].lower() == "on"
        await db.update_chat(m.chat.id, flood_on=enabled)
        await m.reply_text(
            f"<b>Anti-flood {'enabled' if enabled else 'disabled'}.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await m.reply_text(
            "<b>Could not update anti-flood.</b>", parse_mode=enums.ParseMode.HTML
        )


@app.on_message(filters.group & filters.command("setflood", prefixes=["/", "?", "!", "."]))
async def setflood_command(_, m: types.Message) -> None:
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    try:
        if len(m.command) != 2:
            raise ValueError
        if m.command[1].lower() in ("off", "no", "0"):
            await db.update_chat(m.chat.id, flood_on=False, flood_timer_count=0)
            await m.reply_text(
                "<b>Anti-flood disabled.</b>", parse_mode=enums.ParseMode.HTML,
            )
            return
        limit = int(m.command[1])
        if not 2 <= limit <= 50:
            raise ValueError
        await db.update_chat(m.chat.id, flood_on=True, flood_limit=limit,
                             flood_timer_count=0)
        await m.reply_text(
            f"<b>Flood limit set to {limit} messages per 10 seconds.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    except ValueError:
        await m.reply_text(
            "Usage: <code>/setflood &lt;2-50&gt;</code> or "
            "<code>/setflood off</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await m.reply_text(
            "<b>Could not set the flood limit.</b>",
            parse_mode=enums.ParseMode.HTML,
        )


@app.on_message(filters.group & ~filters.service, group=4)
async def flood_watcher(_, m: types.Message) -> None:
    try:
        user = m.from_user
        if not user or user.is_bot:
            return
        settings = await db.get_chat(m.chat.id)
        if not settings.get("flood_on", False):
            return
        limit = int(settings.get("flood_limit", config.DEFAULT_FLOOD_LIMIT))
        window = int(settings.get("flood_window", 10))
        timed_count = int(settings.get("flood_timer_count", 0))
        if timed_count > 0:
            limit = timed_count
            window = int(settings.get("flood_timer_secs", window))
        action = str(settings.get("flood_action", "mute"))
        act_secs = int(settings.get("flood_act_secs", 600))
        now = time.monotonic()
        messages = _flood[(m.chat.id, user.id)]
        messages.append(now)
        while messages and now - messages[0] > window:
            messages.popleft()
        if len(messages) <= limit:
            return
        messages.clear()
        if await is_admin(m.chat.id, user.id):
            return
        # The live role lookup above can yield while an admin approves this
        # member.  Reload the chat state at the destructive boundary so the
        # newest per-chat exemption wins.
        current = await db.get_chat(m.chat.id)
        if is_exempt(current, user.id, "anti_flood"):
            return
        # Rose actions include permanent and timed moderation.
        duration = max(1, act_secs)
        if action in ("ban", "tban"):
            try:
                kwargs = {}
                if action == "tban":
                    kwargs["until_date"] = datetime.now(timezone.utc) + timedelta(seconds=duration)
                await app.ban_chat_member(m.chat.id, user.id, **kwargs)
                acted = "banned" if action == "ban" else f"temporarily banned for {duration // 60} minutes"
            except Exception:
                return
        elif action == "kick":
            try:
                await app.ban_chat_member(m.chat.id, user.id)
                await app.unban_chat_member(m.chat.id, user.id)
                acted = "kicked"
            except Exception:
                return
        elif action in ("mute", "tmute"):
            if not await safe_restrict(m.chat.id, user.id, _closed_permissions()):
                return
            _remember_task(_unrestrict_later(m.chat.id, user.id, duration))
            acted = f"muted for {max(1, duration // 60)} minutes"
        else:
            return
        if settings.get("flood_clear"):
            try:
                await m.delete()
            except Exception:
                pass
        if now - _flood_notice.get(m.chat.id, 0) >= 60:
            _flood_notice[m.chat.id] = now
            name = html.escape(user.first_name or "user")
            await m.reply_text(
                f"{mention(user.id, name)} was {acted} for flooding.",
                parse_mode=enums.ParseMode.HTML,
            )
    except Exception:
        return


async def _unrestrict_later(chat_id: int, user_id: int, delay: int) -> None:
    try:
        await asyncio.sleep(delay)
        if await is_admin(chat_id, user_id):
            return
        await safe_restrict(chat_id, user_id, _open_permissions())
    except asyncio.CancelledError:
        raise
    except Exception:
        return


async def _clear_raid_later(chat_id: int, delay: int = 300) -> None:
    try:
        await asyncio.sleep(delay)
        await db.update_chat(chat_id, raid_lock=False)
    except asyncio.CancelledError:
        raise
    except Exception:
        return


@app.on_message(filters.group & filters.new_chat_members, group=3)
async def raid_watcher(_, m: types.Message) -> None:
    try:
        me = await app.get_me()
        members = [member for member in (m.new_chat_members or []) if member.id != me.id]
        if not members:
            return

        settings = await db.get_chat(m.chat.id)
        window = int(settings.get("raid_window", 10))
        now = time.monotonic()
        joins = _joins[m.chat.id]
        joins.extend([now] * len(members))
        while joins and now - joins[0] > window:
            joins.popleft()

        locked = bool(settings.get("raid_lock", False))
        threshold = int(settings.get("raid_threshold", config.RAID_JOIN_LIMIT))
        if settings.get("raid_on", True) and threshold > 0 and len(joins) >= threshold and not locked:
            await db.update_chat(m.chat.id, raid_lock=True)
            locked = True
            _remember_task(_clear_raid_later(m.chat.id, int(settings.get("raid_duration", 21600))))
            await m.reply_text(
                "<b>Raid detected</b> — new members will be auto-muted for 5 minutes.",
                parse_mode=enums.ParseMode.HTML,
            )

        if locked:
            for member in members:
                if await is_admin(m.chat.id, member.id):
                    continue
                if await _temp_ban(m.chat.id, member.id, int(settings.get("raid_act_secs", 600))):
                    _remember_task(
                        _unrestrict_later(
                            m.chat.id, member.id,
                            int(settings.get("raid_act_secs", 600)),
                        )
                    )
    except Exception:
        return


async def _temp_ban(chat_id: int, user_id: int, seconds: int) -> bool:
    """Temporarily ban a raid joiner, rather than merely muting them."""
    try:
        result = await app.ban_chat_member(
            chat_id, user_id,
            until_date=datetime.now(timezone.utc) + timedelta(seconds=max(60, seconds)),
        )
        return result is not False
    except Exception:
        return False