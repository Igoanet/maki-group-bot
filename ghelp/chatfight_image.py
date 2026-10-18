"""Offline Pillow renderer for the Chat Fight leaderboard.

The renderer intentionally knows nothing about Telegram and never resolves
avatars or usernames.  The request handler supplies bounded in-memory avatar
bytes when available; offline fixtures can do the same, while private users
get an explicit initials fallback.  The public synchronous function is useful
for image tests; the async wrapper is what the plugin uses in the event loop.
"""

from __future__ import annotations

import asyncio
import io
import unicodedata
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont


WIDTH = 2160
# Keep the existing source portrait size in the landscape layout.
AVATAR_SIZE = 82
ROW_HEIGHT = 92  # 82px portraits plus a clear 10px gap between rows
BAR_HEIGHT = 24
TOP_ROWS = 10
DEFAULT_TITLE_WIDTH = 35  # percent of the inner panel's width
# A nocturnal charcoal-and-navy palette: the card should feel like a deliberate
# object in Telegram's light image viewer, rather than a pale dashboard.
_BG = "#090d14"
_CARD = "#111923"
_TEXT = "#f4f5f7"
_MUTED = "#aebdca"
_ACCENT = "#557fae"
_ACCENT_END = "#87a9d2"
_BORDER = "#506b86"
_DEEP_BLUE = "#294661"
_FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
_FALLBACK_FONT_FILES = (
    "Roboto-500.ttf",
    "NotoSans-VF.ttf",
    "NotoSansDevanagari-VF.ttf",
    "NotoSansArabic-VF.ttf",
    "NotoSansBengali-VF.ttf",
    "NotoSansTamil-VF.ttf",
    "NotoSansTelugu-VF.ttf",
    "NotoSansKannada-VF.ttf",
    "NotoSansMalayalam-VF.ttf",
    "NotoSansGujarati-VF.ttf",
    "NotoSansGurmukhi-VF.ttf",
    "NotoSansOriya-VF.ttf",
    "NotoSansThai-VF.ttf",
    "NotoSansHebrew-VF.ttf",
    "NotoSansSinhala-VF.ttf",
    "NotoSansMyanmar-VF.ttf",
    "NotoSansKhmer-VF.ttf",
    "NotoSansCJKsc-Regular.otf",
    "NotoSansSymbols2-Regular.ttf",
    "NotoSansMath-Regular.ttf",
)
_EMOJI_FONT_FILE = "NotoColorEmoji.ttf"
_EMOJI_FONT_SIZE = 109
_TWEMOJI_ARCHIVE = "Twemoji-72x72.zip"
_REMOVED_FORMAT_CONTROLS = {
    "\u200b", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2060", "\u2066", "\u2067", "\u2068", "\u2069", "\ufeff",
}


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load the bundled font, failing loudly when packaging is broken."""

    filename = "Roboto-500.ttf" if bold else "Roboto-400.ttf"
    path = Path(__file__).resolve().parents[1] / "assets" / "fonts" / filename
    if not path.is_file():
        raise RuntimeError(f"Chat Fight font is missing: {path}")
    return ImageFont.truetype(str(path), size)


@lru_cache(maxsize=16)
def _name_fonts(size: int) -> tuple[ImageFont.FreeTypeFont, ...]:
    fonts = []
    for filename in _FALLBACK_FONT_FILES:
        path = _FONT_DIR / filename
        if not path.is_file():
            raise RuntimeError(f"Chat Fight fallback font is missing: {path}")
        fonts.append(ImageFont.truetype(str(path), size))
    return tuple(fonts)


@lru_cache(maxsize=1)
def _emoji_font() -> ImageFont.FreeTypeFont:
    path = _FONT_DIR / _EMOJI_FONT_FILE
    if not path.is_file():
        raise RuntimeError(f"Chat Fight emoji font is missing: {path}")
    return ImageFont.truetype(str(path), _EMOJI_FONT_SIZE)


@lru_cache(maxsize=512)
def _twemoji_png(value: str) -> bytes | None:
    archive = _FONT_DIR / _TWEMOJI_ARCHIVE
    if not archive.is_file():
        raise RuntimeError(f"Chat Fight emoji archive is missing: {archive}")
    codepoints = [f"{ord(char):x}" for char in value]
    candidates = (
        "-".join(codepoints) + ".png",
        "-".join(code for code in codepoints if code != "fe0f") + ".png",
    )
    with zipfile.ZipFile(archive) as assets:
        names = set(assets.namelist())
        for filename in candidates:
            if filename in names:
                return assets.read(filename)
    return None


def _is_emoji_char(char: str) -> bool:
    code = ord(char)
    return (
        0x1F000 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27FF
        or 0x1F1E6 <= code <= 0x1F1FF
    )


def _clusters(value: str) -> list[str]:
    """Keep combining marks, skin tones, flags and ZWJ emoji together."""
    result: list[str] = []
    join_next = False
    regional_count = 0
    for char in value:
        code = ord(char)
        combining = unicodedata.category(char) in {"Mn", "Mc", "Me"}
        modifier = 0x1F3FB <= code <= 0x1F3FF
        variation = 0xFE00 <= code <= 0xFE0F
        regional = 0x1F1E6 <= code <= 0x1F1FF
        previous_virama = bool(
            result
            and "VIRAMA" in unicodedata.name(result[-1][-1], "")
        )
        if result and (
            combining
            or modifier
            or variation
            or char in {"\u200c", "\u200d"}
            or join_next
            or previous_virama
        ):
            result[-1] += char
        elif result and regional and regional_count == 1:
            result[-1] += char
        else:
            result.append(char)
        join_next = char == "\u200d"
        regional_count = regional_count + 1 if regional else 0
        if regional_count >= 2:
            regional_count = 0
    return result


def _script_font_file(cluster: str) -> str | None:
    ranges = (
        (0x0590, 0x05FF, "NotoSansHebrew-VF.ttf"),
        (0x0600, 0x08FF, "NotoSansArabic-VF.ttf"),
        (0x0900, 0x097F, "NotoSansDevanagari-VF.ttf"),
        (0x0980, 0x09FF, "NotoSansBengali-VF.ttf"),
        (0x0A00, 0x0A7F, "NotoSansGurmukhi-VF.ttf"),
        (0x0A80, 0x0AFF, "NotoSansGujarati-VF.ttf"),
        (0x0B00, 0x0B7F, "NotoSansOriya-VF.ttf"),
        (0x0B80, 0x0BFF, "NotoSansTamil-VF.ttf"),
        (0x0C00, 0x0C7F, "NotoSansTelugu-VF.ttf"),
        (0x0C80, 0x0CFF, "NotoSansKannada-VF.ttf"),
        (0x0D00, 0x0D7F, "NotoSansMalayalam-VF.ttf"),
        (0x0D80, 0x0DFF, "NotoSansSinhala-VF.ttf"),
        (0x0E00, 0x0E7F, "NotoSansThai-VF.ttf"),
        (0x1000, 0x109F, "NotoSansMyanmar-VF.ttf"),
        (0x1780, 0x17FF, "NotoSansKhmer-VF.ttf"),
        (0x1D400, 0x1D7FF, "NotoSansMath-Regular.ttf"),
    )
    for char in cluster:
        code = ord(char)
        for start, end, filename in ranges:
            if start <= code <= end:
                return filename
        if (
            0x2E80 <= code <= 0x9FFF
            or 0x3040 <= code <= 0x30FF
            or 0xAC00 <= code <= 0xD7AF
        ):
            return "NotoSansCJKsc-Regular.otf"
    return None


def _font_supports(font: ImageFont.FreeTypeFont, cluster: str) -> bool:
    missing = bytes(font.getmask("\U0010ffff"))
    visible = [
        char for char in cluster
        if not unicodedata.combining(char)
        and char not in {"\u200c", "\u200d", "\ufe0e", "\ufe0f"}
    ]
    return bool(visible) and all(bytes(font.getmask(char)) != missing for char in visible)


def _cluster_font(
    cluster: str,
    fonts: tuple[ImageFont.FreeTypeFont, ...],
) -> ImageFont.FreeTypeFont | None:
    if any(_is_emoji_char(char) for char in cluster):
        return None
    preferred = _script_font_file(cluster)
    if preferred is not None:
        for font in fonts:
            if Path(font.path).name == preferred:
                return font
    for font in fonts:
        if _font_supports(font, cluster):
            return font
    return fonts[-1]


def _name_runs(value: str, size: int) -> list[tuple[str, ImageFont.FreeTypeFont | None]]:
    fonts = _name_fonts(size)
    runs: list[tuple[str, ImageFont.FreeTypeFont | None]] = []
    for cluster in _clusters(value):
        font = _cluster_font(cluster, fonts)
        if runs and runs[-1][1] is font:
            runs[-1] = (runs[-1][0] + cluster, font)
        else:
            runs.append((cluster, font))
    return runs


def _emoji_width(value: str, size: int) -> float:
    if _twemoji_png(value) is not None:
        return float(size + 5)
    bbox = _emoji_font().getbbox(value)
    width = max(1, bbox[2] - bbox[0])
    height = max(1, bbox[3] - bbox[1])
    return width * ((size + 5) / height)


def _name_width(value: str, size: int) -> float:
    width = 0.0
    for text, font in _name_runs(value, size):
        width += _emoji_width(text, size) if font is None else font.getlength(text)
    return width


def _clip_name(value: Any, size: int, max_width: int) -> str:
    text = _display_name(value)
    if _name_width(text, size) <= max_width:
        return text
    clusters = _clusters(text)
    ellipsis = "…"
    while clusters and _name_width("".join(clusters).rstrip() + ellipsis, size) > max_width:
        clusters.pop()
    clipped = "".join(clusters).rstrip()
    return clipped + ellipsis if clipped else ellipsis


def _draw_emoji(
    image: Image.Image,
    value: str,
    left: int,
    center_y: int,
    size: int,
) -> int:
    target_h = size + 5
    twemoji = _twemoji_png(value)
    if twemoji is not None:
        with Image.open(io.BytesIO(twemoji)) as source:
            layer = source.convert("RGBA").resize(
                (target_h, target_h),
                Image.Resampling.LANCZOS,
            )
        image.paste(layer, (left, round(center_y - target_h / 2)), layer)
        return target_h

    font = _emoji_font()
    bbox = font.getbbox(value)
    source_w = max(1, bbox[2] - bbox[0])
    source_h = max(1, bbox[3] - bbox[1])
    layer = Image.new("RGBA", (source_w + 8, source_h + 8), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(
        (4 - bbox[0], 4 - bbox[1]),
        value,
        font=font,
        embedded_color=True,
    )
    target_w = max(1, round(source_w * target_h / source_h))
    layer = layer.resize((target_w, target_h), Image.Resampling.LANCZOS)
    image.paste(layer, (left, round(center_y - target_h / 2)), layer)
    return target_w


def _draw_name(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    left: int,
    center_y: int,
    value: str,
    size: int,
) -> None:
    cursor = float(left)
    for text, font in _name_runs(value, size):
        if font is None:
            cursor += _draw_emoji(image, text, round(cursor), center_y, size)
            continue
        direction = None
        if any(unicodedata.bidirectional(char) in {"R", "AL", "AN"} for char in text):
            direction = "rtl"
        kwargs = dict(
            font=font,
            fill=_TEXT,
            anchor="lm",
            stroke_width=1,
            stroke_fill="#080b10",
        )
        try:
            draw.text((cursor, center_y), text, direction=direction, **kwargs)
        except (KeyError, TypeError):
            draw.text((cursor, center_y), text, **kwargs)
        cursor += font.getlength(text)


def _text_width(draw: ImageDraw.ImageDraw, value: str, font: ImageFont.FreeTypeFont) -> float:
    return draw.textlength(value, font=font)


def _clip(
    draw: ImageDraw.ImageDraw,
    value: Any,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    """Clip a possibly hostile display name at a pixel boundary.

    Telegram names are user controlled and can contain very long strings,
    combining marks, or emoji.  Pixel clipping keeps the count column and
    progress bars aligned rather than allowing a name to run into them.
    """

    # Do not normalise observed names: spaces, punctuation and Unicode are
    # user-authored content.  Only the visible suffix is replaced when the
    # complete string cannot fit its allocated column.
    text = _display_name(value)
    if _text_width(draw, text, font) <= max_width:
        return text
    ellipsis = "…"
    while text and _text_width(draw, text + ellipsis, font) > max_width:
        text = text[:-1]
    return (text.rstrip() + ellipsis) if text else ellipsis


def _display_name(value: Any, fallback: str = "Unknown member") -> str:
    """Return visible user text without allowing invisible control names.

    The counter stores names exactly as observed.  Cleaning is limited to
    characters that can hide a name or alter Telegram/terminal rendering;
    letters, punctuation, combining marks and emoji remain untouched.
    """

    text = str(value) if value is not None else ""
    text = "".join(
        char for char in text
        if unicodedata.category(char) != "Cc"
        and char not in _REMOVED_FORMAT_CONTROLS
    ).strip()
    has_visible_base = any(
        unicodedata.category(char) not in {"Cf", "Mn", "Me"}
        for char in text
    )
    return text if text and has_visible_base else fallback


def _as_count(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return default


def _avatar_bytes(row: Mapping[str, Any]) -> bytes | None:
    """Get validated-enough in-memory photo bytes from a fixture row.

    Telegram data is validated again while decoding below.  Supporting a few
    explicit aliases keeps the renderer useful for offline fixtures without
    making it responsible for fetching anything.
    """

    for key in ("avatar", "avatar_bytes", "profile_photo", "photo"):
        value = row.get(key)
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            if raw and len(raw) <= 2_000_000:
                return raw
    return None


def _initials(value: Any) -> str:
    text = _display_name(value)
    words = [word for word in text.split() if word]
    if len(words) > 1:
        result = "".join(word[0] for word in words[:2])
    else:
        result = text[:2]
    return result.upper() or "?"


def _avatar_image(
    row: Mapping[str, Any],
    name: str,
    *,
    size: int = AVATAR_SIZE,
    border_color: str = _BORDER,
) -> Image.Image:
    """Decode a real photo, or draw a clearly-labelled initials fallback.

    No generated photographic substitute is used: if Telegram does not make a
    photo available, the initials card is intentionally obvious.
    """

    avatar = None
    raw = _avatar_bytes(row)
    if raw is not None:
        try:
            with Image.open(io.BytesIO(raw)) as source:
                # Telegram profile photos are small; reject crafted
                # decompression bombs before Pillow expands them.
                if not (0 < source.width <= 1024 and 0 < source.height <= 1024):
                    raise ValueError("profile photo dimensions are too large")
                source.load()
                avatar = source.convert("RGB")
        except Exception:
            avatar = None

    if avatar is not None:
        side = min(avatar.width, avatar.height)
        left = (avatar.width - side) // 2
        top = (avatar.height - side) // 2
        avatar = avatar.crop((left, top, left + side, top + side))
        avatar.thumbnail((size, size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (size, size), "#243b5a")
        canvas.paste(avatar, ((size - avatar.width) // 2, (size - avatar.height) // 2))
    else:
        # Stable colours make the fallback easy to distinguish without
        # pretending that it is a profile photograph.
        palette = ("#294a68", "#345777", "#2f5e70", "#3f5474", "#264866")
        seed = sum(ord(char) for char in name) % len(palette)
        canvas = Image.new("RGB", (size, size), palette[seed])
        draw = ImageDraw.Draw(canvas)
        font_size = max(22, size // 3)
        initials = _initials(name)
        initials = _clip_name(initials, font_size, size - 12)
        text_width = _name_width(initials, font_size)
        _draw_name(
            canvas,
            draw,
            round((size - text_width) / 2),
            size // 2,
            initials,
            font_size,
        )

    # Match Telegram's familiar circular DP treatment without changing the
    # requested 82px source/display box.
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    rounded = Image.new("RGB", (size, size), _CARD)
    rounded.paste(canvas, (0, 0), mask)
    ImageDraw.Draw(rounded).ellipse(
        (1, 1, size - 2, size - 2), outline=border_color, width=3
    )
    return rounded


def _horizontal_gradient(width: int, height: int, start: str, end: str) -> Image.Image:
    """Create a compact RGB gradient used only inside a rounded bar mask."""
    a = ImageColor.getrgb(start)
    b = ImageColor.getrgb(end)
    line = Image.new("RGB", (max(1, width), 1))
    pixels = line.load()
    denominator = max(1, width - 1)
    for x in range(width):
        ratio = x / denominator
        pixels[x, 0] = tuple(
            round(a[channel] + (b[channel] - a[channel]) * ratio)
            for channel in range(3)
        )
    return line.resize((max(1, width), height))


def _draw_chat_icon(draw: ImageDraw.ImageDraw, left: int, top: int) -> None:
    """Draw a bold white chat mark with the same intensity as the title."""
    draw.rounded_rectangle(
        (left + 13, top, left + 63, top + 38),
        radius=12,
        fill="#f4f5f7",
        outline="#f4f5f7",
        width=3,
    )
    draw.polygon(
        ((left + 47, top + 35), (left + 58, top + 49), (left + 57, top + 33)),
        fill="#f4f5f7",
    )
    draw.rounded_rectangle(
        (left, top + 19, left + 52, top + 58),
        radius=12,
        fill="#f4f5f7",
        outline="#f4f5f7",
        width=3,
    )
    draw.polygon(
        ((left + 10, top + 54), (left + 2, top + 68), (left + 20, top + 56)),
        fill="#f4f5f7",
    )
    for offset in (14, 25, 36):
        draw.ellipse(
            (left + offset, top + 36, left + offset + 5, top + 41),
            fill="#29435d",
        )


def _value(data: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = data.get(key, default)
    return default if value is None else value


def _render(
    data: Mapping[str, Any],
    group_name: str = "Group",
    *,
    width: int = WIDTH,
) -> bytes:
    if not isinstance(data, Mapping):
        raise TypeError("Chat Fight image data must be a mapping")
    if width < 720:
        raise ValueError("Chat Fight image width is too small")

    requested_width = width
    width = WIDTH
    entries: Sequence[Mapping[str, Any]] = _value(data, "entries", ()) or ()
    # ActivityStore pages are ten rows, but the renderer is defensive when it
    # is handed fixture data or a future store implementation.
    entries = list(entries)[:TOP_ROWS]

    # Keep the measured reference panel and bars, but allow a 10px photo gap.
    # The reference has no photos, so its tighter row pitch cannot be reused.
    # Rank/count labels and footer are intentionally absent from the image.
    header_font = _font(164, bold=True)
    row_name_font = _font(29, bold=True)

    # Keep the established portrait pitch so ten 82px avatars remain fully
    # inside the taller smoked-glass frame.
    list_top = 238
    height = 1216
    # Deep vertical gradient gives the outer canvas depth without adding text.
    image = Image.new("RGB", (width, height), _BG)
    background = Image.new("RGB", (1, height))
    bg_pixels = background.load()
    # Keep the established corner tone for image compatibility; the panel
    # itself is intentionally much darker than this outer atmospheric field.
    bg_top, bg_bottom = ImageColor.getrgb("#10161d"), ImageColor.getrgb("#111d30")
    for y in range(height):
        ratio = y / max(1, height - 1)
        bg_pixels[0, y] = tuple(
            round(bg_top[channel] + (bg_bottom[channel] - bg_top[channel]) * ratio)
            for channel in range(3)
        )
    image.paste(background.resize((width, height)), (0, 0))
    draw = ImageDraw.Draw(image)

    # Restrained navy geometry gives the outer field depth without competing
    # with the smoked-glass leaderboard panel.
    for bounds, stroke in (
        ((28, -291, 483, 165), 76),
        ((590, -194, 918, 135), 68),
        ((-186, 249, 373, 736), 80),
        ((1645, 535, 2310, 1300), 92),
        ((2000, 630, 2390, 980), 50),
    ):
        draw.ellipse(bounds, outline="#172b43", width=stroke)

    # Fit actual ink, not the font's invisible ascender/line-box padding.
    title = "LEADERBOARD"
    box = draw.textbbox((0, 0), title, font=header_font)
    heading = Image.new("L", (box[2] - box[0], box[3] - box[1]))
    ImageDraw.Draw(heading).text((-box[0], -box[1]), title, font=header_font, fill=255)
    title_percent = int(_value(data, "title_width_percent", DEFAULT_TITLE_WIDTH))
    if not 20 <= title_percent <= 60:
        raise ValueError("Title width must be between 20 and 60 percent")
    title_width = round((width - 248) * title_percent / 100)
    title_height = round(title_width * 114 / 1176)
    heading = heading.resize((title_width, title_height), Image.Resampling.LANCZOS)
    # A restrained title shadow improves separation from the outer artwork.
    image.paste(
        "#090d12",
        ((width - title_width) // 2, 143 - title_height // 2),
        heading,
    )
    image.paste(
        _TEXT,
        ((width - title_width) // 2, 139 - title_height // 2),
        heading,
    )
    _draw_chat_icon(
        draw,
        (width - title_width) // 2 - 92,
        105,
    )

    row_left, row_right = 124, width - 124
    # Smoked glass keeps the atmospheric navy visible beneath the panel while
    # preserving strong contrast for names and bars.
    glass_tint = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(glass_tint).rounded_rectangle(
        (row_left, 232, row_right, 1166),
        radius=48,
        fill=(10, 17, 28, 184),
    )
    image = Image.alpha_composite(image.convert("RGBA"), glass_tint).convert("RGB")
    # The soft border glow does not change the panel's 80/20 interior ratio.
    frame_glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(frame_glow).rounded_rectangle(
        (row_left, 232, row_right, 1166),
        radius=48,
        outline=(80, 105, 132, 150),
        width=14,
    )
    frame_glow = frame_glow.filter(ImageFilter.GaussianBlur(18))
    image = Image.alpha_composite(image.convert("RGBA"), frame_glow).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (row_left, 232, row_right, 1166),
        radius=48, outline=_BORDER, width=4,
    )
    draw.rounded_rectangle(
        (row_left + 8, 240, row_right - 8, 1158),
        radius=40, outline=_DEEP_BLUE, width=2,
    )
    max_count = max(
        [_as_count(_value(row, "count", 0)) for row in entries] or [1]
    )
    # Give real names enough room while retaining a generous chart column.
    portrait_left = row_left + 52
    name_left = portrait_left + AVATAR_SIZE + 20
    name_width = 280
    bar_left = 595
    bar_width = row_right - bar_left - 62
    avatar_rings = ("#6d8eae", "#5f7f9d", "#52728f")
    for index, row in enumerate(entries):
        y = list_top + index * ROW_HEIGHT
        name_value = ""
        for candidate in (
            row.get("name"),
            row.get("display_name"),
            row.get("displayUser"),
            row.get("user_id"),
        ):
            visible = _display_name(candidate, "")
            if visible:
                name_value = visible
                break
        name_value = name_value or "Unknown member"
        portrait = _avatar_image(
            row,
            name_value,
            border_color=avatar_rings[index] if index < 3 else _BORDER,
        )
        image.paste(portrait, (portrait_left, y + (ROW_HEIGHT - AVATAR_SIZE) // 2))
        name = _clip_name(name_value, 29, name_width)
        _draw_name(
            image,
            draw,
            name_left,
            round(y + ROW_HEIGHT / 2),
            name,
            29,
        )

        count = _as_count(_value(row, "count", 0))
        bar_height = BAR_HEIGHT
        bar_y = y + (ROW_HEIGHT - bar_height) // 2
        progress = int(bar_width * count / max_count) if max_count else 0
        if progress:
            mask = Image.new("L", (progress, bar_height), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, progress - 1, bar_height - 1),
                radius=min(bar_height // 2, progress // 2),
                fill=255,
            )
            fill = _horizontal_gradient(progress, bar_height, _ACCENT, _ACCENT_END)
            image.paste(fill, (bar_left, bar_y), mask)
            # A subtle highlight keeps the original thin blue bars crisp.
            highlight_end = bar_left + max(0, progress - bar_height // 2)
            if highlight_end > bar_left + 2:
                draw.line(
                    (bar_left + bar_height // 2, bar_y + 3, highlight_end, bar_y + 3),
                    fill="#9ab6da",
                    width=1,
                )

    if not entries:
        empty = "No counted member messages in this period yet."
        draw.text(
            (row_left + 24, list_top + 30),
            _clip(draw, empty, row_name_font, row_right - row_left - 48),
            font=row_name_font,
            fill=_MUTED,
        )

    if requested_width != WIDTH:
        image = image.resize(
            (requested_width, round(height * requested_width / WIDTH)),
            Image.Resampling.LANCZOS,
        )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_leaderboard(
    data: Mapping[str, Any],
    group_name: str = "Group",
    *,
    width: int = WIDTH,
) -> bytes:
    """Render a leaderboard into PNG bytes without touching the filesystem."""

    return _render(data, group_name, width=width)


# Explicit sync alias useful to callers whose ``render_*`` naming convention
# distinguishes the event-loop wrapper from Pillow work.
render_leaderboard_sync = render_leaderboard


async def render_leaderboard_async(
    data: Mapping[str, Any],
    group_name: str = "Group",
    *,
    width: int = WIDTH,
) -> bytes:
    """Run all Pillow work outside the Telegram event loop."""

    return await asyncio.to_thread(render_leaderboard, data, group_name, width=width)


async def render_chatfight_image(
    data: Mapping[str, Any],
    group_name: str = "Group",
    *,
    width: int = WIDTH,
) -> bytes:
    """Descriptive alias used by the plugin and integrations."""

    return await render_leaderboard_async(data, group_name, width=width)


# Friendly aliases for callers that do not use the leaderboard terminology.
# Keep the synchronous ``render_chatfight`` useful in small offline scripts;
# Telegram handlers should use ``render_chatfight_image``/the async wrapper.
render_chatfight = render_leaderboard
render_image = render_leaderboard


async def render(
    data: Mapping[str, Any],
    group_name: str = "Group",
    *,
    width: int = WIDTH,
) -> bytes:
    return await render_leaderboard_async(data, group_name, width=width)


__all__ = [
    "TOP_ROWS",
    "WIDTH",
    "render_chatfight_image",
    "render_chatfight",
    "render_image",
    "render",
    "render_leaderboard",
    "render_leaderboard_async",
    "render_leaderboard_sync",
]