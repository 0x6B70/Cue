"""YouTube search via yt-dlp's `ytsearch` — no API key, no quota.

The Data API's search.list costs 100 of a default 10,000 daily quota units,
i.e. 100 searches per day for the whole application. yt-dlp's flat search has
no such ceiling and needs no credentials.
"""

from __future__ import annotations

import logging

from .models import SearchResult
from .ytdlp import YtDlp

log = logging.getLogger(__name__)

MAX_RESULTS = 50


def thumbnail_for(entry: dict, video_id: str) -> str | None:
    """Pick a thumbnail, preferring a mid-size one from the entry itself."""
    thumbs = entry.get("thumbnails") or []
    usable = [t for t in thumbs if t.get("url")]
    if usable:
        # Prefer the widest thumbnail that is still small enough to be cheap.
        ranked = sorted(usable, key=lambda t: abs((t.get("width") or 320) - 320))
        return ranked[0]["url"]
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
    return None


def parse_entry(entry: dict) -> SearchResult | None:
    video_id = entry.get("id")
    if not video_id or entry.get("_type") == "playlist":
        return None
    live_status = entry.get("live_status")
    return SearchResult(
        id=video_id,
        title=entry.get("title") or "(untitled)",
        channel=entry.get("channel") or entry.get("uploader"),
        duration=entry.get("duration"),
        thumbnail=thumbnail_for(entry, video_id),
        live=live_status in ("is_live", "is_upcoming"),
    )


class SearchService:
    def __init__(self, ytdlp: YtDlp):
        self.ytdlp = ytdlp

    async def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        query = query.strip()
        if not query:
            return []
        limit = max(1, min(limit, MAX_RESULTS))

        # --flat-playlist avoids a full extraction per hit, which is the
        # difference between ~1s and ~30s for 20 results.
        info = await self.ytdlp.run_json(
            ["-J", "--flat-playlist", f"ytsearch{limit}:{query}"],
            timeout=45,
        )

        results = []
        for entry in info.get("entries") or []:
            parsed = parse_entry(entry)
            if parsed:
                results.append(parsed)
        log.info("search %r -> %d results", query, len(results))
        return results
