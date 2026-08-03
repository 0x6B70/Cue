"""SponsorBlock lookups.

Pre-roll and mid-roll ads are already absent — we play the raw stream — but
in-video sponsor reads are part of the video itself. This fetches community
segment data so the engine can seek past them.

Uses the hash-prefix endpoint: only the first four hex characters of the
video ID's SHA-256 are sent, so the API never learns exactly what is playing.
"""

from __future__ import annotations

import hashlib
import logging

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://sponsor.ajay.app/api/skipSegments"
TIMEOUT = 8.0
# Skipping a sub-second segment is more jarring than sitting through it.
MIN_SEGMENT_SECONDS = 1.0


class Segment:
    __slots__ = ("start", "end", "category")

    def __init__(self, start: float, end: float, category: str):
        self.start = start
        self.end = end
        self.category = category

    def contains(self, position: float) -> bool:
        return self.start <= position < self.end - 0.2

    def __repr__(self) -> str:
        return f"Segment({self.category} {self.start:.1f}-{self.end:.1f})"


def parse_segments(entries: list[dict], categories: list[str]) -> list[Segment]:
    wanted = set(categories)
    segments = []
    for entry in entries:
        category = entry.get("category")
        bounds = entry.get("segment") or []
        if category not in wanted or len(bounds) != 2:
            continue
        try:
            start, end = float(bounds[0]), float(bounds[1])
        except (TypeError, ValueError):
            continue
        if end - start >= MIN_SEGMENT_SECONDS:
            segments.append(Segment(start, end, category))
    return sorted(segments, key=lambda s: s.start)


async def fetch_segments(video_id: str, categories: list[str]) -> list[Segment]:
    """Return skippable segments for a video. Never raises — this is optional."""
    if not categories:
        return []
    prefix = hashlib.sha256(video_id.encode()).hexdigest()[:4]
    params = {"categories": '["' + '","'.join(categories) + '"]'}

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{API_BASE}/{prefix}", params=params)
            if response.status_code == 404:
                return []
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        log.debug("sponsorblock lookup failed for %s", video_id, exc_info=True)
        return []

    for entry in payload if isinstance(payload, list) else []:
        if entry.get("videoID") != video_id:
            continue
        segments = parse_segments(entry.get("segments") or [], categories)
        log.info("sponsorblock: %d segment(s) for %s", len(segments), video_id)
        return segments
    return []
