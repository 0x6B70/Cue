"""Saving videos to a folder the user picked.

Separate from the offline cache on purpose. The cache is ours: a few upcoming
items in the user data dir, under a budget, evicted when it fills. Saved files
are the user's: named after the song, in a folder they chose, never evicted.

Every byte comes through the same path playback uses -- the resolver (with
its playability probe, so the URL is warm) and bounded-range downloads -- and
a file already in the offline cache is reused instead of fetched again. Saves
run one at a time: several parallel full downloads are exactly what bulk
scraping looks like.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .cache import OfflineCache, download_ranged
from .config import Settings
from .library import Library
from .resolver import Resolver

log = logging.getLogger(__name__)

# Refuse to start a save that would leave less than this free on the disk.
MIN_FREE_BYTES = 1024**3

# Progress pushes to the UI at most this often, in seconds.
PROGRESS_INTERVAL = 0.5

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

# Characters Windows forbids in file names, plus control characters.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class SaveError(RuntimeError):
    pass


@dataclass
class SaveJob:
    video_id: str
    title: str
    channel: str | None = None
    duration: float | None = None
    thumbnail: str | None = None
    state: str = "queued"  # queued, saving, done, failed
    progress: float = 0.0  # 0..1
    path: str | None = None
    error: str | None = None

    def public(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k in ("state", "progress", "path", "error")}


def safe_filename(title: str, video_id: str, ext: str) -> str:
    """`<title> [<id>].<ext>`, legal on every desktop filesystem.

    The id keeps two songs with the same title apart and makes the file
    findable again; the title is cut so the full path stays well inside
    Windows' 260-character limit.
    """
    name = _UNSAFE.sub("_", " ".join((title or video_id).split())).strip(" .")
    name = name[:120].rstrip(" .") or video_id
    return f"{name} [{video_id}].{ext}"


def sniff_extension(path: Path, fallback: str) -> str:
    """Container of a downloaded stream from its first bytes."""
    try:
        head = path.open("rb").read(12)
    except OSError:
        return fallback
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    return fallback


class Saver:
    def __init__(
        self,
        settings: Settings,
        resolver: Resolver,
        cache: OfflineCache,
        library: Library,
        on_change: Callable[[], None],
    ):
        self.settings = settings
        self.resolver = resolver
        self.cache = cache
        self.library = library
        self.on_change = on_change
        self.jobs: dict[str, SaveJob] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._last_push = 0.0

    # ----------------------------------------------------------------- api

    def request(self, video_id: str, **meta) -> SaveJob:
        """Queue a save. A video already saved, or already saving, is left be."""
        job = self.jobs.get(video_id)
        if job and job.state in ("queued", "saving"):
            return job
        existing = self.library.saved_path(video_id)
        if existing:
            job = SaveJob(video_id, meta.get("title") or video_id, state="done", progress=1.0, path=str(existing))
            self.jobs[video_id] = job
            return job
        job = SaveJob(
            video_id,
            meta.get("title") or video_id,
            meta.get("channel"),
            meta.get("duration"),
            meta.get("thumbnail"),
        )
        self.jobs[video_id] = job
        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self.on_change()
        return job

    def status(self) -> dict[str, dict]:
        """Jobs worth showing: anything in flight, or finished this session."""
        return {vid: job.public() for vid, job in self.jobs.items()}

    def delete(self, video_id: str) -> bool:
        path = self.library.saved_path(video_id)
        if not path:
            return False
        path.unlink(missing_ok=True)
        self.library.set_saved(video_id, None)
        self.jobs.pop(video_id, None)
        self.on_change()
        return True

    def cancel_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    def ffmpeg(self) -> str | None:
        return self.settings.ffmpeg_binary()

    # ------------------------------------------------------------ internals

    def _push(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_push >= PROGRESS_INTERVAL:
            self._last_push = now
            self.on_change()

    async def _run(self, job: SaveJob) -> None:
        async with self._lock:
            job.state = "saving"
            self._push(force=True)
            try:
                path = await self._save(job)
            except asyncio.CancelledError:
                job.state, job.error = "failed", "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001 - reported to the UI
                log.warning("saving %s failed: %s", job.video_id, exc, exc_info=not isinstance(exc, SaveError))
                job.state, job.error = "failed", str(exc) or exc.__class__.__name__
            else:
                job.state, job.progress, job.path = "done", 1.0, str(path)
                self.library.set_saved(
                    job.video_id,
                    str(path),
                    title=job.title,
                    channel=job.channel,
                    duration=job.duration,
                    thumbnail=job.thumbnail,
                )
                log.info("saved %s to %s", job.video_id, path)
            finally:
                self._push(force=True)

    async def _save(self, job: SaveJob) -> Path:
        directory = self.settings.download_directory()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SaveError(f"cannot create {directory}: {exc}") from exc
        free = shutil.disk_usage(directory).free
        if free < MIN_FREE_BYTES:
            raise SaveError(f"not enough free space in {directory} ({free / 1024**3:.1f} GB left)")

        work = directory / ".cue-partial"
        work.mkdir(exist_ok=True)
        try:
            if self.settings.download_format == "audio":
                return await self._save_audio(job, directory, work)
            if self.ffmpeg():
                return await self._save_merged(job, directory, work)
            return await self._save_muxed(job, directory, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _fetch(self, url: str, headers: dict, dest: Path, share: tuple[float, float], job: SaveJob) -> None:
        """Download one stream, mapping its progress into `share` of the job."""
        low, high = share

        def progress(done: int, total: int | None) -> None:
            if total:
                job.progress = low + (high - low) * min(1.0, done / total)
                self._push()

        await download_ranged(url, headers, dest, progress)

    async def _save_audio(self, job: SaveJob, directory: Path, work: Path) -> Path:
        cached = self.cache.paths(job.video_id)
        if cached and cached[1]:
            source = cached[1]
        else:
            stream = await self.resolver.resolve(job.video_id)
            if not stream.audio_url:
                # A muxed-only stream has no separate audio to save.
                return await self._save_muxed(job, directory, work)
            source = work / "audio"
            await self._fetch(stream.audio_url, stream.headers, source, (0.0, 1.0), job)
        ext = sniff_extension(source, "m4a")
        return self._place(source, directory, job, "m4a" if ext == "mp4" else ext, copy=source.parent != work)

    async def _save_muxed(self, job: SaveJob, directory: Path, work: Path) -> Path:
        stream = await self.resolver.resolve(job.video_id, muxed=True)
        source = work / "video"
        await self._fetch(stream.video_url, stream.headers, source, (0.0, 1.0), job)
        return self._place(source, directory, job, sniff_extension(source, "mp4"), copy=False)

    async def _save_merged(self, job: SaveJob, directory: Path, work: Path) -> Path:
        cached = self.cache.paths(job.video_id)
        if cached:
            video, audio = cached
        else:
            stream = await self.resolver.resolve(job.video_id)
            video = work / "video"
            await self._fetch(stream.video_url, stream.headers, video, (0.0, 0.8 if stream.audio_url else 0.95), job)
            audio = None
            if stream.audio_url:
                audio = work / "audio"
                await self._fetch(stream.audio_url, stream.headers, audio, (0.8, 0.95), job)
        if audio is None:
            return self._place(video, directory, job, sniff_extension(video, "mp4"), copy=video.parent != work)

        # mp4 when both streams are mp4 family (avc1/mp4a), else Matroska,
        # which holds anything: VP9/AV1 video, Opus audio.
        both_mp4 = sniff_extension(video, "") == "mp4" and sniff_extension(audio, "") == "mp4"
        for ext in (["mp4"] if both_mp4 else []) + ["mkv"]:
            out = work / f"merged.{ext}"
            if await self._mux(video, audio, out):
                return self._place(out, directory, job, ext, copy=False)
        raise SaveError("ffmpeg could not merge the video and audio")

    async def _mux(self, video: Path, audio: Path, out: Path) -> bool:
        argv = [
            self.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video), "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(out),
        ]  # fmt: skip
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, creationflags=_NO_WINDOW
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.info("ffmpeg could not write %s: %s", out.name, stderr.decode("utf-8", "replace")[-300:])
            out.unlink(missing_ok=True)
            return False
        return True

    def _place(self, source: Path, directory: Path, job: SaveJob, ext: str, copy: bool) -> Path:
        """Move (or copy, for a cache file we must keep) into the folder."""
        dest = directory / safe_filename(job.title, job.video_id, ext)
        if copy:
            shutil.copyfile(source, dest)
        else:
            shutil.move(str(source), dest)
        return dest
