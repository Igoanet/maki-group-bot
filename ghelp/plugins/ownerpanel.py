"""Owner-only panel: group list & stats, post-as-bot, cross-group ban,
mass member ban. Every command is limited to config.OWNER_ID and is
intentionally hidden from /help (listed in HIDDEN_COMMANDS in basic.py).

Known groups are tracked two ways: chat-member updates about the bot
itself, and a passive watcher that records any group a message arrives in.
"""

import asyncio
import html
import logging
import time

from pyrogram import enums, filters, types
from pyrogram.errors import FloodWait

from ghelp import app
from ghelp.selfdel import autodel_reply
from ghelp.config import config
from ghelp.database import db
from ghelp.helpers import ikb, ikm, mention, resolve_user
from ghelp.sudo import is_sudo

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]
_CHATS_KEY = "known_chats"     # db global: {str(chat_id): {"title": str, "admin": bool}}
_CACHE_TTL = 300               # seconds before /groups re-checks live
_MASSBAN_CAP = 1500            # never try to ban more than this in one go

_cache: dict = {"ts": 0.0, "rows": []}


def _is_owner(m: types.Message) -> bool:
    return (bool(m.from_user) and config.OWNER_ID != 0
            and m.from_user.id == config.OWNER_ID)


@autodel_reply
async def _reply(m: types.Message, text: str, **kwargs):
    return await m.reply_text(text, parse_mode=enums.ParseMode.HTML, **kwargs)


# ---- membership tracking -------------------------------------------------

async def _record(chat_id: int, title: str, admin: bool) -> None:
    def _put(chats):
        rec = chats.get(str(chat_id)) or {}
        if rec.get("title") == title and rec.get("admin") == admin:
            return False
        rec["title"] = title
        rec["admin"] = admin
        chats[str(chat_id)] = rec
        return True

    await db.mutate(_CHATS_KEY, _put, {})


async def _drop(chat_id: int) -> None:
    def _rm(chats):
        if str(chat_id) in chats:
            del chats[str(chat_id)]
            return True
        return False

    await db.mutate(_CHATS_KEY, _rm, {})


@app.on_chat_member_updated()
async def track_membership(_, u: types.ChatMemberUpdated) -> None:
    """Bot added / promoted / demoted / kicked anywhere."""
    try:
        if u.chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
            return
        new = u.new_chat_member
        old = u.old_chat_member
        if new and new.user and new.user.is_self:
            if new.status in (enums.ChatMemberStatus.LEFT,
                              enums.ChatMemberStatus.BANNED):
                await _drop(u.chat.id)
            else:
                await _record(
                    u.chat.id, u.chat.title or str(u.chat.id),
                    new.status in (enums.ChatMemberStatus.ADMINISTRATOR,
                                   enums.ChatMemberStatus.OWNER),
                )
        elif old and old.user and old.user.is_self:
            # removal updates can carry only the previous participant
            await _drop(u.chat.id)
        else:
            return  # someone else's membership changed — not ours
        _cache["ts"] = 0.0  # membership changed: next /groups re-checks
    except Exception as exc:
        logger.info("track_membership: %s", exc)


@app.on_message(filters.group & ~filters.service, group=-10)
async def track_chat_passive(_, m: types.Message) -> None:
    """Catch groups the bot was added to before this tracker existed."""
    try:
        chats = await db.get(_CHATS_KEY, {})
        rec = chats.get(str(m.chat.id))
        title = m.chat.title or str(m.chat.id)
        if rec and rec.get("title") == title:
            return
        # keep a previously known admin flag; unknown otherwise (live
        # refresh in /groups fixes it)
        await _record(m.chat.id, title, bool(rec.get("admin")) if rec else False)
        _cache["ts"] = 0.0
    except Exception as exc:
        logger.info("track_chat_passive: %s", exc)


# ---- /groups -------------------------------------------------------------

def _rows_from_store(chats: dict) -> list[dict]:
    rows = [
        {"id": int(cid), "title": rec.get("title") or cid,
         "admin": bool(rec.get("admin")),
         "members": rec.get("members"), "rights": rec.get("rights") or {}}
        for cid, rec in chats.items()
    ]
    rows.sort(key=lambda r: r["title"].lower())
    return rows


