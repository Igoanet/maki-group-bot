"""Entry point: python -u group_help.py  (cwd = bots/group_help)

Loads all ghelp.plugins modules via pyrogram's plugin system, then starts
any plugin background tasks (modules may define `async def on_start(app)` —
e.g. the night-mode scheduler).
"""

import asyncio
import importlib
import os
import pkgutil

os.makedirs("data", exist_ok=True)

from pyrogram import idle, types

from ghelp import app, logger, restrict_gate
from ghelp.config import config
import ghelp.plugins as _plugins_pkg


# Owner's user restriction (/restrict, shared with the music bot): gate handlers at registration,
# before pyrogram loads the plugins. Only feature handlers are hidden from restricted users —
# commands, buttons, inline mode and these feature watchers; moderation watchers still run.
FEATURE_WATCHERS = {"ghelp.plugins.notes", "ghelp.plugins.gtools"}  # #notes, @admin paging


async def main() -> None:
    restrict_gate.hook(app, owner_id=config.OWNER_ID, all_groups=False, feature_modules=FEATURE_WATCHERS)
    await app.start()

    started = 0
    plugin_tasks = []
    for mod in pkgutil.iter_modules(_plugins_pkg.__path__):
        module = importlib.import_module(f"ghelp.plugins.{mod.name}")
        on_start = getattr(module, "on_start", None)
        if callable(on_start):
            plugin_tasks.append(asyncio.create_task(on_start(app)))
            started += 1

    me = await app.get_me()

    # Self-healing: systemd watchdog heartbeat + live Telegram probe.  This is
    # what makes "the bot is never off" an enforced property rather than a
    # hope — a wedged loop or a dead session is detected and restarted.
    from ghelp.watchdog import start_watchdog

    plugin_tasks.append(start_watchdog(app))

    # Moderation must never touch the account Maki's own VC notices depend on.
    from ghelp.config import config as _cfg
    from ghelp.guard import install_moderation_guard

    guarded = install_moderation_guard(app, _cfg.PROTECTED_IDS)
    logger.info(
        "moderation guard: protecting %s (%d entry points)",
        sorted(_cfg.PROTECTED_IDS), guarded,
    )

    # Texts (/start, /help, welcome) name the bot; default to Telegram's real
    # display name unless GH_BOT_NAME overrides it.
    if not os.getenv("GH_BOT_NAME") and me.first_name:
        config.BOT_NAME = me.first_name
    logger.info(
        "Bot started as @%s — %d plugins, %d background tasks",
        me.username,
        len(list(pkgutil.iter_modules(_plugins_pkg.__path__))),
        started,
    )

    # Slash-command menu — Telegram shows NOTHING unless the bot registers it
    # (this was empty, which is why /tq was undiscoverable). Keep it short
    # and user-facing; admin verbs live in /help instead of a 30-item menu.
    try:
        await app.set_bot_commands([
            types.BotCommand("q", "Quote the replied message as a sticker"),
            types.BotCommand("fq", "Fake quote: their identity, your text"),
            types.BotCommand("tq", "Fake quote (same as /fq)"),
            types.BotCommand("help", "All commands, explained by section"),
            types.BotCommand("rules", "Read the group rules"),
            types.BotCommand("notes", "List the saved notes"),
            types.BotCommand("chatfight", "Today's group activity leaderboard"),
            types.BotCommand("weektop", "This week's group activity leaderboard"),
            types.BotCommand("top", "All-time group activity leaderboard"),
            types.BotCommand("report", "Reply to a message to alert the admins"),
            types.BotCommand("afk", "Mark yourself away"),
            types.BotCommand("id", "Show chat & user IDs"),
            types.BotCommand("ping", "Check the bot is alive"),
        ])
    except Exception as exc:
        logger.warning("set_bot_commands failed: %s", exc)

    try:
        await idle()
    finally:
        for task in plugin_tasks:
            task.cancel()
        for task in plugin_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
