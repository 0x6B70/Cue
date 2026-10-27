"""FastAPI application: HTTP + WebSocket surface over the engine.

Binds to loopback only, on an ephemeral port. Because any local process (and
any web page the user has open) could otherwise POST to a known localhost
port, every /api route requires a token generated at startup and handed to the
Electron shell through a handshake file.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import socket
import sys
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, bundle_dir, user_data_dir
from .engine import Engine
from .overlay import LABEL_MODES, STYLES as OVERLAY_STYLES
from .player import OVERLAY_ALIGNMENTS, PlayerUnavailable, logo_path
from .models import AddRequest, AppState, ReorderRequest, SearchResult, SeekRequest, SingerRequest, VolumeRequest
from .potoken import PoTokenBundle
from .updater import maybe_update_ytdlp
from .ws import ConnectionManager
from .ytdlp import YtDlpError

log = logging.getLogger(__name__)

APP_TOKEN = secrets.token_urlsafe(32)
UI_DIR = bundle_dir() / "frontend" / "ui"
HANDSHAKE_PATH = user_data_dir() / "backend.json"

settings = Settings.load()
engine = Engine(settings)
manager = ConnectionManager(engine.state)


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine.on_change = manager.mark_dirty
    await manager.start()
    await engine.start(open_player=app.state.open_player)
    if settings.ytdlp_auto_update:
        asyncio.create_task(maybe_update_ytdlp(settings))
    try:
        yield
    finally:
        await manager.stop()
        await engine.shutdown()
        with contextlib.suppress(OSError):
            HANDSHAKE_PATH.unlink()


app = FastAPI(title="Cue", lifespan=lifespan)
app.state.open_player = True


@app.exception_handler(PlayerUnavailable)
async def player_unavailable(request: Request, exc: PlayerUnavailable) -> JSONResponse:
    """Report a missing or closed player as such.

    Every transport control reaches straight into mpv, so with no player these
    raise from inside the route and FastAPI turns them into a bare HTTP 500 --
    the UI then shows "HTTP 500" instead of the one thing worth saying, which
    is that mpv could not be found.
    """
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc), "code": "notice.player_unavailable"},
    )


def require_token(x_app_token: str | None = Header(default=None)) -> None:
    if not x_app_token or not secrets.compare_digest(x_app_token, APP_TOKEN):
        raise HTTPException(status_code=401, detail="invalid or missing app token")


guard = [Depends(require_token)]


# --------------------------------------------------------------------- pages


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "pid": os.getpid()}


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    path = UI_DIR / "index.html"
    if not path.exists():
        return HTMLResponse("<h1>UI not found</h1>", status_code=500)
    html = path.read_text(encoding="utf-8")
    # Same-origin injection so the page never has to be told the token twice.
    html = html.replace("</head>", f'<script>window.__APP_TOKEN__="{APP_TOKEN}";</script></head>', 1)
    return HTMLResponse(html)


@app.get("/potoken", response_class=HTMLResponse)
async def potoken_page() -> HTMLResponse:
    """Served to Electron's hidden window; see frontend/potoken/."""
    path = bundle_dir() / "frontend" / "potoken" / "index.html"
    if not path.exists():
        return HTMLResponse("<h1>PO token page not found</h1>", status_code=404)
    html = path.read_text(encoding="utf-8")
    html = html.replace("</head>", f'<script>window.__APP_TOKEN__="{APP_TOKEN}";</script></head>', 1)
    return HTMLResponse(html)


# ---------------------------------------------------------------------- api


@app.get("/api/state", response_model=AppState, dependencies=guard)
async def get_state() -> AppState:
    return engine.state()


@app.get("/api/search", response_model=list[SearchResult], dependencies=guard)
async def search(q: str = Query(min_length=1), limit: int = Query(default=20, ge=1, le=50)):
    try:
        return await engine.search(q, limit)
    except YtDlpError as exc:
        raise HTTPException(status_code=502, detail=exc.user_message) from exc


@app.get("/api/suggest", dependencies=guard)
async def suggest(q: str = Query(default="", max_length=200)) -> list[dict]:
    if not q.strip():
        return []
    return await engine.suggest(q)


# ------------------------------------------------------------------ library


@app.get("/api/library/search", dependencies=guard)
async def library_search(q: str = Query(min_length=1, max_length=200)) -> list[dict]:
    return await engine.library_search(q)


@app.get("/api/library/local", dependencies=guard)
async def library_local(q: str = "", refresh: bool = False) -> dict:
    """Media files in the library folders (the download folder included)."""
    items = await engine.local_files(q, force=refresh)
    return {"folders": [str(f) for f in engine.local.folders()], "items": items}


