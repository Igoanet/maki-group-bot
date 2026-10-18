"""Shared per-chat trusted-member checks for automatic moderation.

The ``approved`` setting is deliberately separate from Telegram membership
roles.  An approved member is still an ordinary member; this module only
answers whether that member may skip the automatic nuisance filters for one
chat.
"""

from __future__ import annotations

import html
from collections.abc import Iterable


# These identifiers are part of the persisted API. Keep this order stable:
# it is also the order used when rendering settings and callback messages.
APPROVAL_FEATURES = {
    "stickers": "Stickers",
    "gifs": "GIFs",
    "media": "Media",
    "forwards": "Forwards",
    "other_locks": "Other content locks",
    "blacklist_words": "Blacklist words",
    "anti_flood": "Anti-flood",
    "force_subscribe": "Force-subscribe",
    "approval_mode": "Approval mode",
}
DEFAULT_APPROVAL_FEATURES = tuple(APPROVAL_FEATURES)


TRUSTED_FILTERS_DESCRIPTION = (
    "Approved members bypass this chat's automatic nuisance filters for "
    "stickers, GIFs/media, forwards, ordinary content locks, blacklist words, "
    "and anti-flood. This is not Telegram join-request approval and does not "
    "grant admin rights. The existing force-subscribe skip is preserved; "
    "owner-only Link Deleter, high-risk Shield checks, manual bans/mutes, "
    "captcha, and anti-raid remain in force."
)

APPROVAL_DESCRIPTION = (
    f"{TRUSTED_FILTERS_DESCRIPTION} /approve changes only the selected user's "
    "bot-filter exemptions; group and member Telegram permissions are unchanged. It does "
    "not lift an existing ban or mute. Use /unmute for a mute, and /unban "
    "for a ban."
)


def exemption_description() -> str:
    """Return the user-facing explanation shared by approval UIs."""
    return TRUSTED_FILTERS_DESCRIPTION


def approval_description() -> str:
    """Return the /approve-specific explanation for command replies."""
    return APPROVAL_DESCRIPTION


def normalize_user_id(value) -> int | None:
    """Return a Telegram user id from a safe int/string representation.

    Database migrations and older records have used both integer and string
    ids.  Invalid values are ignored rather than making a moderation watcher
    fail.  ``bool`` is excluded explicitly because it is an ``int`` subclass.
    """
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            try:
                parsed = int(value, 10)
            except (TypeError, ValueError):
                return None
            return parsed if parsed > 0 else None
        return None
    except Exception:
        return None


def normalized_exemption_ids(values) -> list[int]:
    """Canonicalize an approved list, retaining each valid id once."""
    # The setting is persisted as a JSON list.  Do not iterate strings,
    # mappings, tuples, or sets: ``"123"`` would otherwise become ``1, 2, 3``
    # and a dict would accidentally trust its keys.
    if not isinstance(values, list):
        return []
    try:
        iterator: Iterable = values
        result: list[int] = []
        seen: set[int] = set()
        for value in iterator:
            user_id = normalize_user_id(value)
            if user_id is not None and user_id not in seen:
                seen.add(user_id)
                result.append(user_id)
        return result
    except Exception:
        # A malformed setting must never crash a content watcher.
        return []


def is_approved(cfg: dict | None, user_id) -> bool:
    """Whether *user_id* is trusted in this particular chat.

    The check fails closed.  Every malformed chat shape, approved-setting
    shape, lookup failure, and ID parsing failure returns ``False`` rather
    than granting an exemption.  Callers still perform their normal live
    admin checks; approved status never grants admin powers.
    """
    try:
        target = normalize_user_id(user_id)
    except Exception:
        return False
    if target is None:
        return False
    try:
        if not isinstance(cfg, dict):
            return False
        approved = cfg.get("approved", [])
        if not isinstance(approved, list):
            return False
        return target in normalized_exemption_ids(approved)
    except Exception:
        return False


def enabled_features(cfg: dict | None) -> tuple[str, ...]:
    """Return the enabled trusted-member exemptions for a chat.

    Old records have no ``approval_features`` key and retain the historical
    behaviour: every exemption is enabled. An explicitly malformed value is
    fail-closed instead. Unknown string identifiers are ignored so a record
    written by a newer bot remains readable by an older one; non-string
    members make the explicit setting malformed.
    """
    try:
        if not isinstance(cfg, dict):
            return ()
        if "approval_features" not in cfg:
            return DEFAULT_APPROVAL_FEATURES
        raw = cfg["approval_features"]
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            return ()
        selected = set(raw)
        return tuple(
            feature
            for feature in DEFAULT_APPROVAL_FEATURES
            if feature in selected
        )
    except Exception:
        return ()


