"""Group-admin controls and Telegram update routes for join requests."""

import html
import logging
import time
import uuid
from dataclasses import dataclass

from pyrogram import enums, filters
from pyrogram.errors import MessageNotModified

from ghelp import app
from ghelp import join_requests as service
from ghelp.helpers import concise_bot_text, ikb, ikm
from ghelp.selfdel import autodel_reply

log = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]
GROUPS = (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)
MODES = {"off": "Manual (OFF)", "verify": "Verify in DM", "instant": "Instant join (ON)"}


@dataclass
class BulkConsent:
    chat_id: int
    user_id: int
    message_id: int
    revision: int
    expires: float
    running: bool = False


_consents: dict[str, BulkConsent] = {}


@autodel_reply
async def _reply(message, text, **kwargs):
    return await message.reply_text(
        concise_bot_text(text), parse_mode=enums.ParseMode.HTML, **kwargs
    )


def _group(message) -> bool:
    return bool(message and getattr(message.chat, "type", None) in GROUPS)


def _actor(message) -> int:
    user = getattr(message, "from_user", None)
    if not user or user.is_bot or getattr(message, "sender_chat", None):
        raise service.RequestError("Use your personal admin account, not anonymous/channel mode.")
    return user.id


def _revoke(chat_id: int) -> None:
    now = time.monotonic()
    for key, item in list(_consents.items()):
        if item.chat_id == chat_id or item.expires <= now:
            _consents.pop(key, None)


def _panel(settings):
    mode = settings.get("join_request_mode", "off")
    label = MODES.get(mode, MODES["off"])
    text = (
        "<b>Join Requests</b>\n\n"
        f"Current mode: <b>{label}</b>\n\n"
        "<b>Verify in DM:</b> Maki sends a short arithmetic question. "
        "Only that requester can answer; a correct answer approves the request immediately.\n"
        "<b>Instant join (ON):</b> new requests are accepted without verification.\n"
        "<b>Manual (OFF):</b> admins handle requests themselves.\n\n"
        "These modes affect new requests. <b>Accept all pending</b> includes older requests "
        "already waiting on Telegram, without verification.\n"
        "Existing separate after-join rules still apply to bulk approvals.\n\n"
        "Admins need <b>Invite users</b> permission, and Maki needs it too. "
        "Use a join-request invite link; this setting does not convert ordinary invite links.\n"
        "If a verification DM cannot be delivered, the request stays pending. "
        "The requester can open Maki privately and send <code>/verifyrequests</code>."
    )
    rows = [
        [ikb(
            label, callback_data=f"gh:jr:mode:{value}",
            style=enums.ButtonStyle.SUCCESS if mode == value else enums.ButtonStyle.PRIMARY,
        )]
        for value, label in MODES.items()
    ]
    rows += [
        [ikb("Accept all pending", callback_data="gh:jr:all", style=enums.ButtonStyle.DANGER)],
        [ikb("Refresh", callback_data="gh:jr:open", style=enums.ButtonStyle.PRIMARY),
         ikb("Close", callback_data="gh:jr:close", style=enums.ButtonStyle.DANGER)],
    ]
    return text, ikm(rows)


async def _show(message, *, edit=False):
    text, markup = _panel(await service.settings(message.chat.id))
    if edit:
        await message.edit_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=markup)
    else:
        await _reply(message, text, reply_markup=markup)


async def _error(message, exc):
    if isinstance(exc, service.RequestError):
        text = html.escape(str(exc))
    else:
        log.warning("Join-request operation failed: %s", type(exc).__name__)
        text = "Could not complete that request. Check Maki's Invite users permission and try again."
    await _reply(message, text, quote=False)


@app.on_message(filters.group & filters.command("autorequest", prefixes=PRE))
async def autorequest(_, message):
    try:
        actor = _actor(message)
        await service.require_access(message.chat.id, actor, check_bot=False)
        args = message.command[1:]
        if args:
            if len(args) != 1 or args[0].lower() not in ("on", "off", "verify"):
                return await _reply(message, "Use <code>/autorequest on|verify|off</code>, or <code>/autorequest</code> for buttons.")
            _revoke(message.chat.id)
            mode = {"on": "instant", "off": "off", "verify": "verify"}[args[0].lower()]
            await service.set_mode(message.chat.id, actor, mode)
        await _show(message)
    except Exception as exc:
        await _error(message, exc)


@app.on_message(filters.group & filters.command(["requestall", "request"], prefixes=PRE))
async def requestall(_, message):
    try:
        actor = _actor(message)
        command = message.command
        if command[0].lower() == "request" and len(command) == 1:
            await service.require_access(message.chat.id, actor, check_bot=False)
            return await _show(message)
        valid = (
            command[0].lower() == "requestall" and len(command) == 1
        ) or (
            command[0].lower() == "request" and len(command) == 2 and command[1].lower() == "all"
        )
        if not valid:
            return await _reply(message, "Use <code>/requestall</code> or <code>/request all</code> to accept every pending request.")
        await service.approve_all(message.chat.id, actor, operation_id=f"message:{message.id}")
        _revoke(message.chat.id)
        await _reply(message, "All pending join requests were accepted, including older requests. Future requests still follow the selected mode.")
    except Exception as exc:
        await _error(message, exc)


