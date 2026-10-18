"""Maki-side VC monitor consumer and owner/admin controls.

Mikasa produces only short-lived public participant events into the private
SQLite transport.  This process is the only publisher: it sends the notice as
Maki and records that exact message id for a five-second best-effort delete.
"""

from __future__ import annotations

import asyncio
import html
import logging
import json
import os
import time
from datetime import datetime, timezone

from pyrogram import enums, filters, types
from pyrogram.errors import FloodWait

from ghelp import app
from ghelp.database import db
from ghelp.helpers import has_right, is_admin
from ghelp.selfdel import autodel_reply
from ghelp.vc_monitor import escaped_notice
from ghelp.vc_monitor_store import VCMonitorStore, get_store


logger = logging.getLogger(__name__)
store: VCMonitorStore = get_store()
_consumer: "MakiVCConsumer | None" = None

# Self-diagnosing coverage alerts.  A broken assistant used to fail in total
# silence — the group simply never saw a join notice and nobody could tell
# why.  Coverage is now evaluated on a slow cadence and a broken state is
# announced IN THE GROUP, with the cause and the fix, plus one recovery line
# when it comes back (2026-09-19).
# How long a voice-chat join notice stays visible before Maki deletes it
# (owner 2026-10-18).  VC_NOTICE_DELETE=0 keeps notices instead.
_NOTICE_DELETE = max(0.0, float(os.getenv("VC_NOTICE_DELETE", "5")))

_ALERT_INTERVAL = 60.0     # seconds between coverage evaluations
_HEALTH_STALE = 45.0       # no observation for this long => stale coverage
_ALERT_COOLDOWN = 1800.0   # re-announce the same state at most this often
_ALERT_TTL = 120.0         # alerts self-delete (the group stays readable)
_ALERT_KEY = "vc_alert_state"

# Statuses that mean "nothing is wrong right now", so no alert is warranted:
# an idle group with no live voice chat is not a fault.
_OK_STATUSES = {"active", "no_active_call", "disabled"}

# Access faults: the account cannot see the group's voice chat at all.
_ACCESS_STATUSES = {"inaccessible", "assistant_offline"}
# Snapshot faults: it can see it, but the participant list is not complete.
_SNAPSHOT_STATUSES = {"partial_snapshot", "update_gap"}

_REASON = {
    "inaccessible": (
        "The monitoring account cannot read this group's voice chat — it is "
        "most likely not a member of this group any more."
    ),
    "assistant_offline": "The monitoring account is offline.",
    "update_gap": (
        "The monitoring account cannot prove a complete participant list yet."
    ),
    "partial_snapshot": (
        "The participant snapshot is incomplete, so joins could be missed."
    ),
}


def _alert_text(status: str, health: dict | None, stale: bool) -> str:
    """One short, actionable notice: what broke, and how to fix it."""
    assistant_id = (health or {}).get("assistant_id")
    reason = _REASON.get(status)
    if reason is None:
        reason = (
            "The monitoring account stopped reporting, so Mikasa may be down."
            if stale
            else "Coverage cannot currently be confirmed."
        )
    detail = str((health or {}).get("detail") or "").strip()
    error = str((health or {}).get("last_error") or "").strip()
    last_seen = (health or {}).get("last_seen")

    lines = [
        "⚠️ <b>Voice-chat join notices are not working in this group.</b>",
        f"Reason: {html.escape(reason)}",
    ]
    # The hint must match the fault.  Telling an admin to re-add an account
    # that is already a member sends them down the wrong path (2026-09-29).
    if assistant_id and status in _ACCESS_STATUSES:
        lines.append(
            "Fix: make sure the account "
            f"<code>{html.escape(str(assistant_id))}</code> is a member of "
            "this group (no admin rights needed)."
        )
    elif status in _SNAPSHOT_STATUSES:
        lines.append(
            "Fix: the monitoring account can read this group but cannot "
            "finish a consistent participant list. It retries on its own; if "
            "this keeps appearing, check the observer log."
        )
    if detail:
        lines.append(f"Detail: {html.escape(detail[:200])}")
    if error:
        lines.append(f"Last error: <code>{html.escape(error[:120])}</code>")
    if last_seen:
        lines.append(f"Last observation: <code>{_when(last_seen)}</code>")
    lines.append(
        "\nGroup admins can check any time with <code>/vcmonitor status</code>."
    )
    return "\n".join(lines)


