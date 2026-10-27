/**
 * Electron main process.
 *
 * Owns three things: the Python backend subprocess, the search/queue window,
 * and a hidden window that mints PO tokens. The *player* window is not an
 * Electron window at all — it is mpv, spawned by the backend.
 */

const { app, BrowserWindow, dialog, ipcMain, session, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const isPackaged = app.isPackaged;
const repoRoot = path.join(__dirname, '..');

let backendProcess = null;
let handshake = null;
let mainWindow = null;
let tokenWindow = null;
let tokenTimer = null;
let shuttingDown = false;

// ---------------------------------------------------------------- backend

function backendCommand() {
  if (process.env.BACKEND_CMD) {
    const parts = process.env.BACKEND_CMD.split(' ');
    return { command: parts[0], args: parts.slice(1) };
  }
  if (isPackaged) {
    const name = process.platform === 'win32' ? 'youtube-queue-backend.exe' : 'youtube-queue-backend';
    return { command: path.join(process.resourcesPath, 'backend', name), args: [] };
  }
  // Development: the virtualenv interpreter next to the repo.
  const python = process.platform === 'win32'
    ? path.join(repoRoot, '.venv', 'Scripts', 'python.exe')
    : path.join(repoRoot, '.venv', 'bin', 'python');
  const interpreter = fs.existsSync(python) ? python : 'python3';
  return { command: interpreter, args: ['-m', 'backend.main'] };
}

/**
 * Where the backend writes its handshake. Mirrors backend.config.user_data_dir,
 * which does not match Electron's app.getPath('appData') on Linux.
 */
function handshakePath() {
  // Must match backend.config.APP_NAME exactly, or the shell cannot find the
  // handshake the backend writes under user_data_dir.
  const appName = 'Cue';
  if (process.platform === 'win32') {
    const base = process.env.APPDATA || path.join(require('node:os').homedir(), 'AppData', 'Roaming');
    return path.join(base, appName, 'backend.json');
  }
  const home = require('node:os').homedir();
  if (process.platform === 'darwin') {
    return path.join(home, 'Library', 'Application Support', appName, 'backend.json');
  }
  const base = process.env.XDG_DATA_HOME || path.join(home, '.local', 'share');
  return path.join(base, appName, 'backend.json');
}

function readHandshakeFile() {
  try {
    const payload = JSON.parse(fs.readFileSync(handshakePath(), 'utf8'));
    return payload.port && payload.token ? payload : null;
  } catch {
    return null;
  }
}

/**
 * Start the backend and resolve once it reports its handshake.
 *
 * The port is ephemeral and the token is generated per run, so both have to
 * come from the child. Two channels are watched: the child's stdout, and the
 * handshake file — a windowed PyInstaller build on Windows has no stdout, so
 * the file is the only one that always works.
 */
function startBackend() {
  return new Promise((resolve, reject) => {
    const { command, args } = backendCommand();
    console.log('[backend] spawning', command, args.join(' '));

    // Remove any handshake left by a previous run so polling cannot latch
    // onto a stale port and token.
    try {
      fs.unlinkSync(handshakePath());
    } catch { /* nothing to remove */ }

    backendProcess = spawn(command, args, {
      cwd: isPackaged ? process.resourcesPath : repoRoot,
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
      stdio: ['ignore', 'pipe', 'pipe']
    });

    let settled = false;
    let buffer = '';

    const finish = (payload) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      clearInterval(poller);
      resolve(payload);
    };

    const fail = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      clearInterval(poller);
      reject(error);
    };

    const poller = setInterval(() => {
      const payload = readHandshakeFile();
      if (payload) finish(payload);
    }, 200);

    const timeout = setTimeout(() => {
      fail(new Error('backend did not report a handshake within 30s'));
    }, 30000);

    backendProcess.stdout.on('data', (chunk) => {
      buffer += chunk.toString();
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';
      for (const line of lines) {
        if (settled || !line.trim().startsWith('{')) {
          if (line.trim()) console.log('[backend]', line);
          continue;
        }
        try {
          const payload = JSON.parse(line);
          if (payload.port && payload.token) finish(payload);
        } catch {
          console.log('[backend]', line);
        }
      }
    });

    backendProcess.stderr.on('data', (chunk) => process.stderr.write(`[backend] ${chunk}`));

    backendProcess.on('error', fail);

    backendProcess.on('exit', (code) => {
      console.log('[backend] exited with', code);
      backendProcess = null;
      if (!settled) {
        fail(new Error(`backend exited with code ${code}`));
      } else if (!shuttingDown) {
        dialog.showErrorBox('Backend stopped', `The backend exited unexpectedly (code ${code}).`);
        app.quit();
      }
    });
  });
}

