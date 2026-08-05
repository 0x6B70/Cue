"""Orchestration: queue + resolver + mpv + SponsorBlock.

Everything that has to happen in a particular order lives here, so the API
layer stays a thin translation of HTTP into these calls.

The guiding rule is that a broken video must never stop the queue. A failed
resolve marks the item and moves on; a mid-playback failure re-resolves once
(the URL may simply have expired) before giving up.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from .cache import OfflineCache
from .config import Settings, user_data_dir
from .models import AddRequest, AppState, ItemState, PlaybackState, PlayerStatus, QueueItem, SearchResult
from .player import MpvPlayer, PlayerUnavailable
from .playqueue import PlayQueue
from .potoken import PoTokenStore
from .resolver import ResolveError, Resolver
from .search import SearchService
from .sponsorblock import Segment, fetch_segments
from .updater import maybe_update_ytdlp
from .ytdlp import FailureKind, YtDlp

log = logging.getLogger(__name__)

# Ceiling for the inter-video pause. Long enough for any real use, short
# enough that a typo cannot look like the queue has hung forever.
MAX_GAP_SECONDS = 3600.0

# How many titles the in-video overlay lists. Enough to see where the queue is
# going, few enough to stay out of the picture.
UPCOMING_OVERLAY_COUNT = 3

# Failures a newer yt-dlp could plausibly fix, so a total-chain failure of one
# of these kinds is worth a reactive update check. The rest are pointless to
# update for: the video is gone (UNAVAILABLE), the network is down (NETWORK),
# or the fix is cookies, not code (AGE_RESTRICTED).
_YTDLP_FIXABLE = frozenset(
    {FailureKind.BOT_CHECK, FailureKind.NO_FORMATS, FailureKind.DRM, FailureKind.BAD_OPTION, FailureKind.OTHER}
)

# A reactive update runs at most this often, whatever fails in between. GitHub
# rate limits, and hammering it after every failed resolve helps no one.
REACTIVE_UPDATE_MIN_INTERVAL = 3600.0


class Engine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ytdlp = YtDlp(settings)
        self.search_service = SearchService(self.ytdlp)
        self.tokens = PoTokenStore()
        self.resolver = Resolver(self.ytdlp, settings, self.tokens)
        self.queue = PlayQueue(user_data_dir() / "queue.json")
        self.cache = OfflineCache(settings, user_data_dir() / "cache")
        self.player: MpvPlayer | None = None
        self.notice: str | None = None
        self.notice_code: str | None = None
        self.notice_params: dict[str, str] = {}
        self.on_change: Callable[[], None] = lambda: None

        self._segments: list[Segment] = []
        self._skipped: set[int] = set()
        self._advancing = False
        # Set synchronously the moment playback is decided on. Resolving is
        # async, so without this two quick adds would both see an idle player
        # and race to start.
        self._playback_pending = False
        self._retried_uid: str | None = None
        # Monotonic clock of the last reactive yt-dlp update, so a run of
        # failures triggers at most one check per REACTIVE_UPDATE_MIN_INTERVAL.
        # -inf, not 0.0: time.monotonic() is uptime-relative and can be well
        # under the interval on a freshly-booted machine, which would wrongly
        # rate-limit the very first check.
        self._last_reactive_update = float("-inf")
        # None rather than [] so the first notify() always pushes, even when
        # the queue is empty and the answer is "clear it".
        self._overlay_titles: list[str] | None = None
        self._background: set[asyncio.Task] = set()
        # The pending inter-video pause, kept so a manual action can cut it
        # short instead of leaving the user staring at a stopped player.
        self._gap_task: asyncio.Task | None = None

    # ------------------------------------------------------------- lifecycle

    async def start(self, open_player: bool = True) -> None:
        loop = asyncio.get_running_loop()
        self.player = MpvPlayer(self.settings, loop)
        self.player.on_end_file = self._on_end_file
        self.player.on_status_change = self._on_status_change
        self.player.on_window_closed = self._on_window_closed
        self.queue.load()

        if open_player:
            try:
                await self.player.ensure_started()
            except PlayerUnavailable as exc:
                # Not fatal: search and queueing still work, and the window can
                # be opened later once mpv is installed.
                self._set_notice(str(exc), "notice.player_unavailable", detail=str(exc))
                log.warning("player unavailable at startup: %s", exc)

        # A missing JS runtime is the single most common cause of silent
        # breakage (see docs/reliability.md) -- extraction is deprecated and
        # formats quietly go missing. It was only visible via /api/diagnostics;
        # surface it, but never over a more urgent player/mpv problem.
        if self.notice is None and self.settings.js_runtime_binary() is None:
            self._set_notice(
                "No JavaScript runtime found (install Deno). Playback may be unreliable until it is set up.",
                "notice.js_runtime_missing",
            )
            log.warning("no JS runtime found at startup; extraction will be degraded")

    async def shutdown(self) -> None:
        for task in list(self._background):
            task.cancel()
        if self.player:
            self.player.shutdown()
        if self.settings.clear_queue_on_exit:
            # Next launch starts empty. clear() persists, so the emptied queue
            # is what gets written -- no stale list survives to be reloaded.
            self.queue.clear()
        else:
            self.queue.save()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def notify(self) -> None:
        self._sync_upcoming_overlay()
        self.on_change()

    def _upcoming_titles(self) -> list[str]:
        if not self.settings.show_upcoming_overlay:
            return []
        return [item.title for item in self.queue.upcoming(UPCOMING_OVERLAY_COUNT)]

    def invalidate_overlay(self) -> None:
        """Force the next notify() to redraw the "up next" list.

        _sync_upcoming_overlay diffs on the titles alone, so a settings change
        that alters only the overlay's appearance -- its font size -- would not
        otherwise reach mpv until the queue next changed shape.
        """
        self._overlay_titles = None

    def _sync_upcoming_overlay(self) -> None:
        """Keep the in-video "up next" list in step with the queue.

        Hung off notify() so every mutation is covered without a polling loop.
        notify() also fires on each time-pos update, several times a second, so
        the push is skipped unless the titles actually changed -- otherwise
        this would be a steady drip of pointless IPC for the whole video.
        """
        if not self.player:
            return
        titles = self._upcoming_titles()
        if titles == self._overlay_titles:
            return
        self._overlay_titles = titles
        self._spawn(self.player.set_upcoming(titles))

    def _background_fetch(self) -> None:
        """Resolve upcoming items ahead of time, and cache them if enabled.

        Spawned wherever the queue changes shape, so both run without polling.
        """
        self._spawn(self._prefetch())
        if self.cache.enabled():
            self._spawn(self._precache())

    def _set_notice(self, text: str, code: str | None = None, **params: str) -> None:
        """Record a user-facing notice as both English text and a translatable key."""
        self.notice = text
        self.notice_code = code
        self.notice_params = {k: str(v) for k, v in params.items()}

    def _clear_notice(self) -> None:
        self.notice = None
        self.notice_code = None
        self.notice_params = {}

    # ----------------------------------------------------------------- state

    def state(self) -> AppState:
        return AppState(
            items=self.queue.items,
            current=self.queue.current,
            player=self.player.status if self.player else PlayerStatus(),
            notice=self.notice,
            notice_code=self.notice_code,
            notice_params=self.notice_params,
        )

    # ---------------------------------------------------------------- search

    async def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        return await self.search_service.search(query, limit)

    # ----------------------------------------------------------------- queue

    async def add(self, request: AddRequest) -> QueueItem:
        item = QueueItem(
            video_id=request.video_id,
            title=request.title or request.video_id,
            channel=request.channel,
            duration=request.duration,
            thumbnail=request.thumbnail or f"https://i.ytimg.com/vi/{request.video_id}/mqdefault.jpg",
        )
        index = self.queue.add(item, play_now=request.play_now)

        idle = not self._is_active()
        if request.play_now or idle:
            self.queue.select(index)
            self._playback_pending = True
            self._spawn(self._start_playback())
        else:
            self._background_fetch()
        self.notify()
        return item

    async def remove(self, index: int) -> bool:
        playing = self.queue.current == index
        removed = self.queue.remove(index)
        if removed and playing:
            await self.next()
        self.notify()
        return removed is not None

    async def reorder(self, from_index: int, to_index: int) -> bool:
        ok = self.queue.reorder(from_index, to_index)
        self.notify()
        return ok

    async def clear(self) -> None:
        self._cancel_gap()
        self.queue.clear()
        if self.player:
            await self.player.stop()
        self._segments = []
        self.notify()

    async def clear_played(self) -> int:
        removed = self.queue.clear_played()
        self.notify()
        return removed

    # -------------------------------------------------------------- playback

    def _is_active(self) -> bool:
        if self._playback_pending:
            return True
        if not self.player:
            return False
        return self.player.status.state in (PlaybackState.PLAYING, PlaybackState.PAUSED, PlaybackState.LOADING)

    async def _start_playback(self) -> None:
        """Play the selected item, falling through to the next if it fails."""
        try:
            if not await self._play_current():
                await self._advance(mark_played=False)
        finally:
            self._playback_pending = False

    async def play_index(self, index: int) -> bool:
        self._cancel_gap()
        if self.queue.select(index) is None:
            return False
        self._playback_pending = True
        await self._start_playback()
        return True

    async def next(self) -> None:
        self._cancel_gap()
        await self._advance(mark_played=True)

    async def previous(self) -> None:
        self._cancel_gap()
        if self.queue.previous() is None:
            return
        await self._play_current()

    async def toggle_pause(self) -> None:
        if self.player:
            await self.player.toggle_pause()
            self.notify()

    async def resume(self) -> None:
        if self.player:
            await self.player.set_paused(False)
            self.notify()

    async def pause(self) -> None:
        if self.player:
            await self.player.set_paused(True)
            self.notify()

    async def stop(self) -> None:
        self._cancel_gap()
        if self.player:
            await self.player.stop()
        self._segments = []
        self.notify()

    async def seek(self, seconds: float, absolute: bool = True) -> None:
        if self.player:
            await self.player.seek(seconds, absolute)

    async def set_volume(self, level: int) -> None:
        if self.player:
            await self.player.set_volume(level)
            self.notify()

    async def open_player_window(self) -> None:
        """Re-open the mpv window after the user closed it."""
        if not self.player:
            return
        await self.player.ensure_started()
        self._clear_notice()
        current = self.queue.current_item
        if current:
            await self._play_current()
        self.notify()

    # ------------------------------------------------------------- internals

    async def _play_current(self) -> bool:
        item = self.queue.current_item
        if not item or not self.player:
            return False

        self._segments = []
        self._skipped.clear()
        item.error = None
        item.error_code = None
        item.state = ItemState.PENDING
        self.notify()

        # Prefer a cached copy: no resolve, no network, immune to YouTube being
        # down. Falls through to a normal resolve if the player is unavailable.
        if self.cache.enabled():
            cached = self.cache.paths(item.video_id)
            if cached and await self._play_cached(item, *cached):
                return True

        try:
            stream = await self.resolver.resolve(item.video_id)
        except ResolveError as exc:
            item.state = ItemState.FAILED
            item.error = str(exc)
            item.error_code = exc.code
            # `reason` is English prose. `reason_code` lets the UI substitute a
            # translated reason instead, so the notice is not half-translated.
            self._set_notice(
                f"{item.title}: {exc}",
                "notice.resolve_failed",
                title=item.title,
                reason=str(exc),
                reason_code=exc.code,
            )
            log.warning("resolve failed for %s (%s)", item.video_id, "; ".join(exc.attempts))
            self.notify()
            # The whole chain failed. If a newer yt-dlp could fix it, fetch one
            # in the background rather than waiting up to 12h for the routine
            # check -- most YouTube breakage is fixed upstream within days.
            self._spawn(self._maybe_reactive_update(exc.kind))
            return False

        item.title = stream.title or item.title
        item.duration = stream.duration or item.duration

        try:
            await self.player.load(
                stream.video_url,
                stream.audio_url,
                item.title,
                headers=stream.headers,
                video_id=item.video_id,
            )
        except PlayerUnavailable as exc:
            item.state = ItemState.PENDING
            self._set_notice(str(exc), "notice.player_unavailable", detail=str(exc))
            self.notify()
            return False

        item.state = ItemState.PLAYING
        item.error_code = None
        self._clear_notice()
        self.queue.save()
        self.notify()

        if self.settings.sponsorblock_enabled:
            self._spawn(self._load_segments(item.video_id))
        self._background_fetch()
        return True

    async def _play_cached(self, item: QueueItem, video_path, audio_path) -> bool:
        """Play an item straight from the on-disk cache.

        Mirrors the success path of _play_current, minus resolving: the local
        files never expire and need no headers.
        """
        try:
            await self.player.load(
                str(video_path),
                str(audio_path) if audio_path else None,
                item.title,
                video_id=item.video_id,
            )
        except PlayerUnavailable as exc:
            item.state = ItemState.PENDING
            self._set_notice(str(exc), "notice.player_unavailable", detail=str(exc))
            self.notify()
            return False

        self.cache.touch(item.video_id)
        item.state = ItemState.PLAYING
        item.error = None
        item.error_code = None
        self._clear_notice()
        self.queue.save()
        self.notify()

        if self.settings.sponsorblock_enabled:
            self._spawn(self._load_segments(item.video_id))
        self._background_fetch()
        return True

    def _cancel_gap(self) -> None:
        """Cut a pending pause short — the user asked for something now."""
        task, self._gap_task = self._gap_task, None
        if task and not task.done():
            task.cancel()

    async def _advance_after_gap(self) -> None:
        """Wait out `gap_seconds`, then move on.

        Only a natural end-of-file pauses. A manual Next cancels this and moves
        immediately, because a button that appears to do nothing for ten
        seconds reads as broken.
        """
        # gap_seconds is now hand-typed and persisted as JSON, so it can come
        # back as anything. Never let a bad value raise in a background task or
        # strand the queue on an absurd sleep.
        try:
            gap = min(MAX_GAP_SECONDS, max(0.0, float(self.settings.gap_seconds or 0)))
        except (TypeError, ValueError):
            log.warning("ignoring unusable gap_seconds %r", self.settings.gap_seconds)
            gap = 0.0
        if gap:
            self._set_notice(
                f"Next video in {gap:g}s", "notice.gap", seconds=f"{gap:g}"
            )
            self.notify()
            try:
                await asyncio.sleep(gap)
            except asyncio.CancelledError:
                return  # something else is driving playback now
        await self._advance(mark_played=True)

    async def _advance(self, mark_played: bool) -> None:
        """Move to the next playable item, skipping ones that fail to resolve."""
        if self._advancing:
            return
        self._advancing = True
        try:
            current = self.queue.current_item
            if current and mark_played and current.state is not ItemState.FAILED:
                current.state = ItemState.PLAYED

            # Bounded so a queue where nothing resolves terminates instead of
            # spinning.
            for _ in range(len(self.queue.items) + 1):
                nxt = self.queue.advance()
                if nxt is None:
                    if self.player:
                        self.player.status.state = PlaybackState.IDLE
                    self.queue.save()
                    break
                if await self._play_current():
                    break
        finally:
            self._advancing = False
            self.notify()

    async def _load_segments(self, video_id: str) -> None:
        segments = await fetch_segments(video_id, self.settings.sponsorblock_categories)
        current = self.queue.current_item
        if current and current.video_id == video_id:
            self._segments = segments
            self._skipped.clear()

    async def _prefetch(self) -> None:
        """Resolve the next item(s) ahead of time.

        Two jobs: take the resolve out of the gap between videos, and surface a
        video that is going to fail *before* the queue reaches it, rather than
        at the moment it was supposed to start.

        Deliberately shallow: resolving the whole queue at once looks like
        bulk scraping and is the fastest way to trip a bot check.
        """
        for item in self.queue.upcoming(self.settings.prefetch_ahead):
            if item.state is ItemState.FAILED:
                continue
            try:
                await self.resolver.resolve(item.video_id)
            except ResolveError as exc:
                # Recorded, but left PENDING rather than FAILED: the failure
                # may be transient, and _advance skips FAILED items outright.
                # _play_current clears this and retries properly when the
                # queue actually arrives; until then it is a warning.
                item.error = str(exc)
                item.error_code = exc.code
                log.info("prefetch of %s failed: %s", item.video_id, exc)
            else:
                item.state = ItemState.RESOLVED
                item.error = None
                item.error_code = None
            # Without this the prefetch result never reaches the UI, which is
            # the whole point of doing it early.
            self.notify()
            await asyncio.sleep(1.0)

    def _cache_ahead(self) -> int:
        """cache_ahead as a safe count -- it is hand-editable JSON like the rest."""
        try:
            return max(0, int(self.settings.cache_ahead))
        except (TypeError, ValueError):
            return 0

    def _played_ids(self) -> set[str]:
        return {item.video_id for item in self.queue.items if item.state is ItemState.PLAYED}

    def _cache_protect(self) -> set[str]:
        """Ids eviction must never drop: what is playing and what is next."""
        ids = {item.video_id for item in self.queue.upcoming(self._cache_ahead())}
        current = self.queue.current_item
        if current:
            ids.add(current.video_id)
        return ids

    async def _precache(self) -> None:
        """Download the next few items so they survive YouTube breaking later.

        As shallow as prefetch, and paced the same way: caching the whole queue
        at once is exactly what bulk scraping looks like.
        """
        if not self.cache.enabled():
            return
        protect = self._cache_protect()
        for item in self.queue.upcoming(self._cache_ahead()):
            if item.state is ItemState.FAILED or self.cache.has(item.video_id):
                continue
            try:
                # Reuses the resolver's in-memory result when prefetch already ran.
                stream = await self.resolver.resolve(item.video_id)
            except ResolveError:
                continue  # prefetch already surfaced this on the item
            await self.cache.store(stream)
            self.cache.evict_to_budget(self._played_ids(), protect=protect)
            await asyncio.sleep(1.0)

    async def _maybe_reactive_update(self, kind: FailureKind) -> None:
        """Fetch a newer yt-dlp after a total resolve failure, if it might help.

        Rate limited so a queue full of broken videos triggers one check, not
        one per item. Never raises: a background reliability measure must not be
        able to take the engine down.
        """
        if not self.settings.ytdlp_auto_update or kind not in _YTDLP_FIXABLE:
            return
        now = time.monotonic()
        if now - self._last_reactive_update < REACTIVE_UPDATE_MIN_INTERVAL:
            return
        self._last_reactive_update = now
        log.info("resolve chain exhausted (%s); checking for a newer yt-dlp", kind.value)
        try:
            installed = await maybe_update_ytdlp(self.settings, delay=0, force=True)
        except Exception:  # noqa: BLE001 - reactive check must never crash the engine
            log.info("reactive yt-dlp update failed", exc_info=True)
            return
        if installed:
            # Streams cached against the old binary may be stale; drop them so a
            # retry goes through the new one.
            self.resolver.invalidate_all()
            self._set_notice("Updated yt-dlp — try that video again.", "notice.ytdlp_updated")
            self.notify()

    # ------------------------------------------------------- player callbacks

    def _on_end_file(self, reason: str) -> None:
        if reason == "eof":
            self._gap_task = self._spawn(self._advance_after_gap())
        elif reason == "error":
            self._spawn(self._handle_playback_error())
        # "stop", "quit" and "redirect" are our own doing; ignore them.

    async def _handle_playback_error(self) -> None:
        item = self.queue.current_item
        if not item:
            return
        if self._retried_uid != item.uid:
            # Most likely an expired URL — get a fresh one before giving up.
            self._retried_uid = item.uid
            self.resolver.invalidate(item.video_id)
            # If it was playing from cache, the local file is the suspect: drop
            # it so the retry resolves from the network instead of replaying the
            # same bad file forever.
            if self.cache.has(item.video_id):
                log.info("cached %s failed to play; dropping it and re-resolving", item.video_id)
                self.cache.remove(item.video_id)
            else:
                log.info("playback error on %s, re-resolving", item.video_id)
            if await self._play_current():
                return
        item.state = ItemState.FAILED
        item.error = item.error or "Playback failed."
        item.error_code = item.error_code or "error.playback_failed"
        self._set_notice(
            f"{item.title}: playback failed, skipping.",
            "notice.playback_failed",
            title=item.title,
        )
        await self._advance(mark_played=False)

    def _on_status_change(self) -> None:
        self._check_sponsor_skip()
        self.notify()

    def _check_sponsor_skip(self) -> None:
        if not self._segments or not self.player:
            return
        if self.player.status.state is not PlaybackState.PLAYING:
            return
        position = self.player.status.position
        for index, segment in enumerate(self._segments):
            if index in self._skipped:
                continue
            if segment.contains(position):
                self._skipped.add(index)
                self._set_notice(
                    f"Skipped {segment.category}",
                    "notice.segment_skipped",
                    category=segment.category,
                )
                self._spawn(self.player.seek(segment.end, absolute=True))
                return

    def _on_window_closed(self) -> None:
        self._set_notice("Player window closed.", "notice.window_closed")
        self.notify()
