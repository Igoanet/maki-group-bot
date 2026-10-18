"""Promote levels — grant admin rights in tiers, with an adjustable base set.

/promote        — reply or @user: promote with the chat's configured rights
                  (default: manage VC + pin + add members + edit member tags)
/fullpromote    — same set + the "Add admins" right
/promotepowers  — inline panel to toggle which rights /promote grants

Per-chat state: promote_rights (list of RIGHTS keys) — see database.DEFAULTS.

Authorization model (code-review 2026-09-06):
- Only the owner or an admin holding "Add admins" may promote or edit the
  power pool.
- Telegram authorizes EditAdmin as the BOT, so a caller could otherwise grant
  rights they don't hold via the bot's wider rights. Every granted right is
  therefore intersected with BOTH the bot's and the caller's live privileges
  (owner callers are only limited by the bot's rights).
- Promoting an already-admin target is ADDITIVE: rights they already hold are
  preserved, not silently replaced by the tier set.
- If nothing can be granted (bot/caller lack every selected right), no API
  call is made at all — an empty ChatPrivileges would demote the target.
"""

import html
import inspect
import logging

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.database import db
from ghelp.helpers import ikb, ikm

logger = logging.getLogger(__name__)

# storage key -> (ChatPrivileges attribute, button label)
RIGHTS = {
    "invite_users":       ("can_invite_users", "Add members"),
    "pin_messages":       ("can_pin_messages", "Pin messages"),
    "manage_video_chats": ("can_manage_video_chats", "Manage VC"),
    "manage_tags":        ("can_manage_tags", "Edit member tags"),
    "delete_messages":    ("can_delete_messages", "Delete messages"),
    "restrict_members":   ("can_restrict_members", "Ban users"),
    "change_info":        ("can_change_info", "Change group info"),
    "manage_topics":      ("can_manage_topics", "Manage topics"),
}
_PROMOTE_ATTR = "can_promote_members"  # /fullpromote extra — not toggleable

# every privilege field this fork supports — the additive merge must preserve
# ALL of an existing admin's rights (incl. can_promote_members, anonymity,
# stories…), not just the toggleable tier map (code-review 2026-09-06)
_ALL_ATTRS = tuple(p for p in inspect.signature(types.ChatPrivileges.__init__).parameters
                   if p != "self")
_ATTR_LABEL = {attr: label for attr, label in RIGHTS.values()}


async def _resolve_user(m: types.Message, arg: str | None):
    """Reply target wins; else parse @username / numeric id."""
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user
    if not arg:
        return None
    try:
        return await app.get_users(int(arg) if arg.lstrip("-").isdigit() else arg.lstrip("@"))
    except Exception:
        return None


async def _member_priv(chat_id: int, user_id: int):
    """(privileges|None, is_owner) for a chat member; (None, False) on error."""
    try:
        member = await app.get_chat_member(chat_id, user_id)
        if member.status == enums.ChatMemberStatus.OWNER:
            return None, True
        return getattr(member, "privileges", None), False
    except Exception:
        return None, False


async def _is_promoter(chat_id: int, user_id: int) -> bool:
    """Owner, or an admin holding 'Add admins'."""
    priv, owner = await _member_priv(chat_id, user_id)
    if owner:
        return True
    return bool(priv and getattr(priv, _PROMOTE_ATTR, False))


async def _bot_privileges(chat_id: int):
    try:
        me = await app.get_chat_member(chat_id, (await app.get_me()).id)
        return getattr(me, "privileges", None)
    except Exception:
        return None


def _powers_keyboard(selected: set) -> types.InlineKeyboardMarkup:
    keys = list(RIGHTS)
    rows = []
    for i in range(0, len(keys), 2):
        row = []
        for k in keys[i:i + 2]:
            mark = "Selected" if k in selected else "Not selected"
            row.append(ikb(f"{mark} {RIGHTS[k][1]}",
                           callback_data=f"gh:pr:t:{k}",
                           style=enums.ButtonStyle.PRIMARY))
        rows.append(row)
    rows.append([ikb("Done", callback_data="gh:pr:done",
                     style=enums.ButtonStyle.SUCCESS)])
    return ikm(rows)


