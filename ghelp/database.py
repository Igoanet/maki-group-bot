"""JSON persistence for the GroupHelp-style bot.

One file (data/ghelp.json), async-locked, atomic writes. Per-chat settings
merge over DEFAULTS so new keys appear automatically for old chats.
Follows the same local-JSON decision as the music bot (no external DB).
"""

import asyncio
import copy
import json
import os
from pathlib import Path

_PATH = Path("data/ghelp.json")
_LOCK = asyncio.Lock()

# every per-chat setting lives here — plugins read via db.get_chat() and
# persist via db.update_chat(chat_id, key=value, ...)
DEFAULTS = {
    # welcome / goodbye
    "welcome_on": False,
    "welcome_text": "",
    "goodbye_on": False,
    "goodbye_text": "",
    "clean_welcome": False,      # delete the previous welcome message
    "last_welcome_id": 0,
    # force-subscribe gate (/forcesubscribe)
    "fsub": 0,                 # channel id members must join before chatting
    "fsub_link": "",
    "fsub_title": "",
    "fsub_muted": [],          # uids whose CURRENT mute came from the gate
    # captcha for new members
    "captcha_on": False,
    "captcha_mute_min": 5,       # how long a failed/ignored captcha keeps the mute
    "captcha_mode": "button",    # button | math | text | text2
    "captcha_rules": False,
    "captcha_kick": False,
    "captcha_kick_time": 0,      # seconds; zero means use mute timeout
    "captcha_text": "I'm human",
    # notes (name -> content), per chat
    "notes": {},
    # Rose-style keyword filters (case-folded keyword -> saved response)
    "filters": {},
    # content locks: subset of url, sticker, forward, photo, video, gif,
    # document, audio, voice, bot_inline, phone, email, command
    "locks": [],
    # Separate owner-controlled link policy; legacy URL locks cannot bypass it.
    "linkdel_on": False,
    "linkdel_admins": False,    # owner confirms admin demotion before enabling
    "linkdel_revision": 0,     # revokes in-flight actions after policy changes
    "warn_link_seen": {},      # persistent deduplication of link warning events
    # warns
    "warns": {},                 # str(user_id) -> int
    "warn_limit": 3,             # 0 = unlimited (never auto-ban)
    "warn_action": "mute",       # mute | kick | ban
    # anti-flood
    "flood_on": False,
    "flood_limit": 5,            # messages
    "flood_timer_count": 0,      # timed antiflood count (0 = disabled)
    "flood_timer_secs": 30,      # timed antiflood period
    "flood_clear": False,        # delete messages which trigger a flood
    # edit guardian (delete edited messages)
    "editguard_on": False,
    "editguard_admin": False,    # also delete ADMINS' edited messages
    "editguard_window": 60,      # deletion delay in seconds after the latest edit
    # auto-delete (timed deletion for watched users / other bots)
    "autodel_bots": False,
    "autodel_group_mode": "off",  # independent bot-observed group timer
    "autodel_group_time": 10,
    "autodel_group_error": "",
    "autodel_group_revision": 0,  # cancels timers from an older group policy
    "autodel_legacy_revision": 0, # cancels timers from older legacy settings
    "autodel_instant_users": [],  # explicit instant-delete targets, separate from legacy delay
    "autodel_instant_generations": {},  # target id -> generation for instant ABA safety
    "autodel_users": [],         # list of user ids
    "autodel_time": 10,          # seconds before deletion
    # promote levels — rights granted by /promote (toggled via /promotepowers)
    "promote_rights": ["invite_users", "pin_messages", "manage_video_chats", "manage_tags"],
    # shield — illegal/adult media & piracy protection (master switch OFF)
    "shield_on": False,
    "shield_scan": "new",      # off | new | all — whose media gets AI-scanned
    "shield_new_h": 24,        # hours a joiner counts as "new"
    "shield_media_lock": False,  # new members can't post any media
    "shield_packs": [],        # blocked sticker set names
    "shield_badwords": [],     # custom blacklisted words (normalized)
    "shield_copyright": False, # piracy link/spam filter
    # word blacklist (Rose-style, with action mode)
    "blacklist": [],
    "bl_mode": "del",          # del | warn | kick | mute | ban
    # log channel for moderation events (0 = off)
    "log_chat": 0,
    # approval mode — new members stay muted until an admin approves
    "approval_on": False,
    # Join requests are separate from checks on members who already joined.
    "join_request_mode": "off",          # off | verify | instant
    "join_request_revision": 0,
    "join_request_pending": {},
    "join_request_admitted": {},
    "join_request_bulk_ops": {},
    "join_request_processed": {},
    # per-chat disabled commands (non-admins only)
    "disabled_cmds": [],
    "disabled_del": False,
    "disabled_admin": False,
    # night mode (auto close/open group)
    "night_on": False,
    "night_start": "23:00",
    "night_end": "06:00",
    "night_closed": False,
    # misc
    "rules": "",
    # anti-raid lockdown
    "raid_lock": False,
    # federation this group belongs to ("" = none; data lives at root "feds")
    "fed_id": "",
    # auto-post scheduler: [{"id", "text", "interval", "next"}]
    "autoposts": [],
    # anti-forward: delete channel forwards / post-as-channel messages
    "antiforward": False,
    # Rose parity batch 3
    "approved": [],            # per-group trusted automatic-filter exemptions
    # Missing approval_features keeps all legacy exemptions enabled; explicit
    # malformed values are interpreted fail-closed by approval_trust.
    "approval_features": [
        "stickers", "gifs", "media", "forwards", "other_locks",
        "blacklist_words", "anti_flood", "force_subscribe", "approval_mode",
    ],
    "lockwarns": True,         # locks: send "X is locked" notice after delete
    "allowlist": [],           # locks: domains exempt from the url lock
    "bl_reason": "",           # blacklist: custom reason shown on hits
    "bl_del": True,            # blacklist: delete trigger message (off = warn-only)
    "flood_window": 10,        # antiflood: rolling window, seconds
    "flood_action": "mute",    # antiflood: mute|kick|ban
    "flood_act_secs": 600,     # antiflood: mute duration
    "raid_duration": 21600,     # manual antiraid lockdown duration
    "raid_on": True,           # antiraid: auto detection (autoantiraid)
    "raid_threshold": 10,      # joins required for automatic antiraid
    "raid_window": 10,         # antiraid: join-count window, seconds
    "raid_act_secs": 600,      # antiraid: auto-mute duration
    "privaterules": False,     # rules via PM deep link instead of in-group
    "privatenotes": False,     # notes via PM deep link instead of in-group
    "rules_btn": "",           # custom label for the rules button
    "adminerror": True,        # reply "admins only" to non-admins
    "anonadmin": False,        # anonymous group admins may use admin commands
    "reports": True,           # members may page admins with /report/@admin
    "warn_time": 86400,        # mute duration when warn limit is hit
    # command/service cleanup policies (empty means disabled)
    "cleancommand_types": [],
    "cleanservice_types": [],
    "cleanservice_on": False,  # compatibility with older cleanservice deployments
}


