"""Owner-only /clearme: delete the OWNER'S OWN messages from every group
their personal account is in. A bot can neither list nor delete another
user's message history, so this drives the owner's own account through a
Pyrogram session string (same pattern comman_bot used).

Safety rules: session string accepted only in DM, the paste is deleted
BEFORE processing (fail closed), the file is created atomically at 0600,
the session must belong to config.OWNER_ID, and nothing about it is
ever logged.

Deletion loop: kurigram paginates search_messages with a numeric offset,
so deleting while iterating SKIPS pages. Instead each chat repeatedly
fetches the FIRST page and deletes it until the page comes back empty.
A batch that deletes nothing means "no delete right here" — the chat is
counted failed instead of looping forever.
"""

import asyncio
import logging
import os
import time
from pathlib import Path

from pyrogram import Client, enums, filters, types
from pyrogram.errors import FloodWait

from ghelp import app
from ghelp.config import config
from ghelp.helpers import ikb, ikm

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]

_SESSION_PATH = Path(__file__).resolve().parents[2] / "data" / "clearme.session"
_BATCH = 100            # delete_messages max batch
_BATCH_PAUSE = 1.0      # seconds between delete batches (account safety)
_MAX_FLOOD = 300        # never sleep longer than this on one FloodWait
_MAX_FLOODS_PER_CHAT = 3
_EDIT_GAP = 5.0         # min seconds between progress edits

_stop = asyncio.Event()
_running = False


def _is_owner(m: types.Message) -> bool:
    return (bool(m.from_user) and config.OWNER_ID != 0
            and m.from_user.id == config.OWNER_ID)


def _load_session() -> str | None:
    try:
        s = _SESSION_PATH.read_text().strip()
        return s or None
    except OSError:
        return None


def _save_session(s: str) -> None:
    """Atomic create/replace at 0600 — never a world-readable window."""
    _SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SESSION_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, s.encode())
    finally:
        os.close(fd)
    os.replace(tmp, _SESSION_PATH)


def _client(sess: str) -> Client:
    return Client("clearme_owner", api_id=config.API_ID, api_hash=config.API_HASH,
                  session_string=sess, in_memory=True)


_CONFIRM_TXT = (
    "<b>Dhyan se padho:</b>\n"
    "Isse aapke account ke <b>SAARE messages</b> — jo bhi groups me aapne "
    "kabhi bheje — <b>har group se delete</b> ho jayenge. Yeh wapas nahi "
    "aa sakta.\n\n"
    "• DMs aur Saved Messages chhooe nahi jayenge\n"
    "• Telegram limits ki wajah se dheere chalega (ghanton lag sakte hain)\n"
    "• Beech me rokna ho to progress message ka Stop button\n\n"
    "Pakka karna hai?"
)
_CONFIRM_KB = ikm([[
    ikb("Haan, sab delete karo", callback_data="gh:cm:go",
        style=enums.ButtonStyle.DANGER),
    ikb("Rehne do", callback_data="gh:cm:no",
        style=enums.ButtonStyle.PRIMARY),
]])
_START_KB = ikm([[
    ikb("Start clearing", callback_data="gh:cm:open",
        style=enums.ButtonStyle.DANGER),
]])
_STOP_KB = ikm([[
    ikb("Stop", callback_data="gh:cm:stop",
        style=enums.ButtonStyle.DANGER),
]])


@app.on_message(filters.private & filters.command("clearme_session", prefixes=PRE))
async def clearme_session(_, m: types.Message):
    if not _is_owner(m):
        return
    # the paste carries full account access — it must be gone BEFORE we
    # touch the string; if deletion fails we refuse to save it
    try:
        await m.delete()
    except Exception:
        await app.send_message(
            m.chat.id,
            "Warning: Woh message delete nahi ho paya — session save NAHI kiya. "
            "Us message ko khud delete karo, phir dobara bhejo.")
        return
    sess = " ".join(m.command[1:]).strip() if len(m.command) > 1 else ""
    if not (sess.startswith("BQ") and len(sess) > 250):
        await app.send_message(
            m.chat.id,
            "Warning: Yeh session string nahi lagta. Poora <code>BQ…</code> string "
            "/clearme_session ke aage paste karo.",
            parse_mode=enums.ParseMode.HTML)
        return
    c = _client(sess)
    try:
        await asyncio.wait_for(c.connect(), timeout=30)
        me = await c.get_me()
    except Exception:
        me = None
    finally:
        try:
            await c.disconnect()
        except Exception:
            pass
    if me is None:
        await app.send_message(
            m.chat.id,
            "Warning: Session login fail — string expired ya galat hai. "
            "Naya string bana ke bhejo.")
        return
    if me.id != config.OWNER_ID:
        await app.send_message(
            m.chat.id,
            "Warning: Yeh session kisi AUR account ka hai — save nahi kiya. "
            "Sirf apne account ka string bhejo.")
        return
    _save_session(sess)
    await app.send_message(
        m.chat.id,
        f"Session save ho gaya (<b>{me.first_name}</b>).",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=_START_KB)


