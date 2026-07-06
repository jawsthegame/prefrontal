// Prefrontal Desktop — a minimal macOS Electron wrapper.
//
// It does three things:
//   1. Starts the Prefrontal backend (`prefrontal serve`) as a child process,
//      unless one is already running (e.g. the launchd agent from deploy/).
//   2. Waits for the server's /health probe, then loads /dashboard in a window.
//   3. Lives in the menu bar (tray) with Open / Restart / Logs / Quit, and
//      shuts the backend down cleanly on quit.
//
// Everything the dashboard needs (token prompt, polling, panic overlay) already
// lives in the served HTML — this wrapper only manages the process + window.

const { app, BrowserWindow, Tray, Menu, shell, dialog, nativeImage } = require('electron');
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// Configuration (all overridable via env so you don't have to touch the code).
// ---------------------------------------------------------------------------

// The repo root is the parent of this desktop/ folder by default, so the
// backend's WorkingDirectory is the repo and its `.env` gets loaded. Override
// with PREFRONTAL_DIR if your checkout lives elsewhere.
const REPO_DIR = process.env.PREFRONTAL_DIR || path.resolve(__dirname, '..');

// Prefer the repo's virtualenv binary (matches deploy/*.plist and the
// deployment runbook), fall back to whatever `prefrontal` is on PATH.
const VENV_BIN = path.join(REPO_DIR, '.venv', 'bin', 'prefrontal');
const PREFRONTAL_BIN =
  process.env.PREFRONTAL_BIN || (fs.existsSync(VENV_BIN) ? VENV_BIN : 'prefrontal');

const PORT = parseInt(process.env.PREFRONTAL_PORT || '8000', 10);
// The window always talks to the loopback address even though the server may
// bind 0.0.0.0 (so your phone can still reach it over Tailscale).
const BASE_URL = `http://127.0.0.1:${PORT}`;
const DASHBOARD_URL = `${BASE_URL}/dashboard`;
const ICON_PATH = path.join(__dirname, 'assets', 'icon.png');

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let mainWindow = null;
let tray = null;
let serverProc = null; // the child we spawned (null if one was already running)
const logBuffer = []; // recent stdout/stderr lines, shown via "View logs…"

function log(line) {
  const stamped = `${new Date().toISOString()} ${line}`;
  logBuffer.push(stamped);
  if (logBuffer.length > 500) logBuffer.shift();
  // eslint-disable-next-line no-console
  console.log(stamped);
}

// ---------------------------------------------------------------------------
// Backend lifecycle
// ---------------------------------------------------------------------------

// Resolves true if /health answers within `timeoutMs`.
function probeHealth(timeoutMs = 1500) {
  return new Promise((resolve) => {
    const req = http.get(`${BASE_URL}/health`, { timeout: timeoutMs }, (res) => {
      res.resume(); // drain
      resolve(res.statusCode === 200);
    });
    req.on('timeout', () => req.destroy());
    req.on('error', () => resolve(false));
  });
}

// Polls /health until it comes up or we give up.
async function waitForHealth(totalMs = 30000, everyMs = 500) {
  const deadline = Date.now() + totalMs;
  while (Date.now() < deadline) {
    if (await probeHealth()) return true;
    await new Promise((r) => setTimeout(r, everyMs));
  }
  return false;
}

function startBackend() {
  log(`Starting backend: ${PREFRONTAL_BIN} serve --port ${PORT} (cwd: ${REPO_DIR})`);
  const child = spawn(PREFRONTAL_BIN, ['serve', '--port', String(PORT)], {
    cwd: REPO_DIR,
    env: process.env,
  });

  child.stdout.on('data', (d) => log(`[serve] ${d.toString().trimEnd()}`));
  child.stderr.on('data', (d) => log(`[serve] ${d.toString().trimEnd()}`));

  child.on('error', (err) => {
    log(`Failed to spawn backend: ${err.message}`);
    dialog.showErrorBox(
      'Could not start Prefrontal',
      `Tried to run:\n  ${PREFRONTAL_BIN} serve\n\n${err.message}\n\n` +
        `Set PREFRONTAL_BIN to your prefrontal executable, or PREFRONTAL_DIR ` +
        `to your checkout, then relaunch.`
    );
  });

  child.on('exit', (code, signal) => {
    log(`Backend exited (code=${code}, signal=${signal})`);
    serverProc = null;
  });

  serverProc = child;
}

