from __future__ import annotations

import json

from backend.models import ItemState, QueueItem
from backend.playqueue import PlayQueue


def make(video_id: str) -> QueueItem:
    return QueueItem(video_id=video_id, title=f"Title {video_id}")


def filled(count: int = 4) -> PlayQueue:
    queue = PlayQueue()
    for i in range(count):
        queue.add(make(f"v{i}"))
    return queue


def test_add_appends_and_play_now_inserts_after_current():
    queue = filled(3)
    queue.select(1)
    queue.add(make("urgent"), play_now=True)
    assert [i.video_id for i in queue.items] == ["v0", "v1", "urgent", "v2"]
    # The currently playing item must not shift out from under us.
    assert queue.current_item.video_id == "v1"


def test_advance_skips_failed_items():
    queue = filled(4)
    queue.select(0)
    queue.items[1].state = ItemState.FAILED
    queue.items[2].state = ItemState.FAILED
    assert queue.advance().video_id == "v3"


def test_advance_returns_none_at_end():
    queue = filled(2)
    queue.select(1)
    assert queue.advance() is None


def test_remove_before_current_keeps_pointer_on_same_item():
    queue = filled(4)
    queue.select(2)
    queue.remove(0)
    assert queue.current_item.video_id == "v2"


def test_remove_current_leaves_gap_so_next_advance_lands_on_following():
    queue = filled(4)
    queue.select(1)
    queue.remove(1)
    assert queue.advance().video_id == "v2"


def test_reorder_tracks_the_playing_item():
    queue = filled(4)
    queue.select(3)
    queue.reorder(3, 0)
    assert queue.current == 0
    assert queue.current_item.video_id == "v3"


def test_reorder_rejects_out_of_range():
    queue = filled(2)
    assert queue.reorder(0, 9) is False


def test_clear_played_drops_played_and_keeps_current():
    queue = filled(4)
    queue.items[0].state = ItemState.PLAYED
    queue.items[1].state = ItemState.PLAYED
    queue.select(2)
    assert queue.clear_played() == 2
    assert queue.current_item.video_id == "v2"
    assert queue.current == 0


def test_persistence_round_trip_resets_stale_resolved_state(tmp_path):
    path = tmp_path / "queue.json"
    queue = PlayQueue(path)
    queue.add(make("a"))
    queue.add(make("b"))
    queue.select(1)
    queue.items[1].state = ItemState.PLAYING
    queue.save()

    restored = PlayQueue(path)
    restored.load()
    assert [i.video_id for i in restored.items] == ["a", "b"]
    assert restored.current == 1
    # URLs resolved in a previous session are long expired.
    assert restored.items[1].state is ItemState.PENDING


def test_load_tolerates_corrupt_state(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text("{not json", encoding="utf-8")
    queue = PlayQueue(path)
    queue.load()
    assert queue.items == []


def test_load_skips_unparseable_entries(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"items": [{"nope": 1}, {"video_id": "ok", "title": "t"}], "current": 0}))
    queue = PlayQueue(path)
    queue.load()
    assert [i.video_id for i in queue.items] == ["ok"]


def test_upcoming_returns_items_after_current():
    queue = filled(5)
    queue.select(1)
    assert [i.video_id for i in queue.upcoming(2)] == ["v2", "v3"]
