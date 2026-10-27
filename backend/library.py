"""The user's own library: play history, statistics, favourites, past searches.

Kept in SQLite in the user data dir. It is the one store here that only ever
grows, and the stats are aggregate queries over it, which is what a database
is for; the stdlib module needs nothing bundled.

All calls are synchronous. They are local, indexed, and a few milliseconds at
most -- not worth an executor hop, and keeping them on the event loop means no
locking around the connection.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

# A play only counts once it has run this long, so skipping through a queue
# does not inflate every skipped song's play count.
MIN_COUNTED_SECONDS = 20.0

# Plays that have been closed by finish_play. The one playing right now has a
# row from the moment it starts (so history can show it) but no time yet, and
# must not count in the stats until it is over.
_FINISHED = "(listened_seconds > 0 OR completed = 1)"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    video_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    channel TEXT,
    duration REAL,
    thumbnail TEXT,
    favourite INTEGER NOT NULL DEFAULT 0,
    favourited_at REAL,
    play_count INTEGER NOT NULL DEFAULT 0,
    listened_seconds REAL NOT NULL DEFAULT 0,
    last_played REAL
);
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    singer TEXT,
    started_at REAL NOT NULL,
    listened_seconds REAL NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS plays_started ON plays(started_at);
CREATE TABLE IF NOT EXISTS searches (
    query TEXT PRIMARY KEY,
    uses INTEGER NOT NULL DEFAULT 1,
    last_used REAL NOT NULL
);
"""


