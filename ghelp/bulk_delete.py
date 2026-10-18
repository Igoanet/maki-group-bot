"""The safe, bot-only engine used by ``/purgeall``.

Telegram does not expose ``messages.getHistory`` or
``channels.deleteHistory`` to a bot.  The only supported bot operation here
is therefore a deliberately boring scan of the command message's fixed
numeric ID range, in batches of 100, using ``get_messages`` followed by
``delete_messages``.

The range is fixed before confirmation.  In particular, the confirmation
card and every message sent after the command are outside the range and can
never be deleted by this job.  Message IDs are account-wide in basic groups,
so fetched messages are checked against the requested peer before deletion.
Initially unreadable IDs are attempted only through a supergroup-scoped
request with an independently checked channel identity, never a bare-ID RPC.
"""

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable

from pyrogram import enums, errors, raw, utils

BATCH_SIZE = 100
# Kept as compatibility names for callers/tests from the first bot-only
# implementation.  Normal and refusal batches no longer sleep; the only
# non-zero delay in this module is a server-requested FloodWait.
BATCH_PAUSE = 0.0
REFUSAL_PAUSE = 0.0
SERVICE_PROBE_SIZE = 8
MAX_FLOOD_WAIT = 300
MAX_RPC_ATTEMPTS = 4
RPC_TIMEOUT = 15
logger = logging.getLogger(__name__)


class DeleteStopped(Exception):
    """A deliberate fail-closed stop (owner stop, revocation, or bad range)."""


@dataclass
class DeleteReport:
    """Facts known by the engine, never estimates from an RPC return value."""

    total_ids: int
    scanned_ids: int = 0
    found: int = 0
    deleted: int = 0
    remaining: int = 0
    refused: int = 0
    inaccessible: int = 0
    unverified: int = 0
    absent_or_hidden: int = 0
    hidden_attempted: int = 0
    pending: int = 0
    last_refusal_kind: str = ""
    finished: bool = False
    reason: str = ""
    # ``phase`` and ``wait_seconds`` are live, transient UI state.  Defaults
    # keep old progress/checkpoint snapshots loadable while a current card
    # can say whether the worker is fetching, authorizing, mutating, or
    # verifying; the engine never resumes from either field.
    phase: str = ""
    wait_seconds: int = 0
    # Durable two-pass state.  These are deliberately separate from ``phase``
    # above, which is only a transient description of the RPC in flight.
    pass_phase: str = "ordinary"
    phase_cursor: int = 0
    phase_pending_ids: list[int] = field(default_factory=list)
    # A bounded subset of phase_pending_ids.  Keeping this source marker is
    # what lets an accepted channel-scoped hidden batch stay at 100 IDs while
    # readable service notices remain in small refusal checkpoints.
    phase_pending_hidden_ids: list[int] = field(default_factory=list)
    ordinary_deleted: int = 0
    deferred_services: int = 0
    deferred_hidden: int = 0
    service_examined: int = 0


@dataclass
class _FetchOutcome:
    """One exact outcome for every requested message ID."""

    local: set[int]
    empty: set[int]
    foreign: set[int]
    missing: set[int]


async def live_owner(client, chat_id, user_id) -> bool:
    """Return true only for the live Telegram creator of this chat.

    This intentionally does not accept a configured bot owner or an
    administrator.  The destructive command is creator-only.
    """

    member = await client.get_chat_member(chat_id, user_id)
    return member.status == enums.ChatMemberStatus.OWNER


async def delete_right(client, chat_id) -> bool:
    """Check the bot's live Delete messages right in *chat_id*."""

    me = getattr(client, "me", None) or await client.get_me()
    member = await client.get_chat_member(chat_id, me.id)
    if member.status == enums.ChatMemberStatus.OWNER:
        return True
    return bool(
        member.status == enums.ChatMemberStatus.ADMINISTRATOR
        and getattr(getattr(member, "privileges", None), "can_delete_messages", False)
    )


def _message_id(message):
    value = getattr(message, "id", None)
    # bool is an int subclass but is not a Telegram message ID.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _inspect_messages(messages, chat_id, requested) -> _FetchOutcome:
    """Classify every requested ID, failing closed on malformed coverage.

    ``get_messages`` normally returns one object per requested ID, including
    ``MessageEmpty`` objects.  A shorter list is not evidence that a message
    was deleted: it is an incomplete response and its IDs remain unknown.
    Duplicate/conflicting objects are likewise rejected rather than allowing
    an account-wide basic-group ID collision to be treated as local.
    """

    if not isinstance(messages, (list, tuple)):
        raise DeleteStopped("Telegram returned an unexpected message list.")
    wanted = set(requested)
    seen: dict[int, str] = {}
    for message in messages:
        if message is None:
            raise DeleteStopped("Telegram returned a malformed message.")
        mid = _message_id(message)
        if mid is None:
            raise DeleteStopped("Telegram returned a malformed message ID.")
        if mid not in wanted:
            raise DeleteStopped("Telegram returned an ID outside the requested batch.")
        if mid in seen:
            raise DeleteStopped("Telegram returned duplicate message coverage.")
        if getattr(message, "empty", False):
            seen[mid] = "empty"
            continue
        peer = getattr(getattr(message, "chat", None), "id", None)
        if peer is None:
            raise DeleteStopped("Telegram returned a message without a peer.")
        seen[mid] = "local" if peer == chat_id else "foreign"
    return _FetchOutcome(
        local={mid for mid, kind in seen.items() if kind == "local"},
        empty={mid for mid, kind in seen.items() if kind == "empty"},
        foreign={mid for mid, kind in seen.items() if kind == "foreign"},
        missing=wanted - set(seen),
    )


