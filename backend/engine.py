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
from pathlib import Path

from .cache import OfflineCache
from .config import Settings, user_data_dir
from .library import Library, fold, matches
from .localfiles import LocalLibrary, is_local_id
from .models import AddRequest, AppState, ItemState, PlaybackState, PlayerStatus, QueueItem, SearchResult
from .overlay import UpcomingEntry, format_clock
from .player import MpvPlayer, PlayerUnavailable
from .playqueue import PlayQueue
from .potoken import PoTokenStore
from .resolver import ResolveError, Resolver
from .saver import Saver
from .search import SearchService, youtube_suggestions
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

# How often the intermission card redraws while counting down, in seconds.
INTERMISSION_TICK = 0.25

# Seconds of audio actually heard before a start counts as a success and the
# failure streak resets. A stream that 403s dies well inside this.
CONFIRMED_PLAYING_SECONDS = 3.0

# The playback watchdog: how often it looks, how long a "playing" position may
# stand still before the stream counts as frozen, how close to the end a
# freeze is simply treated as the song finishing, and how many reloads one
# item gets before it is handled as a playback failure.
WATCHDOG_INTERVAL = 3.0
STUCK_SECONDS = 12.0
END_SLACK_SECONDS = 8.0
MAX_RECOVERIES = 3

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

# Failures that are a verdict on the video itself. No amount of waiting brings
# these back, so the item is written off and the queue moves on. Everything
# else -- a bot check, the network, a yt-dlp regression -- says nothing about
# the video and will hit the next item exactly the same way.
_PER_VIDEO_FAILURES = frozenset({FailureKind.UNAVAILABLE, FailureKind.AGE_RESTRICTED, FailureKind.DRM})

# Consecutive non-per-video failures before the queue stops walking forward.
# One is a broken video; two in a row is almost always YouTube or the network,
# and walking on would mark every remaining item failed in a few seconds --
# the whole queue turning red while nothing was actually wrong with it.
STALL_AFTER = 2

# Waits between automatic retries while stalled. The last value repeats, so a
# long outage is retried every five minutes rather than abandoned.
STALL_RETRY_DELAYS = (30.0, 60.0, 120.0, 300.0)