def _summary_text(rows: list[dict]) -> str:
    total = len(rows)
    admin = sum(1 for r in rows if r["admin"])
    lines = [
        f"<b>{config.BOT_NAME} is in {total} group(s)</b> — "
        f"admin in <b>{admin}</b>, plain member in <b>{total - admin}</b>.",
        "",
    ]
    for r in rows[:25]:
        mark = "Admin" if r["admin"] else "Member"
        line = f"{mark}: {html.escape(r['title'])} — <code>{r['id']}</code>"
        if r.get("members"):
            line += f" — {r['members']:,} members"
        rights = r.get("rights") or {}
        if r["admin"] and rights:
            line += " — Rights: " + ", ".join(
                label for attr, _, label in _RIGHTS if rights.get(attr))
        lines.append(line)
    if total > 25:
        lines.append(f"…and {total - 25} more.")
    if any(r["admin"] and r.get("rights") for r in rows):
        lines.append("<i>Rights: ban · delete · pin · invite · "
                     "video chat · info · add admins — "
                     "only the rights the bot HAS are shown. "
                     "Details: /ginfo &lt;id&gt;</i>")
    lines += [
        "",
        "Owner commands: <code>/ginfo &lt;id&gt;</code> · "
        "<code>/groups refresh</code> · <code>/gsay &lt;id&gt; text</code> · "
        "<code>/gban</code> (reply/id) · <code>/gunban</code> · "
        "<code>/massban &lt;id&gt;</code>",
    ]
    return "\n".join(lines)


def _groups_keyboard() -> types.InlineKeyboardMarkup:
        return ikm([[ikb("Refresh", callback_data="gh:groups:refresh",
                     style=enums.ButtonStyle.PRIMARY)]])


# errors that PROVE the bot is no longer in the chat — anything else is
# transient and must not delete the record
_DEFINITIVE_GONE = ("USER_NOT_PARTICIPANT", "CHANNEL_PRIVATE", "PEER_ID_INVALID",
                    "CHAT_ID_INVALID", "CHANNEL_INVALID", "CHAT_NOT_FOUND",
                    "DEACTIVATED", "KICKED")
_MAX_FLOOD_WAIT = 120  # longer waits: don't retry early, just keep old data


def _is_gone_error(exc: Exception) -> bool:
    msg = str(exc).upper()
    return any(tok in msg for tok in _DEFINITIVE_GONE)


# (attribute, unused marker, plain-word label) — labels power both cards.
_RIGHTS = [
    ("can_restrict_members", "", "ban users"),
    ("can_delete_messages", "", "delete messages"),
    ("can_pin_messages", "", "pin messages"),
    ("can_invite_users", "", "invite link"),
    ("can_manage_video_chats", "", "video chat"),
    ("can_change_info", "", "change group info"),
    ("can_promote_members", "", "add admins"),
]


def _rights_of(member) -> dict:
    """attr -> bool for the bot's admin rights in one chat."""
    if member.status == enums.ChatMemberStatus.OWNER:
        return {attr: True for attr, _, _ in _RIGHTS}
    pr = getattr(member, "privileges", None)
    return {attr: bool(getattr(pr, attr, False)) for attr, _, _ in _RIGHTS}


async def _live_refresh(chats: dict) -> list[dict]:
    """Ask Telegram for the bot's real status in every known group."""
    me = await app.get_me()
    rows: list[dict] = []
    counts_ok = True  # flips off if Telegram rate-limits the count calls
    for cid_s, rec in list(chats.items()):  # snapshot: db.get returns the live dict
        cid = int(cid_s)
        row = {"id": cid, "title": rec.get("title") or cid_s,
               "admin": bool(rec.get("admin")),  # unchecked rows keep old flag
               "gone": False}

        async def _check():
            nonlocal counts_ok
            member = await app.get_chat_member(cid, me.id)
            is_admin = member.status in (enums.ChatMemberStatus.ADMINISTRATOR,
                                         enums.ChatMemberStatus.OWNER)
            row["rights"] = _rights_of(member) if is_admin else {}
            if counts_ok:
                try:
                    row["members"] = await app.get_chat_members_count(cid)
                except FloodWait as fw:
                    wait = int(fw.value)
                    if wait <= _MAX_FLOOD_WAIT:
                        await asyncio.sleep(wait)  # honor Telegram's full delay
                        try:
                            row["members"] = await app.get_chat_members_count(cid)
                        except Exception:
                            counts_ok = False  # stop counts for this refresh
                    else:
                        counts_ok = False
                except Exception:
                    pass  # non-flood failure: just skip this chat's count
            return is_admin

        try:
            row["admin"] = await _check()
        except FloodWait as fw:
            wait = int(fw.value)
            if wait <= _MAX_FLOOD_WAIT:
                await asyncio.sleep(wait)  # honor Telegram's full delay
                try:
                    row["admin"] = await _check()
                except Exception as exc2:
                    row["gone"] = _is_gone_error(exc2)
            # longer wait: leave row unchecked with its stored flag
        except Exception as exc:
            row["gone"] = _is_gone_error(exc)
        rows.append(row)
        await asyncio.sleep(0.05)  # stay under flood limits
    return rows


