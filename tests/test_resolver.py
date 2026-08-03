from __future__ import annotations

import pytest

from backend.potoken import PoTokenBundle, PoTokenStore
from backend.resolver import ResolveError, Resolver, extract_headers, split_formats
from backend.ytdlp import FailureKind, YtDlp


@pytest.fixture
def resolver(settings, ytdlp):
    return Resolver(ytdlp, settings, PoTokenStore())


# ------------------------------------------------------------ format parsing


def test_split_formats_separates_video_and_audio():
    info = {
        "requested_formats": [
            {"url": "v", "vcodec": "avc1", "acodec": "none"},
            {"url": "a", "vcodec": "none", "acodec": "mp4a"},
        ]
    }
    assert split_formats(info) == ("v", "a")


def test_split_formats_handles_reversed_order():
    info = {
        "requested_formats": [
            {"url": "a", "vcodec": "none", "acodec": "mp4a"},
            {"url": "v", "vcodec": "avc1", "acodec": "none"},
        ]
    }
    assert split_formats(info) == ("v", "a")


def test_split_formats_falls_back_to_flat_url():
    assert split_formats({"url": "muxed"}) == ("muxed", None)


def test_split_formats_ignores_multiline_url():
    """yt-dlp joins merged URLs with newlines; that is not a playable URL."""
    assert split_formats({"url": "one\ntwo"}) == (None, None)


def test_split_formats_picks_best_muxed_from_formats_list():
    info = {
        "formats": [
            {"url": "low", "vcodec": "avc1", "acodec": "mp4a", "height": 360},
            {"url": "high", "vcodec": "avc1", "acodec": "mp4a", "height": 720},
            {"url": "videoonly", "vcodec": "avc1", "acodec": "none", "height": 1080},
        ]
    }
    assert split_formats(info) == ("high", None)


def test_split_formats_empty():
    assert split_formats({}) == (None, None)


def test_extract_headers_prefers_format_headers():
    info = {
        "requested_formats": [{"url": "v", "http_headers": {"User-Agent": "fmt"}}],
        "http_headers": {"User-Agent": "top"},
    }
    assert extract_headers(info)["User-Agent"] == "fmt"


def test_extract_headers_falls_back_to_top_level():
    assert extract_headers({"http_headers": {"User-Agent": "top"}})["User-Agent"] == "top"


# ------------------------------------------------------------- strategy chain


def test_chain_starts_with_potoken_when_available(settings, ytdlp):
    tokens = PoTokenStore()
    tokens.set(PoTokenBundle(visitor_data="VD", gvs_token="GVS"))
    names = [s.name for s in Resolver(ytdlp, settings, tokens).strategies()]
    assert names[: len(settings.potoken_clients)] == [
        f"potoken:{client}" for client in settings.potoken_clients
    ]


def test_potoken_strategies_label_the_token_for_each_client(settings, ytdlp):
    """A gvs token is bound to the client named in its label.

    Labelling it for a client that cannot serve formats -- as the old single
    `web` strategy did -- makes the whole tier permanently dead.
    """
    tokens = PoTokenStore()
    tokens.set(PoTokenBundle(visitor_data="VD", gvs_token="GVS"))
    chain = {s.name: s.extra_args for s in Resolver(ytdlp, settings, tokens).strategies()}
    for client in settings.potoken_clients:
        args = " ".join(chain[f"potoken:{client}"])
        assert f"player_client={client}" in args
        assert f"po_token={client}.gvs+GVS" in args


def test_chain_without_token_starts_on_configured_clients(resolver, settings):
    names = [s.name for s in resolver.strategies()]
    assert names[0] == "client:default"
    for client in settings.player_clients:
        assert f"client:{client}" in names


def test_chain_includes_cookies_when_configured(settings, ytdlp):
    settings.cookies_from_browser = "firefox"
    names = [s.name for s in Resolver(ytdlp, settings, PoTokenStore()).strategies()]
    assert names[-1] == "cookies"


def test_chain_prefers_last_successful_tier(settings, ytdlp):
    preferred = settings.player_clients[-1]
    settings.last_successful_tier = f"client:{preferred}"
    names = [s.name for s in Resolver(ytdlp, settings, PoTokenStore()).strategies()]
    assert names[0] == f"client:{preferred}"
    # The rest of the order is preserved, not shuffled.
    assert names[1:] == [f"client:{c}" for c in settings.player_clients if c != preferred]


