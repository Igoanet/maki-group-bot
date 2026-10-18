"""Self-healing supervision: make "the bot is never off" an enforced property.

A process that is *alive* is not necessarily a bot that *works*.  Two failure
modes are invisible from the inside:

* the event loop is wedged (a blocking call, a deadlock) — no handler runs and
  no log line appears, yet systemd still sees a perfectly healthy process;
* the loop is turning fine but the MTProto session is dead — the bot keeps
  looping while every Telegram call fails, so it silently answers nothing.

This module closes both.  It is a no-op when the process was not started by
systemd with NOTIFY_SOCKET set, so a manual `python group_help.py` is
unaffected.

1. systemd watchdog (`Type=notify` + `WatchdogSec`).  The loop reports
   WATCHDOG=1 every WatchdogSec/3 seconds.  If it stops reporting, systemd
   kills and restarts the unit — a wedged loop can only ever be seen by the
   supervisor, never by itself.
2. Live Telegram probe.  A heartbeat alone only proves the loop is turning, so
   before reporting healthy the watchdog performs a real authenticated RPC.
   After PROBE_FAILURES consecutive failures the process exits non-zero and
   Restart=always brings it straight back.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

logger = logging.getLogger(__name__)

_NOTIFY_SOCKET = os.getenv("NOTIFY_SOCKET")
try:
    _WATCHDOG_USEC = int(os.getenv("WATCHDOG_USEC") or 0)
except ValueError:
    _WATCHDOG_USEC = 0

_PROBE_TIMEOUT = 20.0
_PROBE_FAILURES = 3
_QUEUE_STUCK_LIMIT = 500
_MIN_INTERVAL = 10.0


def sd_notify(message: str) -> None:
    """Best-effort sd_notify; silently ignored outside a systemd unit."""
    if not _NOTIFY_SOCKET:
        return
    try:
        address = _NOTIFY_SOCKET
        if address.startswith("@"):
            address = "\0" + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except Exception:
        pass


async def _telegram_alive() -> bool:
    """One real round-trip through the bot's own MTProto session."""
    from ghelp import app

    probes = []
    try:
        from pyrogram.raw.functions.updates import GetState

        probes.append(GetState())
    except Exception:
        pass
    try:
        from pyrogram.raw.functions.help import GetNearestDc

        probes.append(GetNearestDc())
    except Exception:
        pass
    if not probes:
        # Cannot build a probe at all (SDK moved): fail open.  Never restart
        # the bot because our own probe broke.
        return True

    for query in probes:
        try:
            await asyncio.wait_for(app.invoke(query), _PROBE_TIMEOUT)
            return True
        except Exception as exc:
            logger.warning("watchdog: Telegram probe failed: %s", type(exc).__name__)
    return False


def _restart(reason: str) -> None:
    logger.error("watchdog: %s — exiting so systemd restarts the bot", reason)
    sd_notify(f"STATUS=restarting: {reason}")
    os._exit(1)


async def watchdog_loop() -> None:
    from ghelp import app

    interval = max(_MIN_INTERVAL, _WATCHDOG_USEC / 3 / 1_000_000) if _WATCHDOG_USEC else 45.0
    failures = 0
    sd_notify("READY=1\nSTATUS=bot started")
    logger.info(
        "watchdog: armed (interval %.0fs, systemd watchdog %s)",
        interval,
        "on" if _WATCHDOG_USEC else "off",
    )

    while True:
        try:
            await asyncio.sleep(interval)
            healthy = await _telegram_alive()
            queue = getattr(getattr(app, "dispatcher", None), "updates_queue", None)
            pending = queue.qsize() if queue is not None else 0
            if pending > _QUEUE_STUCK_LIMIT:
                _restart(f"update queue stuck at {pending} updates")
            if healthy:
                failures = 0
                sd_notify("WATCHDOG=1")
            else:
                failures += 1
                logger.warning(
                    "watchdog: Telegram unreachable (%d/%d)", failures, _PROBE_FAILURES
                )
                if failures >= _PROBE_FAILURES:
                    _restart(f"Telegram unreachable {failures} probes in a row")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The watchdog must never die from its own error.
            logger.warning("watchdog: loop error: %s", type(exc).__name__)


def start_watchdog(app) -> asyncio.Task:
    """Start the supervisor task (call once, after app.start())."""
    return asyncio.get_running_loop().create_task(watchdog_loop())
