"""Command centre in the bot's DM — every command usable without a group.

/commands opens a categorised browser.  Each category lists its commands with
usage; group-scoped commands show the group they will act on, chosen from a
picker, so an owner or admin can drive the bot entirely from DM.
"""

import html

from pyrogram import enums, filters, types

from ghelp import app, logger
from ghelp.config import config
from ghelp.database import db
from ghelp.helpers import ikb, ikm

_PRE = ["/", "!", "?", "."]

# category key -> (label, [(command, usage, description), ...])
CATS: dict[str, tuple] = {
    "play": ("🎵 Playback", [
        ("play", "/play <song|link>", "Play a song in the group's voice chat"),
        ("vplay", "/vplay <song|link>", "Play a video"),
        ("song", "/song <name>", "Download a song as audio"),
        ("queue", "/queue", "Show the current queue"),
        ("skip", "/skip", "Skip to the next track"),
        ("pause", "/pause", "Pause playback"),
        ("resume", "/resume", "Resume playback"),
        ("stop", "/stop", "Stop and clear the queue"),
        ("seek", "/seek <seconds>", "Seek forward"),
        ("loop", "/loop <n|all|off>", "Loop the track or the queue"),
        ("shuffle", "/shuffle", "Shuffle the queue"),
        ("speed", "/speed <x>", "Change playback speed"),
        ("autoplay", "/autoplay on|off|everyone", "Keep playing similar songs"),
        ("playmode", "/playmode admins|everyone", "Who may start playback"),
        ("active", "/active", "List active streams"),
        ("export", "/export", "Export the queue as a file"),
        ("import", "/import (reply)", "Import an exported queue"),
    ]),
    "mod": ("🛡 Moderation", [
        ("ban", "/ban (reply|id)", "Ban a user"),
        ("sban", "/sban (reply|id)", "Silent ban"),
        ("tban", "/tban (reply|id) <mins>", "Temporary ban"),
        ("dban", "/dban (reply)", "Delete the message and ban"),
        ("unban", "/unban (reply|id)", "Unban a user"),
        ("kick", "/kick (reply|id)", "Kick a user"),
        ("mute", "/mute (reply|id)", "Mute a user"),
        ("tmute", "/tmute (reply|id) <mins>", "Temporary mute"),
        ("unmute", "/unmute (reply|id)", "Unmute"),
        ("warn", "/warn (reply) <reason>", "Warn a user"),
        ("unwarn", "/unwarn (reply)", "Remove a warning"),
        ("warns", "/warns (reply)", "Show a user's warnings"),
        ("resetwarn", "/resetwarn (reply)", "Clear warnings"),
        ("setwarnlimit", "/setwarnlimit <n>", "Warnings before action"),
        ("setwarnmode", "/setwarnmode ban|kick|mute", "Action at the limit"),
    ]),
    "admin": ("👑 Admin tools", [
        ("pin", "/pin (reply)", "Pin a message silently"),
        ("unpin", "/unpin", "Unpin"),
        ("pinned", "/pinned", "Link of the last pinned message"),
        ("promote", "/promote (reply)", "Promote a user"),
        ("demote", "/demote (reply)", "Demote a user"),
        ("lowpromote", "/lowpromote (reply)", "Promote with fewer rights"),
        ("fullpromote", "/fullpromote (reply)", "Promote with full rights"),
        ("title", "/title <text>", "Set an admin title"),
        ("adminlist", "/adminlist", "List admins"),
        ("userlist", "/userlist", "List members"),
        ("del", "/del (reply)", "Delete the replied message"),
        ("purge", "/purge (reply)", "Delete up to the replied message"),
        ("spurge", "/spurge <n>", "Delete n messages after the reply"),
        ("invitelink", "/invitelink", "Get an invite link"),
        ("setgtitle", "/setgtitle <text>", "Set the group title"),
        ("setgpic", "/setgpic (reply to photo)", "Set the group photo"),
        ("rmgpic", "/rmgpic", "Remove the group photo"),
        ("setdesc", "/setdesc <text>", "Set the description"),
    ]),
    "locks": ("🎤 Locks & filters", [
        ("lock", "/lock <types>", "Lock content types"),
        ("unlock", "/unlock <types>", "Unlock content types"),
        ("locks", "/locks", "Show active locks"),
        ("locktypes", "/locktypes", "List lockable types"),
        ("antiflood", "/antiflood on|off", "Flood protection"),
        ("setflood", "/setflood <n>", "Flood limit"),
        ("nightmode", "/nightmode on|off", "Night restrictions"),
        ("cleaner", "/cleaner on|off", "Delete service messages"),
        ("antichannel", "/antichannel on|off", "Delete channel posts"),
        ("antibanall", "/antibanall on|off", "Alert on mass-ban"),
        ("reports", "/reports on|off", "Member reports"),
        ("blacklist", "/blacklist (reply|word)", "Block a word or user"),
        ("unblacklist", "/unblacklist", "Unblock"),
        ("filter", "/filter <key> <reply>", "Add a keyword filter"),
        ("stopfilter", "/stopfilter <key>", "Remove a filter"),
        ("filters", "/filters", "List filters"),
    ]),
    "greet": ("🎉 Greetings", [
        ("welcome", "/welcome on|off", "Toggle welcome messages"),
        ("setwelcome", "/setwelcome <text|reply>", "Set the welcome"),
        ("resetwelcome", "/resetwelcome", "Reset the welcome"),
        ("goodbye", "/goodbye on|off", "Toggle goodbye messages"),
        ("setgoodbye", "/setgoodbye <text|reply>", "Set the goodbye"),
        ("resetgoodbye", "/resetgoodbye", "Reset the goodbye"),
        ("cleangoodbye", "/cleangoodbye on|off", "Remove the old goodbye"),
        ("rules", "/rules", "Show the rules"),
        ("setrules", "/setrules <text>", "Set the rules"),
        ("clearrules", "/clearrules", "Clear the rules"),
    ]),
    "util": ("🧰 Utilities", [
        ("id", "/id", "Your chat and user id"),
        ("info", "/info (reply)", "User information"),
        ("ping", "/ping", "Bot status"),
        ("stats", "/stats", "Bot statistics"),
        ("notes", "/notes", "Saved notes"),
        ("save", "/save <name> (reply)", "Save a note"),
        ("get", "/get <name>", "Get a note"),
        ("remind", "/remind <time> <text>", "Set a reminder"),
        ("afk", "/afk [reason]", "Mark yourself away"),
        ("tagall", "/tagall <text>", "Mention everyone (emoji)"),
        ("report", "/report [reason]", "Report a message to admins"),
        ("q", "/q (reply)", "Make a quote"),
        ("shayri", "/shayri", "Random shayari"),
        ("aqquote", "/aqquote", "Random anime quote"),
        ("write", "/write <text>", "Write text on paper"),
        ("tts", "/tts <text>", "Text to speech"),
    ]),
    "owner": ("🔑 Owner", [
        ("owner", "/owner", "Owner panel"),
        ("manage", "/manage <chat_id>", "Group console"),
        ("massban", "/massban <chat_id>", "Silent mass-ban"),
        ("chatlink", "/chatlink <chat_id>", "Invite link for a group"),
        ("bans", "/bans <chat_id>", "Banned users"),
        ("unban", "/unban <chat_id> <id>", "Unban one user"),
        ("broadcast", "/broadcast (reply)", "Message every group"),
        ("addsudo", "/addsudo (reply|id)", "Add a sudo user"),
        ("delsudo", "/delsudo (reply|id)", "Remove a sudo user"),
        ("listsudo", "/listsudo", "List sudo users"),
        ("setlog", "/setlog (forward from channel)", "Bind a log channel"),
        ("unsetlog", "/unsetlog", "Unbind the log channel"),
        ("refreshassistant", "/refreshassistant", "Re-read assistant names"),
        ("groups", "/groups", "Groups the bot is in"),
        ("clean", "/clean", "Delete leftover files"),
        ("restart", "/restart", "Restart the bot"),
        ("leavechat", "/leavechat <chat_id>", "Leave a chat"),
        ("unbanall", "/unbanall", "Unban everyone"),
        ("unmuteall", "/unmuteall", "Unmute everyone"),
        ("clearzombies", "/clearzombies", "Ban deleted accounts"),
        ("kickdumbs", "/kickdumbs", "Kick members with no username"),
    ]),
}

