/**
 * PO token generation, run inside a hidden Electron BrowserWindow.
 *
 * BotGuard attests that a request came from a real browser, and it needs a
 * genuine browser environment to pass. Because this app already ships
 * Chromium, tokens are minted here rather than through a jsdom shim or a
 * separate Node sidecar — the environment is real, so there is nothing to
 * simulate.
 *
 * Bundled by `npm run build:potoken` into bundle.js, which the main process
 * injects into the hidden window. The bundle defines window.__mintPoTokens().
 */

import { BotGuardClient, getChallenge } from 'bgutils-js/botguard';
import { WebPoMinter } from 'bgutils-js/webpo';

// YouTube's web client request key. A public constant, not a credential.
const REQUEST_KEY = 'O43z0dpjhgX20SCx4KAo';
const WAA_API_KEY = 'AIzaSyDyT5W0Jh49F30Pqqtyfdf7pDLFKLJoAnw';
const GENERATE_IT_URL = 'https://jnn-pa.googleapis.com/$rpc/google.internal.waa.v1.Waa/GenerateIT';
const VISITOR_ID_URL = 'https://www.youtube.com/youtubei/v1/visitor_id?prettyPrint=false';
const INNERTUBE_CLIENT = { clientName: 'WEB', clientVersion: '2.20240304.00.00', hl: 'en' };

const DEFAULT_TTL_SECONDS = 6 * 3600;

/** The session identity the token is bound to. */
async function fetchVisitorData() {
  const response = await fetch(VISITOR_ID_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ context: { client: INNERTUBE_CLIENT } })
  });
  if (!response.ok) {
    throw new Error(`visitor_id request failed: HTTP ${response.status}`);
  }
  const payload = await response.json();
  const visitorData = payload?.responseContext?.visitorData;
  if (!visitorData) {
    throw new Error('visitor_id response contained no visitorData');
  }
  return visitorData;
}

/** Runs the BotGuard challenge and returns its attestation response. */
async function runAttestation() {
  const challenge = await getChallenge({
    requestKey: REQUEST_KEY,
    fetchFunction: (...args) => fetch(...args)
  });

  const interpreter = challenge?.interpreterJavascript?.privateDoNotAccessOrElseSafeScriptWrappedValue;
  if (!interpreter) {
    throw new Error('challenge did not include an interpreter script');
  }

  // Loading the VM requires eval; the hidden window's CSP is stripped for
  // exactly this reason (see main.js).
  new Function(interpreter)();

  const botguard = await BotGuardClient.create({
    program: challenge.program,
    globalName: challenge.globalName,
    globalObject: window
  });

  const webPoSignalOutput = [];
  const botguardResponse = await botguard.snapshot({ webPoSignalOutput });
  return { botguardResponse, webPoSignalOutput };
}

/** Trades the attestation response for an integrity token. */
async function fetchIntegrityToken(botguardResponse) {
  const response = await fetch(GENERATE_IT_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json+protobuf',
      'x-goog-api-key': WAA_API_KEY,
      'x-user-agent': 'grpc-web-javascript/0.1'
    },
    body: JSON.stringify([REQUEST_KEY, botguardResponse])
  });
  if (!response.ok) {
    throw new Error(`GenerateIT failed: HTTP ${response.status}`);
  }
  const raw = await response.json();
  if (!Array.isArray(raw) || !raw[0]) {
    throw new Error('GenerateIT returned no integrity token');
  }
  return {
    integrityToken: raw[0],
    estimatedTtlSecs: raw[1],
    mintRefreshThreshold: raw[2],
    websafeFallbackToken: raw[3]
  };
}

/**
 * Mints a session-bound (gvs) PO token.
 *
 * Only the gvs token is produced here: it binds to visitor_data and so covers
 * the whole session. The web *player* token binds to an individual video ID
 * and would need a mint round-trip per item.
 */
export async function mintPoTokens() {
  try {
    const visitorData = await fetchVisitorData();
    const { botguardResponse, webPoSignalOutput } = await runAttestation();
    const integrityToken = await fetchIntegrityToken(botguardResponse);

    const minter = await WebPoMinter.create(integrityToken, webPoSignalOutput);
    const gvsToken = await minter.mintAsWebsafeString(visitorData);

    // Keep the minter around so a per-video player token can be minted later
    // without repeating the whole attestation.
    window.__mintForVideo = (videoId) => minter.mintAsWebsafeString(videoId);

    const ttl = Number(integrityToken.estimatedTtlSecs) || DEFAULT_TTL_SECONDS;
    return {
      ok: true,
      visitor_data: visitorData,
      gvs_token: gvsToken,
      expires_at: Date.now() / 1000 + ttl,
      source: 'electron-botguard'
    };
  } catch (error) {
    // Never throw across the injection boundary: the caller reports the
    // failure to the backend, which simply falls through to the next tier.
    return { ok: false, error: String(error?.message || error) };
  }
}

window.__mintPoTokens = mintPoTokens;