@app.on_message(filters.group & filters.command(["promote", "fullpromote"], prefixes=["/", "?", "!", "."]))
async def promote_command(_, m: types.Message) -> None:
    # Normalise the command list ONCE, before ANY access (owner 2026-10-18).
    # Pyrogram derives ``command`` from the sender's identity and leaves it
    # None for an anonymous admin, so the very first ``m.command[1:]`` raised
    # 'NoneType' object is not subscriptable -- surfacing as "Could not
    # promote".  Patching individual access sites missed the editor branch at
    # the top of this function; doing it here covers every branch at once.
    if not getattr(m, "command", None):
        _body = (m.text or m.caption or "").strip()
        _parts = (_body[1:].split()
                  if _body[:1] in ("/", "!", "?", ".") else [])
        m.command = ([_parts[0].split("@", 1)[0]] + _parts[1:]) if _parts else []
        logger.info("promote: command rebuilt as %r", m.command[:4])
    try:
        caller_priv = caller_owner = None
        if m.from_user:
            caller_priv, caller_owner = await _member_priv(m.chat.id, m.from_user.id)
        if not caller_owner and not (caller_priv and getattr(caller_priv, _PROMOTE_ATTR, False)):
            await m.reply_text(
                "<b>Only an admin with the 'Add admins' right can promote.</b>",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        # ── /promote editor ──────────────────────────────────────────────
        # Grants the Edit Guardian EDITOR role instead of Telegram admin rights:
        # an editor may use /editfree on other members.  The caller guard
        # above already limits this to the owner or an admin with "Add
        # admins", so the power stays with people trusted to promote.
        _raw = [str(a) for a in list(m.command[1:])]
        _low = [a.lower() for a in _raw]
        if _low and _low[0] == "editor":
            _off = any(a in ("off", "remove", "none", "no") for a in _low[1:])
            _name = next((_raw[i + 1] for i, a in enumerate(_low[1:])
                          if a not in ("off", "remove", "none", "no")), None)
            try:
                _target = await _resolve_user(m, _name)
            except Exception:
                _target = None
            if _target is None:
                _reply = getattr(m, "reply_to_message", None)
                _target = getattr(_reply, "from_user", None) if _reply is not None else None
            if _target is None:
                await m.reply_text(
                    "<b>Reply to the member</b> you want as an editor, then "
                    "send <code>/editor</code>.",
                    parse_mode=enums.ParseMode.HTML,
                )
                return
            from ghelp.plugins.exempt import grant_editor, revoke_editor
            if _off:
                _ok = await revoke_editor(m.chat.id, _target.id)
            else:
                _ok = await grant_editor(m.chat.id, _target.id)
            _link = ('<a href="tg://user?id=%s">%s</a>'
                     % (_target.id, html.escape(_target.first_name or "user")))
            await m.reply_text(
                ("\u2705 " + _link + (" is no longer an editor." if _off
                 else " is now an editor \u2014 they can use /editfree."))
                if _ok else "<b>I couldn't save that.</b>",
                parse_mode=enums.ParseMode.HTML,
            )
            return
                # ``command`` can be absent on a replayed/anonymous message: read the
        # text directly so a missing parse never becomes a crash.
        _cmds = getattr(m, "command", None) or []
        if not _cmds:
            _body = (m.text or m.caption or "").strip()
            _parts = _body[1:].split() if _body[:1] in ("/", "!", "?", ".") else []
            _cmds = [_parts[0].split("@", 1)[0]] + _parts[1:] if _parts else []
            m.command = _cmds
        target = await _resolve_user(m, _cmds[1] if len(_cmds) > 1 else None)
        if target is None:
            await m.reply_text(
                "<b>Usage:</b> reply to the user's message or pass "
                "<code>@username</code> / numeric id.",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        if target.is_bot:
            await m.reply_text("<b>Bots can't be promoted with this command.</b>",
                               parse_mode=enums.ParseMode.HTML)
            return
        full = (m.command[0].lower() if m.command else "") == "fullpromote"
        cfg = await db.get_chat(m.chat.id)
        selected = [k for k in (cfg or {}).get("promote_rights", []) if k in RIGHTS]
        if not selected and not full:
            # /fullpromote still works with an empty pool — it only needs the
            # 'Add admins' extra, which the guard below checks separately
            await m.reply_text(
                "<b>No powers selected.</b> Pick some via /promotepowers first "
                "(or use /fullpromote, which only needs 'Add admins').",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        bot_priv = await _bot_privileges(m.chat.id)
        if not bot_priv or not getattr(bot_priv, _PROMOTE_ATTR, False):
            await m.reply_text(
                "<b>I need the 'Add admins' right to promote anyone.</b>",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        def _allowed(attr: str) -> bool:
            # the bot grants as itself — so every right must be held by the
            # bot AND (unless the caller owns the chat) by the caller, or a
            # low-rights admin could promote through the bot's wider rights
            if not getattr(bot_priv, attr, False):
                return False
            if caller_owner:
                return True
            return bool(caller_priv and getattr(caller_priv, attr, False))

        granted, skipped = [], []
        for k in selected:
            attr, label = RIGHTS[k]
            (granted if _allowed(attr) else skipped).append((attr, label))
        if full and not _allowed(_PROMOTE_ATTR):
            await m.reply_text(
                "<b>Full promotion requires 'Add admins'</b> from both "
                "the bot and the promoting admin.",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        if not granted and not full:
            # empty privileges would DEMOTE the target — never call Telegram
            await m.reply_text(
                "<b>Nothing to grant</b> — the selected powers are missing "
                "from my rights or yours.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        kwargs = {attr: True for attr, _ in granted}
        if full:
            kwargs[_PROMOTE_ATTR] = True
        # additive for existing admins: preserve EVERY right they currently
        # hold (not just tier rights), then OR the tier on top. A bot can only
        # re-grant rights it holds itself — anything else is dropped + reported.
        kept, dropped = [], []
        cur_priv, cur_owner = await _member_priv(m.chat.id, target.id)
        if cur_priv and not cur_owner:
            for attr in _ALL_ATTRS:
                if attr in kwargs or not getattr(cur_priv, attr, False):
                    continue
                label = _ATTR_LABEL.get(attr, attr.replace("can_", "").replace("_", " "))
                keepable = bool(getattr(bot_priv, attr, False))
                if keepable:
                    kwargs[attr] = True
                    kept.append(label)
                else:
                    dropped.append(label)

        await app.promote_chat_member(m.chat.id, target.id, types.ChatPrivileges(**kwargs))
        name = html.escape(target.first_name or "user")
        text = (
            f"<a href=\"tg://user?id={target.id}\">{name}</a> "
            f"{'fully promoted' if full else 'promoted'} — "
            + ", ".join(label for _, label in granted)
            + (", <b>Add admins</b>" if full else "")
        )
        if kept:
            text += "\nExisting rights kept: " + ", ".join(kept)
        if dropped:
            text += ("\nCouldn't keep (I don't hold them): " + ", ".join(dropped))
        if skipped:
            text += ("\nSkipped (missing from my rights or yours): "
                     + ", ".join(label for _, label in skipped))
        await m.reply_text(text, parse_mode=enums.ParseMode.HTML)
    except Exception as exc:
        # Full traceback: the one-line message hid the real cause for hours
        # and led to several wrong fixes (owner 2026-10-18).
        logger.exception("promote failed in chat %s", getattr(m.chat, "id", "?"))
        await m.reply_text(
            "<b>Could not promote.</b> Check that the user is a member of "
            "this group and that my admin rights cover the powers.",
            parse_mode=enums.ParseMode.HTML,
        )


@app.on_message(filters.group & filters.command("promotepowers", prefixes=["/", "?", "!", "."]))
async def promotepowers_command(_, m: types.Message) -> None:
    # pool decides what promoting hands out — same gate as promoting itself
    if not m.from_user or not await _is_promoter(m.chat.id, m.from_user.id):
        await m.reply_text(
            "<b>Only an admin with the 'Add admins' right can set promote powers.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    try:
        cfg = await db.get_chat(m.chat.id)
        await m.reply_text(
            "<b>Promote powers</b>\n"
            "Choose the rights granted by <code>/promote</code>.\n"
            "<code>/fullpromote</code> also grants <b>Add admins</b>.\n"
            "<i>Each right requires both the bot and the promoting admin to hold it.</i>",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_powers_keyboard(set(cfg["promote_rights"])),
        )
    except Exception:
        await m.reply_text("<b>Could not open the powers panel.</b>",
                           parse_mode=enums.ParseMode.HTML)


@app.on_callback_query(filters.regex(r"^gh:pr:(t:[a-z_]+|done)$"))
async def promotepowers_callback(_, q: types.CallbackQuery) -> None:
    try:
        if not q.message or not q.from_user:
            await q.answer()
            return
        if not await _is_promoter(q.message.chat.id, q.from_user.id):
            await q.answer("Only admins with 'Add admins' can change this.",
                           show_alert=True)
            return
        if q.data == "gh:pr:done":
            cfg = await db.get_chat(q.message.chat.id)
            sel = [RIGHTS[k][1] for k in cfg["promote_rights"] if k in RIGHTS]
            await q.message.edit_text(
                "<b>Promote powers saved.</b>\n"
                "<code>/promote</code> grants: "
                + (", ".join(sel) if sel else "<i>nothing yet — /promotepowers</i>")
                + "\n<code>/fullpromote</code> adds <b>Add admins</b> on top.",
                parse_mode=enums.ParseMode.HTML,
            )
            await q.answer("Saved")
            return
        key = q.data.rsplit(":", 1)[-1]
        if key not in RIGHTS:
            await q.answer()
            return
        outcome: dict = {}

        def _apply(cfg, _k=key, _out=outcome):
            # under the db lock — concurrent toggles can't clobber each other
            rights = cfg["promote_rights"]
            if _k in rights:
                rights.remove(_k)
                _out["on"] = False
            else:
                rights.append(_k)
                _out["on"] = True

        cfg = await db.mutate_chat(q.message.chat.id, _apply)
        await q.message.edit_reply_markup(_powers_keyboard(set(cfg["promote_rights"])))
        await q.answer(f"{RIGHTS[key][1]}: {'ON' if outcome['on'] else 'OFF'}")
    except Exception as exc:
        logger.info("promotepowers callback: %s", exc)
        try:
            await q.answer("Could not update.", show_alert=True)
        except Exception:
            pass
