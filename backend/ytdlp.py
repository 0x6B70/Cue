"""Thin async wrapper around the yt-dlp *binary*.

yt-dlp is invoked as a subprocess rather than imported as a library so the
shipped copy can be replaced at runtime by :mod:`backend.updater`. YouTube
changes break extraction every few weeks; being able to drop in a new binary
without rebuilding the app is what keeps this working after release.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
from enum import Enum
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0

# The packaged backend is a windowed (console-less) build, so every console
# subprocess it starts gets a brand new console allocated -- a black window
# that flashes on screen for each search and each resolve. yt-dlp needs no
# console of its own; we only ever read its pipes.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class FailureKind(str, Enum):
    BOT_CHECK = "bot_check"
    AGE_RESTRICTED = "age_restricted"
    UNAVAILABLE = "unavailable"
    NETWORK = "network"
    NOT_FOUND = "not_found"
    DRM = "drm"
    NO_FORMATS = "no_formats"
    # A yt-dlp update removed or renamed a command-line flag we pass. Distinct
    # from every other kind because it is *our* invocation that is wrong, not
    # the video or the network, and it is recoverable by dropping the flag.
    BAD_OPTION = "bad_option"
    OTHER = "other"


_PATTERNS: list[tuple[FailureKind, re.Pattern[str]]] = [
    # First: an option yt-dlp no longer understands. argparse prints this and
    # exits before any network work, so the text never carries a video or
    # network error to be confused with -- but a dropped flag would otherwise
    # classify as OTHER and look like an unexplained total failure.
    (FailureKind.BAD_OPTION, re.compile(r"no such option|unrecognized arguments|invalid choice|unknown option", re.I)),
    # Checked before the bot pattern: YouTube phrases both as "Sign in to
    # confirm ...", so matching on that alone would call every age gate a bot
    # check and send the resolver down the wrong fallback path.
    (FailureKind.AGE_RESTRICTED, re.compile(r"age.?restricted|inappropriate for some users|confirm your age", re.I)),
    (FailureKind.BOT_CHECK, re.compile(r"not a bot|too many requests|http error 429", re.I)),
    (
        FailureKind.UNAVAILABLE,
        re.compile(r"video unavailable|private video|removed by the uploader|members-only|has been terminated", re.I),
    ),
    (
        FailureKind.NETWORK,
        re.compile(r"unable to download|urlopen error|temporary failure|connection reset|timed out|tunnel", re.I),
    ),
    # Both of the below are per-client verdicts, not verdicts on the video, so
    # the resolver must keep walking the chain. YouTube runs DRM and SABR-only
    # experiments per session and per client: `tv` can be DRM-locked while
    # `default` plays the same video fine.
    (FailureKind.DRM, re.compile(r"drm protected", re.I)),
    (
        FailureKind.NO_FORMATS,
        re.compile(r"requested format is not available|only images are available", re.I),
    ),
]


class YtDlpError(RuntimeError):
    """A yt-dlp invocation failed, classified so callers can react usefully."""

    def __init__(
        self,
        message: str,
        stderr: str = "",
        returncode: int | None = None,
        kind: FailureKind | None = None,
    ):
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode
        self.kind = kind or classify(f"{message}\n{stderr}")

    @property
    def user_message(self) -> str:
        return {
            FailureKind.BOT_CHECK: "YouTube asked us to confirm we're not a bot.",
            FailureKind.AGE_RESTRICTED: "This video is age-restricted and needs a signed-in account.",
            FailureKind.UNAVAILABLE: "This video is unavailable.",
            FailureKind.NETWORK: "Network problem reaching YouTube.",
            FailureKind.NOT_FOUND: "yt-dlp is missing.",
            FailureKind.DRM: "This video is DRM protected.",
            FailureKind.NO_FORMATS: "No playable format was offered for this video.",
            FailureKind.BAD_OPTION: "yt-dlp rejected an option; it may be mid-update.",
        }.get(self.kind, "Could not load this video.")

    @property
    def code(self) -> str:
        """Stable key for the UI to translate; see frontend/ui/i18n.js."""
        return f"error.{self.kind.value}"


def classify(text: str) -> FailureKind:
    for kind, pattern in _PATTERNS:
        if pattern.search(text):
            return kind
    return FailureKind.OTHER


class YtDlp:
    """Runs the yt-dlp binary and parses its JSON output."""

    def __init__(self, settings: Settings):
        self.settings = settings
        # Latched for the life of the process if yt-dlp rejects one of the
        # challenge-solver flags -- see js_runtime_args(). Dropping them makes
        # extraction worse (deprecated, sometimes images-only) but keeps yt-dlp
        # running, which beats every resolve dying on a parse error. A restart
        # tries them again, in case an update re-added the option or the user
        # rolled back.
        self._js_runtime_disabled = False

    @property
    def js_runtime_disabled(self) -> bool:
        """True once a bad-option failure forced the JS-runtime flags off."""
        return self._js_runtime_disabled

    def _binary(self) -> list[str]:
        path = self.settings.ytdlp_binary()
        if path:
            return [path]
        if getattr(sys, "frozen", False):
            # In a frozen build sys.executable is *this* application, so the
            # fallback below would relaunch the backend -- spawning a second
            # server per resolve rather than running yt-dlp. Fail cleanly and
            # let the updater fetch a real binary instead.
            raise YtDlpError(
                "yt-dlp binary not found", "no bundled or downloaded copy", kind=FailureKind.NOT_FOUND
            )
        # Development fallback: the module installed in the current interpreter.
        return [sys.executable, "-m", "yt_dlp"]

    def js_runtime_args(self) -> list[str]:
        """Flags that let yt-dlp solve YouTube's signature and n challenges.

        Both halves are required: a runtime alone still fails, because the
        challenge-solver script ships separately. Without them YouTube returns
        images-only or throttled formats, and yt-dlp warns that extraction
        without a runtime is deprecated.

        These have to be passed explicitly -- `--ignore-config` below means a
        runtime configured in the user's own yt-dlp config would be ignored.

        Returns nothing once :attr:`_js_runtime_disabled` latches, so a yt-dlp
        release that renames or drops one of these flags degrades gracefully
        instead of failing every resolve on a parse error.
        """
        if self._js_runtime_disabled:
            return []
        args: list[str] = []
        runtime = self.settings.js_runtime_binary()
        if runtime:
            # yt-dlp wants RUNTIME[:PATH], and RUNTIME has to name a runtime it
            # knows. Take it from the filename so pointing js_runtime_path at
            # node or bun still produces a label yt-dlp accepts, rather than
            # handing it a node binary labelled "deno".
            name = Path(runtime).stem.lower() or "deno"
            args += ["--js-runtimes", f"{name}:{runtime}"]
        if self.settings.ejs_remote_components:
            args += ["--remote-components", "ejs:github"]
        return args

    def base_args(self) -> list[str]:
        # --ignore-config keeps a user's global yt-dlp config from breaking us.
        # Warnings are deliberately *not* suppressed: they carry the only
        # explanation of why a client returned nothing playable, and the
        # resolver logs them when a tier fails.
        return [
            *self._binary(),
            "--ignore-config",
            "--no-progress",
            "--no-playlist",
            "--socket-timeout",
            "15",
            "--retries",
            "2",
            *self.js_runtime_args(),
        ]

    def cookie_args(self) -> list[str]:
        if self.settings.cookies_from_browser:
            return ["--cookies-from-browser", self.settings.cookies_from_browser]
        if self.settings.cookies_file and Path(self.settings.cookies_file).exists():
            return ["--cookies", self.settings.cookies_file]
        return []

    async def _exec(self, argv: list[str], timeout: float) -> tuple[bytes, str, int | None]:
        """Run a yt-dlp command line, returning (stdout, stderr, returncode)."""
        log.debug("yt-dlp %s", " ".join(argv[1:]))
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise YtDlpError("yt-dlp binary not found", str(exc), kind=FailureKind.NOT_FOUND) from exc

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise YtDlpError(f"yt-dlp timed out after {timeout:.0f}s") from None

        return stdout, stderr.decode("utf-8", "replace").strip(), proc.returncode

    async def run_json(self, args: list[str], timeout: float = DEFAULT_TIMEOUT) -> dict:
        """Run yt-dlp and parse a single JSON object from stdout."""
        stdout, err, returncode = await self._exec([*self.base_args(), *args], timeout)

        # A yt-dlp update that renamed or dropped one of the challenge-solver
        # flags makes *every* resolve fail here on a parse error, before any
        # network work. Rather than let that look like YouTube went down, drop
        # the flags and try once more; the retry -- and every later call --
        # runs without them because js_runtime_args() now returns nothing.
        if returncode != 0 and not self._js_runtime_disabled:
            if classify(err) is FailureKind.BAD_OPTION and self.js_runtime_args():
                log.warning(
                    "yt-dlp rejected an option; retrying without the JS-runtime flags. "
                    "Extraction is degraded until yt-dlp is updated. stderr: %s",
                    err[:300],
                )
                self._js_runtime_disabled = True
                stdout, err, returncode = await self._exec([*self.base_args(), *args], timeout)

        if returncode != 0:
            raise YtDlpError("yt-dlp exited non-zero", err, returncode)
        if not stdout.strip():
            raise YtDlpError("yt-dlp produced no output", err, returncode)
        if err:
            # Succeeded, but yt-dlp had something to say -- missing PO token,
            # a SABR experiment, formats skipped. Worth having when quality
            # silently drops.
            log.debug("yt-dlp warnings: %s", err)

        try:
            return json.loads(stdout.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise YtDlpError(f"could not parse yt-dlp output: {exc}", err) from exc

    async def version(self) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._binary(),
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except (FileNotFoundError, asyncio.TimeoutError, OSError, YtDlpError):
            return None
        return stdout.decode().strip() or None
