"""Approval mode: new members stay muted until an admin explicitly unmutes.
Filter approval is separate and never changes Telegram permissions.

NOTE: mutually redundant with /captcha — use one or the other.
"""

import asyncio
import html
import logging
import time
import weakref

from pyrogram import enums, filters, types

from ghelp import app
from ghelp import approval_trust
from ghelp.config import config
from ghelp.database import db
from ghelp.helpers import bot_can, ikb, ikm, mention, require_admin, safe_restrict, is_owner, resolve_user
from ghelp import join_requests
from ghelp.approval_trust import (
    add_approved,
    exemption_description,
    feature_counts,
    is_approved,
)
from ghelp.moderation_reply import send_reply, user_label
from ghelp.plugins.admin import _member_permissions, _muted_permissions
from ghelp.plugins.logchan import log_event
from ghelp.selfdel import arm_delete, autodel_reply

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]
_action_locks = weakref.WeakValueDictionary()
_completed = {}
is_exempt = getattr(approval_trust, "is_exempt", None)


@autodel_reply
async def _reply(message, text, **kwargs):
    return await send_reply(message, text, client=app, **kwargs)


async def _answer(query, text, *, alert=False):
    try:
        await query.answer(text, show_alert=alert)
    except Exception:
        # Expired callback acknowledgements do not change an action's outcome.
        pass


async def _notify(query, text, *, reply_markup=None):
    try:
        result = await query.message.edit_text(
            text, parse_mode=enums.ParseMode.HTML, reply_markup=reply_markup
        )
        if result is False:
            raise RuntimeError("Telegram did not update the approval card")
        if reply_markup is None:
            arm_delete(result)
    except Exception:
        await _reply(query.message, text, quote=False, reply_markup=reply_markup)


def _is_feature_exempt(cfg, uid, feature: str) -> bool:
    """Use the worker's feature policy, with legacy approval compatibility."""
    if callable(is_exempt):
        try:
            return bool(is_exempt(cfg, uid, feature))
        except Exception:
            return False
    return is_approved(cfg, uid)


def _features_text(cfg, uid: int) -> str:
    enabled, available = feature_counts(cfg, uid)
    status = f"<b>{enabled}/{available} enabled</b>"
    if not is_approved(cfg, uid):
        status += " · <b>not approved</b>"
    return (
        f"<b>Approved features</b> · <code>{uid}</code>\n"
        f"{status}\n"
        "<i>Applies to approved members; filter exemptions only. Telegram "
        "permissions were not changed; protections remain in force.</i>"
    )


def _features_button(uid: int, chat_id: int):
    return ikb(
        "Approved features",
        callback_data=f"gh:ap:f:{uid}:{chat_id}",
        style=enums.ButtonStyle.PRIMARY,
    )


def _actions(uid, *, approve=True, unmute=True, show_features=False,
             chat_id=None):
    rows = []
    if approve:
        rows.append([ikb("Approve filters only", callback_data=f"gh:ap:a:{uid}", style=enums.ButtonStyle.SUCCESS)])
    if unmute:
        rows.append([ikb("Unmute user", callback_data=f"gh:ap:u:{uid}", style=enums.ButtonStyle.PRIMARY)])
    if show_features and chat_id is not None:
        rows.append([_features_button(uid, chat_id)])
    if not rows:
        return None
    rows.append([ikb("Ban", callback_data=f"gh:ap:b:{uid}", style=enums.ButtonStyle.DANGER)])
    return ikm(rows)


async def _can_act(chat_id, actor):
    if not actor or getattr(actor, "is_bot", False):
        return False
    member = await app.get_chat_member(chat_id, actor.id)
    return member.status == enums.ChatMemberStatus.OWNER or (
        member.status == enums.ChatMemberStatus.ADMINISTRATOR
        and bool(getattr(getattr(member, "privileges", None), "can_restrict_members", False))
    )


def _finish(key):
    now = time.monotonic()
    for old_key, until in list(_completed.items()):
        if until <= now:
            _completed.pop(old_key, None)
    if len(_completed) >= 1024:
        _completed.pop(next(iter(_completed)))
    _completed[key] = now + 86400


