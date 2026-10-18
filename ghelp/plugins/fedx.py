"""Federation extras (Rose parity): rename/delete feds, fed admins, stats,
ban lists & imports, subscriptions, fed log channel."""

import html
import logging

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.selfdel import autodel_reply
from ghelp.database import db
from ghelp.helpers import concise_bot_text, mention, require_admin, resolve_user

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]


@autodel_reply
async def _reply(m: types.Message, text: str, **kwargs):
    return await m.reply_text(
        concise_bot_text(text), parse_mode=enums.ParseMode.HTML, **kwargs)


async def _owner_fed(m: types.Message):
    """(fed_id, fed) when the caller owns the fed this chat belongs to."""
    cfg = await db.get_chat(m.chat.id)
    fid = str(cfg.get("fed_id") or "")
    if not fid:
        await _reply(m, "This group is not in a federation.")
        return None, None
    feds = await db.get_feds()
    fed = feds.get(fid)
    if not fed:
        await _reply(m, "Federation data missing.")
        return None, None
    if not m.from_user or m.from_user.id != int(fed.get("owner", 0)):
        await _reply(m, "Only the federation owner can do this.")
        return None, None
    return fid, fed


@app.on_message(filters.group & filters.command("renamefed", prefixes=PRE))
async def rename_fed(_, m: types.Message):
    fid, fed = await _owner_fed(m)
    if not fid:
        return
    name = " ".join(m.command[1:]).strip()
    if not name:
        await _reply(m, "Usage: <code>/renamefed &lt;new name&gt;</code>")
        return

    def _set(f):
        f["name"] = name

    await db.mutate_fed(fid, _set)
    await _reply(m, f"Federation renamed to <b>{html.escape(name)}</b>.")


@app.on_message(filters.group & filters.command("delfed", prefixes=PRE))
async def del_fed(_, m: types.Message):
    fid, fed = await _owner_fed(m)
    if not fid:
        return
    for cid in fed.get("chats", []):
        try:
            await db.update_chat(cid, fed_id="")
        except Exception:
            pass
    await db.delete_fed(fid)
    await _reply(m, f"Federation <b>{html.escape(fed.get('name', ''))}</b> deleted.")


@app.on_message(filters.group & filters.command("fedpromote", prefixes=PRE))
async def fed_promote(_, m: types.Message):
    fid, fed = await _owner_fed(m)
    if not fid:
        return
    user = await resolve_user(m)
    if not user:
        await _reply(m, "Warning: Reply to a user or give an @username/id.")
        return

    def _add(f):
        f.setdefault("fadmins", [])
        if user.id not in f["fadmins"]:
            f["fadmins"].append(user.id)

    await db.mutate_fed(fid, _add)
    who = mention(user.id, html.escape(user.first_name or "user"))
    await _reply(m, f"Done: {who} is now a fed-admin (can /fban).")


@app.on_message(filters.group & filters.command("feddemote", prefixes=PRE))
async def fed_demote(_, m: types.Message):
    fid, fed = await _owner_fed(m)
    if not fid:
        return
    user = await resolve_user(m)
    if not user:
        await _reply(m, "Warning: Reply to a user or give an @username/id.")
        return

    def _rm(f):
        if user.id in f.get("fadmins", []):
            f["fadmins"].remove(user.id)

    await db.mutate_fed(fid, _rm)
    who = mention(user.id, html.escape(user.first_name or "user"))
    await _reply(m, f"Done: {who} removed from fed-admins.")


@app.on_message(filters.group & filters.command("feddemoteme", prefixes=PRE))
async def fed_demote_me(_, m: types.Message):
    if not m.from_user:
        return
    cfg = await db.get_chat(m.chat.id)
    fid = str(cfg.get("fed_id") or "")
    if not fid:
        await _reply(m, "Info: This group is not in a federation.")
        return
    fed = (await db.get_feds()).get(fid) or {}
    if m.from_user.id not in fed.get("fadmins", []):
        await _reply(m, "Info: You are not a fed-admin of this federation.")
        return

    def _rm(f):
        if m.from_user.id in f.get("fadmins", []):
            f["fadmins"].remove(m.from_user.id)

    await db.mutate_fed(fid, _rm)
    await _reply(m, "Done: You are no longer a fed-admin.")


@app.on_message(filters.group & filters.command("fedadmins", prefixes=PRE))
async def fed_admins(_, m: types.Message):
    cfg = await db.get_chat(m.chat.id)
    fid = str(cfg.get("fed_id") or "")
    if not fid:
        await _reply(m, "Info: This group is not in a federation.")
        return
    fed = (await db.get_feds()).get(fid)
    if not fed:
        await _reply(m, "Federation data missing.")
        return
    owner = int(fed.get("owner", 0))
    lines = [f"Owner: {mention(owner, 'owner')}"]
    for uid in fed.get("fadmins", []):
        lines.append(f"• {mention(int(uid), 'fed-admin')}")
    await _reply(m, f"<b>{html.escape(fed.get('name', ''))}</b> admins:\n" + "\n".join(lines))


