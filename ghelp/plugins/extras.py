"""Extra group-management features: the commands Maki did not already have.

Built to match the reference help list.  Every command checks rights through
the existing helpers, and every destructive one is owner-gated or
right-gated.  Nothing here duplicates an existing plugin.
"""

import html
import io
import time
from collections import defaultdict, deque

from pyrogram import enums, filters, types

from ghelp import app, logger
from ghelp.config import config
from ghelp.database import db
from ghelp.helpers import ikb, ikm, is_admin, require_admin

_PRE = ["/", "!", "?", "."]

# mass-ban watcher: chat -> deque[(ts, actor_id)]
_ban_events: dict[int, deque] = defaultdict(deque)
_BAN_WINDOW = 10.0
_BAN_THRESHOLD = 5


# ══════════════════════════════════════════════════════════════════════════
# Announcements
# ══════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("announcement", prefixes=_PRE) & filters.group)
async def announcement_cmd(_, m: types.Message):
    if not await require_admin(m):
        return
    args = (m.text or "").split()
    cfg = await db.get_chat(m.chat.id)
    current = bool(cfg.get("announcement"))
    if len(args) == 1:
        return await m.reply_text(
            f"Announcements are <b>{'ON' if current else 'OFF'}</b> in this chat.\n"
            "Use <code>/announcement on</code> or <code>off</code>.",
            parse_mode=enums.ParseMode.HTML)
    want = args[1].lower() in ("on", "yes", "true", "1", "enable")
    await db.update_chat(m.chat.id, announcement=want)
    await m.reply_text(f"Announcements <b>{'enabled' if want else 'disabled'}</b>.",
                       parse_mode=enums.ParseMode.HTML)


# ══════════════════════════════════════════════════════════════════════════
# Anti-channel: drop messages sent as a channel
# ══════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("antichannel", prefixes=_PRE) & filters.group)
async def antichannel_cmd(_, m: types.Message):
    if not await require_admin(m, "can_delete_messages"):
        return
    args = (m.text or "").split()
    cfg = await db.get_chat(m.chat.id)
    current = bool(cfg.get("antichannel"))
    if len(args) == 1:
        return await m.reply_text(
            f"Anti-channel is <b>{'ON' if current else 'OFF'}</b>.\n"
            "When on, messages posted as a channel are deleted.",
            parse_mode=enums.ParseMode.HTML)
    want = args[1].lower() in ("on", "yes", "true", "1", "enable")
    await db.update_chat(m.chat.id, antichannel=want)
    await m.reply_text(f"Anti-channel <b>{'enabled' if want else 'disabled'}</b>.",
                       parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.group & ~filters.service, group=-6)
async def antichannel_watch(_, m: types.Message):
    """Delete channel-sent messages when the group opted in."""
    sender = getattr(m, "sender_chat", None)
    if sender is None or not m.from_user:
        return                                   # an anonymous admin is fine
    try:
        cfg = await db.get_chat(m.chat.id)
    except Exception:
        return
    if not cfg.get("antichannel"):
        return
    if sender.id == m.chat.id:
        return                                   # the group itself
    try:
        await m.delete()
        logger.info("antichannel: removed a channel post in %s", m.chat.id)
    except Exception as exc:
        logger.info("antichannel: delete failed: %s", type(exc).__name__)


# ══════════════════════════════════════════════════════════════════════════
# Self-service bans
# ══════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("banme", prefixes=_PRE) & filters.group)
async def banme_cmd(_, m: types.Message):
    user = m.from_user
    if not user:
        return
    if await is_admin(m.chat.id, user.id):
        return await m.reply_text("Admins cannot ban themselves — demote first.")
    try:
        await app.ban_chat_member(m.chat.id, user.id)
    except Exception as exc:
        return await m.reply_text(f"Could not ban you: <code>{type(exc).__name__}</code>",
                                  parse_mode=enums.ParseMode.HTML)
    try:
        await m.reply_text("You banned yourself. Ask an admin to unban you.")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# Cleaner toggle (service messages + command echo)
# ══════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("cleaner", prefixes=_PRE) & filters.group)
async def cleaner_cmd(_, m: types.Message):
    if not await require_admin(m, "can_delete_messages"):
        return
    args = (m.text or "").split()
    cfg = await db.get_chat(m.chat.id)
    current = bool(cfg.get("cleaner"))
    if len(args) == 1:
        return await m.reply_text(
            f"The service cleaner is <b>{'ON' if current else 'OFF'}</b>.\n"
            "It removes join/leave notices and messages that start with a "
            "command prefix.",
            parse_mode=enums.ParseMode.HTML)
    want = args[1].lower() in ("on", "yes", "true", "1", "enable")
    await db.update_chat(m.chat.id, cleaner=want)
    await m.reply_text(f"Service cleaner <b>{'enabled' if want else 'disabled'}</b>.",
                       parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.service, group=-5)
