"""Rose-style word blacklist with action modes (del/warn/kick/mute/ban).

Distinct from Shield's blockword (always delete-only, attack-focused):
this is the classic admin-configured word filter with a chosen punishment.
"""

import html
import logging
import re
from datetime import datetime, timedelta

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.approval_trust import is_exempt
from ghelp.selfdel import autodel_reply
from ghelp.database import db
from ghelp.helpers import concise_bot_text, is_admin, mention, require_admin, safe_restrict
from ghelp.plugins.admin import _muted_permissions, _name, apply_warn
from ghelp.plugins.logchan import log_event

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]
_MODES = ("del", "warn", "kick", "mute", "ban")
# actions that restrict a member rather than just deleting their message
_PUNITIVE_MODES = {"warn", "kick", "mute", "ban"}


@autodel_reply
async def _reply(message: types.Message, text: str, **kwargs):
    return await message.reply_text(
        concise_bot_text(text), parse_mode=enums.ParseMode.HTML, **kwargs)


def _phrase_from(message: types.Message) -> str:
    """The phrase to (un)blacklist: command args, else the replied text."""
    if len(message.command) > 1:
        return " ".join(message.command[1:]).strip().lower()
    rt = message.reply_to_message
    if rt and (rt.text or rt.caption):
        return (rt.text or rt.caption).strip().lower()
    return ""


def _matches(text: str, phrase: str) -> bool:
    """Match Rose's exact/prefix/lookalike and wildcard forms."""
    text = text or ""
    phrase = phrase or ""
    mode = "contains"
    for prefix in ("exact:", "prefix:", "lookalike:"):
        if phrase.startswith(prefix):
            mode, phrase = prefix[:-1], phrase[len(prefix):]
            break
    if mode == "lookalike":
        # The common Cyrillic homoglyphs are intentionally narrow: this is
        # safer than silently normalising arbitrary scripts.
        table = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p",
                               "с": "c", "х": "x", "у": "y", "в": "b",
                               "і": "i", "ј": "j"})
        text, phrase = text.casefold().translate(table), phrase.casefold()
    else:
        text, phrase = text.casefold(), phrase.casefold()
    if "?" not in phrase and "*" not in phrase:
        if mode == "exact":
            return text.strip() == phrase.strip()
        if mode == "prefix":
            return text.startswith(phrase)
        return phrase in text
    pattern = re.escape(phrase).replace(r"\*\*", r"[\s\S]*").replace(
        r"\*", r"\S*").replace(r"\?", r"\S")
    if mode == "exact":
        return bool(re.fullmatch(pattern, text))
    if mode == "prefix":
        return bool(re.match(pattern, text))
    return bool(re.search(pattern, text, re.IGNORECASE))


def _entry_matches(message: types.Message, phrase: str) -> bool:
    """Match text entries and documented metadata entries."""
    text = message.text or message.caption or ""
    lower = phrase.casefold()
    if lower.startswith("file:"):
        pattern = lower[5:]
        files = [getattr(getattr(message, name, None), "file_name", "") or ""
                 for name in ("document", "video", "audio")]
        return any(_matches(name.casefold(), pattern) for name in files)
    if lower.startswith("inline:"):
        bot = getattr(message, "via_bot", None)
        username = getattr(bot, "username", "") if bot else ""
        return _matches("@" + username.casefold(), lower[7:])
    if lower.startswith("forward:"):
        origin = getattr(message, "forward_from_chat", None)
        username = getattr(origin, "username", "") if origin else ""
        return _matches("@" + username.casefold(), lower[8:])
    if lower.startswith("stickerpack:"):
        sticker = getattr(message, "sticker", None)
        pack = getattr(sticker, "set_name", "") if sticker else ""
        return bool(pack) and (lower == "stickerpack:<>" or
                               _matches(pack.casefold(), lower[12:]))
    if lower.startswith("emoji:"):
        return lower[6:] in text.casefold()
    return _matches(text, phrase)


