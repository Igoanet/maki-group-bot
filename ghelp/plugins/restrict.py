"""Owner-only global controls.

1. User restriction (owner + sudo) — a restricted user can't use any bot function. The list is shared with the
   music bot (ghelp/restrictions.py is a symlink to bots/shared/restrictions.py), so `/restrict`
   in either bot applies in both. group_help.py hooks the dispatcher before the plugins load
   (see ghelp/restrict_gate.py): every command, button and inline handler plus the feature
   watchers (#notes, @admin) are hidden from restricted users. Moderation watchers (locks,
   blocklist, flood, shield, captcha…) keep seeing a restricted user's messages on purpose.

       /restrict <reply|@user|id>   /unrestrict <reply|@user|id>   /restricted

2. /fq guard — nobody gets a fake quote of the bot owner unless the owner allowed them:

       /fqallow <reply|@user|id>    /fqdeny <reply|@user|id>    /fqallowed
"""

import html
import time

from pyrogram import StopPropagation, enums, filters, types

from ghelp import app, logger, restrict_gate, restrictions
from ghelp.selfdel import autodel_reply
from ghelp.config import config
from ghelp.database import db
from ghelp.sudo import is_sudo

PRE = ["/", "?", "!", "."]
NOTICE = "Access denied: you're restricted from using this bot."
_NOTICE_EVERY = 300  # seconds between notices to the same user in PM
_last_notice: dict[int, float] = {}
FQ_ALLOWED_KEY = "fq_allowed"  # db global: [user ids allowed to fake-quote the owner]

_blocked = restrict_gate.blocked_by(config.OWNER_ID)


@app.on_message(filters.private & filters.regex(r"^[/!?]"), group=-3)
@restrict_gate.exempt
async def _pm_notice(_, m: types.Message):
    """Restricted users get one short answer in PM; in groups the bot stays silent."""
    if not _blocked(m.from_user):
        return
    now = time.monotonic()
    if now - _last_notice.get(m.from_user.id, 0.0) > _NOTICE_EVERY:
        _last_notice[m.from_user.id] = now
        try:
            await m.reply_text(NOTICE)
        except Exception:
            pass


async def _blocked_callback_filter(
    _filter, _, cq: types.CallbackQuery
) -> bool:
    """Run the gate handler only for users it can actually block.

    The rate guard wraps every registered callback independently.  Leaving
    this handler unfiltered charges an allowed tap once here and again in its
    feature handler, quickly exhausting the callback bucket before Telegram
    ever reaches the visible button action.
    """
    return _blocked(getattr(cq, "from_user", None))


@app.on_callback_query(
    filters.create(_blocked_callback_filter, "BlockedCallback"),
    group=-3,
)
@restrict_gate.exempt
async def _gate_callback(_, cq: types.CallbackQuery):
    if not _blocked(cq.from_user):
        return
    try:
        await cq.answer(NOTICE, show_alert=True)
    except Exception:
        pass
    raise StopPropagation


@app.on_inline_query(group=-3)
@restrict_gate.exempt
async def _gate_inline(_, iq: types.InlineQuery):
    if not _blocked(iq.from_user):
        return
    try:
        await iq.answer([], cache_time=5, is_personal=True)
    except Exception:
        pass
    raise StopPropagation


# ---------------------------------------------------------------- /fq guard

async def fq_allowed(user: types.User | None) -> bool:
    """May this user make a fake quote of the bot owner?"""
    if not user:
        return False
    if user.id == config.OWNER_ID:
        return True
    return user.id in (await db.get(FQ_ALLOWED_KEY, []) or [])


# ---------------------------------------------------------------- owner commands

def _owner(m: types.Message) -> bool:
    return bool(m.from_user) and m.from_user.id == config.OWNER_ID and config.OWNER_ID != 0


async def _target(m: types.Message) -> types.User | None:
    """Reply → that sender; else the first argument as @username / id / mention-picker tag."""
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user
    for ent in m.entities or []:
        if ent.type == enums.MessageEntityType.TEXT_MENTION and ent.user:
            return ent.user
    if len(m.command) < 2:
        return None
    arg = m.command[1].lstrip("@")
    try:
        user = await app.get_users(int(arg) if arg.isdigit() else arg)
    except Exception:
        return None
    return None if isinstance(user, list) else user  # a single ident never yields a list


