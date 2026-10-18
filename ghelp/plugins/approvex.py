"""Trusted-filter approval extras and approval-feature UI.

This is a per-group exemption from automatic nuisance filters.  It is not
Telegram join-request approval, and it does not release an existing mute.
"""

import asyncio
import html
import logging

from pyrogram import enums, filters, types

from ghelp import app
from ghelp import approval_trust
from ghelp.approval_trust import (
    canonicalize_approved,
    exemption_description,
    is_approved,
    normalize_user_id,
)
from ghelp.moderation_reply import send_reply
from ghelp.selfdel import autodel_reply
from ghelp.database import db
from ghelp.helpers import ikb, ikm, mention, require_admin, resolve_user

logger = logging.getLogger(__name__)
PRE = ["/", "?", "!", "."]
_APPROVED_PAGE_SIZE = 30
_settings_locks: dict[int, asyncio.Lock] = {}

# The Telegram admin right an administrator must personally hold before they
# may approve members or change approval policy: the right that decides what
# members are allowed to post.  The same right already gates the approval
# action buttons, so both paths stay consistent.
_SETTINGS_RIGHT = "can_restrict_members"
_SETTINGS_RIGHT_LABEL = "Ban users (restrict members)"
_SETTINGS_DENIED = (
    "You need the Ban users (restrict members) admin right to change "
    "approval settings."
)

# The worker-side approval_trust module owns these functions.  Keeping the
# imports injectable makes the UI load safely while an older installation is
# being upgraded, and is useful for the offline fake stores used by tests.
APPROVAL_FEATURES = getattr(approval_trust, "APPROVAL_FEATURES", {
    "stickers": "Stickers",
    "gifs": "GIFs",
    "media": "Media",
    "forwards": "Forwards",
    "other_locks": "Other content locks",
    "blacklist_words": "Blacklist words",
    "anti_flood": "Anti-flood",
    "force_subscribe": "Force-subscribe",
    "approval_mode": "Approval mode",
})
enabled_features = getattr(approval_trust, "enabled_features", None)
feature_counts = getattr(approval_trust, "feature_counts", None)
set_feature = getattr(approval_trust, "set_feature", None)
features_description = getattr(approval_trust, "features_description", None)


def _enabled_features(cfg) -> tuple:
    """Read the engine's *actual* enabled policy, without guessing a policy."""
    if not callable(enabled_features):
        return ()
    try:
        return tuple(enabled_features(cfg))
    except Exception:
        logger.info("Could not render approval feature policy", exc_info=True)
        return ()


def _feature_labels() -> dict:
    return APPROVAL_FEATURES if isinstance(APPROVAL_FEATURES, dict) else {}


def _feature_enabled(cfg, feature: str) -> bool:
    return feature in _enabled_features(cfg)


def _feature_count(cfg, uid: int) -> tuple[int, int]:
    if callable(feature_counts):
        try:
            return tuple(feature_counts(cfg, uid))
        except Exception:
            logger.info("Could not count approval features", exc_info=True)
    # Keep the UI fail-closed if an older worker-side module is loaded.
    return (0, len(_feature_labels()))


def _feature_text(cfg: dict, uid: int) -> str:
    """Render a compact, effective per-member feature status card."""
    enabled, available = _feature_count(cfg, uid)
    status = f"<b>{enabled}/{available} enabled</b>"
    if not is_approved(cfg, uid):
        status += " · <b>not approved</b>"
    return (
        f"<b>Approved features</b> · <code>{uid}</code>\n"
        f"{status}\n"
        "<i>Applies to approved members; filter exemptions only. Telegram "
        "permissions and protections are unchanged.</i>"
    )


def _feature_callback(uid: int, chat_id: int) -> str:
    # The chat id is part of the binding.  The callback handlers also require
    # this exact button to still be present on the source message.
    return f"gh:ap:f:{uid}:{chat_id}"


def _settings_callback(feature: str, desired: bool, expected: bool,
                       chat_id: int) -> str:
    # desired and expected are intentionally both encoded: a second click on a
    # stale keyboard cannot invert the newly persisted server state.
    return (
        f"gh:ap:s:{feature}:{int(desired)}:{int(expected)}:{chat_id}"
    )


