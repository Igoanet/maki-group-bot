"""Creator-confirmed, bot-authenticated fixed-boundary group deletion.

``/purgeall`` (and the backwards-compatible ``/deleteall``, ``/deletall``,
``/delall`` and ``/delete all`` aliases) is intentionally a two-step UI.  It
only permits the
live Telegram creator of the current group to confirm.  The destructive work
uses the bot-only engine in :mod:`ghelp.bulk_delete`; no connected user
account, assistant bridge, or user session is involved.

The command's numeric boundary is frozen before confirmation.  The
confirmation and progress cards are therefore always outside the range being
processed.  A bot cannot promise an empty Telegram history: protected or
service messages may be refused, and IDs checked are not counts of messages
found or deleted.
"""

import asyncio
import html
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
import time
import uuid

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.bulk_delete import DeleteReport, delete_right, live_owner, run_delete
from ghelp.selfdel import arm_delete, autodel_reply

logger = logging.getLogger(__name__)

PRE = ["/", "?", "!", "."]
CONFIRM_TTL = 300
EDIT_GAP = 4.0
TOKEN_LEN = 32
GROUP_TYPES = (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)
CHECKPOINT_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "bulk_purge_runtime.json"
)

# Keep reading this old file only for an explicit ``resume`` request.  Older
# releases wrote it for the retired user-account UI.  It is never written,
# never loaded at startup, and its only reusable fact is a validated positive
# original command boundary.  In particular, a failed/ambiguous old action
# is never retried or contacted.
LEGACY_RECEIPT_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "full_clear_native_receipts.json"
)

_DELETE_ALL_TEXT_RE = re.compile(
    r"^(?:[/?!])delete(?:@[A-Za-z0-9_]+)?\s+all(?:\s+resume)?\s*$",
    re.IGNORECASE,
)
_CONFIRM_RE = r"^gh:bp:(?:confirm|cancel|stop):[0-9a-f]{32}$"
_LEGACY_CALLBACK_RE = r"^gh:fc:(?:confirm|cancel|stop):[0-9a-f]{32}$"
_checkpoint_lock = asyncio.Lock()


@dataclass
class PendingPurge:
    chat_id: int
    owner_id: int
    boundary: int
    token: str
    created: float
    start_cursor: int
    prior_report: DeleteReport | None = None
    resume: bool = False
    available_cursor: int | None = None
    card_id: int = 0
    stop: asyncio.Event | None = None
    task: asyncio.Task | None = None
    last_edit: float = 0.0
    # Progress edits are deliberately coalesced.  The delete worker can
    # publish after every read/delete operation without waiting on Telegram
    # UI RPCs; this state belongs only to the presentation layer.
    started_at: float = 0.0
    latest_report: DeleteReport | None = None
    progress_task: asyncio.Task | None = None
    progress_revision: int = 0
    progress_force: bool = False
    progress_finalized: bool = False
    progress_last_phase: str = ""
    progress_last_waiting: bool | None = None
    progress_last_wait_seconds: int | None = None
    progress_wakeup: asyncio.Event | None = None
    migrated_checkpoint: bool = False


# These maps are deliberately process-local.  A restart loses a pending
# consent token and cannot accidentally resume a destructive task.
_pending: dict[str, PendingPurge] = {}
_running: dict[int, PendingPurge] = {}


@autodel_reply
async def _reply(message: types.Message, text: str, **kwargs):
    return await message.reply_text(
        text, parse_mode=enums.ParseMode.HTML, **kwargs
    )


def _group(message) -> bool:
    return bool(
        message
        and getattr(message, "chat", None)
        and message.chat.type in GROUP_TYPES
    )


def _bulk_command_args(message) -> list[str] | None:
    """Normalize supported purge commands, failing closed for ``/delete``."""

    raw = getattr(message, "command", None)
    if not raw:
        return None
    words = [str(value).lower() for value in raw]
    name = words[0].split("@", 1)[0]
    if name in {"purgeall", "deleteall", "deletall", "delall"}:
        return words[1:]
    # The spaced alias is deliberately exact: /delete and /delete 5 are not
    # purge commands, even when this handler is called directly in a test or
    # by another local dispatcher.
    if name == "delete" and len(words) >= 2 and words[1] == "all":
        return words[2:]
    return None


def _expired(item: PendingPurge, now: float | None = None) -> bool:
    return (time.monotonic() if now is None else now) - item.created > CONFIRM_TTL


def _reap() -> None:
    now = time.monotonic()
    for token, item in list(_pending.items()):
        if _expired(item, now) and item.chat_id not in _running:
            _pending.pop(token, None)


