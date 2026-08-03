"""Video ID -> playable stream URLs, via a chain of fallback strategies.

No single extraction path stays reliable, so this tries several in order and
remembers which one worked. The design goal is not "never fail" but "never
hang and never fail silently": an exhausted chain raises, the engine marks the
item failed and moves on.

Tier order:
  1. potoken   - PO token minted in Electron's Chromium + a stable session
  2. client:*  - alternate InnerTube clients (tv, web_embedded, ios, ...)
  3. cookies   - a signed-in session, also the only route to age-restricted
  4. invidious - public instances, slow and flaky but sometimes the only one up
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .config import Settings
from .invidious import InvidiousClient, InvidiousError
from .potoken import PoTokenStore
from .ytdlp import FailureKind, YtDlp, YtDlpError

log = logging.getLogger(__name__)

# googlevideo URLs are good for roughly six hours; expire ours well before that
# so playback never starts against a dead link.
STREAM_TTL_SECONDS = 3 * 3600


@dataclass
class ResolvedStream:
    video_id: str
    video_url: str
    audio_url: str | None
    title: str
    duration: float | None
    tier: str
    # Some InnerTube clients hand back streams that only serve with the
    # matching User-Agent, so the headers travel with the URLs.
    headers: dict[str, str] = field(default_factory=dict)
    resolved_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() - self.resolved_at > STREAM_TTL_SECONDS


class ResolveError(RuntimeError):
    def __init__(self, message: str, kind: FailureKind = FailureKind.OTHER, attempts: list[str] | None = None):
        super().__init__(message)
        self.kind = kind
        self.attempts = attempts or []

    @property
    def code(self) -> str:
        """Stable key for the UI to translate; see frontend/ui/i18n.js."""
        return _code_for(self.kind)


@dataclass
class Strategy:
    name: str
    extra_args: list[str]


def split_formats(info: dict) -> tuple[str | None, str | None]:
    """Pull (video_url, audio_url) out of a yt-dlp info dict.

    Written defensively: yt-dlp populates `requested_formats` when it merges
    separate streams, falls back to a flat `url` for muxed formats, and older
    or unusual extractions may only carry a `formats` list.
    """
    requested = info.get("requested_formats")
    if requested:
        video_url = audio_url = None
        for fmt in requested:
            url = fmt.get("url")
            if not url:
                continue
            has_video = fmt.get("vcodec", "none") != "none"
            has_audio = fmt.get("acodec", "none") != "none"
            if has_video and video_url is None:
                video_url = url
            elif has_audio and audio_url is None:
                audio_url = url
        if video_url:
            return video_url, audio_url

    url = info.get("url")
    if url and "\n" not in url:
        return url, None

    formats = [f for f in info.get("formats") or [] if f.get("url")]
    muxed = [f for f in formats if f.get("vcodec", "none") != "none" and f.get("acodec", "none") != "none"]
    if muxed:
        return max(muxed, key=lambda f: int(f.get("height") or 0))["url"], None
    if formats:
        return formats[-1]["url"], None
    return None, None


def extract_headers(info: dict) -> dict[str, str]:
    """HTTP headers the stream URLs need, if the extractor supplied any."""
    requested = info.get("requested_formats") or []
    for fmt in requested:
        headers = fmt.get("http_headers")
        if headers:
            return dict(headers)
    return dict(info.get("http_headers") or {})


class Resolver:
    def __init__(self, ytdlp: YtDlp, settings: Settings, tokens: PoTokenStore):
        self.ytdlp = ytdlp
        self.settings = settings
        self.tokens = tokens
        self._cache: dict[str, ResolvedStream] = {}
        # Last outcome per tier, surfaced by /api/diagnostics. A tier that
        # never succeeds is invisible otherwise: once one works it is sorted
        # first and the others stop being tried at all.
        self.tier_health: dict[str, str] = {}

    # ------------------------------------------------------------ strategies

    def strategies(self) -> list[Strategy]:
        chain: list[Strategy] = []

        bundle = self.tokens.get()
        if bundle:
            # One strategy per client that actually consumes a gvs token.
            # yt-dlp names the client in the token label ("mweb.gvs+..."), so a
            # token minted for the wrong client is silently useless -- which is
            # what the old single `web` strategy did: `web` serves no formats
            # at all, so the tier could never succeed.
            for client in self.settings.potoken_clients:
                chain.append(Strategy(f"potoken:{client}", bundle.extractor_args(client)))

        for client in self.settings.player_clients:
            if client == "default":
                chain.append(Strategy("client:default", []))
            else:
                chain.append(Strategy(f"client:{client}", ["--extractor-args", f"youtube:player_client={client}"]))

        cookie_args = self.ytdlp.cookie_args()
        if cookie_args:
            chain.append(Strategy("cookies", cookie_args))

        # Start on whatever worked last time, keeping the rest of the order.
        last = self.settings.last_successful_tier
        if last:
            chain.sort(key=lambda s: s.name != last)
        return chain

    def format_selector(self) -> str:
        height = self.settings.max_height
        return f"bv*[height<=?{height}]+ba/b[height<=?{height}]/b"

    # --------------------------------------------------------------- resolve

    async def resolve(self, video_id: str, force: bool = False) -> ResolvedStream:
        cached = self._cache.get(video_id)
        if cached and not cached.expired and not force:
            return cached

        url = f"https://www.youtube.com/watch?v={video_id}"
        attempts: list[str] = []
        last_kind = FailureKind.OTHER

        for strategy in self.strategies():
            try:
                info = await self.ytdlp.run_json(
                    ["-J", "-f", self.format_selector(), *strategy.extra_args, url],
                    timeout=60,
                )
            except YtDlpError as exc:
                attempts.append(f"{strategy.name}: {exc.kind.value}")
                last_kind = exc.kind
                self.tier_health[strategy.name] = exc.kind.value
                # A removed or private video will not be fixed by another
                # client, a token or a cookie jar. Stop wasting time. Note DRM
                # and no-formats are *not* in this class: they are per-client
                # verdicts and the next tier may well succeed.
                if exc.kind is FailureKind.UNAVAILABLE:
                    raise ResolveError(exc.user_message, exc.kind, attempts) from exc
                # yt-dlp's stderr is the only thing that explains *why* a client
                # came back empty ("requires a GVS PO Token", "SABR-only
                # experiment", "DRM"). Without it a dead tier looks like a
                # generic failure forever.
                log.info(
                    "resolve %s: tier %s failed (%s): %s",
                    video_id,
                    strategy.name,
                    exc.kind.value,
                    _summarize(exc.stderr),
                )
                continue

            video_url, audio_url = split_formats(info)
            if not video_url:
                attempts.append(f"{strategy.name}: no playable format")
                self.tier_health[strategy.name] = "no_playable_format"
                continue

            stream = ResolvedStream(
                video_id=video_id,
                video_url=video_url,
                audio_url=audio_url,
                title=info.get("title") or video_id,
                duration=info.get("duration"),
                tier=strategy.name,
                headers=extract_headers(info),
            )
            self._remember(stream)
            return stream

        if self.settings.invidious_fallback:
            try:
                stream = await self._resolve_invidious(video_id)
                self._remember(stream)
                return stream
            except InvidiousError as exc:
                attempts.append(f"invidious: {exc}")

        raise ResolveError(_message_for(last_kind), last_kind, attempts)

    async def _resolve_invidious(self, video_id: str) -> ResolvedStream:
        client = InvidiousClient(self.settings.invidious_instances)
        payload = await client.fetch(video_id)
        duration = payload.get("duration")
        return ResolvedStream(
            video_id=video_id,
            video_url=payload["video_url"],
            audio_url=payload.get("audio_url"),
            title=payload.get("title") or video_id,
            duration=float(duration) if duration else None,
            tier="invidious",
        )

    def _remember(self, stream: ResolvedStream) -> None:
        self._cache[stream.video_id] = stream
        self.tier_health[stream.tier] = "ok"
        if self.settings.last_successful_tier != stream.tier:
            self.settings.last_successful_tier = stream.tier
            self.settings.save()
        log.info("resolved %s via %s", stream.video_id, stream.tier)

    def invalidate(self, video_id: str) -> None:
        """Drop a cached stream — call this when playback fails mid-flight."""
        self._cache.pop(video_id, None)

    def invalidate_all(self) -> None:
        """Drop every cached stream — e.g. after yt-dlp is updated, so retries
        use the new binary rather than URLs the old one produced."""
        self._cache.clear()

    def prune(self) -> None:
        for video_id in [k for k, v in self._cache.items() if v.expired]:
            del self._cache[video_id]


def _summarize(stderr: str, limit: int = 300) -> str:
    """Condense yt-dlp stderr to the lines that explain a failure."""
    if not stderr:
        return "(no stderr)"
    interesting = [
        line.strip()
        for line in stderr.splitlines()
        if line.strip().startswith(("ERROR", "WARNING"))
    ]
    text = " | ".join(interesting or stderr.splitlines())
    return text[:limit] + ("…" if len(text) > limit else "")


def _message_for(kind: FailureKind) -> str:
    return {
        FailureKind.BOT_CHECK: (
            "YouTube's bot check blocked every method. Try again shortly, or add cookies in Settings."
        ),
        FailureKind.AGE_RESTRICTED: "Age-restricted — add browser cookies in Settings to play this.",
        FailureKind.NETWORK: "Could not reach YouTube. Check your connection.",
        FailureKind.NOT_FOUND: "yt-dlp is missing — reinstall the app.",
        FailureKind.DRM: "This video is DRM protected and cannot be played.",
        FailureKind.NO_FORMATS: "No playable format was offered for this video.",
        FailureKind.BAD_OPTION: (
            "yt-dlp rejected one of our options — it may be mid-update. This usually clears once yt-dlp updates."
        ),
    }.get(kind, "Could not find a playable stream for this video.")


def _code_for(kind: FailureKind) -> str:
    """Stable key for the UI to translate; see frontend/ui/i18n.js."""
    return f"resolve.{kind.value}"
