"""Core commands and help UI."""

import html
import logging
import re
import time

from pyrogram import enums, filters, types

from ghelp import app
from ghelp.config import config
from ghelp.helpers import ikb, ikm


_LOG = logging.getLogger(__name__)


def _log_help_failure(operation: str, exc: Exception) -> None:
    """Log a safe failure summary without chat, user, or message content."""
    _LOG.warning("help %s failed (%s)", operation, type(exc).__name__)


# Help is data-driven: one entry per section, rendered by _section_text().
# Each command is (usage, explanation). Every command handler in the plugins
# must appear here exactly once (research/tools/ghelp_command_harness.py
# cross-checks this list against the registered handlers).
HELP_SECTIONS: dict[str, dict] = {
    "aliases": {
        "button": "Aliases & Shortcuts",
        "title": "Aliases \u2014 short names and typos that work",
        "intro": "Common short forms and mis-typings are mapped onto the real "
                 "command, so /muter runs /mute and so on.",
        "cmds": [
            ("/mod", "jump straight to the Moderation commands."),
            ("/muter", "alias for /mute. /mut also works."),
            ("/edit, /edior", "alias for /editor."),
            ("/bann, /kik, /unbann", "aliases for /ban, /kick, /unban."),
            ("/promote /fullpromote", "typos like /fullpromte also work."),
        ],
        "note": "Aliases never replace a real command; they dispatch through it.",
    },
    "modernfeatures": {
        "button": "Member Tags",
        "title": "Member Tags \u2014 modern Telegram features",
        "intro": "Label a member with a short tag next to their name (Telegram "
                 "Bot API 9.5), and see which newer Telegram features work here.",
        "cmds": [
            ("/tag label", "reply to a member to give them that tag."),
            ("/tag clear", "remove their tag."),
            ("/tag list", "every member who has a tag."),
            ("/modern", "which modern Telegram features are active here."),
        ],
        "note": "Tags need the manage-tags admin right for me.",
    },
    "egexempt": {
        "button": "Edit Guardian Trust",
        "title": "Edit Guardian \u2014 edit-free members and editors",
        "intro": "An edit-free member's edits are never deleted. The owner can "
                 "make someone an <b>editor</b>, who can then make other "
                 "members edit-free too.",
        "cmds": [
            ("/editfree", "reply to a member to make them edit-free."),
            ("/editfree off", "remove it."),
            ("/editfree list", "everyone edit-free, and who the editors are."),
            ("/editor", "reply to a member to make them an editor."),
            ("/editor off", "take the editor role back."),
        ],
        "note": "/trust and /exempt are older names for /editfree; /promote editor also works.",
    },
    "more": {
        "button": "\u2795 More commands",
        "title": "More commands \u2014 owner, sudo and utility tools",
        "intro": "The rest of the commands: your own messages, federation bans, "
                 "sudo management, broadcast and other utilities.",
        "cmds": [
            ("/addsudo", "add a user to the sudo list (owner)."),
            ("/broadcast", "send a message to every group (owner)."),
            ("/clearme", "delete your own recent messages here."),
            ("/clearme_session", "delete your messages from this session."),
            ("/clearme_stop", "stop a running clean-me."),
            ("/delsudo", "remove a user from the sudo list (owner)."),
            ("/fqallow", "allow a user to use fake quotes (owner)."),
            ("/fqallowed", "show who may use fake quotes."),
            ("/fqdeny", "block a user from fake quotes (owner)."),
            ("/gban", "globally ban a user across the federation."),
            ("/ginfo", "details about this group."),
            ("/groups", "list every group the bot serves (owner)."),
            ("/gsay", "send a message as the group."),
            ("/gunban", "lift a global ban."),
            ("/help", "this help menu. /help ban jumps straight to a command."),
            ("/listsudo", "show the sudo list (owner)."),
            ("/massban", "ban many users at once (owner)."),
            ("/restrict", "restrict a member - reply to their message."),
            ("/restricted", "list the restricted members."),
            ("/rmsudo", "clear the sudo list (owner)."),
            ("/start", "utility command."),
            ("/sudolist", "utility command."),
            ("/unrestrict", "utility command."),
        ],
        "note": "Owner and sudo commands only work for listed accounts.",
    },
    "commands": {
        "button": "\U0001f916 Command centre",
        "title": "\U0001f916 Command centre — every command in DM",
        "intro": "Send <code>/commands</code> in my private chat for a "
                 "categorised browser of every command, with usage and a "
                 "description. <code>/cmds</code> and <code>/menu</code> do "
                 "the same.",
        "cmds": [
            ("/commands", "open the categorised command browser in DM."),
            ("/cmds", "alias for /commands."),
            ("/menu", "alias for /commands."),
        ],
    },
    "extras": {
        "button": "\U0001f9f0 Extras",
        "title": "\U0001f9f0 Extras — new group tools",
        "intro": "Group tools added recently. Toggles take <code>on</code> or "
                 "<code>off</code>.",
        "cmds": [
            ("/announcement on|off", "enable announcements in the chat."),
            ("/antichannel on|off", "delete messages posted as a channel."),
            ("/cleaner on|off", "delete service messages automatically."),
            ("/cleangoodbye on|off", "remove the previous goodbye before posting a new one."),
            ("/rmgpic", "remove the group photo."),
            ("/userlist", "list members with counts of admins and bots."),
            ("/antibanall on|off", "alert admins if someone mass-bans."),
            ("/write <text>", "write your text on a paper image."),
            ("/shayri", "random Hindi shayri. Also /shayari."),
            ("/aqquote", "random anime quote. Also /animequote, /iaqquotes."),
            ("/banme", "ban yourself (members only)."),
        ],
    },
    "massowner": {
        "button": "\U0001f513 Mass actions",
        "title": "\U0001f513 Mass actions — owner only",
        "intro": "Owner-only bulk operations. They touch every member, so they "
                 "run from DM and report progress there.",
        "cmds": [
            ("/unbanall", "unban every banned member."),
            ("/unmuteall", "lift restriction on every muted member."),
            ("/clearzombies", "ban deleted accounts."),
            ("/kickdumbs", "kick members with no username."),
            ("/leavechat <chat_id>", "make the bot leave a chat."),
        ],
    },
    "chatfight": {
        "button": "Chat Fight",
        "title": "Chat Fight — group activity",
         "intro": "The image card shows the top ten for <b>Today</b>, <b>This week</b> "
                  "or <b>All time</b>. <code>/chatfight</code> opens Today; "
                  "<code>/weektop</code> opens this week; <code>/top</code> opens "
                  "all time. Counts start from newly observed messages, not old "
                  "history; unknown old types stay <b>Unclassified</b>. Bots, "
                  "commands and edits are excluded; days use IST and weeks start "
                  "Monday. Admins pause/resume with the explicit command without "
                  "losing scores.",
        "cmds": [
             ("/chatfight [day|week|all]", "open Today by default; an explicit period overrides the default."),
             ("/chatfight media [day|week|all]", "open typed-media analytics; older unclassified activity remains honest."),
             ("/chatfight on|off", "admins only: pause or resume counting without resetting scores."),
             ("/chatfight title [20-60|reset]", "admins only: view or save the title width for this group; default 35%."),
             ("/weektop", "open this week's leaderboard (add day or all to override)."),
             ("/top [day|week|all]", "open the all-time leaderboard by default."),
             ("/alltop", "alias for /top with the all-time default."),
        ],
    },
    "admin": {
        "button": "Bans & Mutes",
        "title": "Bans & Mutes — kick, ban and mute",
        "intro": (
            "Admins only. Pick the user by <b>replying</b> to their message, "
            "or write <code>@username</code> / user id after the command. "
            "Times are written like <code>10m</code>, <code>2h</code>, <code>1d</code>."
        ),
        "cmds": [
            ("/ban", "ban the user for good."),
            ("/tban 2h", "ban for a limited time."),
            ("/timeban 2h", "same as /tban; reply or provide @user/ID and a duration."),
            ("/dban", "reply: delete that message and ban its sender."),
            ("/deepban", "same as /dban: ban and delete the replied message, not the user's whole history."),
            ("/dtban 2h", "reply: temporarily ban the sender and delete that message."),
            ("/stban 2h", "temporary silent ban; failures still receive an explanation."),
            ("/sban", "silent ban; delete your command and post nothing."),
            ("/unban", "remove a ban."),
            ("/kick", "remove the user; they can join again."),
            ("/dmute [2h]", "reply: delete that message and mute its sender."),
            ("/dkick", "reply: delete that message and kick its sender."),
            ("/skick", "silent kick; the user can join again."),
            ("/mute [2h] [reason]", "mute forever, or for a time if you give one."),
            ("/tmute 2h [reason]", "mute for a limited time."),
            ("/smute [2h]", "silent mute; delete your command."),
            ("/unmute", "let the user talk again."),
            ("/del", "reply: delete that message."),
            ("/delete", "reply: delete one message."),
            ("/purge", "reply to the first message: delete down to your command (max 500)."),
            ("/deleteuser", "reply/@user/id: choose a type, then confirm; up to 5,000 recent IDs."),
            ("/purgeuser", "same fixed-range deletion; includes Cancel and Stop."),
            ("/purgeall", "current group creator only: confirm a bot-only scan/deletion up to this command. Bot needs Admin and Delete messages rights; protected service/history messages may remain. Checked IDs do not prove history is empty; later messages are excluded."),
            ("/deleteall", "owner-confirmed cleanup in batches. Progress separates checked IDs from confirmed deletions; Telegram may still protect some messages."),
            ("/delall", "short alias for /deleteall; requires the same group-owner confirmation."),
            ("/deletall", "also accepts this spelling of /deleteall."),
            ("/deleteall resume", "continue an interrupted scan after fresh confirmation instead of checking the same range again."),
            ("/deleteall stop", "live group owner: stop cleanup even if its progress card is gone."),
            ("/delete all", "alias for /purgeall; add <code>resume</code> for a saved boundary with fresh consent."),
            ("/purgeall resume", "resume the saved bot boundary after fresh live creator confirmation; never uses a connected user account."),
            ("/delete all resume", "resume the saved bot boundary with fresh live creator consent; Telegram may protect service/history messages."),
            ("/kickme", "anyone: kick yourself out of the group."),
        ],
        "note": "You need the matching admin right yourself (ban for /ban, "
        "delete for /del or /delete, pin for /pin) — and the bot needs it too.",
    },
    "promote": {
        "button": "Promote",
        "title": "Promote & admin list",
        "intro": (
            "Only the group owner or an admin with the <b>Add admins</b> right can "
            "promote, and the bot itself must have <b>Add admins</b> too."
        ),
        "cmds": [
            ("/promote", "reply/@user: make admin with rights from /promotepowers (default: add members, pin, manage voice chat)."),
            ("/fullpromote", "same, plus Add admins."),
            ("/promotepowers", "buttons to choose which rights /promote gives."),
            ("/demote", "reply/@user: remove rights from admins Maki promoted."),
            ("/adminlist", "anyone: list the admins of this group."),
            ("/admincache", "refresh the admin list."),
        ],
    },
    "warns": {
        "button": "Warns",
        "title": "Warnings",
        "intro": (
            "Admins only. Default: <b>3</b> warns → <b>mute for 24h</b>; "
            "time means mute duration, not warning expiry. At the limit, the action "
            "runs and resets the count. Settings are owner-only (group creator or "
            "configured bot owner); regular admins cannot change them. Link Deleter "
            "keeps <b>3</b>/mute."
        ),
        "cmds": [
            ("/warn [reason]", "reply or @user: give one warning."),
            ("/dwarn [reason]", "reply: delete that message and warn its sender."),
            ("/swarn [reason]", "silent warn — your command is deleted."),
            ("/warns", "reply or @user: show their warn count."),
            ("/rmwarn", "reply or @user: remove one warning."),
            ("/unwarn", "reply or @user: clear all their warnings."),
            ("/resetwarn", "Rose-compatible alias: reset all warnings for one user."),
            ("/resetallwarns", "clear every warning in this group."),
            ("/warnlimit 3", "owner-only: warns before action (0–20; 0 = never). Alias: /setwarnlimit."),
            ("/warnmode mute|kick|ban", "owner-only: action at the limit. Alias: /warnaction and /setwarnmode."),
            ("/strongwarn on|off", "owner-only: on = ban at the limit, off = kick."),
            ("/warntime [duration]", "owner-only: show or set warn-mute length."),
            ("/warnings", "admin: show warning settings."),
        ],
    },
    "linkdel": {
        "button": "Link Deleter",
        "title": "Link Deleter",
        "intro": (
            "Owner-only. Enabled member links are deleted and count as warns; at <b>3</b>, "
            "the member is muted (default <b>24h</b>). Admin mode deletes admin links and "
            "requires owner confirmation. Demotion/mute needs an editable admin and "
            "matching rights; rights are not restored. Creator, configured bot owner and "
            "anonymous admin links are protected from personal attribution."
        ),
        "cmds": [
            ("/linkdel [on|off]", "switch Link Deleter on or off."),
            ("/linkdeladmins [on|off]", "also delete admin links; enabling needs owner confirmation."),
            ("/setwarntime <duration>", "set the mute duration after 3 link warns (default 24h; 1m–30d)."),
        ],
    },
    "locks": {
        "button": "Locks",
        "title": "Locks — block message types",
        "intro": (
            "Admins only; locked content is auto-deleted (admins exempt). Types: "
            "<code>url sticker forward photo video gif document audio voice bot_inline "
            "phone email</code>."
        ),
        "cmds": [
            ("/lock url sticker", "lock one or more types."),
            ("/unlock url", "unlock one or more types."),
            ("/mediadelete on|off", "delete every media message except from admins and approved members. Also /deletemedia."),
            ("/locks", "buttons to switch each lock on/off."),
            ("/locktypes", "anyone: list all lock types."),
            ("/lockwarns on|off", "post a short notice when something is deleted (default on)."),
            ("/lock url ### reason {ban}", "set a custom lock reason/action; use <code>/lock url ###</code> to reset it."),
            ("/allowlist example.com", "allow this website even when url is locked (no domain = show list, max 50)."),
            ("/rmallowlist example.com", "remove a website from the allow list."),
            ("/rmallowlistall", "clear the allow list."),
        ],
    },
    "blocklist": {
        "button": "Blocklist",
        "title": "Blocklists — banned words and phrases",
        "intro": (
            "Admins only. Remove messages containing listed words or phrases. "
            "/blacklist… and /blocklist… names use the same list."
        ),
        "cmds": [
            ("/addblocklist word", "add a word or phrase (also: /addblacklist)."),
            ("/rmblocklist word", "remove it (also: /unblacklist)."),
            ("/blocklist", "show the list (also: /blacklists)."),
            ("/rmblocklistall", "clear the whole list. Also /unblocklistall."),
            ("/blacklistmode del|warn|kick|mute|ban", "what to do when a word is used (default: delete). Also /blocklistmode."),
            ("/blocklistdelete on|off", "off = keep the message and warn the user instead."),
            ("/setblocklistreason text", "reason shown when a word is caught."),
            ("/resetblocklistreason", "remove that reason."),
        ],
    },
    "flood": {
        "button": "Antiflood",
        "title": "Antiflood & AntiRaid",
        "intro": "Admins only. Stops members who send too many messages, and mass-join raids.",
        "cmds": [
            ("/antiflood on|off", "switch flood protection on/off."),
            ("/setflood 5", "how many messages in the window count as flood (2–50)."),
            ("/setfloodwindow 10", "the window in seconds (2–60)."),
            ("/setfloodmode mute|kick|ban", "flood punishment (default mute). Also /floodmode."),
            ("/setfloodtimer 10m", "how long a flood mute lasts (1m–1d)."),
            ("/flood", "anyone: show the current flood & raid settings."),
            ("/clearflood", "reset the flood counters."),
            ("/autoantiraid on|off", "detect join bursts and mute new joiners."),
            ("/antiraid 1h", "lock down now (1m–1d); new members are muted. <code>/antiraid off</code> ends it."),
            ("/raidtime 30", "join-burst window in seconds (2–120)."),
            ("/raidactiontime 1h", "how long joiners stay muted during a raid (1m–1d)."),
        ],
    },
    "shield": {
        "button": "Shield",
        "title": "Shield — anti ban-attack",
        "intro": (
            "Admins only. Blocks illegal/adult media, bad sticker packs and piracy spam "
            "that can get a group banned. These controls work only while Shield is <b>on</b>."
        ),
        "cmds": [
            ("/shield", "show the current Shield settings."),
            ("/shield on|off", "master switch (default off)."),
            ("/shield scan all|new|off", "AI-check photos/stickers: everyone, new members (default), or never."),
            ("/shield newhours 24", "hours a joiner stays “new” (1–168)."),
            ("/shield medialock on|off", "new members cannot send any media."),
            ("/shield banpack", "reply to a sticker: block its whole pack."),
            ("/shield allowpack", "reply to a sticker: unblock its pack."),
            ("/shield blockword text", "delete messages containing this word/phrase."),
            ("/shield unblockword text", "remove that word."),
            ("/shield copyright on|off", "delete piracy-site links and pirate-movie spam."),
        ],
    },
    "editguard": {
        "button": "Edit Guardian",
        "title": "Edit Guardian — delete edited messages",
        "intro": (
            "Only the group owner can change this. Edited messages are deleted "
            "after the chosen delay; editing again restarts it. The user gets "
            "a notice that disappears in <b>5 seconds</b>. Turning it off or "
            "changing settings cancels pending deletions; restarting does not resume them."
        ),
        "cmds": [
            ("/editguard", "view status and deletion delay. Only the live group owner can change it; anonymous mode must be off."),
            ("/editguard on", "choose whether admins are included, then enable."),
            ("/editguard off", "disable and cancel pending deletions."),
            ("/editguard admin on|off", "include admins or spare them; owner only."),
            ("/editguard time 60", "delete 60 seconds after the latest edit; accepts 5–86400."),
        ],
    },
    "autodel": {
        "button": "Auto-delete",
        "title": "Auto-delete, anti-forward & slow mode",
        "intro": "Two independent modes: timed group cleanup and instant cleanup "
                 "for selected users. Requires Delete messages permission. "
                 "The group timer covers messages Maki receives and Maki's own "
                 "replies; Telegram can hide other bots' posts. For all-bot "
                 "coverage, the manual group owner must enable Telegram's "
                 "built-in Group Auto-Delete setting; no user account "
                 "connection is used.",
        "cmds": [
            ("/autodel", "open separate group-timer, instant-user and legacy delayed controls."),
            ("/autodel 30s", "delete newly observed group messages after 30 seconds, including admins."),
            ("/autodel group 5m", "set the group timer to five minutes."),
            ("/autodel off", "turn off the group timer; instant-user rules remain active."),
            ("/autodeluser", "reply or provide @user/ID: instantly delete this user's new messages, without banning."),
            ("/unautodeluser", "reply or provide @user/ID: remove their instant-deletion rule."),
            ("/autodeluser list", "show instant-deletion targets; use status or clear to inspect/reset."),
            ("/autodel bots on|off", "legacy delayed mode: watch bot messages that Maki can actually receive."),
            ("/autodel add", "legacy delayed mode: watch the replied/@user; this is not instant deletion."),
            ("/autodel del", "remove a legacy delayed target; clear removes legacy targets."),
            ("/autodel time 10", "set the legacy selected-user delay (5–3600 seconds), not the group timer."),
            ("/cleanservice [on|off]", "clean join/leave notices (default off); needs Delete messages. No history scan; Telegram may protect notices."),
            ("/antiforward on|off", "delete channel forwards and channel-posted messages."),
            ("/slowmode 30s|5m|1h|off", "set Telegram slow mode for members."),
        ],
    },
    "nightmode": {
        "button": "Night Mode",
        "title": "Night mode — close the group at night",
        "intro": (
            "Admins only. While night mode is active members cannot write; admins "
            "still can. The group opens again by itself in the morning."
        ),
        "cmds": [
            ("/nightmode on|off", "switch night mode (default 23:00–06:00 IST)."),
            ("/setnighttime 23:00 06:00", "change the closing hours (24-hour clock)."),
        ],
    },
    "welcome": {
        "button": "Greetings",
        "title": "Greetings, CAPTCHA, approval & force-subscribe",
        "intro": (
            "Admins only. Welcome/goodbye text supports "
            "<code>{first} {last} {fullname} {username} {mention} {id} {chat} {count}</code> "
            "and buttons such as <code>[Rules](buttonurl:https://t.me/…)</code>."
        ),
        "cmds": [
            ("/welcome on|off|preview", "switch welcomes or preview the saved message."),
            ("/setwelcome [text]", "set text, or reply to media; optional text is its caption."),
            ("/resetwelcome", "back to the default welcome."),
            ("/goodbye on|off|preview", "switch goodbyes or preview the saved message."),
            ("/setgoodbye [text]", "set text, or reply to media; optional text is its caption."),
            ("/cleanwelcome on|off", "delete the previous welcome when replacing it."),
            ("/forcesubscribe @channel", "require channel membership before chatting; make Maki admin there first. <code>/forcesubscribe off</code> stops it. Alias: /fsub."),
        ],
    },
    "vc_monitor": {
        "button": "VC joins",
        "title": "VC join notices",
        "intro": (
            "Maki posts a joining participant's Telegram-visible name, username and ID, "
            "then deletes the notice after <b>5 seconds</b>. Existing participants and "
            "mute/unmute changes are not joins; no audio is recorded."
        ),
        "cmds": [
            ("/vcmonitor [on|off|status]", "admins: enable, disable or check. Known Maki groups default on; an existing music assistant with group access is required. Status explains when unavailable."),
        ],
        "note": "Maki sends the notices. The assistant does not automatically join groups or voice chats.",
    },
    "join_requests": {
        "button": "Join Requests",
        "title": "Join Requests",
        "intro": (
            "Admin-only. Default: <b>off</b>. Mode changes affect future requests only; "
            "<code>/requestall</code> handles the current backlog. Separate from "
            "post-join captcha/manual approval. You and the bot need <b>Invite Users</b>."
        ),
        "cmds": [
            ("/autorequest [on|verify|off]", "future requests: <code>on</code> approves instantly; <code>verify</code> sends an arithmetic DM and needs the correct answer; <code>off</code> leaves them manual (default)."),
            ("/requestall", "approve ALL current pending requests, including older requests."),
            ("/request all", "alias for /requestall."),
            ("/verifyrequests", "requester: resend verification DM. A failed DM never autoapproves; open the bot first."),
        ],
    },
    "notes": {
        "button": "Notes & Rules",
        "title": "Notes, filters & rules",
        "intro": (
            "Saved notes and automatic keyword filters can contain text, photo, video, "
            "sticker or files. Buttons like <code>[Site](buttonurl:https://…)</code> "
            "are supported; add <code>:same</code> to place the next button on the same row."
        ),
        "cmds": [
            ("/save name text", "admin: save text, or reply to a message with <code>/save name</code>."),
            ("/get name", "anyone: send it; writing <code>#name</code> also works."),
            ("/notes", "anyone: list the saved notes."),
            ("/saved", "same as /notes."),
            ("/clear name", "admin: delete a note."),
            ("/clearall", "admin: colored confirmation before deleting every note."),
            ("/privatenotes on|off", "admin: open notes in the bot's PM."),
            ("/filter keyword response", "admin: automatically reply when that exact word or phrase appears; reply to media with <code>/filter keyword</code> to save it. Also /addfilter."),
            ("/filters", "anyone: list this group's automatic filters."),
            ("/stop keyword", "admin: remove one automatic filter. Also /stopfilter."),
            ("/stopall", "admin: remove every filter after confirmation. Also /stopallfilters."),
            ("/get name noformat", "show the saved source without applying formatting."),
            ("/markdownhelp", "show button, filling, private, protect and preview syntax."),
            ("/setrules text", "admin: set the group rules."),
            ("/rules", "anyone: read the rules."),
            ("/resetrules", "admin: delete the rules."),
            ("/privaterules on|off", "admin: /rules opens rules in PM."),
            ("/setrulesbutton text", "admin: label of that button (max 30 letters)."),
            ("/resetrulesbutton", "admin: default label again."),
        ],
        "note": (
            "Fillings supported in notes, filters and greetings: <code>{first}</code>, "
            "<code>{last}</code>, <code>{fullname}</code>, <code>{username}</code>, "
            "<code>{mention}</code>, <code>{id}</code>, <code>{chat}</code> and "
            "<code>{chatname}</code>."
        ),
    },
    "fed": {
        "button": "Federation",
        "title": "Federations — share bans between groups",
        "intro": (
            "A fed links groups so fed bans apply everywhere. Group admins can create/"
            "join; only the fed owner changes fed settings."
        ),
        "cmds": [
            ("/newfed name", "create a fed, become owner and get its id."),
            ("/joinfed fedid", "connect this group to a fed."),
            ("/leavefed", "disconnect this group from its fed."),
            ("/fedinfo", "anyone: show this group's fed. Also /chatfed."),
            ("/fban [reason]", "fed admins, reply/@user: ban in every fed group."),
            ("/unfban", "fed admins, reply/@user: lift the fed ban."),
            ("/fedstat", "anyone, reply/@user: check a fed ban. Also /fbanstat."),
            ("/fbanlist", "list fed-banned user ids (first 50)."),
            ("/fedadmins", "anyone: show fed owner and admins."),
            ("/fedpromote", "fed owner, reply/@user: make fed admin."),
            ("/feddemote", "fed owner, reply/@user: remove fed admin."),
            ("/feddemoteme", "step down as fed admin."),
            ("/myfeds", "list feds you own (also works in PM)."),
            ("/renamefed name", "fed owner: rename it."),
            ("/delfed", "fed owner: delete the fed."),
            ("/fedreason text", "fed owner: default ban reason (empty = clear)."),
            ("/fednotif on|off", "fed owner: announce fed bans in the group."),
            ("/quietfed on|off", "fed owner: silently ban fed-banned joiners."),
            ("/setfedlog @channel", "fed owner: send ban logs to a channel; bot must be admin. /unsetfedlog removes it."),
            ("/importfbans", "fed owner, reply to a .txt of user ids: import bans."),
            ("/subfed fedid", "fed owner: apply another fed's bans here."),
            ("/unsubfed [fedid]", "fed owner: stop following it (no id = all)."),
            ("/fedsubs", "anyone: list followed feds."),
        ],
    },
    "tools": {
        "button": "Tools",
        "title": "Group tools",
        "intro": "Everyday helpers; admin-only unless marked “anyone”. "
                 "Group info commands need <b>Change group info</b>; /unpinall needs "
                 "<b>Pin messages</b>.",
        "cmds": [
            ("/ping", "anyone: check the bot is alive."),
            ("/settitle text", "change the group name."),
            ("/setdescription text", "change the group description."),
            ("/setgpic", "reply to a photo: set it as the group picture."),
            ("/zombies", "count deleted accounts; <code>/zombies clean</code> removes them."),
            ("/stats", "today's active members; <code>/stats reset</code> clears count."),
            ("/echo text", "repeat your text; <code>/say</code> does too and deletes your command."),
            ("/addpost 6h text", "post this text again every 6h (1m–7d, max 10 posts)."),
            ("/posts", "list the scheduled posts with their ids."),
            ("/delpost id", "stop a scheduled post."),
            ("/actiontopic", "anyone: show the forum topic used for bot actions."),
            ("/setactiontopic", "admin: set the current forum topic as the action topic."),
            ("/newtopic name", "admin: create a forum topic."),
            ("/renametopic name", "admin: rename the current forum topic."),
            ("/closetopic", "admin: close the current forum topic."),
            ("/reopentopic", "admin: reopen the current forum topic."),
            ("/deletetopic", "admin: delete the current forum topic."),
        ],
    },
    "quotes": {
        "button": "Quotes",
        "title": "Quote stickers",
        "intro": "Everyone can use these, in the group and in the bot's PM.",
        "cmds": [
            ("/q", "reply to a message: make a quote sticker. Add @username to keep the same text but show that real account, or add a custom name. Also /quote."),
            ("/fq @user text", "fake quote: their name and photo, your words. Or reply to someone with <code>/fq text</code>. Also /tq. The bot owner can't be fake-quoted unless he allows you."),
        ],
    },
    "reminders": {
        "button": "Reminders & AFK",
        "title": "Reminders & AFK",
        "intro": "Everyone can use these; reminders also work in the bot's PM.",
        "cmds": [
            ("/remindme 10m text", "get a reminder after the delay (30s–30d, max 10)."),
            ("/reminders", "list your reminders."),
            ("/delreminder 1", "delete reminder 1."),
            ("/afk [reason]", "mark away; Maki answers tags/replies until you write again."),
        ],
    },
    "approval": {
        "button": "Approval",
        "title": "Approval — trusted members",
        "intro": "Admins can exempt trusted users from automated locks, blocklists and antiflood. Approval does not grant Telegram admin rights and does not undo an existing ban or mute.",
        "cmds": [
            ("/approval", "check a user's approval status."),
            ("/approve", "ban-rights admin: reply/@user/id to grant rule exemptions."),
            ("/unapprove", "ban-rights admin: remove a user's exemptions."),
            ("/approved", "list approved users."),
            ("/approvesettings", "view approval policy; owner + ban-rights admins can change it."),
            ("/unapproveall", "owner: remove every approval; this cannot be undone."),
        ],
    },
    "captcha": {
        "button": "CAPTCHA",
        "title": "CAPTCHA — verify new members",
        "intro": "Admins only. New joiners must pass the selected test. Welcome messages must be enabled for CAPTCHA to work.",
        "cmds": [
            ("/captcha on|off", "require new members to solve a CAPTCHA."),
            ("/captchamode button|math|text|text2", "choose the CAPTCHA type."),
            ("/captcharules on|off", "require new members to accept the rules."),
            ("/captchamutetime 1h", "unmute after this time; use off to wait indefinitely."),
            ("/captchakick on|off", "kick members who do not solve it."),
            ("/captchakicktime 1h", "set the unsolved CAPTCHA timeout."),
            ("/setcaptchatime 5", "set the join-button timeout in minutes."),
            ("/setcaptchatext text", "set custom CAPTCHA button text."),
            ("/resetcaptchatext", "restore the default button text."),
        ],
    },
    "clean_commands": {
        "button": "Clean Commands",
        "title": "Clean Commands",
        "intro": "Admins only. These settings delete command messages while the command still runs.",
        "cmds": [
            ("/cleancommand all|admin|user|other", "choose command categories to delete."),
            ("/keepcommand type", "stop deleting a command category."),
            ("/cleancommandtypes", "list command categories."),
        ],
    },
    "clean_service": {
        "button": "Clean Service",
        "title": "Clean Service",
        "intro": "Admins only. Delete automated Telegram service notices; this does not scan message history.",
        "cmds": [
            ("/cleanservice type|on|off", "delete selected service-message categories."),
            ("/keepservice type", "keep a service-message category."),
            ("/nocleanservice type", "alias for /keepservice."),
            ("/cleanservicetypes", "list service-message categories."),
        ],
    },
    "connections": {
        "button": "Connections",
        "title": "Connections — manage a group from PM",
        "intro": "Admins can connect to a group's settings from PM. Use a numeric chat ID or username.",
        "cmds": [
            ("/connect chatid|@username", "connect to a chat; no argument lists recent chats in PM."),
            ("/disconnect", "end the current connection."),
            ("/reconnect", "reconnect to the previously connected chat."),
            ("/connection", "show the connected chat."),
            ("/mygroup", "in PM: show data for the connected group."),
        ],
    },
    "disabling": {
        "button": "Disabling",
        "title": "Disabling — restrict commands",
        "intro": "Admins only. Disabled commands are blocked for members by default; enable the admin toggle to block admins too.",
        "cmds": [
            ("/disable command", "stop members using a command; /disable all disables every listed command."),
            ("/enable command", "allow a disabled command again."),
            ("/disableable", "list commands that can be disabled."),
            ("/disabledel on|off", "delete disabled commands used by non-admins."),
            ("/disableadmin on|off", "also enforce disabled commands for admins."),
            ("/disabled", "list commands disabled in this chat."),
        ],
    },
    "misc": {
        "button": "Misc",
        "title": "Miscellaneous",
        "intro": "Small utility commands; permissions are shown per command.",
        "cmds": [
            ("/runs", "reply with a randomly generated run-away message."),
            ("/id", "show a user, group or channel ID."),
            ("/info", "show a user's information."),
            ("/donate", "show donation information, when configured."),
            ("/limits", "show configured bot limits."),
        ],
    },
    "formatting": {
        "button": "Markdown/Formatting",
        "title": "Markdown & Formatting",
        "intro": "Use formatting in saved notes, filters and greetings.",
        "cmds": [
            ("/markdownhelp", "show Markdown, button, filling, private, protect and preview syntax."),
        ],
    },
    "buttons": {
        "button": "Buttons & Fillings",
        "title": "Buttons, Fillings & Random Content",
        "intro": "Message syntax features, not Telegram commands. These work in notes, filters and greetings.",
        "cmds": [
            ("[Label](buttonurl://URL)", "create a URL button; add :same for the same row."),
            ("[Label](buttonurl://#note)", "create a button opening an existing note."),
            ("{first} {last} {fullname} {username} {mention} {id}", "fill user identity details."),
            ("{chatname} {rules}", "fill chat name or add a rules button."),
            ("{preview} {preview:top} {nonotif}", "control previews and notifications."),
            ("{protect} {mediaspoiler}", "protect content or mark media as spoiler."),
            ("%%%", "separate random alternatives in notes, filters and greetings."),
        ],
    },
    "lock_types": {
        "button": "Lock Types",
        "title": "Lock Types & Examples",
        "intro": "Use these values with /lock and /unlock. Locked content is deleted; admins are exempt.",
        "cmds": [
            ("all album audio document photo video", "media and album locks."),
            ("sticker stickeranimated stickerpremium emojicustom", "sticker and custom-emoji locks."),
            ("url invitelink botlink inline command email phone cashtag", "link, command and text locks."),
            ("forward forwardbot forwardchannel forwardstory forwarduser", "forward locks."),
            ("text emoji emojionly cjk cyrillic rtl zalgo", "text and character locks."),
            ("bot guestbot game gif poll location contact voice videonote", "other message/media locks."),
            ("button checklist collage comment externalreply richmessage slideshow spoiler", "rich-message and context locks."),
        ],
    },
    "pins": {
        "button": "Pins",
        "title": "Pins",
        "intro": "Admins need Pin Messages. Reply to a message for /pin and /unpin.",
        "cmds": [
            ("/pin [loud|notify]", "pin the replied message; optionally notify members."),
            ("/permapin text", "send and pin a formatted custom message."),
            ("/unpin", "unpin the replied message, or the current pin."),
            ("/unpinall", "unpin all pinned messages."),
            ("/antichannelpin on|off", "stop automatic linked-channel pins."),
            ("/cleanlinked on|off", "delete messages posted by the linked channel."),
            ("/pinned", "get the current pinned message."),
        ],
    },
    "purges": {
        "button": "Purges",
        "title": "Purges",
        "intro": "Admins need Delete Messages. Purges are irreversible; Telegram may protect some service messages.",
        "cmds": [
            ("/purge", "delete from the replied message through the command."),
            ("/purge X", "delete the next X messages after the replied message."),
            ("/spurge", "purge without the final confirmation message."),
            ("/del", "delete the replied message."),
            ("/purgefrom", "mark a replied message as the start."),
            ("/purgeto", "delete between the marked start and this reply."),
        ],
    },
    "reports": {
        "button": "\U0001f4e9 Reports",
        "title": "\U0001f4e9 Reports — alert every admin",
        "intro": "Anyone can write <code>@admin</code> (best as a reply to the "
                 "problem message) or use <code>/report</code>. Maki answers "
                 "<b>Report sent</b>, silently pings every admin in the group "
                 "and DMs each admin who has started the bot, plus the owner. "
                 "Admins can tap <b>\u21a9\ufe0f Reply to the group</b> in that DM "
                 "to answer straight into the group.",
        "cmds": [
            ("@admin [reason]", "write it anywhere in a message; reply to a message to report it."),
            ("/report [reason]", "reply to a message to alert the admins. Also /admin."),
            ("/reports on|off", "admin: enable or disable member reports."),
        ],
        "note": "Admins only get DMs if they have pressed Start in my private chat once.",
    },
    "tagall": {
        "button": "\U0001f4e3 Tag All",
        "title": "\U0001f4e3 Tag All — mention every member",
        "intro": "Admins with <b>Pin messages</b>. Members are mentioned by emoji "
                 "(names hidden) in batches of 7, and your text repeats on top of "
                 "every batch. A button menu lets you Start, Cancel or Stop.",
        "cmds": [
            ("/tagall text", "open the Tag All menu with your message. Also /mentionall and /all."),
            ("/tagall (reply)", "reply to any message to repeat that message in each batch."),
        ],
        "note": "Up to 150 members per run, 10-minute cooldown per group.",
    },
    "owner": {
        "button": "\U0001f451 Owner console",
        "title": "\U0001f451 Owner console — bot owner only",
        "intro": "Silent tools for the bot owner. The command is deleted from the "
                 "group and the answer arrives in DM. Others get no reply.",
        "cmds": [
            ("/manage <chat_id>", "group panel: title, members, bot rights. Also /chatpanel."),
            ("/bans <chat_id>", "list banned users of a group."),
            ("/unban <chat_id> <user>", "in DM: unban one user in that group. Also /ungban."),
            ("/chatlink <chat_id>", "get the group's invite link. Also /grouplink."),
            ("/power", "every group the bot is in, with its admin rights. Also /adminpower, /mygroups."),
            ("/refreshgroups", "verify stored groups against Telegram. Also /groupsync."),
        ],
    },
    "import_export": {
        "button": "Import/Export",
        "title": "Import & Export",
        "intro": "Export is available to administrators; importing and resetting require the group creator. Operations are rate limited.",
        "cmds": [
            ("/export [categories]", "generate a JSON settings file."),
            ("/import [categories]", "reply to a JSON settings file."),
            ("/reset [categories]", "creator only: remove selected or all chat settings."),
        ],
    },
    "settings": {
        "button": "Settings",
        "title": "Bot settings, logs, backup, PM connection",
        "intro": "Admins only; PM connection commands are marked below.",
        "cmds": [
            ("/adminerror on|off", "reply when non-admins try admin commands (default on)."),
            ("/anonadmin on|off", "let anonymous admins use admin commands."),
            ("/disable command", "hide a command from non-admins, e.g. <code>/disable afk</code>."),
            ("/enable command", "allow it again."),
            ("/disabled", "list disabled commands."),
            ("/setlog @channel", "send ban/mute/delete logs to a channel; make Maki admin first."),
            ("/unsetlog", "stop logging."),
            ("/export", "get this group's settings and notes as a file."),
            ("/export notes filters", "export only selected categories."),
            ("/import [categories]", "reply to that file; group owner only. Categories can be selected."),
            ("/reset [categories]", "group owner only: reset portable settings."),
            ("/connect", "manage this group from PM; run here for a link, or send <code>/connect chatid</code> in PM."),
            ("/mygroup", "in PM: show connected group's notes, rules, blocklist and settings."),
            ("/connection", "in PM: show the connected group."),
            ("/disconnect", "in PM: end the connection."),
            ("/privacy", "anyone: see what data the bot stores."),
        ],
    },
}