def _checked_ids(messages, chat_id, requested) -> tuple[set[int], int]:
    """Return exact-peer IDs and the number of foreign requested IDs."""

    outcome = _inspect_messages(messages, chat_id, requested)
    return outcome.local, len(outcome.foreign)


def checked_ids(messages, chat_id, requested):
    """Compatibility wrapper returning only safe, exact-peer IDs."""

    return _checked_ids(messages, chat_id, requested)[0]


async def _pause(stop: asyncio.Event, seconds: float) -> None:
    """Stop-interruptible delay which always gives callbacks a turn."""

    if stop.is_set():
        raise DeleteStopped("Stopped by the owner.")
    if seconds <= 0:
        await asyncio.sleep(0)
        if stop.is_set():
            raise DeleteStopped("Stopped by the owner.")
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return
    raise DeleteStopped("Stopped by the owner.")


async def _rpc(
    stop: asyncio.Event,
    operation,
    *,
    check_stop: bool = True,
    on_wait: Callable[[int], Awaitable[None]] | None = None,
):
    """Run one RPC with bounded, stop-interruptible FloodWait retries.

    ``on_wait`` is called with the server-requested delay before sleeping and
    with zero once that delay has elapsed.  The callback is intentionally
    shared by fetches, read-backs, authorization lookups, and destructive
    invokes so a progress card never loses the fact that a nested operation
    is waiting on Telegram.  Other failures, especially a timeout from an
    ambiguous mutation, are never retried here.
    """

    for attempt in range(MAX_RPC_ATTEMPTS):
        if check_stop and stop.is_set():
            raise DeleteStopped("Stopped by the owner.")
        try:
            return await asyncio.wait_for(operation(), timeout=RPC_TIMEOUT)
        except errors.FloodWait as exc:
            delay = max(1, int(exc.value))
            logger.warning(
                "bulk purge rate limit: wait_seconds=%s attempt=%s",
                delay, attempt + 1,
            )
            if delay > MAX_FLOOD_WAIT or attempt == MAX_RPC_ATTEMPTS - 1:
                raise DeleteStopped(
                    "Telegram rate limit is too long; run /deleteall again later."
                ) from None
            if on_wait is not None:
                await on_wait(delay)
            await _pause(stop, delay)
            if on_wait is not None:
                await on_wait(0)
    raise DeleteStopped("Telegram rate limit prevented completion.")


async def _prepare_delete_request(client, chat_id: int, ids: list[int]):
    """Resolve the peer and build, but do not invoke, the raw delete request."""

    peer = await client.resolve_peer(chat_id)
    if isinstance(peer, raw.types.InputChannel):
        channel = peer
    elif isinstance(peer, raw.types.InputPeerChannel):
        # ``channels.deleteMessages`` takes InputChannel, while Pyrogram's
        # resolve_peer() returns InputPeerChannel.  They carry the same
        # channel_id/access_hash fields but have different TL constructor IDs;
        # passing the peer object directly serializes the wrong constructor
        # and Telegram rejects the request.
        channel = raw.types.InputChannel(
            channel_id=peer.channel_id,
            access_hash=peer.access_hash,
        )
    else:
        channel = None
    if channel is not None:
        return raw.functions.channels.DeleteMessages(
            channel=channel, id=list(ids)
        )
    return raw.functions.messages.DeleteMessages(id=list(ids), revoke=True)


def _is_group_scoped(request, chat_id: int) -> bool:
    """Unreadable IDs are safe only in this exact channel's ID namespace."""
    if not isinstance(request, raw.functions.channels.DeleteMessages):
        return False
    channel = request.channel
    if not isinstance(channel, raw.types.InputChannel):
        return False
    channel_id = channel.channel_id
    return (
        isinstance(channel_id, int)
        and not isinstance(channel_id, bool)
        and channel_id > 0
        and utils.get_channel_id(channel_id) == chat_id
    )


