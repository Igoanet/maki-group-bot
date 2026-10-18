"""Shared helpers for the GroupHelp-style bot.

Button color convention (same as the music bot): DANGER red only for
stop/close/delete, SUCCESS green for enable/confirm actions, PRIMARY blue
for navigation/info/toggles. Kurigram supports enums.ButtonStyle natively.
"""

import html
import re
import random
from urllib.parse import quote
import time
from html.parser import HTMLParser

from pyrogram import enums, types
from pyrogram.errors import BadRequest, UserAdminInvalid

from . import app
from .config import config


_NOTE_CONTROL_RE = re.compile(
    r"\{(preview(?::top)?|nonotif|mediaspoiler|protect|private|noprivate|admin)\}",
    re.IGNORECASE,
)


def note_controls(text: str) -> tuple[str, dict[str, bool]]:
    """Remove Rose note control tags and return Telegram delivery options.

    Controls are deliberately parsed before HTML/template expansion.  They
    are authoring directives, not content, and therefore must never be sent
    to Telegram (or become part of a caption split).
    """
    options = {
        "preview": False,
        "preview_top": False,
        "nonotif": False,
        "mediaspoiler": False,
        "protect": False,
        "private": False,
        "noprivate": False,
        "admin": False,
        "_preview_tag": False,
    }

    def remove(match):
        key = match.group(1).lower()
        if key == "preview:top":
            options["preview_top"] = True
            options["preview"] = True
            options["_preview_tag"] = True
        else:
            options[key] = True
            if key == "preview":
                options["_preview_tag"] = True
        return ""

    return _NOTE_CONTROL_RE.sub(remove, str(text or "")), options


def random_variant(text: str) -> str:
    """Choose one Rose ``%%%`` variant without changing ordinary content."""
    variants = [part.strip() for part in str(text or "").split("%%%")]
    variants = [part for part in variants if part]
    return random.choice(variants) if variants else ""


async def expand_rules_button(
    text: str, m: types.Message, chat_id: int | None = None
) -> str:
    """Expand Rose rules fillings into a safe PM deep-link button."""
    if "{rules" not in text.lower():
        return text
    target_chat = chat_id or getattr(getattr(m, "chat", None), "id", None)
    if not target_chat:
        return text.replace("{rules:same}", "").replace("{rules}", "")
    try:
        me = await app.get_me()
        username = getattr(me, "username", None)
        if not username:
            return text.replace("{rules:same}", "").replace("{rules}", "")
        url = f"https://t.me/{username}?start=rules_{target_chat}"
        return text.replace(
            "{rules:same}", f"[Rules](buttonurl://{url}:same)"
        ).replace("{rules}", f"[Rules](buttonurl://{url})")
    except Exception:
        return text.replace("{rules:same}", "").replace("{rules}", "")

# negative-only admin cache for the watcher hot path (60s). Never used for
# authorization or target protection — see is_admin vs is_admin_soft.
_admin_cache: dict[tuple[int, int], float] = {}
_ADMIN_TTL = 60

# Status glyphs were useful while this bot was being debugged, but made
# routine replies noisy.  This only removes a bot-owned leading marker; it
# is not used for names, IDs, saved notes, rules, welcome templates, or
# quoted message content supplied by a user.
_BOT_STATUS_PREFIXES = (
    "⚠️", "⚠", "🚫", "⏳", "🎨", "✅", "❌", "ℹ️", "ℹ", "🔄",
    "🔒", "🔓", "🛡️", "🛡", "🛑", "🏓", "🧹", "📌", "📢", "📣",
    "🔗", "🏛", "⏱", "🧟", "👑", "⭐️", "⭐", "📋", "🔍", "🧭",
)


def concise_bot_text(text: str) -> str:
    """Drop one decorative status prefix from a bot-authored reply."""
    if not isinstance(text, str):
        return text
    for prefix in _BOT_STATUS_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):].lstrip()
    return text


async def is_admin(chat_id: int, user_id: int) -> bool:
    """LIVE membership check — command authorization and destructive-target
    protection. Never cached: a demotion must revoke rights instantly and a
    promotion must protect the new admin instantly."""
    if user_id == config.OWNER_ID:
        return True
    try:
        member = await app.get_chat_member(chat_id, user_id)
        return member.status in (
            enums.ChatMemberStatus.OWNER,
            enums.ChatMemberStatus.ADMINISTRATOR,
        )
    except Exception:
        return False


async def is_owner(chat_id: int, user_id: int) -> bool:
    """Return whether *user_id* is the configured or Telegram chat owner.

    This is deliberately a live check.  In particular, an administrator,
    sudo user, or anonymous sender must not inherit owner-only powers when a
    membership lookup fails.
    """
    if not user_id:
        return False
    if config.OWNER_ID and user_id == config.OWNER_ID:
        return True
    try:
        member = await app.get_chat_member(chat_id, user_id)
        return member.status == enums.ChatMemberStatus.OWNER
    except Exception:
        return False