@app.get("/api/library/history", dependencies=guard)
async def library_history(q: str = "", limit: int = Query(default=200, ge=1, le=2000)) -> list[dict]:
    favourites = engine.favourite_ids()
    downloads = set(engine.cache.ids())
    entries = engine.library.history(limit, q)
    for entry in entries:
        entry["favourite"] = entry["id"] in favourites
        entry["downloaded"] = entry["id"] in downloads
    return entries


@app.get("/api/library/track/{video_id}", dependencies=guard)
async def library_track(video_id: str) -> dict:
    """One song's play statistics: totals, who sang it, its recent plays."""
    stats = engine.library.track_stats(video_id)
    if stats is None:
        raise HTTPException(status_code=404, detail="never played")
    return stats


@app.get("/api/library/favourites", dependencies=guard)
async def library_favourites(q: str = "") -> list[dict]:
    downloads = set(engine.cache.ids())
    entries = engine.library.favourites(q)
    for entry in entries:
        entry["downloaded"] = entry["id"] in downloads
    return entries


@app.put("/api/library/favourites/{video_id}", dependencies=guard)
async def add_favourite(video_id: str, meta: dict | None = None) -> dict:
    engine.set_favourite(video_id, True, meta)
    return {"ok": True}


@app.delete("/api/library/favourites/{video_id}", dependencies=guard)
async def remove_favourite(video_id: str) -> dict:
    engine.set_favourite(video_id, False)
    return {"ok": True}


@app.get("/api/library/downloads", dependencies=guard)
async def library_downloads(q: str = "") -> list[dict]:
    return engine.downloads(q)


@app.delete("/api/library/downloads/{video_id}", dependencies=guard)
async def remove_download(video_id: str) -> dict:
    if not engine.remove_download(video_id):
        raise HTTPException(status_code=409, detail="not downloaded, or playing right now")
    return {"ok": True}


@app.post("/api/library/save/{video_id}", dependencies=guard)
async def save_video(video_id: str, meta: dict | None = None) -> dict:
    return engine.save(video_id, meta)


@app.get("/api/library/saved", dependencies=guard)
async def library_saved(q: str = "") -> dict:
    favourites = engine.favourite_ids()
    entries = engine.library.saved(q)
    for entry in entries:
        entry["favourite"] = entry["id"] in favourites
    return {
        "directory": str(settings.download_directory()),
        "ffmpeg": bool(settings.ffmpeg_binary()),
        "items": entries,
    }


@app.delete("/api/library/saved/{video_id}", dependencies=guard)
async def remove_saved(video_id: str) -> dict:
    if not engine.remove_saved(video_id):
        raise HTTPException(status_code=409, detail="not saved, or playing right now")
    return {"ok": True}


@app.post("/api/library/open-folder", dependencies=guard)
async def open_download_folder() -> dict:
    """Show the download folder in the system file manager."""
    directory = settings.download_directory()
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(directory)  # noqa: S606 - a folder we own, opened locally
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        await asyncio.create_subprocess_exec(opener, str(directory))
    return {"ok": True}


@app.get("/api/library/stats", dependencies=guard)
async def library_stats() -> dict:
    return engine.library.stats()


@app.post("/api/queue", dependencies=guard)
async def add_to_queue(request: AddRequest) -> dict:
    try:
        item = await engine.add(request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"item": item.model_dump(mode="json")}


@app.delete("/api/queue/{index}", dependencies=guard)
async def remove_from_queue(index: int) -> dict:
    if not await engine.remove(index):
        raise HTTPException(status_code=404, detail="no such queue index")
    return {"ok": True}


@app.put("/api/queue/{index}/singer", dependencies=guard)
async def set_singer(index: int, request: SingerRequest) -> dict:
    if not await engine.set_singer(index, request.singer):
        raise HTTPException(status_code=404, detail="no such queue index")
    return {"ok": True}


@app.post("/api/queue/reorder", dependencies=guard)
async def reorder_queue(request: ReorderRequest) -> dict:
    if not await engine.reorder(request.from_index, request.to_index):
        raise HTTPException(status_code=400, detail="index out of range")
    return {"ok": True}


@app.post("/api/queue/clear", dependencies=guard)
async def clear_queue() -> dict:
    await engine.clear()
    return {"ok": True}


@app.post("/api/queue/clear-played", dependencies=guard)
async def clear_played() -> dict:
    return {"removed": await engine.clear_played()}


@app.post("/api/queue/play/{index}", dependencies=guard)
async def play_index(index: int) -> dict:
    if not await engine.play_index(index):
        raise HTTPException(status_code=404, detail="no such queue index")
    return {"ok": True}


