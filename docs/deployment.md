# Deploying Prefrontal on a Mac mini

This runbook gets Prefrontal running always-on on an Apple Silicon Mac mini and
wires up the integrations from the README stack:

- **Ollama** — local model inference (orchestrated by n8n)
- **n8n** — workflow orchestration and notification delivery
- **iOS Shortcuts** — one-tap outcome logging and location triggers
- **Pushover / Ntfy** — notification delivery
- **Tailscale** — secure remote access from your phone

**Architecture for this setup:** Prefrontal runs as-is — it is the *memory +
webhook* core. It stores episodes, derives the behavioral profile, and serves it
over HTTP. **n8n does the orchestration**: it fetches the profile, calls Ollama
to compose a reminder, delivers it via Pushover/Ntfy, and logs the result back
into Prefrontal. Outcome capture ("Made it" / "Missed it") goes straight from an
iOS Shortcut to Prefrontal.

```
iOS Shortcut (one-tap) ─────────────► POST /webhooks/shortcut ─► episodes
                                                                     │
n8n schedule ─► GET /profile ─► Ollama ─► Pushover/Ntfy ─► POST /webhooks/n8n
```

The glue files referenced below live in [`../deploy/`](../deploy/).

---

## 0. Prerequisites

Open Terminal on the Mac mini. Install [Homebrew](https://brew.sh) if you don't
have it, then:

```bash
brew install python@3.12 ollama n8n tailscale
```

(You can also run n8n via `npx n8n` or Docker; Homebrew is simplest for an
always-on host.)

---

## 1. Install Prefrontal

```bash
# Clone to a stable location (this runbook assumes ~/prefrontal)
git clone https://github.com/jawsthegame/prefrontal.git ~/prefrontal
cd ~/prefrontal

python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Verify the CLI is on the venv path:

```bash
which prefrontal        # -> ~/prefrontal/.venv/bin/prefrontal
prefrontal --version
```

---

## 2. Configure

```bash
cp .env.example .env
```

Edit `.env`. At minimum set a strong webhook secret:

```ini
PREFRONTAL_DB_PATH=/Users/tom/prefrontal/prefrontal.db
PREFRONTAL_HOST=0.0.0.0
PREFRONTAL_PORT=8000

# Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"
PREFRONTAL_WEBHOOK_SECRET=replace-with-a-long-random-string

# Enable all challenge modules (leave blank), or pick a subset:
PREFRONTAL_MODULES=

# Outbound events to n8n are optional — only if you want Prefrontal to push
# episode.logged events into a workflow. Delivery/inference don't need this.
N8N_WEBHOOK_URL=
N8N_WEBHOOK_TOKEN=
```

Create and seed the database:

```bash
prefrontal init-db
prefrontal modules -v      # confirm the modules you expect are enabled
```

---

## 3. Run always-on with launchd

Copy the bundled launch agent and edit the paths inside it to match your
username and install location:

```bash
cp deploy/com.morningstatic.prefrontal.plist ~/Library/LaunchAgents/
# Edit ~/Library/LaunchAgents/com.morningstatic.prefrontal.plist:
#   - ProgramArguments[0] -> /Users/<you>/prefrontal/.venv/bin/prefrontal
#   - WorkingDirectory    -> /Users/<you>/prefrontal
#   - Std{Out,Err}Path    -> /Users/<you>/Library/Logs/prefrontal.*.log
```

Load and start it:

```bash
launchctl load -w ~/Library/LaunchAgents/com.morningstatic.prefrontal.plist
launchctl list | grep prefrontal           # should show the job
curl -s http://localhost:8000/health       # {"status":"ok",...}
```

`KeepAlive` restarts it on crash; `RunAtLoad` starts it on login/boot. To stop
or reload after editing:

```bash
launchctl unload ~/Library/LaunchAgents/com.morningstatic.prefrontal.plist
launchctl load -w ~/Library/LaunchAgents/com.morningstatic.prefrontal.plist
```

Logs: `tail -f ~/Library/Logs/prefrontal.err.log`.

---

## 4. Ollama (local model)

```bash
brew services start ollama          # always-on daemon on :11434
ollama pull llama3.1:8b             # or your preferred model
# Smoke test:
curl -s http://localhost:11434/api/generate \
  -d '{"model":"llama3.1:8b","prompt":"say hi","stream":false}' | head
```

n8n calls this endpoint; Prefrontal itself does not (in this setup). Pick a model
that fits the mini's RAM — `llama3.1:8b` is a good default on 16GB+.

---

## 5. Tailscale (remote access)

So your phone can reach the Mac mini from anywhere without exposing it publicly:

```bash
sudo tailscale up
tailscale ip -4          # note the 100.x.y.z address; or use the MagicDNS name
```

Your phone (with the Tailscale app, same tailnet) can now reach
`http://<mac-mini-tailscale-name>:8000`. Use this host in the iOS Shortcut.

> Keep `PREFRONTAL_WEBHOOK_SECRET` set. Tailscale limits *who* can connect; the
> token authenticates *what* connects. Use both.

---

## 6. iOS Shortcuts (one-tap capture)

Build the "Made it" / "Missed it" shortcuts and the optional location automation
following [`../deploy/ios-shortcut.md`](../deploy/ios-shortcut.md). They POST to
`/webhooks/shortcut` with your token. Quick test from the Mac first:

```bash
source .env 2>/dev/null
curl -s -X POST http://localhost:8000/webhooks/shortcut \
  -H "X-Prefrontal-Token: $PREFRONTAL_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"action":"made_it","episode_type":"departure","channel":"notification"}'
```

---

## 7. n8n (orchestration + delivery)

```bash
brew services start n8n             # editor at http://localhost:5678
```

1. Open the editor, **Import from File**, and choose
   [`../deploy/n8n/departure-reminder.workflow.json`](../deploy/n8n/departure-reminder.workflow.json).
2. The workflow has five nodes: a schedule trigger → `GET /profile` → Ollama
   compose → Pushover/Ntfy send → `POST /webhooks/n8n` (log that a reminder
   fired).
3. Set credentials/values it can't ship with:
   - **Prefrontal token** — in the two Prefrontal HTTP nodes' `X-Prefrontal-Token`
     header, paste your `PREFRONTAL_WEBHOOK_SECRET`.
   - **Pushover** — set `PUSHOVER_TOKEN` and `PUSHOVER_USER` in the send node
     (or swap it for an Ntfy HTTP node hitting `https://ntfy.sh/<your-topic>`).
   - **Ollama model** — match what you pulled in step 4.
   - Replace the **calendar placeholder** Set node with a real Google
     Calendar / CalDAV node when you're ready; the template hardcodes a sample
     event so you can see the end-to-end flow immediately.
4. **Execute Workflow** once to test, then toggle it **Active**.

This is a starting template — node `typeVersion`s can differ across n8n
releases, so adjust any node n8n flags on import.

---

## 8. Verify end to end

1. `curl http://localhost:8000/health` → ok.
2. `curl -H "X-Prefrontal-Token: $PREFRONTAL_WEBHOOK_SECRET" http://localhost:8000/profile`
   → your behavioral profile in Markdown.
3. Run the n8n workflow manually → a Pushover/Ntfy notification arrives on your
   phone, and a new row appears: `sqlite3 prefrontal.db 'SELECT * FROM episodes ORDER BY id DESC LIMIT 3;'`
4. Tap "Made it" in the iOS Shortcut → another episode row lands.
5. `prefrontal profile` → confirm preferences/patterns read as expected.

---

## What's not automated yet

The interventions declared by each module (`prefrontal modules -v`) are mostly
`planned` — the n8n workflow gives you the proactive reminder loop today, but the
per-module logic (escalation paths, hyperfocus protect-vs-interrupt, etc.) is
still to be wired. The statistical pass that turns accumulated `episodes` into
calibrated `patterns` is also a TODO; until then the profile reflects the seeded
coaching defaults and any patterns you write directly. See the repo issues /
roadmap for what's next.