@app.on_message(filters.group & filters.command("approval", prefixes=PRE))
async def approval_cmd(_, message: types.Message):
    arg = message.command[1].lower() if len(message.command) > 1 else ""
    if not arg:
        user = await resolve_user(message) or message.from_user
        uid = getattr(user, "id", None)
        cfg = await db.get_chat(message.chat.id)
        if uid is None:
            await _reply(message, "Approval status is unavailable for anonymous senders.")
            return
        status = "approved" if _is_feature_exempt(cfg, uid, "approval_mode") else "not approved"
        await _reply(message, f"You are <b>{status}</b> in this chat.")
        return
    if not await require_admin(message, "can_restrict_members", "Ban users"):
        return
    if arg not in ("on", "off"):
        cfg = await db.get_chat(message.chat.id)
        state = "ON" if cfg["approval_on"] else "OFF"
        await _reply(message,
            f"Approval mode is <b>{state}</b>.\n"
            "New members stay muted until an admin chooses <b>Unmute user</b>.\n"
            "<b>Approve filters only</b> separately grants selected bot-filter exemptions "
            "to that member; it never changes group or member Telegram permissions. "
            "The Ban button removes the member instead.\n"
            "<b>Usage:</b> <code>/approval on|off</code> or "
            "<code>/approval</code> to check status.",
        )
        return
    # Turning approval mode on or off changes who may talk in the group, so
    # the caller needs the same personal right the approval buttons demand.
    try:
        allowed = await _can_act(message.chat.id, getattr(message, "from_user", None))
    except Exception:
        # fail closed, but say so rather than dying silently
        await _reply(
            message,
            "Could not verify your admin rights right now — try again.",
        )
        return
    if not allowed:
        await _reply(
            message,
            "You need the <b>Ban users (restrict members)</b> admin right "
            "to change approval mode.",
        )
        return
    if arg == "on" and not await bot_can(message.chat.id, "can_restrict_members"):
        await _reply(message, "I need the <b>Ban users / Restrict members</b> admin right for this.")
        return
    # captcha and approval are mutually exclusive — both mute joiners, and a
    # solved captcha must never bypass a pending admin approval
    note = ""
    if arg == "on":
        cfg = await db.get_chat(message.chat.id)
        if cfg.get("captcha_on"):
            await db.update_chat(message.chat.id, captcha_on=False)
            note = "\nCaptcha was turned <b>OFF</b> (use one, not both)."
    await db.update_chat(message.chat.id, approval_on=(arg == "on"))
    await _reply(message, f"Approval mode <b>{arg.upper()}</b>.{note}")


@app.on_message(filters.new_chat_members, group=5)
async def approval_gate(_, message: types.Message):
    try:
        cfg = await db.get_chat(message.chat.id)
        if not cfg["approval_on"]:
            return
        if not await bot_can(message.chat.id, "can_restrict_members"):
            logger.info("approval: no restrict right in %s, failing open", message.chat.id)
            return
        for u in message.new_chat_members:
            if u.is_bot or _is_feature_exempt(cfg, u.id, "approval_mode"):
                continue  # pre-approved members skip the mute entirely
            try:
                admitted_by_request = await join_requests.is_request_admission(message, u.id)
            except Exception:
                admitted_by_request = False
            if admitted_by_request:
                continue
            if _is_feature_exempt(
                await db.get_chat(message.chat.id), u.id, "approval_mode"
            ):
                continue
            ok = await safe_restrict(message.chat.id, u.id, _muted_permissions())
            if not ok:
                continue
            uname = html.escape(u.first_name or "user")
            await app.send_message(
                message.chat.id,
                f"{mention(u.id, uname)} (<code>{u.id}</code>) joined and is muted.\n\n"
                "<b>Approve filters only:</b> exempt only this member from "
                "sticker/GIF/media locks, blacklist words and anti-flood checks. "
                "Telegram permissions and their existing mute stay unchanged.\n"
                "<b>Unmute user:</b> separately restore this member's chat access, "
                "without granting filter exemptions.\n"
                "<b>Ban:</b> remove this member from the group.\n"
                "These actions require the admin's Ban users permission.",
                parse_mode=enums.ParseMode.HTML,
                reply_markup=_actions(u.id),
            )
    except Exception as exc:
        logger.info("approval gate: %s", exc)


@app.on_callback_query(filters.regex(r"^gh:ap:[aub]:\d+$"))
async def approval_cb(_, q: types.CallbackQuery):
    if (
        not q.from_user or not q.message
        or q.message.chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)
    ):
        return await _answer(q, "Use this action inside its group.", alert=True)
    rows = getattr(getattr(q.message, "reply_markup", None), "inline_keyboard", [])
    if not any(getattr(button, "callback_data", None) == q.data for row in rows for button in row):
        return await _answer(q, "This button is no longer available.", alert=True)
    _, _, act, uid_s = q.data.split(":")
    uid, chat_id = int(uid_s), q.message.chat.id
    key = (chat_id, q.message.id)
    lock = _action_locks.get(key)
    if lock is None:
        lock = _action_locks[key] = asyncio.Lock()
    async with lock:
        if max(_completed.get(key, 0), _completed.get((*key, act, uid), 0)) > time.monotonic():
            return await _answer(q, "This member action has already been completed.", alert=True)
        await _approval_action(q, act, uid, chat_id, key)