def feature_counts(cfg: dict | None, user_id) -> tuple[int, int]:
    """Return effective enabled and available exemption counts for a member.

    ``approval_features`` is a group policy, while ``approved`` is the
    per-member grant.  The UI must report the effective result rather than
    making a policy setting look like an exemption granted to an unapproved
    member.
    """
    total = len(APPROVAL_FEATURES)
    if not is_approved(cfg, user_id):
        return 0, total
    return len(enabled_features(cfg)), total


def is_exempt(cfg: dict | None, user_id, feature: str) -> bool:
    """Whether an approved member has one particular exemption enabled."""
    try:
        if feature not in APPROVAL_FEATURES:
            return False
        return is_approved(cfg, user_id) and feature in enabled_features(cfg)
    except Exception:
        return False


async def set_feature(
    chat_id: int,
    feature: str,
    enabled: bool,
    database=None,
    *,
    expected: bool | None = None,
) -> bool:
    """Atomically enable or disable one exemption for a chat.

    The setting is deliberately group-wide: changing it affects every
    currently approved member on their next watched message. No per-user
    snapshot is created.  When *expected* is supplied, the write is
    compare-and-set: a callback carrying an old UI state cannot overwrite a
    newer policy.  A ``False`` result means either that the requested state
    was already current or that the compare-and-set guard rejected it; callers
    that need to distinguish those cases should read the setting back.
    """
    if (
        feature not in APPROVAL_FEATURES
        or not isinstance(enabled, bool)
        or (expected is not None and not isinstance(expected, bool))
    ):
        return False
    if database is None:
        from ghelp.database import db as database

    outcome = {"changed": False}

    def _apply(cfg):
        before = cfg.get("approval_features", None)
        current = set(enabled_features(cfg))
        if expected is not None and (feature in current) != expected:
            return
        if enabled:
            current.add(feature)
        else:
            current.discard(feature)
        updated = [
            item for item in DEFAULT_APPROVAL_FEATURES if item in current
        ]
        if before != updated:
            cfg["approval_features"] = updated
            outcome["changed"] = True

    await database.mutate_chat(chat_id, _apply)
    return bool(outcome["changed"])


def features_description(cfg: dict | None, user_id) -> str:
    """Describe this user's current exemptions without implying permissions.

    The result is HTML-safe for callback/command replies. It intentionally
    states the negative case too: revoking approval does not alter Telegram
    permissions or existing moderation state.
    """
    approved = is_approved(cfg, user_id)
    enabled = enabled_features(cfg)
    if approved:
        if enabled:
            listed = ", ".join(
                f"{html.escape(APPROVAL_FEATURES[item])} "
                f"(<code>{html.escape(item)}</code>)"
                for item in enabled
            )
            status = f"<b>Approved exemptions:</b> {listed}."
        else:
            status = (
                "<b>Approved exemptions:</b> none are enabled for this group."
            )
    else:
        status = "<b>Not approved:</b> no automatic-filter exemptions apply."
    return (
        f"{status} Telegram permissions, existing bans/mutes, and protections "
        "not listed above are unchanged."
    )


def canonicalize_approved(cfg: dict) -> list[int]:
    """Canonicalize ``cfg['approved']`` in-place and return the IDs.

    Approval commands use this inside ``db.mutate_chat`` so additions and
    removals are atomic even when old records contain string IDs or duplicates.
    """
    values = normalized_exemption_ids(cfg.get("approved", []))
    cfg["approved"] = values
    return values


async def add_approved(chat_id: int, user_id, database=None) -> bool:
    """Atomically add one trusted-filter exemption.

    ``database`` is injectable for offline callers/tests; production callers
    may omit it and use the shared GroupHelp database.  The return value says
    whether the list changed.
    """
    uid = normalize_user_id(user_id)
    if uid is None:
        return False
    if database is None:
        from ghelp.database import db as database
    outcome = {"changed": False}

    def _add(cfg):
        approved = canonicalize_approved(cfg)
        if uid not in approved:
            cfg["approved"].append(uid)
            outcome["changed"] = True

    await database.mutate_chat(chat_id, _add)
    return bool(outcome["changed"])


async def remove_approved(chat_id: int, user_id, database=None) -> bool:
    """Atomically revoke one trusted-filter exemption."""
    uid = normalize_user_id(user_id)
    if uid is None:
        return False
    if database is None:
        from ghelp.database import db as database
    outcome = {"changed": False}

    def _remove(cfg):
        approved = canonicalize_approved(cfg)
        if uid in approved:
            cfg["approved"] = [item for item in approved if item != uid]
            outcome["changed"] = True

    await database.mutate_chat(chat_id, _remove)
    return bool(outcome["changed"])


async def clear_approved(chat_id: int, database=None) -> int:
    """Atomically clear trusted-filter exemptions and return removed count."""
    if database is None:
        from ghelp.database import db as database
    outcome = {"count": 0}

    def _clear(cfg):
        outcome["count"] = len(canonicalize_approved(cfg))
        cfg["approved"] = []

    await database.mutate_chat(chat_id, _clear)
    return int(outcome["count"])