@app.on_message(filters.command("myfeds", prefixes=PRE))
async def my_feds(_, m: types.Message):
    if not m.from_user:
        return
    mine = [
        (fid, f) for fid, f in (await db.get_feds()).items()
        if int(f.get("owner", 0)) == m.from_user.id
    ]
    if not mine:
        await _reply(m, "Info: You own no federations.")
        return
    lines = [
        f"• <b>{html.escape(f.get('name', ''))}</b> — <code>{fid}</code> "
        f"({len(f.get('chats', []))} groups, {len(f.get('banned', []))} bans)"
        for fid, f in mine
    ]
    await _reply(m, "<b>Your federations:</b>\n" + "\n".join(lines))


@app.on_message(filters.group & filters.command("chatfed", prefixes=PRE))
async def chat_fed(_, m: types.Message):
    cfg = await db.get_chat(m.chat.id)
    fid = str(cfg.get("fed_id") or "")
    if not fid:
        await _reply(m, "Info: This group is not in any federation.")
        return
    fed = (await db.get_feds()).get(fid)
    name = html.escape(fed.get("name", "?")) if fed else "missing"
    await _reply(m, f"This group belongs to <b>{name}</b> (<code>{fid}</code>).")


@app.on_message(filters.group & filters.command(["fedstat", "fbanstat"], prefixes=PRE))
async def fed_stat(_, m: types.Message):
    user = await resolve_user(m) or m.from_user
    if not user:
        await _reply(m, "Warning: Reply to a user or give an @username/id.")
        return
    feds = await db.get_feds()
    cfg = await db.get_chat(m.chat.id)
    fid = str(cfg.get("fed_id") or "")
    here = fid and user.id in (feds.get(fid) or {}).get("banned", [])
    total = sum(1 for f in feds.values() if user.id in f.get("banned", []))
    who = mention(user.id, html.escape(user.first_name or "user"))
    here_txt = ("fed-banned here" if here else "not fed-banned here") if fid else "no fed in this group"
    await _reply(m, f"{who}: <b>{here_txt}</b>; banned in <b>{total}</b> federation(s) overall.")


@app.on_message(filters.group & filters.command("fbanlist", prefixes=PRE))
async def fban_list(_, m: types.Message):
    cfg = await db.get_chat(m.chat.id)
    fid = str(cfg.get("fed_id") or "")
    fed = (await db.get_feds()).get(fid) if fid else None
    if not fed:
        await _reply(m, "Info: This group is not in a federation.")
        return
    banned = fed.get("banned", [])
    if not banned:
        await _reply(m, "No fed-banned users.")
        return
    lines = [f"• <code>{uid}</code>" for uid in banned[:50]]
    extra = f"\n… and {len(banned) - 50} more." if len(banned) > 50 else ""
    await _reply(m, f"<b>Fed-banned ({len(banned)}):</b>\n" + "\n".join(lines) + extra)


@app.on_message(filters.group & filters.command("fedreason", prefixes=PRE))
async def fed_reason(_, m: types.Message):
    fid, fed = await _owner_fed(m)
    if not fid:
        return
    reason = " ".join(m.command[1:]).strip()

    def _set(f):
        f["reason"] = reason

    await db.mutate_fed(fid, _set)
    if reason:
        await _reply(m, f"Default fban reason set: <i>{html.escape(reason)}</i>")
    else:
        await _reply(m, "Done: Default fban reason cleared.")


@app.on_message(filters.group & filters.command(["fednotif", "quietfed"], prefixes=PRE))
async def fed_toggle(_, m: types.Message):
    fid, fed = await _owner_fed(m)
    if not fid:
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg not in ("on", "off"):
        await _reply(m, f"Usage: <code>/{m.command[0]} on|off</code>")
        return
    key = "notif" if m.command[0].lower() == "fednotif" else "quiet"

    def _set(f):
        f[key] = (arg == "on")

    await db.mutate_fed(fid, _set)
    label = "Fban announcements" if key == "notif" else "Quiet join-bans"
    await _reply(m, f"{label} <b>{arg.upper()}</b>.")


