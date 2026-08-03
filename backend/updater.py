"""Keeps the bundled yt-dlp current.

YouTube ships changes that break extraction every few weeks. Without this the
app works on release day and dies quietly a month later, so the updater runs
in the background at startup and drops a new binary into the user data dir,
which :func:`backend.config.find_binary` prefers over the bundled copy.

Failure is always non-fatal: if the update cannot be fetched we keep running
on whatever we already have.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .config import Settings, user_data_dir

log = logging.getLogger(__name__)

# The packaged backend is windowed, so a console subprocess would flash a black
# window. yt-dlp needs no console; we only read its pipes.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

STABLE_REPO = "yt-dlp/yt-dlp"
NIGHTLY_REPO = "yt-dlp/yt-dlp-nightly-builds"
# Kept for callers/tests that import it; the stable channel's endpoint.
RELEASE_API = f"https://api.github.com/repos/{STABLE_REPO}/releases/latest"
CHECK_INTERVAL_SECONDS = 12 * 3600
DOWNLOAD_TIMEOUT = 180.0
# Startup is busy enough; let the UI settle before spending bandwidth.
STARTUP_DELAY_SECONDS = 20.0


def release_api(settings: Settings) -> str:
    """The releases endpoint for the configured channel."""
    repo = NIGHTLY_REPO if getattr(settings, "ytdlp_channel", "stable") == "nightly" else STABLE_REPO
    return f"https://api.github.com/repos/{repo}/releases/latest"

ASSET_NAMES = {
    "win32": "yt-dlp.exe",
    "darwin": "yt-dlp_macos",
}
DEFAULT_ASSET = "yt-dlp"


def _state_path() -> Path:
    return user_data_dir() / "update-state.json"


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state: dict) -> None:
    try:
        _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        log.debug("could not persist update state", exc_info=True)


def target_path() -> Path:
    directory = user_data_dir() / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    name = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
    return directory / name


def asset_name() -> str:
    return ASSET_NAMES.get(sys.platform, DEFAULT_ASSET)


def due_for_check(state: dict, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    return now - float(state.get("last_check", 0)) >= CHECK_INTERVAL_SECONDS


async def maybe_update_ytdlp(
    settings: Settings, delay: float = STARTUP_DELAY_SECONDS, force: bool = False
) -> bool:
    """Check for and install a newer yt-dlp. Returns True if one was installed.

    `force` bypasses the 12-hour interval for a reactive check triggered by a
    resolve failure; the caller is responsible for its own rate limiting.
    """
    if delay:
        await asyncio.sleep(delay)

    state = _read_state()
    # Having no binary at all is not something to defer for twelve hours --
    # nothing works without one. This matters for the packaged build, which
    # ships without yt-dlp: a state file left behind by a dev run (where the
    # pip-installed module counts as "current") would otherwise convince it
    # there is nothing to fetch.
    have_binary = settings.ytdlp_binary() is not None
    if have_binary and not force and not due_for_check(state):
        log.debug("yt-dlp update check skipped (checked recently)")
        return False
    if not have_binary:
        log.info("no yt-dlp binary present; fetching one regardless of the check interval")

    state["last_check"] = time.time()
    try:
        installed = await _run_update(settings, state)
    except Exception:  # noqa: BLE001 - never let the updater take the app down
        log.info("yt-dlp update check failed", exc_info=True)
        installed = False
    _write_state(state)
    return installed


async def _run_update(settings: Settings, state: dict) -> bool:
    channel = getattr(settings, "ytdlp_channel", "stable")
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        response = await client.get(release_api(settings), headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        release = response.json()

        latest = release.get("tag_name")
        if not latest:
            return False

        # Both shortcuts below say "we already have this version", which is
        # only meaningful when a real binary exists. In development the
        # pip-installed module reports a version while `ytdlp_binary()` is
        # None, and recording that as installed is what leaves the packaged
        # build with nothing to run. The channel must also match: switching
        # stable<->nightly is a real change even if the version string collides.
        have_binary = settings.ytdlp_binary() is not None
        same_channel = state.get("channel") == channel
        if have_binary and same_channel and state.get("installed_version") == latest:
            log.debug("yt-dlp already at %s (%s)", latest, channel)
            return False

        from .ytdlp import YtDlp

        current = await YtDlp(settings).version()
        if have_binary and same_channel and current and current == latest:
            state["installed_version"] = latest
            state["channel"] = channel
            return False

        wanted = asset_name()
        url = next(
            (a.get("browser_download_url") for a in release.get("assets", []) if a.get("name") == wanted),
            None,
        )
        if not url:
            log.info("no yt-dlp asset named %s in release %s", wanted, latest)
            return False

        log.info("updating yt-dlp %s -> %s", current or "unknown", latest)
        binary = await client.get(url, timeout=DOWNLOAD_TIMEOUT)
        binary.raise_for_status()
        content = binary.content

    if len(content) < 100_000:
        log.warning("downloaded yt-dlp looks truncated (%d bytes); ignoring", len(content))
        return False

    destination = target_path()
    backup = _install_binary(content, destination)

    # A self-updating app can update itself into being broken. Before trusting
    # the new binary, make sure it at least runs -- a truncated or wrong-arch
    # download passes the size check above but cannot execute. If it fails, put
    # the copy we replaced back so the app keeps working on what it had.
    if not await _runs_ok(destination):
        log.warning("newly installed yt-dlp failed its smoke test; rolling back")
        _restore(destination, backup)
        return False

    # `.previous` is kept on success too: the smoke test proves the binary
    # *runs*, not that this release still extracts from YouTube. rollback()
    # undoes it if a resolve fails right after the update.
    state["installed_version"] = latest
    state["channel"] = channel
    log.info("yt-dlp updated to %s at %s (%s)", latest, destination, channel)
    return True


def previous_path() -> Path:
    return target_path().with_suffix(".previous")


def _install_binary(content: bytes, destination: Path) -> Path | None:
    """Write the new binary, preserving the current one as `.previous`.

    Returns the backup path when there was a copy to keep, else None (the very
    first update on a packaged build, which ships nothing in the user dir).
    """
    tmp = destination.with_suffix(".part")
    tmp.write_bytes(content)
    tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    backup = previous_path()
    had_previous = destination.exists()
    if had_previous:
        os.replace(destination, backup)
    os.replace(tmp, destination)
    return backup if had_previous else None


def _restore(destination: Path, backup: Path | None) -> None:
    """Undo _install_binary: put the old copy back, or remove the new one.

    Removing it (when there was no previous) is deliberate -- find_binary then
    falls through to the bundled copy rather than a binary we do not trust.
    """
    if backup is not None and backup.exists():
        os.replace(backup, destination)
    else:
        destination.unlink(missing_ok=True)


async def _runs_ok(path: Path) -> bool:
    """True if the binary executes and prints a version."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(path),
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (OSError, asyncio.TimeoutError):
        return False
    return proc.returncode == 0 and bool(stdout.strip())


def rollback() -> bool:
    """Revert the last yt-dlp update. Returns True if anything changed.

    Restores the `.previous` binary if one was kept, otherwise removes the
    user-dir copy so find_binary falls back to the bundled yt-dlp. For the case
    where a fresh release resolves and plays *worse* than the one it replaced.
    """
    destination = target_path()
    backup = previous_path()
    if backup.exists():
        os.replace(backup, destination)
        log.info("rolled yt-dlp back to the previous binary")
        return True
    if destination.exists():
        destination.unlink()
        log.info("removed the updated yt-dlp; falling back to the bundled copy")
        return True
    return False
