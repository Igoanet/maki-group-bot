"""Button-based human verification for new members."""

import asyncio
import html
import random
import re

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.database import db
from ghelp.helpers import ikb, ikm, is_admin, mention, require_admin, safe_restrict
from ghelp import join_requests
from ghelp.plugins import welcome

_pending: dict[tuple[int, int], dict] = {}

_MUTED = types.ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

_FULL = types.ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_send_polls=True,
    can_invite_users=True,
)


def _question(chat_id: int, user: types.User, mode: str = "button",
              custom: str = "I'm human"):
    left, right = random.randint(1, 9), random.randint(1, 9)
    answer = left + right
    wrong_pool = [number for number in range(2, 19) if number != answer]
    choices = random.sample(wrong_pool, 3) + [answer]
    random.shuffle(choices)
    text = (
        f"Human check — {mention(user.id, html.escape(user.first_name or 'user'))}, "
        f"what is <b>{left} + {right}</b>?"
    )
    if mode in ("text", "text2"):
        return str(answer), text + "\nReply with the answer.", None
    if mode == "button":
        text = (f"Human check — {mention(user.id, html.escape(user.first_name or 'user'))}, "
                f"tap <b>{html.escape(custom)}</b> to confirm.")
        return custom.lower(), text, ikm([[ikb(custom, callback_data=f"gh:cap:{chat_id}:{user.id}:ok",
                                               style=enums.ButtonStyle.SUCCESS)]])
    buttons = [
        ikb(
            str(choice),
            callback_data=f"gh:cap:{chat_id}:{user.id}:{choice}",
            style=(
                enums.ButtonStyle.SUCCESS
                if choice == answer
                else enums.ButtonStyle.DANGER
            ),
        )
        for choice in choices
    ]
    return str(answer), text, ikm([buttons])


async def _timeout(chat_id: int, user_id: int, minutes: int) -> None:
    try:
        await asyncio.sleep(minutes * 60)
        entry = _pending.pop((chat_id, user_id), None)
        if not entry:
            return
        if await is_admin(chat_id, user_id):
            try:
                await app.delete_messages(chat_id, entry["msg_id"])
            except Exception:
                pass
            return
        cfg = await db.get_chat(chat_id)
        if cfg.get("captcha_kick"):
            try:
                await app.ban_chat_member(chat_id, user_id)
                await app.unban_chat_member(chat_id, user_id)
            except Exception:
                pass
        else:
            await safe_restrict(chat_id, user_id, _FULL)
        try:
            await app.delete_messages(chat_id, entry["msg_id"])
        except Exception:
            pass
        try:
            notice = await app.send_message(
                chat_id,
                "A member was removed for not completing verification.",
                parse_mode=enums.ParseMode.HTML,
            )
            await asyncio.sleep(30)
            await notice.delete()
        except Exception:
            pass
    except asyncio.CancelledError:
        return


async def _cancel_pending(chat_id: int, user_id: int) -> dict | None:
    entry = _pending.pop((chat_id, user_id), None)
    if entry:
        task = entry.get("task")
        if task and task is not asyncio.current_task():
            task.cancel()
    return entry


async def _can_disable_approval(chat_id: int, actor) -> bool:
    """Live owner / restrict-right check, with no configured-owner bypass.

    Enabling CAPTCHA turns approval mode off, so it must not become a side
    door around the authorization /approval and /approve demand.
    """
    if not actor or getattr(actor, "is_bot", False):
        return False
    try:
        member = await app.get_chat_member(chat_id, actor.id)
    except Exception:
        return False
    if member.status == enums.ChatMemberStatus.OWNER:
        return True
    return member.status == enums.ChatMemberStatus.ADMINISTRATOR and bool(
        getattr(getattr(member, "privileges", None), "can_restrict_members", False)
    )


