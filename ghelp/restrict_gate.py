"""Pyrogram side of the shared user restriction: hide the bot's handlers from restricted users.

Instead of one early handler that swallows the whole update (which would also hide a restricted
user's messages from the moderation watchers — locks, word blocklist, flood…), `hook()` wraps the
dispatcher's add_handler so every handler a restricted user must not reach gets a "sender is not
restricted" filter prepended *synchronously at registration* — before it can ever be dispatched.
Moderation keeps seeing their messages; the features simply stop matching, exactly as if each
handler had been written with `& ~restricted`.

Call `hook()` once at the top of the bot's entry point, before any plugin is imported/loaded.
Shared by the bots in this project (symlinked into each package next to restrictions.py); needs
the same Pyrogram fork in every bot.
"""

from __future__ import annotations

from typing import Iterable

from pyrogram import filters, handlers

from . import restrictions

# handler types that carry a user request; the rest (raw updates, chat member updates, errors,
# disconnects) are events, not functions a user "uses"
GATED = (handlers.MessageHandler, handlers.EditedMessageHandler, handlers.CallbackQueryHandler,
         handlers.InlineQueryHandler, handlers.ChosenInlineResultHandler)
# always gated whatever their group: buttons and inline mode are features by definition
ALWAYS = (handlers.CallbackQueryHandler, handlers.InlineQueryHandler, handlers.ChosenInlineResultHandler)

EXEMPT_ATTR = "restrict_exempt"


def exempt(func):
    """Mark a handler callback that must keep running for restricted users (the notices)."""
    setattr(func, EXEMPT_ATTR, True)
    return func


def blocked_by(owner_id: int):
    """The per-bot predicate: restricted and not the owner."""

    def is_blocked(user) -> bool:
        return bool(user) and user.id != owner_id and restrictions.is_restricted(user.id)

    return is_blocked


def not_restricted(is_blocked):
    async def func(_, __, update) -> bool:
        if getattr(update, "service", None):
            return True  # service messages are events (joins, VC started…), not requests
        return not is_blocked(getattr(update, "from_user", None))

    return filters.create(func, "NotRestricted")


def _gate(handler, group: int, flt, all_groups: bool, feature_modules: set[str]) -> bool:
    if not isinstance(handler, GATED) or getattr(handler, "_restrict_gated", False):
        return False
    if getattr(handler.callback, EXEMPT_ATTR, False):
        return False
    module = getattr(handler.callback, "__module__", "") or ""
    if not (all_groups or group <= 0 or isinstance(handler, ALWAYS) or module in feature_modules):
        return False
    handler.filters = (flt & handler.filters) if handler.filters else flt
    handler._restrict_gated = True
    return True


def hook(app, *, owner_id: int, all_groups: bool, feature_modules: Iterable[str] = ()) -> int:
    """Gate every handler registered from now on (and any already in the dispatcher).

    `all_groups=True` gates every handler (a bot without moderation watchers); otherwise only
    groups <= 0 (commands and gates), ALWAYS types, and message handlers defined in
    `feature_modules` (feature watchers such as #notes). Returns how many existing handlers were
    gated. Idempotent.
    """
    flt = not_restricted(blocked_by(owner_id))
    modules = set(feature_modules)
    dispatcher = app.dispatcher
    if not getattr(dispatcher, "_restrict_hooked", False):
        original = dispatcher.add_handler

        def add_handler(handler, group: int = 0):
            _gate(handler, group, flt, all_groups, modules)  # before it becomes dispatchable
            return original(handler, group)

        dispatcher.add_handler = add_handler
        dispatcher._restrict_hooked = True
    count = 0
    for group, group_handlers in list(dispatcher.groups.items()):
        for h in group_handlers:
            count += _gate(h, group, flt, all_groups, modules)
    return count
