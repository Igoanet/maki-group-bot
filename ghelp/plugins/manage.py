"""Owner console — fast SILENT mass-ban + group management.

Design rules taken from the operator's requirements:

* SILENT — the only call used is ban_chat_member.  A "kick" (ban+unban) is
  never used, because that is what posts join/leave service messages.
* Nothing is ever sent to the group.  Commands are answered in DM only, and
  every progress/result line goes to the owner's private chat.
* Maximum speed — a large worker pool with no inter-call sleep.  When Telegram
  answers FLOOD_WAIT the item is retried immediately by default instead of
  parking the whole pool, so throughput stays as high as Telegram allows.
* Also provides an invite link, a banned-user list, and single-user unban, so
  the owner can manage the chat entirely from the bot.

Knobs (env): MASSBAN_WORKERS, MASSBAN_RETRIES, MASSBAN_SLEEP, MASSBAN_CAP.
"""

import asyncio
import html
import os
import time

from pyrogram import enums, filters, types
from pyrogram.errors import FloodWait

from ghelp import app, logger
from ghelp.config import config

PRE = ["/", "!", "?", "."]

_WORKERS = int(os.getenv("MASSBAN_WORKERS", "60"))
_RETRIES = int(os.getenv("MASSBAN_RETRIES", "3"))
_SLEEP = float(os.getenv("MASSBAN_SLEEP", "0"))
_CAP = int(os.getenv("MASSBAN_CAP", "200000"))

_PROTECTED = set(getattr(config, "PROTECTED_IDS", set()) or set())


def _is_owner(m: types.Message) -> bool:
    return bool(m.from_user and m.from_user.id == config.OWNER_ID)


def _kb(rows):
    return types.InlineKeyboardMarkup(
        [[types.InlineKeyboardButton(t, callback_data=d) for t, d in row]
         for row in rows]
    )


async def _dm(owner_id: int, text: str, markup=None):
    """Owner-only output.  Never touches the group."""
    try:
        return await app.send_message(
            owner_id, text, parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=markup,
        )
    except Exception as exc:
        logger.info("manage: DM to owner failed: %s", type(exc).__name__)
        return None


async def _edit(msg, text: str):
    if msg is None:
        return
    try:
        await msg.edit_text(text, parse_mode=enums.ParseMode.HTML,
                            disable_web_page_preview=True)
    except Exception:
        pass


async def _silence(m: types.Message):
    """Remove the command from a group so the chat stays untouched."""
    if m.chat and m.chat.type != enums.ChatType.PRIVATE:
        try:
            await m.delete()
        except Exception:
            pass


