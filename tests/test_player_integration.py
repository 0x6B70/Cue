"""Integration tests against a real mpv process.

Skipped when mpv is not installed. These exercise the parts that unit tests
with a fake player cannot: the JSON IPC handshake, property observation, the
end-file event, and the per-file loadfile options (including the fallback
taken by mpv older than 0.38).

Playback uses lavfi test sources, so nothing is downloaded and no display is
needed.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from backend.player import (
    OVERLAY_RES_Y,
    MpvPlayer,
    PlayerUnavailable,
    compose_upcoming,
)

pytestmark = pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv is not installed")

# A scratch overlay id for the layout probe below, so inspecting the overlay
# never disturbs the real one.
PROBE_OVERLAY_ID = 42

# Null output devices keep this headless; force_window off avoids needing X.
HEADLESS = {"force_window": False, "vo": "null", "ao": "null", "osc": False}

VIDEO_SOURCE = "av://lavfi:testsrc=duration=1:size=160x120:rate=10"
AUDIO_SOURCE = "av://lavfi:sine=duration=1:frequency=440"


@pytest.fixture
async def player(settings):
    settings.mpv_options = HEADLESS
    settings.volume = 40
    instance = MpvPlayer(settings, asyncio.get_running_loop())
    try:
        await instance.ensure_started()
    except PlayerUnavailable as exc:  # pragma: no cover - environment dependent
        pytest.skip(str(exc))
    yield instance
    instance.shutdown()


async def wait_for(predicate, timeout: float = 15.0) -> bool:
    """Poll until predicate is true, since mpv reports state asynchronously."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_mpv_starts_and_reports_running(player):
    assert player.running
    assert player.status.window_open


async def test_load_plays_and_reports_position(player):
    await player.load(VIDEO_SOURCE, None, "test clip")
    assert await wait_for(lambda: player.status.position > 0), "mpv never reported a position"


async def test_end_file_fires_at_end_of_playback(player):
    reasons: list[str] = []
    player.on_end_file = reasons.append

    await player.load(VIDEO_SOURCE, None, "test clip")
    assert await wait_for(lambda: reasons), "end-file never fired"
    assert reasons[0] == "eof"


async def test_separate_audio_track_is_attached(player):
    """The video/audio split is the whole reason for per-file options."""
    await player.load(VIDEO_SOURCE, AUDIO_SOURCE, "test clip")
    assert await wait_for(lambda: player.status.position > 0)
    track_count = await player._call(lambda: player.mpv.command("get_property", "track-list/count"))
    # One video track from the file plus one audio track from --audio-file.
    assert track_count >= 2


async def test_headers_are_accepted_without_error(player):
    await player.load(VIDEO_SOURCE, None, "test clip", headers={"User-Agent": "probe/1.0"})
    assert await wait_for(lambda: player.status.position > 0)


async def test_header_values_containing_commas_stay_intact(player):
    """Regression: comma-joining these silently corrupts every request.

    yt-dlp hands us browser headers whose values contain commas. Flattened into
    one string, mpv's list parser splits them into malformed header lines and
    googlevideo answers 403 -- playback fails a second after it starts, with
    nothing in the log to say why.
    """
    headers = {
        "User-Agent": "probe/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-us,en;q=0.5",
    }
    await player.load(VIDEO_SOURCE, None, "test clip", headers=headers)
    assert await wait_for(lambda: player.status.position > 0)

    fields = await player._call(
        lambda: player.mpv.command("get_property", "http-header-fields")
    )
    assert fields == [
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language: en-us,en;q=0.5",
    ]


async def test_header_fields_do_not_leak_between_loads(player):
    await player.load(VIDEO_SOURCE, None, "first", headers={"Accept-Language": "en-us,en;q=0.5"})
    assert await wait_for(lambda: player.status.position > 0)

    await player.load(VIDEO_SOURCE, None, "second")
    fields = await player._call(
        lambda: player.mpv.command("get_property", "http-header-fields")
    )
    assert fields == []


async def test_pause_and_resume_round_trip(player):
    await player.load(VIDEO_SOURCE, None, "test clip")
    assert await wait_for(lambda: player.status.position > 0)

    await player.set_paused(True)
    assert await wait_for(lambda: bool(player.mpv.command("get_property", "pause")))

    await player.set_paused(False)
    assert await wait_for(lambda: not player.mpv.command("get_property", "pause"))


async def test_volume_is_applied_and_persisted(player, settings):
    await player.set_volume(63)
    assert await wait_for(lambda: int(player.mpv.command("get_property", "volume")) == 63)
    assert settings.volume == 63


async def test_stop_returns_to_idle(player):
    await player.load(VIDEO_SOURCE, None, "test clip")
    assert await wait_for(lambda: player.status.position > 0)
    await player.stop()
    from backend.models import PlaybackState

    assert player.status.state is PlaybackState.IDLE


async def test_loading_a_second_file_replaces_the_first(player):
    """Python owns the queue, so mpv must never accumulate a playlist."""
    await player.load(VIDEO_SOURCE, None, "first")
    assert await wait_for(lambda: player.status.position > 0)
    await player.load(VIDEO_SOURCE, None, "second")
    count = await player._call(lambda: player.mpv.command("get_property", "playlist-count"))
    assert count == 1


