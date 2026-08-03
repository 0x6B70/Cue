# Cue

A desktop app with two windows: search YouTube and build a queue in one, watch
it play ad-free in the other.

```
┌─────────────────────┐        ┌──────────────────────┐
│  Window 1: Electron │  HTTP  │  FastAPI (127.0.0.1) │
│  search + queue UI  │◄──────►│  yt-dlp, queue state │
└─────────────────────┘   WS   └──────────┬───────────┘
                                          │ JSON IPC
                               ┌──────────▼───────────┐
                               │  Window 2: mpv       │
                               │  actual playback     │
                               └──────────────────────┘
```

The second window is mpv itself, not a browser window. That buys hardware
decoding, adaptive-stream handling, an on-screen controller and keyboard
bindings for free — and because mpv plays the raw stream directly, there are
no ads in it to remove.

## Before you use this

Ads cannot be removed from YouTube's official embed: the IFrame player serves
the same ad inventory as youtube.com, and the API Terms prohibit "restricting
ads from playing in your API service when they would otherwise play". An
ad-free player therefore has to bypass the official player entirely, which is
what this does and what FreeTube and Invidious-class clients do.

That is against YouTube's Terms of Service. It is fine for a local personal
tool. Do not distribute it publicly, monetize it, or ship it to an app store.

## Requirements

