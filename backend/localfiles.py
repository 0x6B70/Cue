"""Videos the user already has on disk, in folders they chose.

The download folder is always one of them, so anything saved from YouTube
shows up here too, plus whatever was copied in by hand. Files are listed,
searched and queued like any song; mpv plays them directly, with no resolve
and no network.

Each file gets a stable id derived from its path, so it can be queued,
starred and kept in history like a YouTube video. A file whose name carries a
YouTube id in brackets -- how the saver names things, "Title [abc123DEF45].mp4"
-- is recognised as that video, and playing the video from anywhere then uses
the local copy instead of streaming it.

The UI only ever sends ids, never paths: an id is turned back into a path by
looking it up in the scan, so nothing outside the configured folders can be
played through the API.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .library import fold, matches

log = logging.getLogger(__name__)

MEDIA_EXTENSIONS = frozenset(
    {
        # video
        ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".ts",
        # audio
        ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac", ".wma",
    }
)  # fmt: skip

LOCAL_PREFIX = "local-"

# A rescan is cheap but not free on a big folder; listing and searching within
# this many seconds reuse the last one.
SCAN_TTL = 30.0

# Bounds so a folder pointed at a whole drive cannot stall the app.
MAX_FILES = 20000
MAX_DEPTH = 8

# The saver's working directory, and anything hidden.
_SKIP_DIRS = frozenset({".cue-partial"})

# "Some title [dQw4w9WgXcQ]" -- the 11-character id the saver appends.
_YOUTUBE_ID = re.compile(r"\s*\[([A-Za-z0-9_-]{11})\]$")


@dataclass
class LocalFile:
    id: str
    path: Path
    title: str
    folder: Path
    size: int
    mtime: float
    youtube_id: str | None = None

    def public(self) -> dict:
        return {
            "id": self.youtube_id or self.id,
            "local_id": self.id,
            "title": self.title,
            "channel": None,
            "duration": None,
            "thumbnail": f"https://i.ytimg.com/vi/{self.youtube_id}/mqdefault.jpg" if self.youtube_id else None,
            "path": str(self.path),
            "folder": str(self.folder),
            "name": self.path.name,
            "size": self.size,
            "mtime": self.mtime,
            "local": True,
        }


def file_id(path: Path) -> str:
    """Stable id for a file: the same path always gives the same id."""
    key = os.path.normcase(str(path.resolve()))
    return LOCAL_PREFIX + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def is_local_id(video_id: str | None) -> bool:
    return bool(video_id) and video_id.startswith(LOCAL_PREFIX)


def describe(path: Path) -> tuple[str, str | None]:
    """(display title, YouTube id if the name carries one) for a media file."""
    stem = path.stem
    found = _YOUTUBE_ID.search(stem)
    if found:
        return stem[: found.start()].strip() or stem, found.group(1)
    return stem, None


class LocalLibrary:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._files: list[LocalFile] = []
        self._by_id: dict[str, LocalFile] = {}
        self._by_youtube: dict[str, LocalFile] = {}
        self._scanned_at = float("-inf")
        self._scanned_for: tuple[str, ...] = ()
        self._lock = threading.Lock()

    # -------------------------------------------------------------- folders

    def folders(self) -> list[Path]:
        """The download folder first, then the user's own, without repeats."""
        candidates = [self.settings.download_directory()]
        for raw in self.settings.library_folders or []:
            if isinstance(raw, str) and raw.strip():
                candidates.append(Path(os.path.expandvars(os.path.expanduser(raw.strip()))))
        seen, out = set(), []
        for folder in candidates:
            key = os.path.normcase(str(folder))
            if key not in seen:
                seen.add(key)
                out.append(folder)
        return out

    def contains(self, path: Path) -> bool:
        """True if `path` lies inside one of the configured folders."""
        try:
            resolved = path.resolve()
        except OSError:
            return False
        for folder in self.folders():
            try:
                resolved.relative_to(folder.resolve())
                return True
            except (ValueError, OSError):
                continue
        return False

    # ----------------------------------------------------------------- scan

    def scan(self, force: bool = False) -> list[LocalFile]:
        """Every media file in the folders, newest first. Blocking: call off-loop."""
        folders = self.folders()
        signature = tuple(str(f) for f in folders)
        with self._lock:
            fresh = time.monotonic() - self._scanned_at < SCAN_TTL and signature == self._scanned_for
            if fresh and not force:
                return self._files
            files: list[LocalFile] = []
            for folder in folders:
                if folder.is_dir():
                    self._walk(folder, folder, 0, files)
                if len(files) >= MAX_FILES:
                    log.warning("library folders hold over %d media files; listing the first %d", MAX_FILES, MAX_FILES)
                    break
            files.sort(key=lambda f: f.mtime, reverse=True)
            self._files = files
            self._by_id = {f.id: f for f in files}
            self._by_youtube = {}
            for f in files:
                if f.youtube_id:
                    self._by_youtube.setdefault(f.youtube_id, f)
            self._scanned_at = time.monotonic()
            self._scanned_for = signature
            return files

    def _walk(self, root: Path, directory: Path, depth: int, out: list[LocalFile]) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            log.debug("cannot read %s", directory, exc_info=True)
            return
        for entry in entries:
            if len(out) >= MAX_FILES:
                return
            name = entry.name
            if name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH and name not in _SKIP_DIRS:
                        self._walk(root, Path(entry.path), depth + 1, out)
                    continue
                if not entry.is_file() or Path(name).suffix.lower() not in MEDIA_EXTENSIONS:
                    continue
                stat = entry.stat()
            except OSError:
                continue
            path = Path(entry.path)
            title, youtube_id = describe(path)
            out.append(LocalFile(file_id(path), path, title, root, stat.st_size, stat.st_mtime, youtube_id))

    def invalidate(self) -> None:
        with self._lock:
            self._scanned_at = float("-inf")

    # -------------------------------------------------------------- queries

    def find(self, local_id: str) -> LocalFile | None:
        """A file by its local id. Rescans once on a miss: it may be brand new."""
        found = self._by_id.get(local_id)
        if found is None:
            self.scan(force=True)
            found = self._by_id.get(local_id)
        return found if found and found.path.is_file() else None

    def copy_of(self, youtube_id: str) -> Path | None:
        """A local file of this YouTube video, if one is in the folders."""
        self.scan()  # reuses the last scan within SCAN_TTL
        found = self._by_youtube.get(youtube_id)
        return found.path if found and found.path.is_file() else None

    def search(self, query: str = "", limit: int | None = None) -> list[LocalFile]:
        files = self.scan()
        if query.strip():
            files = [f for f in files if matches(query, f.title, f.path.name, str(f.path.parent.name))]
        return files[:limit] if limit else files

    def suggest(self, prefix: str, limit: int = 5) -> list[str]:
        """File titles that start with (or contain) what was typed."""
        folded = fold(prefix).strip()
        if not folded:
            return []
        starts, contains = [], []
        for f in self.scan():
            name = fold(f.title)
            if name.startswith(folded):
                starts.append(f.title)
            elif folded in name:
                contains.append(f.title)
        seen, out = set(), []
        for title in [*starts, *contains]:
            if fold(title) not in seen:
                seen.add(fold(title))
                out.append(title)
            if len(out) >= limit:
                break
        return out
