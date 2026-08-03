"""Optional on-disk cache of resolved videos.

The one mechanism that survives *any* future YouTube change: a video already on
disk plays with no resolve, no network and no bot check. It turns an ongoing
bypass problem into a one-time one -- the escape hatch docs/reliability.md names
as the right answer when reliability matters more than disk.

Deliberately shallow. Downloading is heavier than resolving, and resolving the
whole queue at once is the fastest way to trip a bot check, so only a few items
ahead are ever fetched (`cache_ahead`, default 1).

Video and audio are stored as separate files, matching how the player already
consumes a stream. Merging them into one file would need ffmpeg, which is not
bundled -- keeping them apart avoids that dependency entirely.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

from .config import Settings
from .resolver import ResolvedStream

log = logging.getLogger(__name__)

GIB = 1024**3
DOWNLOAD_TIMEOUT = 300.0
_CHUNK = 1 << 16


class OfflineCache:
    """Stores resolved streams on disk, keyed by video id, under a size budget.

    The index (video id -> filenames, size, last-access time) is a small JSON
    file beside the media, so an interrupted session leaves a consistent view:
    a media file with no index entry is simply re-downloaded.
    """

    def __init__(self, settings: Settings, directory: Path):
        self.settings = settings
        self.dir = directory
        self._index_path = directory / "index.json"
        self._index: dict[str, dict] = {}
        self._load_index()

    def enabled(self) -> bool:
        return bool(getattr(self.settings, "offline_cache", False))

    # --------------------------------------------------------------- queries

    def paths(self, video_id: str) -> tuple[Path, Path | None] | None:
        """(video, audio) paths if fully cached, else None.

        `audio` is None for a muxed/video-only stream. A missing file on disk
        counts as not cached, so a half-deleted entry re-downloads cleanly.
        """
        entry = self._index.get(video_id)
        if not entry:
            return None
        video_path = self.dir / entry["video"]
        if not video_path.exists():
            return None
        audio_path: Path | None = None
        if entry.get("audio"):
            audio_path = self.dir / entry["audio"]
            if not audio_path.exists():
                return None
        return video_path, audio_path

    def has(self, video_id: str) -> bool:
        return self.paths(video_id) is not None

    def touch(self, video_id: str) -> None:
        """Mark a cached item as just used, so eviction sees it as fresh."""
        entry = self._index.get(video_id)
        if entry:
            entry["atime"] = time.time()
            self._save_index()

    # ----------------------------------------------------------------- store

    async def store(self, stream: ResolvedStream) -> bool:
        """Download a resolved stream to disk. Returns True once fully cached.

        Best-effort: a failed download leaves nothing behind and returns False,
        so caching never blocks or breaks playback.
        """
        if not self.enabled():
            return False
        if self.has(stream.video_id):
            self.touch(stream.video_id)
            return True

        self.dir.mkdir(parents=True, exist_ok=True)
        video_name = f"{stream.video_id}.video"
        audio_name = f"{stream.video_id}.audio" if stream.audio_url else None
        try:
            await self._download(stream.video_url, stream.headers, self.dir / video_name)
            if audio_name:
                await self._download(stream.audio_url, stream.headers, self.dir / audio_name)
        except Exception:  # noqa: BLE001 - caching must never break playback
            log.info("caching %s failed", stream.video_id, exc_info=True)
            self._remove_files(video_name, audio_name)
            return False

        size = (self.dir / video_name).stat().st_size
        if audio_name:
            size += (self.dir / audio_name).stat().st_size
        self._index[stream.video_id] = {
            "video": video_name,
            "audio": audio_name,
            "size": size,
            "atime": time.time(),
        }
        self._save_index()
        log.info("cached %s (%.1f MiB)", stream.video_id, size / (1024**2))
        return True

    async def _download(self, url: str, headers: dict[str, str] | None, dest: Path) -> None:
        """Stream a URL to `dest` atomically (via a .part file)."""
        tmp = dest.with_suffix(dest.suffix + ".part")
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=headers or {}) as response:
                response.raise_for_status()
                with open(tmp, "wb") as handle:
                    async for chunk in response.aiter_bytes(_CHUNK):
                        handle.write(chunk)
        tmp.replace(dest)

    # -------------------------------------------------------------- eviction

    def budget_bytes(self) -> float:
        try:
            return max(0.0, float(self.settings.cache_budget_gb)) * GIB
        except (TypeError, ValueError):
            return 5 * GIB

    def total_size(self) -> int:
        return sum(entry.get("size", 0) for entry in self._index.values())

    def evict_to_budget(self, played_ids: set[str], protect: set[str] = frozenset()) -> None:
        """Drop cached items until under budget: played ones first, then oldest.

        `protect` is never evicted -- the currently playing item and the ones
        about to play, so eviction cannot remove what was just cached.
        """
        budget = self.budget_bytes()
        total = self.total_size()
        if total <= budget:
            return
        candidates = [(vid, e) for vid, e in self._index.items() if vid not in protect]
        # played items sort first (False < True), oldest access within each group.
        candidates.sort(key=lambda kv: (kv[0] not in played_ids, kv[1].get("atime", 0)))
        for video_id, entry in candidates:
            if total <= budget:
                break
            total -= entry.get("size", 0)
            self.remove(video_id)

    def remove(self, video_id: str) -> None:
        entry = self._index.pop(video_id, None)
        if not entry:
            return
        self._remove_files(entry.get("video"), entry.get("audio"))
        self._save_index()

    # ----------------------------------------------------------- persistence

    def _remove_files(self, *names: str | None) -> None:
        for name in names:
            if name:
                (self.dir / name).unlink(missing_ok=True)
                (self.dir / f"{name}.part").unlink(missing_ok=True)

    def _load_index(self) -> None:
        try:
            self._index = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._index = {}

    def _save_index(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self._index_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._index, indent=2), encoding="utf-8")
            tmp.replace(self._index_path)
        except OSError:
            log.debug("could not persist cache index", exc_info=True)
