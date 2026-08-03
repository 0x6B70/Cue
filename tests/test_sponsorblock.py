from __future__ import annotations

from backend.sponsorblock import Segment, parse_segments

CATEGORIES = ["sponsor", "selfpromo"]


def test_parse_filters_unwanted_categories():
    entries = [
        {"category": "sponsor", "segment": [10.0, 30.0]},
        {"category": "music_offtopic", "segment": [40.0, 60.0]},
    ]
    segments = parse_segments(entries, CATEGORIES)
    assert [s.category for s in segments] == ["sponsor"]


def test_parse_drops_sub_second_segments():
    """Skipping a blink is more jarring than sitting through it."""
    entries = [{"category": "sponsor", "segment": [10.0, 10.4]}]
    assert parse_segments(entries, CATEGORIES) == []


def test_parse_sorts_by_start_time():
    entries = [
        {"category": "sponsor", "segment": [90.0, 100.0]},
        {"category": "selfpromo", "segment": [10.0, 20.0]},
    ]
    assert [s.start for s in parse_segments(entries, CATEGORIES)] == [10.0, 90.0]


def test_parse_tolerates_malformed_entries():
    entries = [
        {"category": "sponsor"},
        {"category": "sponsor", "segment": ["x", "y"]},
        {"category": "sponsor", "segment": [1.0]},
        {"category": "sponsor", "segment": [5.0, 25.0]},
    ]
    assert len(parse_segments(entries, CATEGORIES)) == 1


def test_segment_contains_excludes_the_tail():
    """The tail guard stops a seek landing back inside the segment."""
    segment = Segment(10.0, 20.0, "sponsor")
    assert segment.contains(10.0)
    assert segment.contains(15.0)
    assert not segment.contains(19.9)
    assert not segment.contains(20.0)
    assert not segment.contains(9.9)