def _label(user: types.User) -> str:
    name = html.escape(user.first_name or user.username or str(user.id))
    return f'<a href="tg://user?id={user.id}">{name}</a> (<code>{user.id}</code>)'


@autodel_reply
async def _reply(m: types.Message, text: str):
    return await m.reply_text(text, parse_mode=enums.ParseMode.HTML)


def _usage(cmd: str) -> str:
    return (f"Usage: reply to the person with <code>/{cmd}</code>, or "
            f"<code>/{cmd} @username</code> / <code>/{cmd} user_id</code>.")


@app.on_message(filters.command(["restrict", "unrestrict", "restricted"], prefixes=PRE))
async def restrict_cmd(_, m: types.Message):
    if not m.from_user or not await is_sudo(m.from_user.id):
        return
    cmd = m.command[0].lower()
    if cmd == "restricted":
        users = restrictions.restricted()
        if not users:
            return await _reply(m, "Nobody is restricted.")
        lines = []
        for uid, rec in users.items():
            name = html.escape(rec.get("name") or "")
            when = time.strftime("%d %b %Y", time.localtime(rec.get("at", 0))) if rec.get("at") else ""
            lines.append(f"• <code>{uid}</code> {name} {('— ' + when) if when else ''}".rstrip())
        return await _reply(m, "<b>Restricted users</b> (both bots):\n" + "\n".join(lines))

    user = await _target(m)
    if user is None:
        return await _reply(m, _usage(cmd))
    if await is_sudo(user.id):
        return await _reply(m, "Staff (owner/sudo) cannot be restricted.")
    if user.is_self:
        return await _reply(m, "That is the bot.")

    if cmd == "restrict":
        added = restrictions.restrict(user.id, by=m.from_user.id, name=user.first_name or user.username or "")
        logger.info("restrict: %s restricted by staff (%s)", user.id, "new" if added else "already")
        text = (f"Blocked: {_label(user)} can no longer use the group-help bot or the music bot."
                if added else f"Already restricted: {_label(user)}.")
    else:
        removed = restrictions.unrestrict(user.id)
        logger.info("restrict: %s unrestricted by staff (%s)", user.id, "removed" if removed else "not listed")
        text = (f"Restriction removed: {_label(user)} can use both bots again."
                if removed else f"Not restricted: {_label(user)}.")
    await _reply(m, text)


@app.on_message(filters.command(["fqallow", "fqdeny", "fqallowed"], prefixes=PRE))
async def fq_allow_cmd(_, m: types.Message):
    if not _owner(m):
        return
    cmd = m.command[0].lower()
    if cmd == "fqallowed":
        allowed = await db.get(FQ_ALLOWED_KEY, []) or []
        if not allowed:
            return await _reply(m, "Nobody may fake-quote you. "
                                "Next: use <code>/fqallow @user</code> to allow someone.")
        return await _reply(m, "<b>Allowed to fake-quote you:</b>\n"
                            + "\n".join(f"• <code>{uid}</code>" for uid in allowed))

    user = await _target(m)  # resolve first — the list is edited atomically below
    if user is None:
        return await _reply(m, _usage(cmd))
    if user.id == config.OWNER_ID:
        return await _reply(m, "The owner can always fake-quote themself.")

    changed: dict = {}

    def _allow(allowed: list) -> bool:
        if user.id in allowed:
            return False
        allowed.append(user.id)
        changed["yes"] = True
        return True

    def _deny(allowed: list) -> bool:
        if user.id not in allowed:
            return False
        allowed.remove(user.id)
        changed["yes"] = True
        return True

    await db.mutate(FQ_ALLOWED_KEY, _allow if cmd == "fqallow" else _deny, [])
    if cmd == "fqallow":
        if not changed:
            return await _reply(m, f"Already allowed: {_label(user)}.")
        logger.info("fq: %s allowed to fake-quote the owner", user.id)
        return await _reply(m, f"Allowed: {_label(user)} may now make fake quotes of you.")
    if not changed:
        return await _reply(m, f"Not allowed: {_label(user)} wasn't on the list.")
    logger.info("fq: %s no longer allowed to fake-quote the owner", user.id)
    await _reply(m, f"Access removed: {_label(user)} can't make fake quotes of you.")
