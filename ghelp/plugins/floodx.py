"""Antiflood & antiraid configuration (Rose parity): /flood /setfloodtimer
/setfloodmode /clearflood /antiraid /raidtime /raidactiontime /autoantiraid."""

import html

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.selfdel import autodel_reply
from ghelp.database import db
from ghelp.helpers import concise_bot_text, parse_duration, require_admin
from ghelp.plugins import antispam as _as

PRE = ["/", "?", "!", "."]


@autodel_reply
async def _reply(m: types.Message, text: str, **kwargs):
    return await m.reply_text(
        concise_bot_text(text), parse_mode=enums.ParseMode.HTML, **kwargs)


@app.on_message(filters.group & filters.command("flood", prefixes=PRE))
async def flood_status(_, m: types.Message):
    cfg = await db.get_chat(m.chat.id)
    await _reply(
        m,
         "<b>Antiflood</b>\n"
        f"• State: <b>{'ON' if cfg.get('flood_on') else 'OFF'}</b>\n"
        f"• Limit: <b>{cfg.get('flood_limit', 5)}</b> msgs / "
        f"<b>{cfg.get('flood_window', 10)}s</b>\n"
        f"• Action: <b>{cfg.get('flood_action', 'mute')}</b> "
        f"({int(cfg.get('flood_act_secs', 600)) // 60}m)\n"
         "<b>Antiraid</b>\n"
        f"• Auto-detect: <b>{'ON' if cfg.get('raid_on', True) else 'OFF'}</b> "
        f"(window <b>{cfg.get('raid_window', 10)}s</b>)\n"
        f"• Lockdown now: <b>{'YES' if cfg.get('raid_lock') else 'no'}</b>\n"
        f"• Joiner mute: <b>{int(cfg.get('raid_act_secs', 600)) // 60}m</b>",
    )


@app.on_message(filters.group & filters.command("setfloodtimer", prefixes=PRE))
async def setfloodtimer(_, m: types.Message):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    if len(m.command) == 2 and m.command[1].lower() in ("off", "no", "0"):
        await db.update_chat(m.chat.id, flood_timer_count=0)
        await _reply(m, "Timed antiflood disabled.")
        return
    try:
        count = int(m.command[1])
        secs = parse_duration(m.command[2])
        if not 2 <= count <= 100 or not secs or not 1 <= secs <= 86400:
            raise ValueError
    except (IndexError, TypeError, ValueError):
        await _reply(m, "Usage: <code>/setfloodtimer &lt;count&gt; &lt;duration&gt;</code> "
                        "(or <code>off</code>).")
        return
    await db.update_chat(m.chat.id, flood_on=True, flood_timer_count=count,
                         flood_timer_secs=secs)
    await _reply(m, f"Timed antiflood triggers after <b>{count}</b> messages "
                    f"in <b>{html.escape(m.command[2])}</b>.")


@app.on_message(filters.group & filters.command("setfloodwindow", prefixes=PRE))
async def setfloodwindow(_, m: types.Message):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    try:
        n = int(m.command[1])
        if not 2 <= n <= 60:
            raise ValueError
    except (IndexError, ValueError):
        await _reply(m, "Usage: <code>/setfloodwindow &lt;2-60 seconds&gt;</code> — "
                        "the detection window for counting floods.")
        return
    await db.update_chat(m.chat.id, flood_window=n)
    await _reply(m, f"Flood detection window set to <b>{n}s</b>.")


@app.on_message(filters.group & filters.command(["setfloodmode", "floodmode"], prefixes=PRE))
async def setfloodmode(_, m: types.Message):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg not in ("mute", "kick", "ban", "tban", "tmute"):
        await _reply(m, "Usage: <code>/floodmode ban|mute|kick|tban|tmute [duration]</code>")
        return
    duration = None
    if len(m.command) > 2:
        duration = parse_duration(m.command[2])
    if arg in ("tban", "tmute") and not duration:
        await _reply(m, "Timed actions require a duration, e.g. <code>/floodmode tban 3d</code>.")
        return
    if duration is not None and not 1 <= duration <= 2592000:
        await _reply(m, "Duration must be between 1 second and 30 days.")
        return
    fields = {"flood_action": arg}
    if duration:
        fields["flood_act_secs"] = duration
    await db.update_chat(m.chat.id, **fields)
    suffix = f" for {html.escape(m.command[2])}" if duration else ""
    await _reply(m, f"Flood action set to <b>{arg}</b>{suffix}.")