def _feature_toggle_callback(uid: int, feature: str, desired: bool,
                             expected: bool, chat_id: int) -> str:
    """Bind a feature toggle to its member, source chat, and UI state."""
    return (
        f"gh:ap:t:{uid}:{feature}:{int(desired)}:{int(expected)}:{chat_id}"
    )


def _close_callback(uid: int, chat_id: int) -> str:
    """Close button binding; uid 0 means the group-wide settings panel."""
    return f"gh:ap:x:{uid}:{chat_id}"


def _close_button(uid: int, chat_id: int):
    return ikb(
        "Close",
        callback_data=_close_callback(uid, chat_id),
        style=enums.ButtonStyle.DANGER,
    )


def _button_is_bound(q, callback_data: str) -> bool:
    rows = getattr(getattr(getattr(q, "message", None), "reply_markup", None),
                   "inline_keyboard", []) or []
    return any(
        getattr(button, "callback_data", None) == callback_data
        for row in rows or [] for button in (row or [])
    )


def _settings_markup(cfg: dict, chat_id: int):
    rows = []
    for feature, label in _feature_labels().items():
        current = _feature_enabled(cfg, feature)
        state = "ON" if current else "OFF"
        rows.append([ikb(
            f"{label}: {state}",
            callback_data=_settings_callback(
                feature, not current, current, chat_id
            ),
            style=(enums.ButtonStyle.SUCCESS if current
                   else enums.ButtonStyle.PRIMARY),
        )])
    rows.append([_close_button(0, chat_id)])
    return ikm(rows) if rows else None


def _feature_markup(cfg: dict, uid: int, chat_id: int, base_markup=None):
    """Build the per-member feature card and retain member actions.

    The selected user's action buttons remain available when this card was
    opened from an approval action. List navigation buttons are deliberately
    not copied into a member card.
    """
    rows = []
    for feature, label in _feature_labels().items():
        current = _feature_enabled(cfg, feature)
        rows.append([ikb(
            f"{label}: {'ON' if current else 'OFF'}",
            callback_data=_feature_toggle_callback(
                uid, feature, not current, current, chat_id
            ),
            style=(enums.ButtonStyle.SUCCESS if current
                   else enums.ButtonStyle.PRIMARY),
        )])

    old_rows = getattr(base_markup, "inline_keyboard", []) or []
    preserved = []
    selected_callback = _feature_callback(uid, chat_id)
    for row in old_rows:
        kept = []
        for button in row or []:
            callback = getattr(button, "callback_data", None)
            if callback == selected_callback or (
                isinstance(callback, str)
                and callback.startswith(("gh:ap:a:", "gh:ap:u:", "gh:ap:b:"))
                and callback.rsplit(":", 1)[-1] == str(uid)
            ):
                kept.append(button)
        if kept:
            preserved.append(kept)
    rows.extend(preserved)
    rows.append([_close_button(uid, chat_id)])
    return ikm(rows) if rows else None


def _settings_text(cfg: dict) -> str:
    labels = _feature_labels()
    enabled = _enabled_features(cfg)
    count = len(set(enabled).intersection(labels))
    lines = [
        "<b>Approval features</b>",
        f"<b>{count}/{len(labels)} enabled</b>",
        "<i>Applies to approved members only; Telegram permissions and "
        "protections are unchanged.</i>",
        "",
    ]
    if not labels:
        lines.append("Approval feature controls are unavailable right now.")
    lines.append(
        f"The group owner and admins with the <b>{_SETTINGS_RIGHT_LABEL}</b> "
        "right can change these settings; other admins can view them."
    )
    return "\n".join(lines)


async def _can_change_settings(chat_id: int, actor) -> bool:
    """Live check for who may change approval policy.

    The group owner always may.  An administrator may as well, but only with
    the Telegram right that actually governs what members are allowed to
    post (``can_restrict_members``): approval exempts a member from the
    media/sticker/link filters, so it must not be grantable by an admin who
    has no power over member content in the first place.  Deliberately no
    configured-bot-owner bypass — this is the group's own policy.
    """
    if not actor or getattr(actor, "is_bot", False):
        return False
    try:
        member = await app.get_chat_member(chat_id, actor.id)
    except Exception:
        return False
    if member.status == enums.ChatMemberStatus.OWNER:
        return True
    return member.status == enums.ChatMemberStatus.ADMINISTRATOR and bool(
        getattr(getattr(member, "privileges", None), _SETTINGS_RIGHT, False)
    )


