"""mpv process management and JSON IPC.

The second window *is* mpv. Handing playback to mpv rather than an HTML5
<video> element buys hardware decoding, correct DASH/adaptive handling, an
on-screen controller and keyboard bindings for free — and, because it plays
the raw stream directly, nothing to strip ads out of.

Only ever one file is loaded at a time: Python owns the queue (see
playqueue.py) so every item gets a freshly-resolved URL.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

from python_mpv_jsonipc import MPV, MPVError

from .config import Settings, user_data_dir
from .models import PlaybackState, PlayerStatus
from .overlay import (
    CANVAS_HEIGHT,
    DEFAULT_CANVAS_WIDTH,
    OverlayTheme,
    UpcomingEntry,
    bar_logo_box,
    card_logo_box,
    compose_bar,
    compose_intermission,
)

log = logging.getLogger(__name__)

OBSERVED_PROPERTIES = ("time-pos", "duration", "pause", "volume", "mute", "core-idle", "osd-dimensions")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

# Overlay ids are a per-client namespace. Ours is named so a second overlay
# added later cannot silently overwrite this one.
UPCOMING_OVERLAY_ID = 1

# The virtual canvas the overlay is laid out on. Pinning the height keeps the
# font size below meaning the same thing whatever size the window is.
OVERLAY_RES_Y = 720

# Titles are cut rather than wrapped: an untruncated one runs the full width of
# the video, which is measurably what happens with a real YouTube title.
OVERLAY_TITLE_CHARS = 40

# Point size on the OVERLAY_RES_Y canvas, so this is a fraction of window
# height rather than pixels -- it looks the same fullscreen and windowed. This
# is the fallback default; the live value comes from settings.overlay_font_size.
OVERLAY_FONT_SIZE = 28

# ASS `\an` alignment codes, laid out like a numpad, for each overlay position
# the settings panel offers. Only the top and bottom edges are here: the middle
# row sits over the middle of the picture, where an "up next" list has no
# business being.
OVERLAY_ALIGNMENTS = {
    "top-left": 7,
    "top-center": 8,
    "top-right": 9,
    "bottom-left": 1,
    "bottom-center": 2,
    "bottom-right": 3,
}

# Where the overlay sits when settings.overlay_position is unset or unknown.
OVERLAY_DEFAULT_POSITION = "bottom-right"

OVERLAY_HEADING = "Up next"

# Bitmap overlay ids (overlay-add has its own namespace, separate from
# osd-overlay's): the logo in the bar and the larger one on the card.
LOGO_BAR_ID = 1
LOGO_CARD_ID = 2


def logo_path():
    """Raw premultiplied BGRA pixels of the user's logo; see /api/overlay/logo."""
    return user_data_dir() / "logo.bgra"

# Written as an escape rather than the literal character, which is invisible in
# a source file and reads as a typo.
_ZERO_WIDTH_SPACE = "\u200b"


def _ass_escape(text: str) -> str:
    r"""Neutralise ASS markup in a title.

    Titles come from YouTube, so they are arbitrary text, and libass reads
    `{...}` as an override block rather than printing it. Measured on mpv
    0.41, a title containing `{\fs90}` really does resize the overlay -- so
    this is a correctness fix, not tidiness. A zero-width space after a
    backslash leaves the character visible while stopping it from opening a
    tag such as `\N`.
    """
    escaped = text.replace("\\", "\\" + _ZERO_WIDTH_SPACE)
    return escaped.replace("{", "\\{").replace("}", "\\}")