class Database:
    def __init__(self):
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is None:
            if _PATH.exists():
                try:
                    self._data = json.loads(_PATH.read_text())
                except Exception:
                    self._data = {}
            else:
                self._data = {}
            self._data.setdefault("chats", {})
            self._data.setdefault("global", {})
            self._data.setdefault("feds", {})
        return self._data

    async def _save(self) -> None:
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=1))
        os.replace(tmp, _PATH)

    async def get_chat(self, chat_id: int) -> dict:
        """A fresh dict of the chat's settings merged over DEFAULTS."""
        async with _LOCK:
            data = self._load()
            stored = data["chats"].get(str(chat_id), {})
            merged = copy.deepcopy(DEFAULTS)
            merged.update(copy.deepcopy(stored))
            return merged

    async def update_chat(self, chat_id: int, **fields) -> dict:
        """Merge fields into the chat's settings and persist."""
        async with _LOCK:
            data = self._load()
            chat = data["chats"].setdefault(str(chat_id), {})
            chat.update(copy.deepcopy(fields))
            await self._save()
            merged = copy.deepcopy(DEFAULTS)
            merged.update(copy.deepcopy(chat))
            return merged

    async def mutate_chat(self, chat_id: int, fn) -> dict:
        """Read-modify-write a chat's settings atomically under one lock.

        fn(chat) receives a DEFAULTS-merged dict and mutates it in place;
        only the keys fn actually changed get persisted (so future DEFAULTS
        changes still reach old records). Returns the merged result. Use this
        instead of get_chat -> update_chat for list/dict edits, or concurrent
        mutations of the same chat silently overwrite each other.
        """
        async with _LOCK:
            data = self._load()
            chat = data["chats"].setdefault(str(chat_id), {})
            merged = copy.deepcopy(DEFAULTS)
            merged.update(copy.deepcopy(chat))
            before = copy.deepcopy(merged)
            fn(merged)
            for key, value in merged.items():
                if before.get(key, object()) != value:
                    chat[key] = copy.deepcopy(value)
            await self._save()
            return merged

    async def get_feds(self) -> dict:
        """A fresh dict of all federations (fed_id -> {name, owner, chats, banned})."""
        async with _LOCK:
            return copy.deepcopy(self._load()["feds"])

    async def mutate_fed(self, fed_id: str, fn) -> dict:
        """Atomic read-modify-write of one federation record."""
        async with _LOCK:
            data = self._load()
            fed = data["feds"].setdefault(
                fed_id, {"name": "", "owner": 0, "chats": [], "banned": []}
            )
            before = copy.deepcopy(fed)
            fn(fed)
            result = copy.deepcopy(fed)
            if before != result:
                await self._save()
            return result

    async def delete_fed(self, fed_id: str) -> None:
        """Remove a federation record entirely (/delfed)."""
        async with _LOCK:
            data = self._load()
            if data["feds"].pop(fed_id, None) is not None:
                await self._save()

    async def all_chats(self) -> dict:
        """A fresh dict of raw per-chat records (str(chat_id) -> stored fields).

        Used by the autopost scheduler; DEFAULTS are NOT merged here, so
        missing keys must be handled by the caller.
        """
        async with _LOCK:
            return copy.deepcopy(self._load()["chats"])

    async def reset_chat(self, chat_id: int) -> None:
        async with _LOCK:
            data = self._load()
            data["chats"].pop(str(chat_id), None)
            await self._save()

    async def get(self, key: str, default=None):
        async with _LOCK:
            return self._load()["global"].get(key, default)

    async def set(self, key: str, value) -> None:
        async with _LOCK:
            self._load()["global"][key] = value
            await self._save()

    async def mutate(self, key: str, fn, default):
        """Read-modify-write one global value under the lock (lists/dicts: use this instead of
        get -> set, or two overlapping commands overwrite each other). fn(value) mutates in
        place and returns True when something changed. Returns the value. Failure-atomic:
        fn runs on a copy and the store only keeps the change after a successful save —
        a failed save rolls memory back so it never diverges from disk."""
        async with _LOCK:
            data = self._load()
            current = data["global"].get(key)
            if current is None:
                current = copy.deepcopy(default)
            working = copy.deepcopy(current)
            if not fn(working):
                return current
            data["global"][key] = working
            try:
                await self._save()
            except Exception:
                data["global"][key] = current  # roll back the in-memory publish
                raise
            return working


db = Database()
