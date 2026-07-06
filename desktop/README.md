# Prefrontal Desktop

A minimal [Electron](https://www.electronjs.org/) wrapper that shows the
Prefrontal dashboard as a native-feeling desktop app with a menu-bar icon.

It works two ways:

- **Remote (the common case): laptop → Mac mini.** Point it at your always-on
  server's URL and it just shows that dashboard. It does **not** run a backend —
  Prefrontal keeps running on the mini.
- **Local: on the mini itself.** Leave the URL at `http://127.0.0.1:8000` and it
  starts `prefrontal serve` for you (or attaches to a copy already running, e.g.
  the launchd agent in [`../deploy/`](../deploy/)).

It's deliberately thin. All the UI (token prompt, polling, panic overlay) is the
self-contained page the backend serves at `/dashboard` — the dashboard makes
same-origin requests, so pointing the window at the mini makes everything talk
to the mini, with your token stored per-server.

```
desktop/
├── main.js        # window, tray, health probe, (local) process lifecycle
├── preload.js     # tiny bridge for the settings window
├── loading.html   # boot / error splash
├── settings.html  # "Set Server URL…" panel
├── assets/icon.png
└── package.json
```

## Get a pre-built `.dmg` (no dev setup)

A GitHub Actions workflow builds the app on a macOS runner and produces `.dmg`
installers for Apple Silicon (`arm64`) and Intel (`x64`).

1. On GitHub: **Actions** → **Build Prefrontal Desktop (.dmg)** → **Run
   workflow**. (Or push a tag like `desktop-v0.1.0` to also attach the DMGs to a
   Release.)
2. When it finishes, download the DMG for your Mac from the run's **Artifacts**
   (Apple Silicon = `arm64`, older Intel Macs = `x64`).
3. Open the DMG and drag **Prefrontal** to Applications.

The build is **ad-hoc signed but not notarized** (no Apple Developer cert), so
on first launch macOS Gatekeeper still blocks a browser-downloaded app.
Right-click the app → **Open** (once), or run:

```bash
xattr -dr com.apple.quarantine /Applications/Prefrontal.app
```

Then launch it and set your server URL (see below).

## Run from source

You don't need Python or the Prefrontal CLI on the laptop — just Node.js:

```bash
cd desktop
npm install
npm start
```

Then set where your server lives:

1. Click the menu-bar icon → **Set Server URL…**
2. Enter your mini's address, e.g. `http://mac-mini.tailnet.ts.net:8000`
   (Tailscale hostname) or `http://192.168.1.50:8000` (LAN IP).
3. **Save & Connect.** The dashboard loads and prompts once for your
   `X-Prefrontal-Token`.

The URL is remembered between launches. You can also pin it for a launch with an
env var: `PREFRONTAL_URL=http://mac-mini.tailnet.ts.net:8000 npm start`.

> The mini must be reachable from the laptop. Over Tailscale that's automatic;
> on a LAN make sure `prefrontal serve` binds `0.0.0.0` (it does by default) and
> the firewall allows the port.

## Run it on the mini (local)

Leave the URL at the default `http://127.0.0.1:8000`. The app will start the
backend itself, looking for `../.venv/bin/prefrontal` first, then `prefrontal`
on `PATH`. This assumes the CLI is installed in the repo:

```bash
cd ..                        # repo root
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e . && prefrontal init-db && prefrontal user add me --operator
```

For a truly always-on host that survives reboots, the launchd agent in
[`../deploy/`](../deploy/com.morningstatic.prefrontal.plist) is still the
recommended way to run the server — this app happily **attaches** to it rather
than starting a second copy.

## Menu-bar actions

Closing the window hides it to the tray (nothing is stopped). From the icon:
**Open Dashboard**, **Open in Browser**, **Set Server URL…**, **Restart Local
Server** (local mode only), **View Logs…**, **Quit**.

## Configuration

All optional. Env wins over the saved setting wins over the default.

| Env var           | Default                     | Purpose                                       |
| ----------------- | --------------------------- | --------------------------------------------- |
| `PREFRONTAL_URL`  | saved value / `127.0.0.1:8000` | Server the window connects to              |
| `PREFRONTAL_DIR`  | parent of `desktop/`        | Local backend working dir (must hold `.env`)  |
| `PREFRONTAL_BIN`  | `$DIR/.venv/bin/prefrontal` | Path to the `prefrontal` executable (local)   |

`PREFRONTAL_DIR` / `PREFRONTAL_BIN` only matter in local mode.

## Packaging a `.app` / `.dmg`

```bash
npm run dist
```

Builds with `electron-builder` into `dist/` (must run on macOS). This bundles
the Electron shell only — in local mode the packaged app still expects
`prefrontal` installed on the machine. For a laptop pointed at a remote mini,
the packaged app is fully self-sufficient.

The same build runs in CI — see
[`.github/workflows/desktop-dmg.yml`](../.github/workflows/desktop-dmg.yml) and
"Get a pre-built `.dmg`" above.