def _truncate(text: str, limit: int = OVERLAY_TITLE_CHARS) -> str:
    """Collapse whitespace and cut to something that fits the corner."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def compose_upcoming(
    titles: list[str],
    font_size: int = OVERLAY_FONT_SIZE,
    position: str = OVERLAY_DEFAULT_POSITION,
) -> str:
    r"""Build the ASS event for the "up next" list.

    `position` picks an `\an` alignment (see OVERLAY_ALIGNMENTS), so the block
    anchors to a corner or edge and grows inward from it. A left or right anchor
    grows the text horizontally toward the middle; a centre anchor grows it
    symmetrically. Either way titles are truncated first, so nothing runs off
    the frame even though the overlay is laid out at an unknown width (res_x is
    left at 0 so mpv keeps pixels square).

    A bottom position shares space with mpv's on-screen controller, a full-width
    bar along the bottom, but the OSC only appears on mouse movement and hides
    itself again, so the two are seldom on screen at once.

    `\q2` stops libass wrapping a title back across the picture, and the
    outline (`\bord2`) keeps it readable over a bright frame.
    """
    lines = [f"{n}. {_ass_escape(_truncate(title))}" for n, title in enumerate(titles, 1)]
    body = "\\N".join([_ass_escape(OVERLAY_HEADING), *lines])
    align = OVERLAY_ALIGNMENTS.get(position, OVERLAY_ALIGNMENTS[OVERLAY_DEFAULT_POSITION])
    # Built by concatenation: an f-string here would need doubled braces around
    # ASS's own braces, which reads as a typo.
    style = "{\\an" + str(align) + "\\fs" + str(font_size) + "\\bord2\\shad0\\q2}"
    return style + body


def _list_label(entry: UpcomingEntry, mode: str) -> str:
    """One line of the classic "up next" list under the chosen label mode."""
    singer = (entry.singer or "").strip()
    if not singer or mode == "title":
        return entry.title
    if mode == "singer":
        return singer
    return f"{singer} — {entry.title}"


def _ensure_dead(process: subprocess.Popen | None) -> None:
    """Make certain an mpv process is gone, children included.

    python-mpv-jsonipc stops mpv with Popen.terminate(), which on Windows is
    TerminateProcess against the process it spawned -- and on Windows that is
    `mpv.com`, a console wrapper that re-executes `mpv.exe` as a *child*. The
    wrapper dies, the real player keeps running with no one driving it, and
    they pile up one per session. taskkill /T takes the tree.
    """
    if process is None or process.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            log.debug("taskkill on mpv failed", exc_info=True)
    else:
        try:
            process.kill()
        except OSError:
            log.debug("mpv kill failed", exc_info=True)


class PlayerUnavailable(RuntimeError):
    pass


class MpvPlayer:
    """Async-friendly facade over a single mpv instance.

    mpv's callbacks arrive on its own listener thread; everything crossing back
    into the app is marshalled onto the asyncio loop.
    """

    def __init__(self, settings: Settings, loop: asyncio.AbstractEventLoop):
        self.settings = settings
        self.loop = loop
        self.mpv: MPV | None = None
        self.status = PlayerStatus(volume=settings.volume)
        self._lock = threading.Lock()
        self._current_video_id: str | None = None
        self._starting = False
        # The overlay is write-only as far as mpv is concerned -- no property
        # reads it back -- so the desired list is kept here and redrawn from
        # scratch whenever a new mpv process comes up.
        self._upcoming: list[str | UpcomingEntry] = []
        self._overlay_text = ""
        # The karaoke card between songs, as (entry, seconds left, total), and
        # the bar's timer text. Both are drawn into the same overlay.
        self._intermission: tuple[UpcomingEntry, float, float] | None = None
        self._timer: str | None = None
        # Width of the overlay canvas, matched to the window's aspect ratio
        # so boxes stay square-cornered rather than stretched.
        self._canvas_width = DEFAULT_CANVAS_WIDTH
        # Window height in pixels, to place the logo bitmap, which (unlike
        # ASS) is positioned in real pixels.
        self._osd_height = CANVAS_HEIGHT
        # Logo bitmaps currently drawn: id -> the overlay-add arguments.
        self._logos: dict[int, tuple] = {}

        # Callbacks installed by the engine.
        self.on_end_file: Callable[[str], None] | None = None
        self.on_status_change: Callable[[], None] | None = None
        self.on_window_closed: Callable[[], None] | None = None

    # ------------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return self.mpv is not None

    async def ensure_started(self) -> None:
        if self.running or self._starting:
            return
        self._starting = True
        try:
            await asyncio.get_running_loop().run_in_executor(None, self._start_blocking)
        finally:
            self._starting = False

    def _start_blocking(self) -> None:
        binary = self.settings.mpv_binary()
        if not binary:
            raise PlayerUnavailable(
                "mpv was not found. Install mpv, or set mpv_path in settings.json."
            )

        log.info("starting mpv from %s", binary)
        options: dict[str, Any] = {
            # Stay alive with a visible window when nothing is queued, so the
            # "player window" exists from launch.
            "idle": True,
            "force_window": True,
            "keep_open": False,
            "terminal": False,
            # We do our own extraction; mpv's ytdl hook must not interfere.
            "ytdl": False,
            "osc": True,
            "input_default_bindings": True,
            "input_vo_keyboard": True,
            "title": "Cue",
            "volume": self.settings.volume,
            "cache": True,
        }
        options.update(self.settings.mpv_options)

        try:
            mpv = MPV(
                start_mpv=True,
                mpv_location=binary,
                quit_callback=self._on_quit,
                **options,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as-is
            raise PlayerUnavailable(f"could not start mpv: {exc}") from exc

        with self._lock:
            self.mpv = mpv
            self.status.window_open = True

        mpv.bind_event("end-file", self._handle_end_file)
        for prop in OBSERVED_PROPERTIES:
            mpv.bind_property_observer(prop, self._handle_property)

        # A fresh process has no overlay, so anything the engine told us about
        # while the window was shut has to be drawn again. This runs on the
        # executor thread that started mpv, where blocking IPC is fine.
        self._overlay_text = ""
        self._logos = {}
        self._apply_upcoming()

    def shutdown(self) -> None:
        with self._lock:
            mpv, self.mpv = self.mpv, None
        # Whatever was drawn goes with the process. `_upcoming` is kept, so a
        # later restart redraws it.
        self._overlay_text = ""
        if not mpv:
            return
        # Order matters: take the tree down *before* terminate(), because
        # terminate() kills the mpv.com wrapper and a dead parent can no
        # longer be used to find its children -- the real mpv.exe would be
        # orphaned beyond reach. terminate() then does the IPC cleanup.
        _ensure_dead(getattr(getattr(mpv, "mpv_process", None), "process", None))
        try:
            mpv.terminate()
        except Exception:  # noqa: BLE001 - best effort on the way out
            log.debug("mpv terminate failed", exc_info=True)

    # ---------------------------------------------------------- mpv callbacks

    def _dispatch(self, fn: Callable[..., Any], *args: Any) -> None:
        """Hop from an mpv thread onto the asyncio loop."""
        if fn is None:
            return
        try:
            self.loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            log.debug("event loop closed, dropping mpv callback")

    def _on_quit(self) -> None:
        """mpv exited — usually the user closed the player window."""
        log.info("mpv exited")
        with self._lock:
            self.mpv = None
        # The overlay died with the process; forget what was drawn so reopening
        # the window redraws it rather than deduplicating it away.
        self._overlay_text = ""
        self.status.window_open = False
        self.status.state = PlaybackState.IDLE
        self._dispatch(self.on_window_closed)

    def _handle_end_file(self, data: dict) -> None:
        reason = (data or {}).get("reason", "eof")
        log.debug("mpv end-file: %s", reason)
        self._dispatch(self.on_end_file, reason)

    def _handle_property(self, name: str, value: Any) -> None:
        status = self.status
        if name == "time-pos":
            status.position = float(value or 0.0)
        elif name == "duration":
            status.duration = float(value) if value else None
        elif name == "pause":
            if status.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                status.state = PlaybackState.PAUSED if value else PlaybackState.PLAYING
        elif name == "volume":
            status.volume = int(value or 0)
        elif name == "mute":
            status.muted = bool(value)
        elif name == "core-idle":
            if value and status.state is PlaybackState.PLAYING and status.position == 0:
                return  # buffering, not a state change worth reporting
        elif name == "osd-dimensions":
            width, height = (value or {}).get("w") or 0, (value or {}).get("h") or 0
            if width > 0 and height > 0:
                canvas = round(CANVAS_HEIGHT * width / height)
                if canvas != self._canvas_width or height != self._osd_height:
                    self._canvas_width = canvas
                    self._osd_height = height
                    self._dispatch(self._redraw_soon)
            return
        self._dispatch(self.on_status_change)

    def _redraw_soon(self) -> None:
        """Repaint the overlay from the event loop (the window was resized)."""
        if self.running:
            asyncio.ensure_future(self._redraw())

    async def _redraw(self) -> None:
        try:
            await self._call(self._apply_upcoming)
        except PlayerUnavailable:
            pass

    # ------------------------------------------------------------- commands

    def _require(self) -> MPV:
        mpv = self.mpv
        if mpv is None:
            raise PlayerUnavailable("the player window is not open")
        return mpv

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return await asyncio.get_running_loop().run_in_executor(None, fn, *args)
        except OSError as exc:
            # python-mpv-jsonipc raises TimeoutError after 120s with no reply
            # and BrokenPipeError once mpv is gone -- neither is an MPVError, so
            # they used to escape every handler and leave the queue frozen
            # mid-advance. Both mean the same thing as a closed window.
            log.warning("mpv IPC failed: %r", exc)
            raise PlayerUnavailable(f"the player stopped responding ({exc})") from exc

    async def load(
        self,
        video_url: str,
        audio_url: str | None,
        title: str,
        headers: dict[str, str] | None = None,
        video_id: str | None = None,
        start: float | None = None,
    ) -> None:
        await self.ensure_started()
        self._current_video_id = video_id
        self.status.state = PlaybackState.LOADING
        self.status.position = start or 0.0
        await self._call(self._load_blocking, video_url, audio_url, title, headers, start)
        self.status.state = PlaybackState.PLAYING

    def _load_blocking(
        self,
        video_url: str,
        audio_url: str | None,
        title: str,
        headers: dict[str, str] | None,
        start: float | None = None,
    ) -> None:
        mpv = self._require()
        options: dict[str, Any] = {"force-media-title": title}
        if start:
            options["start"] = f"{start:.3f}"
        if audio_url:
            options["audio-file"] = audio_url

        fields: list[str] = []
        if headers:
            user_agent = headers.get("User-Agent")
            if user_agent:
                options["user-agent"] = user_agent
            fields = [f"{k}: {v}" for k, v in headers.items() if k.lower() != "user-agent"]

        # http-header-fields is a *list* option. Joining it into one string
        # makes mpv split on every comma, and the values yt-dlp hands us are
        # full of them ("Accept-Language: en-us,en;q=0.5"). The fragments turn
        # into malformed header lines, googlevideo answers 403, and playback
        # dies about a second after it starts. Sending a real array over the
        # IPC skips mpv's string list parser altogether.
        self._set_header_fields(mpv, fields)

        # mpv's pause is global, not per file: pausing in the player window and
        # then moving on used to load every later song paused at 0:00, while
        # the app -- which ignores pause changes mid-load -- reported it as
        # playing. Starting a song means playing it.
        try:
            mpv.command("set_property", "pause", False)
        except MPVError:
            log.debug("could not unpause before loading", exc_info=True)

        try:
            # mpv >= 0.38 takes per-file options as the 4th loadfile argument,
            # which keeps them scoped to this file.
            mpv.command("loadfile", video_url, "replace", 0, options)
        except MPVError:
            log.info("loadfile with options failed; falling back to global properties")
            self._load_fallback(mpv, video_url, audio_url, title, options)

    def _set_header_fields(self, mpv: MPV, fields: list[str]) -> None:
        """Set (or clear) the per-request HTTP headers as a real list.

        Always called, including with an empty list, so headers from a previous
        item can never leak into the next one.
        """
        try:
            mpv.command("set_property", "http-header-fields", fields)
        except MPVError:
            log.debug("could not set http-header-fields", exc_info=True)

    def _load_fallback(
        self,
        mpv: MPV,
        video_url: str,
        audio_url: str | None,
        title: str,
        options: dict[str, Any],
    ) -> None:
        """Older mpv: set the options globally, then load."""
        for key in ("user-agent",):
            if key in options:
                try:
                    mpv.command("set_property", key, options[key])
                except MPVError:
                    log.debug("could not set %s", key, exc_info=True)
        try:
            mpv.command("set_property", "audio-files", [audio_url] if audio_url else [])
        except MPVError:
            log.debug("could not set audio-files", exc_info=True)
        mpv.command("loadfile", video_url, "replace")
        try:
            mpv.command("set_property", "force-media-title", title)
        except MPVError:
            pass

    async def set_upcoming(self, titles: list[str]) -> None:
        """Draw the "up next" list inside the video, or clear it when empty.

        Safe to call with no window open: the list is remembered and drawn when
        one next appears, so the engine never has to care whether mpv is up.
        """
        self._upcoming = list(titles)
        await self._repaint()

    async def set_intermission(
        self, entry: UpcomingEntry | None, remaining: float = 0.0, total: float = 0.0
    ) -> None:
        """Show (or with None, remove) the full-screen card between songs."""
        self._intermission = (entry, remaining, total) if entry else None
        await self._repaint()

    async def set_timer(self, text: str | None) -> None:
        """Set the bar's timer box: time left in the song, or the countdown."""
        if text == self._timer:
            return
        self._timer = text
        if self.settings.overlay_style == "bar":
            await self._repaint()

    async def _repaint(self) -> None:
        if not self.running:
            return
        await self._call(self._apply_upcoming)

    def theme(self) -> OverlayTheme:
        s = self.settings
        return OverlayTheme(
            label_mode=s.overlay_label,
            brand=s.overlay_brand or "",
            bar_heading=s.overlay_bar_heading or "",
            stage_heading=s.overlay_stage_heading or "",
            accent=s.overlay_accent_color,
            bar_color=s.overlay_bar_color,
            box_color=s.overlay_box_color,
            background=s.intermission_color,
            boxes=s.overlay_boxes,
            at_top=str(s.overlay_position).startswith("top"),
            locale=s.locale,
        )

    def logo_size(self) -> tuple[int, int] | None:
        size = self.settings.overlay_logo_size
        if not (isinstance(size, (list, tuple)) and len(size) == 2 and all(isinstance(v, int) and v > 0 for v in size)):
            return None
        if not logo_path().is_file():
            return None
        return size[0], size[1]

    def _bar_visible(self) -> bool:
        return self.settings.overlay_style == "bar" and bool(self._upcoming or self._intermission)

    def compose_overlay(self) -> tuple[str, int]:
        """The full overlay as ASS events, plus the canvas width to draw it on.

        Width 0 asks mpv to derive it from the window, keeping pixels square;
        the list style is laid out by alignment alone and needs nothing more.
        """
        entries = [e if isinstance(e, UpcomingEntry) else UpcomingEntry(title=e) for e in self._upcoming]
        theme = self.theme()
        width = self._canvas_width
        logo = self.logo_size()
        events: list[str] = []
        if self._intermission:
            entry, remaining, total = self._intermission
            events += compose_intermission(entry, remaining, total, theme, width, logo=bool(logo))
        if self.settings.overlay_style == "bar":
            if self._bar_visible():
                timer = self._timer if self.settings.overlay_show_timer else None
                logo_width = bar_logo_box(logo, theme)[2] if logo else 0
                events += compose_bar(entries, theme, timer, width, logo_width)
            return "\n".join(events), width
        if entries:
            labels = [_list_label(entry, theme.label_mode) for entry in entries]
            events.append(
                compose_upcoming(labels, self.settings.overlay_font_size, self.settings.overlay_position)
            )
        return "\n".join(events), (width if self._intermission else 0)

    def _apply_upcoming(self) -> None:
        """Push the remembered overlay to mpv. Runs off the event loop."""
        mpv = self.mpv
        if mpv is None:
            return
        self._apply_logos(mpv)
        text, res_x = self.compose_overlay()
        if text == self._overlay_text:
            return
        try:
            if text:
                mpv.command(
                    "osd-overlay", UPCOMING_OVERLAY_ID, "ass-events", text, res_x, OVERLAY_RES_Y
                )
            else:
                # Format "none" unregisters the overlay outright. Passing
                # ass-events with empty data leaves an empty one behind, which
                # mpv keeps compositing for nothing.
                mpv.command("osd-overlay", UPCOMING_OVERLAY_ID, "none", "", 0, OVERLAY_RES_Y)
        except (MPVError, OSError):
            # Cosmetic: a failure here must never interrupt playback.
            log.debug("could not update the upcoming overlay", exc_info=True)
            return
        self._overlay_text = text

    def logo_placements(self) -> dict[int, tuple[int, int, int, int]]:
        """Where each logo bitmap belongs right now, as window-pixel boxes."""
        logo = self.logo_size()
        if not logo:
            return {}
        scale = self._osd_height / CANVAS_HEIGHT
        boxes = {}
        if self._bar_visible():
            boxes[LOGO_BAR_ID] = bar_logo_box(logo, self.theme())
        if self._intermission:
            boxes[LOGO_CARD_ID] = card_logo_box(logo, self._canvas_width)
        return {key: tuple(max(1, round(v * scale)) if i >= 2 else round(v * scale) for i, v in enumerate(box))
                for key, box in boxes.items()}

    def _apply_logos(self, mpv: MPV) -> None:
        """Add, move or remove the logo bitmaps to match logo_placements().

        overlay-add reads the raw pixels from the file and scales them to the
        display size itself (its dw/dh arguments), so one upload serves every
        window size.
        """
        wanted = {}
        logo = self.logo_size()
        for key, (x, y, w, h) in self.logo_placements().items():
            wanted[key] = (key, x, y, str(logo_path()), 0, "bgra", logo[0], logo[1], logo[0] * 4, w, h)
        for key in list(self._logos):
            if key not in wanted:
                try:
                    mpv.command("overlay-remove", key)
                except (MPVError, OSError):
                    log.debug("could not remove logo overlay", exc_info=True)
                del self._logos[key]
        for key, args in wanted.items():
            if self._logos.get(key) == args:
                continue
            try:
                mpv.command("overlay-add", *args)
            except (MPVError, OSError):
                log.debug("could not draw the logo", exc_info=True)
                continue
            self._logos[key] = args

    def reload_logo(self) -> None:
        """Forget what is drawn so the next repaint re-reads the logo file."""
        self._logos = {}
        self._overlay_text = ""

    async def stop(self) -> None:
        if not self.running:
            return
        await self._call(lambda: self._require().command("stop"))
        self.status.state = PlaybackState.IDLE
        self.status.position = 0.0

    async def set_paused(self, paused: bool) -> None:
        mpv = self._require()
        await self._call(lambda: mpv.command("set_property", "pause", paused))
        self.status.state = PlaybackState.PAUSED if paused else PlaybackState.PLAYING

    async def toggle_pause(self) -> None:
        await self.set_paused(self.status.state is not PlaybackState.PAUSED)

    async def seek(self, seconds: float, absolute: bool = True) -> None:
        mpv = self._require()
        mode = "absolute" if absolute else "relative"
        await self._call(lambda: mpv.command("seek", seconds, mode))

    async def set_volume(self, level: int) -> None:
        mpv = self._require()
        level = max(0, min(level, 130))
        await self._call(lambda: mpv.command("set_property", "volume", level))
        self.status.volume = level
        self.settings.volume = level
        self.settings.save()

    async def set_muted(self, muted: bool) -> None:
        mpv = self._require()
        await self._call(lambda: mpv.command("set_property", "mute", muted))
        self.status.muted = muted