@app.on_message(filters.group & filters.command(["setfedlog", "unsetfedlog"], prefixes=PRE))
async def fed_log(_, m: types.Message):
    fid, fed = await _owner_fed(m)
    if not fid:
        return
    if m.command[0].lower() == "unsetfedlog":
        await db.mutate_fed(fid, lambda f: f.__setitem__("log_chat", 0))
        await _reply(m, "Done: Federation log channel removed.")
        return
    if len(m.command) < 2:
        await _reply(m, "Usage: <code>/setfedlog &lt;@channel|id&gt;</code> (add me there as admin)")
        return
    raw = m.command[1].strip()
    try:
        target = int(raw) if raw.lstrip("-").isdigit() else raw
        probe = await app.send_message(target, "Federation log channel linked.")
        fed_log = probe.chat.id
    except Exception:
        await _reply(m, "I couldn't message that chat — add me there as admin first.")
        return

    def _set(f):
        f["log_chat"] = fed_log

    await db.mutate_fed(fid, _set)
    await _reply(m, f"Federation logs will be posted to <code>{fed_log}</code>.")


@app.on_message(filters.group & filters.command("importfbans", prefixes=PRE))
async def import_fbans(_, m: types.Message):
    fid, fed = await _owner_fed(m)
    if not fid:
        return
    doc = m.reply_to_message and getattr(m.reply_to_message, "document", None)
    if not doc:
        await _reply(m, "Reply to a <b>.txt</b> file with one user id per line.")
        return
    # review 2026-09-06: validate size BEFORE downloading — the id cap does
    # not bound the RAM the download itself consumes
    if int(getattr(doc, "file_size", 0) or 0) > 256 * 1024:
        await _reply(m, "File too large — keep ban lists under 256 KB.")
        return
    try:
        bio = await m.reply_to_message.download(in_memory=True)
        bio.seek(0)
        raw = bio.read().decode("utf-8", "ignore")
    except Exception:
        await _reply(m, "Could not download that file.")
        return
    ids: set[int] = set()
    for tok in raw.replace(",", " ").split():
        if tok.isdigit():
            ids.add(int(tok))
        if len(ids) >= 5000:  # hard cap — a hostile file must not bloat the DB
            break
    if not ids:
        await _reply(m, "No user ids found in that file.")
        return
    added: dict = {}

    def _import(f):
        have = set(f.get("banned", []))
        room = max(0, 10000 - len(have))  # durable cap on total fed bans
        new = [i for i in ids if i not in have][:room]
        f.setdefault("banned", []).extend(new)
        added["n"] = len(new)
        added["dupes"] = len(ids) - len(new)

    await db.mutate_fed(fid, _import)
    await _reply(m, f"Imported <b>{added.get('n', 0)}</b> new fed-bans "
                    f"({added.get('dupes', 0)} duplicates/over-cap ignored). "
                    "They'll be banned on join.")


@app.on_message(filters.group & filters.command("subfed", prefixes=PRE))
async def sub_fed(_, m: types.Message):
    fid, fed = await _owner_fed(m)
    if not fid:
        return
    if len(m.command) < 2:
        await _reply(m, "Usage: <code>/subfed &lt;fed id&gt;</code> — inherit that fed's bans.")
        return
    target = m.command[1].strip().lower()
    if target == fid:
        await _reply(m, "A fed cannot subscribe to itself.")
        return
    feds = await db.get_feds()
    if target not in feds:
        await _reply(m, "No federation with that id.")
        return

    def _add(f):
        f.setdefault("subs", [])
        if target not in f["subs"]:
            f["subs"].append(target)

    await db.mutate_fed(fid, _add)
    await _reply(m, f"Subscribed to <b>{html.escape(feds[target].get('name', ''))}</b> "
                    "— its banned users are now banned here too.")


@app.on_message(filters.group & filters.command("unsubfed", prefixes=PRE))
async def unsub_fed(_, m: types.Message):
    fid, fed = await _owner_fed(m)
    if not fid:
        return
    target = m.command[1].strip().lower() if len(m.command) > 1 else ""

    def _rm(f):
        if target:
            if target in f.get("subs", []):
                f["subs"].remove(target)
        else:
            f["subs"] = []

    await db.mutate_fed(fid, _rm)
    await _reply(m, "Subscription removed." if target else "All subscriptions removed.")


@app.on_message(filters.group & filters.command("fedsubs", prefixes=PRE))
async def fed_subs(_, m: types.Message):
    cfg = await db.get_chat(m.chat.id)
    fid = str(cfg.get("fed_id") or "")
    fed = (await db.get_feds()).get(fid) if fid else None
    if not fed:
        await _reply(m, "Info: This group is not in a federation.")
        return
    feds = await db.get_feds()
    subs = fed.get("subs", [])
    if not subs:
        await _reply(m, "Info: No subscriptions. Use <code>/subfed &lt;fed id&gt;</code>.")
        return
    lines = [
        f"• <b>{html.escape((feds.get(s) or {}).get('name', '?'))}</b> — <code>{s}</code>"
        for s in subs
    ]
    await _reply(m, "<b>Subscribed feds:</b>\n" + "\n".join(lines))