# hidden owner-only commands that intentionally do not appear in /help
HIDDEN_COMMANDS = {"start", "help", "broadcast",
                   "addsudo", "delsudo", "rmsudo", "listsudo", "sudolist",
                   "restrict", "unrestrict", "restricted", "fqallow", "fqdeny", "fqallowed",
                   "groups", "ginfo", "gsay", "gban", "gunban", "massban",
                   "clearme", "clearme_session", "clearme_stop"}

# Two-level help: Main menu -> category -> section.  A flat grid of 40
# buttons was unreadable on a phone; every section belongs to exactly one
# category (enforced below so a new section can never vanish from the menu).
HELP_CATEGORIES: dict[str, dict] = {
    "mod": {"button": "\U0001f6e1 Moderation", "title": "Moderation",
            "desc": "Ban, mute, warn, purge, pin, promote and reports.",
            "sections": ["admin", "warns", "purges", "pins", "promote", "approval", "reports"]},
    "protect": {"button": "\U0001f512 Protection", "title": "Protection",
                "desc": "Locks, blocklists, antiflood, Shield, links, CAPTCHA and night mode.",
                "sections": ["locks", "lock_types", "blocklist", "flood", "shield",
                             "linkdel", "editguard", "captcha", "nightmode"]},
    "members": {"button": "\U0001f44b Members", "title": "Members",
                "desc": "Greetings, join requests, VC joins and Tag All.",
                "sections": ["welcome", "join_requests", "vc_monitor", "tagall"]},
    "content": {"button": "\U0001f4dd Notes & Content", "title": "Notes & Content",
                "desc": "Notes, filters, rules, formatting, buttons and quote stickers.",
                "sections": ["notes", "formatting", "buttons", "quotes"]},
    "clean": {"button": "\U0001f9f9 Cleanup", "title": "Cleanup",
              "desc": "Auto-delete, clean commands and clean service messages.",
              "sections": ["autodel", "clean_commands", "clean_service"]},
    "tools": {"button": "\U0001f9f0 Group Tools", "title": "Group Tools",
              "desc": "Everyday tools, Chat Fight, reminders, AFK and extras.",
              "sections": ["tools", "chatfight", "reminders", "extras", "misc", "commands"]},
    "settings": {"button": "\u2699\ufe0f Settings", "title": "Settings",
                 "desc": "Bot settings, logs, PM connection, disabling, backup and federations.",
                 "sections": ["settings", "connections", "disabling", "import_export", "fed"]},
    "owner": {"button": "\U0001f451 Owner", "title": "Owner",
              "desc": "Bot-owner and group-owner bulk tools.",
              "sections": ["owner", "massowner"]},
}

