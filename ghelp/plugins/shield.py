"""Shield — protect the group from ban-attack content and piracy links.

Attackers post illegal/adult media or sticker packs and mass-report the group
to get it banned (research 2026-09-06: NACBots/group-guardian,
TelechaBot/anti-hentai-bot, ishikki-akabane/GuardianBot, tg.midaccs anti-bomb
guide). Shield layers four defenses, ALL gated behind the master switch:

  /shield on|off              — master switch (default OFF; nothing runs until enabled)
  /shield scan all|new|off    — AI-scan photos/stickers for illegal/adult content
                                (default "new": only members inside the new-hours
                                window get scanned — attackers arrive on fresh
                                accounts, and it keeps AI-call volume low)
  /shield newhours <1-168>    — how long a joiner counts as "new" (default 24h)
  /shield medialock on|off    — new members can't post ANY media at all
  /shield banpack             — reply to a sticker: block its whole pack on sight
  /shield allowpack           — reply to a sticker: unblock its pack
  /shield copyright on|off    — delete piracy-site links / pirate-movie spam

Verdict actions: illegal/CSAM-looking -> delete + BAN; adult sexual -> delete.
AI errors fail OPEN (nothing deleted) — shield must never nuke normal content
because the scanner hiccuped. AI scanning uses the Replit AI-integrations
OpenAI proxy (gpt-5-mini, image input); every scanned image costs one call.

Join times are tracked in memory only: after a bot restart, members who joined
shortly before are not treated as "new" until the window lapses.

Per-chat state: shield_on / shield_scan / shield_new_h / shield_media_lock /
shield_packs / shield_copyright — see database.DEFAULTS.
"""

import asyncio
import base64
import html
import json
import logging
import os
import re
import time

import aiohttp
from pyrogram import enums, filters, types

from ghelp import app
from ghelp.approval_trust import is_exempt
from ghelp.database import db
from ghelp.helpers import is_admin, require_admin
from ghelp.plugins.logchan import log_event

logger = logging.getLogger(__name__)

_MODEL = "gpt-5-mini"
_MAX_IMG_BYTES = 10 * 1024 * 1024  # skip absurdly large media
_SCAN_SEM = asyncio.Semaphore(3)   # never pile up AI calls
_JOINS: dict[int, dict[int, float]] = {}  # chat_id -> {user_id: join_ts} (RAM only)
_MAX_JOINS = 1000  # per chat — beyond this, evict oldest (only a raid fills it)

_MEDIA_KINDS = ("photo", "sticker", "animation", "video", "document",
                "video_note", "voice", "audio")
_SCANNABLE = ("photo", "sticker", "animation", "video")

# piracy domains / spam phrases (Indian context — the user's groups see these)
_PIRACY = (
    "vegamovies", "filmyzilla", "movierulz", "tamilrockers", "katmoviehd",
    "moviesflix", "9xmovies", "worldfree4u", "bolly4u", "hdhub4u",
    "extramovies", "filmywap", "mp4moviez", "skymovieshd", "desiremovies",
    "jalshamoviez", "khatrimaza", "thepiratebay", "1337x", "rarbg",
    "yts.mx", "eztv.", "limetorrents", "torrentgalaxy",
    "full movie free download", "full movie download link", "leaked movie download",
    # categories from DAXXTEAM/COPYRIGHT2: cracked apps, streaming accounts,
    # paid-course piracy
    "mod apk", "modded apk", "cracked app", "free netflix account",
    "netflix premium free", "spotify premium free", "udemy course free",
    "paid course free download",
)

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s",
                       "7": "t", "@": "a", "$": "s", "!": "i", "|": "i"})