@app.post("/api/player/seek", dependencies=guard)
async def player_seek(request: SeekRequest) -> dict:
    await engine.seek(request.seconds, request.absolute)
    return {"ok": True}


@app.post("/api/player/volume", dependencies=guard)
async def player_volume(request: VolumeRequest) -> dict:
    await engine.set_volume(request.level)
    return {"ok": True}


# Declared after the specific routes above: a path parameter would otherwise
# match "seek" and "volume" first and reject them as unknown actions.
@app.post("/api/player/{action}", dependencies=guard)
async def player_action(action: str) -> dict:
    actions = {
        "play": engine.resume,
        "pause": engine.pause,
        "toggle": engine.toggle_pause,
        "next": engine.next,
        "previous": engine.previous,
        "stop": engine.stop,
        "open": engine.open_player_window,
    }
    handler = actions.get(action)
    if not handler:
        raise HTTPException(status_code=404, detail=f"unknown action {action!r}")
    await handler()
    return {"ok": True}


@app.get("/api/settings", dependencies=guard)
async def get_settings() -> dict:
    from dataclasses import asdict

    return asdict(settings)


# Fields whose value is constrained beyond "any JSON": the settings panel only
# ever offers valid choices, but the endpoint is public within the app, so a
# bad value should be a clear 400 rather than silently poisoning settings.json.
_SETTING_CHOICES: dict[str, set[str]] = {
    "ytdlp_channel": {"stable", "nightly"},
    "overlay_position": set(OVERLAY_ALIGNMENTS),
    "overlay_style": set(OVERLAY_STYLES),
    "overlay_label": set(LABEL_MODES),
    "download_format": {"video", "audio"},
}

# Numeric settings the panel bounds with a spinner. The endpoint is public
# within the app, so an out-of-range value should be a clear 400 rather than a
# silently broken (or screen-filling) overlay.
_SETTING_RANGES: dict[str, tuple[float, float]] = {
    "overlay_font_size": (10, 96),
    "overlay_boxes": (1, 6),
}

# Colour settings are drawn straight into ASS markup, so only #rrggbb is let in.
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


@app.put("/api/settings", dependencies=guard)
async def update_settings(payload: dict) -> dict:
    from dataclasses import asdict, fields

    known = {f.name for f in fields(settings)}
    for key, value in payload.items():
        if key not in known:
            continue
        allowed = _SETTING_CHOICES.get(key)
        if allowed is not None and value not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"{key} must be one of {sorted(allowed)}, got {value!r}",
            )
        bounds = _SETTING_RANGES.get(key)
        if bounds is not None:
            lo, hi = bounds
            # bool is a subclass of int, so exclude it explicitly -- True would
            # otherwise sail through the range check as 1.
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
                raise HTTPException(
                    status_code=400,
                    detail=f"{key} must be a number between {lo} and {hi}, got {value!r}",
                )
        if key == "library_folders" and not (
            isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)
        ):
            raise HTTPException(status_code=400, detail="library_folders must be a list of folder paths")
        if key.endswith("_color") and not (isinstance(value, str) and _COLOR.match(value)):
            raise HTTPException(status_code=400, detail=f"{key} must be a #rrggbb colour, got {value!r}")
        setattr(settings, key, value)
    settings.save()
    # Folders (or the download folder) may have changed; list them afresh.
    engine.local.invalidate()
    # A settings edit can change how the overlay looks without touching the
    # queue, so make sure the next notify() actually repaints it.
    engine.invalidate_overlay()
    engine.notify()
    return asdict(settings)


# Largest logo accepted, per side. The UI scales down before uploading; this
# only stops a stray request from writing something absurd to disk.
LOGO_MAX_SIDE = 1024


@app.post("/api/overlay/logo", dependencies=guard)
async def upload_logo(request: Request, w: int = Query(ge=1, le=LOGO_MAX_SIDE), h: int = Query(ge=1, le=LOGO_MAX_SIDE)) -> dict:
    """Store the overlay logo as raw premultiplied BGRA, w*h*4 bytes.

    The browser decodes the image (any format it can show) and sends pixels,
    which is exactly what mpv's overlay-add reads -- no imaging library here.
    """
    data = await request.body()
    if len(data) != w * h * 4:
        raise HTTPException(status_code=400, detail=f"expected {w * h * 4} bytes of BGRA, got {len(data)}")
    path = logo_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    settings.overlay_logo_size = [w, h]
    settings.save()
    if engine.player:
        engine.player.reload_logo()
    engine.invalidate_overlay()
    engine.notify()
    return {"ok": True, "size": [w, h]}


@app.delete("/api/overlay/logo", dependencies=guard)
async def remove_logo() -> dict:
    logo_path().unlink(missing_ok=True)
    settings.overlay_logo_size = None
    settings.save()
    if engine.player:
        engine.player.reload_logo()
    engine.invalidate_overlay()
    engine.notify()
    return {"ok": True}