@app.on_message(filters.command("captcha", prefixes=["/", "?", "!", "."]) & filters.group)
async def captcha_command(_, m: types.Message) -> None:
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg not in ("on", "off"):
        await m.reply_text(
            "Usage: <code>/captcha on</code> or <code>/captcha off</code>",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    try:
        enabled = arg == "on"
        # captcha and approval are mutually exclusive — both mute joiners,
        # and a solved captcha must never bypass a pending admin approval
        cfg = await db.get_chat(m.chat.id)
        note = ""
        if enabled and not cfg.get("welcome_on"):
            await m.reply_text("CAPTCHA requires welcome messages to be enabled first.")
            return
        if enabled and cfg.get("approval_on"):
            if not await _can_disable_approval(
                m.chat.id, getattr(m, "from_user", None)
            ):
                await m.reply_text(
                    "Approval mode is ON here. You need the <b>Ban users "
                    "(restrict members)</b> admin right to switch it off "
                    "for CAPTCHA.",
                    parse_mode=enums.ParseMode.HTML,
                )
                return
            await db.update_chat(m.chat.id, approval_on=False)
            note = "\nApproval mode was turned <b>OFF</b> (use one, not both)."
        await db.update_chat(m.chat.id, captcha_on=enabled)
        await m.reply_text(
            f"Captcha is now <b>{'enabled' if enabled else 'disabled'}</b>.{note}",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await m.reply_text("Could not update captcha settings.", parse_mode=enums.ParseMode.HTML)


def _yesno(value: str) -> bool | None:
    value = value.lower()
    return True if value in ("yes", "on") else False if value in ("no", "off") else None


def _duration(value: str) -> int | None:
    match = re.fullmatch(r"(\d+)([wdhm])", value.lower())
    if not match:
        return 0 if value.lower() == "off" else None
    return int(match.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]


@app.on_message(filters.command("captchamode", prefixes=["/", "?", "!", "."]) & filters.group)
async def captcha_mode_command(_, m):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    mode = m.command[1].lower() if len(m.command) > 1 else ""
    if mode not in {"button", "math", "text", "text2"}:
        await m.reply_text("Usage: <code>/captchamode button|math|text|text2</code>",
                           parse_mode=enums.ParseMode.HTML)
        return
    await db.update_chat(m.chat.id, captcha_mode=mode)
    await m.reply_text(f"CAPTCHA mode set to <b>{mode}</b>.", parse_mode=enums.ParseMode.HTML)


async def _set_bool_command(m, key, label):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    value = _yesno(m.command[1]) if len(m.command) > 1 else None
    if value is None:
        await m.reply_text(f"Usage: <code>/{label} on|off</code>", parse_mode=enums.ParseMode.HTML)
        return
    await db.update_chat(m.chat.id, **{key: value})
    await m.reply_text(f"{label} is now <b>{'enabled' if value else 'disabled'}</b>.",
                       parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command("captcharules", prefixes=["/", "?", "!", "."]) & filters.group)
async def captcha_rules_command(_, m): await _set_bool_command(m, "captcha_rules", "captcharules")


@app.on_message(filters.command("captchakick", prefixes=["/", "?", "!", "."]) & filters.group)
async def captcha_kick_command(_, m): await _set_bool_command(m, "captcha_kick", "captchakick")


@app.on_message(filters.command("captchamutetime", prefixes=["/", "?", "!", "."]) & filters.group)
async def captcha_mute_time_command(_, m):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    seconds = _duration(m.command[1]) if len(m.command) > 1 else None
    if seconds is None:
        await m.reply_text("Use a duration such as <code>2h</code>, <code>1d</code>, or <code>off</code>.",
                           parse_mode=enums.ParseMode.HTML)
        return
    await db.update_chat(m.chat.id, captcha_mute_secs=seconds,
                         captcha_mute_min=max(1, seconds // 60) if seconds else 0)
    await m.reply_text("CAPTCHA mute timeout updated.", parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command("captchakicktime", prefixes=["/", "?", "!", "."]) & filters.group)
async def captcha_kick_time_command(_, m):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    seconds = _duration(m.command[1]) if len(m.command) > 1 else None
    if seconds is None:
        await m.reply_text("Use a duration such as <code>1h</code> or <code>off</code>.",
                           parse_mode=enums.ParseMode.HTML)
        return
    await db.update_chat(m.chat.id, captcha_kick_time=seconds)
    await m.reply_text("CAPTCHA kick timeout updated.", parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command("setcaptchatext", prefixes=["/", "?", "!", "."]) & filters.group)
async def set_captcha_text(_, m):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    text = m.text.split(maxsplit=1)[1].strip() if m.text and len(m.text.split(maxsplit=1)) > 1 else ""
    if not text:
        await m.reply_text("Usage: <code>/setcaptchatext &lt;button text&gt;</code>", parse_mode=enums.ParseMode.HTML)
        return
    await db.update_chat(m.chat.id, captcha_text=text[:64])
    await m.reply_text("CAPTCHA button text updated.")


@app.on_message(filters.command("resetcaptchatext", prefixes=["/", "?", "!", "."]) & filters.group)
async def reset_captcha_text(_, m):
    if await require_admin(m, "can_restrict_members", "Ban users"):
        await db.update_chat(m.chat.id, captcha_text="I'm human")
        await m.reply_text("CAPTCHA button text reset.")


@app.on_message(filters.command("setcaptchatime", prefixes=["/", "?", "!", "."]) & filters.group)
async def set_captcha_time_command(_, m: types.Message) -> None:
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    try:
        minutes = int(m.command[1])
        if not 1 <= minutes <= 60:
            raise ValueError
    except (IndexError, TypeError, ValueError):
        await m.reply_text(
            "Choose a timeout from <b>1</b> to <b>60</b> minutes.",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    try:
        await db.update_chat(m.chat.id, captcha_mute_min=minutes)
        await m.reply_text(
            f"Captcha timeout set to <b>{minutes} minutes</b>.",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await m.reply_text("Could not update the captcha timeout.", parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.new_chat_members & filters.group, group=1)
async def captcha_new_members(_, m: types.Message) -> None:
    try:
        settings = await db.get_chat(m.chat.id)
        if not settings.get("captcha_on") or not settings.get("welcome_on"):
            return
        me = await app.get_me()
        for user in m.new_chat_members or []:
            if user.is_bot or user.id == me.id or await is_admin(m.chat.id, user.id):
                continue
            try:
                admitted_by_request = await join_requests.is_request_admission(m, user.id)
            except Exception:
                admitted_by_request = False
            if admitted_by_request:
                continue
            await _cancel_pending(m.chat.id, user.id)
            if not await safe_restrict(m.chat.id, user.id, _MUTED):
                await m.reply_text(
                    "I could not restrict a new member for verification.",
                    parse_mode=enums.ParseMode.HTML,
                )
                continue
            answer, text, markup = _question(m.chat.id, user, settings.get("captcha_mode", "button"),
                                              settings.get("captcha_text", "I'm human"))
            sent = await app.send_message(
                m.chat.id,
                text,
                reply_markup=markup,
                parse_mode=enums.ParseMode.HTML,
            )
            timeout = settings.get("captcha_kick_time") if settings.get("captcha_kick") else None
            timeout = timeout if timeout is not None else (
                settings.get("captcha_mute_secs") or settings.get("captcha_mute_min", 5) * 60)
            task = (asyncio.create_task(_timeout(m.chat.id, user.id, max(1, timeout / 60)))
                    if timeout else None)
            _pending[(m.chat.id, user.id)] = {
                "answer": answer,
                "msg_id": sent.id,
                "task": task,
                "user": user,
                "mode": settings.get("captcha_mode", "button"),
            }
    except Exception:
        return


@app.on_callback_query(filters.regex(r"^gh:cap:-?\d+:\d+:(?:\d+|ok)$"))
async def captcha_callback(_, query: types.CallbackQuery) -> None:
    try:
        _, _, raw_chat, raw_user, raw_answer = query.data.split(":")
        chat_id, user_id = int(raw_chat), int(raw_user)
        chosen = raw_answer
        if query.from_user.id != user_id:
            await query.answer("This check belongs to another member.", show_alert=True)
            return
        entry = _pending.get((chat_id, user_id))
        if not entry:
            await query.answer("This check has expired.", show_alert=True)
            return
        if chosen != str(entry["answer"]):
            await query.answer("Wrong, try again!", show_alert=True)
            answer, text, markup = _question(
                chat_id, query.from_user, entry.get("mode", "button"),
                (await db.get_chat(chat_id)).get("captcha_text", "I'm human"))
            entry["answer"] = answer
            await query.message.edit_text(
                text,
                reply_markup=markup,
                parse_mode=enums.ParseMode.HTML,
            )
            return
        if await is_admin(chat_id, user_id):
            await query.answer()
            await _cancel_pending(chat_id, user_id)
            try:
                await query.message.delete()
            except Exception:
                pass
            return
        # approval mode keeps the member muted even after a passed captcha —
        # only an admin's Approve button may restore permissions (covers
        # pre-existing dual-enabled configs too)
        cfg = await db.get_chat(chat_id)
        if cfg.get("approval_on"):
            await query.answer("Verified — waiting for admin approval.")
            await _cancel_pending(chat_id, user_id)
            try:
                await query.message.delete()
            except Exception:
                pass
            return
        if not await safe_restrict(chat_id, user_id, _FULL):
            await query.answer("I could not restore your permissions.", show_alert=True)
            return
        await query.answer("Verified!")
        await _cancel_pending(chat_id, user_id)
        try:
            await query.message.delete()
        except Exception:
            pass
        await welcome.greet(chat_id, query.from_user)
    except Exception:
        try:
            await query.answer("Verification failed. Please try again.", show_alert=True)
        except Exception:
            pass


@app.on_message(filters.group & filters.text, group=1)
async def captcha_text_answer(_, m: types.Message) -> None:
    """Accept answers for text CAPTCHAs without intercepting ordinary chat."""
    if not m.from_user:
        return
    entry = _pending.get((m.chat.id, m.from_user.id))
    if not entry or entry.get("mode") not in ("text", "text2"):
        return
    if (m.text or "").strip().casefold() != str(entry["answer"]).casefold():
        return
    cfg = await db.get_chat(m.chat.id)
    if cfg.get("approval_on"):
        await _cancel_pending(m.chat.id, m.from_user.id)
        return
    if await safe_restrict(m.chat.id, m.from_user.id, _FULL):
        await _cancel_pending(m.chat.id, m.from_user.id)
        try:
            await app.delete_messages(m.chat.id, entry["msg_id"])
        except Exception:
            pass


@app.on_message(filters.left_chat_member & filters.group, group=1)
async def captcha_member_left(_, m: types.Message) -> None:
    user = m.left_chat_member
    if not user:
        return
    try:
        me = await app.get_me()
        if user.id == me.id or user.is_bot or await is_admin(m.chat.id, user.id):
            return
        entry = await _cancel_pending(m.chat.id, user.id)
        if entry:
            try:
                await app.delete_messages(m.chat.id, entry["msg_id"])
            except Exception:
                pass
    except Exception:
        return