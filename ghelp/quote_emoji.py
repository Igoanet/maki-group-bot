"""Transfer bounded custom-emoji thumbnails through the bot session.

The quote renderer must not make its own Bot API request for an emoji.  The
authenticated Pyrogram client does the lookup and this module passes only
validated, request-scoped image data to the renderer.
"""

import asyncio
import base64
import logging
import time
from collections import OrderedDict


log = logging.getLogger(__name__)

# Telegram can return a thumb slowly on a cold file-cache miss.  Keep the
# individual operations bounded, but allow one retry inside a modest whole
# request deadline.  The outer deadline is deliberately longer than the old
# 5s lookup + 3s download combination.
_TOTAL_TIMEOUT = 14
_LOOKUP_TIMEOUT = 6
_DOWNLOAD_TIMEOUT = 5
_DOWNLOAD_RETRIES = 1

_MAX_IDS = 8
_MAX_ASSET_BYTES = 100_000
_DOWNLOAD_CONCURRENCY = 4

# Only successful custom-emoji image assets live here.  This cache contains no
# avatars or files on disk, and each request still receives a new dictionary.
_CACHE_TTL = 300
_CACHE_MAX = 64
_asset_cache = OrderedDict()

# A batch lookup owns one task per requested id set.  Every id in the batch
# points at that task, so overlapping requests for the same bot/client and id
# share both the lookup and thumbnail download.
_asset_inflight = {}
_download_gates = {}


def _client_key(app):
    """Use the authenticated client identity, never a Bot API credential."""
    return id(app)


def _normalise_id(value):
    try:
        value = str(value)
    except Exception:
        return None
    if not value.isdigit() or int(value) <= 0:
        return None
    return value


def _copy_asset(asset):
    # Asset values are scalars, but copying prevents a caller from mutating
    # the shared RAM cache through its request payload.
    return dict(asset)


def _prune_cache(now=None):
    if now is None:
        now = time.monotonic()
    expired = [
        key for key, (expires, _asset) in _asset_cache.items()
        if expires <= now
    ]
    for key in expired:
        _asset_cache.pop(key, None)
    while len(_asset_cache) > _CACHE_MAX:
        _asset_cache.popitem(last=False)


def _cached_asset(app, emoji_id):
    now = time.monotonic()
    _prune_cache(now)
    key = (_client_key(app), emoji_id)
    entry = _asset_cache.get(key)
    if entry is None:
        return None
    # Refresh insertion order while preserving the original bounded TTL.
    _asset_cache.move_to_end(key)
    return _copy_asset(entry[1])


def _cache_asset(app, emoji_id, asset):
    now = time.monotonic()
    _prune_cache(now)
    key = (_client_key(app), emoji_id)
    _asset_cache[key] = (now + _CACHE_TTL, _copy_asset(asset))
    _asset_cache.move_to_end(key)
    _prune_cache(now)


