"""The SQLite-backed library: history, favourites, searches, stats."""

from __future__ import annotations

import pytest

from backend.library import MIN_COUNTED_SECONDS, Library, fold, matches


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path / "library.db")
    yield lib
    lib.close()


def _play(lib, video_id, seconds=60.0, completed=True, singer=None, title=None):
    lib.remember(video_id, title or f"Title {video_id}", channel="Chan")
    play = lib.start_play(video_id, singer)
    lib.finish_play(play, seconds, completed)


def test_fold_ignores_case_and_greek_accents():
    assert fold("Μαρίζα") == fold("ΜΑΡΙΖΑ")
    assert matches("μαριζα ριζου", "Μαρίζα Ρίζου - Είσαι εδώ")
    assert not matches("μαριζα xyz", "Μαρίζα Ρίζου")


def test_a_skipped_play_is_not_counted(library):
    _play(library, "a", seconds=MIN_COUNTED_SECONDS - 1, completed=False)
    assert library.history() == []
    assert library.track("a")["plays"] == 0


def test_counted_plays_build_history_and_stats(library):
    _play(library, "a", singer="Maria")
    _play(library, "a")
    _play(library, "b")
    assert library.history()[0]["id"] == "b"
    stats = library.stats()
    assert stats["plays"] == 3
    assert stats["tracks"] == 2
    assert stats["top_tracks"][0]["id"] == "a"
    assert stats["top_singers"] == [{"singer": "Maria", "plays": 1}]
    assert len(stats["days"]) == 14
    assert stats["days"][-1]["plays"] == 3


def test_history_filter(library):
    _play(library, "a", title="Daft Punk - Get Lucky")
    _play(library, "b", title="Μαρίζα Ρίζου")
    assert [h["id"] for h in library.history(query="ριζου")] == ["b"]


def test_favourites_round_trip(library):
    library.set_favourite("x", True, title="Song X", channel="C")
    assert library.favourite_ids() == ["x"]
    assert library.favourites()[0]["title"] == "Song X"
    library.set_favourite("x", False)
    assert library.favourites() == []


def test_suggest_prefers_prefix_matches_from_searches(library):
    library.record_search("daft punk")
    library.record_search("daft punk")
    library.record_search("the daft club")
    _play(library, "a", title="Daft Punk - Around the World")
    suggestions = library.suggest("daft")
    assert suggestions[0] == "daft punk"
    assert "the daft club" in suggestions
    assert library.suggest("daft punk") == ["Daft Punk - Around the World"]


def test_reopens_after_close(library):
    library.close()
    library.record_search("again")
    assert library.suggest("aga") == ["again"]


def test_history_entries_carry_their_songs_totals(library):
    _play(library, "a", seconds=200, singer="Maria")
    _play(library, "a", seconds=30, completed=False, singer="Nikos")
    _play(library, "b", seconds=50)
    entry = next(h for h in library.history() if h["id"] == "a")
    assert entry["plays"] == 2
    assert entry["listened_seconds"] == 230
    assert entry["completed_count"] == 1
    assert entry["singer_count"] == 2
    assert entry["first_played"] is not None


def test_track_stats_breaks_a_song_down(library):
    _play(library, "a", seconds=200, singer="Maria")
    _play(library, "a", seconds=180, singer="Maria")
    _play(library, "a", seconds=30, completed=False, singer="Nikos")
    stats = library.track_stats("a")
    assert stats["plays"] == 3 and stats["completed_count"] == 2
    assert stats["singers"] == [{"singer": "Maria", "plays": 2}, {"singer": "Nikos", "plays": 1}]
    assert [p["listened"] for p in stats["recent"]][0] == 30  # newest first
    assert library.track_stats("never") is None
