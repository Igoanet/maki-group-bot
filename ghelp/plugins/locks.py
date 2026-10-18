"""Per-chat content locks and enforcement."""

import asyncio
import html
import time

from pyrogram import StopPropagation, enums, filters, raw, types, utils

from ghelp import app
from ghelp.approval_trust import is_exempt
import hashlib
import json
from collections import OrderedDict

from ghelp.selfdel import autodel_reply
from ghelp.database import db
from ghelp.helpers import has_right, concise_bot_text, ikb, ikm, is_admin, require_admin, safe_restrict
from ghelp.plugins.admin import _muted_permissions, apply_warn


LOCK_TYPES = (
    "all", "album", "anonchannel", "url", "botlink", "sticker",
    "stickeranimated", "stickerpremium", "forward", "forwardbot",
    "forwardchannel", "forwardstory", "forwarduser", "externalreply",
    "photo", "video", "videonote", "gif", "document", "audio", "voice",
    "contact", "location", "poll", "game", "emojigame", "button", "text",
    "command", "bot", "bot_inline", "inline", "guestbot", "phone", "email",
    "invitelink", "cashtag", "emoji", "emojicustom", "emojionly", "cyrillic",
    "cjk", "rtl", "spoiler", "checklist", "richmessage", "collage",
    "slideshow", "comment", "reaction", "outsidereaction", "zalgo",
)
_MEDIA_LOCKS = {"photo", "video", "document", "audio", "voice"}
LOCK_ALIASES = {
    "inline": "bot_inline", "bot": "bot_inline", "stickeranimated": "stickeranimated",
    "animatedsticker": "stickeranimated", "premiumsticker": "stickerpremium",
    "videonote": "videonote", "video_note": "videonote",
}
_LOCK_ACTIONS = {"del", "warn", "kick", "mute", "ban"}
# these lock actions restrict the member instead of only deleting the message
_PUNITIVE_LOCK_ACTIONS = {"warn", "kick", "mute", "ban"}
_notices: dict[int, float] = {}


@autodel_reply
async def _reply(message: types.Message, text: str, **kwargs):
    return await message.reply_text(
        concise_bot_text(text), parse_mode=enums.ParseMode.HTML, **kwargs
    )


def _keyboard(locked: set[str]) -> types.InlineKeyboardMarkup:
    buttons = []
    for lock_type in LOCK_TYPES:
        active = lock_type in locked
        buttons.append(
            ikb(
                f"{'Locked' if active else 'Unlocked'} {lock_type}",
                callback_data=f"gh:lock:t:{lock_type}",
                style=(
                    enums.ButtonStyle.DANGER
                    if active
                    else enums.ButtonStyle.PRIMARY
                ),
            )
        )
    return ikm([buttons[i:i + 2] for i in range(0, len(buttons), 2)])


def _requested(message: types.Message) -> list[str]:
    return [LOCK_ALIASES.get(part.lower(), part.lower()) for part in message.command[1:]]


def _lock_spec(message: types.Message):
    """Parse Rose's optional ``### reason {action}`` suffix safely."""
    raw = (message.text or "").split(maxsplit=1)
    args = raw[1] if len(raw) > 1 else ""
    item_text, marker, suffix = args.partition("###")
    requested = [
        LOCK_ALIASES.get(part.lower(), part.lower())
        for part in item_text.split()
    ]
    if not marker:
        return requested, None, None
    suffix = suffix.strip()
    action = "del"
    if suffix.endswith("}") and "{" in suffix:
        reason, action_text = suffix.rsplit("{", 1)
        action = action_text[:-1].strip().lower() or "del"
    else:
        reason = suffix
    return requested, reason.strip(), action