def test_format_selector_respects_max_height(settings, ytdlp):
    settings.max_height = 720
    assert "height<=?720" in Resolver(ytdlp, settings, PoTokenStore()).format_selector()


# ------------------------------------------------------------------- resolve


async def test_resolve_uses_first_working_tier(resolver):
    stream = await resolver.resolve("abc123")
    assert stream.tier == "client:default"
    assert stream.video_url.endswith("client=default")
    assert stream.audio_url.endswith("/audio")
    assert stream.headers["User-Agent"] == "stub-agent"


async def test_resolve_falls_through_to_the_next_client(resolver, settings, monkeypatch):
    monkeypatch.setenv("STUB_FAIL_CLIENTS", settings.player_clients[0])
    monkeypatch.setenv("STUB_FAIL_MODE", "bot")
    stream = await resolver.resolve("abc123")
    assert stream.tier == f"client:{settings.player_clients[1]}"


async def test_resolve_gives_up_when_every_tier_fails(resolver, settings, monkeypatch):
    monkeypatch.setenv("STUB_FAIL_CLIENTS", ",".join(settings.player_clients))
    monkeypatch.setenv("STUB_FAIL_MODE", "bot")
    with pytest.raises(ResolveError) as excinfo:
        await resolver.resolve("abc123")
    assert excinfo.value.kind is FailureKind.BOT_CHECK
    assert len(excinfo.value.attempts) == len(settings.player_clients)


async def test_unavailable_video_aborts_the_chain_immediately(resolver, monkeypatch):
    """No client, token or cookie will resurrect a deleted video."""
    monkeypatch.setenv("STUB_FAIL_MODE", "unavailable")
    with pytest.raises(ResolveError) as excinfo:
        await resolver.resolve("abc123")
    assert excinfo.value.kind is FailureKind.UNAVAILABLE
    assert len(excinfo.value.attempts) == 1


async def test_successful_tier_is_remembered_in_settings(resolver, settings, monkeypatch):
    monkeypatch.setenv("STUB_FAIL_CLIENTS", settings.player_clients[0])
    monkeypatch.setenv("STUB_FAIL_MODE", "bot")
    await resolver.resolve("abc123")
    assert settings.last_successful_tier == f"client:{settings.player_clients[1]}"


async def test_tier_health_records_why_each_tier_failed(resolver, settings, monkeypatch):
    """Without this the first dead tier is invisible: once a later tier works
    it is sorted first and the broken one is never attempted again."""
    monkeypatch.setenv("STUB_FAIL_CLIENTS", settings.player_clients[0])
    monkeypatch.setenv("STUB_FAIL_MODE", "bot")
    await resolver.resolve("abc123")
    assert resolver.tier_health[f"client:{settings.player_clients[0]}"] == "bot_check"
    assert resolver.tier_health[f"client:{settings.player_clients[1]}"] == "ok"


async def test_resolve_is_cached(resolver, settings, monkeypatch):
    first = await resolver.resolve("abc123")
    # Break every client; a cached hit must not care.
    monkeypatch.setenv("STUB_FAIL_CLIENTS", ",".join(settings.player_clients))
    monkeypatch.setenv("STUB_FAIL_MODE", "bot")
    assert (await resolver.resolve("abc123")).video_url == first.video_url


async def test_invalidate_forces_a_fresh_resolve(resolver, settings, monkeypatch):
    await resolver.resolve("abc123")
    resolver.invalidate("abc123")
    monkeypatch.setenv("STUB_FAIL_CLIENTS", ",".join(settings.player_clients))
    monkeypatch.setenv("STUB_FAIL_MODE", "bot")
    with pytest.raises(ResolveError):
        await resolver.resolve("abc123")


async def test_expired_cache_entry_is_refetched(resolver):
    stream = await resolver.resolve("abc123")
    stream.resolved_at -= 10 * 3600
    assert stream.expired
    refreshed = await resolver.resolve("abc123")
    assert not refreshed.expired


async def test_prune_drops_expired_entries(resolver):
    stream = await resolver.resolve("abc123")
    stream.resolved_at -= 10 * 3600
    resolver.prune()
    assert "abc123" not in resolver._cache
