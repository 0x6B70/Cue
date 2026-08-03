from __future__ import annotations

import pytest

from backend.search import SearchService, parse_entry, thumbnail_for
from backend.ytdlp import YtDlpError


def test_thumbnail_prefers_entry_thumbnails():
    entry = {"thumbnails": [{"url": "https://a/small.jpg", "width": 90}, {"url": "https://a/mid.jpg", "width": 320}]}
    assert thumbnail_for(entry, "abc") == "https://a/mid.jpg"


def test_thumbnail_falls_back_to_deterministic_url():
    assert thumbnail_for({}, "abc123") == "https://i.ytimg.com/vi/abc123/mqdefault.jpg"


def test_parse_entry_skips_playlists_and_idless_entries():
    assert parse_entry({"_type": "playlist", "id": "PL1", "title": "x"}) is None
    assert parse_entry({"title": "no id"}) is None


def test_parse_entry_marks_live():
    result = parse_entry({"id": "a", "title": "t", "live_status": "is_live"})
    assert result.live is True


def test_parse_entry_defaults_missing_title():
    assert parse_entry({"id": "a"}).title == "(untitled)"


async def test_search_returns_normalised_results(ytdlp):
    results = await SearchService(ytdlp).search("anything", limit=3)
    assert len(results) == 3
    assert results[0].id == "vid000"
    assert results[0].channel == "Channel 0"
    assert results[0].duration == 100


async def test_search_empty_query_short_circuits(ytdlp):
    assert await SearchService(ytdlp).search("   ") == []


async def test_search_propagates_failures(ytdlp, monkeypatch):
    monkeypatch.setenv("STUB_FAIL_MODE", "network")
    with pytest.raises(YtDlpError) as excinfo:
        await SearchService(ytdlp).search("anything")
    assert "Network problem" in excinfo.value.user_message
