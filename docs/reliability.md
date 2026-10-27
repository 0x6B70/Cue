# Keeping playback working

YouTube's anti-bot machinery is the main reason a tool like this stops
working. There is no permanent bypass — it is an arms race — but the
difference between "breaks weekly" and "rarely breaks" is mostly design
decisions, and they are all in the resolver.

## A JavaScript runtime is required

This is the first thing to check, and the most common cause of "formats are
missing" or "requested format is not available".

YouTube protects stream URLs with a signature challenge and an `n` parameter
challenge, both solved by running YouTube's own JavaScript. yt-dlp needs two
things for that, and **neither is optional**:

1. **A runtime.** Deno is the only one yt-dlp enables by default:
   `winget install DenoLand.Deno`. Note the installer does not add it to the
   `PATH` of already-running shells — set `js_runtime_path` in settings if
   `deno` is not found.
2. **The challenge-solver script**, distributed separately and fetched with
   `--remote-components ejs:github` (`ejs_remote_components` in settings).

A runtime without the solver script still fails. `backend/ytdlp.py` passes both
in `js_runtime_args()`; it has to, because `--ignore-config` means a runtime
configured in the user's own yt-dlp config is ignored.

Without them yt-dlp warns that extraction has been *deprecated*, and clients
start returning images-only. `GET /api/diagnostics` reports `js_runtime`;
if it is `null`, fix that before touching anything else.

## The resolver chain

`backend/resolver.py` tries strategies in order and remembers which one
worked, starting there next time:

| Tier | What it is | When it helps |
| --- | --- | --- |
| `potoken:*` | PO token minted in Electron's own Chromium, offered to each client in `potoken_clients` | The primary path when tokens are being issued |
| `client:*` | Alternate InnerTube clients — see `player_clients` | Enforcement differs per client and shifts over time |
| `cookies` | A signed-in session | Age-restricted videos, and sometimes bot checks |
| `invidious` | Public instances | Slow and flaky, but occasionally the only thing up |

**A gvs PO token is bound to the client named in its label.** `mweb.gvs+…` is
useless to any client other than `mweb`, so `potoken_clients` lists the clients
the token is actually minted for. Labelling it for a client that cannot serve
formats at all — `web` is SABR-restricted and returns images only — makes the
whole tier permanently dead, which is easy to miss because a working tier gets
sorted first and the broken one then stops being tried.

Measured against live YouTube (July 2026), with runtime and solver present:

| Client | Result |
| --- | --- |
| `default`, `web_embedded` | full adaptive formats |
| `tv_simply` | muxed 360p alone; full 1080p with a gvs token, and plays |
| `mweb` | muxed 360p alone; with a gvs token returns URLs mpv cannot play |
| `tv` | DRM-locked by a session experiment ([#12563](https://github.com/yt-dlp/yt-dlp/issues/12563)) |
| `ios`, `web_safari` | images only |

Re-measure before changing `player_clients`; these verdicts shift.

### A tier can resolve and still be useless

`mweb` is the cautionary case. Given a gvs token it returns *better* formats
(1080p AV1 rather than 360p), so the resolve looks like a clean success and
`tier_health` says `ok` — but those URLs are SABR-style and answer an
open-ended `Range: bytes=0-` with **403**. That is precisely the request
ffmpeg makes, so mpv fails about a second after playback starts, and the
engine then marks a perfectly good video as failed.

Checking a candidate client with `--simulate` is therefore not enough. Fetch
the URL it hands back and confirm both of these before trusting it:

```
curl -o /dev/null -w '%{http_code}\n' -H 'Range: bytes=0-' "<stream url>"
curl -o /dev/null -w '%{http_code}\n' "<stream url>"
```

Anything other than 206/200 means mpv will not play it, however healthy the
resolve looked.

The resolver now does this check itself (`verify_streams`, on by default):
every resolved stream is requested with an open range and the stream's own
headers before its tier counts as working. Two things measured in October
2026 make that necessary:

- **`default` resolves via ANDROID_VR**, whose URLs answer `bytes=0-` (and a
  plain GET) with 403 forever while serving `bytes=0-1000` fine. A *bounded*
  probe would wrongly pass them.
- **Fresh URLs 403 for their first two or three seconds** — `web_embedded`
  and `mweb` both — then serve normally. A prefetched URL is minutes old and
  plays; one handed to mpv the instant it was resolved does not. The probe
  retries a 403 for about seven seconds, which tells warming from dead and
  leaves a good URL warm for mpv.

Together these were how a queue "went red after a few songs": a fallback
landed on `client:default`, it became `last_successful_tier`, and every later
song failed a second into playback — including each retry, which re-resolved
and loaded at once, inside the warm-up window. Two guards keep it from
recurring: a stream that still dies in the player demotes its tier
(`Resolver.demote`), and two non-per-video failures in a row stall the queue
and retry with backoff (`engine.STALL_AFTER`) instead of failing every item.

The offline cache downloads in 10 MB ranges for the same reason; a plain GET
is refused outright by some clients.

### Frozen streams and the watchdog

Two more ways a song stops without any error reaching the app:

- **mpv's pause is global.** Pausing in the player window (Space, a click)
  and then moving on used to load every later song paused at 0:00, while the
  app — which ignores pause changes mid-load — kept showing it as playing.
  `MpvPlayer._load_blocking` now unpauses before every load.
- **A seek can hang inside mpv.** Seeking a googlevideo stream to within a
  few seconds of its end (by hand, or a SponsorBlock outro skip) logs
  `[ffmpeg] Seek failed` and then waits on a read that never completes —
  still "playing", position frozen, past mpv's own network timeout, no
  end-file. Reloading the file at the same position recovers it at once.

`Engine._watchdog` checks every few seconds: a song that should be playing
whose position has not moved for `STUCK_SECONDS` is reloaded where it froze,
or, within `END_SLACK_SECONDS` of the end, treated as finished. After
`MAX_RECOVERIES` reloads it is handled as a playback failure, which feeds the
same stall-and-retry logic as everything else. A paused song, the pause
between songs, and a song still at 0:00 (a slow start) are left alone.

Logs are written to `logs/backend.log` in the user data directory, so the
reason a song failed survives a packaged (console-less) run.

A tier failing moves to the next. A video that is *unavailable* — deleted,
private, members-only — aborts the chain immediately, because no client,
token or cookie will bring it back.

## Why tokens are minted in Chromium

BotGuard needs a genuine browser environment to pass attestation. Most yt-dlp
setups fake one with jsdom or run a separate Node or Docker sidecar, and it
works poorly. This app already ships Chromium, so `frontend/potoken/` runs
[BgUtils](https://github.com/LuanRT/BgUtils) in a hidden `BrowserWindow`
against a real youtube.com page. Nothing is simulated, and there is no extra
runtime to bundle.

That window uses its own session partition, has its CSP stripped (loading the
BotGuard VM requires `eval`), and runs with `webSecurity: false` so the WAA
endpoints are reachable. That is safe only because it loads nothing but
YouTube — do not point it at anything else.

Failure is never fatal: the error is reported to `/api/potoken` and the
resolver moves to the next tier.

## What actually keeps this working

**Session consistency.** One `visitor_data` is minted and reused for as long
as it is valid. Rotating identity on every request looks far more automated
than one session working through a queue.

**Request pacing.** `prefetch_ahead` defaults to 1. Resolving the whole queue
at once is the fastest way to trip a bot check — it is exactly what bulk
scraping looks like. One person on a residential IP watching a queue at human
speed is the easiest case to pass; do not throw that advantage away.

**Do not run it over a VPN.** Commercial VPN exit ranges are among the most
aggressively challenged addresses. If bot checks are constant, this is the
first thing to rule out.

**yt-dlp auto-update.** `backend/updater.py` checks GitHub every 12 hours and
drops a new binary into the user data directory, which `find_binary` prefers
over the bundled copy. This is why yt-dlp is invoked as a subprocess rather
than imported — a frozen library copy could never be replaced. Without this
the app works on release day and dies quietly a month later.

## When it stops working

1. `GET /api/diagnostics` — check `js_runtime` is not null, the yt-dlp
   version, whether a PO token is held, which tier last succeeded, and
   `tier_health`. That last one is the fastest way to spot a fallback that has
   been dead for weeks: a tier stuck on a failure there is never reached while
   the preferred tier keeps working.
2. **Check the logs for the actual reason.** Warnings are deliberately not
   suppressed, and the resolver logs yt-dlp's stderr when a tier fails. The
   messages name the problem outright — "requires a GVS PO Token", "SABR-only
   experiment", "DRM protected".
3. **Update yt-dlp** if the version is more than a couple of weeks old. Most
   breakage is fixed upstream within days.
4. **Reorder `player_clients`** in settings. This is the knob that changes
   most often as YouTube adjusts enforcement; no rebuild needed.
5. **Add cookies** — `cookies_from_browser: "firefox"`. Use a throwaway
   Google account, never your main one. Note that YouTube rotates cookies and
   yt-dlp can invalidate the source browser's session.
6. **Wait.** Bot checks are often IP-reputation-based and clear on their own
   within a day or two.

## Bundled helper binaries

The installer ships mpv, yt-dlp, deno and ffmpeg in `vendor/`, because a
machine that has none of them is the normal case — the app is otherwise dead
on arrival there, with no playback and no extraction. ffmpeg is what lets a
saved video be the best video and audio streams merged into one file; without
it a save falls back to YouTube's single-file format, usually 360p.
`build/youtube-queue-backend.spec` picks the directory up automatically when
it exists; the repo copy is gitignored, so populate it before building a
release:

```
vendor/mpv.exe          vendor/mpv.com      vendor/d3dcompiler_43.dll
vendor/yt-dlp.exe       vendor/deno.exe     vendor/ffmpeg.exe
vendor/ffmpeg-LICENSE.txt
```

ffmpeg.exe comes from BtbN's builds, the `ffmpeg-n8.1-latest-win64-lgpl-8.1.zip`
asset at https://github.com/BtbN/FFmpeg-Builds/releases — check it against
that release's `checksums.sha256` before extracting. The LGPL build is
enough: saving only remuxes (`-c copy`), it never encodes.

`find_binary` prefers, in order: an explicit setting, the auto-updated copy in
the user data dir, the bundled copy, `PATH`, then the well-known install
locations below. The user-dir copy outranking the bundle is deliberate — it is
how `backend.updater` can ship a newer yt-dlp than the one built in.

Note mpv is GPL-licensed (ffmpeg, as bundled, is LGPL); bundling them makes
the installer a combined work, so redistributing it publicly carries
source-availability obligations.

## Windows: installers that never touch PATH

`winget install shinchiro.mpv` puts mpv in `Program Files` and adds nothing to
`PATH`, so `shutil.which("mpv")` misses it and the app used to report "mpv was
not found" to someone who had just installed mpv — fixable only by hand-editing
`settings.json`. `find_binary` therefore falls back to the standard install
locations (`_WINDOWS_HINTS` in `backend/config.py`) after checking `PATH`, for
mpv, deno and yt-dlp. `PATH` still wins, so a deliberately chosen copy is never
overridden by a guess.

Add to that list rather than telling people to edit settings; `mpv_path`,
`js_runtime_path` and `ytdlp_path` remain available as explicit overrides.

## Windows: yt-dlp must not be a `.cmd` shim

`CreateProcess` cannot run a batch file, so Windows wraps `.bat`/`.cmd` in an
implicit `cmd.exe /c`, which re-parses the command line. The format selector
contains `<` (`bv*[height<=?1080]`), which cmd reads as input redirection, and
the call dies with "The filename, directory name, or volume label syntax is
incorrect" before yt-dlp ever starts. Some package managers install yt-dlp
exactly this way, so `find_binary` declines a shim and prefers a sibling
`.exe`, falling back to the module.

## The escape hatch

Anything already resolved and downloaded keeps working forever. Caching queued
videos to disk turns an ongoing bypass problem into a one-time one — the only
thing that survives *any* future YouTube change. This is implemented in
`backend/cache.py` and on by default (`offline_cache`): the next few items are
downloaded ahead, under a size budget with least-recently-used eviction, and
played straight from disk with no resolve. Kept as shallow as prefetch, because
downloading the whole queue at once is exactly what bulk scraping looks like.

## Sources

- [yt-dlp PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
- [LuanRT/BgUtils](https://github.com/LuanRT/BgUtils)
- [SponsorBlock API](https://wiki.sponsor.ajay.app/w/API_Docs)