async def cleaner_watch(_, m: types.Message):
    try:
        cfg = await db.get_chat(m.chat.id)
    except Exception:
        return
    if not cfg.get("cleaner"):
        return
    try:
        await m.delete()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# Group photo / goodbye helpers
# ══════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("rmgpic", prefixes=_PRE) & filters.group)
async def rmgpic_cmd(_, m: types.Message):
    if not await require_admin(m, "can_change_info"):
        return
    try:
        await app.delete_chat_photo(m.chat.id)
    except Exception as exc:
        return await m.reply_text(f"Could not remove it: <code>{type(exc).__name__}</code>",
                                  parse_mode=enums.ParseMode.HTML)
    await m.reply_text("Group photo removed.")


@app.on_message(filters.command("cleangoodbye", prefixes=_PRE) & filters.group)
async def cleangoodbye_cmd(_, m: types.Message):
    if not await require_admin(m):
        return
    args = (m.text or "").split()
    cfg = await db.get_chat(m.chat.id)
    current = bool(cfg.get("cleangoodbye"))
    if len(args) == 1:
        return await m.reply_text(
            f"Clean-goodbye is <b>{'ON' if current else 'OFF'}</b>.",
            parse_mode=enums.ParseMode.HTML)
    want = args[1].lower() in ("on", "yes", "true", "1", "enable")
    await db.update_chat(m.chat.id, cleangoodbye=want)
    await m.reply_text(f"Clean-goodbye <b>{'enabled' if want else 'disabled'}</b>.",
                       parse_mode=enums.ParseMode.HTML)


# ══════════════════════════════════════════════════════════════════════════
# Member listing
# ══════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("userlist", prefixes=_PRE) & filters.group)
async def userlist_cmd(_, m: types.Message):
    if not await require_admin(m):
        return
    note = await m.reply_text("Collecting members…")
    total = admins = bots = 0
    names = []
    try:
        async for mem in app.get_chat_members(m.chat.id):
            u = mem.user
            if not u:
                continue
            total += 1
            if u.is_bot:
                bots += 1
            try:
                st = getattr(mem.status, "value", str(mem.status))
                if st in ("administrator", "creator", "owner"):
                    admins += 1
            except Exception:
                pass
            if len(names) < 30:
                names.append(f"• {html.escape(u.first_name or str(u.id))}")
    except Exception as exc:
        return await note.edit_text(
            f"Could not list members: <code>{type(exc).__name__}</code>",
            parse_mode=enums.ParseMode.HTML)
    body = (f"<b>Members in {html.escape(m.chat.title or '')}</b>\n\n"
            f"Total <b>{total}</b> · admins <b>{admins}</b> · bots <b>{bots}</b>\n\n"
            + "\n".join(names)
            + (f"\n<i>…and {total - len(names)} more</i>" if total > len(names) else ""))
    await note.edit_text(body, parse_mode=enums.ParseMode.HTML)


# ══════════════════════════════════════════════════════════════════════════
# Mass actions (owner only — they touch every member)
# ══════════════════════════════════════════════════════════════════════════
def _owner(m: types.Message) -> bool:
    return bool(m.from_user and m.from_user.id == config.OWNER_ID)


