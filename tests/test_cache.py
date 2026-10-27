from __future__ import annotations

import pytest

from backend.cache import GIB, OfflineCache
from backend.resolver import ResolvedStream

# One KiB expressed in the GB unit cache_budget_gb uses.
KIB_IN_GB = 1024 / GIB


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
    # Scaled down to bytes: a 1 KiB budget and 300-byte items behave exactly
    # like 1 GiB and 300 MiB, without writing over a gigabyte to disk per run.
    cache.settings.cache_budget_gb = KIB_IN_GB
    _fake_download(cache, size=300)
    for vid in ("a", "b", "c", "d"):
        await cache.store(_stream(vid, audio=False))
    # Four * 300 B = 1.2 KiB, over budget: one must go, and a played one first.
    cache.evict_to_budget(played_ids={"b"}, protect=set())
    assert not cache.has("b")
    assert all(cache.has(v) for v in ("a", "c", "d"))


async def test_eviction_never_drops_protected_items(cache):
    cache.settings.cache_budget_gb = KIB_IN_GB
    _fake_download(cache, size=400)
    for vid in ("a", "b", "c"):
        await cache.store(_stream(vid, audio=False))
    # 1.2 KiB over a 1 KiB budget, but everything is protected: nothing evicted.
    cache.evict_to_budget(played_ids={"a", "b", "c"}, protect={"a", "b", "c"})
    assert all(cache.has(v) for v in ("a", "b", "c"))


async def test_eviction_uses_lru_within_the_same_played_state(cache):
    cache.settings.cache_budget_gb = KIB_IN_GB
    _fake_download(cache, size=400)  # video-only: 400 B each
    for vid in ("old", "new"):
        await cache.store(_stream(vid, audio=False))
    cache._index["old"]["atime"] = 1.0  # force "old" to be least recently used
    cache._index["new"]["atime"] = 2.0
    # 800 B is under the 1 KiB budget, so nothing is evicted yet.
    cache.evict_to_budget(played_ids=set(), protect=set())
    assert cache.has("old") and cache.has("new")
    # Shrink below one item: the least-recently-used must be the one to go.
    cache.settings.cache_budget_gb = 0.5 * KIB_IN_GB
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


async def test_download_uses_bounded_ranges(cache, monkeypatch):
    """googlevideo 403s a plain GET for some clients; ranged requests work."""
    import httpx

    from backend import cache as cache_module

    body = bytes(range(256)) * 100  # 25,600 bytes
    monkeypatch.setattr(cache_module, "RANGE_BYTES", 10_000)
    ranges = []

    def handler(request):
        header = request.headers.get("Range")
        if not header:
            return httpx.Response(403)
        start, end = (int(x) for x in header.removeprefix("bytes=").split("-"))
        ranges.append((start, end))
        chunk = body[start : end + 1]
        return httpx.Response(
            206, content=chunk, headers={"Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{len(body)}"}
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.cache.httpx.AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    dest = cache.dir / "x.video"
    cache.dir.mkdir(parents=True, exist_ok=True)
    await cache._download("https://g.test/x", {}, dest)
    assert dest.read_bytes() == body
    assert ranges == [(0, 9_999), (10_000, 19_999), (20_000, 29_999)]


async def test_metadata_is_kept_for_listing(cache):
    _fake_download(cache)
    await cache.store(_stream("abc"), {"title": "Nice Song", "channel": "Chan", "thumbnail": "t.jpg"})
    [entry] = cache.entries()
    assert entry["title"] == "Nice Song"
    assert entry["channel"] == "Chan"
    assert cache.ids() == ["abc"]