class Engine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ytdlp = YtDlp(settings)
        self.search_service = SearchService(self.ytdlp)
        self.tokens = PoTokenStore()
        self.resolver = Resolver(self.ytdlp, settings, self.tokens)
        self.queue = PlayQueue(user_data_dir() / "queue.json")
        self.cache = OfflineCache(settings, user_data_dir() / "cache")
        self.library = Library(user_data_dir() / "library.db")
        self.local = LocalLibrary(settings)
        self.saver = Saver(settings, self.resolver, self.cache, self.library, self._on_saver_change)
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
        self._overlay_titles: list[UpcomingEntry] | None = None
        # Last text pushed to the bar's timer box, so the once-a-second change
        # is the only thing that reaches mpv, not every position update.
        self._timer_text: str | None = None
        self._background: set[asyncio.Task] = set()
        # The pending inter-video pause, kept so a manual action can cut it
        # short instead of leaving the user staring at a stopped player.
        self._gap_task: asyncio.Task | None = None
        # Uids of the items in the current run of non-per-video failures. Reset
        # by any successful start; see STALL_AFTER.
        self._failure_streak: list[str] = []
        # How many automatic retries the current stall has used, 0 when not
        # stalled. Indexes STALL_RETRY_DELAYS.
        self._stall_attempts = 0
        self._retry_task: asyncio.Task | None = None
        # The play being listened to, for history: library row id, the item's
        # uid, seconds actually heard, and the last position seen.
        self._play: dict | None = None
        # Resolver tier the playing stream came from (None when from cache),
        # so a stream that dies in the player can count against its tier.
        self._current_tier: str | None = None
        # Watchdog reloads per item uid, so a stream that keeps freezing is
        # eventually treated as broken rather than reloaded forever.
        self._recoveries: dict[str, int] = {}
        self._watchdog_task: asyncio.Task | None = None
        self._favourites: set[str] | None = None
        self._saved: list[str] | None = None

    # ------------------------------------------------------------- lifecycle

    async def start(self, open_player: bool = True) -> None:
        loop = asyncio.get_running_loop()
        self.player = MpvPlayer(self.settings, loop)
        self.player.on_end_file = self._on_end_file
        self.player.on_status_change = self._on_status_change
        self.player.on_window_closed = self._on_window_closed
        self.queue.load()

        # Not via _spawn: it runs for the life of the engine, and _background
        # is what callers wait on to see the engine settle.
        self._watchdog_task = asyncio.create_task(self._watchdog())
        self._watchdog_task.add_done_callback(_log_task_failure)

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
        self._finish_play(completed=False)
        self.saver.cancel_all()
        if self._watchdog_task:
            self._watchdog_task.cancel()
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
        self.library.close()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(_log_task_failure)
        return task

    def notify(self) -> None:
        self._sync_upcoming_overlay()
        self.on_change()

    def _upcoming_entries(self) -> list[UpcomingEntry]:
        if not self.settings.show_upcoming_overlay:
            return []
        count = UPCOMING_OVERLAY_COUNT
        if self.settings.overlay_style == "bar":
            count = _bounded_int(self.settings.overlay_boxes, 1, 6, UPCOMING_OVERLAY_COUNT)
        return [UpcomingEntry(item.title, item.singer) for item in self.queue.upcoming(count)]

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
        entries = self._upcoming_entries()
        if entries == self._overlay_titles:
            return
        self._overlay_titles = entries
        self._spawn(self.player.set_upcoming(entries))

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
            favourites=sorted(self.favourite_ids()),
            downloads=self.cache.ids(),
            saved=self.saved_ids(),
            saving=self.saver.status(),
        )

    # ---------------------------------------------------------------- search

    async def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        results = await self.search_service.search(query, limit)
        self.library.record_search(query)
        return results

    async def suggest(self, query: str) -> list[dict]:
        """Completions for the search box: the user's own history first."""
        local = self.library.suggest(query, limit=5)
        files = await asyncio.to_thread(self.local.suggest, query, 4)
        remote = await youtube_suggestions(query, self.settings.locale)
        seen, out = set(), []
        for source, texts in (("history", local), ("file", files), ("youtube", remote)):
            for text in texts:
                key = fold(text)
                if key in seen or key == fold(query):
                    continue
                seen.add(key)
                out.append({"text": text, "source": source})
        return out[:10]

    # --------------------------------------------------------------- library

    def favourite_ids(self) -> set[str]:
        # Cached: state() runs several times a second for the broadcast.
        if self._favourites is None:
            self._favourites = set(self.library.favourite_ids())
        return self._favourites

    def saved_ids(self) -> list[str]:
        if self._saved is None:
            self._saved = self.library.saved_ids()
        return self._saved

    def _on_saver_change(self) -> None:
        self._saved = None
        self.notify()

    def _meta_for(self, video_id: str, meta: dict | None = None) -> dict:
        """Title and friends for a video: what the caller sent, else the queue's."""
        meta = {k: v for k, v in (meta or {}).items() if k in ("title", "channel", "duration", "thumbnail")}
        if not meta.get("title"):
            for item in self.queue.items:
                if item.video_id == video_id:
                    meta = {
                        "title": item.title,
                        "channel": item.channel,
                        "duration": item.duration,
                        "thumbnail": item.thumbnail,
                    }
                    break
        if not meta.get("title"):
            meta = {**(self.library.track(video_id) or {}), **meta}
            meta = {k: meta.get(k) for k in ("title", "channel", "duration", "thumbnail")}
        return meta

    def save(self, video_id: str, meta: dict | None = None) -> dict:
        """Save a video to the download folder. Returns the job's status."""
        if is_local_id(video_id):
            return {"state": "done", "progress": 1.0, "path": None, "error": None}  # already on disk
        return self.saver.request(video_id, **self._meta_for(video_id, meta)).public()

    def remove_saved(self, video_id: str) -> bool:
        current = self.queue.current_item
        if current and current.video_id == video_id and self._is_active():
            return False  # mpv may have the file open
        return self.saver.delete(video_id)

    def set_favourite(self, video_id: str, favourite: bool, meta: dict | None = None) -> None:
        self.library.set_favourite(video_id, favourite, **self._meta_for(video_id, meta))
        self._favourites = None
        self.notify()

    def downloads(self, query: str = "") -> list[dict]:
        """Cached videos with their metadata, filtered by `query`."""
        favourites = self.favourite_ids()
        out = []
        for entry in self.cache.entries():
            track = self.library.track(entry["id"]) or {}
            for key in ("title", "channel", "duration", "thumbnail"):
                entry[key] = entry.get(key) or track.get(key)
            entry["title"] = entry["title"] or entry["id"]
            entry["thumbnail"] = entry["thumbnail"] or f"https://i.ytimg.com/vi/{entry['id']}/mqdefault.jpg"
            entry["favourite"] = entry["id"] in favourites
            entry["downloaded"] = True
            entry["plays"] = track.get("plays", 0)
            if query and not matches(query, entry["title"], entry["channel"]):
                continue
            out.append(entry)
        return out

    def remove_download(self, video_id: str) -> bool:
        if not self.cache.has(video_id):
            return False
        current = self.queue.current_item
        if current and current.video_id == video_id and self._is_active():
            return False  # mpv has the file open
        self.cache.remove(video_id)
        self.notify()
        return True

    async def local_files(self, query: str = "", force: bool = False) -> list[dict]:
        """Media files in the library folders, as track dicts, filtered by `query`."""
        if force:
            self.local.invalidate()
        files = await asyncio.to_thread(self.local.search, query)
        favourites = self.favourite_ids()
        out = []
        for f in files:
            entry = f.public()
            entry["favourite"] = entry["id"] in favourites
            out.append(entry)
        return out

    async def library_search(self, query: str, limit: int = 30) -> list[dict]:
        """Everything the user already has that matches: played, favourited, on disk."""
        downloaded = set(self.cache.ids())
        saved = set(self.saved_ids())
        found: dict[str, dict] = {}
        for entry in await self.local_files(query):
            found.setdefault(entry["id"], entry)
        for track in self.library.all_tracks():
            if matches(query, track["title"], track["channel"]):
                track["downloaded"] = track["id"] in downloaded or track["id"] in saved
                found.setdefault(track["id"], track)
        for entry in self.downloads(query):
            found.setdefault(entry["id"], entry)
        ranked = sorted(
            found.values(),
            key=lambda t: (
                not t.get("favourite"),
                not (t.get("downloaded") or t.get("local")),
                -(t.get("plays") or 0),
            ),
        )
        return ranked[:limit]

    def _finish_play(self, completed: bool) -> None:
        play, self._play = self._play, None
        if play:
            try:
                self.library.finish_play(play["id"], play["listened"], completed)
            except Exception:  # noqa: BLE001 - history must never break playback
                log.warning("could not record a play", exc_info=True)

    def _start_play(self, item: QueueItem) -> None:
        self._finish_play(completed=False)
        try:
            self.library.remember(item.video_id, item.title, item.channel, item.duration, item.thumbnail)
            play_id = self.library.start_play(item.video_id, item.singer)
        except Exception:  # noqa: BLE001 - history must never break playback
            log.warning("could not record a play", exc_info=True)
            return
        self._play = {"id": play_id, "uid": item.uid, "listened": 0.0, "last": 0.0}

    def _track_listening(self) -> None:
        """Accumulate seconds actually heard, from position updates.

        Only small forward steps count, so a seek -- either way -- adds nothing.
        """
        play = self._play
        if not play or not self.player:
            return
        status = self.player.status
        position = status.position
        if status.state is PlaybackState.PLAYING and 0 < position - play["last"] <= 2.0:
            play["listened"] += position - play["last"]
        play["last"] = position
        if play["listened"] >= CONFIRMED_PLAYING_SECONDS and not play.get("confirmed"):
            play["confirmed"] = True
            self._note_success()

    # ----------------------------------------------------------------- queue

    async def add(self, request: AddRequest) -> QueueItem:
        """Queue a song. Raises LookupError for a local id no folder holds."""
        local_path = None
        title, thumbnail = request.title, request.thumbnail
        if is_local_id(request.video_id):
            found = await asyncio.to_thread(self.local.find, request.video_id)
            if found is None:
                raise LookupError(f"no file in the library folders has id {request.video_id}")
            local_path, title, thumbnail = str(found.path), title or found.title, None
        elif not thumbnail:
            thumbnail = f"https://i.ytimg.com/vi/{request.video_id}/mqdefault.jpg"
        item = QueueItem(
            video_id=request.video_id,
            title=title or request.video_id,
            channel=request.channel,
            duration=request.duration,
            thumbnail=thumbnail,
            singer=(request.singer or "").strip() or None,
            local_path=local_path,
        )
        if request.play_now:
            self._cancel_waits()
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

    async def set_singer(self, index: int, singer: str | None) -> bool:
        if not 0 <= index < len(self.queue.items):
            return False
        self.queue.items[index].singer = (singer or "").strip() or None
        self.queue.save()
        self.notify()
        return True

    async def reorder(self, from_index: int, to_index: int) -> bool:
        ok = self.queue.reorder(from_index, to_index)
        self.notify()
        return ok

    async def clear(self) -> None:
        self._cancel_waits()
        self._finish_play(completed=False)
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
        # A stalled queue counts as busy, so a new add waits its turn behind
        # the items being retried instead of jumping ahead of them.
        if self._playback_pending or self.stalled:
            return True
        if not self.player:
            return False
        return self.player.status.state in (PlaybackState.PLAYING, PlaybackState.PAUSED, PlaybackState.LOADING)

    async def _start_playback(self) -> None:
        """Play the selected item, falling through to the next if it fails."""
        try:
            if not await self._play_current() and not self.stalled:
                await self._advance(mark_played=False)
        finally:
            self._playback_pending = False

    async def play_index(self, index: int) -> bool:
        self._cancel_waits()
        if self.queue.select(index) is None:
            return False
        self._playback_pending = True
        await self._start_playback()
        return True

    async def next(self) -> None:
        self._cancel_waits()
        await self._advance(mark_played=True)

    async def previous(self) -> None:
        self._cancel_waits()
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
        self._cancel_waits()
        self._finish_play(completed=False)
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

    async def _play_current(self, resume_at: float | None = None) -> bool:
        """Start the current item, or with `resume_at`, restart it from there.

        A resume is the watchdog bringing a frozen stream back: it keeps the
        SponsorBlock state and does not count as a new play.
        """
        item = self.queue.current_item
        if not item or not self.player:
            return False

        if resume_at is None:
            self._segments = []
            self._skipped.clear()
        item.error = None
        item.error_code = None
        item.state = ItemState.PENDING
        self.notify()

        # A file the user saved plays first, then a cached copy: no resolve, no
        # network, immune to YouTube being down. A saved file that already
        # failed once for this item is skipped, so the retry goes online.
        if item.local_path:
            return await self._play_local(item, resume_at)
        saved = self.library.saved_path(item.video_id) if self._retried_uid != item.uid else None
        if saved is None and self._retried_uid != item.uid:
            # A copy of this video sitting in one of the library folders --
            # saved earlier, or put there by hand -- beats streaming it.
            saved = await asyncio.to_thread(self.local.copy_of, item.video_id)
        if saved and await self._play_cached(item, saved, None, from_cache=False, start=resume_at):
            return True
        if self.cache.enabled():
            cached = self.cache.paths(item.video_id)
            if cached and await self._play_cached(item, *cached, start=resume_at):
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
            # The whole chain failed. If a newer yt-dlp could fix it, fetch one
            # in the background rather than waiting up to 12h for the routine
            # check -- most YouTube breakage is fixed upstream within days.
            self._spawn(self._maybe_reactive_update(exc.kind))
            self._note_failure(item, exc.kind)
            self.notify()
            return False

        item.title = stream.title or item.title
        item.duration = stream.duration or item.duration
        self._current_tier = stream.tier

        try:
            await self.player.load(
                stream.video_url,
                stream.audio_url,
                item.title,
                headers=stream.headers,
                video_id=item.video_id,
                start=resume_at,
            )
        except PlayerUnavailable as exc:
            item.state = ItemState.PENDING
            self._set_notice(str(exc), "notice.player_unavailable", detail=str(exc))
            self.notify()
            return False

        self._on_started(item, resumed=resume_at is not None)
        return True

    def _on_started(self, item: QueueItem, resumed: bool = False) -> None:
        """Common tail of a successful start, from the network or the cache."""
        if not resumed:
            self._start_play(item)
        item.state = ItemState.PLAYING
        item.error = None
        item.error_code = None
        # Not _note_success(): a load that is accepted can still die a second
        # later on a 403, and counting it as a success would reset the failure
        # streak on every item -- each would go red without the queue ever
        # stalling. Success is declared once audio is actually heard.
        self._clear_notice()
        self.queue.save()
        self.notify()

        if self.settings.sponsorblock_enabled and not resumed and not item.local_path:
            self._spawn(self._load_segments(item.video_id))
        self._background_fetch()

    async def _play_local(self, item: QueueItem, resume_at: float | None) -> bool:
        """Play a file from the library folders. A missing file is the item's
        own problem -- moved or deleted -- so it fails without stalling the queue."""
        path = Path(item.local_path)
        if not path.is_file():
            item.state = ItemState.FAILED
            item.error = "The file is no longer there."
            item.error_code = "error.file_missing"
            self._set_notice(f"{item.title}: the file is no longer there.", "notice.file_missing", title=item.title)
            self.notify()
            return False
        return await self._play_cached(item, path, None, from_cache=False, start=resume_at)

    async def _play_cached(
        self, item: QueueItem, video_path, audio_path, from_cache: bool = True, start: float | None = None
    ) -> bool:
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
                start=start,
            )
        except PlayerUnavailable as exc:
            item.state = ItemState.PENDING
            self._set_notice(str(exc), "notice.player_unavailable", detail=str(exc))
            self.notify()
            return False

        if from_cache:
            self.cache.touch(item.video_id)
        self._current_tier = None
        self._on_started(item, resumed=start is not None)
        return True

    def _cancel_waits(self) -> None:
        """Cut a pending pause or stall retry short — the user asked for something now.

        A manual action also ends the stall itself: whatever they picked gets
        a fresh run, and a failure there starts counting from zero.
        """
        for task in (self._gap_task, self._retry_task):
            if task and not task.done():
                task.cancel()
        self._gap_task = self._retry_task = None
        self._failure_streak = []
        self._stall_attempts = 0

    @property
    def stalled(self) -> bool:
        """True while the queue is waiting to retry after repeated failures."""
        return self._retry_task is not None and not self._retry_task.done()

    def _note_success(self) -> None:
        self._failure_streak = []
        self._stall_attempts = 0

    def _note_failure(self, item: QueueItem, kind: FailureKind) -> bool:
        """Record a failed start. Returns True if the queue should stall here.

        A per-video failure (removed, age-gated, DRM) is the video's fault and
        never stalls: the queue moves on. Anything else counts towards a streak,
        and once STALL_AFTER items in a row have failed that way -- or while a
        stall's retry is the thing that just failed -- the queue stops instead
        of burning through every remaining item.
        """
        if kind in _PER_VIDEO_FAILURES:
            return False
        if item.uid not in self._failure_streak:
            self._failure_streak.append(item.uid)
        if self._stall_attempts == 0 and len(self._failure_streak) < STALL_AFTER:
            return False
        self._stall()
        return True

    def _stall(self) -> None:
        """Park the queue on the first item of the failure streak and retry later.

        The items in the streak go back to PENDING with their error kept, so
        the UI shows them as warnings rather than as written off, and the retry
        starts from the earliest of them -- nothing is silently skipped.
        """
        streak = set(self._failure_streak)
        first = None
        for index, item in enumerate(self.queue.items):
            if item.uid in streak:
                item.state = ItemState.PENDING
                if first is None:
                    first = index
        if first is not None:
            self.queue.select(first)
        if self.player and self.player.status.state is PlaybackState.LOADING:
            self.player.status.state = PlaybackState.IDLE

        delay = STALL_RETRY_DELAYS[min(self._stall_attempts, len(STALL_RETRY_DELAYS) - 1)]
        self._stall_attempts += 1
        self._set_notice(
            f"Playback paused: YouTube is not responding. Retrying in {delay:g}s.",
            "notice.stalled",
            seconds=f"{delay:g}",
            # Carried over from the failure notice this replaces. A playback
            # failure has no resolver reason, so it says that instead.
            reason=self.notice_params.get("reason") or "Playback failed.",
            reason_code=self.notice_params.get("reason_code") or "error.playback_failed",
        )
        log.warning(
            "%d items failed in a row; stalling the queue, retry %d in %gs",
            len(streak),
            self._stall_attempts,
            delay,
        )
        if self._retry_task and not self._retry_task.done():
            self._retry_task.cancel()
        self._retry_task = self._spawn(self._retry_after(delay))
        self.queue.save()
        self.notify()

    async def _retry_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        # Not via _cancel_waits: the stall counters must survive, so a failed
        # retry backs off further instead of starting the streak again.
        self._retry_task = None
        item = self.queue.current_item
        if item is None:
            return
        log.info("retrying %s after stall", item.video_id)
        # Streams cached during the outage may be what is failing; start clean.
        self.resolver.invalidate_all()
        self._playback_pending = True
        await self._start_playback()

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
                await self._intermission(gap)
            except asyncio.CancelledError:
                return  # something else is driving playback now
        await self._advance(mark_played=True)

    async def _intermission(self, gap: float) -> None:
        """Sleep out the gap, drawing the "next on stage" countdown if enabled.

        Ticks a few times a second so the progress bar moves smoothly; the card
        is always taken down afterwards, including when a manual action cuts
        the gap short, so it can never be left covering the next video.
        """
        upcoming = self.queue.upcoming(1)
        if not (self.settings.show_intermission and upcoming and self.player):
            await asyncio.sleep(gap)
            return
        entry = UpcomingEntry(upcoming[0].title, upcoming[0].singer)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + gap
        try:
            while (remaining := deadline - loop.time()) > 0:
                await self.player.set_intermission(entry, remaining, gap)
                await self.player.set_timer(format_clock(-(-remaining // 1)))
                await asyncio.sleep(min(INTERMISSION_TICK, remaining))
        finally:
            # Spawned: a cancelled task cannot reliably await in its finally.
            self._spawn(self.player.set_intermission(None))
            self._timer_text = None  # let _sync_timer take the box back

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
                if self.stalled:
                    break  # parked to retry; walking on would fail them all
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
            if item.state is ItemState.FAILED or item.local_path:
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
        # Favourites stay on disk once downloaded: they are what the user most
        # wants to keep playable if YouTube breaks.
        return ids | self.favourite_ids()

    async def _precache(self) -> None:
        """Download the next few items so they survive YouTube breaking later.

        As shallow as prefetch, and paced the same way: caching the whole queue
        at once is exactly what bulk scraping looks like.
        """
        if not self.cache.enabled():
            return
        protect = self._cache_protect()
        for item in self.queue.upcoming(self._cache_ahead()):
            if item.state is ItemState.FAILED or item.local_path or self.cache.has(item.video_id):
                continue
            try:
                # Reuses the resolver's in-memory result when prefetch already ran.
                stream = await self.resolver.resolve(item.video_id)
            except ResolveError:
                continue  # prefetch already surfaced this on the item
            await self.cache.store(
                stream, {"title": item.title, "channel": item.channel, "thumbnail": item.thumbnail}
            )
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

    # --------------------------------------------------------------- watchdog

    async def _watchdog(self) -> None:
        """Bring back a song whose stream has silently frozen.

        Measured October 2026: after a seek -- the user's, or a SponsorBlock
        skip -- mpv can sit on a googlevideo read forever, still "playing",
        position frozen, past its own network timeout, with no end-file event
        to react to. From the outside that is the queue simply stopping.
        Reloading the file at the same position recovers it at once, so that
        is what this does; frozen in the last seconds, it moves on instead.
        """
        frozen_since: float | None = None
        last: tuple[str, float] | None = None
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            item = self.queue.current_item
            player = self.player
            watching = (
                item is not None
                and player is not None
                and player.running
                and player.status.state is PlaybackState.PLAYING
                # Still at 0 is a slow start, not a freeze; a start that never
                # comes ends in mpv's own network timeout and an end-file.
                and player.status.position > 0
                and not self._advancing
                and not self._playback_pending
                and not (self._gap_task and not self._gap_task.done())
            )
            if not watching:
                frozen_since, last = None, None
                continue
            sample = (item.uid, round(player.status.position, 2))
            now = time.monotonic()
            if sample != last:
                frozen_since, last = now, sample
                continue
            if now - frozen_since < STUCK_SECONDS:
                continue
            frozen_since, last = None, None
            await self._recover_frozen(item)

    async def _recover_frozen(self, item: QueueItem) -> None:
        status = self.player.status
        position = status.position
        duration = status.duration or item.duration or 0
        recoveries = self._recoveries.get(item.uid, 0) + 1
        self._recoveries[item.uid] = recoveries
        if duration and position >= duration - END_SLACK_SECONDS:
            log.warning("playback of %s froze %.1fs from the end; treating it as finished", item.video_id, duration - position)
            self._on_end_file("eof")
            return
        if recoveries > MAX_RECOVERIES:
            log.warning("playback of %s keeps freezing; giving up on this stream", item.video_id)
            self._spawn(self._handle_playback_error())
            return
        log.warning("playback of %s froze at %.1fs; reloading it there (attempt %d)", item.video_id, position, recoveries)
        await self._play_current(resume_at=position)

    # ------------------------------------------------------- player callbacks

    def _on_end_file(self, reason: str) -> None:
        if reason == "eof":
            if self._play and self._play.get("listened", 0) > 0:
                self._note_success()
                current = self.queue.current_item
                if self.settings.download_played and current:
                    self.save(current.video_id)
            self._finish_play(completed=True)
            self._gap_task = self._spawn(self._advance_after_gap())
        elif reason == "error":
            self._spawn(self._handle_playback_error())
        # "stop", "quit" and "redirect" are our own doing; ignore them.

    async def _handle_playback_error(self) -> None:
        item = self.queue.current_item
        if not item:
            return
        if item.local_path:
            # A file that will not play will not start playing on a retry, and
            # says nothing about YouTube: fail it and move on, no stall.
            item.state = ItemState.FAILED
            item.error, item.error_code = "Playback failed.", "error.playback_failed"
            self._set_notice(f"{item.title}: playback failed, skipping.", "notice.playback_failed", title=item.title)
            await self._advance(mark_played=False)
            return
        if self._current_tier:
            # The stream resolved but would not play: stop preferring the tier
            # that produced it, so the retry does not go straight back to it.
            self.resolver.demote(self._current_tier)
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
            if await self._play_current() or self.stalled:
                return
        item.state = ItemState.FAILED
        item.error = item.error or "Playback failed."
        item.error_code = item.error_code or "error.playback_failed"
        self._set_notice(
            f"{item.title}: playback failed, skipping.",
            "notice.playback_failed",
            title=item.title,
        )
        # Playback dying on item after item -- streams that resolve but will
        # not play -- is as systemic as a resolve failure, and stalls the same.
        if self._note_failure(item, FailureKind.OTHER):
            return
        await self._advance(mark_played=False)

    def _on_status_change(self) -> None:
        self._check_sponsor_skip()
        self._track_listening()
        self._sync_timer()
        self.notify()

    def _sync_timer(self) -> None:
        """Keep the bar's timer box on the time left in the current song.

        Position updates arrive several times a second; only a change in the
        displayed whole second is pushed to mpv. The intermission drives the
        box itself while it runs.
        """
        if not self.player or self._gap_task and not self._gap_task.done():
            return
        status = self.player.status
        text = None
        if status.state in (PlaybackState.PLAYING, PlaybackState.PAUSED) and status.duration:
            text = format_clock(max(0.0, status.duration - status.position))
        if text != self._timer_text:
            self._timer_text = text
            self._spawn(self.player.set_timer(text))

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


def _log_task_failure(task: asyncio.Task) -> None:
    """Log an exception that escaped a background task.

    Without this asyncio only reports it at garbage collection, if ever -- a
    crashed advance would leave the queue silently stopped with no trace.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("background task failed", exc_info=exc)


def _bounded_int(value, low: int, high: int, default: int) -> int:
    """A hand-editable setting as a safe int in [low, high]."""
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default