async def run_delete(
    client,
    chat_id: int,
    owner_id: int,
    maximum_id: int,
    stop: asyncio.Event,
    progress: Callable[[DeleteReport], Awaitable[None]],
    *,
    start_id: int | None = None,
    prior_report: DeleteReport | None = None,
    checkpoint: Callable[[int, DeleteReport], Awaitable[None]] | None = None,
) -> DeleteReport:
    """Scan ``maximum_id`` down to one within the authorized group's scope.

    The caller owns the confirmation card and supplies a per-job stop event.
    The initial scan is authorized once.  Each destructive RPC authorizes
    immediately before it is sent (including every split/retry); batches that
    do no destructive work perform one live check before their checkpoint.
    Message-specific refusals are isolated so other messages can still be
    removed; authorization and unreadable-result failures stop rather than
    pretending deletion succeeded.
    """

    report = (
        replace(
            prior_report,
            total_ids=maximum_id,
            pending=0,
            finished=False,
            reason="",
            phase="",
            wait_seconds=0,
        )
        if prior_report is not None
        else DeleteReport(total_ids=maximum_id)
    )
    message_metadata: dict[int, tuple[str, int | None]] = {}
    hidden_submitted: set[int] = set()
    batch_mutation_started = False

    async def publish_progress() -> None:
        try:
            # Do not let later mutations rewrite an already emitted snapshot.
            await progress(replace(
                report,
                phase_pending_ids=list(report.phase_pending_ids),
                phase_pending_hidden_ids=list(report.phase_pending_hidden_ids),
            ))
        except Exception:
            # A failed status edit must never masquerade as failed deletion.
            pass

    async def rpc_call(phase: str, operation):
        """Run an RPC while exposing its phase and FloodWait state.

        Nested authorization is deliberately represented as its own phase and
        then restores the enclosing mutation/read phase.  In particular, a
        FloodWait from a rights lookup must not be shown as a deletion wait.
        The progress callback is announced before the RPC, so a Stop tap made
        by that callback is observed by ``_rpc`` before any request starts.
        """

        previous_phase = report.phase
        previous_wait = report.wait_seconds
        report.phase = phase
        report.wait_seconds = 0

        async def on_wait(seconds: int) -> None:
            report.wait_seconds = seconds
            await publish_progress()

        try:
            # The initial authorization/fetch deliberately remain quiet so a
            # Stop callback cannot prevent the first boundary read before it
            # has seen any scan progress.  Once a batch is visible, announce
            # the active phase; this also gives Stop a turn immediately before
            # a prepared mutation.  FloodWait callbacks below always publish
            # the active phase and wait countdown.
            if report.scanned_ids or report.pending:
                await publish_progress()
            return await _rpc(stop, operation, on_wait=on_wait)
        finally:
            report.phase = previous_phase
            report.wait_seconds = previous_wait

    async def record_outcome(
        ids: list[int], *, deleted: int = 0, refused: int = 0,
        refused_id: int | None = None,
    ) -> None:
        report.pending = max(0, report.pending - len(ids))
        if refused:
            kind, age = message_metadata.get(
                refused_id if refused_id is not None else ids[0],
                ("message", None),
            )
            report.last_refusal_kind = kind
            logger.warning(
                "bulk purge refusal: kind=%s age_hours=%s refused=%s pending=%s",
                kind, age, report.refused, report.pending,
            )
        await publish_progress()
        if refused:
            # Yield after a refusal so a Stop callback gets a turn, without
            # imposing an artificial per-message delay.
            await _pause(stop, 0)

    async def authorize() -> None:
        if stop.is_set():
            raise DeleteStopped("Stopped by the owner.")
        if not await rpc_call(
            "authorize", lambda: delete_right(client, chat_id)
        ):
            raise DeleteStopped("Maki no longer has Delete messages permission.")
        # Creator consent is app-enforced, so check it after every other
        # awaited permission lookup. Telegram enforces bot rights on the RPC.
        if not await rpc_call(
            "authorize", lambda: live_owner(client, chat_id, owner_id)
        ):
            raise DeleteStopped("The initiating user is no longer the group owner.")
        if stop.is_set():
            raise DeleteStopped("Stopped by the owner.")

    async def save_checkpoint(next_cursor: int) -> None:
        if checkpoint is None:
            return
        previous_phase = report.phase
        previous_wait = report.wait_seconds
        report.phase = "checkpoint"
        report.wait_seconds = 0
        try:
            # These fields describe the live worker, not resumable work.
            # Never make a checkpoint look as if a future resume is still
            # waiting on a rate limit or paused in an old phase.
            await checkpoint(
                next_cursor,
                replace(
                    report,
                    phase=previous_phase,
                    wait_seconds=previous_wait,
                    phase_pending_ids=list(report.phase_pending_ids),
                    phase_pending_hidden_ids=list(report.phase_pending_hidden_ids),
                ),
            )
        finally:
            report.phase = previous_phase
            report.wait_seconds = previous_wait

    async def read(ids: list[int]) -> _FetchOutcome:
        try:
            messages = await rpc_call(
                "fetch", lambda: client.get_messages(chat_id, ids, replies=0)
            )
        except DeleteStopped:
            raise
        except Exception:
            # The batch was requested but Telegram did not provide a
            # trustworthy response.  Its IDs are unverified, not deleted.
            report.unverified += len(ids)
            raise
        try:
            outcome = _inspect_messages(messages, chat_id, ids)
        except DeleteStopped:
            # This is not a deletion failure, but it is also not safe to
            # continue.  Nothing from this malformed fetch is acted on.
            report.unverified += len(ids)
            raise
        if outcome.missing:
            # An initially truncated response is unknown as a whole.  Do not
            # delete even the local-looking subset from this batch.
            report.unverified += len(ids)
            raise DeleteStopped(
                "Telegram returned incomplete message coverage; the batch is unverified."
            )
        report.inaccessible += len(outcome.foreign)
        # Empty on FIRST read means absent or hidden from this bot, not
        # something this job deleted. Keep that distinction visible.
        report.absent_or_hidden += len(outcome.empty)
        message_metadata.clear()
        for message in messages:
            mid = _message_id(message)
            if mid not in outcome.local:
                continue
            service = getattr(message, "service", None)
            kind = str(getattr(service, "value", None) or "message")[:64]
            date = getattr(message, "date", None)
            age = None
            if isinstance(date, datetime):
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                age = max(0, int((datetime.now(timezone.utc) - date).total_seconds() // 3600))
            message_metadata[mid] = (kind, age)
        logger.info(
            "bulk purge read: ids=%s..%s present=%s absent_or_hidden=%s foreign=%s",
            min(ids), max(ids), len(outcome.local), len(outcome.empty),
            len(outcome.foreign),
        )
        return outcome

    async def verify(ids: list[int]) -> _FetchOutcome:
        """Read back after deletion; an unreadable result is unverified."""

        if stop.is_set():
            raise DeleteStopped("Deletion was sent, but the result is unverified.")
        try:
            messages = await rpc_call(
                "verify", lambda: client.get_messages(chat_id, ids, replies=0)
            )
            outcome = _inspect_messages(messages, chat_id, ids)
            if outcome.missing:
                report.unverified += len(outcome.missing)
            if outcome.foreign:
                # A foreign peer at read-back is not proof of deletion.
                report.unverified += len(outcome.foreign)
            return outcome
        except DeleteStopped:
            raise
        except Exception:
            raise DeleteStopped(
                "Deletion was sent, but Telegram could not confirm the result."
            ) from None

    async def erase(ids: list[int], *, hidden: bool = False) -> None:
        if not ids:
            return

        # A basic-group ID is account-wide and cannot be scoped safely when
        # its first read was empty.  Do not even resolve/build a hidden
        # deletion request for that peer; only supergroup channel requests
        # can establish the required namespace.
        if hidden and utils.get_peer_type(chat_id) != "channel":
            return

        # Resolve the peer and build the raw request before final auth.  The
        # high-level delete_messages helper resolves the peer internally,
        # leaving a stop/demotion window after authorization.
        try:
            request = await rpc_call(
                "prepare",
                lambda: _prepare_delete_request(client, chat_id, ids),
            )
        except DeleteStopped:
            if not stop.is_set() and not hidden:
                report.unverified += len(ids)
            raise
        except Exception:
            if not hidden:
                report.unverified += len(ids)
            raise
        if hidden and not _is_group_scoped(request, chat_id):
            # Basic-group IDs are account-wide. An empty read cannot prove
            # which chat owns one; never send those IDs to messages.deleteMessages.
            return
        if not hidden and (
            isinstance(request, raw.functions.channels.DeleteMessages)
            or utils.get_peer_type(chat_id) == "channel"
        ) and not _is_group_scoped(request, chat_id):
            # A readable ID is not enough if peer resolution built a request
            # for another channel (or an account-wide request for a channel).
            report.unverified += len(ids)
            raise DeleteStopped("Resolved deletion request does not match this group.")

        # Revalidate immediately before EVERY destructive raw invoke,
        # including every retry after a FloodWait.
        async def issue():
            nonlocal batch_mutation_started
            await authorize()
            if stop.is_set():
                raise DeleteStopped("Stopped by the owner.")
            if hidden:
                new_ids = set(ids) - hidden_submitted
                hidden_submitted.update(new_ids)
                report.hidden_attempted += len(new_ids)
                # Empty before and after is not evidence of deletion. Count
                # these once even if a rejected batch is split or rate-limited.
                report.unverified += len(new_ids)
            # Disable SDK-level retries/sleeps: each retry must pass through
            # our fresh live-owner/right/Stop authorization above.
            batch_mutation_started = True
            return await client.invoke(
                request, retries=1, sleep_threshold=0, retry_delay=0
            )

        try:
            await rpc_call("delete", issue)
        except (
            errors.MessageDeleteForbidden,
            errors.MessageIdInvalid,
            errors.MsgIdInvalid,
        ) as exc:
            # Telegram can reject a protected service post in a mixed batch.
            # Split until the refused ID is isolated; never claim it deleted.
            # MessageDeleteForbidden is a 403 Forbidden, NOT a 400 BadRequest.
            # Catch only message-local refusals: permission/peer errors must
            # still stop the job, not be swallowed as undeletable messages.
            if len(ids) > 1:
                report.last_refusal_kind = "batch rejected; checking individual messages"
                logger.warning(
                    "bulk purge batch rejected: messages=%s error=%s",
                    len(ids), type(exc).__name__,
                )
                await publish_progress()
                if not hidden and all(
                    message_metadata.get(mid, ("message", None))[0] != "message"
                    for mid in ids
                ):
                    # Avoid a 2*N-1 binary tree for dense protected notices.
                    # Small probes still batch the deletable service clusters,
                    # rather than pessimistically sending every ID alone when
                    # only one notice is protected. A type is a batching hint,
                    # NEVER a deletion verdict. Every invoke gets fresh auth
                    # and every visible result is read back.
                    size = SERVICE_PROBE_SIZE if len(ids) > SERVICE_PROBE_SIZE else 1
                    for offset in range(0, len(ids), size):
                        await erase(ids[offset:offset + size])
                else:
                    middle = len(ids) // 2
                    await erase(ids[:middle], hidden=hidden)
                    await erase(ids[middle:], hidden=hidden)
                return
            if hidden:
                logger.info(
                    "bulk purge unreadable ID rejected: error=%s; outcome unverified",
                    type(exc).__name__,
                )
                await publish_progress()
                await _pause(stop, 0)
                return
            try:
                still_present = await verify(ids)
            except DeleteStopped:
                report.unverified += 1
                raise
            if still_present.local:
                report.remaining += 1
                report.refused += 1
            elif still_present.foreign or still_present.missing:
                # verify() already counted this ID as unverified.
                pass
            else:
                # It disappeared concurrently, so this engine did not
                # establish that our failed RPC deleted it.
                report.unverified += 1
            await record_outcome(ids, refused=len(still_present.local))
            return
        except DeleteStopped:
            if not stop.is_set() and not hidden:
                report.unverified += len(ids)
            raise
        except Exception:
            # A timeout may have reached Telegram; without a read-back it is
            # not honest to report this batch as deleted.
            if not hidden:
                report.unverified += len(ids)
            raise
        if hidden:
            # Reading these IDs again cannot distinguish previously absent
            # entries from successful deletion of bot-invisible messages.
            await publish_progress()
            return

        try:
            outcome = await verify(ids)
        except DeleteStopped:
            report.unverified += len(ids)
            raise
        report.deleted += len(outcome.empty)
        report.remaining += len(outcome.local)
        if outcome.local:
            # Telegram accepted the RPC but left some IDs in place.
            report.refused += len(outcome.local)
        await record_outcome(
            ids, deleted=len(outcome.empty), refused=len(outcome.local),
            refused_id=min(outcome.local) if outcome.local else None,
        )

    try:
        if (
            isinstance(maximum_id, bool)
            or not isinstance(maximum_id, int)
            or not 1 <= maximum_id <= 2_147_483_647
        ):
            raise DeleteStopped("Invalid group message boundary; no messages were deleted.")
        if start_id is None:
            cursor = maximum_id
        elif (
            isinstance(start_id, bool)
            or not isinstance(start_id, int)
            or not 0 <= start_id <= maximum_id
        ):
            raise DeleteStopped("Invalid resume cursor; no messages were deleted.")
        else:
            cursor = start_id
        await authorize()
        for upper in range(cursor, 0, -BATCH_SIZE):
            ids = list(range(max(1, upper - BATCH_SIZE + 1), upper + 1))
            # The initial authorization protects the first fetch.  Read-only
            # batches are checked once below before their checkpoint; every
            # batch that invokes deletion is checked by ``issue`` immediately
            # before that invoke.  Do not pay two live member lookups before
            # every read.
            batch_mutation_started = False
            hidden_submitted.clear()
            outcome = await read(ids)
            found = outcome.local
            report.found += len(found)
            # These IDs have been read, even if erasing them needs splitting.
            # Only the completed-batch checkpoint below advances the resume
            # cursor; partial progress must never be saved as completed work.
            report.scanned_ids += len(ids)
            report.pending = len(found)
            await publish_progress()
            # Keep protected service notices from poisoning normal-message
            # batches. All types are still attempted within the same boundary.
            ordinary = []
            services: dict[str, list[int]] = {}
            for mid in sorted(found):
                kind = message_metadata.get(mid, ("message", None))[0]
                if kind == "message":
                    ordinary.append(mid)
                else:
                    services.setdefault(kind, []).append(mid)
            await erase(ordinary)
            for service_ids in services.values():
                await erase(service_ids)
            # Readable messages go first; unknown IDs must not hold them up.
            # Foreign-peer evidence disqualifies this batch from unreadable-ID
            # attempts, even if peer resolution claims it is a supergroup.
            if not outcome.foreign:
                await erase(sorted(outcome.empty), hidden=True)
            if not batch_mutation_started:
                # In particular, an empty basic-group batch cannot issue a
                # hidden-ID request.  It still needs a live owner/right check
                # before its checkpoint so a revoked job cannot finish as if
                # that read had authorized it.
                await authorize()
            next_cursor = max(0, upper - BATCH_SIZE)
            await save_checkpoint(next_cursor)
            await publish_progress()
            # Yield for a prompt Stop callback, but never impose a normal
            # batch/refusal delay.  Server FloodWaits are handled in _rpc.
            await _pause(stop, 0)
        report.finished = True
        await save_checkpoint(0)
    except DeleteStopped as exc:
        report.reason = str(exc)
    except asyncio.CancelledError:
        report.reason = "The deletion task was cancelled."
        raise
    except Exception as exc:
        report.reason = f"Telegram request failed ({type(exc).__name__}); the scan stopped."
    return report


# Kept below the original implementation so deployments which import private
# helpers from this module retain those helpers.  Public callers use this v2
# binding (assigned at the end of the file).
async def _run_delete_two_pass(
    client, chat_id: int, owner_id: int, maximum_id: int, stop: asyncio.Event,
    progress: Callable[[DeleteReport], Awaitable[None]], *, start_id: int | None = None,
    prior_report: DeleteReport | None = None,
    checkpoint: Callable[[int, DeleteReport], Awaitable[None]] | None = None,
) -> DeleteReport:
    """Run ordinary and deferred passes over one immutable command boundary."""
    report = replace(
        prior_report, total_ids=maximum_id, finished=False, reason="",
        phase="", wait_seconds=0,
    ) if prior_report else DeleteReport(total_ids=maximum_id)
    metadata: dict[int, tuple[str, int | None]] = {}
    hidden_submitted: set[int] = set()
    mutation_started = False
    active_deferred_ids: set[int] = set()

    def settle_deferred(ids) -> None:
        """Remove outcomes already counted from an interrupted <=8 unit."""
        nonlocal active_deferred_ids
        settled = active_deferred_ids.intersection(ids)
        if not settled:
            return
        active_deferred_ids -= settled
        report.phase_pending_ids = [
            mid for mid in report.phase_pending_ids if mid not in settled
        ]
        report.phase_pending_hidden_ids = [
            mid for mid in report.phase_pending_hidden_ids if mid not in settled
        ]
        report.pending = len(report.phase_pending_ids)

    async def publish():
        try:
            await progress(replace(
                report, phase_pending_ids=list(report.phase_pending_ids),
                phase_pending_hidden_ids=list(report.phase_pending_hidden_ids),
            ))
        except Exception:
            pass

    async def call(phase, operation):
        old_phase, old_wait = report.phase, report.wait_seconds
        report.phase, report.wait_seconds = phase, 0

        async def waited(seconds):
            report.wait_seconds = seconds
            await publish()
        try:
            if report.scanned_ids or report.pending:
                await publish()
            return await _rpc(stop, operation, on_wait=waited)
        finally:
            report.phase, report.wait_seconds = old_phase, old_wait

    async def authorize():
        if stop.is_set():
            raise DeleteStopped("Stopped by the owner.")
        # Rights first, owner second, Stop last: repeat this directly before
        # every invoke, including split children and FloodWait retries.
        if not await call("authorize", lambda: delete_right(client, chat_id)):
            raise DeleteStopped("Maki no longer has Delete messages permission.")
        if not await call("authorize", lambda: live_owner(client, chat_id, owner_id)):
            raise DeleteStopped("The initiating user is no longer the group owner.")
        if stop.is_set():
            raise DeleteStopped("Stopped by the owner.")

    async def save():
        if checkpoint is None:
            return
        old_phase, old_wait = report.phase, report.wait_seconds
        report.phase, report.wait_seconds = "checkpoint", 0
        try:
            await checkpoint(
                report.phase_cursor,
                replace(report, phase=old_phase, wait_seconds=old_wait,
                        phase_pending_ids=list(report.phase_pending_ids),
                        phase_pending_hidden_ids=list(report.phase_pending_hidden_ids)),
            )
        finally:
            report.phase, report.wait_seconds = old_phase, old_wait

    async def read(ids, *, account):
        try:
            messages = await call(
                "fetch", lambda: client.get_messages(chat_id, ids, replies=0)
            )
        except DeleteStopped:
            # Stop/authorization/FloodWait control flow can happen before the
            # request.  It is not evidence about these IDs and must remain
            # stable across an explicitly confirmed resume.
            raise
        except Exception:
            report.unverified += len(ids)
            raise
        try:
            outcome = _inspect_messages(messages, chat_id, ids)
        except DeleteStopped:
            # This is a malformed Telegram result, not a control stop.
            report.unverified += len(ids)
            raise
        if outcome.missing:
            report.unverified += len(ids)
            raise DeleteStopped(
                "Telegram returned incomplete message coverage; the batch is unverified."
            )
        if account:
            report.inaccessible += len(outcome.foreign)
            report.absent_or_hidden += len(outcome.empty)
        metadata.clear()
        for message in messages:
            mid = _message_id(message)
            if mid not in outcome.local:
                continue
            kind = str(getattr(getattr(message, "service", None), "value", None)
                       or "message")[:64]
            date, age = getattr(message, "date", None), None
            if isinstance(date, datetime):
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                age = max(0, int(
                    (datetime.now(timezone.utc) - date).total_seconds() // 3600
                ))
            metadata[mid] = (kind, age)
        return outcome

    async def verify(ids):
        if stop.is_set():
            raise DeleteStopped("Deletion was sent, but the result is unverified.")
        try:
            outcome = _inspect_messages(
                await call("verify", lambda: client.get_messages(chat_id, ids, replies=0)),
                chat_id, ids,
            )
            report.unverified += len(outcome.missing) + len(outcome.foreign)
            return outcome
        except DeleteStopped:
            raise
        except Exception:
            raise DeleteStopped(
                "Deletion was sent, but Telegram could not confirm the result."
            ) from None

    async def outcome(ids, *, refused=0, refused_id=None, already_settled=False):
        if already_settled or active_deferred_ids.intersection(ids):
            report.pending = len(report.phase_pending_ids)
        else:
            report.pending = max(0, report.pending - len(ids))
        if refused:
            kind, age = metadata.get(
                refused_id if refused_id is not None else ids[0], ("message", None)
            )
            report.last_refusal_kind = kind
            logger.warning("bulk purge refusal: kind=%s age_hours=%s refused=%s",
                           kind, age, report.refused)
        await publish()
        if refused:
            await _pause(stop, 0)

    async def erase(ids, *, hidden=False, ordinary=False):
        nonlocal mutation_started
        if not ids:
            return
        if hidden and utils.get_peer_type(chat_id) != "channel":
            return
        try:
            request = await call(
                "prepare", lambda: _prepare_delete_request(client, chat_id, ids)
            )
        except DeleteStopped:
            if not stop.is_set() and not hidden:
                report.unverified += len(ids)
            raise
        except Exception:
            if not hidden:
                report.unverified += len(ids)
            raise
        if hidden and not _is_group_scoped(request, chat_id):
            return
        if not hidden and (
            isinstance(request, raw.functions.channels.DeleteMessages)
            or utils.get_peer_type(chat_id) == "channel"
        ) and not _is_group_scoped(request, chat_id):
            report.unverified += len(ids)
            raise DeleteStopped("Resolved deletion request does not match this group.")

        async def issue():
            nonlocal mutation_started
            await authorize()
            if hidden:
                new = set(ids) - hidden_submitted
                hidden_submitted.update(new)
                report.hidden_attempted += len(new)
                # Empty reads are evidence only of an attempt, never messages.
                report.unverified += len(new)
            mutation_started = True
            return await client.invoke(
                request, retries=1, sleep_threshold=0, retry_delay=0
            )
        try:
            await call("delete", issue)
        except (errors.MessageDeleteForbidden, errors.MessageIdInvalid, errors.MsgIdInvalid):
            if len(ids) > 1:
                # Deferred units are <=8.  Dense protected notices therefore
                # cannot hold the older ordinary pass hostage or replay 100 IDs.
                if not hidden and all(metadata.get(mid, ("message", None))[0] != "message"
                                      for mid in ids):
                    size = SERVICE_PROBE_SIZE if len(ids) > SERVICE_PROBE_SIZE else 1
                    for offset in range(0, len(ids), size):
                        await erase(ids[offset:offset + size],
                                    hidden=hidden, ordinary=ordinary)
                else:
                    middle = len(ids) // 2
                    await erase(ids[:middle], hidden=hidden, ordinary=ordinary)
                    await erase(ids[middle:], hidden=hidden, ordinary=ordinary)
                return
            if hidden:
                settle_deferred(ids)
                await publish()
                await _pause(stop, 0)
                return
            visible = await verify(ids)
            if visible.local:
                report.remaining += 1
                report.refused += 1
            elif not (visible.foreign or visible.missing):
                report.unverified += 1
            settle_deferred(ids)
            await outcome(ids, refused=len(visible.local), already_settled=True)
            return
        except DeleteStopped:
            if not stop.is_set() and not hidden:
                report.unverified += len(ids)
            raise
        except Exception:
            if not hidden:
                report.unverified += len(ids)
            raise
        if hidden:
            settle_deferred(ids)
            await publish()
            return
        visible = await verify(ids)
        report.deleted += len(visible.empty)
        if ordinary:
            report.ordinary_deleted += len(visible.empty)
        report.remaining += len(visible.local)
        report.refused += len(visible.local)
        await outcome(ids, refused=len(visible.local),
                      refused_id=min(visible.local) if visible.local else None)
        settle_deferred(ids)

    try:
        if (isinstance(maximum_id, bool) or not isinstance(maximum_id, int)
                or not 1 <= maximum_id <= 2_147_483_647):
            raise DeleteStopped("Invalid group message boundary; no messages were deleted.")
        if start_id is not None and (
            isinstance(start_id, bool) or not isinstance(start_id, int)
            or not 0 <= start_id <= maximum_id
        ):
            raise DeleteStopped("Invalid resume cursor; no messages were deleted.")
        if report.pass_phase not in {"ordinary", "deferred"}:
            raise DeleteStopped("Invalid saved deletion phase; no messages were deleted.")
        pending = report.phase_pending_ids
        hidden_pending = report.phase_pending_hidden_ids
        if (not isinstance(pending, list) or len(pending) > BATCH_SIZE
                or len(set(pending)) != len(pending)
                or any(isinstance(mid, bool) or not isinstance(mid, int)
                       or not 1 <= mid <= maximum_id for mid in pending)):
            raise DeleteStopped("Invalid saved deferred IDs; no messages were deleted.")
        if (not isinstance(hidden_pending, list)
                or len(hidden_pending) > BATCH_SIZE
                or len(set(hidden_pending)) != len(hidden_pending)
                or not set(hidden_pending).issubset(pending)):
            raise DeleteStopped("Invalid saved hidden deferred IDs; no messages were deleted.")
        cursor = report.phase_cursor if (
            report.phase_cursor or report.pass_phase == "deferred"
        ) else (maximum_id if start_id is None else start_id)
        if not 0 <= cursor <= maximum_id:
            raise DeleteStopped("Invalid saved phase cursor; no messages were deleted.")
        await authorize()

        if report.pass_phase == "ordinary":
            for upper in range(cursor, 0, -BATCH_SIZE):
                ids = list(range(max(1, upper - BATCH_SIZE + 1), upper + 1))
                mutation_started = False
                first_read = await read(ids, account=True)
                found = first_read.local
                report.found += len(found)
                report.scanned_ids += len(ids)
                ordinary = [mid for mid in sorted(found)
                            if metadata.get(mid, ("message", None))[0] == "message"]
                report.deferred_services += len(found) - len(ordinary)
                report.deferred_hidden += len(first_read.empty)
                report.pending = len(ordinary)
                await publish()
                await erase(ordinary, ordinary=True)
                if not mutation_started:
                    await authorize()
                report.phase_cursor = max(0, upper - BATCH_SIZE)
                await save()
                await publish()
                await _pause(stop, 0)
            report.pass_phase, report.phase_cursor = "deferred", maximum_id
            report.phase_pending_ids, report.phase_pending_hidden_ids = [], []
            report.pending = 0
            await save()
            await publish()

        while report.pass_phase == "deferred":
            if not report.phase_pending_ids:
                if report.phase_cursor <= 0:
                    break
                upper = report.phase_cursor
                ids = list(range(max(1, upper - BATCH_SIZE + 1), upper + 1))
                mutation_started = False
                fetched = await read(ids, account=False)
                services = [mid for mid in sorted(fetched.local)
                            if metadata.get(mid, ("message", None))[0] != "message"]
                # This pass does not delete a newly visible ordinary message:
                # it was not safely eligible in pass one.  Do not relabel it
                # as an empty/hidden success; expose the conservative outcome.
                report.unverified += len(fetched.local) - len(services)
                hidden = (
                    sorted(fetched.empty)
                    if not fetched.foreign and utils.get_peer_type(chat_id) == "channel"
                    else []
                )
                report.service_examined += len(services)
                report.phase_cursor = max(0, upper - BATCH_SIZE)
                report.phase_pending_ids = services + hidden
                report.phase_pending_hidden_ids = list(hidden)
                report.pending = len(report.phase_pending_ids)
                if not report.phase_pending_ids:
                    await authorize()
                    await save()
                    await publish()
                    await _pause(stop, 0)
                    continue
            hidden_set = set(report.phase_pending_hidden_ids)
            service_pending = [
                mid for mid in report.phase_pending_ids if mid not in hidden_set
            ]
            # Start an all-service cluster with one optimistic 100-ID invoke:
            # ordinary pass has already protected older normal messages from a
            # rejection.  If Telegram rejects it, erase() falls back to
            # 8-ID probes and then singletons.  This preserves the fast path
            # for deletable service histories without making refusals replay a
            # 100-ID mutation after a Stop.
            unit = (
                service_pending[:BATCH_SIZE]
                if service_pending else report.phase_pending_hidden_ids[:BATCH_SIZE]
            )
            mutation_started = False
            active_deferred_ids = set(unit)
            # The full 100-ID read selected this bounded outstanding unit.
            # Read it again after a resumed confirmation (and immediately
            # before mutation) so a saved service classification is never
            # trusted as current Telegram state.
            current = await read(unit, account=False)
            service_unit = [mid for mid in sorted(current.local)
                            if metadata.get(mid, ("message", None))[0] != "message"]
            ordinary_unit = [
                mid for mid in sorted(current.local)
                if metadata.get(mid, ("message", None))[0] == "message"
            ]
            hidden_unit = [] if current.foreign else sorted(current.empty)
            # A candidate can change classification between the range re-read
            # and the immediate unit re-read.  Never silently settle that
            # state: ordinary is explicitly still present + unverified, and a
            # foreign peer is inaccessible/unknown rather than hidden.
            if ordinary_unit:
                report.remaining += len(ordinary_unit)
                report.unverified += len(ordinary_unit)
            if current.foreign:
                report.inaccessible += len(current.foreign)
                report.unverified += len(current.foreign)
            if service_pending:
                await erase(service_unit)
                await erase(hidden_unit, hidden=True)
                completed = set(unit)
            else:
                # A formerly hidden ID can become readable between the 100-ID
                # selection read and this re-read.  Leave that service in the
                # durable service queue for a later <=8 mutation; it must not
                # be smuggled into the normal hidden 100-ID request.
                late_services = set(service_unit)
                if late_services:
                    report.phase_pending_hidden_ids = [
                        mid for mid in report.phase_pending_hidden_ids
                        if mid not in late_services
                    ]
                await erase(hidden_unit, hidden=True)
                completed = set(unit) - late_services
            if not mutation_started:
                await authorize()
            report.phase_pending_ids = [
                mid for mid in report.phase_pending_ids if mid not in completed
            ]
            report.phase_pending_hidden_ids = [
                mid for mid in report.phase_pending_hidden_ids if mid not in completed
            ]
            report.pending = len(report.phase_pending_ids)
            active_deferred_ids.clear()
            await save()
            await publish()
            await _pause(stop, 0)
        report.finished = True
        report.phase_cursor, report.phase_pending_ids = 0, []
        report.phase_pending_hidden_ids, report.pending = [], 0
        await save()
    except DeleteStopped as exc:
        report.reason = str(exc)
        if report.pass_phase == "deferred":
            try:
                await save()
            except Exception:
                pass
    except asyncio.CancelledError:
        report.reason = "The deletion task was cancelled."
        raise
    except Exception as exc:
        report.reason = f"Telegram request failed ({type(exc).__name__}); the scan stopped."
        # A successful child of an optimistic service probe may already have
        # been settled before a later RPC fails.  Persist the exact remainder;
        # a future owner confirmation never replays that completed child.
        if report.pass_phase == "deferred":
            try:
                await save()
            except Exception:
                pass
    return report


run_delete = _run_delete_two_pass