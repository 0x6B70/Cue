"""Paths, bundled-binary discovery and persisted user settings."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "Cue"


def user_data_dir() -> Path:
    """Per-platform writable directory for settings, queue state and updates."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def bundle_dir() -> Path:
    """Root of the bundle: the PyInstaller temp dir when frozen, else the repo."""
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        return Path(frozen)
    return Path(__file__).resolve().parent.parent


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


# Where Windows installers actually put these, because several of them never
# touch PATH. mpv is the important one: `winget install shinchiro.mpv` drops it
# in Program Files and adds nothing to PATH, so shutil.which misses it and the
# app ends up telling someone to install mpv that they have already installed.
_WINDOWS_HINTS: dict[str, tuple[str, ...]] = {
    "mpv": (
        r"%ProgramFiles%\MPV Player\mpv.exe",  # winget: shinchiro.mpv
        r"%ProgramFiles%\mpv\mpv.exe",
        r"%ProgramFiles(x86)%\mpv\mpv.exe",
        r"%LOCALAPPDATA%\Programs\mpv\mpv.exe",
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\mpv.exe",
        r"%USERPROFILE%\scoop\shims\mpv.exe",
        r"%USERPROFILE%\scoop\apps\mpv\current\mpv.exe",
        r"%ProgramData%\chocolatey\bin\mpv.exe",
    ),
    "deno": (
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\deno.exe",
        r"%USERPROFILE%\.deno\bin\deno.exe",
        r"%USERPROFILE%\scoop\shims\deno.exe",
    ),
    "ffmpeg": (
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe",
        r"%USERPROFILE%\scoop\shims\ffmpeg.exe",
        r"%ProgramData%\chocolatey\bin\ffmpeg.exe",
        r"%ProgramFiles%\ffmpeg\bin\ffmpeg.exe",
    ),
    "yt-dlp": (
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\yt-dlp.exe",
        r"%USERPROFILE%\scoop\shims\yt-dlp.exe",
        r"%ProgramData%\chocolatey\bin\yt-dlp.exe",
    ),
}


def _well_known(name: str) -> str | None:
    """Last resort: check the standard install locations on this platform."""
    if sys.platform != "win32":
        return None
    for hint in _WINDOWS_HINTS.get(name, ()):
        candidate = Path(os.path.expandvars(hint))
        if "%" in str(candidate):  # an env var that does not exist here
            continue
        if candidate.exists():
            log.info("found %s at %s (not on PATH)", name, candidate)
            return _usable(str(candidate))
    return None


def _usable(path: str | None) -> str | None:
    """Steer away from Windows launcher stubs, towards the real executable.

    Two kinds cause trouble:

    `.bat`/`.cmd` — CreateProcess cannot run these directly, so Windows wraps
    them in an implicit `cmd.exe /c`, which re-parses the whole command line
    for shell metacharacters. Our yt-dlp format selector contains `<`
    (`bv*[height<=?1080]`), which cmd reads as input redirection, and the call
    dies with "The filename, directory name, or volume label syntax is
    incorrect". Package managers really do ship yt-dlp this way.

    `.com` — mpv ships `mpv.com`, a console wrapper that re-executes `mpv.exe`
    as a child. PATHEXT puts `.COM` ahead of `.EXE`, so shutil.which finds the
    wrapper first; terminating it then leaves the real player orphaned. A .com
    is a legitimate executable, so it is only swapped when a sibling .exe
    exists, never rejected outright.
    """
    if not path or sys.platform != "win32":
        return path
    suffix = Path(path).suffix.lower()
    if suffix not in (".bat", ".cmd", ".com"):
        return path
    sibling = Path(path).with_suffix(".exe")
    if sibling.exists():
        return str(sibling)
    if suffix == ".com":
        return path
    log.warning("ignoring %s: a .bat/.cmd shim cannot be called safely on Windows", path)
    return None


def find_binary(name: str, override: str | None = None) -> str | None:
    """Locate a helper binary.

    Preference order: explicit override, the auto-updated copy in the user data
    dir, the copy shipped in the bundle, then whatever is on PATH.
    """
    if override:
        return _usable(override) if Path(override).exists() or shutil.which(override) else None

    candidates = [
        user_data_dir() / "bin" / _exe(name),
        bundle_dir() / "vendor" / _exe(name),
        bundle_dir() / "vendor" / sys.platform / _exe(name),
    ]
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    # PATH first, so a deliberately installed copy wins, then the places
    # installers drop things without announcing them.
    return _usable(shutil.which(name)) or _well_known(name)