def _read_checkpoints() -> dict:
    try:
        with CHECKPOINT_PATH.open(encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _checkpoint_for(chat_id: int) -> dict | None:
    row = _read_checkpoints().get(str(chat_id))
    if not isinstance(row, dict):
        return None
    try:
        boundary = row["boundary"]
        cursor = row["next_cursor"]
        if (
            isinstance(boundary, bool)
            or not isinstance(boundary, int)
            or boundary < 1
            or boundary > 2_147_483_647
            or isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or not 0 <= cursor <= boundary
        ):
            return None
    except (KeyError, TypeError):
        return None
    # Pre-two-pass checkpoints have one ambiguous cursor: it may be after an
    # ordinary read, a service refusal, or an unreadable-ID request.  Never
    # reinterpret it as deferred work.  Resume instead starts pass one at the
    # original boundary with fresh consent and no comparable old totals.
    if row.get("algorithm") != "two_pass_v2":
        return {
            "boundary": boundary,
            "next_cursor": boundary,
            "report": None,
            "updated": row.get("updated"),
            "migrated": True,
        }
    report_data = row.get("report") or {}
    if not isinstance(report_data, dict):
        report_data = {}
    fields = {
        key: report_data.get(key, default)
        for key, default in (
            ("total_ids", boundary),
            ("scanned_ids", 0),
            ("found", 0),
            ("deleted", 0),
            ("remaining", 0),
            ("refused", 0),
            ("inaccessible", 0),
            ("unverified", 0),
            ("finished", False),
            ("absent_or_hidden", 0),
            ("hidden_attempted", 0),
            ("pass_phase", row.get("phase", "ordinary")),
            ("phase_cursor", cursor),
            ("phase_pending_ids", []),
            ("phase_pending_hidden_ids", []),
            ("ordinary_deleted", 0),
            ("deferred_services", 0),
            ("deferred_hidden", 0),
            ("service_examined", 0),
            ("last_refusal_kind", ""),
            ("reason", ""),
        )
    }
    try:
        for key in (
            "total_ids",
            "scanned_ids",
            "found",
            "deleted",
            "remaining",
            "refused",
            "inaccessible",
            "unverified",
            "absent_or_hidden",
            "hidden_attempted",
            "phase_cursor",
            "ordinary_deleted",
            "deferred_services",
            "deferred_hidden",
            "service_examined",
        ):
            if (
                isinstance(fields[key], bool)
                or not isinstance(fields[key], int)
                or fields[key] < 0
            ):
                return None
        if (
            fields["total_ids"] != boundary
            or fields["scanned_ids"] > boundary
            or fields["phase_cursor"] > boundary
        ):
            return None
        if (
            not isinstance(fields["finished"], bool)
            or not isinstance(fields["reason"], str)
            or not isinstance(fields["last_refusal_kind"], str)
            or fields["pass_phase"] not in {"ordinary", "deferred"}
            or not isinstance(fields["phase_pending_ids"], list)
            or len(fields["phase_pending_ids"]) > 100
            or len(set(fields["phase_pending_ids"])) != len(fields["phase_pending_ids"])
            or any(
                isinstance(mid, bool) or not isinstance(mid, int)
                or not 1 <= mid <= boundary
                for mid in fields["phase_pending_ids"]
            )
            or not isinstance(fields["phase_pending_hidden_ids"], list)
            or len(fields["phase_pending_hidden_ids"]) > 100
            or len(set(fields["phase_pending_hidden_ids"]))
            != len(fields["phase_pending_hidden_ids"])
            or not set(fields["phase_pending_hidden_ids"]).issubset(
                fields["phase_pending_ids"]
            )
        ):
            return None
        if "absent_or_hidden" not in report_data:
            # Older checkpoints also store only fully completed batches.
            fields["absent_or_hidden"] = max(
                0, fields["scanned_ids"] - fields["found"] - fields["inaccessible"]
            )
        report = DeleteReport(**fields)
    except (TypeError, ValueError):
        return None
    return {
        "boundary": boundary,
        "next_cursor": cursor,
        "report": report,
        "updated": row.get("updated"),
        "migrated": False,
    }


def _incomplete_checkpoint(chat_id: int) -> dict | None:
    checkpoint = _checkpoint_for(chat_id)
    if checkpoint is None:
        return None
    report = checkpoint["report"]
    if report is not None and report.finished:
        return None
    return checkpoint


async def _save_checkpoint(
    chat_id: int,
    boundary: int,
    next_cursor: int,
    report: DeleteReport,
) -> None:
    """Atomically persist only a completed batch's cursor and report."""

    record = {
        "algorithm": "two_pass_v2",
        "phase": report.pass_phase,
        "boundary": boundary,
        "next_cursor": next_cursor,
        "report": asdict(report),
    }
    async with _checkpoint_lock:
        all_rows = _read_checkpoints()
        all_rows[str(chat_id)] = record
        CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = CHECKPOINT_PATH.with_name(
            f".{CHECKPOINT_PATH.name}.{os.getpid()}.tmp"
        )
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(all_rows, fh, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        os.replace(temporary, CHECKPOINT_PATH)


def _read_legacy_receipts() -> dict:
    """Read retired UI metadata without contacting or retrying its owner."""

    try:
        with LEGACY_RECEIPT_PATH.open(encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _legacy_boundary_for(chat_id: int) -> int | None:
    """Return only a validated old command boundary for explicit resume.

    This compatibility path intentionally ignores every other field.  It does
    not infer whether a previous user-account mutation happened and it never
    performs any bridge operation.
    """

    candidates: list[tuple[float, str, int]] = []
    for operation_id, row in _read_legacy_receipts().items():
        if not isinstance(row, dict) or row.get("chat_id") != chat_id:
            continue
        if row.get("state") not in {
            "checking",
            "prepared",
            "sending",
            "accepted",
            "refused",
            "unknown",
            "cancelled",
            "expired",
        }:
            continue
        boundary = row.get("boundary")
        if (
            isinstance(boundary, bool)
            or not isinstance(boundary, int)
            or not 1 <= boundary <= 2_147_483_647
        ):
            continue
        updated = row.get("updated", 0)
        if isinstance(updated, bool) or not isinstance(updated, (int, float)):
            updated = 0.0
        candidates.append((float(updated), str(operation_id), boundary))
    if not candidates:
        return None
    return max(candidates, key=lambda entry: (entry[0], entry[1]))[2]


def _token() -> str:
    return uuid.uuid4().hex


def _keyboard(token: str, *, stop: bool = False, resume: bool = False):
    if stop:
        return types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton(
                "Stop deletion",
                callback_data=f"gh:bp:stop:{token}",
                style=enums.ButtonStyle.DANGER,
            )
        ]])
    return types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(
            "Resume deletion" if resume else "Permanently delete",
            callback_data=f"gh:bp:confirm:{token}",
            style=enums.ButtonStyle.DANGER,
        ),
        types.InlineKeyboardButton(
            "Cancel",
            callback_data=f"gh:bp:cancel:{token}",
            style=enums.ButtonStyle.PRIMARY,
        ),
    ]])


