"""Storage for Proof-of-Origin tokens minted by the Electron renderer.

BotGuard requires a genuine browser environment to pass attestation. Because
this app already ships Chromium, the tokens are generated in a hidden
BrowserWindow (see frontend/potoken/) and POSTed here, rather than through a
jsdom shim or a separate Node sidecar.

One session identity is reused for as long as it stays valid: rotating
visitor_data on every request looks far more automated than a single session
working through a queue.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Refresh a little before the real expiry so a resolve never races it.
REFRESH_MARGIN_SECONDS = 300


@dataclass
class PoTokenBundle:
    visitor_data: str
    gvs_token: str | None = None
    player_token: str | None = None
    expires_at: float = field(default_factory=lambda: time.time() + 6 * 3600)
    source: str = "electron"

    @property
    def valid(self) -> bool:
        return bool(self.visitor_data and (self.gvs_token or self.player_token))

    @property
    def fresh(self) -> bool:
        return self.valid and time.time() < self.expires_at - REFRESH_MARGIN_SECONDS

    def extractor_args(self, client: str = "web") -> list[str]:
        """Render as a yt-dlp `--extractor-args` pair.

        Format: youtube:player_client=web;visitor_data=X;po_token=web.gvs+A,web.player+B
        """
        parts = [f"player_client={client}", f"visitor_data={self.visitor_data}"]
        tokens = []
        if self.gvs_token:
            tokens.append(f"{client}.gvs+{self.gvs_token}")
        if self.player_token:
            tokens.append(f"{client}.player+{self.player_token}")
        if tokens:
            parts.append("po_token=" + ",".join(tokens))
        return ["--extractor-args", "youtube:" + ";".join(parts)]


class PoTokenStore:
    def __init__(self) -> None:
        self._bundle: PoTokenBundle | None = None
        self._last_error: str | None = None

    def set(self, bundle: PoTokenBundle) -> None:
        if not bundle.valid:
            log.warning("ignoring incomplete PO token bundle")
            return
        self._bundle = bundle
        self._last_error = None
        log.info("PO token stored (source=%s, expires in %ds)", bundle.source, int(bundle.expires_at - time.time()))

    def set_error(self, message: str) -> None:
        self._last_error = message
        log.warning("PO token generation failed: %s", message)

    def get(self) -> PoTokenBundle | None:
        if self._bundle and self._bundle.fresh:
            return self._bundle
        return None

    @property
    def status(self) -> dict:
        bundle = self._bundle
        return {
            "have_token": bool(bundle and bundle.fresh),
            "expires_in": int(bundle.expires_at - time.time()) if bundle else None,
            "source": bundle.source if bundle else None,
            "last_error": self._last_error,
        }