_CAT_ROWS = [["play", "mod"], ["admin", "locks"], ["greet", "util"], ["owner"]]


def _is_owner(uid: int) -> bool:
    return bool(uid and uid == config.OWNER_ID)


def _menu_kb(uid: int):
    rows = []
    for pair in _CAT_ROWS:
        row = []
        for key in pair:
            label = CATS[key][0]
            row.append(ikb(f"{label} ({len(CATS[key][1])})", f"dmc:cat:{key}"))
        rows.append(row)
    rows.append([ikb("✖️ Close", "dmc:close", enums.ButtonStyle.DANGER)])
    return ikm(rows)


def _cat_text(key: str, uid: int) -> str:
    label, items = CATS[key]
    if key == "owner" and not _is_owner(uid):
        return ("<b>🔑 Owner</b>\n\nThese commands are owner-only.")
    lines = [f"<b>{label}</b>", ""]
    for cmd, usage, desc in items:
        lines.append(f"• <code>{html.escape(usage)}</code>\n   <i>{html.escape(desc)}</i>")
    lines.append("\n<i>Group commands act in the group you pick below.</i>")
    return "\n".join(lines)


def _cat_kb(key: str):
    return ikm([[
        ikb("« Back", "dmc:menu", enums.ButtonStyle.PRIMARY),
        ikb("✖️ Close", "dmc:close", enums.ButtonStyle.DANGER),
    ]])


@app.on_message(filters.command(["commands", "cmds", "menu"], prefixes=_PRE)
                & filters.private)
async def commands_cmd(_, m: types.Message):
    if not m.from_user:
        return
    await m.reply_text(
        "<b>🤖 Command centre</b>\n\n"
        "Every command, grouped by purpose.  Group commands run in whichever "
        "group the bot is serving — no need to type them there.",
        reply_markup=_menu_kb(m.from_user.id))


@app.on_callback_query(filters.regex(r"^dmc:menu$"))
async def dmc_menu(_, q: types.CallbackQuery):
    await q.answer()
    if not q.from_user:
        return
    try:
        await q.message.edit_text(
            "<b>🤖 Command centre</b>\n\nPick a category.",
            reply_markup=_menu_kb(q.from_user.id))
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^dmc:cat:([a-z]+)$"))
async def dmc_cat(_, q: types.CallbackQuery):
    await q.answer()
    if not q.from_user:
        return
    key = q.data.split(":")[-1]
    if key not in CATS:
        return
    if key == "owner" and not _is_owner(q.from_user.id):
        return await q.answer("Owner only.", show_alert=True)
    try:
        await q.message.edit_text(_cat_text(key, q.from_user.id),
                                  reply_markup=_cat_kb(key))
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^dmc:close$"))
async def dmc_close(_, q: types.CallbackQuery):
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        pass
