import logging

from pyrogram import Client

from .config import config

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s - %(levelname)s] - %(name)s: %(message)s",
    datefmt="%d-%b-%y %H:%M:%S",
)
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logger = logging.getLogger("ghelp")

app = Client(
    "group_help",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    workdir="data",
    # 2-vCPU box: pyrogram's default min(32, cpu+4) == 6 workers left the
    # rate guard with only 4 handler slots, so legitimate commands were
    # silently dropped whenever a few quote/render jobs were in flight.
    workers=32,
    plugins=dict(root="ghelp.plugins"),
)

from .selfdel import install_client_cleanup  # noqa: E402

install_client_cleanup(app)

# owner/sudo tools that legitimately run for minutes
NO_TIMEOUT_CMDS = frozenset({"broadcast", "massban", "zombies"})


async def _is_staff(uid: int) -> bool:
    if uid == config.OWNER_ID:
        return True
    try:
        from .sudo import is_sudo

        return await is_sudo(uid)
    except Exception:
        return False


from .rateguard import install_guard, is_bulk_purge_stop  # noqa: E402

install_guard(
    app,
    _is_staff,
    NO_TIMEOUT_CMDS,
    # The group-2 lock/link watcher must process moderation updates even when
    # a user's command rate or the handler semaphore is saturated.
    # gtools holds the anonymous-admin gate: it BLOCKS a command by raising
    # StopPropagation, which the wrapper would otherwise swallow.  This
    # module must be registered unwrapped or the gate silently lets the
    # command run (owner 2026-10-18).
    message_exempt_modules={
        "ghelp.plugins.locks",
        "ghelp.plugins.autodel",
        "ghelp.plugins.gtools",
    },
    # massban's ban loop runs from its confirm callback and can take many
    # minutes — it must not be cancelled by the callback timeout
    cb_exempt_modules={"ghelp.plugins.ownerpanel"},
    priority_callback=is_bulk_purge_stop,
    log=logger,
)


# ── command registry ───────────────────────────────────────────────────────
# Captured at DECORATION time, with this wrapper sitting OUTSIDE rateguard, so
# it receives the plugin's RAW handler.  Two reasons this is the reliable path:
#   * this client defers dispatcher registration until start(), so
#     ``app.dispatcher.groups`` is empty for anything that is not the running
#     process;
#   * the filter tree is not introspectable in every build, which made the
#     old name-walk miss commands (e.g. it found nothing for a real command).
# The anonymous-admin verification flow uses this to run the withheld command.
COMMAND_HANDLERS: dict[str, list] = {}


def _flt_commands(flt) -> set:
    """Command names a filter matches, unwrapped out of its wrapper chain."""
    out, stack, guard = set(), [flt], 0
    while stack and guard < 400:
        guard += 1
        node = stack.pop()
        if node is None or isinstance(node, (str, bytes, int, float, bool)):
            continue
        if isinstance(node, (list, tuple, set, frozenset, dict)):
            try:
                stack.extend(list(node))
            except TypeError:
                pass
            continue
        cmds = getattr(node, "commands", None)
        if cmds:
            try:
                for c in cmds:
                    out.add(str(c).split("@")[0].strip().lower())
            except TypeError:
                out.add(str(cmds).split("@")[0].strip().lower())
        for attr in ("base", "other", "left", "right", "filters"):
            stack.append(getattr(node, attr, None))
    return out


_guard_on_message = app.on_message


def _registry_on_message(flt=None, group=0):
    def deco(handler):
        try:
            for name in _flt_commands(flt):
                COMMAND_HANDLERS.setdefault(name, []).append((group, handler))
        except Exception:  # never break plugin loading for bookkeeping
            pass
        return _guard_on_message(flt, group)(handler)

    return deco


app.on_message = _registry_on_message


def find_command_handlers(name: str) -> list:
    """[(group, handler)] registered for a command, lowest group first."""
    return sorted(COMMAND_HANDLERS.get((name or "").lower(), []),
                  key=lambda item: item[0])
