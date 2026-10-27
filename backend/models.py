"""Wire types shared by the HTTP API and the WebSocket broadcast."""

from __future__ import annotations

import uuid
from enum import Enum

from pydantic import BaseModel, Field


class ItemState(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    PLAYING = "playing"
    PLAYED = "played"
    FAILED = "failed"


class PlaybackState(str, Enum):
    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"


class SearchResult(BaseModel):
    id: str
    title: str
    channel: str | None = None
    duration: float | None = None
    thumbnail: str | None = None
    live: bool = False


class QueueItem(BaseModel):
    uid: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    video_id: str
    title: str
    channel: str | None = None
    duration: float | None = None
    thumbnail: str | None = None
    # Who is singing it, for karaoke nights. Shown in the in-video overlay
    # instead of (or beside) the title, per settings.overlay_label.
    singer: str | None = None
    # A file from the user's library folders, played as-is: no resolve, no
    # prefetch, no SponsorBlock. Set by the engine from the scan, never taken
    # from a request, so only files inside those folders can be queued.
    local_path: str | None = None
    state: ItemState = ItemState.PENDING
    # `error` stays the English text so anything reading the API still works;
    # `error_code` is what the UI translates. A missing key falls back to text.
    error: str | None = None
    error_code: str | None = None

    @classmethod
    def from_result(cls, result: SearchResult) -> "QueueItem":
        return cls(
            video_id=result.id,
            title=result.title,
            channel=result.channel,
            duration=result.duration,
            thumbnail=result.thumbnail,
        )


class PlayerStatus(BaseModel):
    state: PlaybackState = PlaybackState.IDLE
    position: float = 0.0
    duration: float | None = None
    volume: int = 100
    muted: bool = False
    window_open: bool = False


class AppState(BaseModel):
    """The single payload pushed to every connected client on any change."""

    items: list[QueueItem] = Field(default_factory=list)
    current: int = -1
    player: PlayerStatus = Field(default_factory=PlayerStatus)
    # As with QueueItem.error: `notice` is the English text, `notice_code` plus
    # `notice_params` is what the UI renders in the active locale.
    notice: str | None = None
    notice_code: str | None = None
    notice_params: dict[str, str] = Field(default_factory=dict)
    # Video ids, so any list in the UI can mark a song as starred / on disk.
    favourites: list[str] = Field(default_factory=list)
    downloads: list[str] = Field(default_factory=list)
    # Saved to the user's download folder, and saves in progress this session
    # (video id -> {state, progress, path, error}).
    saved: list[str] = Field(default_factory=list)
    saving: dict[str, dict] = Field(default_factory=dict)


class AddRequest(BaseModel):
    video_id: str
    title: str | None = None
    channel: str | None = None
    duration: float | None = None
    thumbnail: str | None = None
    singer: str | None = None
    play_now: bool = False


class ReorderRequest(BaseModel):
    from_index: int = Field(alias="from")
    to_index: int = Field(alias="to")

    model_config = {"populate_by_name": True}


class SeekRequest(BaseModel):
    seconds: float
    absolute: bool = True


class SingerRequest(BaseModel):
    singer: str | None = Field(default=None, max_length=80)


class VolumeRequest(BaseModel):
    level: int = Field(ge=0, le=130)