_BUTTON_LABELS = {
    "chatfight": "\U0001f3c6 Chat Fight", "admin": "\U0001f528 Bans & Mutes",
    "promote": "\u2b06\ufe0f Promote", "warns": "\u26a0\ufe0f Warns",
    "linkdel": "\U0001f517 Link Deleter", "locks": "\U0001f512 Locks",
    "blocklist": "\U0001f6ab Blocklist", "flood": "\U0001f30a Antiflood",
    "shield": "\U0001f6e1 Shield", "editguard": "\u270f\ufe0f Edit Guardian",
    "autodel": "\u23f1 Auto-delete", "nightmode": "\U0001f319 Night Mode",
    "welcome": "\U0001f44b Greetings", "vc_monitor": "\U0001f399 VC Joins",
    "join_requests": "\U0001f6aa Join Requests", "notes": "\U0001f4cc Notes & Rules",
    "fed": "\U0001f310 Federation", "tools": "\U0001f527 Tools",
    "quotes": "\U0001f4ac Quotes", "reminders": "\u23f0 Reminders & AFK",
    "approval": "\u2705 Approval", "captcha": "\U0001f916 CAPTCHA",
    "clean_commands": "\U0001f9fd Clean Commands", "clean_service": "\U0001f9f9 Clean Service",
    "connections": "\U0001f50c Connections", "disabling": "\u26d4 Disabling",
    "misc": "\U0001f4e6 Misc", "formatting": "\U0001f520 Formatting",
    "buttons": "\U0001f518 Buttons & Fillings", "lock_types": "\U0001f4cb Lock Types",
    "pins": "\U0001f4cd Pins", "purges": "\U0001f5d1 Purges",
    "import_export": "\U0001f4be Import/Export", "settings": "\u2699\ufe0f Bot Settings",
}
for _k, _label in _BUTTON_LABELS.items():
    if _k in HELP_SECTIONS:
        HELP_SECTIONS[_k]["button"] = _label