async def _run_mass(note, chat_id, action: str) -> None:
    """action: 'unban' | 'unmute' | 'zombies' | 'dumbs'."""
    done = failed = scanned = 0
    t0 = time.time()

    async def progress():
        try:
            await note.edit_text(
                f"Running <b>{action}</b> in <code>{chat_id}</code>\n"
                f"Scanned <b>{scanned}</b> · done <b>{done}</b> · failed <b>{failed}</b>",
                parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass

    flt = None
    if action == "unban":
        flt = enums.ChatMembersFilter.BANNED
    elif action == "unmute":
        flt = enums.ChatMembersFilter.RESTRICTED
    try:
        kwargs = {"filter": flt} if flt else {}
        async for mem in app.get_chat_members(chat_id, **kwargs):
            u = mem.user
            if not u:
                continue
            scanned += 1
            try:
                if action == "unban":
                    await app.unban_chat_member(chat_id, u.id)
                elif action == "unmute":
                    await app.restrict_chat_member(
                        chat_id, u.id,
                        types.ChatPermissions(
                            can_send_messages=True, can_send_media_messages=True,
                            can_send_other_messages=True,
                            can_add_web_page_previews=True))
                elif action == "zombies" and u.is_deleted:
                    await app.ban_chat_member(chat_id, u.id)
                elif action == "dumbs" and not u.username and not u.is_bot:
                    await app.ban_chat_member(chat_id, u.id)
                    await app.unban_chat_member(chat_id, u.id)
                else:
                    continue
                done += 1
            except Exception:
                failed += 1
            if scanned % 100 == 0:
                await progress()
    except Exception as exc:
        await note.edit_text(f"Stopped: <code>{type(exc).__name__}</code>",
                             parse_mode=enums.ParseMode.HTML)
        return
    took = time.time() - t0
    await note.edit_text(
        f"<b>{action} finished</b> in <code>{chat_id}</code>\n"
        f"Scanned <b>{scanned}</b> · changed <b>{done}</b> · failed <b>{failed}</b>\n"
        f"{took:.0f}s",
        parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command(["unbanall", "unmuteall", "clearzombies", "kickdumbs"],
                                prefixes=_PRE))
async def mass_actions(_, m: types.Message):
    if not _owner(m):
        return
    cmd = (m.command[0] if getattr(m, "command", None) else "").lower()
    action = {"unbanall": "unban", "unmuteall": "unmute",
              "clearzombies": "zombies", "kickdumbs": "dumbs"}[cmd]
    if m.chat.type != enums.ChatType.PRIVATE:
        try:
            await m.delete()
        except Exception:
            pass
    note = await app.send_message(m.from_user.id, f"Starting <b>{action}</b>…",
                                  parse_mode=enums.ParseMode.HTML)
    await _run_mass(note, m.chat.id, action)


# ══════════════════════════════════════════════════════════════════════════
# AntiBanAll: alert when an admin mass-bans
# ══════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("antibanall", prefixes=_PRE) & filters.group)
async def antibanall_cmd(_, m: types.Message):
    if not await require_admin(m):
        return
    args = (m.text or "").split()
    cfg = await db.get_chat(m.chat.id)
    current = bool(cfg.get("antibanall"))
    if len(args) == 1:
        return await m.reply_text(
            f"AntiBanAll is <b>{'ON' if current else 'OFF'}</b>.\n"
            f"When on, I alert admins if anyone bans more than "
            f"<b>{_BAN_THRESHOLD}</b> users in <b>{int(_BAN_WINDOW)}s</b>.",
            parse_mode=enums.ParseMode.HTML)
    want = args[1].lower() in ("on", "yes", "true", "1", "enable")
    await db.update_chat(m.chat.id, antibanall=want)
    await m.reply_text(f"AntiBanAll <b>{'enabled' if want else 'disabled'}</b>.",
                       parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.group, group=-7)
async def antibanall_watch(_, m: types.Message):
    """Watch for a burst of bans by one actor and alert the admins."""
    target = getattr(m, "left_chat_member", None) or getattr(m, "new_chat_members", None)
    if target is None:
        return
    try:
        cfg = await db.get_chat(m.chat.id)
    except Exception:
        return
    if not cfg.get("antibanall"):
        return
    actor = m.from_user.id if m.from_user else 0
    if not actor:
        return
    now = time.time()
    events = _ban_events[m.chat.id]
    events.append((now, actor))
    while events and now - events[0][0] > _BAN_WINDOW:
        events.popleft()
    same = [e for e in events if e[1] == actor]
    if len(same) < _BAN_THRESHOLD:
        return
    events.clear()
    who = m.from_user.mention if m.from_user else f"<code>{actor}</code>"
    text = (f"🚨 <b>Possible mass-ban detected</b>\n\n"
            f"{who} removed <b>{len(same)}</b> members in "
            f"{int(_BAN_WINDOW)}s.\n"
            f"<i>Review their admin rights if this was not intended.</i>")
    try:
        await app.send_message(m.chat.id, text, parse_mode=enums.ParseMode.HTML)
    except Exception:
        pass
    try:
        await app.send_message(config.OWNER_ID, text + f"\n\nChat: <code>{m.chat.id}</code>",
                               parse_mode=enums.ParseMode.HTML)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# Quotes and shayari
# ══════════════════════════════════════════════════════════════════════════
_SHAYARI = (
    "दिल से निकली दुआ कभी बेकार नहीं जाती,\nसिर्फ वक़्त लगता है फूल खिलने में।",
    "मंज़िल मिले न मिले ये तो मुक़द्दर की बात है,\nहम कोशिश ही न करें ये तो ग़लत बात है।",
    "जो अपने हों वो अपने लगते हैं हर हाल में,\nग़ैरों से उम्मीद रखना बेकार है।",
    "मुस्कुराते रहो हर हाल में,\nग़म भी तुम्हें देखकर पीछे हट जाएगा।",
    "रिश्ते निभाने पड़ते हैं बनाने नहीं,\nदिल से जुड़े लोग कभी दूर नहीं होते।",
)

_ANIME_QUOTES = (
    ("The world isn't perfect. But it's there for us, doing the best it can.", "— a well-known anime line"),
    ("Whatever you lose, you'll find it again. But what you throw away you'll never get back.", "— a well-known anime line"),
    ("If you don't take risks, you can't create a future.", "— a well-known anime line"),
    ("Power comes in response to a need, not a desire.", "— a well-known anime line"),
    ("Hard work is worthless for those that don't believe in themselves.", "— a well-known anime line"),
)


@app.on_message(filters.command(["shayri", "shayari"], prefixes=_PRE))
async def shayri_cmd(_, m: types.Message):
    import random
    await m.reply_text(random.choice(_SHAYARI))


@app.on_message(filters.command(["aqquote", "iaqquotes", "animequote"], prefixes=_PRE))
async def aqquote_cmd(_, m: types.Message):
    import random
    text, source = random.choice(_ANIME_QUOTES)
    await m.reply_text(f"<i>{html.escape(text)}</i>\n\n{html.escape(source)}",
                       parse_mode=enums.ParseMode.HTML)


# ══════════════════════════════════════════════════════════════════════════
# /write — put text on a paper-style image
# ══════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("write", prefixes=_PRE))
async def write_cmd(_, m: types.Message):
    text = (m.text or "").partition(" ")[2].strip()
    if not text:
        return await m.reply_text("Usage: <code>/write your text here</code>",
                                  parse_mode=enums.ParseMode.HTML)
    if len(text) > 600:
        text = text[:600] + "…"
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return await m.reply_text("Image support is unavailable on this bot.")
    W, H = 900, 600
    img = Image.new("RGB", (W, H), (250, 246, 236))
    draw = ImageDraw.Draw(img)
    # faint ruled lines for a paper feel
    for y in range(80, H - 40, 46):
        draw.line([(60, y), (W - 60, y)], fill=(228, 220, 205), width=1)
    draw.rectangle([(30, 30), (W - 30, H - 30)], outline=(200, 190, 170), width=3)
    font = None
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                 "/usr/share/fonts/truetype/freefont/FreeSans.ttf"):
        try:
            font = ImageFont.truetype(path, 30)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    # wrap to the paper width
    lines, line = [], ""
    for word in text.replace("\n", " \n ").split(" "):
        if word == "\n":
            lines.append(line); line = ""
            continue
        probe = (line + " " + word).strip()
        try:
            width = draw.textlength(probe, font=font)
        except Exception:
            width = len(probe) * 15
        if width > W - 140 and line:
            lines.append(line); line = word
        else:
            line = probe
    if line:
        lines.append(line)
    y = 70
    for ln in lines[:12]:
        draw.text((70, y), ln, fill=(45, 40, 35), font=font)
        y += 42
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "write.png"
    try:
        await m.reply_photo(buf, caption="✍️")
    except Exception as exc:
        logger.info("write: send failed: %s", type(exc).__name__)
        await m.reply_text("Could not render that.")


# ══════════════════════════════════════════════════════════════════════════
# /leave — owner makes the bot leave a chat
# ══════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command(["leavechat"], prefixes=_PRE))
async def leavechat_cmd(_, m: types.Message):
    if not _owner(m):
        return
    args = (m.text or "").split()
    if len(args) < 2 or not args[1].lstrip("-").isdigit():
        return await m.reply_text("Usage: <code>/leavechat &lt;chat_id&gt;</code>",
                                  parse_mode=enums.ParseMode.HTML)
    cid = int(args[1])
    try:
        await app.leave_chat(cid)
    except Exception as exc:
        return await m.reply_text(f"Could not leave: <code>{type(exc).__name__}</code>",
                                  parse_mode=enums.ParseMode.HTML)
    await m.reply_text(f"Left <code>{cid}</code>.", parse_mode=enums.ParseMode.HTML)
