from __future__ import annotations

from backend.invidious import _pick_streams


def test_prefers_highest_adaptive_video_and_audio():
    payload = {
        "adaptiveFormats": [
            {"type": "video/mp4", "url": "v360", "height": 360, "bitrate": "500"},
            {"type": "video/mp4", "url": "v1080", "height": 1080, "bitrate": "2500"},
            {"type": "audio/mp4", "url": "a128", "bitrate": "128000"},
            {"type": "audio/mp4", "url": "a64", "bitrate": "64000"},
        ]
    }
    assert _pick_streams(payload) == ("v1080", "a128")


def test_falls_back_to_muxed_when_adaptive_is_incomplete():
    payload = {
        "adaptiveFormats": [{"type": "video/mp4", "url": "v720", "height": 720}],
        "formatStreams": [{"url": "muxed360", "height": 360}, {"url": "muxed720", "height": 720}],
    }
    assert _pick_streams(payload) == ("muxed720", None)


def test_tolerates_non_numeric_bitrates():
    payload = {
        "adaptiveFormats": [
            {"type": "video/mp4", "url": "v", "height": 720, "bitrate": None},
            {"type": "audio/mp4", "url": "a", "bitrate": "not-a-number"},
        ]
    }
    assert _pick_streams(payload) == ("v", "a")


def test_returns_nothing_when_no_formats():
    assert _pick_streams({}) == (None, None)
