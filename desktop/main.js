// Prefrontal Desktop — a minimal Electron wrapper.
//
// Two ways to use it:
//
//   • Remote (laptop → mini): point it at your always-on server's URL, e.g.
//     http://mac-mini.tailnet.ts.net:8000. It just shows that dashboard; it
//     never starts a backend. This is the common case for a laptop.
//
//   • Local (on the mini itself): leave the URL at http://127.0.0.1:8000 and it
//     will start `prefrontal serve` for you (or attach to a copy that's already
//     running, e.g. the launchd agent in deploy/).
//
// The server URL is remembered between launches and editable from the menu-bar
// icon ("Set Server URL…"). All the actual UI is the self-contained page the
// backend serves at /dashboard — this wrapper only manages the window and, when
// local, the server process.

const { app, BrowserWindow, Tray, Menu, shell, dialog, nativeImage, ipcMain } =
  require('electron');
const { spawn } = require('child_process');
const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// Config (persisted to userData/config.json, overridable via env)
// ---------------------------------------------------------------------------

const DEFAULT_URL = 'http://127.0.0.1:8000';
const CONFIG_PATH = () => path.join(app.getPath('userData'), 'config.json');

function loadConfig() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_PATH(), 'utf8'));
  } catch {
    return {};
  }
}

function saveConfig(patch) {
  const next = { ...loadConfig(), ...patch };
  fs.mkdirSync(path.dirname(CONFIG_PATH()), { recursive: true });
  fs.writeFileSync(CONFIG_PATH(), JSON.stringify(next, null, 2));
  return next;
}

// Env wins over the saved value wins over the default, so you can pin a URL for
// a one-off launch without changing the stored setting.
function serverUrl() {
  const raw = (process.env.PREFRONTAL_URL || loadConfig().serverUrl || DEFAULT_URL).trim();
  return raw.replace(/\/+$/, ''); // strip trailing slash
}

const DASHBOARD_URL = () => `${serverUrl()}/dashboard`;

// We only manage a backend process when the target is this machine's loopback.
function isLocal() {
  try {
    const h = new URL(serverUrl()).hostname;
    return h === '127.0.0.1' || h === 'localhost' || h === '::1';
  } catch {
    return false;
  }
}

// Where the local backend (loopback mode only) lives.
const REPO_DIR = process.env.PREFRONTAL_DIR || path.resolve(__dirname, '..');
const VENV_BIN = path.join(REPO_DIR, '.venv', 'bin', 'prefrontal');
const PREFRONTAL_BIN =
  process.env.PREFRONTAL_BIN || (fs.existsSync(VENV_BIN) ? VENV_BIN : 'prefrontal');

const ICON_PATH = path.join(__dirname, 'assets', 'icon.png');

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let mainWindow = null;
let settingsWindow = null;
let tray = null;
let serverProc = null; // the child we spawned (null if remote or already running)
const logBuffer = [];

function log(line) {
  const stamped = `${new Date().toISOString()} ${line}`;
  logBuffer.push(stamped);
  if (logBuffer.length > 500) logBuffer.shift();
  // eslint-disable-next-line no-console
  console.log(stamped);
}

// ---------------------------------------------------------------------------
// Health probe (works for http and https, remote or local)
// ---------------------------------------------------------------------------

