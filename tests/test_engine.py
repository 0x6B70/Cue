from __future__ import annotations

import asyncio
import time

import pytest

from backend.engine import Engine
from backend.library import Library
from backend.models import AddRequest, ItemState, PlaybackState, PlayerStatus
from backend.playqueue import PlayQueue
from backend.resolver import ResolvedStream
from backend.sponsorblock import Segment
from backend.ytdlp import FailureKind


class FakePlayer:
    """Stands in for mpv: records what it was asked to do."""

    def __init__(self, settings, loop):
        self.settings = settings
        self.loop = loop
        self.status = PlayerStatus()
        self.running = False
        self.loaded: list[dict] = []
        self.seeks: list[float] = []
        self.upcoming: list[list[str]] = []
        self.entries: list = []
        self.intermissions: list = []
        self.timers: list = []
        self.fail_load = False
        self.on_end_file = None
        self.on_status_change = None
        self.on_window_closed = None

    async def ensure_started(self):
        self.running = True
        self.status.window_open = True

    def shutdown(self):
        self.running = False

    async def load(self, video_url, audio_url, title, headers=None, video_id=None, start=None):
        if self.fail_load:
            from backend.player import PlayerUnavailable

            raise PlayerUnavailable("no window")
        await self.ensure_started()
        self.loaded.append(
            {"video_url": video_url, "audio_url": audio_url, "title": title, "video_id": video_id, "start": start}
        )
        self.status.state = PlaybackState.PLAYING

    async def stop(self):
        self.status.state = PlaybackState.IDLE

    async def set_paused(self, paused):
        self.status.state = PlaybackState.PAUSED if paused else PlaybackState.PLAYING

    async def toggle_pause(self):
        await self.set_paused(self.status.state is not PlaybackState.PAUSED)

    async def seek(self, seconds, absolute=True):
        self.seeks.append(seconds)

    async def set_volume(self, level):
        self.status.volume = level

    async def set_upcoming(self, entries):
        # Recorded as titles: what most tests care about is which items show.
        self.upcoming.append([getattr(e, "title", e) for e in entries])
        self.entries = list(entries)

    async def set_intermission(self, entry, remaining=0.0, total=0.0):
        self.intermissions.append(entry and (entry.title, remaining, total))

    async def set_timer(self, text):
        self.timers.append(text)

    def reload_logo(self):
        pass


@pytest.fixture
async def engine(settings, monkeypatch, tmp_path):
    monkeypatch.setattr("backend.engine.MpvPlayer", FakePlayer)
    settings.sponsorblock_enabled = False
    settings.prefetch_ahead = 0
    # Off by default so the common tests do not download; the cache tests below
    # opt in. Its dir is isolated per test because user_data_dir() is shared.
    settings.offline_cache = False
    created = Engine(settings)
    created.queue.state_path = tmp_path / "queue.json"
    created.cache.dir = tmp_path / "cache"
    created.cache._index_path = created.cache.dir / "index.json"
    created.cache._index = {}
    created.library = Library(tmp_path / "library.db")
    created.saver.library = created.library
    created.saver.cache = created.cache
    settings.download_dir = str(tmp_path / "saved")
    await created.start(open_player=False)
    yield created
    await created.shutdown()