async def _punitive_mode_allowed(message: types.Message) -> bool:
    """Ban users is also needed while the effective action punishes members."""
    try:
        cfg = await db.get_chat(message.chat.id)
    except Exception:
        cfg = {}
    if str(cfg.get("bl_mode") or "del") not in _PUNITIVE_MODES:
        return True
    return await require_admin(message, "can_restrict_members", "Ban users")


@app.on_message(filters.group & filters.command("addblacklist", prefixes=PRE))
async def add_blacklist(_, message: types.Message):
    if not await require_admin(message, "can_delete_messages", "Delete messages"):
        return
    # A new trigger inherits the group's blacklist mode, so while that mode
    # punishes members, adding one is a restrict action too.
    if not await _punitive_mode_allowed(message):
        return
    phrase = _phrase_from(message)
    reason = ""
    if " ### " in phrase:
        phrase, reason = (part.strip() for part in phrase.split(" ### ", 1))
    if not phrase:
        await _reply(message, "Usage: <code>/addblacklist &lt;word&gt;</code> (or reply to a message).")
        return
    outcome: dict = {}

    def _apply(cfg):
        if phrase not in cfg["blacklist"]:
            cfg["blacklist"].append(phrase)
            # A plain /addblacklist entry follows the group mode; drop any
            # per-entry action left behind by an earlier /addblocklist so a
            # stale ban/mute cannot come back with the word.
            cfg.setdefault("bl_entries", {}).pop(phrase, None)
            if reason:
                cfg.setdefault("bl_reasons", {})[phrase] = reason
            outcome["changed"] = True

    await db.mutate_chat(message.chat.id, _apply)
    if outcome.get("changed"):
        await _reply(message, f"Blacklisted: <code>{html.escape(phrase)}</code>")
    else:
        await _reply(message, "Info: Already blacklisted.")


@app.on_message(filters.group & filters.command("unblacklist", prefixes=PRE))
async def del_blacklist(_, message: types.Message):
    if not await require_admin(message, "can_delete_messages", "Delete messages"):
        return
    phrase = _phrase_from(message)
    if not phrase:
        await _reply(message, "Usage: <code>/unblacklist &lt;word&gt;</code> (or reply to a message).")
        return
    outcome: dict = {}

    def _apply(cfg):
        if phrase in cfg["blacklist"]:
            cfg["blacklist"].remove(phrase)
            cfg.setdefault("bl_reasons", {}).pop(phrase, None)
            # remove the rule completely, including any stored action
            cfg.setdefault("bl_entries", {}).pop(phrase, None)
            outcome["changed"] = True

    await db.mutate_chat(message.chat.id, _apply)
    if outcome.get("changed"):
        await _reply(message, f"Done: Removed from blacklist: <code>{html.escape(phrase)}</code>")
    else:
        await _reply(message, "Info: That word is not blacklisted.")


@app.on_message(filters.group & filters.command("blacklists", prefixes=PRE))
async def list_blacklist(_, message: types.Message):
    if not await require_admin(message, "can_delete_messages", "Delete messages"):
        return
    cfg = await db.get_chat(message.chat.id)
    words = cfg["blacklist"]
    if not words:
        await _reply(message, "Info: No blacklisted words here.")
        return
    lines = "\n".join(f"• <code>{html.escape(w)}</code>" for w in words[:100])
    await _reply(
        message,
        f"<b>Blacklisted words ({len(words)}):</b>\n{lines}\n\n"
        f"Mode: <b>{cfg['bl_mode']}</b>",
    )


@app.on_message(filters.group & filters.command(
    ["blacklistmode", "blocklistmode"], prefixes=PRE))