SECTION_CATEGORY: dict[str, str] = {}
for _ck, _cat in HELP_CATEGORIES.items():
    _cat["sections"] = [k for k in _cat["sections"] if k in HELP_SECTIONS]
    for _sk in _cat["sections"]:
        SECTION_CATEGORY.setdefault(_sk, _ck)
for _sk in HELP_SECTIONS:  # safety net: never orphan a section
    if _sk not in SECTION_CATEGORY:
        HELP_CATEGORIES["tools"]["sections"].append(_sk)
        SECTION_CATEGORY[_sk] = "tools"

_NO_PREVIEW = types.LinkPreviewOptions(is_disabled=True)

_ALIAS_RE = re.compile(r"(?<![\w/<])/([a-z]+)")


def _build_command_index() -> dict[str, str]:
    """command name -> section key, covering the usage token of every entry
    plus aliases mentioned in descriptions ("Also /quote", "Alias: /fsub")."""
    index: dict[str, str] = {}
    for key, sec in HELP_SECTIONS.items():
        for usage, desc in sec["cmds"]:
            index.setdefault(usage.split()[0].lstrip("/"), key)
            for alias in _ALIAS_RE.findall(desc):
                index.setdefault(alias, key)
    return index


COMMAND_INDEX = _build_command_index()

