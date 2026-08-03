"""Last-resort fallback through public Invidious instances.

Slower and flakier than direct extraction — instances rate-limit and disappear
without notice — but it converts a hard failure into a degraded success, which
is the difference between a queue that skips a video and one that stops.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 12.0


class InvidiousError(RuntimeError):
    pass


def _pick_streams(payload: dict) -> tuple[str | None, str | None]:
    """Return (video_url, audio_url) from an Invidious video payload."""
    adaptive = payload.get("adaptiveFormats") or []

    def bitrate(fmt: dict) -> int:
        try:
            return int(fmt.get("bitrate") or 0)
        except (TypeError, ValueError):
            return 0

    videos = [f for f in adaptive if (f.get("type") or "").startswith("video/") and f.get("url")]
    audios = [f for f in adaptive if (f.get("type") or "").startswith("audio/") and f.get("url")]

    if videos and audios:
        best_video = max(videos, key=lambda f: (int(f.get("height") or 0), bitrate(f)))
        best_audio = max(audios, key=bitrate)
        return best_video["url"], best_audio["url"]

    # Muxed legacy formats — lower quality, but a single URL that always plays.
    muxed = [f for f in payload.get("formatStreams") or [] if f.get("url")]
    if muxed:
        best = max(muxed, key=lambda f: int(f.get("height") or 0))
        return best["url"], None

    return None, None


class InvidiousClient:
    def __init__(self, instances: list[str]):
        self.instances = instances

    async def fetch(self, video_id: str) -> dict:
        """Try each instance in turn; return the first usable payload."""
        errors = []
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            for base in self.instances:
                url = f"{base.rstrip('/')}/api/v1/videos/{video_id}"
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    payload = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append(f"{base}: {exc}")
                    continue

                video_url, audio_url = _pick_streams(payload)
                if not video_url:
                    errors.append(f"{base}: no playable formats")
                    continue

                log.info("resolved %s via invidious instance %s", video_id, base)
                return {
                    "video_url": video_url,
                    "audio_url": audio_url,
                    "title": payload.get("title") or video_id,
                    "duration": payload.get("lengthSeconds"),
                    "instance": base,
                }

        raise InvidiousError("; ".join(errors) or "no instances configured")