def _norm(text: str) -> str:
    """Lowercase + de-leet + collapse non-alphanumerics, so obfuscation like
    'C.P l!nk', 'c_p' or 'c h i l d' all match the same banned term."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower().translate(_LEET))


# CSAM-solicitation vocabulary seen in group-ban attacks — matched AFTER
# _norm collapsing (source study 2026-09-06: every serious protection bot —
# group-guardian, GuardianBot, COPYRIGHT2 — carries a banned-words layer;
# media-only AI scanning misses text-only attacks). Text hits are
# DELETE-ONLY — never a ban (see the watcher for the policy rationale).
_ILLEGAL_TERMS = tuple(_norm(t) for t in (
    "child porn", "child sex", "child nude", "kiddie porn", "pedo porn",
    "pedo video", "minor sex", "underage sex", "preteen sex",
    "cp link", "cp links", "cp video", "cp videos", "cp group", "cp channel",
))

_SCAN_PROMPT = (
    "You are a strict safety filter for a Telegram group. Analyze this image "
    "and reply with ONLY minified JSON, no markdown, no extra words:\n"
    '{"illegal": bool, "sexual": bool, "reason": "<5 words>"}\n'
    "illegal = true ONLY for child sexual abuse material or content that is "
    "clearly illegal everywhere (any person who could be under 18 in a sexual "
    "or suggestive context counts as illegal).\n"
    "sexual = true for adult pornographic/nude sexual content.\n"
    "Normal photos, memes, anime art without explicit sexual content, and "
    "clothed people are BOTH false. When unsure, answer false."
)


def _media_kind(m: types.Message) -> str | None:
    for kind in _MEDIA_KINDS:
        if getattr(m, kind, None):
            return kind
    return None


def _media_feature(kind: str | None) -> str:
    """Choose the narrow exemption for a broad media-lock rule."""
    if kind == "sticker":
        return "stickers"
    if kind == "animation":
        return "gifs"
    return "media"


def _parse_verdict(text: str) -> dict | None:
    """Tolerant JSON extraction from the model reply; None on any failure."""
    try:
        match = re.search(r"\{.*\}", text or "", re.S)
        if not match:
            return None
        v = json.loads(match.group(0))
        illegal, sexual = v.get("illegal"), v.get("sexual")
        # only REAL JSON booleans count — a schema-invalid reply (e.g. the
        # string "false", which bool() would make truthy -> ban!) fails OPEN
        if not isinstance(illegal, bool) or not isinstance(sexual, bool):
            return None
        return {"illegal": illegal, "sexual": sexual,
                "reason": str(v.get("reason", ""))[:80]}
    except Exception:
        return None


def _verdict_action(v: dict | None) -> str | None:
    """'ban' | 'delete' | None — None also when the verdict is unusable."""
    if not v:
        return None
    if v["illegal"]:
        return "ban"
    if v["sexual"]:
        return "delete"
    return None


def _join_ts(chat_id: int, user_id: int) -> float | None:
    return _JOINS.get(chat_id, {}).get(user_id)


def _is_new(chat_id: int, user_id: int, window_h: int) -> bool:
    ts = _join_ts(chat_id, user_id)
    # unknown join time (bot restarted / member predates tracking) -> not new
    return ts is not None and (time.time() - ts) < window_h * 3600


def _prune_joins(joins: dict, window_h: int) -> None:
    """Drop entries past the window and hard-cap the map (evicting oldest),
    so the tracker stays bounded in both quiet and raid-flooded chats."""
    horizon = time.time() - window_h * 3600
    for uid, ts in list(joins.items()):
        if ts < horizon:
            del joins[uid]
    while len(joins) > _MAX_JOINS:
        del joins[min(joins, key=joins.get)]


async def _download_image(m: types.Message, kind: str):
    """(bytes, mime) for the AI scan, or None. Videos/animations and
    animated/video stickers (.tgs/.webm aren't images) -> static thumbnail."""
    try:
        media = getattr(m, kind, None)
        wants_thumb = kind in ("video", "animation") or (
            kind == "sticker" and (getattr(media, "is_animated", False)
                                   or getattr(media, "is_video", False)))
        if wants_thumb:
            thumbs = getattr(media, "thumbs", None)
            if not thumbs:
                return None
            bio = await app.download_media(thumbs[-1].file_id, in_memory=True)
            return (bio.getvalue(), "image/jpeg") if bio else None
        size = getattr(media, "file_size", 0) or 0
        if size > _MAX_IMG_BYTES:
            return None
        bio = await app.download_media(m, in_memory=True)
        if not bio:
            return None
        mime = "image/webp" if kind == "sticker" else "image/jpeg"
        return bio.getvalue(), mime
    except Exception as exc:
        logger.info("shield: download failed: %s", exc)
        return None


async def _scan_image(img: bytes, mime: str) -> dict | None:
    """One chat-completions call with the image; None on any failure (fail-open)."""
    base = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")
    key = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY")
    if not base or not key:
        logger.warning("shield: AI-integrations env vars missing — scan skipped")
        return None
    payload = {"model": _MODEL, "messages": [{"role": "user", "content": [
        {"type": "text", "text": _SCAN_PROMPT},
        {"type": "image_url", "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(img).decode()}"}},
    ]}]}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(base.rstrip("/") + "/chat/completions",
                              json=payload,
                              headers={"Authorization": f"Bearer {key}"},
                              timeout=aiohttp.ClientTimeout(total=45)) as r:
                data = await r.json(content_type=None)
        content = data["choices"][0]["message"]["content"]
        return _parse_verdict(content)
    except Exception as exc:
        logger.info("shield: scan call failed: %s", exc)
        return None