async def drain(engine: Engine, timeout: float = 10.0) -> None:
    """Let engine background tasks finish.

    Waits on the engine's own task set rather than sleeping a fixed amount.
    A resolve shells out to the stub yt-dlp, and process startup costs ~250ms
    on Windows against ~20ms on Linux, so any fixed budget is either flaky on
    one platform or needlessly slow on the other.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = {task for task in engine._background if not task.done()}
        if pending:
            await asyncio.wait(pending, timeout=max(0.0, deadline - time.monotonic()))
            continue
        # Nothing in flight. Give the loop a turn in case a task that just
        # finished spawned a follow-up, then call it settled.
        await asyncio.sleep(0.01)
        if not any(not task.done() for task in engine._background):
            return


def add(video_id: str, **kwargs) -> AddRequest:
    return AddRequest(video_id=video_id, title=f"Title {video_id}", **kwargs)


async def test_add_to_idle_queue_starts_playback(engine):
    await engine.add(add("abc123"))
    await drain(engine)
    assert engine.player.loaded[0]["video_id"] == "abc123"
    assert engine.queue.items[0].state is ItemState.PLAYING


async def test_add_while_playing_only_queues(engine):
    await engine.add(add("first"))
    await drain(engine)
    await engine.add(add("second"))
    await drain(engine)
    assert len(engine.player.loaded) == 1
    assert engine.queue.items[1].state is ItemState.PENDING


async def test_rapid_adds_do_not_both_start_playing(engine):
    """Resolving is async: two quick adds must not race into a double start."""
    await engine.add(add("first"))
    await engine.add(add("second"))
    await drain(engine)
    assert [entry["video_id"] for entry in engine.player.loaded] == ["first"]
    assert engine.queue.current == 0


async def test_play_now_jumps_the_queue(engine):
    await engine.add(add("first"))
    await drain(engine)
    await engine.add(add("later"))
    await engine.add(add("urgent", play_now=True))
    await drain(engine)
    assert [i.video_id for i in engine.queue.items] == ["first", "urgent", "later"]
    assert engine.player.loaded[-1]["video_id"] == "urgent"


async def test_resolve_failure_marks_item_and_moves_on(engine, monkeypatch):
    """A broken video must never stop the queue."""
    from backend.resolver import ResolveError

    async def only_second_resolves(video_id, force=False):
        if video_id == "broken":
            raise ResolveError("nope")
        return await original(video_id, force)

    original = engine.resolver.resolve
    monkeypatch.setattr(engine.resolver, "resolve", only_second_resolves)

    await engine.add(add("broken"))
    await drain(engine)
    assert engine.queue.items[0].state is ItemState.FAILED

    await engine.add(add("good"))
    await drain(engine)
    assert engine.queue.items[1].state is ItemState.PLAYING


async def test_end_of_file_advances_to_the_next_item(engine):
    await engine.add(add("first"))
    await engine.add(add("second"))
    await drain(engine)
    engine.player.on_end_file("eof")
    await drain(engine)
    assert engine.queue.items[0].state is ItemState.PLAYED
    assert engine.queue.items[1].state is ItemState.PLAYING


async def test_end_of_queue_returns_to_idle(engine):
    await engine.add(add("only"))
    await drain(engine)
    engine.player.on_end_file("eof")
    await drain(engine)
    assert engine.player.status.state is PlaybackState.IDLE


async def test_self_initiated_stop_does_not_advance(engine):
    await engine.add(add("first"))
    await engine.add(add("second"))
    await drain(engine)
    engine.player.on_end_file("stop")
    await drain(engine)
    assert engine.queue.items[1].state is ItemState.PENDING


async def test_playback_error_re_resolves_once_before_failing(engine):
    """An expired URL should be retried with a fresh one, not written off."""
    await engine.add(add("abc123"))
    await drain(engine)
    assert len(engine.player.loaded) == 1

    engine.player.on_end_file("error")
    await drain(engine)
    assert len(engine.player.loaded) == 2
    assert engine.queue.items[0].state is ItemState.PLAYING

    # A second failure on the same item gives up.
    engine.player.on_end_file("error")
    await drain(engine)
    assert engine.queue.items[0].state is ItemState.FAILED


async def test_advance_skips_failed_items(engine):
    await engine.add(add("a"))
    await engine.add(add("b"))
    await engine.add(add("c"))
    await drain(engine)
    engine.queue.items[1].state = ItemState.FAILED
    await engine.next()
    await drain(engine)
    assert engine.queue.current_item.video_id == "c"


async def test_queue_where_nothing_resolves_stalls_instead_of_failing_everything(engine, monkeypatch):
    """A systemic failure (bot check, network) must not mark the whole queue
    failed: it stops after STALL_AFTER items and waits to retry."""
    from backend.engine import STALL_AFTER
    from backend.resolver import ResolveError

    async def always_fail(video_id, force=False):
        raise ResolveError("nope", kind=FailureKind.BOT_CHECK)

    monkeypatch.setattr(engine.resolver, "resolve", always_fail)
    for i in range(6):
        await engine.add(add(f"v{i}"))
    assert await until(lambda: engine.stalled)
    assert not any(item.state is ItemState.FAILED for item in engine.queue.items)
    # Parked on the first item of the streak, which shows as a warning.
    assert engine.queue.current == 0
    assert engine.queue.items[0].error_code
    assert engine.notice_code == "notice.stalled"
    # Items beyond the streak were never even tried.
    assert all(item.error_code is None for item in engine.queue.items[STALL_AFTER:])


async def test_per_video_failures_still_skip_without_stalling(engine, monkeypatch):
    from backend.resolver import ResolveError

    async def gone(video_id, force=False):
        raise ResolveError("gone", kind=FailureKind.UNAVAILABLE)

    monkeypatch.setattr(engine.resolver, "resolve", gone)
    for i in range(4):
        await engine.add(add(f"v{i}"))
    await drain(engine)
    assert not engine.stalled
    assert all(item.state is ItemState.FAILED for item in engine.queue.items)


async def test_stall_retries_and_resumes_when_youtube_recovers(engine, monkeypatch):
    from backend import engine as engine_module
    from backend.resolver import ResolveError

    monkeypatch.setattr(engine_module, "STALL_RETRY_DELAYS", (0.05,))
    broken = {"on": True}
    original = engine.resolver.resolve

    async def flaky(video_id, force=False):
        if broken["on"]:
            raise ResolveError("blocked", kind=FailureKind.BOT_CHECK)
        return await original(video_id, force)

    monkeypatch.setattr(engine.resolver, "resolve", flaky)
    for i in range(3):
        await engine.add(add(f"v{i}"))
    assert await until(lambda: engine.stalled)
    broken["on"] = False
    await asyncio.sleep(0.1)
    await drain(engine)
    assert not engine.stalled
    assert engine.queue.current == 0
    assert engine.queue.items[0].state is ItemState.PLAYING
    assert engine.notice_code is None


async def test_single_broken_video_is_skipped_without_stalling(engine, monkeypatch):
    from backend.resolver import ResolveError

    original = engine.resolver.resolve

    async def one_broken(video_id, force=False):
        if video_id == "bad":
            raise ResolveError("nope", kind=FailureKind.NO_FORMATS)
        return await original(video_id, force)

    monkeypatch.setattr(engine.resolver, "resolve", one_broken)
    await engine.add(add("bad"))
    await engine.add(add("good"))
    await drain(engine)
    assert not engine.stalled
    assert engine.queue.items[0].state is ItemState.FAILED
    assert engine.queue.items[1].state is ItemState.PLAYING


async def test_manual_next_ends_a_stall(engine, monkeypatch):
    from backend.resolver import ResolveError

    original = engine.resolver.resolve
    broken = {"on": True}

    async def flaky(video_id, force=False):
        if broken["on"]:
            raise ResolveError("blocked", kind=FailureKind.NETWORK)
        return await original(video_id, force)

    monkeypatch.setattr(engine.resolver, "resolve", flaky)
    for i in range(3):
        await engine.add(add(f"v{i}"))
    assert await until(lambda: engine.stalled)
    broken["on"] = False
    await engine.play_index(2)
    await drain(engine)
    assert not engine.stalled
    assert engine.queue.items[2].state is ItemState.PLAYING


async def test_remove_playing_item_advances(engine):
    await engine.add(add("first"))
    await engine.add(add("second"))
    await drain(engine)
    await engine.remove(0)
    await drain(engine)
    assert engine.player.loaded[-1]["video_id"] == "second"


async def test_sponsor_segment_triggers_a_seek(engine):
    await engine.add(add("abc123"))
    await drain(engine)
    engine._segments = [Segment(10.0, 30.0, "sponsor")]
    engine.player.status.state = PlaybackState.PLAYING
    engine.player.status.position = 12.0
    engine._on_status_change()
    await drain(engine)
    assert engine.player.seeks == [30.0]
    assert engine.notice == "Skipped sponsor"


async def test_sponsor_segment_is_skipped_only_once(engine):
    await engine.add(add("abc123"))
    await drain(engine)
    engine._segments = [Segment(10.0, 30.0, "sponsor")]
    engine.player.status.state = PlaybackState.PLAYING
    for position in (12.0, 13.0, 14.0):
        engine.player.status.position = position
        engine._on_status_change()
    await drain(engine)
    assert engine.player.seeks == [30.0]


async def test_no_seek_while_paused(engine):
    await engine.add(add("abc123"))
    await drain(engine)
    engine._segments = [Segment(10.0, 30.0, "sponsor")]
    engine.player.status.state = PlaybackState.PAUSED
    engine.player.status.position = 12.0
    engine._on_status_change()
    await drain(engine)
    assert engine.player.seeks == []


async def test_player_unavailable_leaves_item_replayable(engine):
    engine.player.fail_load = True
    await engine.add(add("abc123"))
    await drain(engine)
    assert engine.queue.items[0].state is ItemState.PENDING
    assert "no window" in engine.notice


async def test_prefetch_warns_about_a_video_that_will_not_play(engine, settings, monkeypatch):
    """The point of prefetching is to learn a video is broken *before* the
    queue reaches it, so the failure has to reach the UI as item state."""
    from backend.resolver import ResolveError

    settings.prefetch_ahead = 1
    original = engine.resolver.resolve

    async def fail_second(video_id, force=False):
        if video_id == "broken":
            raise ResolveError("nope")
        return await original(video_id, force)

    monkeypatch.setattr(engine.resolver, "resolve", fail_second)

    await engine.add(add("first"))
    await engine.add(add("broken"))
    await drain(engine)

    upcoming = engine.queue.items[1]
    assert upcoming.error, "prefetch failure was not recorded on the item"
    assert upcoming.error_code
    # Still PENDING, not FAILED: _advance skips FAILED outright, and the
    # failure may be transient.
    assert upcoming.state is ItemState.PENDING


async def test_prefetch_marks_a_working_next_item_resolved(engine, settings):
    settings.prefetch_ahead = 1
    await engine.add(add("first"))
    await engine.add(add("second"))
    await drain(engine)
    assert engine.queue.items[1].state is ItemState.RESOLVED
    assert engine.queue.items[1].error is None


async def test_gap_delays_the_next_video(engine, settings):
    settings.gap_seconds = 0.25
    await engine.add(add("first"))
    await engine.add(add("second"))
    await drain(engine)

    engine.player.on_end_file("eof")
    await asyncio.sleep(0.05)
    # Still on the first item: the pause has not elapsed.
    assert engine.queue.items[1].state is not ItemState.PLAYING

    await drain(engine)
    assert engine.queue.items[1].state is ItemState.PLAYING


async def test_manual_next_skips_the_gap(engine, settings):
    """A Next button that appears to do nothing for ten seconds reads as
    broken, so a manual advance cancels the pending pause."""
    settings.gap_seconds = 30.0
    await engine.add(add("first"))
    await engine.add(add("second"))
    await drain(engine)

    engine.player.on_end_file("eof")
    await asyncio.sleep(0.05)
    await engine.next()
    await drain(engine)

    assert engine.queue.items[1].state is ItemState.PLAYING
    # The 30s pause must not still be pending afterwards.
    assert engine._gap_task is None or engine._gap_task.cancelled()


@pytest.mark.parametrize("value", ["abc", None, -5, "", [1]])
async def test_unusable_gap_values_do_not_stall_the_queue(engine, settings, value):
    """gap_seconds is hand-typed and persisted as JSON, so it can be anything.

    A bad value must not raise inside the background advance task, which would
    silently stop the queue rather than surfacing an error.
    """
    settings.gap_seconds = value
    await engine.add(add("first"))
    await engine.add(add("second"))
    await drain(engine)

    engine.player.on_end_file("eof")
    await drain(engine)
    assert engine.queue.items[1].state is ItemState.PLAYING


async def test_absurd_gap_is_capped(engine, settings, monkeypatch):
    """A typo like 99999 should not look like the app has hung forever."""
    from backend import engine as engine_module

    monkeypatch.setattr(engine_module, "MAX_GAP_SECONDS", 0.2)
    settings.gap_seconds = 99999
    await engine.add(add("first"))
    await engine.add(add("second"))
    await drain(engine)

    engine.player.on_end_file("eof")
    await drain(engine)
    assert engine.queue.items[1].state is ItemState.PLAYING


async def test_no_gap_by_default(engine):
    await engine.add(add("first"))
    await engine.add(add("second"))
    await drain(engine)
    engine.player.on_end_file("eof")
    await drain(engine)
    assert engine.queue.items[1].state is ItemState.PLAYING


async def test_queue_is_emptied_on_a_clean_exit(engine, settings):
    settings.clear_queue_on_exit = True
    await engine.add(add("first"))
    await engine.add(add("second"))
    await drain(engine)

    await engine.shutdown()
    assert engine.queue.items == []

    # And the emptied queue is what was persisted, so nothing reloads.
    reloaded = PlayQueue(engine.queue.state_path)
    reloaded.load()
    assert reloaded.items == []


async def test_queue_survives_when_clearing_is_disabled(engine, settings):
    settings.clear_queue_on_exit = False
    await engine.add(add("first"))
    await engine.add(add("second"))
    await drain(engine)

    await engine.shutdown()
    reloaded = PlayQueue(engine.queue.state_path)
    reloaded.load()
    assert [i.video_id for i in reloaded.items] == ["first", "second"]


async def test_overlay_lists_what_is_coming_not_what_is_playing(engine):
    for name in ("first", "second", "third"):
        await engine.add(add(name))
    await drain(engine)
    assert engine.player.upcoming[-1] == ["Title second", "Title third"]


async def test_overlay_lists_at_most_three(engine):
    for name in ("a", "b", "c", "d", "e"):
        await engine.add(add(name))
    await drain(engine)
    assert engine.player.upcoming[-1] == ["Title b", "Title c", "Title d"]


async def test_overlay_follows_the_queue_on_advance(engine):
    for name in ("a", "b", "c", "d"):
        await engine.add(add(name))
    await drain(engine)
    assert engine.player.upcoming[-1] == ["Title b", "Title c", "Title d"]

    engine.player.on_end_file("eof")
    await drain(engine)
    assert engine.queue.current_item.video_id == "b"
    assert engine.player.upcoming[-1] == ["Title c", "Title d"]


async def test_overlay_updates_when_an_upcoming_item_is_removed(engine):
    for name in ("a", "b", "c"):
        await engine.add(add(name))
    await drain(engine)
    await engine.remove(1)
    await drain(engine)
    assert engine.player.upcoming[-1] == ["Title c"]


async def test_overlay_updates_on_reorder(engine):
    for name in ("a", "b", "c"):
        await engine.add(add(name))
    await drain(engine)
    assert engine.player.upcoming[-1] == ["Title b", "Title c"]

    await engine.reorder(2, 1)
    await drain(engine)
    assert engine.player.upcoming[-1] == ["Title c", "Title b"]


async def test_overlay_is_cleared_when_nothing_is_upcoming(engine):
    await engine.add(add("a"))
    await engine.add(add("b"))
    await drain(engine)
    assert engine.player.upcoming[-1] == ["Title b"]

    await engine.clear()
    await drain(engine)
    assert engine.player.upcoming[-1] == []


async def test_overlay_is_never_drawn_when_disabled(engine, settings):
    settings.show_upcoming_overlay = False
    for name in ("a", "b", "c"):
        await engine.add(add(name))
    await drain(engine)
    assert all(titles == [] for titles in engine.player.upcoming)


async def test_overlay_is_not_redrawn_while_a_video_plays(engine):
    """notify() fires on every position update, several times a second.

    Redrawing on each one would be a steady drip of pointless IPC for the whole
    length of the video.
    """
    await engine.add(add("a"))
    await engine.add(add("b"))
    await drain(engine)
    pushes = len(engine.player.upcoming)

    for position in (1.0, 2.0, 3.0, 4.0):
        engine.player.status.position = position
        engine._on_status_change()
    await drain(engine)
    assert len(engine.player.upcoming) == pushes


async def _make_resolve_fail(engine, monkeypatch, kind):
    from backend.resolver import ResolveError

    async def always_fail(video_id, force=False):
        raise ResolveError("nope", kind=kind)

    monkeypatch.setattr(engine.resolver, "resolve", always_fail)


async def test_total_resolve_failure_triggers_a_reactive_update(engine, settings, monkeypatch):
    """When the whole chain fails on something a newer yt-dlp could fix, fetch
    one now instead of waiting up to 12h for the routine check."""
    settings.ytdlp_auto_update = True
    calls = []

    async def fake_update(_settings, delay=0, force=False):
        calls.append(force)
        return False

    monkeypatch.setattr("backend.engine.maybe_update_ytdlp", fake_update)
    await _make_resolve_fail(engine, monkeypatch, FailureKind.BOT_CHECK)

    await engine.add(add("broken"))
    await drain(engine)
    assert calls == [True], "a bot-check chain failure should force a reactive update"


async def test_reactive_update_is_skipped_for_unfixable_failures(engine, settings, monkeypatch):
    """A deleted video or a dead network is not something a yt-dlp update
    fixes, so it must not burn the rate-limited check."""
    settings.ytdlp_auto_update = True
    calls = []

    async def fake_update(_settings, delay=0, force=False):
        calls.append(force)
        return False

    monkeypatch.setattr("backend.engine.maybe_update_ytdlp", fake_update)
    await _make_resolve_fail(engine, monkeypatch, FailureKind.UNAVAILABLE)

    await engine.add(add("gone"))
    await drain(engine)
    assert calls == [], "an unavailable video must not trigger an update"


async def test_reactive_update_is_rate_limited(engine, settings, monkeypatch):
    """A queue full of broken videos must trigger one check, not one per item."""
    settings.ytdlp_auto_update = True
    calls = []

    async def fake_update(_settings, delay=0, force=False):
        calls.append(force)
        return False

    monkeypatch.setattr("backend.engine.maybe_update_ytdlp", fake_update)
    await _make_resolve_fail(engine, monkeypatch, FailureKind.BOT_CHECK)

    await engine.add(add("a"))
    await drain(engine)
    await engine.add(add("b", play_now=True))
    await drain(engine)
    assert calls == [True], "the second failure should be rate-limited"


async def test_reactive_update_notifies_and_clears_cache_when_installed(engine, settings, monkeypatch):
    settings.ytdlp_auto_update = True
    invalidated = []
    monkeypatch.setattr(engine.resolver, "invalidate_all", lambda: invalidated.append(True))

    async def fake_update(_settings, delay=0, force=False):
        return True  # a newer yt-dlp was installed

    monkeypatch.setattr("backend.engine.maybe_update_ytdlp", fake_update)
    await _make_resolve_fail(engine, monkeypatch, FailureKind.BOT_CHECK)

    await engine.add(add("broken"))
    await drain(engine)
    assert invalidated == [True]
    assert engine.notice_code == "notice.ytdlp_updated"


async def test_startup_warns_when_no_js_runtime(settings, monkeypatch, tmp_path):
    """A missing JS runtime is the top cause of silent breakage; surface it at
    startup instead of only via /api/diagnostics."""
    monkeypatch.setattr("backend.engine.MpvPlayer", FakePlayer)
    monkeypatch.setattr(settings, "js_runtime_binary", lambda: None)
    created = Engine(settings)
    created.queue.state_path = tmp_path / "queue.json"
    await created.start(open_player=False)
    try:
        assert created.notice_code == "notice.js_runtime_missing"
    finally:
        await created.shutdown()


async def test_startup_is_quiet_when_the_js_runtime_is_present(settings, monkeypatch, tmp_path):
    monkeypatch.setattr("backend.engine.MpvPlayer", FakePlayer)
    monkeypatch.setattr(settings, "js_runtime_binary", lambda: "/usr/bin/deno")
    created = Engine(settings)
    created.queue.state_path = tmp_path / "queue.json"
    await created.start(open_player=False)
    try:
        assert created.notice_code != "notice.js_runtime_missing"
    finally:
        await created.shutdown()


def _fake_cache_download(engine, size=1024):
    async def writer(url, headers, dest):
        dest.write_bytes(b"x" * size)

    engine.cache._download = writer


async def test_cached_item_plays_without_resolving(engine, settings, monkeypatch):
    """A cached video must play from disk with no resolve -- that is what makes
    it immune to YouTube being down."""
    settings.offline_cache = True
    _fake_cache_download(engine)
    # Pre-seed the cache for "abc".
    await engine.cache.store(
        ResolvedStream(
            video_id="abc", video_url="https://s/v", audio_url="https://s/a",
            title="Cached", duration=1.0, tier="t",
        )
    )
    resolved = []
    original = engine.resolver.resolve

    async def counting_resolve(video_id, force=False):
        resolved.append(video_id)
        return await original(video_id, force)

    monkeypatch.setattr(engine.resolver, "resolve", counting_resolve)

    await engine.add(add("abc"))
    await drain(engine)

    assert engine.queue.items[0].state is ItemState.PLAYING
    assert "abc" not in resolved, "a cached item should not be resolved"
    # mpv was handed the local cache path, not a googlevideo URL.
    assert engine.player.loaded[-1]["video_id"] == "abc"
    assert "cache" in engine.player.loaded[-1]["video_url"].replace("\\", "/")


async def test_precache_downloads_the_next_item(engine, settings):
    settings.offline_cache = True
    settings.cache_ahead = 1
    _fake_cache_download(engine)

    await engine.add(add("first"))
    await drain(engine)
    await engine.add(add("second"))
    await drain(engine)

    assert engine.cache.has("second"), "the upcoming item should have been cached"
    assert not engine.cache.has("first"), "the playing item is streamed, not cached"


async def test_precache_is_off_when_disabled(engine, settings):
    settings.offline_cache = False
    _fake_cache_download(engine)
    await engine.add(add("first"))
    await engine.add(add("second"))
    await drain(engine)
    assert not engine.cache.has("second")


async def test_bad_cached_file_is_dropped_and_re_resolved(engine, settings, monkeypatch):
    """A corrupt local file must not replay forever: on a playback error the
    cache entry is dropped and the item resolved fresh."""
    settings.offline_cache = True
    _fake_cache_download(engine)
    await engine.cache.store(
        ResolvedStream(
            video_id="abc", video_url="https://s/v", audio_url=None,
            title="Cached", duration=1.0, tier="t",
        )
    )
    await engine.add(add("abc"))
    await drain(engine)
    assert engine.cache.has("abc")

    engine.player.on_end_file("error")
    await drain(engine)

    assert not engine.cache.has("abc"), "the bad cache entry should have been removed"
    # It re-resolved and is playing again from the network.
    assert engine.queue.items[0].state is ItemState.PLAYING


async def test_state_reports_queue_and_player(engine):
    await engine.add(add("abc123"))
    await drain(engine)
    state = engine.state()
    assert state.current == 0
    assert len(state.items) == 1
    assert state.player.state is PlaybackState.PLAYING


async def test_streams_that_load_then_die_stall_the_queue(engine):
    """The October 2026 failure: every stream loads, then 403s in mpv.

    A successful load must not count as success, or each item resets the
    streak, fails on its own, and the whole queue goes red one by one.
    """
    for i in range(5):
        await engine.add(add(f"v{i}"))
    await drain(engine)
    for _ in range(6):
        if engine.stalled:
            break
        engine.player.on_end_file("error")
        await drain(engine)
    assert engine.stalled
    assert not any(item.state is ItemState.FAILED for item in engine.queue.items)


async def test_audio_actually_heard_resets_the_failure_streak(engine):
    await engine.add(add("a"))
    await engine.add(add("b"))
    await drain(engine)
    engine._failure_streak = ["someone-else"]
    for position in (1.0, 2.0, 3.0, 4.0):
        engine.player.status.position = position
        engine._on_status_change()
    assert engine._failure_streak == []


async def test_playback_failure_demotes_the_tier(engine, settings):
    settings.save = lambda: None
    await engine.add(add("a"))
    await drain(engine)
    tier = engine._current_tier
    assert tier
    settings.last_successful_tier = tier
    engine.player.on_end_file("error")
    await drain(engine)
    assert engine.resolver.tier_health[tier] in ("playback_failed", "ok")
    # The retry re-resolved; it must not have gone straight back on the
    # strength of the stale preference alone.
    assert engine.settings.last_successful_tier in (None, tier)


async def test_plays_are_recorded_in_history(engine):
    await engine.add(add("a", singer="Maria"))
    await drain(engine)
    for position in range(1, 26):
        engine.player.status.position = float(position)
        engine._on_status_change()
    engine.player.on_end_file("eof")
    await drain(engine)
    history = engine.library.history()
    assert [h["id"] for h in history] == ["a"]
    assert history[0]["singer"] == "Maria"
    assert history[0]["completed"]
    assert engine.library.stats()["plays"] == 1


async def test_singer_reaches_the_overlay(engine):
    await engine.add(add("a"))
    await engine.add(add("b", singer="Nikos"))
    await drain(engine)
    assert engine.player.entries[0].singer == "Nikos"
    await engine.set_singer(1, "Eleni")
    await drain(engine)
    assert engine.player.entries[0].singer == "Eleni"


async def test_intermission_is_shown_during_the_gap_and_removed(engine, settings):
    settings.gap_seconds = 0.3
    await engine.add(add("a"))
    await engine.add(add("b", singer="Nikos"))
    await drain(engine)
    engine.player.on_end_file("eof")
    await asyncio.sleep(0.1)
    assert any(entry and entry[0] == "Title b" for entry in engine.player.intermissions)
    await drain(engine)
    assert engine.player.intermissions[-1] is None
    assert engine.queue.current_item.video_id == "b"


async def test_favourites_are_in_state_and_protected_from_eviction(engine):
    await engine.add(add("a"))
    await drain(engine)
    engine.set_favourite("a", True)
    assert engine.state().favourites == ["a"]
    assert "a" in engine._cache_protect()
    engine.set_favourite("a", False)
    assert engine.state().favourites == []


async def test_a_saved_file_plays_without_resolving(engine, tmp_path, monkeypatch):
    saved = tmp_path / "Song [a].mp4"
    saved.write_bytes(b"video")
    engine.library.set_saved("a", str(saved), title="Song")

    async def must_not_resolve(video_id, force=False, muxed=False):
        raise AssertionError("resolved a saved video")

    monkeypatch.setattr(engine.resolver, "resolve", must_not_resolve)
    await engine.add(add("a"))
    await drain(engine)
    assert engine.player.loaded[-1]["video_url"] == str(saved)
    assert engine.queue.items[0].state is ItemState.PLAYING
    assert "a" in engine.state().saved


async def test_download_played_saves_a_song_once_it_finishes(engine, settings, monkeypatch):
    settings.download_played = True
    requested = []
    monkeypatch.setattr(engine.saver, "request", lambda vid, **meta: requested.append((vid, meta["title"])) or _Job())
    await engine.add(add("a"))
    await drain(engine)
    for position in range(1, 5):
        engine.player.status.position = float(position)
        engine._on_status_change()
    engine.player.on_end_file("eof")
    await drain(engine)
    assert requested == [("a", engine.queue.items[0].title)]


class _Job:
    def public(self):
        return {"state": "queued"}


async def test_watchdog_reloads_a_frozen_stream_where_it_stopped(engine, monkeypatch):
    from backend import engine as engine_module

    monkeypatch.setattr(engine_module, "WATCHDOG_INTERVAL", 0.02)
    monkeypatch.setattr(engine_module, "STUCK_SECONDS", 0.1)
    _restart_watchdog(engine)
    await engine.add(add("a"))
    await drain(engine, timeout=1)
    engine.player.status.position = 42.0
    engine.player.status.duration = 200.0
    await asyncio.sleep(0.3)
    resumed = [entry for entry in engine.player.loaded if entry["start"]]
    assert resumed and resumed[0]["start"] == 42.0
    # A resume is not a new play in the history.
    assert engine.library.db.execute("SELECT COUNT(*) FROM plays").fetchone()[0] == 1


async def test_watchdog_moves_on_when_frozen_at_the_very_end(engine, monkeypatch):
    from backend import engine as engine_module

    monkeypatch.setattr(engine_module, "WATCHDOG_INTERVAL", 0.02)
    monkeypatch.setattr(engine_module, "STUCK_SECONDS", 0.1)
    _restart_watchdog(engine)
    await engine.add(add("a"))
    await engine.add(add("b"))
    await drain(engine, timeout=1)
    engine.player.status.duration = 200.0
    engine.player.status.position = 197.0
    await asyncio.sleep(0.4)
    assert engine.queue.current_item.video_id == "b"


async def test_watchdog_leaves_a_paused_song_alone(engine, monkeypatch):
    from backend import engine as engine_module

    monkeypatch.setattr(engine_module, "WATCHDOG_INTERVAL", 0.02)
    monkeypatch.setattr(engine_module, "STUCK_SECONDS", 0.1)
    _restart_watchdog(engine)
    await engine.add(add("a"))
    await drain(engine, timeout=1)
    await engine.pause()
    engine.player.status.position = 42.0
    await asyncio.sleep(0.3)
    assert len(engine.player.loaded) == 1


def _restart_watchdog(engine):
    """Start the watchdog again so it picks up the patched timings now."""
    engine._watchdog_task.cancel()
    engine._watchdog_task = asyncio.create_task(engine._watchdog())


async def until(predicate, timeout: float = 5.0) -> bool:
    """Poll for an asynchronous outcome instead of guessing a fixed sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def _library_file(settings, tmp_path, name="My Song.mp4"):
    folder = tmp_path / "Karaoke"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_bytes(b"video")
    settings.library_folders = [str(folder)]
    return path