async def _require_settings_right(m: types.Message) -> bool:
    """Command-side twin of :func:`_can_change_settings`.

    ``require_right`` is deliberately not used here: it lets the configured
    bot owner and anonymous ``sender_chat`` admins through without proving a
    personal ``can_restrict_members`` right, and approval decides who may
    bypass this group's content filters.
    """
    if await _can_change_settings(m.chat.id, getattr(m, "from_user", None)):
        return True
    await _reply(
        m,
        f"You need the <b>{_SETTINGS_RIGHT_LABEL}</b> admin right in this "
        "group to change approvals.",
    )
    return False


async def _actual_group_creator(chat_id: int, actor) -> bool:
    """Live Telegram creator check, with no configured-owner bypass."""
    if not actor or getattr(actor, "is_bot", False):
        return False
    try:
        member = await app.get_chat_member(chat_id, actor.id)
    except Exception:
        return False
    return member.status == enums.ChatMemberStatus.OWNER


async def _live_admin_or_owner(chat_id: int, actor) -> bool:
    if not actor or getattr(actor, "is_bot", False):
        return False
    try:
        member = await app.get_chat_member(chat_id, actor.id)
    except Exception:
        return False
    return member.status in (
        enums.ChatMemberStatus.OWNER,
        enums.ChatMemberStatus.ADMINISTRATOR,
    )


async def _edit_settings(q, cfg: dict) -> None:
    text = _settings_text(cfg)
    markup = _settings_markup(cfg, q.message.chat.id)
    try:
        result = await q.message.edit_text(
            text, parse_mode=enums.ParseMode.HTML, reply_markup=markup
        )
        if result is False:
            raise RuntimeError("Telegram did not update approval settings")
    except Exception:
        await _reply(q.message, text, quote=False, reply_markup=markup)


async def _edit_features(q, uid: int, cfg: dict) -> None:
    text = _feature_text(cfg, uid)
    markup = _feature_markup(
        cfg, uid, q.message.chat.id,
        base_markup=getattr(q.message, "reply_markup", None),
    )
    try:
        result = await q.message.edit_text(
            text, parse_mode=enums.ParseMode.HTML, reply_markup=markup
        )
        if result is False:
            raise RuntimeError("Telegram did not update approved features")
    except Exception:
        await _reply(q.message, text, quote=False, reply_markup=markup)


async def _show_features(q, uid: int, chat_id: int) -> None:
    cfg = await db.get_chat(chat_id)
    # Refresh before rendering: a card can outlive a revoke or a policy
    # change, and must never display historical exemptions as current ones.
    text = _feature_text(cfg, uid)
    try:
        # Preserve the source card's member actions while replacing the old
        # prose display with actual feature controls.
        result = await q.message.edit_text(
            text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_feature_markup(
                cfg, uid, chat_id,
                base_markup=getattr(q.message, "reply_markup", None),
            ),
        )
        if result is False:
            raise RuntimeError("Telegram did not update approval features")
    except Exception:
        await _reply(
            q.message, text, quote=False,
            reply_markup=_feature_markup(
                cfg, uid, chat_id,
                base_markup=getattr(q.message, "reply_markup", None),
            ),
        )


async def _can_view_features(chat_id: int, actor, uid: int) -> bool:
    if actor and normalize_user_id(getattr(actor, "id", None)) == uid:
        return True
    return await _live_admin_or_owner(chat_id, actor)


@autodel_reply
async def _reply(m: types.Message, text: str, **kwargs):
    return await send_reply(m, text, client=app, **kwargs)


async def _answer(query, text, *, alert=False):
    try:
        await query.answer(text, show_alert=alert)
    except Exception:
        # An expired acknowledgement does not change the display outcome.
        pass


def _actor(m: types.Message) -> str:
    actor = getattr(m, "from_user", None)
    if actor and normalize_user_id(getattr(actor, "id", None)) is not None:
        return mention(actor.id, actor.first_name or "admin")
    sender_chat = getattr(m, "sender_chat", None)
    if sender_chat and getattr(sender_chat, "title", None):
        return html.escape(sender_chat.title)
    return "the acting admin"


