"""Sudo user management — the owner appoints trusted staff.

/addsudo /delsudo (rmsudo) — owner only; /listsudo (sudolist) — owner+sudo.
What sudo users may do is decided per command (see ghelp/sudo.py); the owner
is always sudo and can never be removed.
"""

import html

from pyrogram import enums, filters, types

from ghelp import app, logger
from ghelp.selfdel import autodel_reply
from ghelp.config import config
from ghelp.sudo import is_sudo, sudo_add, sudo_remove, sudo_users
from ghelp.helpers import concise_bot_text

PRE = ["/", "?", "!", "."]


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
    return await m.reply_text(
        concise_bot_text(text), parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command(["addsudo", "delsudo", "rmsudo"], prefixes=PRE))
async def sudo_cmd(_, m: types.Message):
    if not _owner(m):
        return
    cmd = m.command[0].lower()
    user = await _target(m)
    if user is None:
        return await _reply(
            m,
            f"Usage: reply to the person with <code>/{cmd}</code>, or "
            f"<code>/{cmd} @username</code> / <code>/{cmd} user_id</code>.",
        )
    if cmd == "addsudo":
        if user.id == config.OWNER_ID:
            return await _reply(m, "The owner is always sudo.")
        if user.is_bot:
            return await _reply(m, "Bots cannot be sudo.")
        added = await sudo_add(user.id)
        logger.info("sudo: %s added by owner (%s)", user.id, "new" if added else "already")
        return await _reply(
            m,
            f"Sudo added: {_label(user)}." if added
            else f"Already sudo: {_label(user)}.",
        )
    if user.id == config.OWNER_ID:
        return await _reply(m, "The owner cannot lose sudo.")
    removed = await sudo_remove(user.id)
    logger.info("sudo: %s removed by owner (%s)", user.id, "removed" if removed else "not listed")
    await _reply(
        m,
        f"Sudo removed: {_label(user)}." if removed
        else f"Not sudo: {_label(user)}.",
    )


@app.on_message(filters.command(["listsudo", "sudolist"], prefixes=PRE))
async def listsudo_cmd(_, m: types.Message):
    if not m.from_user or not await is_sudo(m.from_user.id):
        return
    try:
        owner_line = _label(await app.get_users(config.OWNER_ID))
    except Exception:
        owner_line = f"<code>{config.OWNER_ID}</code>"
    lines = []
    for uid in sorted(await sudo_users()):
        try:
            lines.append(f"{_label(await app.get_users(uid))}")
        except Exception:
            lines.append(f"<code>{uid}</code>")
    text = (
        f"<b>Owner:</b> {owner_line}\n\n<b>Sudo users:</b>\n"
        + ("\n".join(lines) if lines else "None")
    )
    await _reply(m, text)
