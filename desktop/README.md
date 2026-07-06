# Prefrontal Desktop (macOS)

A minimal [Electron](https://www.electronjs.org/) wrapper that runs Prefrontal
as a native-feeling Mac app: it starts the `prefrontal serve` backend, waits for
it to come up, and shows the dashboard in a window with a menu-bar icon.

It's deliberately thin. All the UI (token prompt, polling, panic overlay) is the
self-contained page the backend already serves at `/dashboard` — this wrapper
only manages the server process and the window.

```
desktop/
├── main.js        # process lifecycle, window, tray, health probe
├── preload.js     # (empty) keeps contextIsolation on
├── loading.html   # boot / error splash shown until /health is green
├── assets/icon.png
└── package.json
```

## Prerequisites

1. **Prefrontal installed** in the repo (one directory up):

   ```bash
   cd ..                       # repo root
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -e .
   prefrontal init-db
   prefrontal user add me --operator   # prints your dashboard token
   ```

   The wrapper looks for `../.venv/bin/prefrontal` first, then falls back to
   `prefrontal` on your `PATH`.

2. **Node.js** (for Electron itself):

   ```bash
   cd desktop
   npm install
   ```

## Run

```bash
npm start
```

On launch it will:

1. Probe `http://127.0.0.1:8000/health`. If something is already serving (e.g.
   the launchd agent in [`../deploy/`](../deploy/)), it attaches to that instead
   of starting a second copy.
2. Otherwise spawn `prefrontal serve` with the repo as its working directory (so
   the repo's `.env` is loaded).
3. Load `/dashboard` once healthy. The dashboard prompts once for your
   `X-Prefrontal-Token` and remembers it.

Closing the window hides it to the menu bar (the server keeps running). Use the
menu-bar icon to **Open Dashboard**, **Restart Server**, **View Logs…**, or
**Quit**.

## Configuration

All optional — sensible defaults are derived from the checkout layout.

| Env var           | Default                       | Purpose                                  |
| ----------------- | ----------------------------- | ---------------------------------------- |
| `PREFRONTAL_DIR`  | parent of `desktop/`          | Backend working dir (must hold `.env`)   |
| `PREFRONTAL_BIN`  | `$DIR/.venv/bin/prefrontal`   | Path to the `prefrontal` executable      |
| `PREFRONTAL_PORT` | `8000`                        | Port the server binds / the window loads |

Example:

```bash
PREFRONTAL_DIR=~/prefrontal PREFRONTAL_PORT=8123 npm start
```

## Packaging a `.app` / `.dmg`

```bash
npm run dist
```

This builds with `electron-builder` into `dist/`. Note: it bundles the Electron
shell, **not** the Python backend — the packaged app still expects `prefrontal`
to be installed on the machine (via the venv or `PATH`). For a fully always-on
setup that survives reboots, the launchd agent in
[`../deploy/`](../deploy/com.morningstatic.prefrontal.plist) is still the
recommended way to run the server; this app can then just attach to it.

> The icon is a 256×256 PNG. For a crisper packaged app, drop a 512×512 (or
> `.icns`) at `assets/icon.png` before running `npm run dist`.