def _target(user) -> str:
    uid = normalize_user_id(getattr(user, "id", None))
    name = getattr(user, "first_name", None) or "user"
    return f"{mention(uid, name)} <code>{uid}</code>"


@app.on_message(filters.group & filters.command("approve", prefixes=PRE))
async def approve_cmd(_, m: types.Message):
    # Approving grants filter exemptions, so the caller must personally hold
    # the right that governs what members may post.
    if not await _require_settings_right(m):
        return
    user = await resolve_user(m)
    if not user:
        await _reply(m, "Warning: Reply to a user or give an @username/id.")
        return

    uid = normalize_user_id(getattr(user, "id", None))
    if uid is None:
        await _reply(m, "Warning: That target has no valid Telegram user ID.")
        return
    outcome: dict[str, object] = {}

    def _add(cfg):
        approved = canonicalize_approved(cfg)
        if uid in approved:
            outcome["changed"] = False
        else:
            cfg["approved"].append(uid)
            outcome["changed"] = True

    await db.mutate_chat(m.chat.id, _add)
    state = "approved" if outcome.get("changed") else "already approved"
    cfg = await db.get_chat(m.chat.id)
    if callable(features_description):
        details = _feature_text(cfg, uid)
    else:
        details = (
            f"<b>Approved features</b> · <code>{uid}</code>\n"
            "Applies to approved members; filter exemptions only. Telegram "
            "permissions and protections are unchanged."
        )
    await _reply(
        m,
        f"Done: {_target(user)} is <b>{state}</b> by {_actor(m)}.\n"
        f"{details}",
        reply_markup=ikm([[
            ikb(
                "Approved features",
                callback_data=_feature_callback(uid, m.chat.id),
                style=enums.ButtonStyle.PRIMARY,
            )
        ]]),
    )


@app.on_message(filters.group & filters.command("approved", prefixes=PRE))
async def approved_list(_, m: types.Message):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    cfg = await db.get_chat(m.chat.id)
    users = canonicalize_approved(cfg)
    if not users:
        await _reply(m, f"Info: No approved users.\n{exemption_description()}")
        return
    await _render_approved_page(m, users, 0, cfg=cfg)


@app.on_message(filters.group & filters.command("unapprove", prefixes=PRE))
async def unapprove_cmd(_, m: types.Message):
    if not await _require_settings_right(m):
        return
    user = await resolve_user(m)
    if not user:
        await _reply(m, "Warning: Reply to a user or give an @username/id.")
        return

    uid = normalize_user_id(getattr(user, "id", None))
    if uid is None:
        await _reply(m, "Warning: That target has no valid Telegram user ID.")
        return
    outcome: dict[str, object] = {}

    def _rm(cfg):
        approved = canonicalize_approved(cfg)
        if uid in approved:
            cfg["approved"] = [item for item in approved if item != uid]
            outcome["changed"] = True
        else:
            outcome["changed"] = False

    await db.mutate_chat(m.chat.id, _rm)
    state = "removed from" if outcome.get("changed") else "not on"
    await _reply(
        m,
        f"Done: {_target(user)} is <b>{state}</b> the trusted-filter "
        f"exemption list (actor: {_actor(m)}).\n"
        "Existing bans/mutes were not changed; use /unmute separately.",
    )


@app.on_message(filters.group & filters.command("unapproveall", prefixes=PRE))
async def unapprove_all(_, m: types.Message):
    if not await _actual_group_creator(m.chat.id, getattr(m, "from_user", None)):
        await _reply(m, "Only the group creator can unapprove everyone.")
        return

    outcome: dict[str, int] = {}

    def _clear(cfg):
        outcome["count"] = len(canonicalize_approved(cfg))
        cfg["approved"] = []

    await db.mutate_chat(m.chat.id, _clear)
    await _reply(
        m,
        f"Done: trusted-filter exemption list cleared "
        f"({outcome.get('count', 0)} users) by {_actor(m)}.\n"
        "Existing bans/mutes were not changed; use /unmute separately.",
    )