@app.on_message(filters.private & filters.command(["autorequest", "requestall", "request"], prefixes=PRE))
async def private_settings(_, message):
    await _reply(message, "Open <code>/autorequest</code> in the group you manage. To complete your own pending verification here, send <code>/verifyrequests</code>.")


@app.on_message(filters.command("verifyrequests", prefixes=PRE))
async def verifyrequests(_, message):
    if message.chat.type != enums.ChatType.PRIVATE:
        return await _reply(message, "Open Maki privately and send <code>/verifyrequests</code>. Verification questions stay in DM.")
    try:
        count = await service.resend_for(_actor(message))
        if count:
            await _reply(message, f"Sent {count} verification question(s). Answer the buttons to finish joining.")
        else:
            await _reply(message, "No active verification could be sent. If your question expired, cancel and submit your join request again, or ask a group admin.")
    except Exception as exc:
        await _error(message, exc)


@app.on_chat_join_request()
async def incoming_request(_, request):
    if getattr(request.chat, "type", None) not in GROUPS:
        return
    try:
        await service.handle_request(request)
    except Exception as exc:
        # Never convert DM or verification failures into an approval.
        log.warning("Incoming join request could not be handled: %s", type(exc).__name__)


@app.on_callback_query(filters.regex(r"^gh:jr:v:"))
async def verify_callback(_, callback):
    try:
        await service.handle_verification(callback)
    except Exception as exc:
        log.warning("Join verification failed: %s", type(exc).__name__)
        try:
            await callback.answer("Could not confirm the result. Check the group, or send /verifyrequests in DM.", show_alert=True)
        except Exception:
            pass


@app.on_callback_query(filters.regex(r"^gh:jr:(?:open|mode:(?:off|verify|instant)|all|confirm:[0-9a-f]{24}|cancel|close)$"))
async def settings_callback(_, callback):
    message, user = callback.message, callback.from_user
    if not _group(message):
        return await callback.answer("Open /autorequest inside your group.", show_alert=True)
    try:
        if not user or user.is_bot:
            raise service.RequestError("Use your personal admin account.")
        await service.require_access(message.chat.id, user.id, check_bot=False)
    except service.RequestError as exc:
        return await callback.answer(str(exc), show_alert=True)
    except Exception:
        return await callback.answer("Could not verify your admin permissions. Try again.", show_alert=True)
    await callback.answer()
    action = callback.data.removeprefix("gh:jr:")
    try:
        if action in ("open", "cancel", "close"):
            _revoke(message.chat.id)
            if action == "close":
                return await message.delete()
        elif action.startswith("mode:"):
            _revoke(message.chat.id)
            await service.set_mode(message.chat.id, user.id, action.removeprefix("mode:"))
        elif action == "all":
            await service.require_access(message.chat.id, user.id)
            config = await service.settings(message.chat.id)
            _revoke(message.chat.id)
            if len(_consents) >= 512:
                _consents.pop(next(iter(_consents)))
            token = uuid.uuid4().hex[:24]
            pending = BulkConsent(message.chat.id, user.id, message.id,
                                  int(config.get("join_request_revision", 0)), time.monotonic() + 180)
            _consents[token] = pending
            try:
                await message.edit_text(
                    "<b>Accept every pending request?</b>\n\n"
                    "This accepts all requests currently waiting on Telegram, including older ones, "
                    "<b>without requester verification</b>. It does not enable future auto-accept.\n"
                    "Separate after-join group rules remain active.",
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=ikm([
                        [ikb("Accept all now", callback_data=f"gh:jr:confirm:{token}", style=enums.ButtonStyle.DANGER)],
                        [ikb("Cancel", callback_data="gh:jr:cancel", style=enums.ButtonStyle.PRIMARY)],
                    ]),
                )
            except BaseException:
                if _consents.get(token) is pending:
                    _consents.pop(token, None)
                raise
            return
        elif action.startswith("confirm:"):
            token = action.removeprefix("confirm:")
            pending = _consents.get(token)
            if not pending or pending.expires <= time.monotonic() or (
                pending.chat_id, pending.user_id, pending.message_id
            ) != (message.chat.id, user.id, message.id):
                return await _reply(message, "This confirmation expired or belongs to another admin/message. Open /autorequest again.", quote=False)
            if pending.running:
                return await _reply(message, "This approval request is already being processed.", quote=False)
            pending.running = True
            try:
                await service.approve_all(
                    message.chat.id, user.id,
                    expected_revision=pending.revision,
                    still_authorized=lambda: _consents.get(token) is pending and pending.expires > time.monotonic(),
                    operation_id=f"confirm:{token}",
                )
            finally:
                if _consents.get(token) is pending:
                    _consents.pop(token, None)
            await _reply(message, "All pending join requests were accepted. Future requests still follow the selected mode.", quote=False)
        await _show(message, edit=True)
    except MessageNotModified:
        pass
    except Exception as exc:
        await _error(message, exc)