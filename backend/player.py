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

from .config import Settings
from .models import PlaybackState, PlayerStatus

log = logging.getLogger(__name__)

OBSERVED_PROPERTIES = ("time-pos", "duration", "pause", "volume", "mute", "core-idle")

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
        self._upcoming: list[str] = []
        self._overlay_text = ""

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
        self._dispatch(self.on_status_change)

    # ------------------------------------------------------------- commands

    def _require(self) -> MPV:
        mpv = self.mpv
        if mpv is None:
            raise PlayerUnavailable("the player window is not open")
        return mpv

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    async def load(
        self,
        video_url: str,
        audio_url: str | None,
        title: str,
        headers: dict[str, str] | None = None,
        video_id: str | None = None,
    ) -> None:
        await self.ensure_started()
        self._current_video_id = video_id
        self.status.state = PlaybackState.LOADING
        self.status.position = 0.0
        await self._call(self._load_blocking, video_url, audio_url, title, headers)
        self.status.state = PlaybackState.PLAYING

    def _load_blocking(
        self,
        video_url: str,
        audio_url: str | None,
        title: str,
        headers: dict[str, str] | None,
    ) -> None:
        mpv = self._require()
        options: dict[str, Any] = {"force-media-title": title}
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
        if not self.running:
            return
        await self._call(self._apply_upcoming)

    def _apply_upcoming(self) -> None:
        """Push the remembered list to mpv. Runs off the event loop."""
        mpv = self.mpv
        if mpv is None:
            return
        text = (
            compose_upcoming(
                self._upcoming,
                self.settings.overlay_font_size,
                self.settings.overlay_position,
            )
            if self._upcoming
            else ""
        )
        if text == self._overlay_text:
            return
        try:
            if text:
                mpv.command(
                    "osd-overlay", UPCOMING_OVERLAY_ID, "ass-events", text, 0, OVERLAY_RES_Y
                )
            else:
                # Format "none" unregisters the overlay outright. Passing
                # ass-events with empty data leaves an empty one behind, which
                # mpv keeps compositing for nothing.
                mpv.command("osd-overlay", UPCOMING_OVERLAY_ID, "none", "", 0, OVERLAY_RES_Y)
        except MPVError:
            # Cosmetic: a failure here must never interrupt playback.
            log.debug("could not update the upcoming overlay", exc_info=True)
            return
        self._overlay_text = text

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