@app.on_message(filters.private & filters.command("clearme", prefixes=PRE))
async def clearme(_, m: types.Message):
    if not _is_owner(m):
        return
    if _running:
        await m.reply_text("Already running. Rokne ke liye /clearme_stop.")
        return
    if not _load_session():
        await m.reply_text(
            "Warning: Pehle apna session string save karo:\n"
            "<code>/clearme_session BQ…</code>\n"
            "(string wala message main turant delete kar dunga)",
            parse_mode=enums.ParseMode.HTML)
        return
    await m.reply_text(_CONFIRM_TXT, parse_mode=enums.ParseMode.HTML,
                       reply_markup=_CONFIRM_KB)


@app.on_message(filters.private & filters.command("clearme_stop", prefixes=PRE))
async def clearme_stop(_, m: types.Message):
    if not _is_owner(m):
        return
    if _running:
        _stop.set()
        await m.reply_text("Stopping — 1-2 second me ruk jayega.")
    else:
        await m.reply_text("Kuch chal nahi raha.")


@app.on_callback_query(filters.regex(r"^gh:cm:(go|no|open|stop)$"))
async def clearme_cb(_, q: types.CallbackQuery):
    global _running
    if not q.from_user or q.from_user.id != config.OWNER_ID:
        await q.answer("Sirf owner ke liye hai.", show_alert=True)
        return
    if q.data == "gh:cm:open":
        if _running:
            await q.answer("Pehle se chal raha hai.", show_alert=True)
            return
        if not _load_session():
            await q.answer("Pehle /clearme_session se session save karo.",
                           show_alert=True)
            return
        await q.message.edit_text(_CONFIRM_TXT, parse_mode=enums.ParseMode.HTML,
                                  reply_markup=_CONFIRM_KB)
        await q.answer()
        return
    if q.data == "gh:cm:stop":
        if _running:
            _stop.set()
            await q.answer("Stopping…")
        else:
            await q.answer("Kuch chal nahi raha.", show_alert=True)
        return
    if q.data == "gh:cm:no":
        await q.message.edit_text("Cancelled — kuch delete nahi hua.")
        await q.answer()
        return
    # reserve the job atomically — no await between check and set
    if _running:
        await q.answer("Pehle se chal raha hai.", show_alert=True)
        return
    _running = True
    _stop.clear()
    try:
        await q.answer("Shuru!")
        status = await q.message.edit_text("Connecting account…",
                                           reply_markup=_STOP_KB)
    except Exception:
        # reservation without a task would wedge _running until restart
        _running = False
        return
    asyncio.create_task(_run_clear(status))


async def _wait(seconds: float) -> None:
    """Stop-interruptible sleep — long FloodWaits must not freeze /clearme_stop."""
    end = time.monotonic() + min(seconds, _MAX_FLOOD)
    while not _stop.is_set():
        left = end - time.monotonic()
        if left <= 0:
            return
        await asyncio.sleep(min(1.0, left))


async def _delete_ids(c: Client, chat_id: int, ids: list[int]) -> int:
    """Delete one batch. Returns the number Telegram ACTUALLY deleted
    (so a zero-effect delete trips the caller's n == 0 guard); raises
    FloodWait when the limit will not lift; returns 0 when deletion
    itself is not allowed."""
    last_fw: FloodWait | None = None
    for _ in range(3):
        try:
            res = await c.delete_messages(chat_id, ids)
            return res if isinstance(res, int) else len(ids)
        except FloodWait as e:
            if e.value > _MAX_FLOOD:
                raise
            last_fw = e
            await _wait(e.value + 1)
        except Exception:
            return 0          # no delete right / messages already gone
    assert last_fw is not None
    raise last_fw


