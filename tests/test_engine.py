from __future__ import annotations

import asyncio
import time

import pytest

from backend.engine import Engine
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
        self.fail_load = False
        self.on_end_file = None
        self.on_status_change = None
        self.on_window_closed = None

    async def ensure_started(self):
        self.running = True
        self.status.window_open = True

    def shutdown(self):
        self.running = False

    async def load(self, video_url, audio_url, title, headers=None, video_id=None):
        if self.fail_load:
            from backend.player import PlayerUnavailable

            raise PlayerUnavailable("no window")
        await self.ensure_started()
        self.loaded.append({"video_url": video_url, "audio_url": audio_url, "title": title, "video_id": video_id})
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

    async def set_upcoming(self, titles):
        self.upcoming.append(list(titles))


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


async def test_queue_where_nothing_resolves_terminates(engine, monkeypatch):
    from backend.resolver import ResolveError

    async def always_fail(video_id, force=False):
        raise ResolveError("nope")

    monkeypatch.setattr(engine.resolver, "resolve", always_fail)
    for i in range(4):
        await engine.add(add(f"v{i}"))
    await drain(engine)
    assert all(item.state is ItemState.FAILED for item in engine.queue.items)


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