async def _do_refresh(from_msg: types.Message | None = None):
    chats = await db.get(_CHATS_KEY, {})
    rows = await _live_refresh(chats)

    def _sync(store):
        changed = False
        for row in rows:
            key = str(row["id"])
            if row["gone"]:
                if key in store:
                    del store[key]
                    changed = True
            else:
                rec = store.setdefault(key, {})
                if rec.get("admin") != row["admin"]:
                    rec["admin"] = row["admin"]
                    changed = True
                if row.get("members") and rec.get("members") != row["members"]:
                    rec["members"] = row["members"]
                    changed = True
                if rec.get("rights") != row.get("rights"):
                    rec["rights"] = row.get("rights")
                    changed = True
        return changed

    await db.mutate(_CHATS_KEY, _sync, {})
    rows = [r for r in rows if not r["gone"]]
    rows.sort(key=lambda r: r["title"].lower())
    _cache.update(ts=time.time(), rows=rows)
    return rows


@app.on_message(filters.command("groups", prefixes=PRE))
async def groups_cmd(_, m: types.Message):
    if not m.from_user or not await is_sudo(m.from_user.id):
        return
    chats = await db.get(_CHATS_KEY, {})
    if not chats:
        await _reply(m, "Info: no groups known yet — the bot notes a group "
                        "when it's added there or a message arrives.")
        return
    force = len(m.command) > 1 and m.command[1].lower() in ("refresh", "live")
    if force or time.time() - _cache["ts"] > _CACHE_TTL or not _cache["rows"]:
        status = await _reply(m, f"Checking {len(chats)} groups live…",
                           preserve=True)
        try:
            rows = await _do_refresh()
            await status.edit_text(_summary_text(rows),
                                   parse_mode=enums.ParseMode.HTML,
                                   reply_markup=_groups_keyboard())
        except Exception:
            try:
                await status.delete()
            except Exception:
                pass
            raise
    else:
        await _reply(m, _summary_text(_cache["rows"]),
                     reply_markup=_groups_keyboard())


@app.on_callback_query(filters.regex(r"^gh:groups:refresh$"))
async def groups_refresh_cb(_, q: types.CallbackQuery):
    if not q.from_user or not await is_sudo(q.from_user.id):
        await q.answer("Staff only.", show_alert=True)
        return
    await q.answer("Refreshing…")
    if not q.message:
        return
    rows = await _do_refresh()
    try:
        await q.message.edit_text(_summary_text(rows),
                                  parse_mode=enums.ParseMode.HTML,
                                  reply_markup=_groups_keyboard())
    except Exception:
        pass


# ---- /ginfo --------------------------------------------------------------

@app.on_message(filters.command("ginfo", prefixes=PRE))
async def ginfo(_, m: types.Message):
    """Full card for one group: info + the bot's exact rights there."""
    if not m.from_user or not await is_sudo(m.from_user.id):
        return
    if len(m.command) < 2:
        await _reply(m, "Usage: <code>/ginfo &lt;chat_id&gt;</code> "
                        "— see /groups for the ids.")
        return
    try:
        cid = int(m.command[1])
    except ValueError:
        await _reply(m, "Error: give the numeric chat id (see /groups).")
        return
    try:
        chat = await app.get_chat(cid)
        me = await app.get_me()
        member = await app.get_chat_member(cid, me.id)
    except Exception as exc:
        await _reply(m, f"Error: could not read that group: "
                        f"<code>{html.escape(str(exc)[:200])}</code>")
        return
    if chat is None:
        await _reply(m, "Error: could not read that group (no data).")
        return
    if member.status == enums.ChatMemberStatus.OWNER:
        status_line = "Bot is the <b>owner</b>"
    elif member.status == enums.ChatMemberStatus.ADMINISTRATOR:
        status_line = "Bot is <b>admin</b>"
    else:
        status_line = "Bot is a plain <b>member</b>"
    rights = _rights_of(member)
    rlines = [f"{'Yes' if rights.get(attr) else 'No'}: {label}"
              for attr, _, label in _RIGHTS]
    uname = f"@{chat.username}" if getattr(chat, "username", None) else "private"
    members = getattr(chat, "members_count", None)
    id_line = f"<code>{cid}</code> · {uname}"
    if members:
        id_line += f" · {members:,} members"
    text = "\n".join([
        f"<b>{html.escape(chat.title or str(cid))}</b>",
        id_line, "", status_line, "", *rlines,
    ])
    desc = (getattr(chat, "description", None) or "").strip()
    if desc:
        text += f"\n\n<i>{html.escape(desc[:200])}</i>"
    await _reply(m, text)