function backendUrl(pathname) {
  return `http://127.0.0.1:${handshake.port}${pathname}`;
}

async function waitForHealth(attempts = 40) {
  for (let i = 0; i < attempts; i += 1) {
    try {
      const response = await fetch(backendUrl('/health'));
      if (response.ok) return true;
    } catch {
      // not up yet
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

async function postToBackend(pathname, body) {
  return fetch(backendUrl(pathname), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-App-Token': handshake.token },
    body: JSON.stringify(body)
  });
}

// ----------------------------------------------------------------- windows

// The download-folder picker in the settings panel. A web page can only read
// files it is handed, never name a directory, so the native dialog lives here.
ipcMain.handle('choose-directory', async (_event, current) => {
  const result = await dialog.showOpenDialog(mainWindow, {
    defaultPath: typeof current === 'string' ? current : undefined,
    properties: ['openDirectory', 'createDirectory']
  });
  return result.canceled ? null : result.filePaths[0];
});

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 720,
    minHeight: 520,
    title: 'Cue — Search',
    backgroundColor: '#12131a',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  mainWindow.loadURL(backendUrl('/'));
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('closed', () => {
    mainWindow = null;
    // Quit explicitly rather than relying on 'window-all-closed': the hidden
    // PO-token window stays open for the life of the app, so that event never
    // fires and Electron would sit in the background with the backend and mpv
    // still running. Closing the search window is the user quitting.
    if (!shuttingDown) app.quit();
  });

  // Anything that is not our own UI belongs in the user's browser.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
}

/**
 * Create the hidden window used for BotGuard attestation.
 *
 * It loads a real youtube.com page: attestation is likeliest to pass in the
 * origin it was designed for. Its CSP is stripped because loading the
 * BotGuard VM requires eval, and it runs in an isolated session partition so
 * none of this touches the main window.
 */
function createTokenWindow() {
  const partition = 'persist:potoken';
  const tokenSession = session.fromPartition(partition);

  tokenSession.webRequest.onHeadersReceived((details, callback) => {
    const headers = { ...details.responseHeaders };
    for (const key of Object.keys(headers)) {
      if (key.toLowerCase() === 'content-security-policy') delete headers[key];
      if (key.toLowerCase() === 'content-security-policy-report-only') delete headers[key];
    }
    callback({ responseHeaders: headers });
  });

  tokenWindow = new BrowserWindow({
    show: false,
    width: 800,
    height: 600,
    webPreferences: {
      partition,
      contextIsolation: true,
      nodeIntegration: false,
      // Lets the page reach the WAA endpoints without CORS getting in the
      // way. Safe here only because this window loads nothing but YouTube.
      webSecurity: false
    }
  });

  tokenWindow.on('closed', () => {
    tokenWindow = null;
  });
}

function bundleSource() {
  const bundlePath = path.join(__dirname, 'potoken', 'bundle.js');
  if (!fs.existsSync(bundlePath)) {
    throw new Error('potoken/bundle.js is missing — run `npm run build:potoken`');
  }
  return fs.readFileSync(bundlePath, 'utf8');
}

/**
 * Mint a PO token and hand it to the backend.
 *
 * Every failure path reports to /api/potoken so the resolver knows to fall
 * through to another tier instead of waiting on a token that is not coming.
 */
async function refreshPoToken() {
  if (!tokenWindow) createTokenWindow();

  try {
    await tokenWindow.loadURL('https://www.youtube.com/embed/aqz-KE-bpKQ');
    await tokenWindow.webContents.executeJavaScript(bundleSource());
    const result = await tokenWindow.webContents.executeJavaScript('window.__mintPoTokens()');

    if (result?.ok) {
      await postToBackend('/api/potoken', result);
      console.log('[potoken] minted, expires in', Math.round(result.expires_at - Date.now() / 1000), 's');
      scheduleTokenRefresh(result.expires_at);
      return;
    }
    throw new Error(result?.error || 'unknown failure');
  } catch (error) {
    console.warn('[potoken] failed:', error.message);
    await postToBackend('/api/potoken', { error: error.message }).catch(() => {});
    // Retry on a slow cadence; the resolver has other tiers meanwhile.
    scheduleTokenRefresh(Date.now() / 1000 + 900);
  }
}

function scheduleTokenRefresh(expiresAt) {
  if (tokenTimer) clearTimeout(tokenTimer);
  const seconds = Math.max(300, expiresAt - Date.now() / 1000 - 600);
  tokenTimer = setTimeout(() => {
    refreshPoToken().catch((error) => console.warn('[potoken] refresh threw:', error));
  }, seconds * 1000);
}

// ---------------------------------------------------------------- lifecycle

async function bootstrap() {
  try {
    handshake = await startBackend();
  } catch (error) {
    dialog.showErrorBox('Could not start backend', String(error.message || error));
    app.quit();
    return;
  }

  if (!(await waitForHealth())) {
    dialog.showErrorBox('Backend not responding', 'The backend started but never became healthy.');
    app.quit();
    return;
  }

  createMainWindow();
  // Not awaited: the UI is usable while attestation runs.
  refreshPoToken().catch((error) => console.warn('[potoken] initial mint threw:', error));
  setupAutoUpdate();
}

/**
 * Check GitHub Releases for a newer app and install it on next quit.
 *
 * Kept entirely non-fatal and packaged-only: a dev run has no release feed,
 * and a failed check (offline, rate-limited, module missing) must never get in
 * the way of using the app. electron-updater downloads in the background and,
 * with autoInstallOnAppQuit, applies the update the next time the app closes --
 * no prompt, no interruption.
 */
function setupAutoUpdate() {
  if (!isPackaged) return; // runs from source in dev; nothing to update against

  let autoUpdater;
  try {
    ({ autoUpdater } = require('electron-updater'));
  } catch (error) {
    console.warn('[updater] electron-updater unavailable:', error.message);
    return;
  }

  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.on('error', (error) => console.warn('[updater] error:', error?.message || error));
  autoUpdater.on('update-available', (info) => console.log('[updater] update available:', info?.version));
  autoUpdater.on('update-not-available', () => console.log('[updater] already up to date'));
  autoUpdater.on('update-downloaded', (info) =>
    console.log('[updater] downloaded', info?.version, '- installs on next quit')
  );

  autoUpdater.checkForUpdates().catch((error) =>
    console.warn('[updater] check failed:', error?.message || error)
  );
}

/**
 * Take down the backend and everything it started.
 *
 * On Windows kill() is TerminateProcess, which ends only the named process.
 * mpv is a child of the backend, so killing the parent alone leaves an mpv
 * window playing with nothing driving it — the "closed it but it is still
 * running" case. taskkill /T takes the whole tree.
 */
function killBackendTree() {
  const child = backendProcess;
  if (!child || child.exitCode !== null) return;
  if (process.platform === 'win32') {
    try {
      require('node:child_process').execFileSync(
        'taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' }
      );
    } catch { /* already gone */ }
  } else {
    try { child.kill('SIGKILL'); } catch { /* already gone */ }
  }
}