- **Python 3.11+**
- **mpv 0.37+** — [installation](https://mpv.io/installation/). Bundled on
  Windows and macOS builds; declared as a dependency on Linux.
- **Node 20+** and **yt-dlp**, for development.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
npm install
npm start          # builds the PO token bundle, then launches Electron
```

`npm start` spawns the backend from `.venv` automatically. To run the backend
alone:

```bash
.venv/bin/python -m backend.main --no-player
```

It binds to loopback on an ephemeral port and writes the port and a
per-run auth token to `backend.json` in the user data directory.

### Tests

```bash
.venv/bin/python -m pytest
```

130 tests. Extraction is exercised against a stubbed yt-dlp binary, so the
suite needs no network. `tests/test_player_integration.py` drives a real mpv
process using lavfi test sources — it needs no display and skips when mpv is
absent.

## Building an installer

```bash
npm run dist          # current platform
npm run dist:win      # NSIS installer
npm run dist:mac      # DMG (needs an Apple Developer identity, see below)
npm run dist:linux    # AppImage + deb
```

This builds the PO token bundle, freezes the backend with PyInstaller, then
packages everything with electron-builder. To bundle mpv and yt-dlp rather
than relying on the user's PATH, drop the binaries in `vendor/` before
building — the spec picks the directory up automatically.

macOS builds need codesigning and notarization or Gatekeeper hard-blocks
them. `build/entitlements.mac.plist` already grants the JIT and
library-validation exceptions Chromium and the spawned helpers need.

## Settings

`settings.json` in the user data directory
(`%APPDATA%\Cue` on Windows,
`~/Library/Application Support/Cue` on macOS,
`~/.local/share/Cue` on Linux). Also editable at runtime via
`PUT /api/settings`.

| Key | Default | Notes |
| --- | --- | --- |
| `max_height` | `1080` | Caps the video quality requested. |
| `player_clients` | `["default","tv","web_embedded","ios"]` | Resolver fallback order. The knob most likely to need changing. |
| `cookies_from_browser` | `null` | e.g. `"firefox"`. Needed for age-restricted videos. |
| `cookies_file` | `null` | Netscape cookie file, used if no browser is set. |
| `invidious_fallback` | `true` | Last-resort public instances. |
| `sponsorblock_enabled` | `true` | Skips in-video sponsor segments. |
| `ytdlp_auto_update` | `true` | Checks for a new yt-dlp every 12 hours, and reactively when the whole resolver chain fails. |
| `ytdlp_channel` | `"stable"` | `"nightly"` tracks yt-dlp's nightly builds, which carry YouTube fixes days earlier. |
| `mpv_options` | `{}` | Merged into mpv's command line, e.g. `{"hwdec": "auto-safe"}`. |
| `prefetch_ahead` | `1` | How many upcoming items to resolve early. |
| `offline_cache` | `true` | Downloads upcoming videos to disk so they play even if YouTube later blocks extraction. Uses disk and doubles the bandwidth of cached items. |
| `cache_ahead` | `1` | How many upcoming items to cache. Kept shallow to avoid looking like bulk scraping. |
| `cache_budget_gb` | `5.0` | Cache ceiling; least-recently-used items (played first) are evicted past it. |
| `show_upcoming_overlay` | `true` | Lists the next few queued titles in the corner of the mpv window. |

## Reliability

YouTube's bot checks are the main thing that will break this. See
[docs/reliability.md](docs/reliability.md) for how the resolver works around
them and what to change when it stops working.

`GET /api/diagnostics` reports the yt-dlp version, the resolved binary paths,
whether a PO token is held, and which resolver tier last succeeded.

## Layout

```
backend/       FastAPI app, resolver, queue, mpv control
frontend/
  main.js      Electron main: backend lifecycle, windows, PO tokens
  ui/          search and queue interface
  potoken/     BotGuard attestation, bundled by esbuild
build/         PyInstaller spec and electron-builder config
tests/
```

## Requirements

**Installed from the setup .exe:** nothing. mpv, yt-dlp and deno are bundled,
so it plays on a machine that has none of them.

**Running from source** you need those three yourself. mpv is the player;
yt-dlp does extraction; deno is the JavaScript runtime yt-dlp needs to solve
YouTube's signature and `n` challenges — without it, extraction is deprecated
and some clients return images only:

```
winget install shinchiro.mpv DenoLand.Deno
```

Note the mpv installer does not add itself to `PATH`; the app looks in the
standard install locations anyway. `GET /api/diagnostics` reports which binary
it resolved for each. See [docs/reliability.md](docs/reliability.md).

To build an installer, populate `vendor/` first (see the same doc) — it is
gitignored, and without it the build silently produces an app that depends on
whatever the target machine happens to have.

## Known gaps

- Only the session-bound (`gvs`) PO token is minted, and it is offered to the
  clients in `potoken_clients` that accept one. The web *player* token binds to
  an individual video ID and would need a mint round-trip per item;
  `window.__mintForVideo` is in place for that but is not wired up.
- `player_clients` is a snapshot of what worked when it was last measured.
  YouTube shifts DRM and SABR-only experiments per client and per session, so
  expect to re-measure — `tier_health` in `/api/diagnostics` shows which
  fallbacks are actually alive.
- The UI ships English and Greek. Backend messages travel as codes
  (`notice_code`, `error_code`) so they translate too; adding a locale means
  adding a table in `frontend/ui/i18n.js`.
- No playlist or channel import; search results only.

## Releasing

The app updates itself from GitHub Releases (electron-updater, configured under
`publish:` in `build/electron-builder.yml`). A build only reaches existing
installs if it is published as a release *and* carries a higher version, so:

1. **Bump `version` in `package.json`.** electron-updater compares versions; a
   rebuild at the same version is invisible to clients, however different the
   binary. This is the step most easily forgotten.
2. Populate `vendor/` (see [docs/reliability.md](docs/reliability.md)).
3. Publish:

   ```
   set GH_TOKEN=<a token with repo access>
   npm run dist:win -- --publish always
   ```

   `GH_TOKEN` is only needed to *upload*; the shipped app reads public release
   assets and carries no credential. yt-dlp still auto-updates on its own, far
   more often than the app itself — see Reliability.

Two caveats for a public release: the installer bundles GPL-licensed mpv, so
distributing it carries source-availability obligations; and unsigned builds
trip Windows SmartScreen (a warning) and are hard-blocked by macOS Gatekeeper.
