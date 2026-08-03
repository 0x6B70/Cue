from __future__ import annotations

import pytest

from backend.cache import GIB, OfflineCache
from backend.resolver import ResolvedStream


@pytest.fixture
def cache(settings, tmp_path):
    settings.offline_cache = True
    settings.cache_budget_gb = 1.0
    return OfflineCache(settings, tmp_path / "cache")


def _stream(video_id, audio=True):
    return ResolvedStream(
        video_id=video_id,
        video_url=f"https://stub/{video_id}/video",
        audio_url=f"https://stub/{video_id}/audio" if audio else None,
        title=f"Title {video_id}",
        duration=100.0,
        tier="test",
        headers={"User-Agent": "x"},
    )


def _fake_download(cache, size=1024):
    """Replace the network download with writing `size` bytes to the target."""

    async def writer(url, headers, dest):
        dest.write_bytes(b"x" * size)

    cache._download = writer


async def test_store_then_has_and_paths(cache):
    _fake_download(cache)
    assert await cache.store(_stream("abc")) is True
    assert cache.has("abc")
    video, audio = cache.paths("abc")
    assert video.exists() and audio.exists()


async def test_video_only_stream_caches_without_audio(cache):
    _fake_download(cache)
    assert await cache.store(_stream("noaudio", audio=False)) is True
    video, audio = cache.paths("noaudio")
    assert video.exists()
    assert audio is None


async def test_a_missing_file_counts_as_not_cached(cache):
    _fake_download(cache)
    await cache.store(_stream("abc"))
    cache.paths("abc")[0].unlink()  # someone cleared the disk under us
    assert cache.has("abc") is False


async def test_disabled_cache_stores_nothing(cache):
    cache.settings.offline_cache = False
    _fake_download(cache)
    assert await cache.store(_stream("abc")) is False
    assert not cache.has("abc")


async def test_failed_download_leaves_nothing_behind(cache):
    async def boom(url, headers, dest):
        dest.write_bytes(b"partial")
        raise RuntimeError("network died")

    cache._download = boom
    assert await cache.store(_stream("abc")) is False
    assert not cache.has("abc")
    # No stray .part or half files.
    assert list(cache.dir.glob("abc*")) == []


async def test_eviction_drops_played_items_first(cache):
    # Video-only so each item is exactly 300 MiB; budget is 1 GiB.
    _fake_download(cache, size=300 * 1024**2)
    for vid in ("a", "b", "c", "d"):
        await cache.store(_stream(vid, audio=False))
    # Four * 300 MiB = 1.2 GiB, over budget: one must go, and a played one first.
    cache.evict_to_budget(played_ids={"b"}, protect=set())
    assert not cache.has("b")
    assert all(cache.has(v) for v in ("a", "c", "d"))


async def test_eviction_never_drops_protected_items(cache):
    _fake_download(cache, size=400 * 1024**2)
    for vid in ("a", "b", "c"):
        await cache.store(_stream(vid, audio=False))
    # 1.2 GiB over a 1 GiB budget, but everything is protected: nothing evicted.
    cache.evict_to_budget(played_ids={"a", "b", "c"}, protect={"a", "b", "c"})
    assert all(cache.has(v) for v in ("a", "b", "c"))


async def test_eviction_uses_lru_within_the_same_played_state(cache):
    _fake_download(cache, size=400 * 1024**2)  # video-only: 400 MiB each
    for vid in ("old", "new"):
        await cache.store(_stream(vid, audio=False))
    cache._index["old"]["atime"] = 1.0  # force "old" to be least recently used
    cache._index["new"]["atime"] = 2.0
    # 800 MiB is under the 1 GiB budget, so nothing is evicted yet.
    cache.evict_to_budget(played_ids=set(), protect=set())
    assert cache.has("old") and cache.has("new")
    # Shrink below one item: the least-recently-used must be the one to go.
    cache.settings.cache_budget_gb = 0.5
    cache.evict_to_budget(played_ids=set(), protect=set())
    assert cache.has("new")
    assert not cache.has("old")


async def test_budget_tolerates_junk(cache):
    cache.settings.cache_budget_gb = "not a number"
    assert cache.budget_bytes() == 5 * GIB


async def test_index_survives_a_reload(cache, settings):
    _fake_download(cache)
    await cache.store(_stream("abc"))
    reloaded = OfflineCache(settings, cache.dir)
    assert reloaded.has("abc")