@app.on_message(filters.group & filters.command("lock", prefixes=["/", "?", "!", "."]))
async def lock_content(_, message: types.Message):
    if not await require_admin(message, "can_delete_messages", "Delete messages"):
        return
    requested, reason, action = _lock_spec(message)
    if action in _PUNITIVE_LOCK_ACTIONS and not await require_admin(
        message, "can_restrict_members", "Ban users"
    ):
        return
    if reason is not None and action not in _LOCK_ACTIONS:
        await _reply(message, "Unknown lock action. Use <code>del|warn|kick|mute|ban</code>.")
        return
    if requested == ["all"] and reason is not None:
        await db.update_chat(message.chat.id, lock_actions={})
        await _reply(message, "Custom lock actions and reasons cleared.")
        return
    unknown = [item for item in requested if item not in LOCK_TYPES]
    if not requested or unknown:
        valid = ", ".join(LOCK_TYPES)
        await _reply(message, f"Valid locks: <code>{html.escape(valid)}</code>")
        return
    try:
        settings = await db.get_chat(message.chat.id)
        if reason is None:
            stored = settings.get("lock_actions") or {}
            inherited = [item for item in requested
                         if str((stored.get(item) or {}).get("action"))
                         in _PUNITIVE_LOCK_ACTIONS]
            if inherited and not await require_admin(
                message, "can_restrict_members", "Ban users"
            ):
                # switching these back on would ban/mute members again
                return
        locked = set(settings.get("locks", []))
        locked.update(requested)
        fields = {"locks": sorted(locked)}
        if reason is not None:
            actions = dict(settings.get("lock_actions") or {})
            for item in requested:
                actions[item] = {"reason": reason, "action": action}
            fields["lock_actions"] = actions
        await db.update_chat(message.chat.id, **fields)
        names = ", ".join(html.escape(item) for item in requested)
        await _reply(message, f"Locked: <b>{names}</b>.")
    except Exception:
        await _reply(message, "Could not update content locks.")


# One switch for "no media here unless you are approved".  It drives the
# ordinary lock engine (delete + approval exemption), so nothing about the
# group's Telegram permissions is touched: members keep their media button
# and the bot simply removes the post.
_MEDIA_DELETE_LOCKS = (
    "photo", "video", "videonote", "gif", "document", "audio", "voice",
    "sticker", "stickeranimated", "stickerpremium",
)


def _media_delete_on(settings: dict) -> bool:
    return set(_MEDIA_DELETE_LOCKS) <= set(settings.get("locks", []))


def _media_delete_state(settings: dict) -> str:
    """ON only when every media type is covered; PART when some of them are."""
    locked = set(settings.get("locks", []))
    if set(_MEDIA_DELETE_LOCKS) <= locked:
        return "on"
    return "part" if locked & set(_MEDIA_DELETE_LOCKS) else "off"


@app.on_message(filters.group & filters.command(
    ["mediadelete", "deletemedia"], prefixes=["/", "?", "!", "."]))
async def media_delete(_, message: types.Message):
    if not await require_admin(message, "can_delete_messages",
                               "Delete messages"):
        return
    arg = (message.command[1].lower() if len(message.command) > 1 else "")
    try:
        settings = await db.get_chat(message.chat.id)
        if arg not in {"on", "yes", "off", "no"}:
            state = {
                "on": "ON",
                "part": "PARTLY ON (only some media types are locked)",
                "off": "OFF",
            }[_media_delete_state(settings)]
            await _reply(
                message,
                f"<b>Media delete</b>: <b>{state}</b>\n"
                "Use <code>/mediadelete on</code> to delete every photo, "
                "video, GIF, sticker, document, audio and voice message from "
                "members who are not approved. Approve someone with "
                "<code>/approve</code> (reply to them).",
            )
            return
        locked = set(settings.get("locks", []))
        if arg in {"on", "yes"}:
            locked.update(_MEDIA_DELETE_LOCKS)
            # Delete-only by design: keep any custom reason but force the
            # action back to plain deletion, so this switch can never start
            # banning or muting members.
            actions = {}
            for key, value in (settings.get("lock_actions") or {}).items():
                if key in _MEDIA_DELETE_LOCKS and isinstance(value, dict):
                    value = {**value, "action": "del"}
                actions[key] = value
            await db.update_chat(message.chat.id, locks=sorted(locked),
                                 lock_actions=actions)
            await _reply(
                message,
                "<b>Media delete is ON.</b>\n"
                "Photos, videos, GIFs, stickers, documents, audio and voice "
                "messages are deleted for everyone except admins and approved "
                "members. Nobody is muted or banned for it, even if a media "
                "lock is given a punitive action later.\n"
                "Approve a member: reply <code>/approve</code> · "
                "Switch off: <code>/mediadelete off</code>",
            )
            return
        locked.difference_update(_MEDIA_DELETE_LOCKS)
        await db.update_chat(message.chat.id, locks=sorted(locked))
        await _reply(
            message,
            "<b>Media delete is OFF.</b> Members can post media again.\n"
            "This also clears any media lock set earlier with "
            "<code>/lock photo</code> and friends.",
        )
    except Exception:
        await _reply(message, "Could not update media delete.")


