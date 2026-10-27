"""ASS composition for the karaoke-style overlays drawn inside the mpv window.

mpv renders these through libass, the same engine it uses for subtitles, so
the "custom player" look -- a bar of numbered boxes, a timer, a full-screen
"next on stage" card between songs -- costs nothing in playback reliability:
mpv still decodes and plays exactly as before, this only draws on top.

Everything here is a pure function from data to an ASS event string, so it is
testable without mpv. The player pushes the result with `osd-overlay`.

Layout is done on a virtual canvas OVERLAY_RES_Y (720) units tall and as wide
as the window's aspect ratio makes it, so boxes keep their shape in any window
and the same numbers work windowed and fullscreen.
"""

from __future__ import annotations

from dataclasses import dataclass

CANVAS_HEIGHT = 720
DEFAULT_CANVAS_WIDTH = 1280

# Rough average glyph width as a fraction of the font size, for cutting labels
# to fit a box. libass offers no measuring call over IPC, and an estimate that
# errs a little wide is fine: the cost is an ellipsis a few characters early.
_GLYPH_WIDTH = 0.56

_ZERO_WIDTH_SPACE = "​"

# Strings drawn by the backend, so they follow settings.locale rather than the
# UI's i18n table. Any of them can be replaced by a setting.
STRINGS = {
    "en": {
        "bar_heading": "NEXT UP",
        "stage_heading": "NEXT ON STAGE",
        "starting_in": "Starting in {seconds}″",
        "empty_slot": "—",
    },
    "el": {
        "bar_heading": "ΕΠΟΜΕΝΟΙ",
        "stage_heading": "ΕΠΟΜΕΝΟΣ ΣΤΗ ΣΚΗΝΗ",
        "starting_in": "Ξεκινάμε σε {seconds}″",
        "empty_slot": "—",
    },
}

LABEL_MODES = ("title", "singer", "both")
STYLES = ("list", "bar")


@dataclass
class UpcomingEntry:
    title: str
    singer: str | None = None


@dataclass
class OverlayTheme:
    """The user-tunable look. Colours are #rrggbb as stored in settings."""

    label_mode: str = "both"
    brand: str = ""
    bar_heading: str = ""
    stage_heading: str = ""
    accent: str = "#a78bfa"
    bar_color: str = "#14151d"
    box_color: str = "#3b3f52"
    background: str = "#5b3f78"
    boxes: int = 3
    at_top: bool = False
    locale: str = "en"

    def text(self, key: str) -> str:
        custom = {"bar_heading": self.bar_heading, "stage_heading": self.stage_heading}.get(key)
        if custom:
            return custom
        table = STRINGS.get(self.locale) or STRINGS["en"]
        return table.get(key) or STRINGS["en"][key]


# ------------------------------------------------------------------ helpers


def ass_escape(text: str) -> str:
    r"""Neutralise ASS markup in user text (titles, names).

    libass reads `{...}` as an override block, and a backslash can open a tag
    such as `\N`; a zero-width space after it keeps the character visible.
    """
    escaped = text.replace("\\", "\\" + _ZERO_WIDTH_SPACE)
    return escaped.replace("{", "\\{").replace("}", "\\}")


def fit(text: str, width: float, font_size: float) -> str:
    """Collapse whitespace and cut `text` to roughly fit `width` canvas units."""
    text = " ".join((text or "").split())
    limit = max(1, int(width / (font_size * _GLYPH_WIDTH)))
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def ass_color(hex_color: str, fallback: str = "#ffffff") -> str:
    """#rrggbb -> ASS &HBBGGRR&. Bad input falls back rather than raising."""
    value = (hex_color or "").strip().lstrip("#")
    if len(value) != 6:
        value = fallback.lstrip("#")
    try:
        int(value, 16)
    except ValueError:
        value = fallback.lstrip("#")
    r, g, b = value[0:2], value[2:4], value[4:6]
    return f"&H{b}{g}{r}&".upper()


def _mix(hex_color: str, other: str, amount: float) -> str:
    """Blend two #rrggbb colours; amount 0 keeps the first, 1 gives the second."""

    def parse(value: str) -> tuple[int, int, int]:
        value = (value or "").strip().lstrip("#")
        try:
            return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
        except (ValueError, IndexError):
            return 0, 0, 0

    a, b = parse(hex_color), parse(other)
    mixed = (round(x + (y - x) * amount) for x, y in zip(a, b))
    return "#" + "".join(f"{max(0, min(255, c)):02x}" for c in mixed)