@app.on_message(filters.group & filters.command("clearflood", prefixes=PRE))
async def clearflood(_, m: types.Message):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg in ("yes", "on"):
        await db.update_chat(m.chat.id, flood_clear=True)
        await _reply(m, "Flood-triggering messages will be deleted.")
    elif arg in ("no", "off"):
        await db.update_chat(m.chat.id, flood_clear=False)
        await _reply(m, "Flood-triggering messages will be kept.")
    else:
        doomed = [key for key in _as._flood if key[0] == m.chat.id]
        for key in doomed:
            _as._flood.pop(key, None)
        await _reply(m, f"Flood counters cleared ({len(doomed)} users). "
                        "Use /clearflood on|off to configure deletion.")


@app.on_message(filters.group & filters.command("antiraid", prefixes=PRE))
async def antiraid(_, m: types.Message):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg in ("off", "no"):
        await db.update_chat(m.chat.id, raid_lock=False)
        await _reply(m, "Raid lockdown lifted — joins are open again.")
        return
    secs = parse_duration(arg) if arg else None
    if not secs or not 60 <= secs <= 86400:
        await _reply(m, "Usage: <code>/antiraid &lt;1m-1d&gt;</code> — manual "
                        "lockdown: every new member is auto-muted for that long.\n"
                        "<code>/antiraid off</code> lifts it early.")
        return
    await db.update_chat(m.chat.id, raid_lock=True, raid_duration=secs)
    _as._remember_task(_as._clear_raid_later(m.chat.id, secs))
    await _reply(m, f"<b>Manual lockdown for {html.escape(arg)}</b> — new "
                    "members will be auto-muted. /antiraid off to lift early.")


@app.on_message(filters.group & filters.command("raidtime", prefixes=PRE))
async def raidtime(_, m: types.Message):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    if len(m.command) == 1:
        cfg = await db.get_chat(m.chat.id)
        await _reply(m, f"Antiraid duration: <b>{int(cfg.get('raid_duration', 21600)) // 3600}h</b>.")
        return
    n = parse_duration(m.command[1])
    if not n or not 60 <= n <= 604800:
        await _reply(m, "Usage: <code>/raidtime &lt;duration&gt;</code> (1m–7d).")
        return
    await db.update_chat(m.chat.id, raid_duration=n)
    await _reply(m, f"Antiraid duration set to <b>{html.escape(m.command[1])}</b>.")


@app.on_message(filters.group & filters.command("raidactiontime", prefixes=PRE))
async def raidactiontime(_, m: types.Message):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    if len(m.command) == 1:
        cfg = await db.get_chat(m.chat.id)
        await _reply(m, f"Raid action duration: <b>{int(cfg.get('raid_act_secs', 3600)) // 3600}h</b>.")
        return
    secs = parse_duration(m.command[1])
    if not secs or not 60 <= secs <= 86400:
        await _reply(m, "Usage: <code>/raidactiontime &lt;duration&gt;</code> (1m–7d).")
        return
    await db.update_chat(m.chat.id, raid_act_secs=secs)
    await _reply(m, f"Raid joiner mute set to <b>{html.escape(m.command[1])}</b>.")


@app.on_message(filters.group & filters.command("autoantiraid", prefixes=PRE))
async def autoantiraid(_, m: types.Message):
    if not await require_admin(m, "can_restrict_members", "Ban users"):
        return
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg in ("off", "no", "0"):
        await db.update_chat(m.chat.id, raid_threshold=0, raid_on=False)
        await _reply(m, "Automatic antiraid disabled.")
        return
    try:
        threshold = int(arg)
        if threshold < 1:
            raise ValueError
    except ValueError:
        await _reply(m, "Usage: <code>/autoantiraid &lt;number&gt;</code> or off.")
        return
    await db.update_chat(m.chat.id, raid_threshold=threshold, raid_on=True)
    await _reply(m, f"Automatic antiraid enabled after <b>{threshold}</b> joins.")