async def blacklist_mode(_, message: types.Message):
    if not await require_admin(message, "can_delete_messages", "Delete messages"):
        return
    arg = message.command[1].lower() if len(message.command) > 1 else ""
    if arg in _PUNITIVE_MODES and not await require_admin(
        message, "can_restrict_members", "Ban users"
    ):
        # warn/kick/mute/ban make the bot restrict members
        return
    if arg not in _MODES:
        await _reply(
            message,
            "Usage: <code>/blacklistmode del|warn|kick|mute|ban</code>",
        )
        return
    await db.update_chat(message.chat.id, bl_mode=arg)
    await _reply(message, f"Blacklist action set to <b>{arg}</b>.")


@app.on_message(filters.group & ~filters.service, group=7)
async def blacklist_watcher(_, message: types.Message):
    try:
        sender = message.from_user
        if not sender or sender.is_self:
            return
        cfg = await db.get_chat(message.chat.id)
        words = cfg["blacklist"]
        text = (message.text or message.caption or "").lower()
        if not words or not text:
            return
        hit = next((w for w in words if _entry_matches(message, w)), None)
        if not hit:
            return
        if await is_admin(message.chat.id, sender.id):
            return  # admins are trusted (live check — destructive path)
        # Role checks are awaited, so load approval state again immediately
        # before taking the automatic action.  /approve must win over a stale
        # watcher snapshot; approval never changes manual moderation commands.
        current = await db.get_chat(message.chat.id)
        if is_exempt(current, sender.id, "blacklist_words"):
            return
        words = current.get("blacklist") or []
        text = (message.text or message.caption or "").lower()
        hit = next((w for w in words if _entry_matches(message, w)), None)
        if not hit:
            return
        cfg = current
        # every enforcement input comes from this post-role-check snapshot, so
        # a rule edited while the lookup was awaited cannot punish anyone
        entry = (cfg.get("bl_entries") or {}).get(hit) or {}
        mode = entry.get("action") or cfg["bl_mode"]
        if mode.startswith("tmute"):
            mode = "mute"
        delete = entry.get("delete", cfg.get("bl_del", True))
        if mode == "warn":
            # warn first — the reply must land before the message is gone
            await apply_warn(message, sender, f"blacklisted word: {hit}")
            try:
                await message.delete()
            except Exception:
                pass
        else:
            if not delete:
                # /blocklistdelete off — keep the message, warn in chat instead
                reason = str(entry.get("reason") or (cfg.get("bl_reasons") or {}).get(hit)
                             or cfg.get("bl_reason") or "")
                await _reply(
                    message,
                    f"{_name(sender)}, that word is blacklisted here."
                    + (f"\n<b>Reason:</b> {html.escape(reason)}" if reason else ""),
                )
            else:
                try:
                    await message.delete()
                except Exception:
                    return  # cannot delete -> acting further would confuse
            if mode == "kick":
                await app.ban_chat_member(message.chat.id, sender.id)
                await app.unban_chat_member(message.chat.id, sender.id)
            elif mode == "mute":
                duration = entry.get("duration")
                if duration:
                    try:
                        await app.restrict_chat_member(
                            message.chat.id, sender.id, _muted_permissions(),
                            until_date=datetime.now() + timedelta(seconds=int(duration)),
                        )
                    except Exception:
                        return
                elif not await safe_restrict(message.chat.id, sender.id, _muted_permissions()):
                    return
            elif mode == "ban":
                await app.ban_chat_member(message.chat.id, sender.id)
            if mode != "del":
                word = {"kick": "kicked", "mute": "muted", "ban": "banned"}[mode]
                await _reply(message, f"{_name(sender)} was <b>{word}</b> — blacklisted word.")
        who = html.escape(getattr(sender, "first_name", None) or "user")
        bl_reason = str(entry.get("reason") or (cfg.get("bl_reasons") or {}).get(hit)
                        or cfg.get("bl_reason") or "")
        await log_event(
            message.chat.id,
            f"Blacklist ({mode}): {who} — word <code>{html.escape(hit)}</code>"
            + (f" — {html.escape(bl_reason)}" if bl_reason else ""),
        )
    except Exception as exc:
        logger.info("blacklist watcher: %s", exc)