# ---------------------------------------------------------------- commands


@app.on_message(filters.group & filters.command("shield", prefixes=["/", "?", "!", "."]))
async def shield_command(_, m: types.Message) -> None:
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    args = [a.lower() for a in (m.command or [])[1:]]
    try:
        if not args:
            cfg = await db.get_chat(m.chat.id)
            packs = cfg["shield_packs"]
            await m.reply_text(
                "<b>Shield</b>\n"
                f"Status: <b>{'ON' if cfg['shield_on'] else 'OFF'}</b>\n"
                f"AI media scan: <b>{cfg['shield_scan']}</b>"
                f" (new = first {cfg['shield_new_h']}h after joining)\n"
                f"New-member media lock: <b>{'ON' if cfg['shield_media_lock'] else 'OFF'}</b>\n"
                f"Blocked sticker packs: <b>{len(packs)}</b>\n"
                f"Blacklisted words: <b>{len(cfg['shield_badwords'])}</b>"
                " (built-in illegal terms always active)\n"
                f"Piracy-link filter: <b>{'ON' if cfg['shield_copyright'] else 'OFF'}</b>\n\n"
                "<code>/shield on|off</code>\n"
                "<code>/shield scan all|new|off</code>\n"
                "<code>/shield newhours &lt;1-168&gt;</code>\n"
                "<code>/shield medialock on|off</code>\n"
                "<code>/shield banpack</code> / <code>allowpack</code> (reply to a sticker)\n"
                "<code>/shield blockword</code> / <code>unblockword</code> &lt;word&gt;\n"
                "<code>/shield copyright on|off</code>",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        if len(args) == 1 and args[0] in ("on", "off"):
            on = args[0] == "on"
            await db.update_chat(m.chat.id, shield_on=on)
            await m.reply_text(
                f"<b>Shield {'enabled' if on else 'disabled'}.</b>"
                + (" Configured protections are now active." if on else
                   " All protections paused."),
                parse_mode=enums.ParseMode.HTML,
            )
            return

        if len(args) == 2 and args[0] == "scan" and args[1] in ("all", "new", "off"):
            await db.update_chat(m.chat.id, shield_scan=args[1])
            note = {"all": "every member's media",
                    "new": "new members' media only",
                    "off": "AI scanning paused"}[args[1]]
            await m.reply_text(f"<b>AI scan:</b> {note}.",
                               parse_mode=enums.ParseMode.HTML)
            return

        if len(args) == 2 and args[0] == "newhours":
            try:
                hrs = int(args[1])
                if not 1 <= hrs <= 168:
                    raise ValueError
            except ValueError:
                await m.reply_text("<b>Usage:</b> <code>/shield newhours &lt;1-168&gt;</code>",
                                   parse_mode=enums.ParseMode.HTML)
                return
            await db.update_chat(m.chat.id, shield_new_h=hrs)
            await m.reply_text(f"Members count as <b>new for {hrs}h</b> after joining.",
                               parse_mode=enums.ParseMode.HTML)
            return

        if len(args) == 2 and args[0] in ("medialock", "copyright") \
                and args[1] in ("on", "off"):
            field = "shield_media_lock" if args[0] == "medialock" else "shield_copyright"
            on = args[1] == "on"
            await db.update_chat(m.chat.id, **{field: on})
            label = ("New-member media lock" if args[0] == "medialock"
                     else "Piracy-link filter")
            await m.reply_text(f"<b>{label} {'enabled' if on else 'disabled'}.</b>",
                               parse_mode=enums.ParseMode.HTML)
            return

        if len(args) == 1 and args[0] in ("banpack", "allowpack"):
            st = m.reply_to_message and getattr(m.reply_to_message, "sticker", None)
            set_name = getattr(st, "set_name", None) if st else None
            if not set_name:
                await m.reply_text("<b>Reply to a sticker</b> from the pack.",
                                   parse_mode=enums.ParseMode.HTML)
                return
            adding = args[0] == "banpack"
            outcome: dict = {}

            def _apply(cfg, _s=set_name, _add=adding, _out=outcome):
                packs = cfg["shield_packs"]
                if _add and _s not in packs:
                    packs.append(_s)
                    _out["changed"] = True
                elif not _add and _s in packs:
                    packs.remove(_s)
                    _out["changed"] = True

            await db.mutate_chat(m.chat.id, _apply)
            if outcome.get("changed"):
                state = ("<b>blocked</b> — its stickers will be deleted on sight"
                         if adding else "unblocked")
                await m.reply_text(
                    f"Sticker pack <code>{html.escape(set_name)}</code> {state}.",
                    parse_mode=enums.ParseMode.HTML,
                )
            else:
                await m.reply_text("Pack already in that state.",
                                   parse_mode=enums.ParseMode.HTML)
            return

        if len(args) >= 2 and args[0] in ("blockword", "unblockword"):
            phrase = _norm(" ".join(m.command[2:]))
            if not phrase:
                await m.reply_text(
                    "<b>Usage:</b> <code>/shield blockword &lt;word or phrase&gt;</code>",
                    parse_mode=enums.ParseMode.HTML,
                )
                return
            adding = args[0] == "blockword"
            outcome: dict = {}

            def _apply_word(cfg, _p=phrase, _add=adding, _out=outcome):
                words = cfg["shield_badwords"]
                if _add and _p not in words:
                    words.append(_p)
                    _out["changed"] = True
                elif not _add and _p in words:
                    words.remove(_p)
                    _out["changed"] = True

            await db.mutate_chat(m.chat.id, _apply_word)
            if outcome.get("changed"):
                state = ("<b>blacklisted</b> — messages containing it will be deleted"
                         if adding else "unblocked")
                await m.reply_text(
                    f"Word {state}: <code>{html.escape(phrase)}</code>",
                    parse_mode=enums.ParseMode.HTML,
                )
            else:
                await m.reply_text("Word already in that state.",
                                   parse_mode=enums.ParseMode.HTML)
            return

        await m.reply_text(
            "<b>Usage:</b>\n"
            "<code>/shield on|off</code>\n"
            "<code>/shield scan all|new|off</code>\n"
            "<code>/shield newhours &lt;h&gt;</code>\n"
            "<code>/shield medialock on|off</code>\n"
            "<code>/shield banpack|allowpack</code> (reply)\n"
            "<code>/shield blockword|unblockword &lt;w&gt;</code>\n"
            "<code>/shield copyright on|off</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await m.reply_text("<b>Could not update Shield.</b>",
                           parse_mode=enums.ParseMode.HTML)


# ---------------------------------------------------------------- watchers


@app.on_message(filters.new_chat_members, group=4)
async def shield_join_tracker(_, m: types.Message) -> None:
    """Remember join times (RAM) so 'new member' rules have something to check.
    Records even while Shield is OFF — protection enabled mid-attack must
    already know who the fresh joiners are."""
    try:
        cfg = await db.get_chat(m.chat.id)
        window = cfg["shield_new_h"] if cfg["shield_on"] else 24
        joins = _JOINS.setdefault(m.chat.id, {})
        _prune_joins(joins, window)
        now = time.time()
        for u in m.new_chat_members:
            if not u.is_bot:
                joins[u.id] = now
        _prune_joins(joins, window)  # a huge join event can't breach the cap
    except Exception as exc:
        logger.info("shield join tracker: %s", exc)


@app.on_message(filters.group & ~filters.service, group=6)
async def shield_watcher(_, m: types.Message) -> None:
    try:
        cfg = await db.get_chat(m.chat.id)
        if not cfg["shield_on"]:
            return
        sender = m.from_user
        if sender is None or sender.is_self:
            return  # anonymous admin / channel post / our own messages
        kind = _media_kind(m)
        raw_text = m.text or m.caption or ""
        text = raw_text.lower()
        norm = _norm(raw_text)

        # stale-joins hygiene on every relevant message (review: conditional
        # pruning left quiet chats' entries forever)
        joins = _JOINS.get(m.chat.id)
        if joins:
            _prune_joins(joins, cfg["shield_new_h"])

        # Cheap deterministic rules first; the LIVE admin check runs lazily
        # right before anything destructive/costly (code-review 2026-09-06: a
        # cached non-admin answer could delete/ban someone promoted <60s ago,
        # but a live lookup on every plain message would flood the API).
        st = getattr(m, "sticker", None)
        reasons: list[tuple[str, str | None]] = []
        if st and getattr(st, "set_name", None) \
                and st.set_name in cfg["shield_packs"]:
            reasons.append(("blocked sticker pack", "stickers"))
        if kind and cfg["shield_media_lock"] \
                and _is_new(m.chat.id, sender.id, cfg["shield_new_h"]):
            reasons.append((
                "media from a brand-new member",
                _media_feature(kind),
            ))
        if norm and any(t in norm for t in _ILLEGAL_TERMS):
            # Built-in illegal-content terms are high-risk and never
            # bypassed by an approval exemption.
            reasons.append(("illegal content terms", None))
        if norm and any(w in norm for w in cfg["shield_badwords"]):
            reasons.append(("blacklisted word", "blacklist_words"))
        if cfg["shield_copyright"] and text \
                and any(p in text for p in _PIRACY):
            # Piracy filtering is an attack-focused/high-risk rule, unlike
            # the configurable nuisance media/sticker/word rules above.
            reasons.append(("piracy link/spam", None))
        if reasons:
            if await is_admin(m.chat.id, sender.id):
                return  # admins are trusted
            # Shield's automatic filters are nuisance moderation too. Reload
            # approval state after the awaited role check so a concurrent
            # /approve wins over this message's initial settings snapshot.
            current = await db.get_chat(m.chat.id)
            # High-risk reasons deliberately win over exempt nuisance reasons:
            # an exempt sticker/media rule must not mask illegal terms or a
            # piracy hit later in the same message.
            high_risk = [item for item, feature in reasons if feature is None]
            if high_risk:
                reason = high_risk[0]
            else:
                reason = next(
                    (
                        item for item, feature in reasons
                        if not is_exempt(current, sender.id, feature)
                    ),
                    None,
                )
                if reason is None:
                    return
            # Deterministic text/pattern hits are DELETE-ONLY, forever —
            # keywords are too ambiguous for an irreversible action, and
            # repeating them raises message count, not confidence
            # (code-review 2026-09-06). The only auto-ban in Shield is a
            # high-confidence AI media verdict (double live admin checks).
            await _remove(m, sender, reason, ban=False)
            return

        # AI scan of scannable media
        mode = cfg["shield_scan"]
        if mode == "off" or kind not in _SCANNABLE:
            return
        if mode == "new" and not _is_new(m.chat.id, sender.id, cfg["shield_new_h"]):
            return
        if await is_admin(m.chat.id, sender.id):
            return  # trusted — and never burns an AI call on admins
        if _SCAN_SEM.locked():  # scanner saturated — drop rather than queue
            logger.info("shield: scanner busy, skipped msg %s", m.id)
            return
        async with _SCAN_SEM:
            img = await _download_image(m, kind)
            if not img:
                return
            verdict = await _scan_image(*img)
        action = _verdict_action(verdict)
        if action:
            # the scan can take up to 45s — re-check admin status LIVE right
            # before acting, or someone promoted mid-scan could get
            # deleted/banned (code-review 2026-09-06)
            if await is_admin(m.chat.id, sender.id):
                return
            await _remove(m, sender,
                          f"AI: {verdict.get('reason') or 'policy violation'}",
                          ban=(action == "ban"))
    except Exception as exc:
        logger.info("shield watcher: %s", exc)


async def _remove(m: types.Message, sender, why: str, ban: bool) -> None:
    """Delete the message (+ optionally ban), then post a short notice."""
    name = html.escape(getattr(sender, "first_name", None) or "user")
    try:
        await m.delete()
    except Exception as exc:
        logger.info("shield: delete failed (%s): %s", why, exc)
        return  # if we can't delete, a notice would just spam the chat
    banned = False
    if ban:
        try:
            await app.ban_chat_member(m.chat.id, sender.id)
            banned = True
        except Exception as exc:
            logger.info("shield: ban failed: %s", exc)
    try:
        note = (f"<b>Shield:</b> removed <a href=\"tg://user?id={sender.id}\">{name}</a>'s "
                f"message ({html.escape(why)})"
                + (", and <b>banned</b> them." if banned else
                   ". <i>Ban failed — check my rights.</i>" if ban else "."))
        await app.send_message(m.chat.id, note, parse_mode=enums.ParseMode.HTML)
    except Exception:
        pass
    ban_note = " + BANNED" if banned else ""
    await log_event(
        m.chat.id,
        f"Shield: <a href=\"tg://user?id={sender.id}\">{name}</a> — "
        f"{html.escape(why)}{ban_note}",
    )
