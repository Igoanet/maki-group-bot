"""Backup & restore group settings — /export and /import (reply to the file).

Only whitelisted, content-safe keys are exported/imported: notes,
blocklist, rules, welcome/goodbye, locks, flood/raid/warn tuning. Membership
state (approved lists, warns, connections, feds, stats) is never restored.
"""

import io
import json
import time

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.database import db
from ghelp.helpers import is_owner, require_admin

PRE = ["/", "?", "!", "."]
_BACKUP_KEYS = (
    "notes", "filters", "blacklist", "bl_mode", "bl_reason", "bl_del",
    "rules", "rules_btn", "welcome_on", "welcome_text", "goodbye_on",
    "goodbye_text", "clean_welcome", "locks", "lockwarns", "allowlist",
    "flood_window", "flood_action", "flood_act_secs", "raid_on",
    "raid_window", "raid_act_secs", "warn_limit", "warn_mode", "warn_time",
    "warn_action", "linkdel_on", "linkdel_admins",
    "linkdel_revision",
    "privaterules", "privatenotes", "adminerror", "anonadmin",
    "clean_service", "antichannelpin", "cleanlinked",
)
_PROTECTED_WARN_KEYS = frozenset({
    "warn_limit", "warn_mode", "warn_action", "warn_time",
    "linkdel_on", "linkdel_admins", "linkdel_revision",
})

_CATEGORY_KEYS = {
    "notes": {"notes", "rules", "rules_btn", "privaterules", "privatenotes"},
    "filters": {"filters"},
    "blocklists": {"blacklist", "bl_mode", "bl_reason", "bl_del"},
    "greetings": {"welcome_on", "welcome_text", "goodbye_on", "goodbye_text",
                  "clean_welcome"},
    "locks": {"locks", "lockwarns", "allowlist"},
    "antiflood": {"flood_window", "flood_action", "flood_act_secs"},
    "raids": {"raid_on", "raid_window", "raid_act_secs"},
    "warns": {"warn_limit", "warn_mode", "warn_action", "warn_time"},
    "clean_service": {"clean_service", "antichannelpin", "cleanlinked"},
    "pins": {"antichannelpin", "cleanlinked"},
    "admin": {"adminerror", "anonadmin"},
}


def _selected_keys(arguments: list[str]) -> set[str]:
    if not arguments:
        return set(_BACKUP_KEYS)
    selected: set[str] = set()
    for category in arguments:
        selected.update(_CATEGORY_KEYS.get(category.casefold(), set()))
    return selected


def _command_arguments(m: types.Message) -> list[str]:
    """Read command arguments from Pyrogram or lightweight message objects."""
    parsed = getattr(m, "command", None)
    if parsed:
        return [str(part) for part in parsed[1:]]
    source = str(getattr(m, "text", None) or getattr(m, "caption", None) or "")
    return source.split()[1:]


@app.on_message(filters.group & filters.command("export", prefixes=PRE))
async def export_data(_, m: types.Message) -> None:
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    chat = await db.get_chat(m.chat.id)
    selected = _selected_keys(_command_arguments(m))
    data = {
        k: chat.get(k) for k in _BACKUP_KEYS
        if k in selected and chat.get(k) not in (None, {}, [], "")
    }
    payload = {
        "app": "mikasa-group-bot",
        "version": 1,
        "chat": m.chat.title or "",
        "exported": int(time.time()),
        "data": data,
    }
    buf = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=1).encode())
    buf.name = f"backup_{abs(m.chat.id)}.json"
    await m.reply_document(
        buf,
        caption="<b>Group backup</b> — reply to this file with "
                "<code>/import</code> to restore these settings.",
        parse_mode=enums.ParseMode.HTML,
    )