async def _render_approved_page(message, users: list[int], page: int,
                                *, edit: bool = False, cfg: dict | None = None
                                ) -> None:
    """Render a truthful, paginated approved-user list."""
    total = len(users)
    pages = max(1, (total + _APPROVED_PAGE_SIZE - 1) // _APPROVED_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * _APPROVED_PAGE_SIZE
    shown = users[start:start + _APPROVED_PAGE_SIZE]
    end = start + len(shown)
    lines = [
        f"<b>Trusted-filter exemptions ({total})</b>",
        f"Showing <b>{start + 1}–{end}</b> of <b>{total}</b> approved users.",
        "",
    ]
    rows = []
    for uid in shown:
        lines.append(f"• {mention(uid, 'user')} <code>{uid}</code>")
        rows.append([ikb(
            f"Approved features · {uid}",
            callback_data=_feature_callback(uid, message.chat.id),
            style=enums.ButtonStyle.PRIMARY,
        )])
    if pages > 1:
        nav = []
        if page:
            nav.append(ikb(
                "Previous",
                callback_data=f"gh:ap:page:{page - 1}:{message.chat.id}",
                style=enums.ButtonStyle.PRIMARY,
            ))
        if page + 1 < pages:
            nav.append(ikb(
                "Next",
                callback_data=f"gh:ap:page:{page + 1}:{message.chat.id}",
                style=enums.ButtonStyle.PRIMARY,
            ))
        rows.append(nav)
    footer = (
        "Each user's <b>Approved features</b> button shows the current "
        "server policy for that member."
        if callable(features_description)
        else exemption_description()
    )
    text = "\n".join(lines) + f"\n\n{footer}"
    markup = ikm(rows)
    if edit:
        try:
            result = await message.edit_text(
                text, parse_mode=enums.ParseMode.HTML, reply_markup=markup
            )
            if result is False:
                raise RuntimeError("Telegram did not update approved users")
            return
        except Exception:
            pass
    await _reply(message, text, reply_markup=markup)


@app.on_message(filters.group & filters.command("approvesettings", prefixes=PRE))
async def approve_settings_cmd(_, m: types.Message):
    """Show the policy panel; the owner check belongs to each toggle."""
    cfg = await db.get_chat(m.chat.id)
    await _reply(
        m,
        _settings_text(cfg),
        reply_markup=_settings_markup(cfg, m.chat.id),
    )


@app.on_callback_query(filters.regex(r"^gh:ap:s:[a-z_]+:[01]:[01]:-?\d+$"))
async def approve_settings_cb(_, q: types.CallbackQuery):
    if (
        not q.from_user or not q.message
        or q.message.chat.type not in (
            enums.ChatType.GROUP, enums.ChatType.SUPERGROUP
        )
    ):
        return await _answer(q, "Use approval settings inside the group.", alert=True)
    try:
        _, _, _, feature, desired_s, expected_s, chat_s = q.data.split(":")
        if desired_s not in ("0", "1") or expected_s not in ("0", "1"):
            raise ValueError
        source_chat = int(chat_s)
        desired, expected = bool(int(desired_s)), bool(int(expected_s))
    except (AttributeError, TypeError, ValueError):
        return await _answer(q, "This settings button is malformed.", alert=True)
    if source_chat != q.message.chat.id or feature not in _feature_labels():
        return await _answer(q, "This settings button is no longer valid.", alert=True)
    if not _button_is_bound(q, q.data):
        return await _answer(q, "This settings panel is no longer available.", alert=True)
    if not await _can_change_settings(q.message.chat.id, q.from_user):
        return await _answer(q, _SETTINGS_DENIED, alert=True)

    lock = _settings_locks.setdefault(q.message.chat.id, asyncio.Lock())
    async with lock:
        # The actor may have been demoted while another settings callback was
        # waiting on this chat's lock. Re-check immediately before the
        # compare-and-set write; a creator check made before the lock is not
        # sufficient authorization for a later mutation.
        if not await _can_change_settings(q.message.chat.id, q.from_user):
            return await _answer(q, _SETTINGS_DENIED, alert=True)
        cfg = await db.get_chat(q.message.chat.id)
        current = _feature_enabled(cfg, feature)
        if current != expected:
            await _answer(q, "This button is stale; showing the current settings.", alert=True)
            return await _edit_settings(q, cfg)
        if current != desired:
            if not callable(set_feature):
                return await _answer(
                    q, "Approval feature controls are unavailable right now.",
                    alert=True,
                )
            try:
                await set_feature(
                    q.message.chat.id,
                    feature,
                    desired,
                    database=db,
                    expected=expected,
                )
            except Exception as exc:
                logger.warning(
                    "Approval feature save failed: %s", type(exc).__name__
                )
                return await _answer(
                    q, "Could not save this approval setting. Try again.",
                    alert=True,
                )
        cfg = await db.get_chat(q.message.chat.id)
        # set_feature's compare-and-set guard also protects against writers
        # outside this UI lock. Never claim that a stale card changed policy.
        if _feature_enabled(cfg, feature) != desired:
            await _answer(q, "This button is stale; showing the current settings.", alert=True)
            return await _edit_settings(q, cfg)
        await _answer(q, "Approval setting updated.")
        await _edit_settings(q, cfg)


@app.on_callback_query(filters.regex(
    r"^gh:ap:t:-?\d+:[a-z_]+:[01]:[01]:-?\d+$"
))
async def approved_feature_toggle_cb(_, q: types.CallbackQuery):
    """Toggle one group policy from a member-bound features card.

    Feature policy remains group-wide by design. The member id is retained
    in the callback so a button from one approved-member card cannot be
    replayed as a different target, and the effective per-member count is
    always refreshed before rendering.
    """
    if (
        not q.from_user or not q.message
        or q.message.chat.type not in (
            enums.ChatType.GROUP, enums.ChatType.SUPERGROUP
        )
    ):
        return await _answer(q, "Use feature controls inside the group.", alert=True)
    try:
        _, _, _, uid_s, feature, desired_s, expected_s, chat_s = q.data.split(":")
        uid = int(uid_s)
        source_chat = int(chat_s)
        if desired_s not in ("0", "1") or expected_s not in ("0", "1"):
            raise ValueError
        desired, expected = bool(int(desired_s)), bool(int(expected_s))
    except (AttributeError, TypeError, ValueError):
        return await _answer(q, "This feature button is malformed.", alert=True)
    if (
        uid <= 0
        or source_chat != q.message.chat.id
        or feature not in _feature_labels()
    ):
        return await _answer(q, "This feature button is no longer valid.", alert=True)
    if not _button_is_bound(q, q.data):
        return await _answer(q, "This feature button is no longer available.", alert=True)
    if not await _can_change_settings(q.message.chat.id, q.from_user):
        return await _answer(q, _SETTINGS_DENIED, alert=True)

    lock = _settings_locks.setdefault(q.message.chat.id, asyncio.Lock())
    async with lock:
        # Authorization is intentionally repeated immediately before the
        # compare-and-set write, just as it is for /approvesettings.
        if not await _can_change_settings(q.message.chat.id, q.from_user):
            return await _answer(q, _SETTINGS_DENIED, alert=True)
        cfg = await db.get_chat(q.message.chat.id)
        current = _feature_enabled(cfg, feature)
        if current != expected:
            await _answer(q, "This button is stale; showing current features.", alert=True)
            return await _edit_features(q, uid, cfg)
        if current != desired:
            if not callable(set_feature):
                return await _answer(
                    q, "Approval feature controls are unavailable right now.",
                    alert=True,
                )
            try:
                await set_feature(
                    q.message.chat.id,
                    feature,
                    desired,
                    database=db,
                    expected=expected,
                )
            except Exception as exc:
                logger.warning(
                    "Approval feature save failed: %s", type(exc).__name__
                )
                return await _answer(
                    q, "Could not save this approval setting. Try again.",
                    alert=True,
                )
        cfg = await db.get_chat(q.message.chat.id)
        if _feature_enabled(cfg, feature) != desired:
            await _answer(q, "This button is stale; showing current features.", alert=True)
            return await _edit_features(q, uid, cfg)
        await _answer(q, "Approval feature updated.")
        await _edit_features(q, uid, cfg)


@app.on_callback_query(filters.regex(r"^gh:ap:page:-?\d+:-?\d+$"))
async def approved_page_cb(_, q: types.CallbackQuery):
    if (
        not q.from_user or not q.message
        or q.message.chat.type not in (
            enums.ChatType.GROUP, enums.ChatType.SUPERGROUP
        )
    ):
        return await _answer(q, "Use this list inside its group.", alert=True)
    try:
        _, _, _, page_s, chat_s = q.data.split(":")
        page, source_chat = int(page_s), int(chat_s)
    except (AttributeError, TypeError, ValueError):
        return await _answer(q, "This list button is malformed.", alert=True)
    if source_chat != q.message.chat.id or page < 0:
        return await _answer(q, "This list button is no longer valid.", alert=True)
    if not _button_is_bound(q, q.data):
        return await _answer(q, "This list is no longer available.", alert=True)
    if not await _live_admin_or_owner(q.message.chat.id, q.from_user):
        return await _answer(q, "Only a current group admin can browse this list.", alert=True)
    cfg = await db.get_chat(q.message.chat.id)
    users = canonicalize_approved(cfg)
    await _answer(q, "Loading…")
    await _render_approved_page(q.message, users, page, edit=True, cfg=cfg)


@app.on_callback_query(filters.regex(r"^gh:ap:f:-?\d+:-?\d+$"))
async def approved_features_cb(_, q: types.CallbackQuery):
    """Open the selected user's compact feature controls."""
    if (
        not q.from_user or not q.message
        or q.message.chat.type not in (
            enums.ChatType.GROUP, enums.ChatType.SUPERGROUP
        )
    ):
        return await _answer(q, "Use this display inside its group.", alert=True)
    try:
        _, _, _, uid_s, chat_s = q.data.split(":")
        uid, source_chat = int(uid_s), int(chat_s)
    except (AttributeError, TypeError, ValueError):
        return await _answer(q, "This feature button is malformed.", alert=True)
    if source_chat != q.message.chat.id or uid <= 0:
        return await _answer(q, "This feature button is no longer valid.", alert=True)
    if not _button_is_bound(q, q.data):
        return await _answer(q, "This feature button is no longer available.", alert=True)
    if not await _can_view_features(q.message.chat.id, q.from_user, uid):
        return await _answer(
            q, "Only this user or a current group admin can view these features.",
            alert=True,
        )
    await _answer(q, "Loading approved features…")
    await _show_features(q, uid, q.message.chat.id)


@app.on_callback_query(filters.regex(r"^gh:ap:x:-?\d+:-?\d+$"))
async def approve_close_cb(_, q: types.CallbackQuery):
    """Close an approval panel: remove the card, or strip it if undeletable."""
    if (
        not q.from_user or not q.message
        or q.message.chat.type not in (
            enums.ChatType.GROUP, enums.ChatType.SUPERGROUP
        )
    ):
        return await _answer(q, "Use this panel inside its group.", alert=True)
    try:
        _, _, _, uid_s, chat_s = q.data.split(":")
        uid, source_chat = int(uid_s), int(chat_s)
    except (AttributeError, TypeError, ValueError):
        return await _answer(q, "This close button is malformed.", alert=True)
    if source_chat != q.message.chat.id or uid < 0:
        return await _answer(q, "This close button is no longer valid.", alert=True)
    if not _button_is_bound(q, q.data):
        return await _answer(q, "This panel is no longer available.", alert=True)
    # A member may close their own feature card; the group-wide settings panel
    # stays with the people who are allowed to see it.
    allowed = (
        await _can_view_features(q.message.chat.id, q.from_user, uid)
        if uid else await _live_admin_or_owner(q.message.chat.id, q.from_user)
    )
    if not allowed:
        return await _answer(
            q, "Only this user or a current group admin can close this panel.",
            alert=True,
        )
    await _answer(q, "Closed.")
    try:
        await q.message.delete()
        return
    except Exception:
        # Without delete rights the card cannot disappear; make it inert
        # instead of leaving live controls behind.
        logger.info("Could not delete an approval panel", exc_info=True)
    try:
        await q.message.edit_text(
            "<b>Approval panel closed.</b>", parse_mode=enums.ParseMode.HTML,
            reply_markup=None,
        )
    except Exception:
        logger.info("Could not strip a closed approval panel", exc_info=True)