@dataclass
class Settings:
    """User-tunable settings, persisted as JSON.

    `player_clients` and `cookies_*` exist because YouTube's bot checks shift
    over time: they are the knobs most likely to need changing without a
    rebuild. See docs/reliability.md.
    """

    max_height: int = 1080
    volume: int = 100
    locale: str = "en"
    # Tier 2 of the resolver chain walks this list. "default" means "whatever
    # yt-dlp picks on its own". Measured against live YouTube (October 2026):
    # `web_embedded`, `tv_simply` and `mweb` all return URLs mpv plays;
    # `default` now resolves via ANDROID_VR, whose URLs 403 on the open-ended
    # range mpv requests, so it goes after the clients that work (the
    # playability probe catches it either way -- see verify_streams). `tv`
    # (DRM experiment), `ios` and `web_safari` (images only) fail outright and
    # are deliberately absent. Re-measure before changing.
    player_clients: list[str] = field(
        default_factory=lambda: ["web_embedded", "tv_simply", "default", "mweb"]
    )
    # Clients the minted gvs PO token is offered to.
    #
    # `mweb` is deliberately excluded even though yt-dlp asks it for a token by
    # name: with one it returns SABR-style URLs that reject an open-ended
    # `Range: bytes=0-` with 403, which is exactly what mpv sends. That tier
    # resolves "successfully" and then fails playback a second later -- worse
    # than not running it at all. `tv_simply` returns ordinary progressive URLs
    # and plays. Verify *playback*, not just resolution, before adding a client.
    potoken_clients: list[str] = field(default_factory=lambda: ["tv_simply"])
    cookies_from_browser: str | None = None
    cookies_file: str | None = None
    invidious_fallback: bool = True
    invidious_instances: list[str] = field(
        default_factory=lambda: ["https://yewtu.be", "https://invidious.nerdvpn.de", "https://inv.nadeko.net"]
    )
    sponsorblock_enabled: bool = True
    sponsorblock_categories: list[str] = field(
        default_factory=lambda: ["sponsor", "selfpromo", "interaction", "intro", "outro"]
    )
    ytdlp_auto_update: bool = True
    # "stable" tracks yt-dlp's releases; "nightly" tracks the nightly-builds
    # repo, which carries YouTube fixes days earlier at the cost of less
    # testing. Worth switching to when extraction breaks and a fix has not
    # reached a stable release yet.
    ytdlp_channel: str = "stable"
    ytdlp_path: str | None = None
    mpv_path: str | None = None
    # yt-dlp needs a JS runtime to solve YouTube's signature and n challenges;
    # without one, extraction is deprecated and formats go missing. Deno is the
    # only runtime yt-dlp enables by default.
    js_runtime_path: str | None = None
    # The challenge-solver script is distributed separately from the runtime.
    # Without it a runtime alone still fails to solve anything.
    ejs_remote_components: bool = True
    # Merged into mpv's command line, e.g. {"hwdec": "auto-safe", "vo": "gpu"}.
    # Keys use mpv's option names with underscores for dashes.
    mpv_options: dict = field(default_factory=dict)
    # Before trusting a resolved stream, request it the way mpv will and drop
    # the tier if YouTube refuses. Costs one status line per resolve; without
    # it a tier whose URLs 403 in the player still counts as working.
    verify_streams: bool = True
    prefetch_ahead: int = 1
    # Download upcoming videos to disk so they play even if YouTube later blocks
    # extraction -- the one thing that survives any upstream change. On by
    # default because reliability is the point of this app, but it uses disk and
    # doubles the bandwidth of what it caches, so it is a visible toggle.
    offline_cache: bool = True
    # How many upcoming items to cache. Kept as shallow as prefetch for the same
    # reason: downloading the whole queue at once looks like bulk scraping.
    cache_ahead: int = 1
    # Ceiling on the cache, in gigabytes. Least-recently-used items are evicted
    # once it is exceeded, played ones first.
    cache_budget_gb: float = 5.0
    # Draw the next few queued titles inside the mpv window, for whoever is
    # watching the video rather than the queue panel. Anything drawn on top of
    # the picture is a matter of taste, so it can be turned off.
    show_upcoming_overlay: bool = True
    # Point size of the "up next" overlay, measured on a 720px-tall virtual
    # canvas so it scales with the window rather than being a fixed pixel size.
    # Roughly 3-13% of the video height across its 10-96 range; 28 is the
    # long-standing default. Bounds are enforced by PUT /api/settings.
    overlay_font_size: int = 28
    # Corner or edge the overlay sits in, e.g. "bottom-left", "top-right". The
    # full set of choices is player.OVERLAY_ALIGNMENTS; PUT /api/settings rejects
    # anything outside it.
    overlay_position: str = "bottom-right"
    # "bar" draws a karaoke-style strip of numbered boxes along the bottom (or
    # top, following overlay_position) edge; "list" is the original corner
    # list. See backend/overlay.py.
    overlay_style: str = "bar"
    # What each upcoming entry shows: "title", "singer" or "both". An entry
    # with no singer name always falls back to its title.
    overlay_label: str = "both"
    # Number of boxes in the bar, 1-6.
    overlay_boxes: int = 3
    # Text at the left of the bar and on top of the intermission card, e.g. a
    # karaoke night's name. Empty draws nothing.
    overlay_brand: str = ""
    # Replace the bar / intermission headings. Empty uses the localised default
    # ("NEXT UP", "NEXT ON STAGE").
    overlay_bar_heading: str = ""
    overlay_stage_heading: str = ""
    # Time left in the song, in a box at the left of the bar.
    overlay_show_timer: bool = True
    overlay_accent_color: str = "#a78bfa"
    overlay_bar_color: str = "#14151d"
    overlay_box_color: str = "#3b3f52"
    # Full-screen "next on stage" card with a countdown, shown during the
    # pause between songs (gap_seconds). Needs a gap to have anything to show.
    show_intermission: bool = True
    intermission_color: str = "#5b3f78"
    # [width, height] of the uploaded logo (user_data_dir/logo.bgra), or None.
    # Drawn at the left of the bar and on top of the intermission card. The UI
    # decodes the image and uploads raw pixels, so no imaging library is needed.
    overlay_logo_size: list | None = None
    # Where saved videos go. None means <home>/Videos/Cue.
    download_dir: str | None = None
    # More folders whose videos are listed, searched and queued alongside the
    # download folder, which is always included. See backend/localfiles.py.
    library_folders: list[str] = field(default_factory=list)
    # "video" saves picture and sound; "audio" saves the sound alone, at full
    # quality, with no ffmpeg needed.
    download_format: str = "video"
    # Save every song to download_dir once it has played to the end.
    download_played: bool = False
    # ffmpeg merges the separate best video and audio streams into one file.
    # Without it a video save falls back to the single-file format YouTube
    # offers, usually 360p.
    ffmpeg_path: str | None = None
    # Start each session with an empty queue. Only applies to a clean exit --
    # if the app is killed outright, nothing gets the chance to run.
    clear_queue_on_exit: bool = True
    # Quiet pause between videos, in seconds. Applies only when one ends on its
    # own -- a manual Next stays immediate.
    gap_seconds: float = 0.0
    # Remembered so the next session starts on the strategy that last worked.
    last_successful_tier: str | None = None

    @property
    def path(self) -> Path:
        return user_data_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        path = user_data_dir() / "settings.json"
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("settings.json unreadable, falling back to defaults", exc_info=True)
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def ytdlp_binary(self) -> str | None:
        return find_binary("yt-dlp", self.ytdlp_path)

    def mpv_binary(self) -> str | None:
        return find_binary("mpv", self.mpv_path)

    def js_runtime_binary(self) -> str | None:
        return find_binary("deno", self.js_runtime_path)

    def ffmpeg_binary(self) -> str | None:
        return find_binary("ffmpeg", self.ffmpeg_path)

    def download_directory(self) -> Path:
        if self.download_dir:
            return Path(os.path.expandvars(os.path.expanduser(self.download_dir)))
        return Path.home() / "Videos" / "Cue"
