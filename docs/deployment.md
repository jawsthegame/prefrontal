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

## 8. Module 1 — Location-Aware Task Anchor (Coffee Shop Nudge)

The escalation logic lives in Prefrontal; n8n polls and delivers. Flow:

```
"Going out" Shortcut ─► POST /webhooks/outing/start   (logs intention + window)
n8n (every minute)   ─► POST /webhooks/outing/check   (returns due nudges)
                          ├─ level soft (50%)  ─► Pushover
                          ├─ level firm (100%) ─► Pushover (high priority)
                          └─ level call (150%) ─► Twilio voice call
"I'm back" Shortcut  ─► POST /webhooks/outing/return  (logs actual vs stated)
```

**a. Twilio setup**

1. Create a [Twilio](https://www.twilio.com/) account and buy a voice-capable
   number. Note your **Account SID**, **Auth Token**, and the number (E.164,
   e.g. `+15551234567`).
2. In n8n: **Credentials → New → Basic Auth**. Username = Account SID,
   Password = Auth Token. Name it e.g. "Twilio".

**b. Import and configure the workflow**

1. Import [`../deploy/n8n/coffee-shop-nudge.workflow.json`](../deploy/n8n/coffee-shop-nudge.workflow.json).
2. **Check Outings** node → set the `X-Prefrontal-Token` header to your secret.
3. **Twilio Voice Call** node → attach the Basic Auth credential; replace
   `REPLACE_WITH_TWILIO_ACCOUNT_SID` in the URL, and set `To` (your phone) and
   `From` (your Twilio number). The spoken text comes from `{{ $json.message }}`.
4. **Pushover Push** node → set `PUSHOVER_TOKEN` / `PUSHOVER_USER` (or swap for
   an Ntfy HTTP node).
5. (Optional) Set your name for the voice message:
   `prefrontal` has a `user_name` coaching key —
   `sqlite3 prefrontal.db "UPDATE coaching_state SET value='Tom' WHERE key='user_name';"`
6. **Execute Workflow** to test, then toggle **Active**.

**c. Build the iOS shortcuts**

"Going out" and "I'm back" — see
[`../deploy/ios-shortcut.md`](../deploy/ios-shortcut.md).

**c2. (Optional) Location-gating**

If you feed the phone's location into the `Check Outings` node body
(`current_lat`/`current_lon`, from a Home Assistant or iOS Shortcuts source) and
set `home_lat`/`home_lon` when starting an outing, Prefrontal will **suppress the
call and close the outing automatically once you're within the home radius**
(`home_radius_m`, default 150 m). That means coming home early — or forgetting to
tap "I'm back" — never triggers the 150% call. Without location it falls back to
pure time-based escalation, so this is optional. Outings left open past
`abandon_after_ratio`× the window (default 3×) auto-close as `abandoned`.

**d. Try it (fast, no waiting)**

Start an outing with a tiny window so thresholds trip within a minute or two:

```bash
source .env 2>/dev/null
curl -s -X POST http://localhost:8000/webhooks/outing/start \
  -H "X-Prefrontal-Token: $PREFRONTAL_WEBHOOK_SECRET" -H "Content-Type: application/json" \
  -d '{"intention":"test coffee run","time_window_minutes":1}'

# Wait ~30s (50%), then poll the way n8n does — note level/fire/message:
curl -s -X POST http://localhost:8000/webhooks/outing/check \
  -H "X-Prefrontal-Token: $PREFRONTAL_WEBHOOK_SECRET" -d '{}' | python3 -m json.tool

# Close it and see intention-vs-actual logged as an episode:
curl -s -X POST http://localhost:8000/webhooks/outing/return \
  -H "X-Prefrontal-Token: $PREFRONTAL_WEBHOOK_SECRET" -d '{}' | python3 -m json.tool
```

---

## 9. Verify end to end

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
still to be wired. See `ROADMAP.md` for what's next.

**Schedule the learning pass + summary.** Run these periodically (nightly is
plenty for one user) via a launchd agent with `StartCalendarInterval`, `cron`, or
an n8n schedule node:

```bash
prefrontal learn       # episodes -> calibrated patterns + time-estimation bias
prefrontal summarize   # structured profile -> Ollama -> profile.md (prose)
```

`prefrontal summarize` writes a narrative `profile.md` using the local Ollama
model from `.env` (`OLLAMA_MODEL`, default `llama3.1:8b`); if Ollama is down it
falls back to the structured profile, so the file is always written. The live
`GET /profile` endpoint always returns the fast structured profile — point agents
at the generated `profile.md` when you want the prose version.