def fold(text: str | None) -> str:
    """Case- and accent-insensitive form for matching.

    SQLite's LIKE only folds ASCII case, which is useless for Greek: "μαρίζα"
    must match "ΜΑΡΙΖΑ". Matching happens in Python on this form instead.
    """
    decomposed = unicodedata.normalize("NFD", (text or "").casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def matches(query: str, *fields: str | None) -> bool:
    """True if every word of `query` appears in at least one of `fields`."""
    words = fold(query).split()
    haystack = " ".join(fold(f) for f in fields if f)
    return all(word in haystack for word in words)


class Library:
    def __init__(self, path: Path):
        self.path = path
        self._db: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        """The connection, opened on first use and again after close()."""
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(self.path), check_same_thread=False)
            db.row_factory = sqlite3.Row
            db.executescript(_SCHEMA)
            _migrate(db)
            db.commit()
            self._db = db
        return self._db

    def close(self) -> None:
        db, self._db = self._db, None
        if db is None:
            return
        try:
            db.close()
        except sqlite3.Error:
            log.debug("closing the library failed", exc_info=True)

    # --------------------------------------------------------------- tracks

    def remember(self, video_id: str, title: str, channel=None, duration=None, thumbnail=None) -> None:
        """Insert or refresh a track's metadata without touching its counters."""
        self.db.execute(
            """
            INSERT INTO tracks (video_id, title, channel, duration, thumbnail)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                title = excluded.title,
                channel = COALESCE(excluded.channel, tracks.channel),
                duration = COALESCE(excluded.duration, tracks.duration),
                thumbnail = COALESCE(excluded.thumbnail, tracks.thumbnail)
            """,
            (video_id, title or video_id, channel, duration, thumbnail),
        )
        self.db.commit()

    def all_tracks(self) -> list[dict]:
        return [_track(r) for r in self.db.execute("SELECT * FROM tracks")]

    def track(self, video_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM tracks WHERE video_id = ?", (video_id,)).fetchone()
        return _track(row) if row else None

    # ---------------------------------------------------------------- plays

    def start_play(self, video_id: str, singer: str | None = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO plays (video_id, singer, started_at) VALUES (?, ?, ?)",
            (video_id, singer, time.time()),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_play(self, play_id: int, listened: float, completed: bool) -> None:
        """Close a play. Too short to count, and it is dropped altogether."""
        row = self.db.execute("SELECT video_id, started_at FROM plays WHERE id = ?", (play_id,)).fetchone()
        if not row:
            return
        listened = max(0.0, float(listened))
        if listened < MIN_COUNTED_SECONDS and not completed:
            self.db.execute("DELETE FROM plays WHERE id = ?", (play_id,))
        else:
            self.db.execute(
                "UPDATE plays SET listened_seconds = ?, completed = ? WHERE id = ?",
                (listened, int(completed), play_id),
            )
            self.db.execute(
                """
                UPDATE tracks SET play_count = play_count + 1,
                    listened_seconds = listened_seconds + ?, last_played = ?
                WHERE video_id = ?
                """,
                (listened, row["started_at"], row["video_id"]),
            )
        self.db.commit()

    def history(self, limit: int = 200, query: str = "") -> list[dict]:
        """Most recent plays first, each with its track's metadata."""
        rows = self.db.execute(
            """
            SELECT t.*, p.id AS play_id, p.started_at AS play_started,
                p.listened_seconds AS play_listened, p.completed AS play_completed, p.singer AS play_singer
            FROM plays p JOIN tracks t ON t.video_id = p.video_id
            ORDER BY p.started_at DESC LIMIT ?
            """,
            (limit if not query else 5000,),
        ).fetchall()
        # Per-song totals for the stats line on each entry, in one query
        # rather than one per row.
        totals = {
            r["video_id"]: r
            for r in self.db.execute(
                "SELECT video_id, MIN(started_at) AS first_played, SUM(completed) AS completed_count, "
                "COUNT(DISTINCT singer) AS singers FROM plays WHERE " + _FINISHED + " GROUP BY video_id"
            )
        }
        out = []
        for row in rows:
            entry = _track(row)
            song = totals.get(row["video_id"])
            entry.update(
                # Aliased in the query: plays and tracks share column names,
                # and the play's would otherwise shadow the song's totals.
                play_id=row["play_id"],
                played_at=row["play_started"],
                listened=row["play_listened"],
                completed=bool(row["play_completed"]),
                singer=row["play_singer"],
                first_played=song["first_played"] if song else None,
                completed_count=(song["completed_count"] or 0) if song else 0,
                singer_count=(song["singers"] or 0) if song else 0,
            )
            if query and not matches(query, entry["title"], entry["channel"], entry["singer"]):
                continue
            out.append(entry)
            if len(out) >= limit:
                break
        return out

    def track_stats(self, video_id: str, recent: int = 20) -> dict | None:
        """Everything known about one song's plays, for its History details."""
        track = self.track(video_id)
        if track is None:
            return None
        finished = "video_id = ? AND " + _FINISHED
        totals = self.db.execute(
            "SELECT COUNT(*) AS plays, COALESCE(SUM(listened_seconds), 0) AS seconds, "
            "COALESCE(SUM(completed), 0) AS completed, MIN(started_at) AS first, MAX(started_at) AS last "
            "FROM plays WHERE " + finished,
            (video_id,),
        ).fetchone()
        singers = [
            {"singer": r["singer"], "plays": r["plays"]}
            for r in self.db.execute(
                "SELECT singer, COUNT(*) AS plays FROM plays WHERE " + finished
                + " AND singer IS NOT NULL AND singer != '' GROUP BY singer ORDER BY plays DESC",
                (video_id,),
            )
        ]
        plays = [
            {
                "played_at": r["started_at"],
                "singer": r["singer"],
                "listened": r["listened_seconds"],
                "completed": bool(r["completed"]),
            }
            for r in self.db.execute(
                "SELECT started_at, singer, listened_seconds, completed FROM plays WHERE " + finished
                + " ORDER BY started_at DESC LIMIT ?",
                (video_id, recent),
            )
        ]
        return {
            **track,
            "plays": totals["plays"],
            "listened_seconds": totals["seconds"],
            "completed_count": totals["completed"],
            "first_played": totals["first"],
            "last_played": totals["last"],
            "singers": singers,
            "recent": plays,
        }

    # ---------------------------------------------------------------- saved

    def set_saved(self, video_id: str, path: str | None, **meta) -> None:
        """Record (or with None, forget) the file a video was saved to."""
        if meta.get("title"):
            self.remember(video_id, **meta)
        elif not self.track(video_id):
            self.remember(video_id, video_id)
        self.db.execute(
            "UPDATE tracks SET saved_path = ?, saved_at = ? WHERE video_id = ?",
            (path, time.time() if path else None, video_id),
        )
        self.db.commit()

    def saved_path(self, video_id: str) -> Path | None:
        """The saved file for a video, if it is still on disk."""
        row = self.db.execute("SELECT saved_path FROM tracks WHERE video_id = ?", (video_id,)).fetchone()
        if not row or not row[0]:
            return None
        path = Path(row[0])
        return path if path.is_file() else None

    def saved(self, query: str = "") -> list[dict]:
        """Saved videos whose files still exist, newest first."""
        rows = self.db.execute(
            "SELECT * FROM tracks WHERE saved_path IS NOT NULL ORDER BY saved_at DESC"
        ).fetchall()
        out = []
        for row in rows:
            path = Path(row["saved_path"])
            if not path.is_file():
                continue
            track = _track(row)
            if query and not matches(query, track["title"], track["channel"], path.name):
                continue
            track["path"] = str(path)
            track["size"] = path.stat().st_size
            out.append(track)
        return out

    def saved_ids(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT video_id FROM tracks WHERE saved_path IS NOT NULL")]

    # ----------------------------------------------------------- favourites

    def set_favourite(self, video_id: str, favourite: bool, **meta) -> None:
        if meta.get("title"):
            self.remember(video_id, **meta)
        elif not self.track(video_id):
            self.remember(video_id, video_id)
        self.db.execute(
            "UPDATE tracks SET favourite = ?, favourited_at = ? WHERE video_id = ?",
            (int(favourite), time.time() if favourite else None, video_id),
        )
        self.db.commit()

    def favourite_ids(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT video_id FROM tracks WHERE favourite = 1")]

    def favourites(self, query: str = "") -> list[dict]:
        rows = self.db.execute("SELECT * FROM tracks WHERE favourite = 1 ORDER BY favourited_at DESC").fetchall()
        tracks = [_track(r) for r in rows]
        return [t for t in tracks if not query or matches(query, t["title"], t["channel"])]

    # ------------------------------------------------------------- searches

    def record_search(self, query: str) -> None:
        query = " ".join(query.split())
        if not query:
            return
        self.db.execute(
            """
            INSERT INTO searches (query, uses, last_used) VALUES (?, 1, ?)
            ON CONFLICT(query) DO UPDATE SET uses = uses + 1, last_used = excluded.last_used
            """,
            (query, time.time()),
        )
        self.db.commit()

    def suggest(self, prefix: str, limit: int = 8) -> list[str]:
        """Past searches, then track titles, that start with (or contain) `prefix`."""
        folded = fold(prefix).strip()
        if not folded:
            return []
        searches = [r[0] for r in self.db.execute("SELECT query FROM searches ORDER BY uses DESC, last_used DESC LIMIT 500")]
        titles = [r[0] for r in self.db.execute("SELECT title FROM tracks ORDER BY play_count DESC, last_played DESC LIMIT 1000")]
        starts, contains = [], []
        for text in [*searches, *titles]:
            f = fold(text)
            if f == folded:
                continue
            if f.startswith(folded):
                starts.append(text)
            elif folded in f:
                contains.append(text)
        seen, out = set(), []
        for text in [*starts, *contains]:
            key = fold(text)
            if key not in seen:
                seen.add(key)
                out.append(text)
            if len(out) >= limit:
                break
        return out

    # ---------------------------------------------------------------- stats

    def stats(self, days: int = 14) -> dict:
        db = self.db
        totals = db.execute(
            "SELECT COUNT(*) AS plays, COALESCE(SUM(listened_seconds), 0) AS seconds, "
            "COUNT(DISTINCT video_id) AS tracks FROM plays WHERE " + _FINISHED
        ).fetchone()
        top_tracks = [
            _track(r)
            for r in db.execute(
                "SELECT * FROM tracks WHERE play_count > 0 ORDER BY play_count DESC, listened_seconds DESC LIMIT 10"
            )
        ]
        top_channels = [
            {"channel": r["channel"], "plays": r["plays"], "seconds": r["seconds"]}
            for r in db.execute(
                """
                SELECT t.channel AS channel, COUNT(*) AS plays, SUM(p.listened_seconds) AS seconds
                FROM plays p JOIN tracks t ON t.video_id = p.video_id AND (p.listened_seconds > 0 OR p.completed = 1)
                WHERE t.channel IS NOT NULL GROUP BY t.channel ORDER BY plays DESC LIMIT 10
                """
            )
        ]
        top_singers = [
            {"singer": r["singer"], "plays": r["plays"]}
            for r in db.execute(
                "SELECT singer, COUNT(*) AS plays FROM plays WHERE (listened_seconds > 0 OR completed = 1) AND singer IS NOT NULL AND singer != '' "
                "GROUP BY singer ORDER BY plays DESC LIMIT 10"
            )
        ]
        # Bucketed by local calendar day, oldest first, with empty days kept
        # so a chart of it has no gaps.
        now = time.localtime()
        today = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
        start = today - (days - 1) * 86400
        buckets = {}
        for i in range(days):
            day = time.localtime(start + i * 86400 + 3600)  # +1h steps over DST edges
            buckets[time.strftime("%Y-%m-%d", day)] = {"plays": 0, "seconds": 0.0}
        for r in db.execute("SELECT started_at, listened_seconds FROM plays WHERE started_at >= ? AND " + _FINISHED, (start,)):
            key = time.strftime("%Y-%m-%d", time.localtime(r["started_at"]))
            if key in buckets:
                buckets[key]["plays"] += 1
                buckets[key]["seconds"] += r["listened_seconds"]
        favourites = db.execute("SELECT COUNT(*) FROM tracks WHERE favourite = 1").fetchone()[0]
        return {
            "plays": totals["plays"],
            "listened_seconds": totals["seconds"],
            "tracks": totals["tracks"],
            "favourites": favourites,
            "top_tracks": top_tracks,
            "top_channels": top_channels,
            "top_singers": top_singers,
            "days": [{"day": k, **v} for k, v in buckets.items()],
        }


def _migrate(db: sqlite3.Connection) -> None:
    """Add columns introduced after a library was first created."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(tracks)")}
    if "saved_path" not in columns:
        db.execute("ALTER TABLE tracks ADD COLUMN saved_path TEXT")
    if "saved_at" not in columns:
        db.execute("ALTER TABLE tracks ADD COLUMN saved_at REAL")


def _track(row: sqlite3.Row) -> dict:
    return {
        "id": row["video_id"],
        "title": row["title"],
        "channel": row["channel"],
        "duration": row["duration"],
        "thumbnail": row["thumbnail"],
        "favourite": bool(row["favourite"]),
        "plays": row["play_count"],
        "listened_seconds": row["listened_seconds"],
        "last_played": row["last_played"],
    }