@app.post("/api/potoken", dependencies=guard)
async def set_potoken(payload: dict) -> dict:
    """Receives tokens minted by the hidden Electron window."""
    if payload.get("error"):
        engine.tokens.set_error(str(payload["error"]))
        return {"ok": False}
    bundle = PoTokenBundle(
        visitor_data=payload.get("visitor_data", ""),
        gvs_token=payload.get("gvs_token"),
        player_token=payload.get("player_token"),
        source=payload.get("source", "electron"),
    )
    if payload.get("expires_at"):
        bundle.expires_at = float(payload["expires_at"])
    engine.tokens.set(bundle)
    return {"ok": bundle.valid}


@app.get("/api/potoken/status", dependencies=guard)
async def potoken_status() -> dict:
    return engine.tokens.status


@app.post("/api/shutdown", dependencies=guard)
async def shutdown_backend() -> dict:
    """Stop cleanly, at the shell's request.

    Windows has no SIGTERM: Node's kill() becomes TerminateProcess, which ends
    this process without running the lifespan shutdown and without touching its
    children -- so mpv is left orphaned, still holding a window, and the queue
    is never written. Asking uvicorn to exit runs the normal teardown instead.
    """
    server = getattr(app.state, "server", None)
    if server is None:  # running under TestClient, or embedded
        return {"ok": False, "detail": "no server to stop"}
    log.info("shutdown requested by the shell")
    server.should_exit = True
    return {"ok": True}


@app.get("/api/diagnostics", dependencies=guard)
async def diagnostics() -> dict:
    return {
        "ytdlp_version": await engine.ytdlp.version(),
        "ytdlp_path": settings.ytdlp_binary(),
        "mpv_path": settings.mpv_binary(),
        # Absent means signature/n-challenge solving is off: extraction is
        # deprecated in that state and formats silently go missing.
        "js_runtime": settings.js_runtime_binary(),
        # Absent means saved videos fall back to YouTube's single-file format.
        "ffmpeg": settings.ffmpeg_binary(),
        "download_dir": str(settings.download_directory()),
        # True once a yt-dlp update made us drop the challenge-solver flags to
        # keep running. Extraction is degraded until yt-dlp is updated again.
        "ytdlp_js_runtime_disabled": engine.ytdlp.js_runtime_disabled,
        "player_running": bool(engine.player and engine.player.running),
        "potoken": engine.tokens.status,
        "last_successful_tier": settings.last_successful_tier,
        # Last outcome per tier. A tier stuck on a failure here is a dead
        # fallback, which is invisible while the preferred tier keeps working.
        "tier_health": engine.resolver.tier_health,
    }


# ---------------------------------------------------------------- websocket


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(default="")):
    if not secrets.compare_digest(token, APP_TOKEN):
        await websocket.close(code=1008)
        return
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # client sends nothing; this detects close
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:  # noqa: BLE001
        manager.disconnect(websocket)


if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")


# --------------------------------------------------------------- entry point


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_handshake(port: int) -> None:
    """Tell the Electron shell where we are and how to authenticate."""
    payload = {"port": port, "token": APP_TOKEN, "pid": os.getpid()}
    tmp = HANDSHAKE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(tmp, 0o600)
    tmp.replace(HANDSHAKE_PATH)
    # Also emit it on stdout when there is one. A windowed PyInstaller build
    # on Windows has no stdout, so the file above is the authoritative
    # channel and this is only a convenience for console runs.
    try:
        print(json.dumps(payload), flush=True)
    except (OSError, AttributeError, ValueError):
        log.debug("no usable stdout for the handshake")


def run() -> None:
    parser = argparse.ArgumentParser(description="YouTube queue player backend")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--no-player", action="store_true", help="do not open the mpv window at startup")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    log_format = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    logging.basicConfig(level=args.log_level.upper(), format=log_format)
    # The packaged build is windowed, so stderr goes nowhere and a failure in
    # the field used to leave no trace at all. Keep a small rotating file.
    try:
        log_dir = user_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "backend.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(log_format))
        logging.getLogger().addHandler(file_handler)
    except OSError:
        log.warning("could not open the log file", exc_info=True)

    port = args.port or free_port()
    app.state.open_player = not args.no_player
    write_handshake(port)
    log.info("backend listening on http://127.0.0.1:%d", port)

    # Built explicitly rather than via uvicorn.run() so /api/shutdown can ask
    # this server to stop, which is the only way to exit cleanly on Windows.
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level=args.log_level, access_log=False)
    )
    app.state.server = server
    server.run()


if __name__ == "__main__":
    run()