async def _approval_action(q, act, uid, chat_id, key):
    try:
        if not await _can_act(chat_id, q.from_user):
            return await _answer(q, "An admin with Ban users permission must do this.", alert=True)
        if act != "a" and not await bot_can(chat_id, "can_restrict_members"):
            return await _answer(q, "Maki needs the Ban users / Restrict members admin right.", alert=True)
        member = await app.get_chat_member(chat_id, uid)
        target = member.user
        me = await app.get_me()
        if uid in (me.id, config.OWNER_ID) or member.status in (
            enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR
        ):
            return await _answer(q, "I cannot take this action on an owner, admin or myself.", alert=True)
        if act in ("a", "u") and member.status in (
            enums.ChatMemberStatus.BANNED, enums.ChatMemberStatus.LEFT
        ):
            return await _answer(q, "This user is not a current member. Approval does not unban or join them.", alert=True)
        if not await _can_act(chat_id, q.from_user):
            return await _answer(q, "Your Ban users permission changed. No action was taken.", alert=True)
    except Exception as exc:
        logger.warning("Approval action preflight failed: %s", type(exc).__name__)
        return await _answer(q, "Could not verify current membership and permissions. Try again.", alert=True)

    await _answer(q, "Processing…")
    keyboard = None
    target_label = user_label(target)
    actor_label = user_label(q.from_user)
    if act == "a":
        try:
            await add_approved(chat_id, uid, database=db)
        except Exception as exc:
            logger.warning("Approval list save failed: %s", type(exc).__name__)
            return await _reply(q.message, f"The trusted exemption for {target_label} could not be saved. No Telegram permissions were changed. An admin can retry with <code>/approve</code>.", quote=False)
        feature_cfg = await db.get_chat(chat_id)
        verb = "Approved filters only for"
        result = (
            f"<b>{verb}</b> {target_label}.\n<b>By:</b> {actor_label}\n"
            f"{_features_text(feature_cfg, uid)}"
        )
        if member.status == enums.ChatMemberStatus.RESTRICTED:
            result += "\n\nThis user is still muted. Use <b>Unmute user</b> or <code>/unmute</code> separately."
        keyboard = _actions(
            uid, approve=False, unmute=(
                member.status == enums.ChatMemberStatus.RESTRICTED
            ), show_features=True, chat_id=chat_id
        )
    elif act == "u":
        if member.status == enums.ChatMemberStatus.RESTRICTED:
            try:
                ok = await safe_restrict(chat_id, uid, _member_permissions())
            except Exception:
                ok = False
            if not ok:
                return await _reply(q.message, f"Could not unmute {target_label}. No filter exemptions were changed. Check Maki's Ban users permission.", quote=False)
            verb = "Unmuted"
        else:
            verb = "Already unmuted"
        result = (
            f"<b>{verb}</b> {target_label}.\n<b>By:</b> {actor_label}\n"
            "This action grants no filter exemptions and does not change group-wide permissions."
        )
        keyboard = _actions(
            uid, approve=_completed.get((*key, "a", uid), 0) <= time.monotonic(), unmute=False
        )
    else:
        if member.status == enums.ChatMemberStatus.BANNED:
            verb = "Already banned"
        else:
            try:
                if await app.ban_chat_member(chat_id, uid) is False:
                    raise RuntimeError("Telegram rejected ban")
            except Exception as exc:
                logger.warning("Approval ban failed: %s", type(exc).__name__)
                return await _reply(q.message, f"Could not confirm the ban for {target_label}. Check Maki's Ban users permission and the member's current status.", quote=False)
            verb = "Banned"
        result = f"<b>{verb}</b> {target_label}.\n<b>By:</b> {actor_label}"
    _finish(key if act == "b" else (*key, act, uid))
    await _notify(q, result, reply_markup=keyboard)
    try:
        await log_event(chat_id, f"{verb}: {target_label}\nBy: {actor_label}")
    except Exception as exc:
        logger.info("Approval action succeeded; log delivery failed: %s", type(exc).__name__)
