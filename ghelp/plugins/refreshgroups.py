"""Owner-only /refreshgroups — bots cannot call get_dialogs, so this verifies
the stored chat ids (and any ids passed as arguments) against live Telegram."""
from pyrogram import enums, filters, types
from ghelp import app
from ghelp.config import config
from ghelp.database import db

_PRE = ["/", "!", "?", "."]
_R = ("can_delete_messages","can_restrict_members","can_invite_users","can_pin_messages",
      "can_change_info","can_promote_members","can_manage_video_chats","can_manage_topics")


@app.on_message(filters.command(["refreshgroups","groupsync"], prefixes=_PRE))
async def refresh_groups(_, m: types.Message):
    if not m.from_user or m.from_user.id != config.OWNER_ID:
        return
    if m.chat and m.chat.type != enums.ChatType.PRIVATE:
        try: await m.delete()
        except Exception: pass
    stored = await db.get("known_chats", {}) or {}
    ids = {int(k) for k in stored}
    for a in m.command[1:]:
        if a.lstrip("-").isdigit():
            ids.add(int(a))
    note = await app.send_message(m.from_user.id, f"Verifying {len(ids)} chat ids…")
    me = await app.get_me()
    recs, bad = {}, []
    for cid in sorted(ids):
        try:
            ch = await app.get_chat(cid)
            mem = await app.get_chat_member(cid, me.id)
            st = getattr(mem.status, "value", str(mem.status)); pr = getattr(mem, "privileges", None)
            recs[str(cid)] = {"title": ch.title or str(cid),
                              "admin": st in ("administrator","creator","owner"),
                              "status": st, "rights": [k for k in _R if pr and getattr(pr, k, False)],
                              "can_ban": bool(pr and getattr(pr, "can_restrict_members", False))}
        except Exception as e:
            bad.append((cid, type(e).__name__))
    await db.set("known_chats", recs)
    adm = [v for v in recs.values() if v["admin"]]
    lines = [f"{i}. <b>{v['title']}</b> <code>{c}</code> · " +
             ("ban" if v.get("can_ban") else "no-ban") + f" · {len(v['rights'])}/8"
             for i, (c, v) in enumerate(sorted(recs.items(), key=lambda x: (not x[1]["admin"], x[1]["title"])), 1)]
    await note.edit_text(
        f"<b>Groups refreshed</b>\nAdmin: <b>{len(adm)}</b> · member-only: "
        f"<b>{len(recs)-len(adm)}</b> · unreachable: <b>{len(bad)}</b>\n\n"
        + "\n".join(lines[:40]), parse_mode=enums.ParseMode.HTML)