@app.on_message(filters.group & filters.command("unlock", prefixes=["/", "?", "!", "."]))
async def unlock_content(_, message: types.Message):
    if not await require_admin(message, "can_delete_messages", "Delete messages"):
        return
    requested, _, _ = _lock_spec(message)
    unknown = [item for item in requested if item not in LOCK_TYPES]
    if not requested or unknown:
        valid = ", ".join(LOCK_TYPES)
        await _reply(message, f"Valid locks: <code>{html.escape(valid)}</code>")
        return
    try:
        settings = await db.get_chat(message.chat.id)
        locked = set(settings.get("locks", []))
        locked.difference_update(requested)
        await db.update_chat(message.chat.id, locks=sorted(locked))
        names = ", ".join(html.escape(item) for item in requested)
        await _reply(message, f"Unlocked: <b>{names}</b>.")
    except Exception:
        await _reply(message, "Could not update content locks.")


@app.on_message(filters.group & filters.command("locks", prefixes=["/", "?", "!", "."]))
async def show_locks(_, message: types.Message):
    # the panel carries toggle buttons, so it needs the lock right itself
    if not await require_admin(message, "can_delete_messages", "Delete messages"):
        return
    try:
        settings = await db.get_chat(message.chat.id)
        locked = set(settings.get("locks", []))
        await _reply(
            message,
            "<b>Content locks</b>\nTap a type to toggle it.",
            reply_markup=_keyboard(locked),
        )
    except Exception:
        await _reply(message, "Could not load content locks.")


@app.on_callback_query(filters.regex(r"^gh:lock:t:"))
async def toggle_lock(_, callback: types.CallbackQuery):
    message = callback.message
    if not message or not callback.from_user:
        await callback.answer("Unavailable.", show_alert=True)
        return
    if not await has_right(message.chat.id, callback.from_user.id,
                           "can_delete_messages"):
        await callback.answer("You need the Delete messages admin right.",
                              show_alert=True)
        return
    lock_type = callback.data.removeprefix("gh:lock:t:")
    if lock_type not in LOCK_TYPES:
        await callback.answer("Unknown lock.", show_alert=True)
        return
    try:
        settings = await db.get_chat(message.chat.id)
        stored = (settings.get("lock_actions") or {}).get(lock_type) or {}
        if str(stored.get("action")) in _PUNITIVE_LOCK_ACTIONS and (
            not await has_right(message.chat.id, callback.from_user.id,
                                "can_restrict_members")
        ):
            # switching this lock back on would ban/mute members
            await callback.answer(
                "This lock bans or mutes members — you need the Ban users "
                "admin right.", show_alert=True)
            return
        locked = set(settings.get("locks", []))
        if lock_type in locked:
            locked.remove(lock_type)
            answer = f"Unlocked {lock_type}"
        else:
            locked.add(lock_type)
            answer = f"Locked {lock_type}"
        await db.update_chat(message.chat.id, locks=sorted(locked))
        await message.edit_reply_markup(reply_markup=_keyboard(locked))
        await callback.answer(answer)
    except Exception:
        await callback.answer("Could not update lock.", show_alert=True)