/**
 * Ask the backend to stop cleanly, then make sure it and mpv are gone.
 *
 * The polite request matters: it is what terminates mpv properly and writes
 * the queue to disk. The tree kill is the guarantee.
 */
async function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  if (tokenTimer) clearTimeout(tokenTimer);
  if (!backendProcess) return;

  const exited = new Promise((resolve) => {
    if (backendProcess.exitCode !== null) return resolve();
    backendProcess.once('exit', resolve);
  });

  try {
    await Promise.race([
      postToBackend('/api/shutdown', {}),
      new Promise((resolve) => setTimeout(resolve, 1500))
    ]);
    // Give the clean exit a moment to actually finish before forcing it.
    await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 2500))]);
  } catch {
    /* backend may already be unreachable; fall through to the kill */
  }
  killBackendTree();
}

// One instance only — a second would spawn a second backend and a second
// player window.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(bootstrap);

  app.on('window-all-closed', () => app.quit());

  // Cleanup is async (it asks the backend to stop before forcing it), and
  // Electron would otherwise exit while that is still in flight — leaving the
  // backend and mpv behind, which is the whole problem being fixed.
  let cleanupDone = false;
  app.on('before-quit', (event) => {
    if (cleanupDone) return;
    event.preventDefault();
    shutdown().finally(() => {
      cleanupDone = true;
      app.quit();
    });
  });
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0 && handshake) createMainWindow();
  });
}