def _recovered_text() -> str:
    return (
        "✅ <b>Voice-chat join notices are working again in this group.</b>\n"
        "Participant monitoring is back online."
    )


def _when(value: float | None) -> str:
    if not value:
        return "never"
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (TypeError, ValueError, OverflowError):
        return "unknown"


class MakiVCConsumer:
    """Durable event consumer owned by the Maki process."""

    def __init__(
        self,
        bot,
        *,
        storage: VCMonitorStore | None = None,
        sync_known=None,
        clock=time.time,
        tick: float = 0.25,
        delete_delay: float = 5.0,
        alert_interval: float = _ALERT_INTERVAL,
    ) -> None:
        self.bot = bot
        self.store = storage or store
        self.sync_known = sync_known
        self.clock = clock
        self.tick = max(0.05, float(tick))
        self.delete_delay = max(0.0, float(delete_delay))
        self.alert_interval = max(10.0, float(alert_interval))
        self._last_alert_check: float | None = None
        self.known_sync_interval = 30.0
        self._last_known_sync: float | None = None
        self._running = False
        self._delete_tasks: set[asyncio.Task] = set()

    async def _delete_row(self, row: dict) -> None:
        try:
            await self.bot.delete_messages(int(row["chat_id"]), int(row["message_id"]))
            logger.info("vc notice %s deleted in %s",
                        row["message_id"], row["chat_id"])
        except asyncio.CancelledError:
            self.store.release_pending_delete(int(row["id"]))
            raise
        except Exception as exc:
            # Telegram Forbidden/MessageIdInvalid are expected best-effort
            # outcomes.  Persist only the exception type, never its text.
            self.store.finish_delete(
                int(row["id"]),
                success=False,
                error=type(exc).__name__,
                now=self.clock(),
            )
        else:
            self.store.finish_delete(
                int(row["id"]), success=True, now=self.clock()
            )

    async def process_pending_deletes(self) -> None:
        rows = self.store.claim_pending_deletes(now=self.clock(), limit=25)
        for row in rows:
            await self._delete_row(row)

    async def _delete_after_send(self, delete_id: int) -> None:
        try:
            await asyncio.sleep(self.delete_delay)
            # Claiming through SQLite makes restart cleanup and this timer
            # idempotent if a polling tick reaches the same work item.
            row = self.store.claim_pending_delete(delete_id, now=self.clock())
            if row is not None:
                await self._delete_row(row)
        except asyncio.CancelledError:
            raise

    def _track_delete(self, delete_id: int) -> None:
        task = asyncio.create_task(self._delete_after_send(delete_id))
        self._delete_tasks.add(task)
        task.add_done_callback(self._delete_tasks.discard)

    async def process_events(self) -> None:
        now = self.clock()
        rows = self.store.claim_events(now=now, limit=25)
        for row in rows:
            chat_id = int(row["chat_id"])
            if not self.store.is_enabled(chat_id):
                self.store.ack_event(int(row["id"]))
                continue
            identity_name = str(row.get("name") or "Telegram user")
            identity_username = str(row.get("username") or "")
            # The renderer takes the already-normalized public fields and
            # escapes them.  Numeric id is always retained.
            from ghelp.vc_monitor import PublicIdentity

            identity = PublicIdentity(
                peer_id=int(row["user_id"]),
                name=identity_name,
                username=identity_username,
                kind=str(row.get("kind") or "user"),
            )
            text = _join_notice(identity)
            try:
                sent = await self.bot.send_message(
                    chat_id,
                    text,
                    parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except asyncio.CancelledError:
                raise
            except FloodWait as exc:
                wait = min(float(getattr(exc, "value", 0) or 0) + 1.0, 120.0)
                logger.info("vc monitor publish flood-limited %.0fs", wait)
                self.store.release_event(int(row["id"]))
                await asyncio.sleep(wait)
                continue
            except Exception as exc:
                logger.info("vc monitor publish failed: %s", type(exc).__name__)
                self.store.release_event(int(row["id"]))
                continue
            sent_id = getattr(sent, "id", None)
            if sent_id is None:
                # A successful transport without a message id cannot be
                # safely scheduled for deletion; acknowledge it rather than
                # accidentally deleting an unrelated message.
                self.store.ack_event(int(row["id"]))
                continue
            sent_at = self.clock()
            delete_id = self.store.add_pending_delete(
                chat_id,
                int(sent_id),
                due_at=sent_at + self.delete_delay,
                now=sent_at,
            )
            self.store.ack_event(int(row["id"]))
            self._track_delete(delete_id)
            logger.info("vc notice %s in %s scheduled for deletion in %.0fs",
                        sent_id, chat_id, self.delete_delay)

    async def _delete_alert_later(self, chat_id: int, message_id: int) -> None:
        try:
            await asyncio.sleep(_ALERT_TTL)
            await self.bot.delete_messages(chat_id, message_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _post_alert(self, chat_id: int, text: str, kind: str = "alert") -> None:
        """Announce coverage state in the group; the notice self-deletes.

        ``kind`` is "alert" for a broken state and "recovery" for the
        all-clear, so the log states which one was actually sent.
        """
        try:
            sent = await self.bot.send_message(
                chat_id,
                text,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("vc coverage alert failed to send: %s", type(exc).__name__)
            return
        sent_id = getattr(sent, "id", None)
        if sent_id is None:
            return
        logger.info("vc coverage %s posted in %s", kind, chat_id)
        task = asyncio.create_task(self._delete_alert_later(chat_id, int(sent_id)))
        self._delete_tasks.add(task)
        task.add_done_callback(self._delete_tasks.discard)

    async def evaluate_coverage(self) -> None:
        """Tell the group when join notices cannot work, and when they recover.

        Runs on its own slow cadence.  The previous state is persisted in
        Maki's database so a restart neither spams the group nor hides a
        problem that is still present.
        """
        try:
            chats = self.store.known_chats(limit=200)
        except Exception as exc:
            logger.info("vc coverage check: known_chats failed: %s", type(exc).__name__)
            return
        now = self.clock()
        try:
            states = dict(await db.get(_ALERT_KEY, {}) or {})
        except Exception:
            states = {}
        changed = False
        for chat_id in chats:
            try:
                if not self.store.is_enabled(chat_id):
                    continue
                health = self.store.get_health(chat_id)
                status = str((health or {}).get("status") or "")
                last_seen = (health or {}).get("last_seen")
                stale = bool(
                    last_seen
                    and status == "active"
                    and now - float(last_seen) > _HEALTH_STALE
                )
                key = str(chat_id)
                record = states.get(key) or {}
                # An idle group (no live voice chat) and a healthy monitor are
                # both "nothing to report"; only problem states are announced.
                broken = status not in _OK_STATUSES or stale
                if not broken:
                    if record.get("broken"):
                        await self._post_alert(chat_id, _recovered_text(), kind="recovery")
                        states.pop(key, None)
                        changed = True
                    continue
                same = record.get("broken") and record.get("status") == status
                if same and now - float(record.get("at") or 0) < _ALERT_COOLDOWN:
                    continue
                await self._post_alert(chat_id, _alert_text(status, health, stale), kind="alert")
                states[key] = {"broken": True, "status": status, "at": now}
                changed = True
            except Exception as exc:
                logger.info(
                    "vc coverage check failed for %s: %s", chat_id, type(exc).__name__
                )
        if changed:
            try:
                await db.set(_ALERT_KEY, states)
            except Exception:
                logger.info("vc coverage alert: could not persist alert state")

    async def run(self) -> None:
        self._running = True
        try:
            # Restart cleanup happens before accepting new notices.
            await self.process_pending_deletes()
            while self._running:
                try:
                    now = self.clock()
                    if (
                        self.sync_known is not None
                        and (
                            self._last_known_sync is None
                            or now - self._last_known_sync >= self.known_sync_interval
                        )
                    ):
                        # This is intentionally independent of the 0.25s
                        # delete/event tick.  Commands call remember_chat()
                        # directly, so their setting is immediate.
                        self._last_known_sync = now
                        result = self.sync_known()
                        if hasattr(result, "__await__"):
                            await result
                    self.store.prune(now=now)
                    await self.process_pending_deletes()
                    await self.process_events()
                    if (
                        self._last_alert_check is None
                        or now - self._last_alert_check >= self.alert_interval
                    ):
                        # Independent slow cadence: coverage is a health fact,
                        # not a per-event concern.
                        self._last_alert_check = now
                        await self.evaluate_coverage()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.info("vc monitor consumer tick failed: %s", type(exc).__name__)
                await asyncio.sleep(self.tick)
        finally:
            self._running = False
            # Do not delete durable pending rows on shutdown.  A fresh Maki
            # process will retry them; in-flight timers are no longer needed.
            for task in list(self._delete_tasks):
                task.cancel()

    def stop(self) -> None:
        self._running = False


async def _sync_known_groups() -> None:
    """Publish Maki's known group set without sharing its JSON database."""
    try:
        chats = await db.all_chats()
    except Exception as exc:
        logger.info("vc monitor known-group sync failed: %s", type(exc).__name__)
        return
    for raw_chat_id in chats:
        try:
            chat_id = int(raw_chat_id)
        except (TypeError, ValueError):
            continue
        # Maki's chat settings are group-scoped.  Ignore accidental private
        # records so the monitor cannot scan a private conversation.
        if chat_id < 0:
            store.remember_chat(chat_id)


# ── assistant recovery hand-off (owner 2026-10-18) ────────────────────────
# A KICKED account can never rejoin from an invite link -- Telegram refuses
# until the ban is lifted.  Maki is the bot that holds admin rights in these
# groups, so it lifts the ban; the music bot owns the assistant session, so it
# performs the join.  They share one small file on this host.
_REJOIN_FILE = "/root/.maki_vc_rejoin.json"
_ASSISTANT_ID = int(os.getenv("ASSISTANT_ID", "6189989810"))


def _read_rejoin() -> dict:
    try:
        with open(_REJOIN_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_rejoin(data: dict) -> None:
    try:
        with open(_REJOIN_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception as exc:
        logger.info("vcmonitor: rejoin file write failed: %s",
                    type(exc).__name__)


async def _api(method: str, **params):
    """Plain Bot API call with Maki's own token (no client involved)."""
    import aiohttp
    token = getattr(config, "BOT_TOKEN", "") or ""
    if not token:
        return None
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                url, json=params,
                timeout=aiohttp.ClientTimeout(total=40, sock_connect=15),
            ) as r:
                return await r.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.info("vcmonitor: %s failed (%s)", method, type(exc).__name__)
        return None


async def offer_assistant_rejoin(chat_id: int) -> bool:
    """Unban the assistant and leave an invite link for the music bot.

    Returns True when a rejoin request was written.
    """
    body = await _api("getChatMember", chat_id=int(chat_id),
                      user_id=_ASSISTANT_ID)
    status = (body or {}).get("result", {}).get("status")
    if status != "kicked":
        return False
    ub = await _api("unbanChatMember", chat_id=int(chat_id),
                    user_id=_ASSISTANT_ID, only_if_banned=True)
    if not (ub and ub.get("ok")):
        logger.warning(
            "vcmonitor: could not unban the assistant in %s (%s) - I need "
            "the 'ban users' right there", chat_id,
            str((ub or {}).get("description"))[:60])
        return False
    link_body = await _api("exportChatInviteLink", chat_id=int(chat_id))
    link = (link_body or {}).get("result") if link_body else None
    data = _read_rejoin()
    data[str(chat_id)] = {"link": link, "at": time.time()}
    _write_rejoin(data)
    logger.warning(
        "vcmonitor: assistant was KICKED in %s - unbanned, rejoin request "
        "left for the music bot (link=%s)", chat_id, "yes" if link else "no")
    return True


def _join_notice(identity) -> str:
    """Short voice-chat join notice, one detail per line (owner 2026-10-18).

    Every detail gets its own line and its own relevant emoji, including the
    username when the account has one, so nothing is crammed onto one line.
    """
    name = html.escape(str(getattr(identity, "name", "") or "user")[:256], quote=True)
    username = getattr(identity, "username", None)
    try:
        peer = int(getattr(identity, "peer_id", 0))
    except (TypeError, ValueError):
        peer = 0
    kind = getattr(identity, "kind", "user")
    if kind == "channel":
        public_id, heading = str(-1000000000000 - abs(peer)), "\U0001f3a7 <b>Channel in VC</b>"
    elif kind == "chat":
        public_id, heading = str(-abs(peer)), "\U0001f3a7 <b>Chat in VC</b>"
    else:
        public_id, heading = str(peer), "\U0001f3a7 <b>Joined VC</b>"
    now = datetime.now(timezone.utc).astimezone()
    lines = [heading, f"\U0001f464 {name}"]
    if username:
        # the username on its own line, so it never crowds the display name
        lines.append(f"\U0001f516 <code>@{html.escape(str(username)[:64], quote=True)}</code>")
    lines.append(f"\U0001f194 <code>{public_id}</code>")
    lines.append(f"\U0001f550 {now.strftime('%I:%M %p').lstrip('0')}")
    return "\n".join(lines)


@app.on_message(filters.group & filters.command("vcmonitor", prefixes=["/", "?", "!", "."]))
@autodel_reply
async def vcmonitor_command(_, message: types.Message) -> None:
    # Do not inherit /anonadmin's anonymous sender exception here: this
    # setting is explicitly limited to a live, identifiable group owner/admin.
    if not message.from_user or not await has_right(
        int(message.chat.id), int(message.from_user.id),
        "can_manage_video_chats",
    ):
        await message.reply_text(
            "You need the Manage video chats admin right to use this command."
        )
        return
    chat_id = int(message.chat.id)
    store.remember_chat(chat_id)
    args = [part.lower() for part in (message.command or [])[1:]]
    if len(args) > 1 or (args and args[0] not in {"on", "off", "status"}):
        args = ["status"]
        usage = (
            "\n\nUsage: <code>/vcmonitor on|off|status</code> "
            "(group owner/admin only)."
        )
    else:
        usage = ""
    command = args[0] if args else "status"
    if command in {"on", "off"}:
        enabled = command == "on"
        store.set_enabled(chat_id, enabled)
        await message.reply_text(
            f"<b>VC monitor {'enabled' if enabled else 'disabled'}.</b>"
            + (
                " Next: ensure Mikasa has an accessible assistant account. "
                "Maki deletes notices after 5 seconds."
                if enabled
                else " Queued notices for this group were discarded."
            ),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    setting = store.get_setting(chat_id)
    health = store.get_health(chat_id)
    enabled_text = "ON" if setting["enabled"] else "OFF"
    if not health:
        coverage = (
            "Coverage unavailable: Mikasa has not reported an online assistant "
            "with access to this group's voice chat."
        )
    else:
        status = str(health.get("status") or "unknown")
        last_seen_value = health.get("last_seen")
        stale = (
            not last_seen_value
            or time.time() - float(last_seen_value) > 45.0
        )
        if status == "active" and not stale:
            coverage = (
                "Coverage active: participant sync is complete; join/leave "
                "detection is enabled."
            )
        elif status == "active" and stale:
            coverage = (
                "Coverage unavailable: the last active assistant observation is "
                "stale, so Mikasa may be offline."
            )
        elif status == "assistant_offline":
            coverage = "Coverage unavailable: Mikasa assistant is offline."
        elif status in {"inaccessible", "update_gap", "partial_snapshot"}:
            coverage = (
                "Coverage incomplete: the selected assistant cannot prove a "
                "complete participant snapshot yet."
            )
        elif status == "no_active_call":
            coverage = "No active voice chat; coverage will resume when one exists."
        elif status == "disabled":
            coverage = "Monitoring is disabled for this group."
        else:
            coverage = "Coverage is not currently proven by Mikasa."
    last_seen = _when(health.get("last_seen")) if health else "never"
    detail = html.escape(str(health.get("detail") or "")[:256]) if health else ""
    error = html.escape(str(health.get("last_error") or "")[:160]) if health else ""
    suffix = (
        f"\nLast assistant observation: <code>{last_seen}</code>"
        + (f"\nDetail: {detail}" if detail else "")
        + (f"\nPoll error: <code>{error}</code>" if error else "")
    )
    await message.reply_text(
        f"<b>VC monitor: {enabled_text}</b>\n{coverage}{suffix}{usage}",
        parse_mode=enums.ParseMode.HTML,
    )


async def on_start(_app) -> None:
    global _consumer
    if _consumer is not None:
        return
    _consumer = MakiVCConsumer(
        app, storage=store, sync_known=_sync_known_groups,
        delete_delay=_NOTICE_DELETE,
    )
    await _consumer.run()