async def is_admin_soft(chat_id: int, user_id: int) -> bool:
    """Reply-only watcher skip check with a 60s negative cache (one API call
    per regular member per minute instead of per message). ONLY for
    non-destructive paths (note auto-replies) — NEVER gate a delete,
    mute, ban, captcha or other action on this; those must use is_admin."""
    if user_id == config.OWNER_ID:
        return True
    key = (chat_id, user_id)
    hit = _admin_cache.get(key)
    if hit and time.time() - hit < _ADMIN_TTL:
        return False  # cached negative
    ok = await is_admin(chat_id, user_id)
    if ok:
        _admin_cache.pop(key, None)  # never cache positives
    else:
        _admin_cache[key] = time.time()
    return ok


def admin_cache_clear(chat_id: int, user_id: int) -> None:
    _admin_cache.pop((chat_id, user_id), None)


def admin_cache_clear_chat(chat_id: int) -> None:
    """Drop all cached non-admin results for a chat (/admincache)."""
    for key in [k for k in _admin_cache if k[0] == chat_id]:
        _admin_cache.pop(key, None)


async def has_right(chat_id: int, user_id: int, privilege: str) -> bool:
    """True when this user may personally exercise *privilege* in this chat.

    Button handlers use this where require_right's reply flow does not fit:
    the chat owner always passes, an administrator only with that Telegram
    right, everybody else never.
    """
    if config.OWNER_ID and user_id == config.OWNER_ID:
        return True
    try:
        member = await app.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    if member.status == enums.ChatMemberStatus.OWNER:
        return True
    return member.status == enums.ChatMemberStatus.ADMINISTRATOR and bool(
        getattr(getattr(member, "privileges", None), privilege, False)
    )


def _anon_confirmed(m: types.Message) -> bool:
    """True when the sender added the explicit confirmation token.

    Anonymous admins are real admins but the account behind the mask cannot
    be verified, so the action is never taken on the first try (2026-09-29).
    """
    import re as _re

    text = "%s %s" % (getattr(m, "text", None) or "",
                      getattr(m, "caption", None) or "")
    return bool(_re.search(r"(?:^|\\s)-y(?:\\s|$)", text))