def _confirmation_text(item: PendingPurge, title: str) -> str:
    title = html.escape(title or str(item.chat_id))
    if item.resume:
        if item.migrated_checkpoint:
            scope = (
                "An older one-pass checkpoint was found, but its cursor cannot "
                "safely identify deferred service work. This is a fresh "
                "two-pass confirmation at its original fixed boundary; old "
                "counts were cleared and are not comparable."
            )
        elif item.prior_report is not None:
            scope = (
                "This is a fresh Maki-only confirmation for the previously "
                "confirmed fixed boundary; "
                f"the saved {item.prior_report.pass_phase} pass cursor is "
                f"<code>{item.start_cursor}</code>."
            )
        else:
            scope = (
                "This is a fresh Maki-only confirmation for the previously "
                "recorded fixed boundary; both safe passes start at its "
                "original boundary."
            )
    else:
        scope = (
            "This starts a new fixed boundary at the command's message ID. "
            "A previous incomplete scan, if any, is not reused."
        )
    prior = ""
    if item.prior_report is not None and not item.resume:
        prior = (
            "\n\nAn incomplete earlier bot scan is available at boundary "
            f"<code>{item.prior_report.total_ids}</code>, with "
            f"<code>{item.available_cursor}</code> as its saved next cursor. "
            "Use <code>/purgeall resume</code> for that boundary."
        )
    if item.prior_report is not None and item.prior_report.refused:
        prior += (
            "\n\n<b>Telegram already refused "
            f"{item.prior_report.refused} messages in the previous scan.</b> "
            "Starting again does not bypass that refusal."
        )
    return (
        f"<b>Delete group history in {title}?</b>\n\n"
        "Maki will use only its own bot session to scan message IDs from "
        "<code>1</code> through the frozen command boundary "
        f"<code>{item.boundary}</code>, then delete messages Telegram permits "
        "the bot to delete. This cannot be undone.\n\n"
        "This includes members', admins' and bots' messages, media and service "
        "notices. In supergroups, Maki also attempts deletion of IDs hidden from "
        "its reads, using requests restricted to this group. Basic groups need "
        "a readable message to establish its group. No connected user account "
        "is used.\n\n"
        f"{scope}\n\n"
        "Messages sent after the command — including this confirmation card "
        "and progress card — are outside the fixed range and will not be "
        "touched. Telegram may refuse protected or service messages, and this "
        "bot cannot guarantee an empty history. IDs checked are not a count of "
        "messages found or deleted. Refused messages are skipped so cleanup "
        "can continue through the rest of the range. "
        "Only the live Telegram group creator may confirm."
        f"{prior}"
    )