def _rect(x1: float, y1: float, x2: float, y2: float, color: str, alpha: int = 0, radius: float = 0) -> str:
    """One ASS event drawing a filled (optionally rounded) rectangle."""
    x1, y1, x2, y2 = (round(v) for v in (x1, y1, x2, y2))
    r = round(min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    if r > 0:
        path = (
            f"m {x1 + r} {y1} l {x2 - r} {y1} b {x2} {y1} {x2} {y1} {x2} {y1 + r} "
            f"l {x2} {y2 - r} b {x2} {y2} {x2} {y2} {x2 - r} {y2} "
            f"l {x1 + r} {y2} b {x1} {y2} {x1} {y2} {x1} {y2 - r} "
            f"l {x1} {y1 + r} b {x1} {y1} {x1} {y1} {x1 + r} {y1}"
        )
    else:
        path = f"m {x1} {y1} l {x2} {y1} l {x2} {y2} l {x1} {y2}"
    return (
        "{\\an7\\pos(0,0)\\bord0\\shad0\\1c" + ass_color(color) + f"\\1a&H{alpha:02X}&\\p1}}" + path + "{\\p0}"
    )


def _text(
    x: float,
    y: float,
    text: str,
    size: float,
    color: str = "#ffffff",
    bold: bool = False,
    align: int = 5,
    outline: float = 1.2,
    spacing: float = 0,
) -> str:
    """One ASS event with escaped text anchored at (x, y)."""
    tags = (
        f"\\an{align}\\pos({round(x)},{round(y)})\\fs{round(size)}\\b{1 if bold else 0}"
        f"\\bord{outline:g}\\shad0\\q2\\1c{ass_color(color)}\\3c&H000000&"
    )
    if spacing:
        tags += f"\\fsp{spacing:g}"
    return "{" + tags + "}" + ass_escape(text)


def _labels(entry: UpcomingEntry, mode: str) -> tuple[str, str | None]:
    """(main, secondary) text for an entry under the chosen label mode.

    A missing singer always falls back to the title, so "singer" mode on a
    queue nobody has named still shows something useful.
    """
    singer = (entry.singer or "").strip()
    if mode == "singer":
        return (singer or entry.title), None
    if mode == "both" and singer:
        return singer, entry.title
    return entry.title, None


# ------------------------------------------------------------------- bar


BAR_HEIGHT = 74

# The logo is a bitmap, not ASS, so these place it on the same canvas and the
# player converts the box to window pixels. (x, y, w, h), canvas units.
LOGO_MAX_BAR_WIDTH = 220
LOGO_CARD_HEIGHT = 170


def bar_logo_box(logo_size: tuple[int, int], theme: OverlayTheme) -> tuple[float, float, float, float]:
    top = 0 if theme.at_top else CANVAS_HEIGHT - BAR_HEIGHT
    h = BAR_HEIGHT - 14
    w = h * logo_size[0] / logo_size[1]
    if w > LOGO_MAX_BAR_WIDTH:
        w, h = LOGO_MAX_BAR_WIDTH, LOGO_MAX_BAR_WIDTH * logo_size[1] / logo_size[0]
    return 12, top + (BAR_HEIGHT - h) / 2, w, h


def card_logo_box(logo_size: tuple[int, int], width: int) -> tuple[float, float, float, float]:
    h = LOGO_CARD_HEIGHT
    w = h * logo_size[0] / logo_size[1]
    if w > width * 0.6:
        w, h = width * 0.6, width * 0.6 * logo_size[1] / logo_size[0]
    return (width - w) / 2, 150 - h / 2, w, h


def compose_bar(
    entries: list[UpcomingEntry],
    theme: OverlayTheme,
    timer: str | None = None,
    width: int = DEFAULT_CANVAS_WIDTH,
    logo_width: float = 0,
) -> list[str]:
    """The strip of numbered "next up" boxes, with logo, brand and timer at the left.

    `logo_width` reserves room at the left edge for the logo bitmap, which the
    player draws separately.
    """
    height = CANVAS_HEIGHT
    top = 0 if theme.at_top else height - BAR_HEIGHT
    events = [_rect(0, top, width, top + BAR_HEIGHT, theme.bar_color, alpha=0x30)]

    x = 14 + (logo_width + 12 if logo_width else 0)
    if theme.brand:
        brand = fit(theme.brand, 200, 22)
        events.append(_text(x, top + BAR_HEIGHT / 2, brand, 22, theme.accent, bold=True, align=4, outline=1.5))
        x += min(200, len(brand) * 22 * _GLYPH_WIDTH) + 16
    if timer is not None:
        events.append(_rect(x, top + 22, x + 94, top + BAR_HEIGHT - 10, theme.box_color, alpha=0x20, radius=6))
        events.append(_text(x + 47, top + 22 + (BAR_HEIGHT - 32) / 2, f"[{timer}]", 20, "#ffffff", bold=True))
        x += 94 + 12

    events.append(
        _text((x + width) / 2, top + 11, theme.text("bar_heading"), 12, theme.accent, bold=True, outline=1, spacing=1)
    )

    count = max(1, min(int(theme.boxes or 3), 6))
    gap = 8
    box_w = (width - 12 - x - gap * (count - 1)) / count
    box_top, box_bottom = top + 22, top + BAR_HEIGHT - 8
    middle = (box_top + box_bottom) / 2
    for slot in range(count):
        bx = x + slot * (box_w + gap)
        events.append(_rect(bx, box_top, bx + box_w, box_bottom, theme.box_color, alpha=0x28, radius=6))
        events.append(_text(bx + 10, middle, str(slot + 1), 16, theme.accent, bold=True, align=4))
        text_x, text_w = bx + box_w / 2 + 8, box_w - 40
        if slot < len(entries):
            main, secondary = _labels(entries[slot], theme.label_mode)
            if secondary:
                events.append(_text(text_x, middle - 9, fit(main, text_w, 17), 17, bold=True))
                events.append(_text(text_x, middle + 11, fit(secondary, text_w, 12), 12, "#c9c9d6"))
            else:
                events.append(_text(text_x, middle, fit(main, text_w, 17), 17, bold=True))
        else:
            events.append(_text(text_x, middle, theme.text("empty_slot"), 17, "#c9c9d6", bold=True))
    return events


# --------------------------------------------------------------- intermission


def compose_intermission(
    entry: UpcomingEntry,
    remaining: float,
    total: float,
    theme: OverlayTheme,
    width: int = DEFAULT_CANVAS_WIDTH,
    logo: bool = False,
) -> list[str]:
    """The full-screen card shown during the pause between songs.

    With `logo`, the top of the card is left to the logo bitmap and the brand
    text is not drawn.
    """
    height = CANVAS_HEIGHT
    events: list[str] = []

    # libass has no gradients; horizontal strips blending top to bottom look
    # like one at any window size. Overlapping by a unit hides the seams.
    strips = 36
    top_color = _mix(theme.background, "#000000", 0.55)
    for index in range(strips):
        y1 = height * index / strips
        y2 = height * (index + 1) / strips + 1
        events.append(_rect(0, y1, width, y2, _mix(top_color, theme.background, index / (strips - 1))))

    centre = width / 2
    if theme.brand and not logo:
        events.append(_text(centre, 150, fit(theme.brand, width * 0.8, 58), 58, "#ffffff", bold=True, outline=3))
    events.append(_text(centre, 290, theme.text("stage_heading"), 22, "#e9e6f5", outline=1, spacing=1.5))

    main, secondary = _labels(entry, theme.label_mode)
    events.append(_text(centre, 360, fit(main, width * 0.85, 60), 60, "#ffffff", bold=True, outline=2))
    if secondary:
        events.append(_text(centre, 418, fit(secondary, width * 0.8, 26), 26, "#d8d2ea", outline=1))

    seconds = max(0, int(-(-remaining // 1)))  # ceil without importing math
    starting = theme.text("starting_in").replace("{seconds}", str(seconds))
    events.append(_text(centre, 500, starting, 26, "#ffffff", bold=True, outline=1.5))

    track_x1, track_x2 = width * 0.2, width * 0.8
    events.append(_rect(track_x1, 526, track_x2, 532, "#ffffff", alpha=0xB0, radius=3))
    done = 0.0 if total <= 0 else max(0.0, min(1.0, 1 - remaining / total))
    if done > 0:
        events.append(_rect(track_x1, 526, track_x1 + (track_x2 - track_x1) * done, 532, "#ffffff", radius=3))
    return events


def format_clock(seconds: float | None) -> str:
    """mm:ss (or h:mm:ss) for the bar's timer box."""
    total = max(0, int(seconds or 0))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
