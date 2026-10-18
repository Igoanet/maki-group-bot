"""Small, ordinary message purge commands.

The destructive full-history operation remains isolated in bulk_purge.  These
commands only remove a bounded range selected by an administrator and always
delete their own command/notice where Telegram permits it.
"""

from pyrogram import filters, types

from ghelp import app
from ghelp.helpers import require_right
from ghelp.selfdel import arm_delete, autodel_reply

PRE = ["/", "?", "!", "."]
_marks: dict[int, tuple[int, int]] = {}


@autodel_reply
async def _reply(m, text, **kwargs):
    return await m.reply_text(text, parse_mode="html", **kwargs)


async def _remove(chat_id: int, ids: list[int]) -> int:
    if not ids:
        return 0
    try:
        result = await app.delete_messages(chat_id, ids)
        # Pyrogram returns True for successful bulk deletion.
        return len(ids) if result is not False else 0
    except Exception:
        return 0


@app.on_message(filters.group & filters.command(["purge", "spurge"], prefixes=PRE))
async def purge(_, m: types.Message):
    if not await require_right(m, "can_delete_messages", "Delete messages"):
        return
    reply = m.reply_to_message
    if not reply:
        await _reply(m, "Reply to the first message to purge.")
        return
    try:
        amount = int(m.command[1]) if len(m.command) > 1 else None
        if amount is not None and not 1 <= amount <= 1000:
            raise ValueError
    except ValueError:
        await _reply(m, "Usage: <code>/purge</code> or <code>/purge &lt;1-1000&gt;</code>.")
        return
    start = reply.id
    end = min(m.id, start + amount) if amount is not None else m.id
    first = start if amount is None else start + 1
    count = await _remove(m.chat.id, list(range(first, end + 1)))
    if m.command[0].lower() == "spurge":
        return
    notice = await _reply(m, f"Purged <b>{count}</b> messages.")
    if notice:
        arm_delete(notice)


@app.on_message(filters.group & filters.command("del", prefixes=PRE))
async def delete_reply(_, m: types.Message):
    if not await require_right(m, "can_delete_messages", "Delete messages"):
        return
    if not m.reply_to_message:
        await _reply(m, "Reply to the message you want to delete.")
        return
    await _remove(m.chat.id, [m.reply_to_message.id, m.id])


@app.on_message(filters.group & filters.command("purgefrom", prefixes=PRE))
async def purge_from(_, m: types.Message):
    if not await require_right(m, "can_delete_messages", "Delete messages"):
        return
    if not m.reply_to_message:
        await _reply(m, "Reply to the first message with <code>/purgefrom</code>.")
        return
    _marks[m.chat.id] = (m.reply_to_message.id, m.from_user.id if m.from_user else 0)
    await _reply(m, "Purge start marked. Reply to the final message with <code>/purgeto</code>.")


@app.on_message(filters.group & filters.command("purgeto", prefixes=PRE))
async def purge_to(_, m: types.Message):
    if not await require_right(m, "can_delete_messages", "Delete messages"):
        return
    if not m.reply_to_message or m.chat.id not in _marks:
        await _reply(m, "Use <code>/purgefrom</code> first, then reply with <code>/purgeto</code>.")
        return
    start, owner = _marks.pop(m.chat.id)
    if owner and m.from_user and owner != m.from_user.id:
        await _reply(m, "Only the administrator who marked the range may finish it.")
        return
    end = m.reply_to_message.id
    if end < start:
        start, end = end, start
    count = await _remove(m.chat.id, list(range(start, end + 1)) + [m.id])
    notice = await _reply(m, f"Purged <b>{count}</b> messages.")
    if notice:
        arm_delete(notice)