# ---- /gsay ---------------------------------------------------------------

@app.on_message(filters.command("gsay", prefixes=PRE))
async def gsay(_, m: types.Message):
    """Post into one known group as the bot."""
    if not _is_owner(m):
        return
    if len(m.command) < 2:
        await _reply(m, "Usage: <code>/gsay &lt;chat_id&gt; &lt;text&gt;</code> "
                        "— or reply to any message with "
                        "<code>/gsay &lt;chat_id&gt;</code> to copy it there. "
                        "Use /groups to see the ids.")
        return
    try:
        cid = int(m.command[1])
    except ValueError:
        await _reply(m, "Error: first word must be the numeric chat id (see /groups).")
        return
    try:
        if m.reply_to_message:
            await m.reply_to_message.copy(cid)
        else:
            text = " ".join(m.command[2:]).strip()
            if not text:
                await _reply(m, "Usage: give text after the chat id, or reply to a message.")
                return
            await app.send_message(cid, text, parse_mode=enums.ParseMode.HTML)
        await _reply(m, f"Success: posted to <code>{cid}</code>.")
    except Exception as exc:
        await _reply(m, f"Error: could not post there: "
                        f"<code>{html.escape(str(exc)[:200])}</code>")


# ---- /gban & /gunban (this user in every group) --------------------------

async def _gact(m: types.Message, ban: bool):
    user = await resolve_user(m)
    if not user:
        await _reply(m, "Usage: reply to a user or give an @username/id.")
        return
    if user.id == config.OWNER_ID or user.is_bot:
        await _reply(m, "Error: cannot do that to this user.")
        return
    reason = " ".join(
        m.command[1:] if m.reply_to_message else m.command[2:]
    ).strip()
    chats = list((await db.get(_CHATS_KEY, {})).items())  # snapshot live dict
    verb = "Banning" if ban else "Unbanning"
    status = await _reply(m, f"{verb} in {len(chats)} known groups…")
    done = skipped = 0
    for cid_s, rec in chats:
        if not rec.get("admin"):
            skipped += 1
            continue
        cid = int(cid_s)

        async def _act():
            if ban:
                await app.ban_chat_member(cid, user.id)
            else:
                await app.unban_chat_member(cid, user.id)

        try:
            await _act()
            done += 1
        except FloodWait as fw:
            wait = int(fw.value)
            if wait > _MAX_FLOOD_WAIT:
                skipped += len(chats) - done - skipped  # report rest as skipped
                break  # honor the limit instead of retrying early
            await asyncio.sleep(wait)
            try:
                await _act()
                done += 1
            except Exception:
                skipped += 1
        except Exception:
            skipped += 1  # user not there / rights missing — fine
        await asyncio.sleep(0.1)
    who = mention(user.id, html.escape(user.first_name or "user"))
    reason_line = f"\n<b>Reason:</b> {html.escape(reason)}" if reason else ""
    done_word = "Global-banned" if ban else "Global-unbanned"
    await status.edit_text(
        f"{done_word} {who} in <b>{done}</b> groups "
        f"({skipped} skipped — bot not admin or user not there).{reason_line}",
        parse_mode=enums.ParseMode.HTML,
    )


@app.on_message(filters.command("gban", prefixes=PRE))
async def gban(_, m: types.Message):
    if not _is_owner(m):
        return
    await _gact(m, ban=True)


@app.on_message(filters.command("gunban", prefixes=PRE))
async def gunban(_, m: types.Message):
    if not _is_owner(m):
        return
    await _gact(m, ban=False)
