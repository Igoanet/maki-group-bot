"""The two independent bot-side auto-delete policies.

``/autodel <duration>`` is a bounded group watcher.  It deletes every human
message delivered to Maki, including messages from admins and the owner.  A
small outbound wrapper also schedules Maki's own sent messages.  Telegram does
not deliver other bots' posts to a bot account, so this policy never claims
coverage it cannot have.

``/autodeluser`` is a separate, immediate policy.  It only ever deletes a
message whose sender is an explicitly selected user, and performs the live
staff/protection/permission checks again immediately before the delete.

The old selected-user timer settings are deliberately left intact.  They are
read as a legacy policy rather than silently migrating those targets to the
new immediate policy.
"""

from __future__ import annotations

import asyncio
import functools
import html
import logging
import re
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.bulk_delete import _prepare_delete_request, delete_right
from ghelp.config import config
from ghelp.database import db
from ghelp.helpers import has_right, ikb, ikm, is_admin, require_admin
from ghelp.message_scope import SCOPES, matches_scope

logger = logging.getLogger(__name__)

_MIN_DELAY, _MAX_DELAY = 1, 30 * 24 * 3600  # legacy selected-user timer
_GROUP_MIN_DELAY, _GROUP_MAX_DELAY = 5, 86400
_GROUP_BUTTONS = (5, 10, 30, 60, 300, 900, 3600, 86400)
_DELAY_BUTTONS = (5, 10, 30, 60, 300, 900, 3600)
_GROUP_TYPES = (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_VIEW_RE = re.compile(r"^[0-9a-f]{8}$")
_CALLBACK_RE = r"^gh:ad:"
_MENU_TTL = 600
_DURATION_RE = re.compile(r"^(\d+)(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days)?$")
_GROUP_MODES = frozenset({"off", "custom"})
_GROUP_LOCKS: dict[int, asyncio.Lock] = {}
_POLICY_TOKENS: dict[tuple[str, int], object] = {}
_OUTBOUND_METHODS = (
    "send_message", "send_photo", "send_video", "send_animation",
    "send_document", "send_audio", "send_voice", "send_sticker",
    "send_media_group", "copy_message", "forward_messages",
)
_OUTBOUND_INSTALLED = set()
_OUTBOUND_SEEN: dict[tuple[int, int], float] = {}
_OUTBOUND_SEEN_TTL = 3600
_OUTBOUND_TRACK_TASKS: set[asyncio.Task] = set()
_OUTBOUND_TRACK_LIMIT = 64
_INSTANT_QUEUE: asyncio.Queue | None = None
_INSTANT_QUEUE_LOOP = None
_INSTANT_WORKERS: set[asyncio.Task] = set()
_INSTANT_PENDING: set[tuple[int, int]] = set()
_INSTANT_WORKERS_MAX = 4
_INSTANT_QUEUE_MAX = 512


def _safe_delay(value) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 10
    return value if _MIN_DELAY <= value <= _MAX_DELAY else 10


def _safe_group_delay(value) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 10
    return (
        value
        if _GROUP_MIN_DELAY <= value <= _GROUP_MAX_DELAY
        else 10
    )


def _duration(value) -> int | None:
    """Parse a duration without rounding it.

    Bare numbers are seconds for compatibility with the old ``time`` command.
    ``0`` is deliberately not a duration: only the explicit ``off`` command
    disables the group policy.
    """

    match = _DURATION_RE.fullmatch(str(value or "").strip().lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    multiplier = {
        "s": 1, "sec": 1, "secs": 1,
        "m": 60, "min": 60, "mins": 60,
        "h": 3600, "hr": 3600, "hrs": 3600,
        "d": 86400, "day": 86400, "days": 86400,
    }[unit]
    seconds = amount * multiplier
    if not _GROUP_MIN_DELAY <= seconds <= _GROUP_MAX_DELAY:
        return None
    return seconds


def _user_ids(values) -> list[int]:
    users = []
    for value in values or []:
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value not in users:
            users.append(value)
    return users


def _settings(cfg: dict) -> dict:
    """Return normalized settings, including migration-safe defaults.

    ``autodel_users`` and ``autodel_on`` are the pre-two-mode policy.  Keep
    them separate from ``autodel_instant_users`` and the group policy so an
    existing installation cannot unexpectedly turn a delayed target into an
    immediate delete.
    """

    users = _user_ids(cfg.get("autodel_users", []))
    instant_users = _user_ids(cfg.get("autodel_instant_users", []))
    scope = cfg.get("autodel_scope", "all")
    if scope not in SCOPES:
        scope = "all"

    stored_group_mode = cfg.get("autodel_group_mode", "off")
    # ``native`` was an unsupported bot-account experiment.  Treat any
    # historic value as OFF and expose a warning only; it must never activate
    # an MTProto user-only RPC or pretend to cover hidden bot posts.
    historic_native = stored_group_mode == "native" or bool(
        cfg.get("autodel_group_native_unknown", False)
    )
    group_mode = (
        stored_group_mode
        if stored_group_mode in _GROUP_MODES
        else "off"
    )
    group_time = _safe_group_delay(
        cfg.get("autodel_group_time", cfg.get("autodel_time", 10))
    )
    try:
        group_revision = int(cfg.get("autodel_group_revision", 0))
    except (TypeError, ValueError):
        group_revision = 0
    try:
        legacy_revision = int(cfg.get("autodel_legacy_revision", 0))
    except (TypeError, ValueError):
        legacy_revision = 0
    instant_generations = {}
    raw_generations = cfg.get("autodel_instant_generations") or {}
    if not isinstance(raw_generations, dict):
        raw_generations = {}
    for key, value in raw_generations.items():
        try:
            instant_generations[str(int(key))] = int(value)
        except (TypeError, ValueError):
            continue

    # Existing installations had no master switch.  Preserve their selected
    # delayed policy exactly; do not infer that it was a group-wide policy.
    enabled = cfg.get("autodel_on")
    if enabled is None:
        enabled = bool(users or cfg.get("autodel_bots", False))
    return {
        "group_mode": group_mode,
        "group_time": group_time,
        "group_native_warning": historic_native,
        "group_revision": group_revision,
        "legacy_revision": legacy_revision,
        "instant_users": instant_users,
        "instant_generations": instant_generations,
        # Legacy names remain part of the normalized shape for old callers and
        # for a safe, explicit compatibility path in the watcher.
        "on": bool(enabled),
        "bots": bool(cfg.get("autodel_bots", False)),
        "users": users,
        "time": _safe_delay(cfg.get("autodel_time", 10)),
        "scope": scope,
    }


def _group_lock(chat_id: int) -> asyncio.Lock:
    return _GROUP_LOCKS.setdefault(chat_id, asyncio.Lock())


def _next_revision(cfg: dict, key: str) -> int:
    try:
        revision = int(cfg.get(key, 0)) + 1
    except (TypeError, ValueError):
        revision = 1
    cfg[key] = revision
    return revision


def _policy_token(policy: str, chat_id: int):
    return _POLICY_TOKENS.setdefault((policy, int(chat_id)), object())


def _invalidate_policy(policy: str, chat_id: int) -> None:
    _POLICY_TOKENS[(policy, int(chat_id))] = object()


def _instant_generation(settings: dict, user_id: int) -> int:
    return settings["instant_generations"].get(str(int(user_id)), 0)


def _bump_instant_generations(cfg: dict, user_ids) -> None:
    stored = cfg.get("autodel_instant_generations")
    generations = dict(stored) if isinstance(stored, dict) else {}
    for user_id in _user_ids(user_ids):
        key = str(user_id)
        try:
            generations[key] = int(generations.get(key, 0)) + 1
        except (TypeError, ValueError):
            generations[key] = 1
    cfg["autodel_instant_generations"] = generations


async def _update_legacy(chat_id: int, **fields) -> dict:
    """Change legacy policy and revoke every timer from its old generation."""

    def update(cfg):
        cfg.update(fields)
        _next_revision(cfg, "autodel_legacy_revision")

    result = await db.mutate_chat(chat_id, update)
    _invalidate_policy("legacy", chat_id)
    return result


async def _clear_instant_users(chat_id: int) -> dict:
    def update(cfg):
        _bump_instant_generations(cfg, _user_ids(cfg.get("autodel_instant_users")))
        cfg["autodel_instant_users"] = []

    result = await db.mutate_chat(chat_id, update)
    _invalidate_policy("instant", chat_id)
    return result


async def _reset_policies(chat_id: int) -> dict:
    """Reset all modes atomically while invalidating their existing jobs."""

    changed_instant = []

    def update(cfg):
        nonlocal changed_instant
        changed_instant = _user_ids(cfg.get("autodel_instant_users"))
        _bump_instant_generations(cfg, changed_instant)
        _next_revision(cfg, "autodel_group_revision")
        _next_revision(cfg, "autodel_legacy_revision")
        cfg.update(
            autodel_group_mode="off",
            autodel_group_time=10,
            autodel_group_error="",
            autodel_instant_users=[],
            autodel_on=False,
            autodel_bots=False,
            autodel_users=[],
            autodel_time=10,
            autodel_scope="all",
        )

    result = await db.mutate_chat(chat_id, update)
    _invalidate_policy("group", chat_id)
    _invalidate_policy("legacy", chat_id)
    _invalidate_policy("instant", chat_id)
    return result


def _display_name(user_id: int, first_name: str = "") -> str:
    return (
        f'<a href="tg://user?id={user_id}">'
        f"{html.escape(first_name or str(user_id))}</a>"
    )


def _group(message) -> bool:
    chat = getattr(message, "chat", None)
    return bool(chat and getattr(chat, "type", None) in _GROUP_TYPES)


async def _resolve_user(message: types.Message, arg: str | None = None):
    """Reply target wins; otherwise resolve one explicit username or ID."""

    reply = getattr(message, "reply_to_message", None)
    if reply is not None and getattr(reply, "from_user", None) is not None:
        return reply.from_user
    if not arg:
        return None
    arg = str(arg).lstrip("@")
    try:
        value = int(arg) if arg.lstrip("-").isdigit() else arg
        return await app.get_users(value)
    except Exception:
        return None


async def _protected_target(chat_id: int, user_id: int) -> bool:
    """Fail closed for Maki, the configured owner, and current admins."""

    try:
        me = await app.get_me()
        if user_id == getattr(me, "id", None):
            return True
    except Exception:
        return True
    if config.OWNER_ID and user_id == config.OWNER_ID:
        return True
    try:
        return await is_admin(chat_id, user_id)
    except Exception:
        return True


async def _target_allowed(message: types.Message, user_id: int) -> bool:
    return not await _protected_target(message.chat.id, user_id)


async def _bot_can_delete(chat_id: int) -> bool:
    """Return the bot's current Telegram Delete messages capability."""

    try:
        return await delete_right(app, chat_id)
    except Exception:
        return False


async def _command_can_delete(message: types.Message) -> bool:
    """Require live admin + Delete messages for policy-changing commands."""

    if not await require_admin(message, "can_delete_messages", "Delete messages"):
        return False
    actor = getattr(message, "from_user", None)
    actor_id = getattr(actor, "id", None)
    if not actor_id:
        return False
    if config.OWNER_ID and actor_id == config.OWNER_ID:
        return True
    try:
        member = await app.get_chat_member(message.chat.id, actor_id)
    except Exception:
        await message.reply_text("Could not verify your Delete messages right.")
        return False
    if getattr(member, "status", None) == enums.ChatMemberStatus.OWNER:
        return True
    privileges = getattr(member, "privileges", None)
    if (
        getattr(member, "status", None) == enums.ChatMemberStatus.ADMINISTRATOR
        and getattr(privileges, "can_delete_messages", False)
    ):
        return True
    await message.reply_text("You need the Delete messages admin right.")
    return False


def _message_from_sender(message, sender_id: int):
    sender = getattr(message, "from_user", None)
    return sender is not None and getattr(sender, "id", None) == sender_id


async def _delete_now(
    chat_id: int,
    msg_id: int,
    sender_id: int,
    *,
    instant_generation: int | None = None,
) -> None:
    """Delete one explicitly selected user's message immediately.

    Authorization and policy are deliberately checked after fetching the
    current message and again immediately before the raw delete request.
    """

    try:
        try:
            fetched = await app.get_messages(chat_id, [msg_id], replies=0)
        except TypeError:
            fetched = await app.get_messages(chat_id, [msg_id])
        if isinstance(fetched, (list, tuple)):
            if len(fetched) != 1:
                return
            current = fetched[0]
        else:
            current = fetched
        if (
            current is None
            or getattr(current, "empty", False)
            or getattr(current, "id", None) != msg_id
            or getattr(getattr(current, "chat", None), "id", None) != chat_id
            or not _message_from_sender(current, sender_id)
            or getattr(getattr(current, "from_user", None), "is_self", False)
        ):
            return

        # Resolve/build before the final authorization check.  This avoids a
        # peer-resolution await after the live target/permission decisions.
        request = await _prepare_delete_request(app, chat_id, [msg_id])
        if not await _bot_can_delete(chat_id):
            return
        if await _protected_target(chat_id, sender_id):
            return
        # This must be the final await before invoking the raw request.  In
        # particular, an off/on ABA change while rights were being checked
        # cannot turn an already observed message into a new target.
        settings = _settings(await db.get_chat(chat_id))
        if (
            sender_id not in settings["instant_users"]
            or (
                instant_generation is not None
                and instant_generation != _instant_generation(settings, sender_id)
            )
        ):
            return
        await app.invoke(request, retries=1, sleep_threshold=0, retry_delay=0)
    except Exception as exc:
        logger.info(
            "autodel instant: could not delete msg %s in chat %s: %s",
            msg_id,
            chat_id,
            exc,
        )


async def _delete_later(
    chat_id: int,
    msg_id: int,
    delay: int,
    sender_id: int | None = None,
    *,
    policy: str = "legacy",
    group_revision: int | None = None,
    legacy_revision: int | None = None,
) -> None:
    """Delete one watched message after the configured delay.

    Re-checking the setting and live admin role at the end of the timer keeps
    a promotion or an OFF button effective even when a timer was already
    waiting.
    """

    await asyncio.sleep(delay)
    try:
        # The original watcher decision is only a scheduling hint.  A target
        # can be removed, promoted, or have its type scope changed while this
        # timer sleeps, and the message can be edited in the meantime.
        if sender_id is None:
            return
        cfg = await db.get_chat(chat_id)
        settings = _settings(cfg)
        if policy == "group":
            if settings["group_mode"] != "custom":
                return
            if delay != settings["group_time"]:
                return
            if (
                group_revision is not None
                and group_revision != settings["group_revision"]
            ):
                return
        elif not settings["on"]:
            return
        elif (
            legacy_revision is not None
            and legacy_revision != settings["legacy_revision"]
        ):
            return
        try:
            fetched = await app.get_messages(chat_id, [msg_id], replies=0)
        except TypeError:
            # Small offline fakes and older SDK shims do not expose replies.
            fetched = await app.get_messages(chat_id, [msg_id])
        if isinstance(fetched, (list, tuple)):
            if len(fetched) != 1:
                return
            current = fetched[0]
        else:
            current = fetched
        if (
            current is None
            or getattr(current, "empty", False)
            or getattr(current, "id", None) != msg_id
            or getattr(getattr(current, "chat", None), "id", None) != chat_id
        ):
            return
        current_sender = getattr(current, "from_user", None)
        if current_sender is None or getattr(current_sender, "id", None) != sender_id:
            return
        if policy != "group" and getattr(current_sender, "is_self", False):
            return
        if policy == "group":
            if not matches_scope(current, "all"):
                return
        else:
            if not matches_scope(current, settings["scope"]):
                return
            candidate = (
                bool(getattr(current_sender, "is_bot", False)) and settings["bots"]
            ) or sender_id in settings["users"]
            if not candidate:
                return

        request = await _prepare_delete_request(app, chat_id, [msg_id])
        # Re-read policy after the sender-role lookup, then make the bot-right
        # check the final permission await before the scoped raw invoke.
        cfg = await db.get_chat(chat_id)
        settings = _settings(cfg)
        if policy == "group":
            allowed = (
                settings["group_mode"] == "custom"
                and settings["group_time"] == delay
                and matches_scope(current, "all")
                and (
                    group_revision is None
                    or group_revision == settings["group_revision"]
                )
            )
        else:
            allowed = (
                settings["on"]
                and matches_scope(current, settings["scope"])
                and (
                    bool(getattr(current_sender, "is_bot", False))
                    and settings["bots"]
                    or sender_id in settings["users"]
                )
                and (
                    legacy_revision is None
                    or legacy_revision == settings["legacy_revision"]
                )
            )
        if not allowed:
            return
        if not await _bot_can_delete(chat_id):
            return
        if policy != "group" and await _protected_target(chat_id, sender_id):
            return
        # Rights lookups can be slow.  Keep the policy revision check as the
        # last operation before invoke, rather than trusting an earlier read.
        settings = _settings(await db.get_chat(chat_id))
        if policy == "group":
            allowed = (
                settings["group_mode"] == "custom"
                and settings["group_time"] == delay
                and (
                    group_revision is None
                    or group_revision == settings["group_revision"]
                )
            )
        else:
            allowed = (
                settings["on"]
                and matches_scope(current, settings["scope"])
                and (
                    bool(getattr(current_sender, "is_bot", False))
                    and settings["bots"]
                    or sender_id in settings["users"]
                )
                and (
                    legacy_revision is None
                    or legacy_revision == settings["legacy_revision"]
                )
            )
        if not allowed:
            return
        await app.invoke(request, retries=1, sleep_threshold=0, retry_delay=0)
    except Exception as exc:
        logger.info(
            "autodel: could not delete msg %s in chat %s: %s",
            msg_id,
            chat_id,
            exc,
        )


def _outbound_messages(result):
    if isinstance(result, (list, tuple)):
        return [item for item in result if item is not None]
    return [result] if result is not None else []


async def _track_outbound(result, fallback_chat_id=None, *, policy_token=None) -> None:
    """Schedule Maki's own returned messages without touching other sessions."""

    if (
        fallback_chat_id is not None
        and policy_token is not None
        and policy_token is not _policy_token("group", fallback_chat_id)
    ):
        return
    now = asyncio.get_running_loop().time()
    for key, seen_at in list(_OUTBOUND_SEEN.items()):
        if now - seen_at > _OUTBOUND_SEEN_TTL:
            _OUTBOUND_SEEN.pop(key, None)
    sender_id = None
    try:
        me = getattr(result, "from_user", None)
        sender_id = getattr(me, "id", None)
        if sender_id is None:
            sender_id = getattr((await app.get_me()), "id", None)
    except Exception:
        return
    for message in _outbound_messages(result):
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None) or fallback_chat_id
        msg_id = getattr(message, "id", None)
        if chat_id is None or msg_id is None:
            continue
        if (
            policy_token is not None
            and policy_token is not _policy_token("group", chat_id)
        ):
            return
        cfg = await db.get_chat(chat_id)
        if (
            policy_token is not None
            and policy_token is not _policy_token("group", chat_id)
        ):
            return
        settings = _settings(cfg)
        if settings["group_mode"] != "custom":
            continue
        key = (int(chat_id), int(msg_id))
        if key in _OUTBOUND_SEEN:
            continue
        _OUTBOUND_SEEN[key] = now
        message_sender_id = getattr(
            getattr(message, "from_user", None), "id", None
        ) or sender_id
        asyncio.create_task(
            _delete_later(
                int(chat_id),
                int(msg_id),
                settings["group_time"],
                sender_id=message_sender_id,
                policy="group",
                group_revision=settings["group_revision"],
            )
        )


def _outbound_chat_id(args, kwargs):
    value = args[0] if args else kwargs.get("chat_id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _install_outbound_wrappers(client) -> None:
    """Install focused wrappers once on Maki's existing bot client."""

    marker = id(client)
    if marker in _OUTBOUND_INSTALLED:
        return
    _OUTBOUND_INSTALLED.add(marker)
    for name in _OUTBOUND_METHODS:
        original = getattr(client, name, None)
        if not callable(original):
            continue

        @functools.wraps(original)
        async def wrapped(*args, __original=original, **kwargs):
            # This is intentionally captured before the send awaits.  A
            # successful Telegram send must return its original result even if
            # tracking later blocks, fails, or is cancelled, and an off/on
            # change while it was in flight must not newly time that message.
            fallback_chat_id = _outbound_chat_id(args, kwargs)
            token = (
                _policy_token("group", fallback_chat_id)
                if fallback_chat_id is not None
                else None
            )
            result = await __original(*args, **kwargs)
            if len(_OUTBOUND_TRACK_TASKS) >= _OUTBOUND_TRACK_LIMIT:
                logger.warning(
                    "autodel outbound tracking queue is full; sent message(s) "
                    "will not be timed"
                )
                return result
            tracking = _track_outbound(
                result, fallback_chat_id, policy_token=token,
            )
            try:
                task = asyncio.create_task(tracking)
            except Exception as exc:
                tracking.close()
                logger.info("autodel outbound tracking could not start: %s", exc)
                return result
            _OUTBOUND_TRACK_TASKS.add(task)

            def tracked(done):
                _OUTBOUND_TRACK_TASKS.discard(done)
                try:
                    done.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.info("autodel outbound tracking: %s", exc)

            task.add_done_callback(tracked)
            return result

        setattr(client, name, wrapped)


async def on_start(client) -> None:
    # Plugins can receive startup hooks from more than one client in larger
    # deployments.  Only Maki's imported bot instance is allowed to be
    # wrapped; never instrument a userbot or another session by accident.
    if client is app:
        _install_outbound_wrappers(client)


async def _instant_worker(queue: asyncio.Queue) -> None:
    while True:
        job = await queue.get()
        chat_id, msg_id, sender_id, generation = job
        try:
            await _delete_now(
                chat_id, msg_id, sender_id, instant_generation=generation
            )
        except Exception as exc:
            logger.info("autodel instant worker: %s", exc)
        finally:
            _INSTANT_PENDING.discard((chat_id, msg_id))
            queue.task_done()


def _enqueue_instant(
    chat_id: int, msg_id: int, sender_id: int, settings: dict,
) -> None:
    """Bound immediate network work without stalling Pyrogram's dispatcher."""

    global _INSTANT_QUEUE, _INSTANT_QUEUE_LOOP
    key = (int(chat_id), int(msg_id))
    if key in _INSTANT_PENDING:
        return
    loop = asyncio.get_running_loop()
    if _INSTANT_QUEUE is None or _INSTANT_QUEUE_LOOP is not loop:
        # Isolated test loops and a clean shutdown cannot safely share queued
        # coroutine waiters.  No queued work survives a process restart by
        # design, so a fresh in-memory queue is the correct baseline.
        _INSTANT_QUEUE = asyncio.Queue(maxsize=_INSTANT_QUEUE_MAX)
        _INSTANT_QUEUE_LOOP = loop
        _INSTANT_PENDING.clear()
    workers = {task for task in _INSTANT_WORKERS if not task.done()}
    _INSTANT_WORKERS.clear()
    _INSTANT_WORKERS.update(workers)
    while len(_INSTANT_WORKERS) < _INSTANT_WORKERS_MAX:
        worker = asyncio.create_task(_instant_worker(_INSTANT_QUEUE))
        _INSTANT_WORKERS.add(worker)
        worker.add_done_callback(_INSTANT_WORKERS.discard)
    try:
        _INSTANT_QUEUE.put_nowait(
            (key[0], key[1], int(sender_id), _instant_generation(settings, sender_id))
        )
    except asyncio.QueueFull:
        # The limitation is explicit: pretending every message was deleted is
        # worse than a visible operational warning during a Telegram flood.
        logger.warning(
            "autodel instant queue is full; cannot immediately process %s in %s",
            msg_id, chat_id,
        )
        return
    _INSTANT_PENDING.add(key)


def _schedule_delete_later(
    chat_id: int,
    msg_id: int,
    delay: int,
    sender_id: int,
    settings: dict,
    *,
    policy: str,
) -> None:
    """Capture the policy generation before yielding to a timer task."""

    kwargs = {"policy": policy}
    if policy == "group":
        kwargs["group_revision"] = settings["group_revision"]
    else:
        kwargs["legacy_revision"] = settings["legacy_revision"]
    asyncio.create_task(
        _delete_later(chat_id, msg_id, delay, sender_id=sender_id, **kwargs)
    )


async def autodel_watcher(_, m: types.Message) -> None:
    """Apply independent instant-user and bot-side group/legacy policies."""

    try:
        cfg = await db.get_chat(m.chat.id)
        settings = _settings(cfg)
        sender = getattr(m, "from_user", None)
        if sender is None:
            # Anonymous admins/channel posts have no personal sender identity.
            return
        if sender.id in settings["instant_users"]:
            _enqueue_instant(m.chat.id, m.id, sender.id, settings)
        if settings["group_mode"] == "custom":
            # Human messages are covered, including admins and owner.  Other
            # bots' posts are not delivered by Telegram; Maki's own outbound
            # messages are scheduled by the focused send wrapper below.
            if not (
                getattr(sender, "is_self", False)
                or getattr(sender, "is_bot", False)
            ):
                _schedule_delete_later(
                    m.chat.id, m.id, settings["group_time"], sender.id, settings,
                    policy="group",
                )
        # Legacy is deliberately independent from both group and instant mode.
        # A group timer neither switches its historic settings off nor makes a
        # selected legacy target lose its own configured timer.
        if not settings["on"] or not (settings["bots"] or settings["users"]):
            return
        if not matches_scope(m, settings["scope"]):
            return
        candidate = (
            bool(getattr(sender, "is_bot", False)) and settings["bots"]
        ) or sender.id in settings["users"]
        if not candidate:
            return
        _schedule_delete_later(
            m.chat.id, m.id, settings["time"], sender.id, settings,
            policy="legacy",
        )
    except Exception as exc:
        logger.info("autodel watcher: %s", exc)


autodel_watcher = app.on_message(
    filters.group & ~filters.service, group=5
)(autodel_watcher)


def _view_token() -> str:
    # Keep callback_data under Telegram's 64-byte limit even for
    # clear_yes/reset_yes actions.
    return uuid.uuid4().hex[:8]


@dataclass
class MenuState:
    token: str
    chat_id: int
    actor_id: int
    target_id: int | None = None
    target_name: str = ""
    card_id: int = 0
    card: object | None = None
    created: float = 0.0
    view: str = field(default_factory=_view_token)
    mode: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


_menus: dict[str, MenuState] = {}


def _menu_cleanup() -> None:
    now = asyncio.get_running_loop().time()
    for token, item in list(_menus.items()):
        if now - item.created > _MENU_TTL:
            _menus.pop(token, None)


def _callback(item: MenuState, action: str, *values: str) -> str:
    parts = ["gh", "ad", action, *values, item.token, item.view]
    return ":".join(parts)


def _menu_keyboard(item: MenuState, cfg: dict):
    settings = _settings(cfg)
    mode = settings["group_mode"]
    group_buttons = [
        ikb(
            ("✓ " if mode == "custom" and settings["group_time"] == delay else "")
            + (
                f"{delay // 86400}d"
                if delay >= 86400
                else f"{delay // 3600}h"
                if delay >= 3600
                else f"{delay // 60}m"
                if delay >= 60
                else f"{delay}s"
            ),
            callback_data=_callback(item, "group_time", str(delay)),
            style=enums.ButtonStyle.SUCCESS,
        )
        for delay in _GROUP_BUTTONS
    ]
    rows = [
        [
            ikb(
                "Group: ON",
                callback_data=_callback(item, "group_reenable"),
                style=enums.ButtonStyle.SUCCESS,
            ),
            ikb(
                "Group: OFF",
                callback_data=_callback(item, "group_off"),
                style=enums.ButtonStyle.PRIMARY,
            ),
        ],
        [
            ikb(
                f"Instant users ({len(settings['instant_users'])})",
                callback_data=_callback(item, "instant_status"),
                style=enums.ButtonStyle.PRIMARY,
            ),
            ikb(
                "Clear instant users",
                callback_data=_callback(item, "instant_clear"),
                style=enums.ButtonStyle.DANGER,
            ),
        ],
    ]
    rows[1:1] = [
        group_buttons[start:start + 4]
        for start in range(0, len(group_buttons), 4)
    ]
    if item.target_id:
        target = html.escape(item.target_name or str(item.target_id))
        rows.extend(
            [
                [
                    ikb(
                        f"Legacy: Watch {target}",
                        callback_data=_callback(item, "target", "add"),
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                    ikb(
                        f"Legacy: Stop {target}",
                        callback_data=_callback(item, "target", "remove"),
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                ],
                [
                    ikb(
                        f"Instant: Watch {target}",
                        callback_data=_callback(item, "instant_target", "add"),
                        style=enums.ButtonStyle.SUCCESS,
                    ),
                    ikb(
                        f"Instant: Stop {target}",
                        callback_data=_callback(item, "instant_target", "remove"),
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                ],
            ]
        )
    rows.extend(
        [
            [
                ikb(
                    f"Other bots (legacy): {'ON' if settings['bots'] else 'OFF'}",
                    callback_data=_callback(item, "bots"),
                    style=enums.ButtonStyle.PRIMARY,
                )
            ],
            [
                ikb(
                    ("✓ " if settings["time"] == delay else "") + f"Legacy {delay}s",
                    callback_data=_callback(item, "delay", str(delay)),
                    style=enums.ButtonStyle.PRIMARY,
                )
                for delay in _DELAY_BUTTONS[:4]
            ],
            [
            ikb(
                ("✓ " if settings["time"] == delay else "")
                + (f"{delay // 60}m" if delay >= 60 else f"{delay}s"),
                callback_data=_callback(item, "delay", str(delay)),
                style=enums.ButtonStyle.PRIMARY,
            )
            for delay in _DELAY_BUTTONS[4:]
            ],
            [
                ikb(
                    ("✓ " if settings["scope"] == value else "") + label,
                    callback_data=_callback(item, "scope", value),
                    style=enums.ButtonStyle.PRIMARY,
                )
                for value, label in (
                    ("all", "All"),
                    ("text", "Text"),
                    ("media", "Media"),
                )
            ],
            [
                ikb(
                    ("✓ " if settings["scope"] == value else "") + label,
                    callback_data=_callback(item, "scope", value),
                    style=enums.ButtonStyle.PRIMARY,
                )
                for value, label in (
                    ("stickers", "Stickers"),
                    ("gifs", "GIFs"),
                    ("forwards", "Forwards"),
                )
            ],
            [
                ikb(
                    "Clear watched users",
                    callback_data=_callback(item, "clear"),
                    style=enums.ButtonStyle.DANGER,
                ),
                ikb(
                    "Reset",
                    callback_data=_callback(item, "reset"),
                    style=enums.ButtonStyle.DANGER,
                ),
            ],
            [
                ikb(
                    "Close",
                    callback_data=_callback(item, "close"),
                    style=enums.ButtonStyle.PRIMARY,
                )
            ],
        ]
    )
    return ikm(rows)


def _menu_text(item: MenuState, cfg: dict) -> str:
    settings = _settings(cfg)
    group_label = "ON" if settings["group_mode"] == "custom" else "OFF"
    target = (
        f"\nTarget: {_display_name(item.target_id, item.target_name)}"
        if item.target_id
        else "\nTarget: <i>reply to /autodel or pass @user/id to choose one</i>"
    )
    return (
        "<b>Auto-delete</b>\n"
        f"Group timer: <b>{group_label}</b>\n"
        f"Group duration: <b>{settings['group_time']}s</b>\n"
        f"Instant users: <b>{len(settings['instant_users'])}</b>\n"
        f"Legacy delayed users: <b>{len(settings['users'])}</b>\n"
        f"Legacy other bots: <b>{'ON' if settings['bots'] else 'OFF'}</b>\n"
        f"Legacy delay: <b>{settings['time']}s</b>\n"
        f"Legacy types: <b>{html.escape(settings['scope'])}</b>\n"
        f"{target}\n\n"
        "Bot-side coverage includes human messages Maki receives, including "
        "admins and the owner, plus Maki's own outgoing messages. Telegram "
        "does not deliver other bots' posts to this bot. "
        + (
            "A historic unsupported native setting was found but is inactive. "
            if settings["group_native_warning"]
            else ""
        )
        +
        "Instant users are independent; current admins, the owner, and Maki "
        "are protected."
    )


async def _show_menu(message: types.Message, target=None) -> None:
    _menu_cleanup()
    actor = getattr(message, "from_user", None)
    if actor is None:
        return
    if target is not None and not await _target_allowed(message, target.id):
        # Do not render a Watch/Stop control that can only fail: admins,
        # Maki, and the configured owner are protected targets.
        target = None
    item = MenuState(
        token=uuid.uuid4().hex,
        chat_id=message.chat.id,
        actor_id=actor.id,
        target_id=getattr(target, "id", None),
        target_name=getattr(target, "first_name", "") if target else "",
        created=asyncio.get_running_loop().time(),
    )
    _menus[item.token] = item
    try:
        cfg = await db.get_chat(item.chat_id)
        sent = await message.reply_text(
            _menu_text(item, cfg),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_menu_keyboard(item, cfg),
        )
        if _menus.get(item.token) is not item:
            return
        item.card = sent
        item.card_id = getattr(sent, "id", 0)
    except Exception:
        _menus.pop(item.token, None)
        try:
            await message.reply_text("Could not open Auto-delete settings.")
        except Exception:
            pass


async def _command_target(message, args: list[str]):
    if getattr(message, "reply_to_message", None) is not None:
        return await _resolve_user(message)
    return await _resolve_user(message, args[0] if args else None)


async def _mutate_target(message, user, adding: bool, *, final_auth=None) -> str:
    if user is None:
        return "Reply to a user's message or pass @username / numeric ID."
    if not await _target_allowed(message, user.id):
        return "Maki cannot watch itself, the configured owner, or an admin."
    if final_auth is not None and not await final_auth():
        return "You are no longer a group admin or this settings card is stale."
    outcome = {}

    def update(cfg):
        users = cfg.setdefault("autodel_users", [])
        users = [int(value) for value in users if str(value).lstrip("-").isdigit()]
        cfg["autodel_users"] = users
        _next_revision(cfg, "autodel_legacy_revision")
        if adding:
            if user.id in users:
                outcome["status"] = "already"
                return
            users.append(user.id)
            outcome["status"] = "added"
        else:
            if user.id not in users:
                outcome["status"] = "absent"
                return
            users.remove(user.id)
            outcome["status"] = "removed"

    await db.mutate_chat(message.chat.id, update)
    _invalidate_policy("legacy", message.chat.id)
    if outcome["status"] == "already":
        return "That user is already watched."
    if outcome["status"] == "absent":
        return "That user is not watched."
    return (
        f"{_display_name(user.id, user.first_name or 'user')} "
        f"{'added to' if adding else 'removed from'} Auto-delete."
    )


async def _mutate_instant(message, user, adding: bool, *, final_auth=None) -> str:
    if user is None:
        return "Reply to a user's message or pass @username / numeric ID."
    if adding and not await _target_allowed(message, user.id):
        return "Maki, the configured owner, and current admins are protected."
    if final_auth is not None and not await final_auth():
        return "You are no longer a group admin or this settings card is stale."
    outcome = {}

    def update(cfg):
        users = _user_ids(cfg.get("autodel_instant_users", []))
        cfg["autodel_instant_users"] = users
        # Bump even for a no-op request.  It prevents an old job from
        # surviving remove/re-add ABA and gives an explicit command a fresh
        # target generation.
        _bump_instant_generations(cfg, [user.id])
        if adding:
            if user.id in users:
                outcome["status"] = "already"
                return
            users.append(user.id)
            outcome["status"] = "added"
        else:
            if user.id not in users:
                outcome["status"] = "absent"
                return
            users.remove(user.id)
            outcome["status"] = "removed"
        cfg["autodel_instant_users"] = users

    await db.mutate_chat(message.chat.id, update)
    _invalidate_policy("instant", message.chat.id)
    if outcome["status"] == "already":
        return "That user is already in the instant list."
    if outcome["status"] == "absent":
        return "That user is not in the instant list."
    return (
        f"{_display_name(user.id, user.first_name or 'user')} "
        f"{'added to' if adding else 'removed from'} instant auto-delete."
    )


async def _set_group_policy(
    message,
    seconds: int,
    *,
    actor_id: int | None = None,
) -> tuple[bool, str]:
    """Enable the bounded bot-side group watcher."""

    if not _GROUP_MIN_DELAY <= seconds <= _GROUP_MAX_DELAY:
        return False, "Choose a group timer from 5 seconds through 24 hours."
    chat_id = message.chat.id
    async with _group_lock(chat_id):
        def update(cfg):
            _next_revision(cfg, "autodel_group_revision")
            cfg.update(
                autodel_group_mode="custom",
                autodel_group_time=seconds,
                autodel_group_error="",
            )

        await db.mutate_chat(chat_id, update)
    _invalidate_policy("group", chat_id)
    return True, (
        f"Bot-side group timer set to {seconds}s. It covers human messages "
        "Maki receives, including admins and the owner, plus Maki's own "
        "outgoing messages. Telegram does not deliver other bots' posts to "
        "this bot, so those are not covered."
    )


async def _disable_group_policy(
    message,
    *,
    actor_id: int | None = None,
) -> tuple[bool, str]:
    chat_id = message.chat.id
    async with _group_lock(chat_id):
        def update(cfg):
            _next_revision(cfg, "autodel_group_revision")
            cfg.update(
                autodel_group_mode="off",
                autodel_group_error="",
            )

        await db.mutate_chat(chat_id, update)
    _invalidate_policy("group", chat_id)
    return True, "Group timer disabled. Instant users were left unchanged."


def _instant_status_text(settings: dict) -> str:
    users = settings["instant_users"]
    if not users:
        return "Instant auto-delete is OFF: no selected users."
    ids = ", ".join(str(value) for value in users)
    return (
        "Instant auto-delete is ON for "
        f"{len(users)} selected user(s): <code>{html.escape(ids)}</code>.\n"
        "Their messages are queued for immediate deletion when this bot receives "
        "them (Telegram rate limits can delay work); "
        "admins, the owner, and Maki are always protected."
    )


async def autodel_command(_, m: types.Message) -> None:
    if not await _command_can_delete(m):
        return
    raw_args = list((getattr(m, "command", None) or [])[1:])
    args = [str(value).lower() for value in raw_args]
    target = None
    if not args or (args and args[0].startswith("@")) or (
        args and args[0].lstrip("-").isdigit()
    ):
        target = await _command_target(m, raw_args)
        await _show_menu(m, target)
        return
    if args == ["off"] or args == ["group", "off"]:
        _, text = await _disable_group_policy(m)
        await m.reply_text(text)
        return
    group_arg = args[1] if len(args) == 2 and args[0] == "group" else (
        args[0] if len(args) == 1 else None
    )
    if group_arg is not None:
        seconds = _duration(group_arg)
        if seconds is None:
            await m.reply_text(
                "Use /autodel <duration> or /autodel group <duration>, "
                "for example 30s, 5m, 1h, or 1d. Use /autodel off to disable."
            )
            return
        _, text = await _set_group_policy(m, seconds)
        await m.reply_text(text)
        return
    if len(args) == 2 and args[0] == "bots" and args[1] in ("on", "off"):
        await _update_legacy(m.chat.id, autodel_bots=args[1] == "on")
        await m.reply_text(
            f"Other-bot auto-delete {'enabled' if args[1] == 'on' else 'disabled'}."
        )
        return
    if args[0] in ("add", "del", "remove"):
        user = await _resolve_user(m, raw_args[1] if len(raw_args) > 1 else None)
        actor_id = getattr(getattr(m, "from_user", None), "id", None)

        async def final_auth():
            return actor_id is not None and await has_right(
                m.chat.id, actor_id, "can_delete_messages")

        await m.reply_text(
            await _mutate_target(
                m,
                user,
                args[0] == "add",
                final_auth=final_auth,
            ),
            parse_mode=enums.ParseMode.HTML,
        )
        return
    if len(args) == 2 and args[0] == "time":
        try:
            delay = int(args[1])
        except ValueError:
            delay = 0
        if not _MIN_DELAY <= delay <= _MAX_DELAY:
            await m.reply_text(
                f"Use a delay from {_MIN_DELAY} to {_MAX_DELAY} seconds."
            )
            return
        await _update_legacy(m.chat.id, autodel_time=delay)
        await m.reply_text(f"Auto-delete delay set to {delay}s.")
        return
    if len(args) == 2 and args[0] in ("scope", "types") and args[1] in SCOPES:
        await _update_legacy(m.chat.id, autodel_scope=args[1])
        await m.reply_text(f"Auto-delete scope set to {args[1]}.")
        return
    if args[0] == "clear" and len(args) == 1:
        # A clear must go through the same destructive confirmation as its
        # button, rather than silently dropping every watched target.
        await _show_menu(m)
        return
    await _show_menu(m)


autodel_command = app.on_message(
    filters.group & filters.command("autodel", prefixes=["/", "?", "!", "."])
)(autodel_command)


async def autodeluser_command(_, m: types.Message) -> None:
    if not await _command_can_delete(m):
        return
    raw_args = list((getattr(m, "command", None) or [])[1:])
    args = [str(value).lower() for value in raw_args]
    reply_target = getattr(getattr(m, "reply_to_message", None), "from_user", None)
    # A bare command replying to a person is the primary target-selection
    # form.  Status is only the bare command when there is no reply target.
    if (
        (not args and reply_target is None)
        or (args and args[0] in ("status", "list"))
    ):
        await m.reply_text(
            _instant_status_text(_settings(await db.get_chat(m.chat.id))),
            parse_mode=enums.ParseMode.HTML,
        )
        return
    if args == ["clear"]:
        await _clear_instant_users(m.chat.id)
        await m.reply_text("Instant user list cleared.")
        return
    user = await _command_target(m, raw_args)
    actor_id = getattr(getattr(m, "from_user", None), "id", None)

    async def final_auth():
        return actor_id is not None and await has_right(
            m.chat.id, actor_id, "can_delete_messages")

    await m.reply_text(
        await _mutate_instant(m, user, True, final_auth=final_auth),
        parse_mode=enums.ParseMode.HTML,
    )


autodeluser_command = app.on_message(
    filters.group & filters.command("autodeluser", prefixes=["/", "?", "!", "."])
)(autodeluser_command)


async def unautodeluser_command(_, m: types.Message) -> None:
    if not await _command_can_delete(m):
        return
    raw_args = list((getattr(m, "command", None) or [])[1:])
    user = await _command_target(m, raw_args)
    actor_id = getattr(getattr(m, "from_user", None), "id", None)

    async def final_auth():
        return actor_id is not None and await has_right(
            m.chat.id, actor_id, "can_delete_messages")

    await m.reply_text(
        await _mutate_instant(m, user, False, final_auth=final_auth),
        parse_mode=enums.ParseMode.HTML,
    )


unautodeluser_command = app.on_message(
    filters.group & filters.command("unautodeluser", prefixes=["/", "?", "!", "."])
)(unautodeluser_command)


def _callback_item(
    query,
    token: str,
    view: str | None = None,
    action: str | None = None,
) -> MenuState | None:
    item = _menus.get(token)
    message = getattr(query, "message", None)
    user = getattr(query, "from_user", None)
    now = asyncio.get_running_loop().time()
    if item and now - item.created > _MENU_TTL:
        _menus.pop(token, None)
        item = None
    if item:
        if item.mode:
            allowed = {f"{item.mode}_yes", f"{item.mode}_no"}
        else:
            allowed = {
                "on", "off", "bots", "delay", "scope", "target",
                "group_reenable", "group_off", "group_time",
                "instant_status", "instant_clear", "instant_target",
                "clear", "reset", "close",
            }
    if (
        not item
        or not _TOKEN_RE.fullmatch(token)
        or view is None
        or action is None
        or not _VIEW_RE.fullmatch(view)
        or item.view != view
        or action not in allowed
        or not message
        or getattr(getattr(message, "chat", None), "id", None) != item.chat_id
        or getattr(message, "id", None) != item.card_id
        or not user
        or user.id != item.actor_id
    ):
        return None
    return item


async def _answer(query, text: str, *, alert: bool = False) -> None:
    try:
        await query.answer(text, show_alert=alert)
    except Exception:
        pass


async def _fresh_admin(item: MenuState) -> bool:
    try:
        if not await is_admin(item.chat_id, item.actor_id):
            return False
        if config.OWNER_ID and item.actor_id == config.OWNER_ID:
            return True
        member = await app.get_chat_member(item.chat_id, item.actor_id)
        if getattr(member, "status", None) == enums.ChatMemberStatus.OWNER:
            return True
        return bool(
            getattr(member, "status", None)
            == enums.ChatMemberStatus.ADMINISTRATOR
            and getattr(
                getattr(member, "privileges", None),
                "can_delete_messages",
                False,
            )
        )
    except Exception:
        return False


async def _refresh_menu(item: MenuState) -> None:
    if not item.card:
        return
    cfg = await db.get_chat(item.chat_id)
    item.mode = ""
    item.view = _view_token()
    await item.card.edit_text(
        _menu_text(item, cfg),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=_menu_keyboard(item, cfg),
    )


@app.on_callback_query(filters.regex(_CALLBACK_RE))
async def autodel_callback(_, query: types.CallbackQuery):
    data = str(getattr(query, "data", "") or "")
    parts = data.split(":")
    # gh:ad:<action>[:value...]:<token>:<view>
    if len(parts) < 5:
        await _answer(query, "That settings button is invalid.", alert=True)
        return
    token, view = parts[-2:]
    action = parts[2]
    values = parts[3:-2]
    item = _callback_item(query, token, view, action)
    if item is None:
        await _answer(query, "This settings card expired or belongs elsewhere.", alert=True)
        return
    async with item.lock:
        # A second callback may have waited while the first one edited the
        # card.  Revalidate the exact old view/state after acquiring the lock.
        if _callback_item(query, token, view, action) is not item:
            await _answer(query, "That button is no longer current.", alert=True)
            return
        if not await _fresh_admin(item):
            await _answer(query, "You are no longer a group admin.", alert=True)
            return
        if action == "close":
            _menus.pop(token, None)
            try:
                await query.message.edit_text("Auto-delete settings closed.")
            except Exception:
                pass
            await _answer(query, "Closed.")
            return
        if action in ("group_reenable", "group_time"):
            if _callback_item(query, token, view, action) is not item:
                return
            if action == "group_reenable":
                cfg = await db.get_chat(item.chat_id)
                seconds = _settings(cfg)["group_time"]
                ok, text = await _set_group_policy(
                    SimpleNamespace(chat=SimpleNamespace(id=item.chat_id)),
                    seconds,
                    actor_id=item.actor_id,
                )
            elif action == "group_time" and values:
                seconds = _duration(values[0])
                if seconds is None:
                    await _answer(query, "That duration is not available.", alert=True)
                    return
                ok, text = await _set_group_policy(
                    SimpleNamespace(chat=SimpleNamespace(id=item.chat_id)),
                    seconds,
                    actor_id=item.actor_id,
                )
            else:
                ok, text = False, "That group setting is not available."
            if not ok:
                await _answer(query, text, alert=True)
                return
            await _refresh_menu(item)
            await _answer(query, text)
            return
        if action == "group_off":
            if _callback_item(query, token, view, action) is not item:
                return
            ok, text = await _disable_group_policy(
                SimpleNamespace(chat=SimpleNamespace(id=item.chat_id)),
                actor_id=item.actor_id,
            )
            if not ok:
                await _answer(query, text, alert=True)
                return
            await _refresh_menu(item)
            await _answer(query, text)
            return
        if action == "instant_status":
            settings = _settings(await db.get_chat(item.chat_id))
            await _answer(query, _instant_status_text(settings), alert=True)
            return
        if action == "instant_clear":
            await _clear_instant_users(item.chat_id)
        elif action == "on":
            if _callback_item(query, token, view, action) is not item:
                return
            await _update_legacy(item.chat_id, autodel_on=True)
        elif action == "off":
            if _callback_item(query, token, view, action) is not item:
                return
            await _update_legacy(item.chat_id, autodel_on=False)
        elif action == "bots":
            cfg = await db.get_chat(item.chat_id)
            if not await _fresh_admin(item):
                await _answer(query, "You are no longer a group admin.", alert=True)
                return
            if _callback_item(query, token, view, action) is not item:
                return
            await _update_legacy(
                item.chat_id, autodel_bots=not _settings(cfg)["bots"],
            )
        elif action == "delay" and values:
            try:
                delay = int(values[0])
            except ValueError:
                delay = 0
            if not _MIN_DELAY <= delay <= _MAX_DELAY:
                await _answer(query, "That delay is not available.", alert=True)
                return
            if _callback_item(query, token, view, action) is not item:
                return
            await _update_legacy(item.chat_id, autodel_time=delay)
        elif action == "scope" and values and values[0] in SCOPES:
            if _callback_item(query, token, view, action) is not item:
                return
            await _update_legacy(item.chat_id, autodel_scope=values[0])
        elif action in ("target", "instant_target") and values:
            if not item.target_id:
                await _answer(query, "No concrete target was selected.", alert=True)
                return
            try:
                user = await app.get_users(item.target_id)
            except Exception:
                await _answer(query, "Could not verify that target.", alert=True)
                return

            async def final_auth():
                return (
                    _callback_item(query, token, view, action) is item
                    and await _fresh_admin(item)
                )

            mutation = _mutate_instant if action == "instant_target" else _mutate_target
            result = await mutation(
                SimpleNamespace(chat=SimpleNamespace(id=item.chat_id)),
                user,
                values[0] == "add",
                final_auth=final_auth,
            )
            if (
                result.endswith("cannot watch itself, the configured owner, or an admin.")
                or result.startswith("Maki, the configured owner")
            ):
                await _answer(query, result, alert=True)
                return
            if result.startswith("You are no longer"):
                await _answer(query, result, alert=True)
                return
        elif action == "clear":
            item.mode = "clear"
            item.view = _view_token()
            try:
                await query.message.edit_text(
                    "<b>Clear every watched user?</b>\n"
                    "This cannot be undone.",
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=ikm(
                        [
                            [
                                ikb(
                                    "Clear watched users",
                                    callback_data=_callback(item, "clear_yes"),
                                    style=enums.ButtonStyle.DANGER,
                                ),
                                ikb(
                                    "Cancel",
                                    callback_data=_callback(item, "clear_no"),
                                    style=enums.ButtonStyle.PRIMARY,
                                ),
                            ]
                        ]
                    ),
                )
            except Exception:
                pass
            await _answer(query, "Confirm the clear action.")
            return
        elif action == "clear_yes":
            if not await _fresh_admin(item):
                await _answer(query, "You are no longer a group admin.", alert=True)
                return
            await _update_legacy(item.chat_id, autodel_users=[])
        elif action == "clear_no":
            await _refresh_menu(item)
            await _answer(query, "Nothing was cleared.")
            return
        elif action == "reset":
            item.mode = "reset"
            item.view = _view_token()
            try:
                await query.message.edit_text(
                    "<b>Reset all Auto-delete settings?</b>\n"
                    "This disables deletion, removes watched users, and restores "
                    "the 10-second / all-types defaults.",
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=ikm(
                        [
                            [
                                ikb(
                                    "Reset settings",
                                    callback_data=_callback(item, "reset_yes"),
                                    style=enums.ButtonStyle.DANGER,
                                ),
                                ikb(
                                    "Cancel",
                                    callback_data=_callback(item, "reset_no"),
                                    style=enums.ButtonStyle.PRIMARY,
                                ),
                            ]
                        ]
                    ),
                )
            except Exception:
                pass
            await _answer(query, "Confirm the reset action.")
            return
        elif action == "reset_yes":
            if not await _fresh_admin(item):
                await _answer(query, "You are no longer a group admin.", alert=True)
                return
            await _reset_policies(item.chat_id)
        elif action == "reset_no":
            await _refresh_menu(item)
            await _answer(query, "Nothing was reset.")
            return
        else:
            await _answer(query, "That settings action is not available.", alert=True)
            return
        try:
            await _refresh_menu(item)
        except Exception:
            pass
        await _answer(query, "Auto-delete settings updated.")