async def layout(player, text: str) -> dict:
    """Ask mpv to lay `text` out and report the box it occupies.

    No property reads an overlay back, so the only way to see what mpv made of
    a payload is to hand it the same payload with compute_bounds set. Bounds
    are reported even under vo=null, which keeps this headless.
    """
    return await player._call(
        lambda: player.mpv.command(
            "osd-overlay",
            PROBE_OVERLAY_ID,
            "ass-events",
            text,
            0,
            OVERLAY_RES_Y,
            0,
            False,
            True,
        )
    )


async def test_upcoming_overlay_reaches_mpv(player):
    titles = ["Alpha clip", "Beta clip", "Gamma clip"]
    await player.set_upcoming(titles)

    for title in titles:
        assert title in player._overlay_text

    box = await layout(player, player._overlay_text)
    assert box["x1"] > box["x0"] and box["y1"] > box["y0"], "mpv laid out nothing"


async def test_overlay_stays_clear_of_the_osc(player):
    """mpv's default OSC is a full-width bar along the bottom of the window.

    A bottom-anchored overlay measures out at y=719 of 720 -- right behind the
    controls -- which is why this sits in the top corner instead.
    """
    await player.set_upcoming(["Alpha", "Beta", "Gamma"])
    box = await layout(player, player._overlay_text)
    assert box["y1"] < OVERLAY_RES_Y / 2, "overlay reaches into the OSC's half of the window"


async def test_overlay_is_anchored_to_the_right_edge(player):
    """Longer text has to grow leftwards, not run off the right of the frame."""
    short = await layout(player, compose_upcoming(["A"]))
    long = await layout(player, compose_upcoming(["A" * 30]))

    assert long["x1"] == pytest.approx(short["x1"], abs=2), "right edge moved"
    assert long["x0"] < short["x0"], "overlay did not grow leftwards"


async def test_ass_markup_in_a_title_is_printed_not_interpreted(player):
    """Titles are arbitrary text from YouTube, and libass reads `{...}` as an
    override block. Unescaped, `{\\fs90}` really does resize the overlay."""
    plain = await layout(player, compose_upcoming(["ordinary title"]))
    hostile = await layout(player, compose_upcoming(["{\\fs90\\c&HFF0000&}ordinary title"]))

    assert hostile["y1"] - hostile["y0"] == pytest.approx(plain["y1"] - plain["y0"], abs=6)


async def test_long_titles_are_truncated_to_fit(player):
    await player.set_upcoming(
        ["A very long video title that would otherwise run the entire width of the frame"]
    )
    assert "…" in player._overlay_text

    box = await layout(player, player._overlay_text)
    assert box["x0"] > 0, "an untruncated title spans the whole video"


async def test_clearing_the_overlay(player):
    await player.set_upcoming(["Alpha"])
    assert player._overlay_text

    await player.set_upcoming([])
    assert player._overlay_text == ""


async def test_overlay_is_redrawn_when_the_window_is_reopened(player):
    """A new mpv process starts with no overlays at all, so what the engine
    last asked for has to be drawn again from this side."""
    await player.set_upcoming(["Alpha", "Beta"])
    drawn = player._overlay_text
    assert drawn

    player.shutdown()
    assert player._overlay_text == ""

    await player.ensure_started()
    assert player._overlay_text == drawn


async def test_engine_pushes_the_overlay_through_to_a_real_mpv(settings, tmp_path):
    """Covers the seam the other tests leave open.

    test_engine.py drives a fake player, and everything above drives mpv
    directly, so nothing otherwise checks that Engine.notify() reaches a real
    process. Queue state is set up by hand: resolving and playback are beside
    the point here, and would drag the network in.
    """
    from backend.engine import Engine
    from backend.models import QueueItem

    settings.mpv_options = HEADLESS
    engine = Engine(settings)
    engine.queue.state_path = tmp_path / "queue.json"
    await engine.start(open_player=True)
    try:
        if not engine.player.running:  # pragma: no cover - environment dependent
            pytest.skip("mpv did not start")

        for n, title in enumerate(["Playing now", "Second video", "Third video"]):
            engine.queue.items.append(QueueItem(video_id=f"v{n}", title=title))
        engine.queue.current = 0
        engine.notify()

        assert await wait_for(lambda: "Second video" in engine.player._overlay_text)
        assert "Third video" in engine.player._overlay_text
        # The playing item is not "upcoming".
        assert "Playing now" not in engine.player._overlay_text
    finally:
        await engine.shutdown()


async def test_setting_the_overlay_without_a_window_does_not_raise(settings):
    """The engine pushes on every queue change, window open or not."""
    instance = MpvPlayer(settings, asyncio.get_running_loop())
    await instance.set_upcoming(["Alpha"])
    assert instance._upcoming == ["Alpha"]
    assert instance._overlay_text == ""


async def test_missing_binary_raises_player_unavailable(settings):
    settings.mpv_path = "/nonexistent/mpv-not-here"
    instance = MpvPlayer(settings, asyncio.get_running_loop())
    with pytest.raises(PlayerUnavailable):
        await instance.ensure_started()