async def _admin_ids(chat_id: int) -> set:
    skip = {config.OWNER_ID}
    try:
        me = await app.get_me()
        skip.add(me.id)
    except Exception:
        pass
    try:
        async for adm in app.get_chat_members(
                chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
            if adm.user:
                skip.add(adm.user.id)
    except Exception as exc:
        logger.info("manage: admin list failed: %s", type(exc).__name__)
    return skip


async def _collect(chat_id: int, skip: set):
    ids = []
    try:
        async for mem in app.get_chat_members(chat_id):
            u = mem.user
            if not u or u.is_deleted or u.id in skip or u.id in _PROTECTED:
                continue
            ids.append(u.id)
            if len(ids) >= _CAP:
                break
    except Exception as exc:
        logger.info("manage: member walk stopped: %s", type(exc).__name__)
    return ids


async def _ban_all(chat_id: int, ids: list, progress) -> tuple:
    """Ban every id through a wide worker pool, silently, at full speed."""
    sem = asyncio.Semaphore(max(1, _WORKERS))
    lock = asyncio.Lock()
    state = {"ok": 0, "fail": 0, "flood": 0, "reported": 0}

    async def worker(uid: int):
        async with sem:
            for attempt in range(max(1, _RETRIES)):
                try:
                    await app.ban_chat_member(chat_id, uid)
                    async with lock:
                        state["ok"] += 1
                    break
                except FloodWait as fw:
                    async with lock:
                        state["flood"] += 1
                    if attempt == max(1, _RETRIES) - 1:
                        async with lock:
                            state["fail"] += 1
                        break
                    # immediate retry: no pool-wide parking
                    await asyncio.sleep(_SLEEP)
                    continue
                except Exception:
                    async with lock:
                        state["fail"] += 1
                    break
            if _SLEEP:
                await asyncio.sleep(_SLEEP)
            async with lock:
                total = state["ok"] + state["fail"]
                if total - state["reported"] >= 250:
                    state["reported"] = total
                    await progress(total, state["ok"], state["fail"], state["flood"])

    await asyncio.gather(*(worker(i) for i in ids))
    return state["ok"], state["fail"], state["flood"]


async def _run_massban(owner: int, cid: int, msg):
    t0 = time.time()
    skip = await _admin_ids(cid)
    ids = await _collect(cid, skip)
    if not ids:
        return await _edit(msg, "Nothing to do — no bannable members found "
                                "(check the chat id and the bot's Ban rights).")
    await _edit(msg, f"Banned-batch starting: <b>{len(ids):,}</b> targets, "
                     f"{_WORKERS} workers.")

    async def progress(total, ok, fail, flood):
        rate = total / max(time.time() - t0, 0.001)
        await _edit(msg, f"Running in <code>{cid}</code>\n"
                         f"Done <b>{total:,}</b> · ok <b>{ok:,}</b> · "
                         f"failed <b>{fail:,}</b> · flood <b>{flood:,}</b>\n"
                         f"{rate:,.0f}/s")

    ok, fail, flood = await _ban_all(cid, ids, progress)
    took = time.time() - t0
    await _edit(msg, f"<b>Finished</b> <code>{cid}</code>\n"
                     f"Banned <b>{ok:,}</b> · failed <b>{fail:,}</b> · "
                     f"flood hits <b>{flood:,}</b>\n"
                     f"{took/60:.1f} min · {(ok+fail)/max(took,0.001):,.0f}/s\n"
                     f"Nothing was posted to the group.")


# ── /massban ────────────────────────────────────────────────────────────────
@app.on_message(filters.command("massban", prefixes=PRE))
async def massban(_, m: types.Message):
    if not _is_owner(m):
        return
    await _silence(m)
    if len(m.command) < 2:
        return await _dm(m.from_user.id,
                         "<b>Silent mass-ban</b>\n"
                         f"<code>/massban &lt;chat_id&gt;</code>\n\n"
                         f"{_WORKERS} workers · no group messages · "
                         f"admins + VC assistant skipped")
    try:
        cid = int(m.command[1])
    except ValueError:
        return await _dm(m.from_user.id, "Error: give the numeric chat id.")
    title = str(cid)
    try:
        chat = await app.get_chat(cid)
        title = html.escape(chat.title or str(cid))
    except Exception:
        pass
    markup = _kb([
        [("Ban all — silent", f"og:mb:{cid}")],
        [("Cancel", "og:mb:x")],
    ])
    await _dm(m.from_user.id,
              f"<b>Silent mass-ban</b> in <b>{title}</b> (<code>{cid}</code>)?\n\n"
              f"No message will appear in the group.\n"
              f"This is permanent.", markup)


@app.on_callback_query(filters.regex(r"^og:mb:(-?\d+|x)$"))
async def massban_cb(_, q: types.CallbackQuery):
    if not q.from_user or q.from_user.id != config.OWNER_ID:
        return await q.answer("Owner only.", show_alert=True)
    await q.answer()
    if q.data.endswith(":x"):
        return await _edit(q.message, "Cancelled.")
    await _edit(q.message, "Collecting members…")
    await _run_massban(q.from_user.id, int(q.data.rsplit(":", 1)[-1]), q.message)


# ── /chatlink ───────────────────────────────────────────────────────────────
@app.on_message(filters.command(["chatlink", "grouplink"], prefixes=PRE))
async def chatlink(_, m: types.Message):
    if not _is_owner(m):
        return
    await _silence(m)
    if len(m.command) < 2:
        return await _dm(m.from_user.id, "Usage: <code>/chatlink &lt;chat_id&gt;</code>")
    try:
        cid = int(m.command[1])
    except ValueError:
        return await _dm(m.from_user.id, "Error: give the numeric chat id.")
    try:
        chat = await app.get_chat(cid)
        link = getattr(chat, "invite_link", None) or await app.export_chat_invite_link(cid)
        title = html.escape(chat.title or str(cid))
    except Exception as exc:
        return await _dm(m.from_user.id,
                         f"Error: <code>{html.escape(type(exc).__name__)}</code> — "
                         "the bot needs <b>Invite Users</b> there.")
    await _dm(m.from_user.id, f"<b>{title}</b>\n<code>{cid}</code>\n\n{html.escape(link)}")


# ── /unban ──────────────────────────────────────────────────────────────────
@app.on_message(filters.command(["unban", "ungban"], prefixes=PRE))
async def unban_cmd(_, m: types.Message):
    if not _is_owner(m):
        return
    await _silence(m)
    if len(m.command) < 3:
        return await _dm(m.from_user.id,
                         "Usage: <code>/unban &lt;chat_id&gt; &lt;user_id | "
                         "@username&gt;</code>\n"
                         "List first: <code>/bans &lt;chat_id&gt;</code>")
    try:
        cid = int(m.command[1])
    except ValueError:
        return await _dm(m.from_user.id, "Error: give the numeric chat id.")
    raw = m.command[2]
    try:
        if raw.lstrip("-").isdigit():
            uid = int(raw)
        else:
            uid = (await app.get_users(raw)).id
        await app.unban_chat_member(cid, uid)
    except Exception as exc:
        return await _dm(m.from_user.id, f"Error: <code>{html.escape(str(exc)[:140])}</code>")
    await _dm(m.from_user.id,
              f"Success: <code>{uid}</code> unbanned in <code>{cid}</code>.")


# ── /bans ───────────────────────────────────────────────────────────────────
@app.on_message(filters.command("bans", prefixes=PRE))
async def bans_cmd(_, m: types.Message):
    if not _is_owner(m):
        return
    await _silence(m)
    if len(m.command) < 2:
        return await _dm(m.from_user.id, "Usage: <code>/bans &lt;chat_id&gt;</code>")
    try:
        cid = int(m.command[1])
    except ValueError:
        return await _dm(m.from_user.id, "Error: give the numeric chat id.")
    n, shown = 0, []
    try:
        async for mem in app.get_chat_members(
                cid, filter=enums.ChatMembersFilter.BANNED):
            if not mem.user:
                continue
            n += 1
            if len(shown) < 40:
                shown.append(f"<code>{mem.user.id}</code> "
                             f"{html.escape(mem.user.first_name or '')}")
    except Exception as exc:
        return await _dm(m.from_user.id, f"Error: <code>{html.escape(type(exc).__name__)}</code>")
    if not n:
        return await _dm(m.from_user.id, "No banned users in that group.")
    more = f"\n<i>…and {n-len(shown)} more</i>" if n > len(shown) else ""
    await _dm(m.from_user.id,
              f"<b>Banned in {cid}:</b> {n}\n" + "\n".join(shown) + more +
              f"\n\nUnban: <code>/unban {cid} &lt;id&gt;</code>")


# ── /manage ─────────────────────────────────────────────────────────────────
@app.on_message(filters.command(["manage", "chatpanel"], prefixes=PRE))
async def manage_cmd(_, m: types.Message):
    if not _is_owner(m):
        return
    await _silence(m)
    if len(m.command) < 2:
        return await _dm(m.from_user.id, "Usage: <code>/manage &lt;chat_id&gt;</code>")
    try:
        cid = int(m.command[1])
    except ValueError:
        return await _dm(m.from_user.id, "Error: give the numeric chat id.")
    try:
        chat = await app.get_chat(cid)
        title = html.escape(chat.title or str(cid))
        count = getattr(chat, "members_count", None)
    except Exception as exc:
        return await _dm(m.from_user.id, f"Error: <code>{html.escape(type(exc).__name__)}</code>")
    rights = {}
    try:
        me = await app.get_me()
        member = await app.get_chat_member(cid, me.id)
        priv = getattr(member, "privileges", None)
        for k in ("can_restrict_members", "can_invite_users", "can_delete_messages"):
            rights[k] = bool(getattr(priv, k, False))
    except Exception:
        pass
    rtxt = " · ".join(f"{k.replace('can_','')}:{'yes' if v else 'NO'}"
                      for k, v in rights.items()) or "n/a"
    markup = _kb([
        [("Invite link", f"og:link:{cid}")],
        [("Banned list", f"og:bans:{cid}")],
        [("Silent mass-ban", f"og:mb:{cid}")],
    ])
    await _dm(m.from_user.id,
              f"<b>{title}</b>\n<code>{cid}</code>\n"
              f"Members: <b>{count if count is not None else '?'}</b>\n"
              f"Bot rights — {html.escape(rtxt)}\n\n"
              f"Unban: <code>/unban {cid} &lt;user_id&gt;</code>", markup)


@app.on_callback_query(filters.regex(r"^og:(link|bans):-?\d+$"))
async def manage_cb(_, q: types.CallbackQuery):
    if not q.from_user or q.from_user.id != config.OWNER_ID:
        return await q.answer("Owner only.", show_alert=True)
    await q.answer()
    _, what, cid_s = q.data.split(":")
    cid = int(cid_s)
    if what == "link":
        try:
            link = await app.export_chat_invite_link(cid)
        except Exception as exc:
            return await _dm(q.from_user.id, f"Error: <code>{html.escape(type(exc).__name__)}</code>")
        return await _dm(q.from_user.id, f"Invite link for <code>{cid}</code>:\n{html.escape(link)}")
    n, shown = 0, []
    try:
        async for mem in app.get_chat_members(
                cid, filter=enums.ChatMembersFilter.BANNED):
            if not mem.user:
                continue
            n += 1
            if len(shown) < 40:
                shown.append(f"<code>{mem.user.id}</code> "
                             f"{html.escape(mem.user.first_name or '')}")
    except Exception as exc:
        return await _dm(q.from_user.id, f"Error: <code>{html.escape(type(exc).__name__)}</code>")
    await _dm(q.from_user.id, (f"<b>Banned in {cid}:</b> {n}\n" + "\n".join(shown)) if n
              else "No banned users in that group.")