def _matches(message: types.Message, lock_type: str) -> bool:
    entities = list(message.entities or []) + list(message.caption_entities or [])
    entity_types = {entity.type for entity in entities}
    text = message.text or message.caption or ""
    if lock_type == "all":
        return True
    if lock_type == "album":
        return bool(getattr(message, "media_group_id", None))
    if lock_type == "url":
        return bool(
            entity_types
            & {enums.MessageEntityType.URL, enums.MessageEntityType.TEXT_LINK}
        )
    if lock_type == "phone":
        return enums.MessageEntityType.PHONE_NUMBER in entity_types
    if lock_type == "email":
        return enums.MessageEntityType.EMAIL in entity_types
    if lock_type == "phone":
        return enums.MessageEntityType.PHONE_NUMBER in entity_types
    if lock_type == "spoiler":
        return enums.MessageEntityType.SPOILER in entity_types
    if lock_type == "emojicustom":
        return getattr(enums.MessageEntityType, "CUSTOM_EMOJI", "custom_emoji") in entity_types
    if lock_type in {"emoji", "emojionly"}:
        import re
        emoji = r"[\U0001F000-\U0001FAFF\u2600-\u27BF]"
        found = re.findall(emoji, text)
        return bool(found) and (
            lock_type == "emoji" or not re.sub(emoji, "", text).strip()
        )
    if lock_type == "emojigame":
        return bool(getattr(message, "dice", None) or getattr(message, "game", None))
    if lock_type == "cashtag":
        import re
        return bool(re.search(r"\$[A-Z][A-Z0-9_]{1,30}\b", text))
    if lock_type in {"invitelink", "botlink"}:
        import re
        pattern = r"(?:t\.me|telegram\.me)/(?:joinchat/|\+|)[\w@-]+"
        if lock_type == "botlink":
            pattern = r"(?:t\.me|telegram\.me)/[A-Za-z0-9_]*bot\b|@\w*bot\b"
        return bool(re.search(pattern, text, re.I))
    if lock_type == "inline":
        lock_type = "bot_inline"
    if lock_type in {"bot", "guestbot"}:
        return bool(
            getattr(message, "new_chat_members", None)
            and any(getattr(u, "is_bot", False)
                    for u in (message.new_chat_members or ()))
        )
    if lock_type in {"forwardbot", "forwardchannel", "forwarduser", "forwardstory"}:
        origin = getattr(message, "forward_from_chat", None)
        user = getattr(message, "forward_from", None)
        if lock_type == "forwardbot":
            return bool(user and getattr(user, "is_bot", False))
        if lock_type == "forwardchannel":
            return bool(origin and getattr(origin, "type", "") == "channel")
        if lock_type == "forwarduser":
            return bool(user)
        return "story" in str(getattr(message, "forward_origin", "")).lower()
    if lock_type == "externalreply":
        reply = getattr(message, "reply_to_message", None)
        return bool(reply and getattr(reply, "chat", None)
                    and reply.chat.id != message.chat.id)
    if lock_type in {"reaction", "outsidereaction"}:
        reactions = getattr(message, "reactions", None)
        return bool(reactions)
    if lock_type in {"checklist", "richmessage", "collage", "slideshow",
                     "comment", "anonchannel", "zalgo"}:
        # These fields are not exposed consistently by Pyrogram. Use only
        # explicit fields when present; never infer a destructive match.
        if lock_type == "comment":
            return bool(getattr(message, "is_automatic_forward", False))
        if lock_type == "anonchannel":
            return bool(getattr(message, "author_signature", None)
                        and not getattr(message, "from_user", None))
        if lock_type == "zalgo":
            import unicodedata
            return sum(unicodedata.combining(c) for c in text) > 8
        return bool(getattr(message, lock_type, None))
    if lock_type == "text":
        return bool(text)
    if lock_type == "command":
        return bool(getattr(message, "command", None)) or text.startswith(("/", "!", "?"))
    if lock_type == "button":
        return bool(getattr(message, "reply_markup", None))
    if lock_type in {"cyrillic", "cjk", "rtl"}:
        patterns = {
            "emoji": r"[\U0001F000-\U0001FAFF\u2600-\u27BF]",
            "cyrillic": r"[\u0400-\u052F]",
            "cjk": r"[\u3040-\u30ff\u3400-\u9fff]",
            "rtl": r"[\u0590-\u08ff]",
        }
        import re
        return bool(re.search(patterns[lock_type], text))
    if lock_type == "forward":
        return bool(
            getattr(message, "forward_date", None)
            or getattr(message, "forward_from_chat", None)
            or getattr(message, "forward_from", None)
            or getattr(message, "forward_origin", None)
        )
    attributes = {
        "sticker": "sticker",
        "photo": "photo",
        "video": "video",
        "gif": "animation",
        "document": "document",
        "audio": "audio",
        "voice": "voice",
        "bot_inline": "via_bot",
        "contact": "contact",
        "location": "location",
        "poll": "poll",
        "game": "game",
        "videonote": "video_note",
    }
    if lock_type == "stickeranimated":
        return bool(getattr(message, "sticker", None) and getattr(message.sticker, "is_animated", False))
    if lock_type == "stickerpremium":
        return bool(getattr(message, "sticker", None) and getattr(message.sticker, "is_premium", False))
    return bool(getattr(message, attributes.get(lock_type, lock_type), None))


def _feature_for_lock(lock_type: str, message: types.Message | None = None) -> str:
    """Map one concrete lock to its independently configurable exemption."""
    if lock_type in ("sticker", "stickeranimated", "stickerpremium"):
        return "stickers"
    if lock_type == "videonote":
        return "media"
    if lock_type == "gif":
        return "gifs"
    if lock_type == "forward":
        return "forwards"
    if lock_type in _MEDIA_LOCKS:
        # A broad media match (including custom/future lock combinations)
        # should still use the narrow sticker/GIF policy when the actual
        # message is one of those media kinds.
        if message is not None:
            if getattr(message, "sticker", None):
                return "stickers"
            if getattr(message, "animation", None):
                return "gifs"
        return "media"
    return "other_locks"


