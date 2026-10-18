"""Forum-topic management commands.

Telegram only exposes these operations for forum supergroups.  The handlers
fail clearly for ordinary groups instead of pretending a topic was changed.
"""

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.database import db
from ghelp.helpers import require_right

PRE = ["/", "?", "!", "."]


async def _reply(m: types.Message, text: str) -> None:
    await m.reply_text(text, parse_mode=enums.ParseMode.HTML)


def _thread_id(m: types.Message) -> int | None:
    return getattr(m, "message_thread_id", None)


@app.on_message(filters.group & filters.command("actiontopic", prefixes=PRE))
async def action_topic(_, m: types.Message):
    cfg = await db.get_chat(m.chat.id)
    topic_id = cfg.get("action_topic_id")
    if topic_id:
        await _reply(m, f"Action topic: <code>{int(topic_id)}</code>.")
    else:
        await _reply(m, "No action topic is configured.")


@app.on_message(filters.group & filters.command("setactiontopic", prefixes=PRE))
async def set_action_topic(_, m: types.Message):
    if not await require_right(m, "can_manage_topics", "Manage Topics"):
        return
    topic_id = _thread_id(m)
    if not topic_id:
        await _reply(m, "Reply from inside the topic you want to use.")
        return
    await db.update_chat(m.chat.id, action_topic_id=int(topic_id))
    await _reply(m, f"Action topic set to <code>{int(topic_id)}</code>.")


@app.on_message(filters.group & filters.command("newtopic", prefixes=PRE))
async def new_topic(_, m: types.Message):
    if not await require_right(m, "can_manage_topics", "Manage Topics"):
        return
    name = " ".join(m.command[1:]).strip()
    if not name:
        await _reply(m, "Usage: <code>/newtopic name</code>.")
        return
    try:
        topic = await app.create_forum_topic(m.chat.id, name)
        topic_id = getattr(topic, "message_thread_id", None)
        await _reply(m, f"Topic created: <b>{name}</b>"
                        + (f" (<code>{topic_id}</code>)" if topic_id else ""))
    except Exception:
        await _reply(m, "Could not create a topic. This must be a forum supergroup.")


@app.on_message(filters.group & filters.command("renametopic", prefixes=PRE))
async def rename_topic(_, m: types.Message):
    if not await require_right(m, "can_manage_topics", "Manage Topics"):
        return
    topic_id = _thread_id(m)
    name = " ".join(m.command[1:]).strip()
    if not topic_id or not name:
        await _reply(m, "Use <code>/renametopic name</code> inside a topic.")
        return
    try:
        await app.edit_forum_topic(m.chat.id, topic_id, name)
        await _reply(m, "Topic renamed.")
    except Exception:
        await _reply(m, "Could not rename this topic.")


@app.on_message(filters.group & filters.command("closetopic", prefixes=PRE))
async def close_topic(_, m: types.Message):
    if not await require_right(m, "can_manage_topics", "Manage Topics"):
        return
    topic_id = _thread_id(m)
    if not topic_id:
        await _reply(m, "Use this command inside a topic.")
        return
    try:
        await app.close_forum_topic(m.chat.id, topic_id)
        await _reply(m, "Topic closed.")
    except Exception:
        await _reply(m, "Could not close this topic.")


@app.on_message(filters.group & filters.command("reopentopic", prefixes=PRE))
async def reopen_topic(_, m: types.Message):
    if not await require_right(m, "can_manage_topics", "Manage Topics"):
        return
    topic_id = _thread_id(m)
    if not topic_id:
        await _reply(m, "Use this command inside a topic.")
        return
    try:
        await app.reopen_forum_topic(m.chat.id, topic_id)
        await _reply(m, "Topic reopened.")
    except Exception:
        await _reply(m, "Could not reopen this topic.")


@app.on_message(filters.group & filters.command("deletetopic", prefixes=PRE))
async def delete_topic(_, m: types.Message):
    if not await require_right(m, "can_manage_topics", "Manage Topics"):
        return
    topic_id = _thread_id(m)
    if not topic_id:
        await _reply(m, "Use this command inside a topic.")
        return
    try:
        await app.delete_forum_topic(m.chat.id, topic_id)
        await _reply(m, "Topic deleted.")
    except Exception:
        await _reply(m, "Could not delete this topic.")