@app.on_message(filters.group & filters.command("import", prefixes=PRE))
async def import_data(_, m: types.Message) -> None:
    if not await is_owner(m.chat.id, m.from_user.id if m.from_user else 0):
        return
    selected = _selected_keys(_command_arguments(m))
    doc = m.reply_to_message and getattr(m.reply_to_message, "document", None)
    if not doc:
        await m.reply_text(
            "Usage: reply to a backup <b>.json</b> file with <code>/import</code>.",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    if int(getattr(doc, "file_size", 0) or 0) > 512 * 1024:
        await m.reply_text("Error: file too large — backups stay under 512 KB.")
        return
    try:
        bio = await m.reply_to_message.download(in_memory=True)
        bio.seek(0)
        payload = json.loads(bio.read().decode("utf-8", "ignore"))
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ValueError
    except Exception:
        await m.reply_text("Error: that's not a valid backup file.")
        return
    owner = True
    clean = {
        k: v
        for k, v in data.items()
        if k in selected and isinstance(v, (dict, list, str, int, bool))
    }
    skipped_protected = bool(_PROTECTED_WARN_KEYS.intersection(clean) and not owner)
    if not owner:
        clean = {k: v for k, v in clean.items() if k not in _PROTECTED_WARN_KEYS}
    else:
        # The link policy is intentionally stronger than a backup file:
        # restoring one must not bypass its fixed 3/mute threshold.
        try:
            current = await db.get_chat(m.chat.id)
        except Exception:
            current = {}
        link_enabled = bool(clean.get("linkdel_on", current.get("linkdel_on")))
        if link_enabled:
            if "warn_limit" in clean and clean["warn_limit"] != 3:
                clean.pop("warn_limit")
                skipped_protected = True
            for key in ("warn_action", "warn_mode"):
                if key in clean and str(clean[key]).lower() != "mute":
                    clean.pop(key)
                    skipped_protected = True
    # Consent to demote named admins is never portable, including in a
    # backup made by the bot/group owner.  A link-policy import also bumps
    # the generation through the mutation below; a supplied generation is
    # metadata, not an authority.
    link_policy_import = bool({
        "linkdel_on", "linkdel_admins", "linkdel_revision"
    }.intersection(data))
    if link_policy_import and owner:
        if data.get("linkdel_admins") is True:
            skipped_protected = True
        clean["linkdel_admins"] = False
        clean.pop("linkdel_revision", None)
    elif link_policy_import:
        clean.pop("linkdel_revision", None)
    if not clean:
        if skipped_protected:
            await m.reply_text(
                "Error: warning/link settings can only be restored by the "
                "bot or group owner." if not owner else
                "Error: the backup contains incompatible link warning settings."
            )
        else:
            await m.reply_text("Error: nothing importable in that file.")
        return
    link_fields = {"linkdel_on", "linkdel_admins"}.intersection(clean)

    def restore(current):
        before = {
            key: current.get(key)
            for key in link_fields
        }
        current.update(clean)
        if link_fields and any(current.get(key) != before.get(key) for key in link_fields):
            try:
                revision = int(current.get("linkdel_revision", 0) or 0)
            except (TypeError, ValueError):
                revision = 0
            current["linkdel_revision"] = revision + 1

    await db.mutate_chat(m.chat.id, restore)
    top = ", ".join(sorted(clean)[:8])
    notice = (
        " Warning/link settings were skipped (owner only)."
        if skipped_protected else ""
    )
    await m.reply_text(
        f"Success: restored <b>{len(clean)}</b> settings ({top}"
        f"{'…' if len(clean) > 8 else ''}).{notice}",
        parse_mode=enums.ParseMode.HTML,
    )


@app.on_message(filters.group & filters.command("reset", prefixes=PRE))
async def reset_data(_, m: types.Message) -> None:
    """Reset portable configuration while retaining membership/runtime state."""
    if not await is_owner(m.chat.id, m.from_user.id if m.from_user else 0):
        return
    selected = _selected_keys(_command_arguments(m))
    empty = {
        "notes": {}, "rules": "", "rules_btn": "", "privaterules": False,
        "privatenotes": False, "filters": {}, "blacklist": [],
        "bl_mode": "del", "bl_reason": "", "bl_del": True,
        "welcome_on": False, "welcome_text": "", "goodbye_on": False,
        "goodbye_text": "", "clean_welcome": False, "locks": [],
        "lockwarns": True, "allowlist": [], "flood_window": 10,
        "flood_action": "mute", "flood_act_secs": 60, "raid_on": False,
        "raid_window": 10, "raid_act_secs": 3600, "warn_limit": 3,
        "warn_mode": "mute", "warn_action": "mute", "warn_time": 86400,
        "clean_service": False, "adminerror": True, "anonadmin": False,
    }
    clean = {key: value for key, value in empty.items() if key in selected}
    if not clean:
        await m.reply_text("No recognized backup categories were supplied.")
        return
    await db.update_chat(m.chat.id, **clean)
    await m.reply_text(
        f"Reset <b>{len(clean)}</b> settings.",
        parse_mode=enums.ParseMode.HTML,
    )
