"""The play queue.

Deliberately *not* mpv's playlist. Stream URLs expire after a few hours and
each item may need a different resolver strategy, so Python owns the ordering
and hands mpv exactly one freshly-resolved file at a time.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .models import ItemState, QueueItem

log = logging.getLogger(__name__)


class PlayQueue:
    def __init__(self, state_path: Path | None = None):
        self.items: list[QueueItem] = []
        self.current: int = -1
        self.state_path = state_path

    # ------------------------------------------------------------------ read

    @property
    def current_item(self) -> QueueItem | None:
        if 0 <= self.current < len(self.items):
            return self.items[self.current]
        return None

    def find(self, uid: str) -> int:
        for index, item in enumerate(self.items):
            if item.uid == uid:
                return index
        return -1

    def upcoming(self, count: int) -> list[QueueItem]:
        """The next `count` items that have not been played yet."""
        start = self.current + 1
        return self.items[start : start + count]

    # ----------------------------------------------------------------- write

    def add(self, item: QueueItem, play_now: bool = False) -> int:
        if play_now:
            index = self.current + 1 if self.current >= 0 else 0
            self.items.insert(index, item)
        else:
            self.items.append(item)
            index = len(self.items) - 1
        self.save()
        return index

    def remove(self, index: int) -> QueueItem | None:
        if not 0 <= index < len(self.items):
            return None
        removed = self.items.pop(index)
        if index < self.current:
            self.current -= 1
        elif index == self.current:
            # The playing item was removed; point at the gap it left behind so
            # the next advance() lands on the following item.
            self.current -= 1
        self.save()
        return removed

    def reorder(self, from_index: int, to_index: int) -> bool:
        if not (0 <= from_index < len(self.items) and 0 <= to_index < len(self.items)):
            return False
        if from_index == to_index:
            return True
        current_uid = self.current_item.uid if self.current_item else None
        item = self.items.pop(from_index)
        self.items.insert(to_index, item)
        if current_uid:
            self.current = self.find(current_uid)
        self.save()
        return True

    def clear(self) -> None:
        self.items.clear()
        self.current = -1
        self.save()

    def clear_played(self) -> int:
        current_uid = self.current_item.uid if self.current_item else None
        before = len(self.items)
        self.items = [i for i in self.items if i.state is not ItemState.PLAYED]
        self.current = self.find(current_uid) if current_uid else -1
        self.save()
        return before - len(self.items)

    # -------------------------------------------------------------- movement

    def select(self, index: int) -> QueueItem | None:
        if not 0 <= index < len(self.items):
            return None
        self.current = index
        return self.items[index]

    def advance(self) -> QueueItem | None:
        """Move to the next item that has not already failed."""
        index = self.current + 1
        while index < len(self.items):
            if self.items[index].state is not ItemState.FAILED:
                self.current = index
                return self.items[index]
            index += 1
        self.current = len(self.items) - 1 if self.items else -1
        return None

    def previous(self) -> QueueItem | None:
        index = self.current - 1
        while index >= 0:
            if self.items[index].state is not ItemState.FAILED:
                self.current = index
                return self.items[index]
            index -= 1
        return None

    # ----------------------------------------------------------- persistence

    def save(self) -> None:
        if not self.state_path:
            return
        payload = {
            "items": [item.model_dump(mode="json") for item in self.items],
            "current": self.current,
        }
        try:
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            log.warning("could not persist queue state", exc_info=True)

    def load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("queue state unreadable, starting empty", exc_info=True)
            return
        items = []
        for entry in raw.get("items", []):
            try:
                item = QueueItem.model_validate(entry)
            except ValueError:
                continue
            # Resolved URLs from a previous session are long gone.
            if item.state in (ItemState.RESOLVED, ItemState.PLAYING):
                item.state = ItemState.PENDING
            items.append(item)
        self.items = items
        self.current = min(raw.get("current", -1), len(items) - 1)