function probeHealth(timeoutMs = 2500) {
  return new Promise((resolve) => {
    let url;
    try {
      url = new URL(`${serverUrl()}/health`);
    } catch {
      return resolve(false);
    }
    const lib = url.protocol === 'https:' ? https : http;
    const req = lib.get(url, { timeout: timeoutMs }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('timeout', () => req.destroy());
    req.on('error', () => resolve(false));
  });
}

async function waitForHealth(totalMs, everyMs = 500) {
  const deadline = Date.now() + totalMs;
  for (;;) {
    if (await probeHealth()) return true;
    if (Date.now() >= deadline) return false;
    await new Promise((r) => setTimeout(r, everyMs));
  }
}

// ---------------------------------------------------------------------------
// Local backend lifecycle (loopback mode only)
// ---------------------------------------------------------------------------

function startBackend() {
  const port = new URL(serverUrl()).port || '8000';
  log(`Starting backend: ${PREFRONTAL_BIN} serve --port ${port} (cwd: ${REPO_DIR})`);
  const child = spawn(PREFRONTAL_BIN, ['serve', '--port', String(port)], {
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
        `If Prefrontal runs on another machine, use "Set Server URL…" in the ` +
        `menu-bar icon to point at it instead.`
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

// Make sure the target is reachable. Remote: just wait. Local: attach if a
// server is already up, otherwise spawn one and wait.
async function ensureBackend() {
  if (await probeHealth()) {
    log(`Server reachable at ${serverUrl()} — attaching.`);
    return true;
  }
  if (!isLocal()) {
    log(`Remote server ${serverUrl()} not reachable yet; will keep polling.`);
    return waitForHealth(10000);
  }
  startBackend();
  return waitForHealth(30000);
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

  const origin = serverUrl();
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(origin)) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  mainWindow.on('close', (e) => {
    if (!app.isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });
}

async function connectAndShow() {
  if (!mainWindow) createWindow();
  mainWindow.loadFile('loading.html');
  const ok = await ensureBackend();
  if (ok) {
    mainWindow.loadURL(DASHBOARD_URL());
  } else {
    mainWindow.loadFile('loading.html', { hash: 'error' });
  }
  mainWindow.show();
  mainWindow.focus();
}

function createSettingsWindow() {
  if (settingsWindow) {
    settingsWindow.focus();
    return;
  }
  settingsWindow = new BrowserWindow({
    width: 460,
    height: 300,
    resizable: false,
    title: 'Prefrontal — Server',
    icon: fs.existsSync(ICON_PATH) ? ICON_PATH : undefined,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
    },
  });
  settingsWindow.loadFile('settings.html');
  settingsWindow.on('closed', () => (settingsWindow = null));
}

function createTray() {
  let image = nativeImage.createFromPath(ICON_PATH);
  if (!image.isEmpty()) {
    image = image.resize({ width: 18, height: 18 });
    image.setTemplateImage(true);
  }
  tray = new Tray(image.isEmpty() ? nativeImage.createEmpty() : image);
  tray.setToolTip('Prefrontal');

  const menu = Menu.buildFromTemplate([
    { label: 'Open Dashboard', click: () => connectAndShow() },
    { label: 'Open in Browser', click: () => shell.openExternal(DASHBOARD_URL()) },
    { type: 'separator' },
    { label: 'Set Server URL…', click: createSettingsWindow },
    {
      label: 'Restart Local Server',
      // Only meaningful when we manage the process ourselves.
      enabled: isLocal(),
      click: async () => {
        stopBackend();
        await new Promise((r) => setTimeout(r, 500));
        await connectAndShow();
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
  tray.on('click', () => mainWindow && mainWindow.show());
}

function showLogs() {
  dialog.showMessageBox({
    type: 'info',
    title: 'Prefrontal — status',
    message: `Server: ${serverUrl()}${isLocal() ? ' (local)' : ' (remote)'}`,
    detail: logBuffer.slice(-40).join('\n') || '(no output yet)',
    buttons: ['Close'],
  });
}

// ---------------------------------------------------------------------------
// IPC — the settings window reads/writes the server URL through preload
// ---------------------------------------------------------------------------

ipcMain.handle('get-server-url', () => serverUrl());
ipcMain.handle('set-server-url', async (_evt, url) => {
  const clean = String(url || '').trim();
  if (!/^https?:\/\//i.test(clean)) {
    return { ok: false, error: 'URL must start with http:// or https://' };
  }
  saveConfig({ serverUrl: clean.replace(/\/+$/, '') });
  log(`Server URL set to ${serverUrl()}`);
  if (settingsWindow) settingsWindow.close();
  // Rebuild the tray so "Restart Local Server" enable-state tracks the new URL.
  if (tray) createTray();
  await connectAndShow();
  return { ok: true };
});

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => mainWindow && mainWindow.show());

  app.whenReady().then(async () => {
    if (process.platform === 'darwin' && fs.existsSync(ICON_PATH)) {
      app.dock.setIcon(ICON_PATH);
    }
    createWindow();
    createTray();
    await connectAndShow();
  });

  app.on('activate', () => {
    if (mainWindow) mainWindow.show();
    else createWindow();
  });

  app.on('window-all-closed', () => {}); // stay alive in the tray

  app.on('before-quit', () => {
    app.isQuitting = true;
    stopBackend();
  });
}