async def _clear_chat(c: Client, chat: types.Chat) -> tuple[int, int, str]:
    """Delete the owner's messages from one chat. Fetch page 1, delete,
    refetch — never paginate a shrinking result set. A batch that deletes
    nothing means we have no delete right here: stop, don't loop.

    Returns (deleted, failed, outcome) where outcome is "complete",
    "failed", or "stopped". "failed" means the chat was aborted with
    messages possibly remaining — even when no ids were ever collected."""
    deleted = failed = 0
    floods = 0   # FloodWait attempts since the last SUCCESSFUL delete —
                 # a successful search does NOT reset it, or a persistent
                 # delete-side flood would re-fetch and retry forever
    while not _stop.is_set():
        try:
            ids = [msg.id async for msg in
                   c.search_messages(chat.id, from_user="me", limit=_BATCH)]
        except FloodWait as e:
            floods += 1
            if floods > _MAX_FLOODS_PER_CHAT or e.value > _MAX_FLOOD:
                return deleted, failed, "failed"
            await _wait(e.value + 1)
            continue
        except Exception:
            return deleted, failed, "failed"
        if not ids:
            return deleted, failed, "complete"
        try:
            n = await _delete_ids(c, chat.id, ids)
        except FloodWait:
            floods += 1
            if floods > _MAX_FLOODS_PER_CHAT:
                return deleted, failed + len(ids), "failed"
            continue
        if n == 0:
            return deleted, failed + len(ids), "failed"   # can't delete here
        deleted += n
        failed += len(ids) - n
        floods = 0          # real progress — flood budget resets here only
        await _wait(_BATCH_PAUSE)
    return deleted, failed, "stopped"


async def _run_clear(status: types.Message) -> None:
    global _running
    c: Client | None = None
    total_del = total_fail = 0
    groups_done = groups_fail = 0
    last_edit = 0.0

    async def _say(text: str, force: bool = False, final: bool = False) -> None:
        nonlocal last_edit
        now = time.monotonic()
        if not force and now - last_edit < _EDIT_GAP:
            return
        try:
            await status.edit_text(text, parse_mode=enums.ParseMode.HTML,
                                   reply_markup=None if final else _STOP_KB)
            last_edit = time.monotonic()
        except Exception:
            pass            # never let progress edits block the job

    try:
        sess = _load_session()
        if not sess:
            await _say("Warning: Session file gayab — /clearme_session se dobara save karo.",
                       force=True, final=True)
            return
        c = _client(sess)
        await c.connect()
        groups = [
            d.chat async for d in c.get_dialogs()
            if d.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)
        ]
        await _say(f"<b>{len(groups)}</b> groups mile. Deletion shuru…",
                   force=True)

        for chat in groups:
            if _stop.is_set():
                break
            d, f, outcome = await _clear_chat(c, chat)
            total_del += d
            total_fail += f
            if outcome == "stopped":
                break
            if outcome == "complete":
                groups_done += 1
            else:
                groups_fail += 1
            await _say(
                f"<b>{groups_done + groups_fail}/{len(groups)}</b> groups\n"
                f"Deleted: <b>{total_del}</b> messages"
                + (f" (skip/fail: {total_fail})" if total_fail else ""))

        word = "Rok diya" if _stop.is_set() else "Ho gaya"
        await _say(
            f"{word}!\n\n"
            f"• Groups poore: <b>{groups_done}</b>/{len(groups)}\n"
            f"• Messages deleted: <b>{total_del}</b>\n"
            + (f"• Adhoore/fail: {groups_fail} groups, {total_fail} msgs\n"
               if (total_fail or groups_fail) else "")
            + "\nFail wale groups me messages bache hain — /clearme dobara "
            "chala sakte ho.",
            force=True, final=True)
    except Exception as exc:
        logger.warning("clearme failed: %s", type(exc).__name__)
        await _say(f"Warning: Error aa gaya ({type(exc).__name__}). "
                   f"Ab tak {total_del} messages delete hue.",
                   force=True, final=True)
    finally:
        _running = False
        if c is not None:
            try:
                await c.disconnect()
            except Exception:
                pass