async def require_admin(m: types.Message, privilege: str | None = None,
                        label: str | None = None) -> bool:
    """Reply with a notice and return False when the sender isn't an admin.

    /anonadmin on: anonymous group admins (sender_chat == this chat) count.
    /adminerror off: fail silently instead of replying.

    When *privilege* names a Telegram admin right (e.g. 'can_delete_messages'),
    being an admin is not enough: the caller must personally hold that right,
    so an admin promoted only to pin messages cannot run a ban command. The
    argument is optional so that view-only commands keep the plain gate.
    """
    if privilege:
        return await require_right(m, privilege, label or privilege)
    if m.from_user and await is_admin(m.chat.id, m.from_user.id):
        return True
    from ghelp.database import db  # lazy import — no module cycle
    try:
        cfg = await db.get_chat(m.chat.id)
    except Exception:
        cfg = {}
    sender_chat = getattr(m, "sender_chat", None)
    if cfg.get("anonadmin") and sender_chat and sender_chat.id == m.chat.id:
        # not taken on the first try: the bot asks for -y (2026-09-29)
        if cfg.get("anonadmin_confirm", True) and not _anon_confirmed(m):
            try:
                await m.reply_text(
                    "You are posting as an <b>anonymous admin</b>. To "
                    "confirm, re-send the command ending with "
                    "<code>-y</code>.",
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                pass
            return False
        return True
    if cfg.get("adminerror", True):
        await m.reply_text(concise_bot_text(
            "This command is for group admins only."))
    return False


async def require_owner(m: types.Message) -> bool:
    """Reply in plain text unless the sender is the bot/group owner."""
    if m.from_user and await is_owner(m.chat.id, m.from_user.id):
        return True
    try:
        await m.reply_text(
            concise_bot_text("This command is for the bot owner or group owner only.")
        )
    except Exception:
        pass
    return False


async def require_right(m: types.Message, privilege: str, label: str) -> bool:
    """Like require_admin, plus the caller must PERSONALLY hold this Telegram
    admin right (e.g. 'can_pin_messages'). Bot owner and group owner always
    pass; anonymous admins are refused because their personal rights can't
    be checked."""
    if m.from_user:
        if config.OWNER_ID and m.from_user.id == config.OWNER_ID:
            return True
        try:
            member = await app.get_chat_member(m.chat.id, m.from_user.id)
        except Exception:
            member = None
        if member is None:
            # fail CLOSED: a transient lookup error must not fall through to
            # the weaker admin-only gate and skip the rights check
            try:
                await m.reply_text(
                    concise_bot_text(
                        "Could not verify your admin rights — try again."))
            except Exception:
                pass
            return False
        if member.status == enums.ChatMemberStatus.OWNER:
            return True
        if member.status == enums.ChatMemberStatus.ADMINISTRATOR:
            priv = getattr(member, "privileges", None)
            if getattr(priv, privilege, False):
                return True
            await m.reply_text(
                concise_bot_text(
                    f"You need the <b>{html.escape(label)}</b> admin "
                    "right to use this command."),
                parse_mode=enums.ParseMode.HTML,
            )
            return False
        # plain member: let require_admin produce the admins-only reply
        return await require_admin(m)
    # Anonymous group admin / channel post: there is no personal account to
    # check this right against, so a named right must fail CLOSED here even
    # when /anonadmin is on.  Those callers can act with a visible account.
    try:
        await m.reply_text(
            concise_bot_text(
                f"Anonymous admins cannot use this command: the "
                f"<b>{html.escape(label)}</b> right has to be checked on your "
                "own account. Turn off anonymous posting and try again."),
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        pass
    return False


async def resolve_user(m: types.Message) -> types.User | None:
    """Target user from a reply or an argument (@username / numeric id)."""
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user
    if len(m.command) >= 2:
        arg = m.command[1].lstrip("@")
        try:
            return await app.get_users(int(arg) if arg.isdigit() else arg)
        except Exception:
            return None
    return None


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'


def fill(text: str, m: types.Message, user: types.User | None = None) -> str:
    """Template substitution for welcomes/notes.

    {first} {last} {fullname} {username} {mention} {id} {chat} {chatname}
    """
    u = user or m.from_user
    chat = m.chat
    out = text
    if u:
        # every substituted field is HTML-escaped — profile names are
        # user-controlled and the rendered output is sent as HTML
        first = html.escape(u.first_name or "")
        last = html.escape(u.last_name or "")
        out = out.replace("{first}", first)
        out = out.replace("{last}", last)
        out = out.replace("{fullname}", f"{first} {last}".strip())
        out = out.replace(
            "{username}",
            html.escape(f"@{u.username}")
            if u.username
            else mention(u.id, u.first_name or "user"),
        )
        out = out.replace("{mention}", mention(u.id, u.first_name or "user"))
        out = out.replace("{id}", str(u.id))
    if chat:
        chat_name = html.escape(chat.title or "")
        out = out.replace("{chat}", chat_name)
        out = out.replace("{chatname}", chat_name)
    return out


# --- duration parsing for mutes: "30s", "10m", "2h", "1d" ---------------
_DUR = re.compile(r"^(\d+)\s*([smhd])$", re.I)
_DUR_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(raw: str) -> int | None:
    """Return seconds, or None when unparsable."""
    match = _DUR.match(raw or "")
    if not match:
        return None
    return int(match.group(1)) * _DUR_MULT[match.group(2).lower()]


# --- colored inline keyboards (kurigram ButtonStyle) ---------------------
def ikb(
    text: str,
    callback_data: str | None = None,
    url: str | None = None,
    style: enums.ButtonStyle = enums.ButtonStyle.PRIMARY,
) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(
        text, callback_data=callback_data, url=url, style=style
    )


def ikm(rows: list[list[types.InlineKeyboardButton]]) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(rows)


def close_kb(lang_close: str = "Close") -> types.InlineKeyboardMarkup:
    return ikm([[ikb(lang_close, callback_data="gh:close", style=enums.ButtonStyle.DANGER)]])


# --- permission helpers ----------------------------------------------------
async def bot_can(chat_id: int, privilege: str) -> bool:
    """Check our own admin right, e.g. bot_can(id, 'can_delete_messages')."""
    try:
        me = await app.get_chat_member(chat_id, (await app.get_me()).id)
        return bool(getattr(me.privileges, privilege, False)) if me.privileges else False
    except Exception:
        return False


async def safe_restrict(chat_id: int, user_id: int, perms: types.ChatPermissions) -> bool:
    try:
        result = await app.restrict_chat_member(chat_id, user_id, perms)
        if result is False:
            return False
    except UserAdminInvalid:
        return False  # can't restrict another admin
    except Exception:
        return False
    # Any successful moderation restrict/unrestrict supersedes force-sub's
    # claim on the user's restriction (the gate re-tracks right after its own
    # call, so this centralized hook can never undo the gate's own mute).
    try:
        from ghelp.plugins.forcesub import untrack_fsub
        await untrack_fsub(chat_id, user_id)
    except Exception:
        pass
    return True


# --- Rose-style button markup: [label](buttonurl:link) ---------------------
_BUTTON_URL_RE = re.compile(
    r"\[([^\]\n]{1,80})\]\(\s*buttonurl(?:#(primary|danger|success))?:(?://)?([^\s)]+)\s*\)",
    flags=re.IGNORECASE,
)


def parse_buttons(
    text: str, *, note_url: str | None = None
) -> tuple[str, types.InlineKeyboardMarkup | None]:
    """Extract [label](buttonurl:link) markup (Rose syntax).

    Returns (clean_text, markup|None). Links without a scheme get https://.
    Works in notes and rules (welcome has its own older parser).
    """
    found = _BUTTON_URL_RE.findall(text or "")[:8]
    body = _BUTTON_URL_RE.sub("", text or "")
    # Button syntax is bot-owned markup, but all remaining whitespace belongs
    # to the author.  Only collapse an all-whitespace body so a buttons-only
    # media entry does not turn into an invalid empty text message.
    if not body.strip():
        body = ""
    if not found:
        return body, None
    rows = []
    for label, style_name, url in found:
        same_row = url.lower().endswith(":same")
        if same_row:
            url = url[:-5]
        if url.lower() == "#note" and note_url:
            url = note_url
        elif url.startswith("#") and note_url:
            target = url[1:]
            match = re.match(r"(.+[?&]start=note_-?\d+_)", note_url)
            if target and match:
                url = match.group(1) + quote(target, safe="_-")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        style = {
            "primary": enums.ButtonStyle.PRIMARY,
            "danger": enums.ButtonStyle.DANGER,
            "success": enums.ButtonStyle.SUCCESS,
        }.get((style_name or "primary").lower(), enums.ButtonStyle.PRIMARY)
        button = ikb(label, url=url, style=style)
        if same_row and rows:
            rows[-1].append(button)
        else:
            rows.append([button])
    return body, ikm(rows)


# --- media notes --------------------------------------------------------------
# Keep this list deliberately explicit.  In particular, a voice message and
# a round video are not documents just because both have a file_id.
_MEDIA_ATTRS = (
    "photo",
    "video_note",
    "video",
    "animation",
    "audio",
    "voice",
    "document",
    "sticker",
)
SUPPORTED_MEDIA_TYPES = frozenset(_MEDIA_ATTRS)

# These are message fields which are media-like but cannot be saved by file_id
# and replayed with one of the native send methods below.  This is also useful
# to callers which need to distinguish an empty text message from an
# unsupported message (for example /save).
_UNSUPPORTED_MEDIA_ATTRS = (
    "poll",
    "contact",
    "location",
    "venue",
    "dice",
    "game",
    "story",
)

_MEDIA_SEND_METHODS = {
    "photo": ("reply_photo", "send_photo"),
    "video": ("reply_video", "send_video"),
    "animation": ("reply_animation", "send_animation"),
    "audio": ("reply_audio", "send_audio"),
    "voice": ("reply_voice", "send_voice"),
    "document": ("reply_document", "send_document"),
    "sticker": ("reply_sticker", "send_sticker"),
    "video_note": ("reply_video_note", "send_video_note"),
}

# Telegram counts captions and message text in UTF-16 code units.  Slicing a
# Python string at 1024 characters therefore breaks the limit for astral
# emoji, and slicing arbitrary HTML breaks both tags and entities.
_CAPTION_LIMIT = 1024
_TEXT_LIMIT = 4096
_HTML_TOKEN_RE = re.compile(r"<[^>\n]*>|&(?:#\d+|#x[0-9a-f]+|[a-z][a-z0-9]+);", re.I)
_HTML_TAG_RE = re.compile(r"^<\s*(/?)\s*([a-z][a-z0-9-]*)\b[^>]*>\s*$", re.I)
_HTML_VOID_TAGS = frozenset(("br", "hr", "img"))
_HTML_ALLOWED_TAGS = frozenset((
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "blockquote", "code", "pre", "a", "span", "tg-spoiler", "tg-emoji",
))
_HTML_ALLOWED_ATTRS = {
    "a": frozenset(("href",)),
    "span": frozenset(("class",)),
    "blockquote": frozenset(("expandable",)),
    "code": frozenset(("class",)),
    "tg-emoji": frozenset(("emoji-id",)),
}
# Find entity-looking fragments first, then validate their exact spelling.
# This deliberately also catches malformed numeric forms such as ``&#x;``.
_HTML_ENTITY_RE = re.compile(r"&[^;\n]{1,64};")


class _SavedHTMLValidator(HTMLParser):
    """Validate only the HTML Telegram's HTML parser understands.

    Saved text is authored content, not a template we should rewrite.  If it
    is not a balanced, whitelisted fragment, callers escape the *whole*
    fragment so a literal ``<``/unknown entity cannot make Telegram reject
    the message or cause a parser to discard part of it.
    """

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.invalid = False

    def _check_attrs(self, tag: str, attrs) -> None:
        allowed = _HTML_ALLOWED_ATTRS.get(tag, frozenset())
        names = [name.lower() for name, _ in attrs if name]
        if any(name not in allowed for name in names):
            self.invalid = True
            return
        if tag == "a":
            values = {name.lower(): value for name, value in attrs if name}
            if not values.get("href"):
                self.invalid = True
        elif tag == "span":
            values = {name.lower(): value for name, value in attrs if name}
            if values.get("class", "").lower() != "tg-spoiler":
                self.invalid = True
        elif tag == "blockquote":
            # Telegram accepts both ordinary and ``expandable`` blocks.
            values = {name.lower(): value for name, value in attrs if name}
            expandable = values.get("expandable")
            if expandable not in (None, ""):
                self.invalid = True
        elif tag == "code":
            values = {name.lower(): value for name, value in attrs if name}
            if values and not values.get("class", "").lower().startswith("language-"):
                self.invalid = True
        elif tag == "tg-emoji":
            values = {name.lower(): value for name, value in attrs if name}
            if not values.get("emoji-id", "").isdigit():
                self.invalid = True

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in _HTML_ALLOWED_TAGS:
            self.invalid = True
            return
        self._check_attrs(tag, attrs)
        if tag not in _HTML_VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag not in _HTML_ALLOWED_TAGS:
            self.invalid = True
            return
        self._check_attrs(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag not in _HTML_ALLOWED_TAGS or not self.stack or self.stack[-1] != tag:
            self.invalid = True
            return
        self.stack.pop()

    def handle_data(self, data):
        # An unescaped '<' is never ordinary authored text.
        if "<" in data:
            self.invalid = True

    def handle_entityref(self, name):
        if html.entities.html5.get(name + ";") is None:
            self.invalid = True

    def handle_charref(self, name):
        try:
            number = int(name[1:], 16) if name.lower().startswith("x") else int(name)
            if number > 0x10FFFF or 0xD800 <= number <= 0xDFFF:
                self.invalid = True
        except ValueError:
            self.invalid = True

    def handle_comment(self, data):
        self.invalid = True

    def handle_decl(self, decl):
        self.invalid = True

    def unknown_decl(self, data):
        self.invalid = True

    def valid(self) -> bool:
        return not self.invalid and not self.stack


def _valid_entity(entity: str) -> bool:
    body = entity[1:-1]
    if body.lower().startswith("#x"):
        try:
            value = int(body[2:], 16)
            return value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF
        except ValueError:
            return False
    if body.startswith("#"):
        try:
            value = int(body[1:])
            return value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF
        except ValueError:
            return False
    return html.entities.html5.get(body + ";") is not None


def _validated_html(text: str) -> str:
    """Keep valid Telegram HTML; escape malformed/literal fragments whole."""
    text = "" if text is None else str(text)
    if not text:
        return text
    if any(not _valid_entity(match.group(0))
           for match in _HTML_ENTITY_RE.finditer(text)):
        return html.escape(text, quote=False)
    parser = _SavedHTMLValidator()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        parser.invalid = True
    if parser.valid():
        return text
    return html.escape(text, quote=False)


_MD_FENCE_RE = re.compile(r"```([A-Za-z0-9_+.-]*)[ \t]*\n?(.*?)```", re.S)
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((?!buttonurl(?:#(?:primary|danger|success))?:)([^)\s]+)\)", re.I)


def rose_markdown_to_html(text: str) -> str:
    """Convert the documented Rose markdown while retaining authored HTML.

    This is intentionally a hybrid renderer: Telegram HTML already present in
    saved content is treated as opaque, while only text matched as markdown is
    escaped before its HTML wrapper is emitted.  Unclosed markdown is left
    alone, which is safer than guessing at an author's intended boundaries.
    """
    source = "" if text is None else str(text)
    protected: list[str] = []

    def hold(value: str) -> str:
        protected.append(value)
        return f"\x00{len(protected) - 1}\x00"

    # Fences are handled first, including an optional language class.
    def fence(match):
        language = match.group(1)
        body = html.escape(match.group(2), quote=False)
        attr = f' class="language-{html.escape(language, quote=True)}"' if language else ""
        return hold(f"<pre><code{attr}>{body}</code></pre>")

    source = _MD_FENCE_RE.sub(fence, source)
    # Keep valid authored Telegram tags/entities untouched during conversion.
    token_re = re.compile(
        r"<(?:[^>\n]|\\>)*>|&(?:#\d+|#x[0-9a-f]+|[a-z][a-z0-9]+);", re.I
    )
    source = token_re.sub(lambda m: hold(m.group(0)), source)

    def inline(value: str) -> str:
        # Links are deliberately limited to web/deep-link schemes.
        def link(match):
            url = match.group(2)
            if not url.lower().startswith(("http://", "https://", "tg://")):
                url = "https://" + url
            return f'<a href="{html.escape(url, quote=True)}">' \
                   f"{html.escape(match.group(1), quote=False)}</a>"

        value = _MD_LINK_RE.sub(
            link,
            value,
        )
        # The documented order avoids treating underline as two italics.
        value = re.sub(
            r"(?<!\w)__([^_\n]+?)__(?!\w)",
            lambda m: f"<u>{html.escape(m.group(1), quote=False)}</u>",
            value,
        )
        value = re.sub(
            r"\|\|([^|\n]+?)\|\|",
            lambda m: f"<tg-spoiler>{html.escape(m.group(1), quote=False)}</tg-spoiler>",
            value,
        )
        value = re.sub(
            r"(?<!\w)\*([^*\n]+?)\*(?!\w)",
            lambda m: f"<b>{html.escape(m.group(1), quote=False)}</b>",
            value,
        )
        value = re.sub(
            r"(?<!\w)_([^_\n]+?)_(?!\w)",
            lambda m: f"<i>{html.escape(m.group(1), quote=False)}</i>",
            value,
        )
        value = re.sub(
            r"(?<!\w)~([^~\n]+?)~(?!\w)",
            lambda m: f"<s>{html.escape(m.group(1), quote=False)}</s>",
            value,
        )
        value = re.sub(
            r"`([^`\n]+?)`",
            lambda m: f"<code>{html.escape(m.group(1), quote=False)}</code>",
            value,
        )
        return value

    # Expandable quote syntax and ordinary quote lines.  Their contents still
    # receive inline markdown conversion, but are escaped as part of wrappers.
    lines = source.splitlines(keepends=True)
    rebuilt: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("**>"):
            quote_lines = [line[3:]]
            index += 1
            while index < len(lines):
                item = lines[index]
                quote_lines.append(item[1:] if item.startswith(">") else item)
                index += 1
                if "||" in item:
                    break
            joined = "".join(quote_lines)
            if joined.rstrip().endswith("||"):
                joined = joined.rstrip()[:-2] + joined[len(joined.rstrip()):]
            rebuilt.append(f"<blockquote expandable>{inline(joined)}</blockquote>")
            continue
        if line.startswith(">"):
            rebuilt.append(f"<blockquote>{inline(line[1:])}</blockquote>")
        else:
            rebuilt.append(inline(line))
        index += 1
    result = "".join(rebuilt)
    return re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], result)


def media_of(msg: types.Message) -> tuple[str | None, str | None]:
    """Return the explicit supported media kind and its Telegram file ID.

    ``video_note`` is checked as its own kind and before the ordinary video
    path in case a lightweight test/message object exposes more than one
    convenience field.  Unsupported Telegram message types intentionally
    return ``(None, None)``; use :func:`unsupported_media_of` when the caller
    needs to explain why a message cannot be saved.
    """
    for attr in _MEDIA_ATTRS:
        media = getattr(msg, attr, None)
        if not media:
            continue
        file_id = getattr(media, "file_id", None)
        # Preserve the detected kind even when a malformed/lightweight
        # message object has lost its file ID.  /save can then explain the
        # problem instead of treating a caption as a text-only note.
        return attr, str(file_id) if file_id else None
    return None, None


def unsupported_media_of(msg: types.Message) -> str | None:
    """Return the first unsupported media field present on *msg*."""
    for attr in _UNSUPPORTED_MEDIA_ATTRS:
        if getattr(msg, attr, None):
            return attr
    return None


def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def _html_tag(token: str) -> tuple[str, str] | None:
    match = _HTML_TAG_RE.match(token)
    if not match:
        return None
    return ("close" if match.group(1) else "open", match.group(2).lower())


def _html_visible_len(token: str) -> int:
    """Visible UTF-16 length of one token (tags themselves count as zero)."""
    if _html_tag(token):
        return 0
    return _utf16_len(html.unescape(token))


def _safe_html_parts(text: str) -> list[str]:
    """Tokenize HTML without ever cutting an entity or a tag.

    The saved text is authored group content, so this function does not
    rewrite it.  It merely keeps Telegram's markup units together while the
    text is split for a caption/message limit.
    """
    parts: list[str] = []
    position = 0
    for match in _HTML_TOKEN_RE.finditer(text):
        if match.start() > position:
            parts.append(text[position:match.start()])
        parts.append(match.group(0))
        position = match.end()
    if position < len(text):
        parts.append(text[position:])
    return parts or [text]


def _take_visible(text: str, room: int) -> tuple[str, str, int]:
    """Take a whole-codepoint prefix fitting *room* UTF-16 units."""
    if not text:
        return "", "", 0
    used = 0
    index = 0
    for index, char in enumerate(text):
        width = _utf16_len(char)
        if used and used + width > room:
            return text[:index], text[index:], used
        if not used and width > room:
            # This only matters for a caller using a one-unit test limit.  A
            # Telegram limit is much larger, but never loop on an astral char.
            return char, text[index + 1:], width
        used += width
    return text, "", used


def _split_html(text: str, limit: int) -> list[str]:
    """Split authored HTML without cutting tags, entities, or code points."""
    text = _validated_html(text)
    if not text:
        return []
    # Fast path avoids changing ordinary text and is also easier to inspect in
    # payload-level tests.
    if _utf16_len(text) <= limit and not _HTML_TOKEN_RE.search(text):
        return [text]

    chunks: list[str] = []
    current = ""
    stack: list[str] = []
    visible = 0

    def reopen() -> str:
        return "".join(stack)

    def close_current() -> None:
        nonlocal current, visible
        if not current:
            return
        closing = []
        for opening in reversed(stack):
            parsed = _html_tag(opening)
            if parsed and parsed[1] not in _HTML_VOID_TAGS:
                closing.append(f"</{parsed[1]}>")
        chunks.append(current + "".join(closing))
        current = ""
        visible = 0

    for token in _safe_html_parts(text):
        parsed = _html_tag(token)
        if parsed:
            direction, name = parsed
            if direction == "open":
                current += token
                if name not in _HTML_VOID_TAGS and not token.rstrip().endswith("/>"):
                    stack.append(token)
            else:
                current += token
                for index in range(len(stack) - 1, -1, -1):
                    item = _html_tag(stack[index])
                    if item and item[1] == name:
                        del stack[index:]
                        break
            continue

        # A named/numeric entity is a single visible unit, even if its source
        # representation is several ASCII characters.
        atoms = _HTML_TOKEN_RE.findall(token)
        if atoms and "".join(atoms) == token:
            atom_list = atoms
        else:
            atom_list = [token]
        for atom in atom_list:
            if _HTML_TOKEN_RE.fullmatch(atom):
                atom_len = _html_visible_len(atom)
                if visible and visible + atom_len > limit:
                    close_current()
                    current = reopen()
                current += atom
                visible += atom_len
                continue
            rest = atom
            while rest:
                room = limit - visible
                piece, tail, used = _take_visible(rest, max(room, 1))
                if visible and used > room:
                    close_current()
                    current = reopen()
                    continue
                current += piece
                visible += used
                rest = tail
                if rest:
                    close_current()
                    current = reopen()
    close_current()
    return chunks or [text]


def _saved_failure_text(kind: str | None = None) -> str:
    if kind == "unsupported":
        return (
            "That saved message type is not supported. Use text, photo, "
            "video, animation, audio, voice, document, sticker, or video note."
        )
    if kind:
        return f"Could not send the saved {kind}. Ask an admin to save it again."
    return "Could not send the saved note. Try again."


async def _send_saved_error(target, message: str, *, chat_id: int | None = None):
    """Send one short failure notice without retrying the media operation."""
    try:
        if chat_id is None:
            return await target.reply_text(message)
        return await target.send_message(chat_id, message)
    except Exception:
        return None


async def _call_saved_text(
    target,
    text: str,
    markup,
    *,
    chat_id: int | None = None,
    sent_messages: list | None = None,
    controls: dict[str, bool] | None = None,
):
    chunks = _split_html(text, _TEXT_LIMIT)
    if not chunks:
        return None
    sent = None
    for index, chunk in enumerate(chunks):
        kwargs = {
            "parse_mode": enums.ParseMode.HTML,
            "reply_markup": markup if index == len(chunks) - 1 else None,
        }
        controls = controls or {}
        if controls.get("nonotif"):
            kwargs["disable_notification"] = True
        if controls.get("protect"):
            kwargs["protect_content"] = True
        if controls.get("preview"):
            kwargs["link_preview_options"] = types.LinkPreviewOptions(
                is_disabled=False,
                show_above_text=bool(controls.get("preview_top")),
            )
        elif controls.get("preview") is False and controls.get("_preview_tag"):
            kwargs["link_preview_options"] = types.LinkPreviewOptions(
                is_disabled=True
            )
        try:
            if chat_id is None:
                sent = await target.reply_text(chunk, **kwargs)
            else:
                sent = await target.send_message(chat_id, chunk, **kwargs)
            if sent_messages is not None:
                sent_messages.append(sent)
        except BadRequest:
            # A confirmed parser rejection can be retried once as literal,
            # escaped text.  Unknown results (timeouts/network errors) never
            # retry: a second request could duplicate a successful message.
            fallback = html.escape(html.unescape(chunk), quote=False)
            try:
                if chat_id is None:
                    sent = await target.reply_text(fallback, **kwargs)
                else:
                    sent = await target.send_message(chat_id, fallback, **kwargs)
                if sent_messages is not None:
                    sent_messages.append(sent)
            except Exception:
                await _send_saved_error(
                    target,
                    "Could not send the saved text. Try again.",
                    chat_id=chat_id,
                )
                return None
        except Exception:
            await _send_saved_error(
                target,
                "Could not send the saved text. Try again.",
                chat_id=chat_id,
            )
            return None
    return sent


async def send_saved(
    reply_to: types.Message,
    entry,
    m: types.Message,
    *,
    chat_id: int | None = None,
    sent_messages: list | None = None,
    note_url: str | None = None,
    noformat: bool = False,
):
    """Deliver a saved note or media entry without changing its type.

    ``chat_id`` is used by welcomes, whose target is the Client rather than a
    reply Message.  The old three-argument form remains the notes API.
    """
    is_media_entry = isinstance(entry, dict)
    if is_media_entry:
        raw_kind = entry.get("type")
        # Notes written by the first GroupHelp version omitted ``type`` for
        # document file IDs.  Keep that legacy shape readable, but never
        # treat a present unknown type as a document.
        kind = (
            "document"
            if raw_kind is None or str(raw_kind).strip() == ""
            else str(raw_kind).lower()
        )
        if kind not in SUPPORTED_MEDIA_TYPES:
            await _send_saved_error(
                reply_to,
                _saved_failure_text("unsupported"),
                chat_id=chat_id,
            )
            return None
        file_id = entry.get("file_id")
        if not file_id:
            await _send_saved_error(
                reply_to,
                _saved_failure_text(kind),
                chat_id=chat_id,
            )
            return None
        body = entry.get("text")
        body = "" if body is None else str(body)
        if noformat:
            controls = {}
            caption, markup = body, None
        else:
            body = random_variant(body)
            body, controls = note_controls(body)
            rendered = fill(body, m)
            rendered = await expand_rules_button(rendered, m, chat_id)
            caption, markup = parse_buttons(rendered, note_url=note_url)
            caption = rose_markdown_to_html(caption)
        file_id = str(file_id)
        reply_name, send_name = _MEDIA_SEND_METHODS[kind]
        method_name = send_name if chat_id is not None else reply_name
        sender = getattr(reply_to, method_name, None)
        if sender is None:
            await _send_saved_error(
                reply_to,
                _saved_failure_text(kind),
                chat_id=chat_id,
            )
            return None

        # Stickers and round videos have no caption field in Telegram.  Send
        # them natively, then put the authored text (and its buttons) in one
        # or more ordinary messages.
        if kind in ("sticker", "video_note"):
            try:
                media_kwargs = {}
                if controls.get("nonotif"):
                    media_kwargs["disable_notification"] = True
                if controls.get("protect"):
                    media_kwargs["protect_content"] = True
                if controls.get("mediaspoiler") and kind == "video_note":
                    media_kwargs["has_spoiler"] = True
                if chat_id is None:
                    if markup and not caption:
                        sent = await sender(file_id, reply_markup=markup, **media_kwargs)
                    else:
                        sent = await sender(file_id, **media_kwargs)
                else:
                    if markup and not caption:
                        sent = await sender(
                            chat_id, file_id, reply_markup=markup, **media_kwargs
                        )
                    else:
                        sent = await sender(chat_id, file_id, **media_kwargs)
            except Exception:
                await _send_saved_error(
                    reply_to,
                    _saved_failure_text(kind),
                    chat_id=chat_id,
                )
                return None
            if sent_messages is not None:
                sent_messages.append(sent)
            if caption:
                followup = await _call_saved_text(
                    reply_to,
                    caption,
                    markup,
                    chat_id=chat_id,
                    sent_messages=sent_messages,
                    controls=controls,
                )
                if followup is None:
                    return None
            return sent

        # Telegram permits at most 1024 UTF-16 units in a media caption.  Any
        # remainder is sent as ordinary text, never silently discarded.
        caption_chunks = _split_html(caption, _CAPTION_LIMIT)
        first_caption = caption_chunks[0] if caption_chunks else ""
        kwargs = {
            "caption": first_caption or None,
            "parse_mode": enums.ParseMode.HTML,
            "reply_markup": markup,
        }
        if controls.get("nonotif"):
            kwargs["disable_notification"] = True
        if controls.get("protect"):
            kwargs["protect_content"] = True
        if controls.get("mediaspoiler"):
            kwargs["has_spoiler"] = True
        try:
            if chat_id is None:
                sent = await sender(file_id, **kwargs)
            else:
                sent = await sender(chat_id, file_id, **kwargs)
        except BadRequest:
            # Only a confirmed BadRequest permits one safe media retry.  The
            # fallback treats the complete authored caption as literal text,
            # then losslessly sends any remainder as ordinary text.
            fallback_chunks = _split_html(
                html.escape(caption, quote=False), _CAPTION_LIMIT
            )
            fallback_kwargs = {
                "caption": fallback_chunks[0] if fallback_chunks else None,
                "parse_mode": enums.ParseMode.HTML,
                "reply_markup": markup,
            }
            try:
                if chat_id is None:
                    sent = await sender(file_id, **fallback_kwargs)
                else:
                    sent = await sender(chat_id, file_id, **fallback_kwargs)
            except Exception:
                await _send_saved_error(
                    reply_to,
                    _saved_failure_text(kind),
                    chat_id=chat_id,
                )
                return None
            if sent_messages is not None:
                sent_messages.append(sent)
            for remainder in fallback_chunks[1:]:
                followup = await _call_saved_text(
                    reply_to,
                    remainder,
                    None,
                    chat_id=chat_id,
                    sent_messages=sent_messages,
                    controls=controls,
                )
                if followup is None:
                    return None
            return sent
        except Exception:
            # Do not retry a media send after a timeout/unknown result: a
            # second upload can duplicate the saved media.
            await _send_saved_error(
                reply_to,
                _saved_failure_text(kind),
                chat_id=chat_id,
            )
            return None
        if sent_messages is not None:
            sent_messages.append(sent)
        for remainder in caption_chunks[1:]:
            followup = await _call_saved_text(
                reply_to,
                remainder,
                None,
                chat_id=chat_id,
                sent_messages=sent_messages,
                controls=controls,
            )
            if followup is None:
                return None
        return sent

    if noformat:
        text, controls, markup = str(entry), {}, None
    else:
        text = random_variant(str(entry))
        text, controls = note_controls(text)
        text = fill(text, m)
        text = await expand_rules_button(text, m, chat_id)
        text, markup = parse_buttons(text, note_url=note_url)
        text = rose_markdown_to_html(text)
    return await _call_saved_text(
        reply_to, text, markup, chat_id=chat_id, sent_messages=sent_messages,
        controls=controls,
    )