# Telegram hard limit for a text message; sections must stay under it.
_TG_MAX = 4096
# Keep each help page small enough to scan on a phone.  The command data is
# deliberately kept in one place above; pagination is only a presentation
# detail, so adding a command cannot make Telegram silently lose the tail of a
# section.
_HELP_PAGE_SIZE = 7


def _section_page_count(key: str) -> int:
    """Return the number of command pages for a known help section."""
    if key not in HELP_SECTIONS:
        return 1
    count = len(HELP_SECTIONS[key].get("cmds", ()))
    return max(1, (count + _HELP_PAGE_SIZE - 1) // _HELP_PAGE_SIZE)


def _clamp_help_page(page, total: int) -> int:
    """Parse a callback page without allowing stale/hostile values to fail."""
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    return max(1, min(page, max(1, total)))


def _help_target(data: str):
    """Parse a help callback and clamp its page.

    Page one intentionally has the old short callback form (``gh:help:admin``)
    so buttons already sent by older Maki instances remain usable.
    """
    parts = (data or "").split(":")
    if len(parts) < 3 or parts[0] != "gh" or parts[1] != "help":
        return None
    key = parts[2]
    if key == "main":
        if len(parts) > 3:
            return None
        return ("main", 1)
    if key == "cat":
        if len(parts) != 4 or parts[3] not in HELP_CATEGORIES:
            return None
        return ("cat", parts[3])
    if key not in HELP_SECTIONS:
        return None
    if len(parts) > 4:
        return None
    page = parts[3] if len(parts) > 3 else 1
    return key, _clamp_help_page(page, _section_page_count(key))


def _plain_caption(text: str, limit: int = 1024) -> str:
    """Make a bounded, valid caption for a poster that cannot be deleted.

    A caption is a last resort: normal help always transitions to a text
    message, which has the full page and keyboard.  Stripping the static HTML
    tags before escaping means this fallback can never leave an unclosed
    ``<code>``/``<b>`` tag in Telegram.
    """
    plain = html.unescape(re.sub(r"<[^>]+>", "", text or ""))
    plain = re.sub(r"[ \t]+", " ", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
    if len(plain) <= limit:
        return plain
    suffix = "\n\nOpen /help for the full page."
    if limit <= len(suffix):
        return suffix[:limit]
    room = max(0, limit - len(suffix))
    # This is deliberately plain text (not a sliced HTML string).
    return plain[:room].rstrip() + suffix


def _ordered_function_keys() -> list[str]:
    """Function keys in importance order (owner 2026-10-18).

    Group admins reach for moderation, spam control and cleaning first, so
    those lead.  Member-facing and reference tools follow, and configuration /
    owner-only functions sit at the end.  Anything not listed is appended
    afterwards, so a new function can never vanish from the menu.
    """
    order = [
        # 1. the daily moderating core
        "admin", "warns", "purges", "pins", "promote", "reports", "approval",
        # 2. spam control - what keeps a group usable
        "flood", "locks", "lock_types", "blocklist", "shield", "linkdel",
        "editguard", "egexempt", "captcha",
        # 3. members
        "welcome", "join_requests", "tagall", "vc_monitor",
        # 4. reference content members read
        "notes", "filters", "formatting", "buttons", "quotes",
        # 5. housekeeping
        "autodel", "clean_commands", "clean_service", "nightmode",
        # 6. everyday utilities
        "tools", "chatfight", "reminders", "afk", "extras", "misc", "commands",
        # 7. configuration, then owner-only
        "settings", "connections", "disabling", "import_export", "fed",
        "owner", "massowner",
        # 8. catch-all
        "modernfeatures", "more",
    ]
    keys = [k for k in order if k in HELP_SECTIONS]
    keys += [k for k in HELP_SECTIONS if k not in keys]
    return keys



EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emoji, symbols and pictographs
    "\u2190-\u21FF"           # arrows (nav buttons)
    "\u2300-\u23FF"           # technical: clocks, hourglass, stopwatch
    "\u2600-\u27BF"           # misc symbols + dingbats
    "\u2B00-\u2BFF"           # misc symbols and arrows
    "\uFE0F\u200D\u20E3"     # variation selector, ZWJ, keycap
    "]+"
)


def _strip_emoji(text: str) -> str:
    """Remove emoji/pictographs from a button label (owner 2026-10-18).

    Only symbol ranges are touched, so text like "Notes & Rules" or "VC Joins"
    survives intact.  Falls back to the original text if stripping would leave
    nothing, so a button can never end up blank.
    """
    if not text:
        return text
    cleaned = re.sub(r"\s{2,}", " ", EMOJI_RE.sub(" ", str(text))).strip(" -\u00b7|")
    return cleaned or str(text).strip()



def _help_keyboard() -> types.InlineKeyboardMarkup:
    """_FLAT_HELP: every function as its own button, no categories.

    The two-level menu (main -> category -> topic) is gone (owner
    2026-10-17): all topics are listed directly, one per row, and tapping one
    opens its commands.  Order follows HELP_SECTIONS, which is grouped so
    related functions still sit near each other.
    """
    rows = []
    items = [
        (_strip_emoji(HELP_SECTIONS[key].get("button")
                      or HELP_SECTIONS[key].get("title") or key),
         f"gh:help:{key}")
        for key in _ordered_function_keys()
    ]
    # three buttons per row (owner 2026-10-18): full-width rows made the
    # buttons read as one long strip.
    for i in range(0, len(items), 3):
        rows.append([
            ikb(label, callback_data=cb, style=enums.ButtonStyle.PRIMARY)
            for label, cb in items[i:i + 3]
        ])
    rows.append([
        ikb("Close", callback_data="gh:close",
            style=enums.ButtonStyle.DANGER)
    ])
    return ikm(rows)


def _category_text(cat: str) -> str:
    c = HELP_CATEGORIES[cat]
    total = sum(len(HELP_SECTIONS[k]["cmds"]) for k in c["sections"])
    lines = [
        _HELP_HEADER.format(name=html.escape(_help_name())),
        f"<b>{c['button']}</b> \u00b7 <i>{total} commands</i>",
        "",
        html.escape(c["desc"]),
        "",
    ]
    for k in c["sections"]:
        sec = HELP_SECTIONS[k]
        lines.append(f"\u2022 <b>{sec['button']}</b> \u2014 {len(sec['cmds'])} commands")
    lines += ["", "<i>Tap a topic to see its commands.</i>"]
    return "\n".join(lines)




def _category_keyboard(cat: str) -> types.InlineKeyboardMarkup:
    keys = HELP_CATEGORIES[cat]["sections"]
    rows = []
    for k in keys:                      # _ONE_PER_ROW
        rows.append([
            ikb(HELP_SECTIONS[k]["button"], callback_data=f"gh:help:{k}",
                style=enums.ButtonStyle.PRIMARY)
        ])
    rows.append([ikb("Back", callback_data="gh:help:main",
                     style=enums.ButtonStyle.PRIMARY)])
    return ikm(rows)


_HELP_HEADER = "\U0001f4d6 <b>Help</b> \u00b7 {name}"


def _help_name() -> str:
    """The bot's real display name (was the hard-coded 'GroupHelp')."""
    name = getattr(config, "BOT_NAME", None)
    return str(name) if name else "Help"


def _main_help_text() -> str:
    total = sum(len(sec["cmds"]) for sec in HELP_SECTIONS.values())
    return (
        _HELP_HEADER.format(name=html.escape(_help_name())) + "\n"
        f"<i>{total} commands in {len(HELP_SECTIONS)} functions</i>\n\n"
        "Tap any function below to see its commands.\n\n"
        "<b>Quick tips</b>\n"
        "\u2022 Reply to a user's message when using admin commands.\n"
        "\u2022 <code>/</code>, <code>!</code> and <code>?</code> all work.\n"
        "\u2022 <code>/help ban</code> jumps straight to a command.\n"
        "\u2022 Write <code>@admin</code> to alert every admin."
    )


def _section_text(key: str, page=None) -> str:
    """Render one help page with deliberate, even spacing.

    Layout:
        Title
        (blank)
        Intro            <- page 1 only
        (blank)
        • /cmd  — what it does
        • /cmd  — what it does
        (blank)
        page indicator / note
    """
    sec = HELP_SECTIONS[key]
    per_page = 7
    total = max(1, (len(sec["cmds"]) + per_page - 1) // per_page)
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    current = max(1, min(current, total))
    start = (current - 1) * per_page
    commands = sec["cmds"][start:start + per_page]

    title = sec["title"]
    if total > 1:
        title = f"{title} · {current}/{total}"

    out = [f"<b>{title}</b>"]
    if current == 1:
        intro = " ".join(str(sec["intro"]).split())
        out += ["", intro]
    out.append("")
    for usage, desc in commands:
        compact = " ".join(str(desc).split())
        out.append(f"• <code>{html.escape(usage)}</code>")
        out.append(f"   <i>{compact}</i>")
    if total > 1:
        out += ["", f"<i>Page {current} of {total} — use Next for more.</i>"]
    if sec.get("note"):
        out += ["", f"<b>Note:</b> {sec['note']}"]
    return "\n".join(out)


def _section_keyboard(section=None, page=None) -> types.InlineKeyboardMarkup:
    """Only buttons that belong to this topic: its own action (if any),
    page navigation (if it has pages) and Back to its category."""
    rows = []
    if section == "linkdel":
        rows.append([ikb("Open link settings", callback_data="gh:ld:open",
                         style=enums.ButtonStyle.SUCCESS)])
    elif section == "join_requests":
        rows.append([ikb("Open join request settings", callback_data="gh:jr:open",
                         style=enums.ButtonStyle.SUCCESS)])
    if section in HELP_SECTIONS:
        total = _section_page_count(section)
        current = _clamp_help_page(page or 1, total)
        if total > 1:
            # _ONE_PER_ROW: each control on its own line
            if current > 1:
                rows.append([ikb(
                    f"Prev \u00b7 page {current - 1}/{total}",
                    callback_data=f"gh:help:{section}:{current - 1}",
                    style=enums.ButtonStyle.PRIMARY)])
            if current < total:
                rows.append([ikb(
                    f"Next \u00b7 page {current + 1}/{total}",
                    callback_data=f"gh:help:{section}:{current + 1}",
                    style=enums.ButtonStyle.PRIMARY)])
    # Back always returns to the flat function list now.
    rows.append([ikb("Back to all functions", callback_data="gh:help:main",
                     style=enums.ButtonStyle.PRIMARY)])
    return ikm(rows)


@app.on_message(filters.command("start", prefixes=["/", "?", "!", "."]))
async def start_command(_, m: types.Message) -> None:
    try:
        if m.chat.type != enums.ChatType.PRIVATE:
            _text = (f"\U0001f916 <b>{html.escape(config.BOT_NAME)}</b> is ready.\n"
                     "Send <code>/help</code> for the command list, "
                     "or <code>@admin</code> to alert the admins.")
            try:
                from ghelp.plugins.modern import reply_ephemeral as _eph
                await _eph(m, _text, parse_mode=enums.ParseMode.HTML)
            except Exception:
                await m.reply_text(_text, parse_mode=enums.ParseMode.HTML)
            return

        # Rose-style deep links: rules_<chat>, note_<chat>_<name>, connect_<chat>
        payload = (m.command[1] if len(m.command) > 1 else "").strip()
        if payload.startswith(("wc_", "wr_")):
            # welcome deep links are served by the welcome plugin; import it
            # here (as the branches below do) so a path without a handler can
            # never raise NameError.
            try:
                from ghelp.plugins.welcome import serve_deeplink as _serve_wc
            except ImportError:
                _serve_wc = None
            if _serve_wc is not None and await _serve_wc(m, payload):
                return
        if payload:
            try:
                from ghelp.plugins.rulesx import serve_deeplink as _serve
                if await _serve(m, payload):
                    return
                from ghelp.plugins.connect import serve_deeplink as _serve_cx
                if await _serve_cx(m, payload):
                    return
            except Exception:
                pass

        me = await app.get_me()
        username = me.username or ""
        keyboard = ikm(
            [
                [
                    ikb(
                        "Add me to your group",
                        url=f"https://t.me/{username}?startgroup=true",
                        style=enums.ButtonStyle.SUCCESS,
                    )
                ],
                [
                    ikb(
                        "Help",
                        callback_data="gh:help:main",
                        style=enums.ButtonStyle.PRIMARY,
                    )
                ],
                [
                    ikb(
                        "Support",
                        url=config.OWNER_LINK,
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                    ikb(
                        "Owner",
                        url=config.OWNER_LINK,
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                ],
            ]
        )
        text = (
            f"<b>{html.escape(config.BOT_NAME)}</b>\n\n"
            "Moderation, welcomes, notes and group tools.\n"
            "Add me as an admin, then open Help."
        )
        try:
            await m.reply_photo(
                photo=config.START_IMG,
                caption=text,
                parse_mode=enums.ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception:
            # a missing/broken START_IMG must never silence /start
            await m.reply_text(
                text,
                parse_mode=enums.ParseMode.HTML,
                reply_markup=keyboard,
                link_preview_options=_NO_PREVIEW,
            )
    except Exception:
        await m.reply_text(
            "<b>Could not open the start panel.</b>",
            parse_mode=enums.ParseMode.HTML,
        )


@app.on_message(filters.command("id", prefixes=["/", "?", "!", "."]))
async def id_command(_, m: types.Message) -> None:
    try:
        lines = [f"<b>Chat ID:</b> <code>{m.chat.id}</code>"]
        if m.from_user:
            lines.append(f"<b>Your ID:</b> <code>{m.from_user.id}</code>")
        if m.reply_to_message and m.reply_to_message.from_user:
            lines.append(
                f"<b>Replied user ID:</b> "
                f"<code>{m.reply_to_message.from_user.id}</code>"
            )
        await m.reply_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)
    except Exception:
        await m.reply_text("<b>Could not read IDs.</b>", parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command("ping", prefixes=["/", "?", "!", "."]))
async def ping_command(_, m: types.Message) -> None:
    started = time.perf_counter()
    try:
        sent = await m.reply_text("<b>Pinging…</b>", parse_mode=enums.ParseMode.HTML)
        elapsed = (time.perf_counter() - started) * 1000
        await sent.edit_text(
            f"<b>Pong!</b> <code>{elapsed:.0f} ms</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        try:
            await m.reply_text("<b>Ping failed.</b>", parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass


@app.on_message(filters.command("help", prefixes=["/", "?", "!", "."]))
async def help_command(_, m: types.Message) -> None:
    """/help opens the menu; /help <section> or /help <command> jumps straight
    to the matching section (e.g. /help warns, /help ban)."""
    try:
        arg = (m.command[1] if len(m.command) > 1 else "").strip().lstrip("/!?").lower()
        if arg in HELP_CATEGORIES:
            await m.reply_text(
                _category_text(arg), parse_mode=enums.ParseMode.HTML,
                reply_markup=_category_keyboard(arg),
            )
            return
        section = arg if arg in HELP_SECTIONS else COMMAND_INDEX.get(arg)
        if section:
            await m.reply_text(
                _section_text(section, 1),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=_section_keyboard(section, 1),
                link_preview_options=_NO_PREVIEW,
            )
            return
        await m.reply_text(
            _main_help_text(),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_help_keyboard(),
        )
    except Exception:
        await m.reply_text(
            "<b>Could not open help.</b>", parse_mode=enums.ParseMode.HTML
        )


_HELP_CB = (
    r"^gh:(?:close|help:(?:main|cat|"
    + "|".join(HELP_SECTIONS)
    + r")(?::[^:]*)?)$"
)


async def _answer_help(q: types.CallbackQuery, text: str = "", alert: bool = False):
    try:
        await q.answer(text, show_alert=alert)
    except Exception as exc:
        # Callback answers are best effort; a stale query must not create a
        # second exception while the handler is recovering the help panel.
        _log_help_failure("answer", exc)


async def _reopen_help(q: types.CallbackQuery) -> bool:
    """Reopen the root panel only in the callback's own chat."""
    message = getattr(q, "message", None)
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return False
    try:
        await app.send_message(
            chat_id,
            _main_help_text(),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=_help_keyboard(),
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:
        _log_help_failure("reopen", exc)
        return False


async def _media_help_transition(q, text: str, keyboard) -> bool:
    """Put the help where the poster was, so taps act in ONE message.

    Telegram cannot edit a photo message into a text message, so the poster is
    removed and the help is sent in its place.  From then on every button tap
    edits that message, which is what makes the menu feel in-place
    (2026-09-30).
    """
    message = getattr(q, "message", None)
    if message is None:
        return False
    chat_id = message.chat.id
    try:
        await message.delete()
    except Exception as exc:
        _log_help_failure("poster delete", exc)
    try:
        sent = await app.send_message(
            chat_id,
            text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:
        _log_help_failure("help send", exc)
        return False


@app.on_callback_query(filters.regex(_HELP_CB))
async def help_callback(_, q: types.CallbackQuery) -> None:
    data = q.data or ""
    if data == "gh:close":
        # A callback spinner must be acknowledged before doing any Telegram
        # work (deleting/editing can otherwise make every help button feel
        # dead on a busy client).
        await _answer_help(q)
        try:
            if q.message:
                await q.message.delete()
        except Exception as exc:
            _log_help_failure("close", exc)
        return

    target = _help_target(data)
    if target is None:
        await _answer_help(q, "Help expired.", alert=True)
        return
    section, page = target
    message = getattr(q, "message", None)
    if message is None:
        await _answer_help(q, "Help expired.", alert=True)
        return

    try:
        # Ack immediately; the actual edit/send follows in the same callback
        # so a slow media transition cannot leave Telegram's spinner running.
        await _answer_help(q)

        if section == "main":
            text, keyboard = _main_help_text(), _help_keyboard()
        elif section == "cat":
            text, keyboard = _category_text(page), _category_keyboard(page)
        else:
            text = _section_text(section, page)
            keyboard = _section_keyboard(section, page)

        # A /start poster cannot hold a long section as a caption.  Never try
        # to edit its caption with full HTML; make a valid text transition.
        if getattr(message, "photo", None):
            if await _media_help_transition(q, text, keyboard):
                return
            if await _reopen_help(q):
                return
            await _answer_help(q, "Help expired.", alert=True)
            return

        try:
            await message.edit_text(
                text,
                parse_mode=enums.ParseMode.HTML,
                reply_markup=keyboard,
                link_preview_options=_NO_PREVIEW,
            )
            return
        except Exception as exc:
            if "MESSAGE_NOT_MODIFIED" in str(exc):
                return  # same page tapped twice: nothing to do
            # A deleted/old text message is recoverable, but only by writing
            # the root help message back into that exact chat.
            # Never post a second help message on a tap: that is what left
            # duplicate prompts piling up in the chat.
            _log_help_failure("edit", exc)
            await _answer_help(q, "This help menu expired — send /help again.", alert=True)
    except Exception as exc:
        # Keep stale/corrupt callback data user-visible rather than leaving a
        # spinner forever.  _reopen_help is chat-local and catches its own
        # Telegram errors.
        _log_help_failure("callback", exc)
        await _answer_help(q, "This help menu expired — send /help again.", alert=True)

@app.on_message(filters.group & filters.service)
async def bot_added(_, m: types.Message) -> None:
    try:
        me = await app.get_me()
        members = m.new_chat_members or []
        added = any(member.id == me.id for member in members)
        upgraded = bool(m.migrate_from_chat_id or m.migrate_to_chat_id)
        if not added and not upgraded:
            return
        keyboard = ikm(
            [
                [
                    ikb(
                        "Add to another group",
                        url=f"https://t.me/{me.username}?startgroup=true",
                        style=enums.ButtonStyle.SUCCESS,
                    ),
                    ikb(
                        "Help",
                        callback_data="gh:help:main",
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                ]
            ]
        )
        await m.reply_text(
            f"<b>{html.escape(config.BOT_NAME)}</b> is ready!\n"
            "Promote me as an admin, then open Help to configure the group.",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception:
        pass