function stopBackend() {
  if (serverProc) {
    log('Stopping backend…');
    serverProc.kill('SIGTERM');
    serverProc = null;
  }
}

// Ensure a backend is reachable: reuse an existing one, otherwise spawn ours.
async function ensureBackend() {
  if (await probeHealth()) {
    log('A Prefrontal server is already running — attaching to it.');
    return true;
  }
  startBackend();
  return waitForHealth();
}

// ---------------------------------------------------------------------------
// Window + tray
// ---------------------------------------------------------------------------

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 820,
    minWidth: 480,
    minHeight: 600,
    title: 'Prefrontal',
    icon: fs.existsSync(ICON_PATH) ? ICON_PATH : undefined,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
    },
  });

  mainWindow.loadFile('loading.html');
  mainWindow.once('ready-to-show', () => mainWindow.show());

  // Open external links (docs, etc.) in the system browser, not the app window.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(BASE_URL)) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  // Closing the window hides it to the tray instead of quitting (macOS-style),
  // so the backend keeps running. Quit from the tray or Cmd+Q.
  mainWindow.on('close', (e) => {
    if (!app.isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });
}

function showDashboard() {
  if (!mainWindow) createWindow();
  mainWindow.loadURL(DASHBOARD_URL);
  mainWindow.show();
  mainWindow.focus();
}

function createTray() {
  let image = nativeImage.createFromPath(ICON_PATH);
  if (!image.isEmpty()) {
    image = image.resize({ width: 18, height: 18 });
    image.setTemplateImage(true); // render as a monochrome menu-bar glyph
  }
  tray = new Tray(image.isEmpty() ? nativeImage.createEmpty() : image);
  tray.setToolTip('Prefrontal');

  const menu = Menu.buildFromTemplate([
    { label: 'Open Dashboard', click: showDashboard },
    {
      label: 'Open in Browser',
      click: () => shell.openExternal(DASHBOARD_URL),
    },
    { type: 'separator' },
    {
      label: 'Restart Server',
      click: async () => {
        stopBackend();
        await new Promise((r) => setTimeout(r, 500));
        const ok = await ensureBackend();
        if (ok && mainWindow) mainWindow.loadURL(DASHBOARD_URL);
      },
    },
    { label: 'View Logs…', click: showLogs },
    { type: 'separator' },
    {
      label: 'Quit Prefrontal',
      click: () => {
        app.isQuitting = true;
        app.quit();
      },
    },
  ]);
  tray.setContextMenu(menu);
  tray.on('click', showDashboard);
}

function showLogs() {
  dialog.showMessageBox({
    type: 'info',
    title: 'Prefrontal — recent logs',
    message: `Backend: ${PREFRONTAL_BIN}`,
    detail: logBuffer.slice(-40).join('\n') || '(no output yet)',
    buttons: ['Close'],
  });
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

// Single-instance: focus the existing window instead of launching a second app
// (which would collide on the port).
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', showDashboard);

  app.whenReady().then(async () => {
    if (process.platform === 'darwin' && fs.existsSync(ICON_PATH)) {
      app.dock.setIcon(ICON_PATH);
    }
    createWindow();
    createTray();

    const ok = await ensureBackend();
    if (ok) {
      mainWindow.loadURL(DASHBOARD_URL);
    } else {
      log('Backend did not become healthy in time.');
      mainWindow.loadFile('loading.html', { hash: 'error' });
    }
  });

  app.on('activate', () => {
    if (mainWindow) mainWindow.show();
    else createWindow();
  });

  // Keep running in the tray when all windows close.
  app.on('window-all-closed', () => {});

  app.on('before-quit', () => {
    app.isQuitting = true;
    stopBackend();
  });
}
