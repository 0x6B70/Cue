"""WebSocket fan-out.

Playback position changes many times a second, so state pushes are coalesced:
callers mark the state dirty and a background task flushes at a fixed rate.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from fastapi import WebSocket
from starlette.websockets import WebSocketState

log = logging.getLogger(__name__)

FLUSH_INTERVAL = 0.25


class ConnectionManager:
    def __init__(self, state_provider: Callable[[], object]):
        self.state_provider = state_provider
        self.connections: set[WebSocket] = set()
        self._dirty = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.add(websocket)
        await self._send(websocket, self._payload())

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.discard(websocket)

    def mark_dirty(self) -> None:
        """Request a broadcast on the next flush tick."""
        self._dirty.set()

    def _payload(self) -> dict:
        state = self.state_provider()
        return state.model_dump(mode="json") if hasattr(state, "model_dump") else dict(state)

    async def _flush_loop(self) -> None:
        while True:
            try:
                await self._dirty.wait()
                self._dirty.clear()
                await self.broadcast()
                await asyncio.sleep(FLUSH_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a broadcast failure must not kill the loop
                log.warning("broadcast failed", exc_info=True)
                await asyncio.sleep(FLUSH_INTERVAL)

    async def broadcast(self) -> None:
        if not self.connections:
            return
        payload = self._payload()
        for websocket in list(self.connections):
            await self._send(websocket, payload)

    async def _send(self, websocket: WebSocket, payload: dict) -> None:
        if websocket.client_state is not WebSocketState.CONNECTED:
            self.connections.discard(websocket)
            return
        try:
            await websocket.send_json(payload)
        except Exception:  # noqa: BLE001 - client vanished mid-send
            self.connections.discard(websocket)
