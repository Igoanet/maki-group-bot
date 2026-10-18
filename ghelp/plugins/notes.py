"""Saved notes and group rules."""

import html

from pyrogram import enums, filters, types
from pyrogram.errors import BadRequest

from ghelp import app
from ghelp.database import db
from ghelp.helpers import (
    has_right,
    close_kb,
    fill,
    is_admin_soft,
    media_of,
    parse_buttons,
    require_admin,
    send_saved,
    unsupported_media_of,
    is_admin,
    ikb,
    ikm,
    note_controls,
    rose_markdown_to_html,
)


async def _send_raw(
    m: types.Message,
    text: str,
    *,
    reply_markup: types.InlineKeyboardMarkup | None = None,
) -> None:
    """Send admin-authored HTML, falling back to escaped plain text."""
    try:
        await m.reply_text(
            text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except BadRequest:
        await m.reply_text(
            html.escape(text),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=reply_markup,
        )


@app.on_message(filters.group & filters.command("save", prefixes=["/", "?", "!", "."]))
async def save_note(_: object, m: types.Message) -> None:
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    try:
        source = m.text or m.caption or ""
        parts = source.split(maxsplit=1)
        arguments = parts[1].strip() if len(parts) > 1 else ""

        if m.reply_to_message:
            name = arguments
            kind, file_id = media_of(m.reply_to_message)
            unsupported = unsupported_media_of(m.reply_to_message)
            body = m.reply_to_message.text or m.reply_to_message.caption
            if body is not None:
                body = getattr(body, "html", None) or str(body)
            if unsupported:
                await m.reply_text(
                    "That message type is not supported. Save text, photo, "
                    "video, animation, audio, voice, document, sticker, "
                    "or video note.",
                )
                return
            if kind and not file_id:
                await m.reply_text(
                    "That media has no reusable file ID. Save it again as "
                    "text, photo, video, animation, audio, voice, document, "
                    "sticker, or video note.",
                )
                return
            if not name or (not body and not file_id):
                await m.reply_text(
                    "Reply to a message (text, photo, video, sticker…) "
                    "with <code>/save name</code>.",
                    parse_mode=enums.ParseMode.HTML,
                )
                return
            note = ({"type": kind, "file_id": file_id, "text": body or ""}
                    if file_id else body)
        else:
            split_arguments = arguments.split(maxsplit=1)
            if len(split_arguments) < 2:
                await m.reply_text(
                    "Use <code>/save name note text</code>.",
                    parse_mode=enums.ParseMode.HTML,
                )
                return
            name, note = split_arguments

        name = name.strip().lower()
        if not name:
            await m.reply_text(
                "The note name cannot be empty.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        chat = await db.get_chat(m.chat.id)
        notes = dict(chat.get("notes") or {})
        notes[name] = note
        await db.update_chat(m.chat.id, notes=notes)
        await m.reply_text(
            f"Note <code>#{html.escape(name)}</code> saved.",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await m.reply_text(
            "Could not save that note.",
            parse_mode=enums.ParseMode.HTML,
        )


@app.on_message(filters.group & filters.command("get", prefixes=["/", "?", "!", "."]))
async def get_note(_: object, m: types.Message) -> None:
    try:
        if len(m.command) < 2:
            await m.reply_text(
                "Use <code>/get name</code>.",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        name = m.command[1].lower()
        noformat = any(arg.lower() == "noformat" for arg in m.command[2:])
        chat = await db.get_chat(m.chat.id)
        notes = chat.get("notes") or {}
        if name not in notes:
            await m.reply_text(
                f"Note <code>#{html.escape(name)}</code> was not found.",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        entry = notes[name]
        if noformat and isinstance(entry, dict):
            entry = dict(entry)
        clean, controls = note_controls(
            str(entry.get("text") if isinstance(entry, dict) else entry)
        )
        if controls.get("admin") and not (
            m.from_user and await is_admin(m.chat.id, m.from_user.id)
        ):
            return
        if controls.get("private") or (
            chat.get("privatenotes") and not controls.get("noprivate")
        ):
            me = await app.get_me()
            await m.reply_text(
                "This note is read privately — tap below.",
                reply_markup=ikm([[
                    ikb(
                        f"#{name}",
                        url=f"https://t.me/{me.username}?start=note_{m.chat.id}_{name}",
                        style=enums.ButtonStyle.PRIMARY,
                    )
                ]]),
            )
            return
        me = await app.get_me()
        await send_saved(
            m, entry, m,
            note_url=f"https://t.me/{me.username}?start=note_{m.chat.id}_{name}",
            noformat=noformat,
        )
    except Exception:
        await m.reply_text(
            "Could not load that note.",
            parse_mode=enums.ParseMode.HTML,
        )


@app.on_message(filters.group & filters.command("notes", prefixes=["/", "?", "!", "."]))
async def list_notes(_: object, m: types.Message) -> None:
    try:
        chat = await db.get_chat(m.chat.id)
        notes = chat.get("notes") or {}
        if not notes:
            text = "No notes are saved."
        else:
            names = sorted((str(name) for name in notes), key=str.casefold)
            text = "<b>Saved notes</b>\n\n" + "\n".join(
                f"<code>#{html.escape(name)}</code>" for name in names
            )
        await m.reply_text(
            text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=close_kb(),
        )
    except Exception:
        await m.reply_text(
            "Could not load the notes.",
            parse_mode=enums.ParseMode.HTML,
        )


@app.on_message(filters.group & filters.command("saved", prefixes=["/", "?", "!", "."]))
async def saved_alias(_: object, m: types.Message) -> None:
    """Rose-compatible alias for /notes."""
    await list_notes(_, m)


@app.on_message(filters.group & filters.command("markdownhelp", prefixes=["/", "?", "!", "."]))
async def markdown_help(_: object, m: types.Message) -> None:
    await m.reply_text(
        "<b>Note formatting</b>\n"
        "Markdown: <code>`code`</code> <i>_italic_</i> <b>*bold*</b> "
        "<u>__underline__</u> <s>~strike~</s> "
        "<tg-spoiler>||spoiler||</tg-spoiler>.\n"
        "Blocks: <code>```lang\\ncode```</code>, <code>&gt; quote</code>, "
        "<code>**&gt; expandable||</code>.\n"
        "Links: <code>[label](https://example.com)</code>.\n"
        "Buttons: <code>[label](buttonurl://https://example.com)</code> "
        "or <code>buttonurl#danger</code>.\n"
        "Controls: <code>{preview}</code>, <code>{preview:top}</code>, "
        "<code>{nonotif}</code>, <code>{protect}</code>, "
        "<code>{mediaspoiler}</code>, <code>{private}</code>, "
        "<code>{noprivate}</code>, <code>{admin}</code>.",
        parse_mode=enums.ParseMode.HTML,
    )


@app.on_message(filters.group & filters.command("clear", prefixes=["/", "?", "!", "."]))
async def clear_note(_: object, m: types.Message) -> None:
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    try:
        if len(m.command) < 2:
            await m.reply_text(
                "Use <code>/clear name</code>.",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        name = m.command[1].lower()
        chat = await db.get_chat(m.chat.id)
        notes = dict(chat.get("notes") or {})
        if name not in notes:
            await m.reply_text(
                f"Note <code>#{html.escape(name)}</code> was not found.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        del notes[name]
        await db.update_chat(m.chat.id, notes=notes)
        await m.reply_text(
            f"Note <code>#{html.escape(name)}</code> removed.",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await m.reply_text(
            "Could not remove that note.",
            parse_mode=enums.ParseMode.HTML,
        )


@app.on_message(filters.group & filters.command("clearall", prefixes=["/", "?", "!", "."]))
async def clear_all_notes(_: object, m: types.Message) -> None:
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    await m.reply_text(
        "Remove all saved notes? This cannot be undone.",
        reply_markup=ikm([[
            ikb("Confirm", callback_data="gh:clearall:yes",
                style=enums.ButtonStyle.SUCCESS),
            ikb("Cancel", callback_data="gh:clearall:no",
                style=enums.ButtonStyle.DANGER),
        ]]),
    )


@app.on_callback_query(filters.regex(r"^gh:clearall:(yes|no)$"))
async def clear_all_callback(_: object, query: types.CallbackQuery) -> None:
    if not query.message or not query.from_user:
        return
    if not await has_right(query.message.chat.id, query.from_user.id,
                           "can_change_info"):
        await query.answer("You need the Change group info admin right.",
                           show_alert=True)
        return
    action = (
        query.matches[0].group(1)
        if getattr(query, "matches", None)
        else str(getattr(query, "data", "")).rsplit(":", 1)[-1]
    )
    if action == "no":
        await query.message.edit_text("Saved notes were kept.")
        await query.answer()
        return
    await db.update_chat(query.message.chat.id, notes={})
    await query.message.edit_text("All saved notes were removed.")
    await query.answer()


@app.on_message(filters.group & filters.command("setrules", prefixes=["/", "?", "!", "."]))
async def set_rules(_: object, m: types.Message) -> None:
    if not await require_admin(m, "can_change_info", "Change group info"):
        return
    try:
        source = m.text or m.caption or ""
        parts = source.split(maxsplit=1)
        rules = parts[1].strip() if len(parts) > 1 else ""
        if not rules:
            await m.reply_text(
                "Use <code>/setrules rules text</code>.",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        await db.update_chat(m.chat.id, rules=rules)
        await m.reply_text(
            "Group rules saved.",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        await m.reply_text(
            "Could not save the rules.",
            parse_mode=enums.ParseMode.HTML,
        )


@app.on_message(filters.group & filters.command("rules", prefixes=["/", "?", "!", "."]))
async def show_rules(_: object, m: types.Message) -> None:
    try:
        chat = await db.get_chat(m.chat.id)
        rules = str(chat.get("rules") or "")
        if not rules:
            await m.reply_text(
                "No rules set.",
                parse_mode=enums.ParseMode.HTML,
                reply_markup=close_kb(),
            )
            return
        if chat.get("privaterules"):
            # /privaterules on — read rules via PM deep link (Rose-style)
            from ghelp.helpers import ikb, ikm
            me = await app.get_me()
            label = str(chat.get("rules_btn") or "Rules")
            await m.reply_text(
                "Group rules are read privately — tap below.",
                reply_markup=ikm([[ikb(
                    label,
                    url=f"https://t.me/{me.username}?start=rules_{m.chat.id}",
                    style=enums.ButtonStyle.PRIMARY,
                )]]),
            )
            return
        # Rose's rules tags are button shorthands.  They intentionally point
        # at the same safe deep-link used by /privaterules.
        me = await app.get_me()
        rules_url = f"https://t.me/{me.username}?start=rules_{m.chat.id}"
        rules = rules.replace(
            "{rules:same}", f"[{html.escape(str(chat.get('rules_btn') or 'Rules'))}]"
            f"(buttonurl://{rules_url}:same)"
        ).replace(
            "{rules}", f"[{html.escape(str(chat.get('rules_btn') or 'Rules'))}]"
            f"(buttonurl://{rules_url})"
        )
        body, btn_markup = parse_buttons(fill(rules, m))
        body = rose_markdown_to_html(body)
        await _send_raw(m, body, reply_markup=btn_markup or close_kb())
    except Exception:
        await m.reply_text(
            "Could not load the rules.",
            parse_mode=enums.ParseMode.HTML,
        )


@app.on_message(filters.group & ~filters.service, group=3)
async def note_watcher(_: object, m: types.Message) -> None:
    if not m.from_user or m.from_user.is_self:
        return
    try:
        if await is_admin_soft(m.chat.id, m.from_user.id):
            return
        content = m.text or m.caption
        if not content or not content.startswith("#"):
            return
        name = content[1:].split(maxsplit=1)[0].lower() if content[1:] else ""
        if not name:
            return

        chat = await db.get_chat(m.chat.id)
        notes = chat.get("notes") or {}
        if name in notes:
            raw = notes[name].get("text", "") if isinstance(notes[name], dict) else notes[name]
            _, controls = note_controls(str(raw))
            if controls.get("admin") and not await is_admin(m.chat.id, m.from_user.id):
                return
            private = controls.get("private") or (
                chat.get("privatenotes") and not controls.get("noprivate")
            )
            if private:
                # /privatenotes on — deep link instead of an in-group reply
                from ghelp.helpers import ikb, ikm
                me = await app.get_me()
                await m.reply_text(
                    f"Note <code>#{html.escape(name)}</code> — tap to read privately.",
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=ikm([[ikb(
                        f"#{name}",
                        url=f"https://t.me/{me.username}?start=note_{m.chat.id}_{name}",
                        style=enums.ButtonStyle.PRIMARY,
                    )]]),
                )
                return
            me = await app.get_me()
            await send_saved(
                m, notes[name], m,
                note_url=f"https://t.me/{me.username}?start=note_{m.chat.id}_{name}",
            )
    except Exception:
        return