def _url_allowed(message: types.Message, allowed: list) -> bool:
    """True when EVERY url in the message points at an allowlisted domain."""
    if not allowed:
        return False
    text = message.text or message.caption or ""
    ents = list(message.entities or []) + list(message.caption_entities or [])
    domains: set[str] = set()
    for e in ents:
        try:
            if e.type == enums.MessageEntityType.TEXT_LINK and e.url:
                host = e.url.split("://", 1)[-1].split("/", 1)[0]
                domains.add(host.lower())
            elif e.type == enums.MessageEntityType.URL:
                seg = text[e.offset:e.offset + e.length]
                host = seg.split("://", 1)[-1].split("/", 1)[0]
                domains.add(host.lower())
        except Exception:
            return False
    if not domains:
        return False
    ok = {str(a).lower().removeprefix("www.") for a in allowed}
    return all(d.removeprefix("www.") in ok for d in domains)


async def _remove_notice(notice: types.Message):
    await asyncio.sleep(8)
    try:
        await notice.delete()
    except Exception:
        pass


@app.on_message(filters.group & ~filters.service, group=2)
async def enforce_locks(_, message: types.Message):
    try:
        settings = await db.get_chat(message.chat.id)
        link_owned = False
        if settings.get("linkdel_on"):
            from ghelp.link_detection import has_links
            from ghelp.plugins.linkdel import enforce
            if has_links(message, buttons=True):
                link_owned = True
                if await enforce(message, settings):
                    # A deleted link gets one warning, not another blacklist/
                    # flood/shield punishment later in the same dispatcher pass.
                    raise StopPropagation
                # Do not issue a second ordinary URL-lock warning after an
                # unconfirmed Link Deleter attempt. Still let independent
                # flood/Shield/blacklist protections inspect the post.
        if not message.from_user:
            return
        matched = [
            lock_type
            for lock_type in settings.get("locks", [])
            if lock_type in LOCK_TYPES
            and not (link_owned and lock_type == "url")
            and _matches(message, lock_type)
        ]
        if not matched:
            return
        matched = [
            lock_type
            for lock_type in matched
            if not (
                lock_type == "url"
                and _url_allowed(message, settings.get("allowlist") or [])
            )
        ]
        if not matched:
            return  # allowlisted domains are exempt from the url lock
        me = await app.get_me()
        if message.from_user.id == me.id or await is_admin(
            message.chat.id, message.from_user.id
        ):
            return
        # Link Deleter deliberately ran above and owns every link decision
        # while enabled.  Only the legacy/content locks are bypassed here.
        # Reload after the live role lookup so a concurrent /approve is seen
        # before this automatic deletion.
        current = await db.get_chat(message.chat.id)
        # A lock removed while this watcher was awaiting the role lookup must
        # win: enforce only what is still configured in the fresh snapshot,
        # otherwise a stored punitive action could outlive its own lock.
        current_locks = set(current.get("locks", []))
        matched = [
            lock_type
            for lock_type in matched
            if lock_type in current_locks
            and not (
                lock_type == "url"
                and _url_allowed(message, current.get("allowlist") or [])
            )
        ]
        if not matched:
            return
        # Evaluate every matching lock independently. A sticker exemption must
        # not accidentally bypass a simultaneously locked forward (or any
        # other category).
        active = [
            lock_type
            for lock_type in matched
            if not is_exempt(
                current,
                message.from_user.id,
                _feature_for_lock(lock_type, message),
            )
        ]
        if not active:
            return
        await message.delete()
        actions = current.get("lock_actions") or {}
        # While the comprehensive media switch is ON, a media match can only
        # ever delete — even when a non-media lock matched the same message,
        # which must still contribute its own action.
        delete_only = _media_delete_on(current)
        custom = next(
            (
                actions.get(lock_type)
                for lock_type in active
                if actions.get(lock_type)
                and not (delete_only and lock_type in _MEDIA_DELETE_LOCKS)
            ),
            None,
        )
        action = (custom or {}).get("action", "del")
        reason = (custom or {}).get("reason", "")
        if action == "warn":
            await apply_warn(message, message.from_user, reason or "locked content")
        elif action == "kick":
            await app.ban_chat_member(message.chat.id, message.from_user.id)
            await app.unban_chat_member(message.chat.id, message.from_user.id)
        elif action == "mute":
            await safe_restrict(message.chat.id, message.from_user.id, _muted_permissions())
        elif action == "ban":
            await app.ban_chat_member(message.chat.id, message.from_user.id)
        if not current.get("lockwarns", True):
            return  # /lockwarns off — silent deletion, no notice
        now = time.monotonic()
        if now - _notices.get(message.chat.id, 0) < 60:
            return
        _notices[message.chat.id] = now
        notice = await app.send_message(
            message.chat.id,
            f"<b>{html.escape(', '.join(active))}</b> is locked here.",
            parse_mode=enums.ParseMode.HTML,
        )
        asyncio.create_task(_remove_notice(notice))
    except StopPropagation:
        raise
    except Exception:
        return