def _report_wait_seconds(report: DeleteReport) -> int:
    """Read the optional live wait hint without making it checkpoint state."""

    value = getattr(report, "wait_seconds", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _report_phase(report: DeleteReport) -> str:
    value = getattr(report, "phase", "")
    return value.strip() if isinstance(value, str) else ""


def _report_text(
    report: DeleteReport,
    *,
    final: bool = False,
    elapsed_seconds: float | None = None,
) -> str:
    total_ids = max(1, report.total_ids)
    scanned_ids = max(0, min(total_ids, report.scanned_ids))
    coverage_percent = int(scanned_ids * 100 / total_ids)
    # Reading the final batch happens before its deletion/read-back work.
    # Until the engine marks the fixed range finished, 100% would falsely
    # suggest that every readable message has already been handled.
    if report.pass_phase == "deferred":
        deferred_done = max(0, min(total_ids, total_ids - report.phase_cursor))
        # Both full-range passes are work.  Never announce 100% while a
        # deferred cursor or bounded outstanding unit remains.
        completion_percent = int((total_ids + deferred_done) * 100 / (2 * total_ids))
    else:
        completion_percent = int(scanned_ids * 50 / total_ids)
    completion_percent = 100 if report.finished else min(99, completion_percent)
    if final:
        heading = (
            "<b>Group history scan complete (fixed range only).</b>"
            if report.finished
            else "<b>Group history deletion stopped.</b>"
        )
    else:
        heading = "<b>Group history deletion is running…</b>"
    lines = [
        heading,
        (
            "Overall scan/deletion progress: "
            f"<b>{completion_percent}%</b>"
        ),
        f"Messages found: <b>{report.found}</b>",
        f"Confirmed deleted: <b>{report.deleted}</b>",
        f"Ordinary messages confirmed deleted: <b>{report.ordinary_deleted}</b>",
        f"Services deferred from ordinary pass: <b>{report.deferred_services}</b>",
        f"Deferred services examined (attempt count): <b>{report.service_examined}</b>",
        f"Still present: <b>{report.remaining}</b>",
        f"Telegram refused visible messages: <b>{report.refused}</b>",
        f"Absent / hidden IDs: <b>{report.absent_or_hidden}</b>",
        (
            "Additional hidden / absent IDs attempted: "
            f"<b>{report.hidden_attempted}</b> (not confirmed deleted)"
        ),
        f"Inaccessible / outside this group: <b>{report.inaccessible}</b>",
        f"Unverified / conservative attempt outcomes: <b>{report.unverified}</b>",
        (
            "Message IDs checked (ID coverage; not messages): "
            f"<b>{scanned_ids}</b>/{report.total_ids} "
            f"(<b>{coverage_percent}% raw coverage</b>)"
        ),
    ]
    if report.pass_phase == "deferred":
        phase_done = max(0, min(total_ids, total_ids - report.phase_cursor))
        lines.append(
            "Deferred-pass ID coverage: "
            f"<b>{phase_done}</b>/{report.total_ids}; outstanding bounded IDs: "
            f"<b>{len(report.phase_pending_ids)}</b>"
        )
    phase = _report_phase(report)
    wait_seconds = _report_wait_seconds(report)
    if final:
        phase = "complete" if report.finished else "stopped"
    elif not phase:
        phase = "working"
    if phase:
        lines.append(f"Phase: <b>{html.escape(phase.replace('_', ' '))}</b>")
    if wait_seconds:
        lines.append(
            "Waiting for Telegram rate limit: "
            f"<b>{wait_seconds}s</b> (deletion is paused, not finished)."
        )
    if elapsed_seconds is not None:
        elapsed = max(0, int(elapsed_seconds))
        lines.append(f"Elapsed: <b>{elapsed}s</b>")
    if report.pending:
        lines.append(f"Current batch still pending: <b>{report.pending}</b>")
    if report.last_refusal_kind:
        lines.append(
            "Last rejection type: "
            + html.escape(report.last_refusal_kind.replace("_", " "))
        )
        if report.last_refusal_kind in {"new_chat_members", "left_chat_member"}:
            lines.append(
                "Join/leave notices bache hain: Telegram ne bot deletion reject ki."
            )
    lines.append("IDs checked is not a count of existing or deleted messages.")
    if report.hidden_attempted:
        lines.append(
            "Hidden / absent ID attempts are not confirmed deletions: "
            "Maki could not read those messages before the request."
        )
    if report.reason:
        lines.append(f"Stopped because: {html.escape(report.reason)}")
    if final and report.finished and report.refused:
        lines.append(
            "The full fixed range was scanned. Telegram refused "
            f"{report.refused} visible messages; they were skipped and were "
            "not counted as deleted."
        )
    elif final and report.finished:
        if not report.remaining and not report.unverified:
            lines.append(
                "No bot-visible messages remain in the checked range. "
                "This is not a guarantee that the group's entire history is "
                "empty; protected or hidden history may remain. "
                "Hidden history may still be visible to other members."
            )
        else:
            lines.append(
                "The fixed range finished, but this is not an all-history-empty "
                "claim: inaccessible, refused, hidden, or remaining messages "
                "may still exist."
            )
    elif final:
        lines.append(
            "The scan stopped before the fixed range finished. Inaccessible or "
            "unverified IDs are never counted as deleted."
        )
        lines.append(
            "To continue this same frozen boundary, the group creator must "
            "explicitly confirm <code>/deleteall resume</code> "
            "(no automatic resume)."
        )
    return "\n".join(lines)


async def _live_checks(chat_id: int, owner_id: int) -> None:
    """Fail closed if creator status or bot delete rights changed."""

    if not await delete_right(app, chat_id):
        raise PermissionError("Maki needs the group's Delete messages right.")
    if not await live_owner(app, chat_id, owner_id):
        raise PermissionError("Only the live Telegram group creator may confirm.")


async def _show_error(message, text: str) -> None:
    try:
        await _reply(message, html.escape(text), quote=False)
    except Exception:
        # A failed notice must not turn an authorization failure into a
        # handler exception (and must never start deletion).
        pass


async def _fallback_terminal_notice(
    item: PendingPurge,
    stale_card: types.Message,
    text: str,
) -> None:
    """Replace an uneditable card with a plain, self-deleting summary."""

    try:
        sent = await app.send_message(
            item.chat_id, text, parse_mode=enums.ParseMode.HTML
        )
        if sent is not None:
            arm_delete(sent)
    except Exception as exc:
        logger.warning(
            "bulk purge fallback notice failed: %s", type(exc).__name__
        )
    try:
        arm_delete(stale_card)
    except Exception as exc:
        logger.warning("bulk purge stale-card cleanup failed: %s", type(exc).__name__)


def _elapsed_seconds(item: PendingPurge) -> float:
    if not item.started_at:
        return 0.0
    return max(0.0, time.monotonic() - item.started_at)


async def _progress_writer(item: PendingPurge, card: types.Message) -> None:
    """Write the newest progress snapshot without blocking the delete worker.

    ``run_delete`` publishes frequently around each batch.  Only one writer
    task is allowed, and it always reads the latest snapshot after its
    throttle delay.  A phase change or entering/leaving a rate-limit wait
    bypasses the delay so a long (including 300 second) wait is visible.
    """

    writer = asyncio.current_task()
    try:
        while not item.progress_finalized:
            current = item.latest_report
            if current is None:
                return
            now = time.monotonic()
            delay = max(0.0, EDIT_GAP - (now - item.last_edit))
            if delay and not item.progress_force:
                wakeup = item.progress_wakeup
                if wakeup is None:
                    wakeup = asyncio.Event()
                    item.progress_wakeup = wakeup
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                wakeup.clear()
                continue

            item.progress_force = False
            if item.progress_wakeup is not None:
                item.progress_wakeup.clear()
            item.last_edit = time.monotonic()
            revision = item.progress_revision
            try:
                await asyncio.wait_for(
                    card.edit_text(
                        _report_text(
                            current,
                            elapsed_seconds=_elapsed_seconds(item),
                        ),
                        parse_mode=enums.ParseMode.HTML,
                        reply_markup=_keyboard(item.token, stop=True),
                    ),
                    timeout=5,
                )
            except Exception:
                # UI availability must never change the deletion result.  The
                # next important transition can still provide a fresh edit.
                pass
            if item.progress_finalized or item.progress_revision == revision:
                return
            # A newer report arrived while Telegram was being edited.  Loop
            # once, preserving the time throttle unless it is important.
    finally:
        if item.progress_task is writer:
            item.progress_task = None


def _queue_progress(item: PendingPurge, card: types.Message, report: DeleteReport) -> None:
    """Store a progress snapshot and schedule at most one network writer."""

    if item.progress_finalized:
        return
    item.latest_report = report
    item.progress_revision += 1
    phase = _report_phase(report)
    wait_seconds = _report_wait_seconds(report)
    waiting = wait_seconds > 0
    previous_wait_seconds = item.progress_last_wait_seconds
    wait_changed = (
        previous_wait_seconds is not None
        and wait_seconds != previous_wait_seconds
    )
    wait_transition = (
        previous_wait_seconds is None and waiting
        or previous_wait_seconds is not None
        and waiting != bool(previous_wait_seconds)
    )
    item.progress_last_phase = phase
    item.progress_last_waiting = waiting
    item.progress_last_wait_seconds = wait_seconds
    # Ordinary fetch/authorize/delete/verify phase changes are frequent
    # engine detail and stay behind EDIT_GAP.  Only a rate-limit transition
    # (or a changed active wait duration) is urgent enough to wake a writer.
    important = wait_transition or wait_changed
    item.progress_force = item.progress_force or important
    if important and item.progress_wakeup is not None:
        item.progress_wakeup.set()
    task = item.progress_task
    if task is None or task.done():
        if item.progress_wakeup is None:
            item.progress_wakeup = asyncio.Event()
        item.progress_task = asyncio.create_task(_progress_writer(item, card))


async def _finish_progress(item: PendingPurge) -> None:
    """Stop the coalescer before the terminal card is edited."""

    item.progress_finalized = True
    if item.progress_wakeup is not None:
        item.progress_wakeup.set()
    task = item.progress_task
    if task is not None and task is not asyncio.current_task():
        # A writer may be in its inexpensive throttle sleep.  Cancel that
        # stale snapshot rather than delaying the terminal status; awaiting
        # the task still serializes completion before the final edit.
        if not task.done():
            task.cancel()
        try:
            # The writer's Telegram call is bounded to five seconds.  Waiting
            # here serializes the terminal edit so stale progress cannot
            # overwrite the final result.
            await task
        except BaseException:
            pass
    item.progress_task = None


async def _bot_purge_command(message, command_args: list[str]) -> None:
    """Create a bot-only confirmation card; no deletion happens here."""

    _reap()
    if not message.from_user:
        return
    owner_id = message.from_user.id
    try:
        if not await delete_right(app, message.chat.id):
            await _show_error(
                message, "Maki needs the group's Delete messages right. Nothing was sent."
            )
            return
        if not await live_owner(app, message.chat.id, owner_id):
            await _show_error(
                message, "Only the live Telegram group creator can use this command."
            )
            return
    except Exception:
        await _show_error(
            message, "Could not verify live creator or bot permission. Nothing was sent."
        )
        return
    if len(command_args) > 1 or (
        command_args and command_args[0] not in {"resume", "stop"}
    ):
        await _show_error(
            message,
            "Usage: <code>/purgeall</code>, <code>/deleteall</code>, "
            "<code>/deletall</code>, <code>/delall</code>, "
            "<code>/delete all</code>, or the same command with "
            "<code>resume</code> or <code>stop</code>.",
        )
        return

    if command_args == ["stop"]:
        running = _running.get(message.chat.id)
        if running is None or running.stop is None:
            await _show_error(message, "No bot deletion is currently running in this group.")
            return
        running.stop.set()
        await _show_error(message, "Stopping deletion after the current safe request…")
        return

    resume = bool(command_args)
    boundary = message.id
    if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 1:
        await _show_error(message, "This command has no valid Telegram message boundary.")
        return

    checkpoint = _incomplete_checkpoint(message.chat.id)
    if resume:
        if checkpoint is not None:
            boundary = checkpoint["boundary"]
            start_cursor = checkpoint["next_cursor"]
            prior_report = checkpoint["report"]
            available_cursor = checkpoint["next_cursor"]
            migrated_checkpoint = checkpoint.get("migrated", False)
        else:
            # Compatibility with a failed old account-UI receipt: use only
            # its validated original boundary and require this fresh bot
            # confirmation.  Never contact or retry the retired operation.
            boundary = _legacy_boundary_for(message.chat.id)
            if boundary is None:
                await _show_error(
                    message,
                    "No resumable bot checkpoint or validated previous cleanup "
                    "boundary is available. Nothing was sent.",
                )
                return
            start_cursor = boundary
            prior_report = None
            available_cursor = None
            migrated_checkpoint = False
    else:
        start_cursor = boundary
        prior_report = checkpoint["report"] if checkpoint is not None else None
        available_cursor = checkpoint["next_cursor"] if checkpoint is not None else None
        migrated_checkpoint = False

    if message.chat.id in _running:
        await _show_error(message, "A bot deletion is already running in this group.")
        return
    if any(item.chat_id == message.chat.id for item in _pending.values()):
        await _show_error(
            message, "A bot deletion confirmation is already open in this group."
        )
        return

    item = PendingPurge(
        chat_id=message.chat.id,
        owner_id=owner_id,
        boundary=boundary,
        token=_token(),
        created=time.monotonic(),
        start_cursor=start_cursor,
        prior_report=prior_report,
        resume=resume,
        available_cursor=available_cursor,
        migrated_checkpoint=migrated_checkpoint,
    )
    _pending[item.token] = item
    try:
        sent = await _reply(
            message,
            _confirmation_text(item, getattr(message.chat, "title", "")),
            reply_markup=_keyboard(item.token, resume=resume),
            preserve=True,
        )
        if _pending.get(item.token) is not item:
            try:
                await sent.delete()
            except Exception:
                pass
            return
        item.card_id = sent.id
    except asyncio.CancelledError:
        if _pending.get(item.token) is item:
            _pending.pop(item.token, None)
        raise
    except Exception:
        if _pending.get(item.token) is item:
            _pending.pop(item.token, None)
        await _show_error(
            message, "Could not open bot deletion confirmation. Nothing was sent."
        )


@app.on_message(
    filters.group & (
        filters.command(
            ["purgeall", "deleteall", "deletall", "delall"], prefixes=PRE
        )
        | (
            filters.command("delete", prefixes=PRE)
            & filters.regex(_DELETE_ALL_TEXT_RE)
        )
    )
)
async def purgeall(_, message: types.Message):
    """Open a bound bot-only permanent-deletion confirmation card."""

    command_args = _bulk_command_args(message)
    if command_args is None:
        return
    return await _bot_purge_command(message, command_args)


async def _bound_callback(query: types.CallbackQuery, token: str):
    """Resolve a token only when chat, card and creator all match."""

    message = query.message
    user = query.from_user
    item = _pending.get(token)
    data = getattr(query, "data", "")
    action = data.split(":")[2] if isinstance(data, str) and data else ""
    if (
        not isinstance(data, str)
        or not data.startswith("gh:bp:")
        or not message
        or not _group(message)
        or not user
        or not item
        or (_expired(item) and not (
            action == "stop" and _running.get(item.chat_id) is item
        ))
        or item.card_id != getattr(message, "id", 0)
        or item.chat_id != getattr(message.chat, "id", None)
        or item.owner_id != user.id
    ):
        return None
    return item


def admits_stop_callback(query: types.CallbackQuery) -> bool:
    """Local admission hint for rateguard; authorization stays below."""

    data = getattr(query, "data", "")
    prefix = "gh:bp:stop:"
    if not isinstance(data, str) or not data.startswith(prefix):
        return False
    token = data[len(prefix):]
    if len(token) != TOKEN_LEN or any(
        char not in "0123456789abcdef" for char in token
    ):
        return False
    item = _pending.get(token)
    message = getattr(query, "message", None)
    user = getattr(query, "from_user", None)
    return bool(
        item
        and _running.get(item.chat_id) is item
        and item.stop is not None
        and message
        and getattr(getattr(message, "chat", None), "id", None) == item.chat_id
        and getattr(message, "id", None) == item.card_id
        and user
        and user.id == item.owner_id
    )


@app.on_callback_query(filters.regex(_LEGACY_CALLBACK_RE))
async def retired_cleanup_callback(_, query: types.CallbackQuery):
    """Safely retire old assistant-account buttons.

    A stale button must never be interpreted as a Maki confirmation, Stop, or
    cancellation.  This handler does not inspect pending state and does not
    import or contact the retired assistant bridge.
    """

    try:
        await query.answer(
            "Assistant-based cleanup is disabled. Start a fresh Maki-only "
            "/purgeall command and confirmation.",
            show_alert=True,
        )
    except Exception:
        # A stale callback is inert even if Telegram rejects the answer.
        pass


@app.on_callback_query(filters.regex(_CONFIRM_RE))
async def purgeall_callback(_, query: types.CallbackQuery):
    """Handle confirmation, cancellation and Stop with exact card binding."""

    data = getattr(query, "data", "")
    token = data.rsplit(":", 1)[-1]
    action = data.split(":")[2] if len(data.split(":")) >= 4 else ""
    item = await _bound_callback(query, token)
    if not item:
        try:
            await query.answer(
                "This confirmation expired or belongs to another group/card.",
                show_alert=True,
            )
        except Exception:
            pass
        return

    if action == "cancel":
        # No await between checking and consuming the token: another
        # confirmation callback cannot win after this point.
        if item.chat_id in _running:
            await query.answer("Deletion is already running.", show_alert=True)
            return
        if _pending.get(token) is item:
            _pending.pop(token, None)
            try:
                edited = await query.message.edit_text(
                    "Deletion cancelled — no messages were deleted."
                )
                if edited is False:
                    raise RuntimeError("terminal cancellation edit returned false")
                arm_delete(query.message)
            except Exception as exc:
                logger.warning(
                    "bulk purge cancellation card edit failed: %s",
                    type(exc).__name__,
                )
                await _fallback_terminal_notice(
                    item,
                    query.message,
                    "Deletion cancelled — no messages were deleted.",
                )
        await query.answer("Cancelled.")
        return

    if action == "stop":
        running = _running.get(item.chat_id)
        if running is not item or not item.stop:
            await query.answer("That deletion is no longer running.", show_alert=True)
            return
        try:
            if not await live_owner(app, item.chat_id, query.from_user.id):
                await query.answer(
                    "Only the live group creator can stop this.", show_alert=True
                )
                return
        except Exception:
            await query.answer("Could not verify the live group creator.", show_alert=True)
            return
        item.stop.set()
        await query.answer("Stopping after the current safe request…")
        return

    # Confirmation is consumed/reserved before any await, making duplicate
    # taps and a second callback single-flight safe.
    if item.chat_id in _running:
        await query.answer("Deletion is already running.", show_alert=True)
        return
    _running[item.chat_id] = item
    item.stop = asyncio.Event()
    item.started_at = time.monotonic()
    try:
        await query.answer("Starting…")
        await _live_checks(item.chat_id, item.owner_id)
        await asyncio.wait_for(
            query.message.edit_text(
                "Starting fixed-boundary bot-only group-history deletion…\n\n"
                + _report_text(
                    DeleteReport(total_ids=item.boundary),
                    elapsed_seconds=_elapsed_seconds(item),
                ),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=_keyboard(item.token, stop=True),
            ),
            timeout=5,
        )
    except PermissionError as exc:
        _running.pop(item.chat_id, None)
        _pending.pop(token, None)
        await _show_error(query.message, str(exc))
        return
    except asyncio.CancelledError:
        _running.pop(item.chat_id, None)
        _pending.pop(token, None)
        raise
    except Exception:
        _running.pop(item.chat_id, None)
        _pending.pop(token, None)
        await _show_error(query.message, "Could not start deletion. Nothing was deleted.")
        return

    item.task = asyncio.create_task(_run(item, query.message))


async def _run(item: PendingPurge, card: types.Message) -> None:
    """Run the bot engine and leave a truthful final card in place."""

    report: DeleteReport | None = None
    try:
        async def progress(current: DeleteReport):
            # This callback only queues the latest snapshot.  In particular,
            # the engine never waits for one Telegram edit per batch.
            _queue_progress(item, card, current)

        async def checkpoint(next_cursor: int, current: DeleteReport):
            await _save_checkpoint(
                item.chat_id, item.boundary, next_cursor, current
            )

        initial_report = (
            item.prior_report
            if item.resume and item.prior_report is not None
            else DeleteReport(total_ids=item.boundary)
        )
        await _save_checkpoint(
            item.chat_id, item.boundary, item.start_cursor, initial_report
        )
        report = await run_delete(
            app,
            item.chat_id,
            item.owner_id,
            item.boundary,
            item.stop,
            progress,
            start_id=item.start_cursor,
            prior_report=item.prior_report if item.resume else None,
            checkpoint=checkpoint,
        )
        if report.reason:
            logger.warning("bulk purge scan stopped: %s", report.reason[:240])
    except asyncio.CancelledError:
        report = DeleteReport(
            total_ids=item.boundary,
            reason="The deletion task was cancelled; its final result is unverified.",
            unverified=1,
        )
    except Exception as exc:
        logger.warning("bulk purge task failed: %s", type(exc).__name__)
        report = DeleteReport(
            total_ids=item.boundary,
            reason=f"Deletion stopped ({type(exc).__name__}); result is unverified.",
            unverified=1,
        )
    finally:
        if report is None:
            report = DeleteReport(
                total_ids=item.boundary,
                reason="Deletion stopped; result is unverified.",
                unverified=1,
            )
        await _finish_progress(item)
        _running.pop(item.chat_id, None)
        if _pending.get(item.token) is item:
            _pending.pop(item.token, None)
        final_text = _report_text(
            report,
            final=True,
            elapsed_seconds=_elapsed_seconds(item),
        )
        try:
            edited = await asyncio.wait_for(
                card.edit_text(
                    final_text,
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=None,
                ),
                timeout=5,
            )
            if edited is False:
                raise RuntimeError("terminal summary edit returned false")
            arm_delete(card)
        except Exception as exc:
            logger.warning(
                "bulk purge final card edit failed: %s", type(exc).__name__
            )
            await _fallback_terminal_notice(item, card, final_text)