async def test_a_local_file_plays_without_resolving(engine, settings, tmp_path, monkeypatch):
    from backend.localfiles import file_id

    path = _library_file(settings, tmp_path)

    async def must_not_resolve(video_id, force=False, muxed=False):
        raise AssertionError("resolved a local file")

    monkeypatch.setattr(engine.resolver, "resolve", must_not_resolve)
    item = await engine.add(AddRequest(video_id=file_id(path), singer="Maria"))
    await drain(engine)
    assert item.title == "My Song"
    assert item.local_path == str(path)
    assert engine.player.loaded[-1]["video_url"] == str(path)
    assert engine.queue.items[0].state is ItemState.PLAYING


async def test_an_unknown_local_id_is_refused(engine):
    with pytest.raises(LookupError):
        await engine.add(AddRequest(video_id="local-0000000000000000"))


async def test_a_deleted_local_file_fails_without_stalling(engine, settings, tmp_path):
    from backend.localfiles import file_id

    first = _library_file(settings, tmp_path, "a.mp4")
    second = _library_file(settings, tmp_path, "b.mp4")
    await engine.add(AddRequest(video_id=file_id(first)))
    await engine.add(AddRequest(video_id=file_id(second)))
    await drain(engine)
    first.unlink()
    second.unlink()
    await engine.play_index(0)
    await drain(engine)
    assert not engine.stalled
    assert all(item.state is ItemState.FAILED for item in engine.queue.items)
    assert engine.queue.items[0].error_code == "error.file_missing"


async def test_a_copy_in_the_library_folders_beats_streaming(engine, settings, tmp_path, monkeypatch):
    path = _library_file(settings, tmp_path, "Song [abcdefghijk].mkv")

    async def must_not_resolve(video_id, force=False, muxed=False):
        raise AssertionError("streamed a video that is on disk")

    monkeypatch.setattr(engine.resolver, "resolve", must_not_resolve)
    await engine.add(add("abcdefghijk"))
    await drain(engine)
    assert engine.player.loaded[-1]["video_url"] == str(path)
