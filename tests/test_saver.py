"""Saving videos to the user's folder (backend/saver.py)."""

from __future__ import annotations

import asyncio
from collections import namedtuple

import pytest

from backend import saver as saver_module
from backend.cache import OfflineCache
from backend.library import Library
from backend.resolver import ResolvedStream
from backend.saver import Saver, safe_filename, sniff_extension

MP4_HEAD = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20
WEBM_HEAD = b"\x1a\x45\xdf\xa3" + b"\x00" * 20


class FakeResolver:
    def __init__(self, audio=True):
        self.calls = []
        self.audio = audio

    async def resolve(self, video_id, force=False, muxed=False):
        self.calls.append((video_id, muxed))
        return ResolvedStream(
            video_id,
            f"https://g.test/{video_id}/{'muxed' if muxed else 'video'}",
            None if muxed or not self.audio else f"https://g.test/{video_id}/audio",
            f"Title {video_id}",
            100.0,
            "test",
            {"User-Agent": "UA"},
        )


@pytest.fixture
def parts(settings, tmp_path, monkeypatch):
    settings.download_dir = str(tmp_path / "saved")
    settings.offline_cache = True
    settings.ffmpeg_path = str(tmp_path / "no-ffmpeg.exe")  # absent: no ffmpeg
    library = Library(tmp_path / "library.db")
    cache = OfflineCache(settings, tmp_path / "cache")
    resolver = FakeResolver()
    fetched = []

    async def fake_download(url, headers, dest, progress=None):
        fetched.append(url)
        dest.write_bytes((WEBM_HEAD if url.endswith("audio") else MP4_HEAD) + b"x" * 100)
        if progress:
            progress(124, 124)

    monkeypatch.setattr(saver_module, "download_ranged", fake_download)
    # The free-space guard reads the real disk; these tests write a few bytes
    # and must not fail because the machine running them is nearly full.
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(saver_module.shutil, "disk_usage", lambda _: usage(100 * 1024**3, 0, 100 * 1024**3))
    changes = []
    saver = Saver(settings, resolver, cache, library, lambda: changes.append(1))
    Parts = namedtuple("Parts", "saver settings library cache resolver fetched changes tmp")
    yield Parts(saver, settings, library, cache, resolver, fetched, changes, tmp_path)
    library.close()


async def finish(saver):
    while saver._tasks:
        await asyncio.gather(*saver._tasks, return_exceptions=True)


def test_safe_filename_is_legal_and_keeps_the_id():
    name = safe_filename('AC/DC: "Back" in <Black>?  ', "abc123", "mp4")
    assert name == "AC_DC_ _Back_ in _Black__ [abc123].mp4"
    assert len(safe_filename("x" * 500, "id", "mkv")) < 140


def test_sniff_extension(tmp_path):
    (tmp_path / "a").write_bytes(MP4_HEAD)
    (tmp_path / "b").write_bytes(WEBM_HEAD)
    (tmp_path / "c").write_bytes(b"junk")
    assert sniff_extension(tmp_path / "a", "x") == "mp4"
    assert sniff_extension(tmp_path / "b", "x") == "webm"
    assert sniff_extension(tmp_path / "c", "x") == "x"


async def test_without_ffmpeg_a_video_is_saved_in_the_single_file_format(parts):
    job = parts.saver.request("vid", title="My Song")
    await finish(parts.saver)
    assert job.state == "done", job.error
    assert parts.resolver.calls == [("vid", True)]
    path = parts.library.saved_path("vid")
    assert path.name == "My Song [vid].mp4"
    assert path.parent == parts.settings.download_directory()
    assert not (path.parent / ".cue-partial").exists()
    assert parts.library.saved()[0]["title"] == "My Song"


async def test_audio_only_save_uses_the_audio_stream(parts):
    parts.settings.download_format = "audio"
    parts.saver.request("vid", title="Tune")
    await finish(parts.saver)
    path = parts.library.saved_path("vid")
    assert path.name == "Tune [vid].webm"
    assert parts.fetched == ["https://g.test/vid/audio"]


async def test_audio_save_reuses_the_offline_cache_and_keeps_it(parts):
    parts.settings.download_format = "audio"

    async def cached_download(url, headers, dest):
        dest.write_bytes(MP4_HEAD + b"a" * 10)

    parts.cache._download = cached_download
    await parts.cache.store(await parts.resolver.resolve("vid"), {"title": "Tune"})
    parts.resolver.calls.clear()
    parts.saver.request("vid", title="Tune")
    await finish(parts.saver)
    assert parts.resolver.calls == []  # no network at all
    assert parts.library.saved_path("vid").suffix == ".m4a"
    assert parts.cache.has("vid")  # copied, not moved


async def test_with_ffmpeg_both_streams_are_merged(parts, monkeypatch):
    parts.settings.ffmpeg_path = None
    monkeypatch.setattr(parts.settings, "ffmpeg_binary", lambda: "ffmpeg")
    merged = []

    async def fake_mux(video, audio, out):
        merged.append(out.suffix)
        out.write_bytes(b"merged")
        return True

    monkeypatch.setattr(parts.saver, "_mux", fake_mux)
    parts.saver.request("vid", title="Song")
    await finish(parts.saver)
    assert parts.resolver.calls == [("vid", False)]
    assert merged == [".mkv"]  # webm audio cannot go in mp4
    assert parts.library.saved_path("vid").name == "Song [vid].mkv"


async def test_a_full_disk_refuses_to_start(parts, monkeypatch):
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(saver_module.shutil, "disk_usage", lambda _: usage(10, 10, 1024))
    job = parts.saver.request("vid", title="Song")
    await finish(parts.saver)
    assert job.state == "failed"
    assert "free space" in job.error
    assert parts.fetched == []


async def test_repeat_requests_do_not_save_twice(parts):
    parts.saver.request("vid", title="Song")
    parts.saver.request("vid", title="Song")
    await finish(parts.saver)
    again = parts.saver.request("vid", title="Song")
    assert again.state == "done"
    assert len(parts.fetched) == 1


async def test_delete_removes_the_file(parts):
    parts.saver.request("vid", title="Song")
    await finish(parts.saver)
    path = parts.library.saved_path("vid")
    assert parts.saver.delete("vid")
    assert not path.exists()
    assert parts.library.saved_path("vid") is None