def _valid_image(raw):
    """Return whether *raw* is a small PNG, JPEG, or WebP image."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return False
    raw = bytes(raw)
    if not raw or len(raw) > _MAX_ASSET_BYTES:
        return False
    return (
        raw.startswith(b"\x89PNG\r\n\x1a\n")
        or raw.startswith(b"\xff\xd8\xff")
        or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")
    )


def _stream_bytes(downloaded):
    if isinstance(downloaded, (bytes, bytearray, memoryview)):
        return bytes(downloaded)
    getter = getattr(downloaded, "getvalue", None)
    if callable(getter):
        raw = getter()
    else:
        reader = getattr(downloaded, "read", None)
        if not callable(reader):
            return None
        raw = reader()
    return bytes(raw) if isinstance(raw, (bytes, bytearray, memoryview)) else None


def _download_gate(app):
    """Get a semaphore shared by this client within the current event loop."""
    loop = asyncio.get_running_loop()
    key = (_client_key(app), id(loop))
    gate = _download_gates.get(key)
    if gate is None:
        gate = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)
        _download_gates[key] = gate
    return gate


async def _download_candidate(app, candidate, deadline):
    file_id = getattr(candidate, "file_id", None)
    if not file_id:
        return None
    declared_size = getattr(candidate, "file_size", None) or 0
    if declared_size > _MAX_ASSET_BYTES:
        return None

    gate = _download_gate(app)
    attempts = _DOWNLOAD_RETRIES + 1
    for attempt in range(attempts):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        acquired = False
        try:
            await asyncio.wait_for(gate.acquire(), timeout=remaining)
            acquired = True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            downloaded = await asyncio.wait_for(
                app.download_media(file_id, in_memory=True),
                timeout=min(_DOWNLOAD_TIMEOUT, remaining),
            )
            raw = _stream_bytes(downloaded) if downloaded is not None else None
            # Invalid bytes are not a cold-cache failure.  Try another actual
            # thumb/static sticker candidate rather than retrying TGS/garbage.
            if _valid_image(raw):
                return bytes(raw)
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt + 1 < attempts:
                log.info("Quote emoji thumbnail retry: %s", type(exc).__name__)
            else:
                log.info(
                    "Quote emoji thumbnail unavailable: %s",
                    type(exc).__name__,
                )
        finally:
            if acquired:
                gate.release()
    return None


def _sticker_candidates(sticker):
    thumbs = getattr(sticker, "thumbs", None) or []
    candidates = sorted(
        thumbs,
        key=lambda thumb: getattr(thumb, "width", 0) or 0,
        reverse=True,
    )
    # Animated/video custom emoji are represented by TGS/WEBM documents:
    # only their actual image thumbnails are safe to hand to quote-api.
    if not getattr(sticker, "is_animated", False) and not getattr(
        sticker, "is_video", False
    ):
        # Keep two real thumbs plus the static sticker itself.  The latter is
        # a useful fallback when Telegram's thumb is cold, and remains safe
        # because its bytes go through the same image-signature validation.
        return candidates[:2] + [sticker]
    return candidates[:3]


def _asset_from(sticker, raw):
    asset = {
        "data": base64.b64encode(raw).decode("ascii"),
        "repaint": bool(getattr(sticker, "needs_repainting", False)),
    }
    # A missing emoji must not become a made-up star/clown in the renderer.
    emoji = getattr(sticker, "emoji", None)
    if isinstance(emoji, str) and emoji:
        asset["emoji"] = emoji
    return asset


async def _fetch_batch(app, ids):
    """Lookup and download a batch, caching only validated successes."""
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TOTAL_TIMEOUT
    try:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return {}
        stickers = await asyncio.wait_for(
            app.get_custom_emoji_stickers([int(emoji_id) for emoji_id in ids]),
            timeout=min(_LOOKUP_TIMEOUT, remaining),
        )
        by_id = {}
        for sticker in stickers or []:
            emoji_id = _normalise_id(
                getattr(sticker, "custom_emoji_id", None)
            )
            if emoji_id in ids and emoji_id not in by_id:
                by_id[emoji_id] = sticker

        async def load(emoji_id, sticker):
            for candidate in _sticker_candidates(sticker):
                raw = await _download_candidate(app, candidate, deadline)
                if raw is not None:
                    return emoji_id, _asset_from(sticker, raw)
            return emoji_id, None

        results = await asyncio.gather(
            *(load(emoji_id, sticker) for emoji_id, sticker in by_id.items()),
            return_exceptions=True,
        )
        loaded = {}
        for result in results:
            if isinstance(result, tuple) and result[1] is not None:
                emoji_id, asset = result
                _cache_asset(app, emoji_id, asset)
                loaded[emoji_id] = _copy_asset(asset)
        return loaded
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.info("Quote emoji lookup unavailable: %s", type(exc).__name__)
        return {}
    finally:
        for emoji_id in ids:
            key = (_client_key(app), emoji_id)
            if _asset_inflight.get(key) is task:
                _asset_inflight.pop(key, None)


async def _load_assets(app, ids):
    """Get assets, joining an existing per-client/id request where possible."""
    tasks = {}
    owners = []
    for emoji_id in ids:
        cached = _cached_asset(app, emoji_id)
        if cached is not None:
            tasks[emoji_id] = cached
            continue
        key = (_client_key(app), emoji_id)
        task = _asset_inflight.get(key)
        if task is None:
            owners.append(emoji_id)
        else:
            tasks[emoji_id] = task

    if owners:
        task = asyncio.create_task(_fetch_batch(app, owners))
        for emoji_id in owners:
            _asset_inflight[(_client_key(app), emoji_id)] = task
            tasks[emoji_id] = task

    assets = {}
    for emoji_id in ids:
        value = tasks.get(emoji_id)
        if isinstance(value, dict):
            assets[emoji_id] = value
            continue
        try:
            # A cancelled caller must not cancel the shared cold-thumbnail
            # request.  Its successful result may serve a later quote.
            result = await asyncio.shield(value)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("Quote emoji thumbnail unavailable: %s", type(exc).__name__)
            continue
        asset = result.get(emoji_id)
        if asset is not None:
            assets[emoji_id] = _copy_asset(asset)
    return assets


def _collect_ids(messages):
    """Return status IDs first, then body custom-emoji IDs, capped at eight."""
    statuses = []
    body = []

    def add(values, target):
        for value in values:
            emoji_id = _normalise_id(value)
            if emoji_id and emoji_id not in statuses and emoji_id not in body:
                target.append(emoji_id)

    # A status changes the sender header and is more important than an
    # arbitrary body emoji when Telegram's eight-id limit is reached.
    for message in messages:
        if not isinstance(message, dict):
            continue
        add([message.get("from", {}).get("emoji_status")], statuses)
    for message in messages:
        if not isinstance(message, dict):
            continue
        add(
            (
                entity.get("custom_emoji_id")
                for entity in message.get("entities", [])
                if isinstance(entity, dict)
            ),
            body,
        )
    return (statuses + body)[:_MAX_IDS], set(statuses)


async def _hydrate(payload, app):
    messages = payload.get("messages", [])
    ids, _ = _collect_ids(messages)
    if not ids:
        return
    assets = await _load_assets(app, ids)
    for message in messages:
        if not isinstance(message, dict):
            continue
        scoped = {}
        for emoji_id, asset in assets.items():
            scoped[emoji_id] = _copy_asset(asset)
        message["customEmojiAssets"] = scoped


async def hydrate_emoji(payload, app):
    """Populate validated custom-emoji assets without leaking request state."""
    messages = payload.get("messages", [])
    for message in messages:
        if isinstance(message, dict):
            # Deliberately reset on every request, even when lookup fails.
            message["customEmojiAssets"] = {}
    try:
        await asyncio.wait_for(_hydrate(payload, app), timeout=_TOTAL_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        log.info("Quote emoji hydration timed out")