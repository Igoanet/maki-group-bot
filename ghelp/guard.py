"""Refuse to ban or restrict accounts Maki itself depends on.

Maki's voice-chat notices require the Mikasa assistant account to be a member
of the group.  It was permanently banned twice — once natively by an admin and
once through Maki's own /ban command — and each time coverage died silently.

Rather than a guard in every one of the ~25 ban call sites, this intercepts the
client's ban/restrict methods once: whatever feature asks, a protected account
is never touched.  The refusal is logged so the attempt is visible instead of
silent (2026-09-19).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Methods that can remove or cripple the monitoring account.  unban is
# deliberately absent: restoring the account must always be allowed.
_GUARDED = ("ban_chat_member", "restrict_chat_member")


def install_moderation_guard(app, protected_ids) -> int:
    """Wrap app.ban_chat_member / app.restrict_chat_member.  Returns how many
    entry points were installed."""
    ids = {int(x) for x in (protected_ids or ()) if str(x).strip().isdigit()}
    if not ids:
        return 0

    def _wrap(name: str) -> int:
        original = getattr(app, name, None)
        if original is None:
            return 0

        async def guarded(chat_id, user_id, *args, **kwargs):
            try:
                target = int(user_id)
            except (TypeError, ValueError):
                target = None
            if target in ids:
                logger.warning(
                    "moderation guard: refused %s on protected account %s in %s",
                    name, target, chat_id,
                )
                return False
            return await original(chat_id, user_id, *args, **kwargs)

        setattr(app, name, guarded)
        return 1

    return sum(_wrap(name) for name in _GUARDED)