# Telegram re-sends a message through UpdateEditMessage/UpdateEditChannelMessage
# whenever its METADATA changes — a reaction added or removed, a pin, a view
# counter.  Lock enforcement must only act on genuine CONTENT edits: otherwise
# reacting to a locked-type post (photo, sticker, video…) deletes that post the
# moment someone reacts, which is what members saw as the bot "stopping" their
# reaction.  A content fingerprint recorded when the message first arrived is
# what tells the two apart (2026-09-19).
_EDIT_UPDATES = (raw.types.UpdateEditMessage, raw.types.UpdateEditChannelMessage)
_CONTENT_FIELDS = (
    "photo", "video", "document", "audio", "voice", "video_note",
    "animation", "sticker", "contact", "location", "poll", "web_page",
)
_baseline: "OrderedDict[tuple, str]" = OrderedDict()
_BASELINE_MAX = 3000


def _lock_fingerprint(message) -> str:
    """Stable hash of everything that counts as the message's content."""
    payload = {
        "text": getattr(message, "text", None),
        "caption": getattr(message, "caption", None),
        "entities": [str(e) for e in (getattr(message, "entities", None) or [])],
        "caption_entities": [
            str(e) for e in (getattr(message, "caption_entities", None) or [])
        ],
        "media": [
            (field, str(getattr(message, field, None)))
            for field in _CONTENT_FIELDS
            if getattr(message, field, None) is not None
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


def remember_lock_content(message) -> None:
    """Record the content of a newly delivered group message."""
    try:
        if getattr(message, "chat", None) is None or getattr(message, "id", None) is None:
            return
        key = (message.chat.id, message.id)
        _baseline[key] = _lock_fingerprint(message)
        _baseline.move_to_end(key)
        while len(_baseline) > _BASELINE_MAX:
            _baseline.popitem(last=False)
    except Exception:
        pass


def _metadata_only(message) -> bool:
    """True when a re-delivered message carries no content change.

    False only when the content provably differs from the recorded baseline —
    the single case in which a lock may act.
    """
    try:
        key = (message.chat.id, message.id)
    except Exception:
        return False
    try:
        current = _lock_fingerprint(message)
    except Exception:
        return False
    stored = _baseline.get(key)
    if stored is not None:
        if stored == current:
            return True
        _baseline[key] = current  # a genuine edit becomes the new baseline
        return False
    # No baseline (restart) but the update still carries reactions: a reaction
    # is metadata, never an edit of what the member posted — fail closed.
    if getattr(message, "reactions", None) is not None:
        _baseline[key] = current
        return True
    _baseline[key] = current
    return False


@app.on_message(filters.group & ~filters.service, group=-3)
async def remember_locked_content(_, message: types.Message) -> None:
    """Baseline every incoming group message so edits can be told from metadata."""
    remember_lock_content(message)


@app.on_raw_update(group=2)
async def enforce_locks_on_raw_edit(_, update, users, chats):
    """Re-check locks only for Telegram's genuine content-edit updates."""
    if not isinstance(update, _EDIT_UPDATES):
        return
    raw_message = getattr(update, "message", None)
    peer = getattr(raw_message, "peer_id", None)
    message_id = getattr(raw_message, "id", None)
    if peer is None or message_id is None:
        return
    try:
        chat_id = utils.get_peer_id(peer)
        message = await asyncio.wait_for(
            app.get_messages(chat_id, message_id),
            timeout=15,
        )
        if message is None or getattr(message, "empty", False):
            return
        if _metadata_only(message):
            # Reaction / pin / view-counter update — the content is untouched,
            # so no lock may fire and the post is never deleted.
            return
        await enforce_locks(_, message)
    except StopPropagation:
        raise
    except Exception:
        return