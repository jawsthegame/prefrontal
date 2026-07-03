# Deploying Prefrontal on a Mac mini

This runbook gets Prefrontal running always-on on an Apple Silicon Mac mini and
wires up the integrations from the README stack:

- **Ollama** — local model inference (orchestrated by n8n)
- **n8n** — workflow orchestration and notification delivery
- **iOS Shortcuts** — one-tap outcome logging and location triggers
- **ntfy** (Pushover optional) — notification delivery, with one-tap action buttons
- **Tailscale** — secure remote access from your phone

**Architecture for this setup:** Prefrontal runs as-is — it is the *memory +
webhook* core. It stores episodes, derives the behavioral profile, and serves it
over HTTP. **n8n does the orchestration**: it fetches the profile, calls Ollama
to compose a reminder, delivers it via ntfy (Pushover optional), and logs the result back
into Prefrontal. Outcome capture ("Made it" / "Missed it") goes straight from an
iOS Shortcut to Prefrontal.

```
iOS Shortcut (one-tap) ─────────────► POST /webhooks/shortcut ─► episodes
                                                                     │
n8n schedule ─► GET /profile ─► Ollama ─► ntfy ─► POST /webhooks/n8n
```

The glue files referenced below live in [`../deploy/`](../deploy/).

---

## 0. Prerequisites

Open Terminal on the Mac mini. Install [Homebrew](https://brew.sh) if you don't
have it, then:

```bash
brew install python@3.12 ollama tailscale
```

**n8n is not in Homebrew** — install it with npm (see step 7). It needs
**Node.js 22 LTS** (newer Node breaks n8n's native `isolated-vm` build):

```bash
brew install node@22
brew link --force --overwrite node@22   # node@22 is keg-only
```

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

### Optional: Claude for the dashboard assistant

The dashboard's assistant chat box (natural-language edits to todos/commitments)
parses with the local Ollama model by default — no extra setup. For more reliable
parsing you can opt into Claude: install the extra and set a key.

```bash
pip install -e '.[anthropic]'          # adds the official anthropic SDK
# in .env:
#   ANTHROPIC_API_KEY=sk-ant-...        # enables Claude for the assistant
#   ANTHROPIC_MODEL=claude-opus-4-8     # optional; this is the default
```

When the key is set the assistant prefers Claude and falls back to Ollama if it's
unreachable; with no key it stays fully local. The key is only ever sent on
outbound calls to Anthropic — no other data leaves the host because of this.

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

Install via npm (n8n is not a Homebrew formula). The native `isolated-vm`
dependency compiles with `node-gyp`, which needs a Python that still ships
`distutils` — provide it with `setuptools` in a venv:

```bash
python3.12 -m venv ~/.n8n-gyp && ~/.n8n-gyp/bin/pip install setuptools
npm_config_python=~/.n8n-gyp/bin/python npm install -g n8n
n8n start                           # editor at http://localhost:5678
```

To run it always-on, add a launchd agent like the one in step 3 whose
`ProgramArguments` is `/opt/homebrew/bin/n8n start` and whose `PATH` includes
`/opt/homebrew/opt/node@22/bin`. (Or just `npx n8n` for ad-hoc use.)

> **macOS networking gotcha:** the bundled workflows call Prefrontal and Ollama
> at `http://127.0.0.1:...`, **not** `localhost`. Node resolves `localhost` to
> IPv6 `::1`, but Prefrontal (uvicorn `0.0.0.0`) and Ollama bind IPv4 only, so
> `localhost` URLs fail with `ECONNREFUSED`. Keep any new HTTP nodes on
> `127.0.0.1`. Also: stop n8n before editing its SQLite DB directly — it holds
> the DB open (WAL) and caches active workflows in memory.

1. Open the editor, **Import from File**, and choose
   [`../deploy/n8n/departure-reminder.workflow.json`](../deploy/n8n/departure-reminder.workflow.json).
2. The workflow has five nodes: a schedule trigger (every 5 min) →
   `POST /webhooks/departure/check` → a Code node that continues only when a
   reminder is due (`fire === true`) → **Publish to ntfy** → `POST /webhooks/n8n`
   (log that a reminder fired). Prefrontal does the "when to leave" reasoning and
   returns a ready `message`, so there's no Ollama compose step on this path.
3. Set credentials/values it can't ship with:
   - **Prefrontal token** — in the two Prefrontal HTTP nodes' `X-Prefrontal-Token`
     header, paste your `PREFRONTAL_WEBHOOK_SECRET`.
   - **ntfy** — the Publish node posts to `https://ntfy.sh` on the `prefrontal-me`
     topic; change the topic to your own (and the URL to a self-hosted ntfy if you
     run one). Subscribe your phone to that topic in the ntfy app. The push
     carries the endpoint's signed **Made it / Missed it** buttons (see the
     one-tap note below). *(Prefer Pushover? The native Python delivery client
     (`prefrontal coach --deliver`) still supports it; or swap this node for a
     Pushover HTTP node.)*
4. **Execute Workflow** once to test, then toggle it **Active**.

> **For a *travel-time* estimate** (rather than the static `lead_minutes`
> fallback), Prefrontal needs two things: the phone's recent location (set up the
> "Update location" shortcut in `deploy/ios-shortcut.md`) and destination
> coordinates on commitments (`dest_lat`/`dest_lon`, populated by the calendar
> sync or a manual `POST /commitments`). Without them the reminder still fires —
> it just leans on each commitment's `lead_minutes`.

> **One-tap action buttons.** The departure and coffee-nudge workflows attach the
> signed **action buttons** the endpoint returns — **Made it / Missed it** on a
> departure, **I'm back / Abandon** on an outing. ntfy renders them as inline
> `http` buttons, so a tap fires `GET /nudge/act` in the **background** (no app
> switch): it logs the outcome and, for an outing, pins the escalation so no
> further push or 150% call fires. The buttons are only minted when both
> `OAUTH_BASE_URL` (your Tailscale HTTPS origin — the tap fires off-box) and
> `SESSION_SECRET` (signs the link) are set; otherwise `actions` is empty and you
> get a plain notification. *(A signed `GET /nudge/dismiss` link still exists for
> a Pushover-style single "silence" URL if you build a Pushover node instead.)*

This is a starting template — node `typeVersion`s can differ across n8n
releases, so adjust any node n8n flags on import.

---

## 8. Module 1 — Location-Aware Task Anchor (Coffee Shop Nudge)

The escalation logic lives in Prefrontal; n8n polls and delivers. Flow:

```
"Going out" Shortcut ─► POST /webhooks/outing/start   (logs intention + window)
n8n (every minute)   ─► POST /webhooks/outing/check   (returns due nudges)
                          ├─ level soft (50%)  ─► ntfy
                          ├─ level firm (100%) ─► ntfy (high priority)
                          └─ level call (150%) ─► Twilio voice call
"I'm back" Shortcut  ─► POST /webhooks/outing/return  (logs actual vs stated)
```

> **Impact analysis** rides along automatically: if you've synced commitments
> (§10), the check projects your realistic return from the learned bias and names
> any at-risk commitment in the nudge ("…'Team sync' is now at risk"), with a
> `hard_conflict` flag your workflow can use to escalate harder.

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
4. **ntfy Push (50% / 100%)** node → set your ntfy topic (defaults to
   `prefrontal-me` on `https://ntfy.sh`); it carries the **I'm back / Abandon**
   buttons. (Prefer Pushover? Swap this for a Pushover HTTP node.)
5. (Optional) Set your name for the voice message:
   `prefrontal` has a `user_name` coaching key —
   `sqlite3 prefrontal.db "UPDATE coaching_state SET value='Tom' WHERE key='user_name';"`
6. **Execute Workflow** to test, then toggle **Active**.

**c. Build the iOS shortcuts**

"Going out" and "I'm back" — see
[`../deploy/ios-shortcut.md`](../deploy/ios-shortcut.md).

**c2. (Optional) Location-gating**

If you feed the phone's location into the `Check Outings` node body
(`current_lat`/`current_lon`) and set `home_lat`/`home_lon` when starting an
outing, Prefrontal will **suppress the call and close the outing automatically
once you're within the home radius** (`home_radius_m`, default 150 m). That means
coming home early — or forgetting to tap "I'm back" — never triggers the 150%
call. Without location it falls back to pure time-based escalation, so this is
optional. Outings left open past `abandon_after_ratio`× the window (default 3×)
auto-close as `abandoned`.

Two ways to supply location — an iOS arrival geofence (simplest) or continuous
Home Assistant tracking (full mid-outing gating) — are written up in
[`../deploy/ios-shortcut.md`](../deploy/ios-shortcut.md) under "Location source".

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
3. Run the n8n workflow manually → an ntfy notification arrives on your
   phone, and a new row appears: `sqlite3 prefrontal.db 'SELECT * FROM episodes ORDER BY id DESC LIMIT 3;'`
4. Tap "Made it" in the iOS Shortcut → another episode row lands.
5. `prefrontal profile` → confirm preferences/patterns read as expected.

---

## 10. Calendar sync + double-booking alerts

Feeds your schedule into Prefrontal's `commitments` table so it can (next)
do impact analysis and (now) flag double-bookings across calendars.

Flow: every 15 min, n8n fetches **four read-only ICS feeds** — personal, work,
Outlook, and family — parses each, merges them, and POSTs the combined batch to
`/webhooks/calendar/sync`. It then sends an ntfy **double-booking alert** for hard
overlaps and a separate **possible-conflict alert** for soft ones. (No Google
OAuth node — every calendar is pulled via its secret iCal URL, so there are no
credentials to manage.)

```
n8n (every 15 min) ─┬─ GET personal ICS ─┐
                    ├─ GET work ICS      ├─► merge ─► POST /webhooks/calendar/sync
                    ├─ GET Outlook ICS   │             ├─ double-booked ─► ntfy alert
                    └─ GET family ICS  ──┘             └─ possible conflict ─► ntfy alert
```

1. Import [`../deploy/n8n/calendar-sync.workflow.json`](../deploy/n8n/calendar-sync.workflow.json).
2. **Get {Personal,Work,Outlook,Family} ICS nodes** → paste each calendar's
   shared/secret **iCal feed URL** (read-only). Google/Outlook/most providers
   expose one per calendar (in Google: *Settings → Secret address in iCal format*).
   Leave a node's URL blank to skip that feed.
3. **POST sync node** → set the `X-Prefrontal-Token` header to your secret.
4. **Double-booking / possible-conflict alert** nodes → set your ntfy topic
   (defaults to `prefrontal-me`).
5. **Execute Workflow** once, then toggle **Active**.

Notes:
- Events are namespaced per feed (`personal:…` / `work:…` / `outlook:…` /
  `family:…`), so the feeds are deduped and one calendar's sync never prunes
  another's events. Always sync all calendars together (this workflow does) so
  pruning stays correct.
- The ICS parser is minimal (UID/SUMMARY/DTSTART/DTEND/LOCATION/TZID). It sends
  each time as-is with its `TZID`; **Prefrontal resolves the zone to UTC
  server-side** (IANA names and Windows names like `Eastern Standard Time` both
  work). A time with no zone (floating, or a manual commitment) is interpreted in
  `PREFRONTAL_TIMEZONE` — set that to your home zone so unzoned events don't land
  hours off. Times with an explicit offset or `Z` keep their own zone regardless.
- Check the result: `GET /commitments` (upcoming) and `GET /commitments/conflicts`
  (overlaps). Add one-offs with `POST /commitments`.

---

## 11. Morning briefing

A daily digest of today's commitments, double-bookings, what slipped this past
week, and a reminder of your time bias — calibrated to `preferred_briefing_format`
(`short`/`long`).

- Preview it now: `prefrontal briefing` (add `--llm` for Ollama prose).
- Deliver it daily: import [`../deploy/n8n/morning-briefing.workflow.json`](../deploy/n8n/morning-briefing.workflow.json),
  set the `X-Prefrontal-Token` header and your ntfy topic. It fires at 7am,
  `GET /briefing`, and pushes the digest text. (Insert an Ollama node between the
  two for prose, or point it at `prefrontal summarize`-style output.)
- The briefing is best once calendars are syncing (§10) and a few days of
  episodes have accrued (so "what slipped" and the bias are meaningful).

---

## 11a. Panic mode (on-demand + proactive)

The calm morning briefing's opposite number, for when you're overwhelmed and
frozen: it ranks what's *actually* bearing down now — across calendar, todos,
and mail — into already-behind / bearing-down-soon / piling-up, and hands back
**one concrete first step**.

- Preview it now: `prefrontal panic` (add `--llm` for Ollama prose).
- **On the dashboard / family view:** the "😮‍💨 Panic" / "Feeling overwhelmed?"
  button opens a focused overlay backed by `GET /panic`. Nothing to configure.
- **One tap from your phone:** add the **"Panic"** shortcut (see
  [`../deploy/ios-shortcut.md`](../deploy/ios-shortcut.md)) — it `GET /panic`s and
  reads back the `headline` (grounding + first step) in one tap.
- **Proactive nudge:** import
  [`../deploy/n8n/panic-check.workflow.json`](../deploy/n8n/panic-check.workflow.json),
  set the token + your ntfy topic. It polls `POST /webhooks/panic/check`
  every 20 min and pushes **only when the plate tips into overwhelm**. The server
  edge-triggers on the level (like the departure signature) and honors a
  cooldown, so a sustained pile-up nudges once, not every poll.
- Tune the bar with coaching-state keys (defaults in parentheses):
  ```
  # How many "pressing" items (with something already late) counts as overwhelm (3):
  sqlite3 prefrontal.db "UPDATE coaching_state SET value='4' WHERE key='panic_alert_min_pressing';"
  # Minutes to stay quiet after an alert, even across a new spike (180):
  sqlite3 prefrontal.db "UPDATE coaching_state SET value='240' WHERE key='panic_alert_cooldown_minutes';"
  ```

---

## 12. Schedule the nightly learning pass

The behavioral profile only improves if patterns are recomputed as episodes
accrue. Run the learning pass on a schedule (nightly is plenty for one user):

```bash
prefrontal learn       # episodes -> calibrated patterns + time-estimation bias
prefrontal summarize   # profile -> Ollama -> cache (+ profile.md) as prose
```

`learn` is **recency-weighted**: each episode counts by an exponential decay on
its age (half-life `learning_half_life_days`, default 30 days), so the profile
follows you as your behavior changes instead of being anchored to a year-old
average. Tune it per user in `coaching_state`
(`sqlite3 prefrontal.db "INSERT OR REPLACE INTO coaching_state (user_id, key, value, source) VALUES (<uid>, 'learning_half_life_days', '45', 'explicit');"`),
or set it to `0` to weigh all history equally.

`prefrontal summarize` generates the narrative with the local Ollama model from
`.env` (`OLLAMA_MODEL`, default `llama3.1:8b`); if Ollama is down it falls back to
the structured profile, so it always produces something. It **caches** the
narrative in the `profile_cache` table (and writes `profile.md` for inspection),
so the live `GET /profile` endpoint serves that prose to agents without a
per-request model call. Add `?refresh=1` to regenerate on demand, or
`?format=structured` to get the raw structured profile instead. The `X-Profile-*`
response headers report the source, model, generation time, and whether the cache
has gone stale relative to the current facts (i.e. it's time to re-run
`summarize`).

`deploy/learn.sh` chains the two steps (launchd can't run `learn && summarize`
in one `ProgramArguments`), with timestamped logging (a `summarize` failure is
logged but treated as non-fatal), and
`deploy/com.morningstatic.prefrontal-learn.plist` runs it nightly at 03:30 —
a *periodic* job (no `KeepAlive`), distinct from the always-on server agent in
§3.

```bash
cp deploy/com.morningstatic.prefrontal-learn.plist ~/Library/LaunchAgents/
# Edit the paths inside both files to match your install:
#   - learn.sh:  PREFRONTAL_HOME (repo root, so the adjacent .env loads)
#   - plist:     ProgramArguments[0], WorkingDirectory, PREFRONTAL_HOME,
#                Std{Out,Err}Path, and Hour/Minute if 03:30 doesn't suit
launchctl load -w ~/Library/LaunchAgents/com.morningstatic.prefrontal-learn.plist
launchctl start com.morningstatic.prefrontal-learn   # run once now, don't wait
tail -f ~/Library/Logs/prefrontal.learn.log          # watch it work
```

`StartCalendarInterval` fires at the wall-clock time daily; if the mini is
asleep then, launchd runs the job on the next wake. Prefer `cron` or an n8n
schedule node instead? Either works — they just call the same two commands.

## 13. Schedule periodic mail fetch (optional)

If you triage email through Prefrontal, run the built-in mail fetch on a timer
(the mail analogue of §12). `deploy/mail-fetch.sh` pulls and syncs a batch;
`deploy/com.morningstatic.prefrontal-mail.plist` runs it on an interval via
launchd.

```bash
cp deploy/com.morningstatic.prefrontal-mail.plist ~/Library/LaunchAgents/
# Edit the paths inside mail-fetch.sh + the plist to match your install
# (PREFRONTAL_HOME, ProgramArguments[0], Std{Out,Err}Path, and the interval),
# then load it:
launchctl load -w ~/Library/LaunchAgents/com.morningstatic.prefrontal-mail.plist
```

(Alternatively an n8n Gmail/IMAP workflow can POST batches to
`/webhooks/mail/sync` — the same shape as the calendar sync.)

## 14. Multiple users (optional)

One deployment can serve several people (full design in
[`multi-tenant.md`](multi-tenant.md)). Provision each with the CLI — it prints
their token **once**:

```bash
prefrontal user add sam --display-name "Sam"   # prints Sam's token — save it
prefrontal user list                           # never prints tokens
prefrontal user rotate sam                      # new token; the old one stops working
```

Each person pastes their own token into their Shortcuts / widget, and the server
scopes every request to that user. Solo installs can skip this — the single
`PREFRONTAL_WEBHOOK_SECRET` (or `PREFRONTAL_DEFAULT_USER`) keeps the one-user path
working. The `/admin/users` endpoints do the same over HTTP for an operator token.

---

## 15. Coaching agent (unified nudge tick)

The coaching agent (`docs/coaching-agent.md`) is one poll that fans over every
enabled module — it asks each "anything due?", picks a channel (urgency floor →
learned `channel_response` bump), and suppresses on quiet hours + debounce.

- Preview: `prefrontal coach` (add `--dry-run` to see cues before suppression).
- Deliver: import [`../deploy/n8n/coach-check.workflow.json`](../deploy/n8n/coach-check.workflow.json),
  set the token + ntfy topic. It polls `POST /webhooks/coach/check` and publishes
  each returned cue to ntfy at a priority matching the agent's chosen channel,
  passing through any signed one-tap `actions` a cue carries (so ntfy renders the
  background buttons). Today it surfaces the Task Paralysis "tiny first step"
  nudge, the outing escalation cues, and — when enabled — the **Self-Care checks**
  ("have you eaten?" with one-tap Ate / Snooze, and "drink some water" with Drank /
  Snooze). Turn them on by setting the `self_care` coaching key to `on`; each is a
  basic-needs check with a **daily target** — the meal check is target 1 (one Ate
  ends it for the day; tune `meal_start_hour`, `meal_reask_minutes`), water is
  `water_daily_target` (default 6; tune `water_start_hour`, `water_interval_minutes`),
  and each Drank counts one and defers a full interval. Both interrupt a focus
  block by design, respect responsive hours, and can be toggled individually
  (`meal_enabled` / `water_enabled`). The intervals also **self-tune** in the
  nightly `learn`: snooze a lot and it widens; respond genuinely and it eases off
  — but a burst of *instant* confirms (reflexive dismissals) holds it rather than
  backing off. Pin an interval yourself (set it with `source='explicit'`) and the
  learner leaves it alone. New module coaching lights up as each `evaluate()` is
  filled in.

---

## 16. Encouragement (rough-day recovery, opt-in)

The counterweight to nudging: when a day genuinely goes rough, one warm message
with a get-back-on-track plan instead of another reminder (`docs/encouragement.md`).

- **Off by default** — enable per user via coaching-state keys (defaults shown):
  ```
  # Master switch (off ⇒ /encouragement always returns rough:false):
  sqlite3 prefrontal.db "UPDATE coaching_state SET value='on' WHERE key='encouragement';"
  # Rough-score threshold (a missed hard commitment = 3.0 trips it alone):
  sqlite3 prefrontal.db "INSERT OR REPLACE INTO coaching_state(user_id,key,value,source) \
    SELECT id,'encouragement_threshold','3.0','explicit' FROM users LIMIT 1;"
  # Tone: 'warm' (reassuring) or 'plain' (matter-of-fact, no affect words):
  sqlite3 prefrontal.db "INSERT OR REPLACE INTO coaching_state(user_id,key,value,source) \
    SELECT id,'encouragement_tone','warm','explicit' FROM users LIMIT 1;"
  ```
- Preview: `prefrontal encourage` (add `--llm` for warmer Ollama prose).
- Deliver: import [`../deploy/n8n/encouragement.workflow.json`](../deploy/n8n/encouragement.workflow.json).
  It polls `GET /encouragement` a few times an afternoon and, on a rough & unsent
  day, pushes the message to ntfy **once**, then `POST /encouragement/sent` stamps
  the day (a `last_encouragement_date` cursor caps it at one per day).

---

## 17. Hyperfocus + interactive nudges

Two more delivery workflows round out the module coverage:

- **Hyperfocus interrupts** — import
  [`../deploy/n8n/hyperfocus-check.workflow.json`](../deploy/n8n/hyperfocus-check.workflow.json).
  It polls `POST /webhooks/focus/check` and pushes the gentle alignment check /
  biological-break nudge when an aligned block overruns.
- **Calendar-armed focus** — import
  [`../deploy/n8n/focus-arm-check.workflow.json`](../deploy/n8n/focus-arm-check.workflow.json).
  Every 5 min it POSTs `/webhooks/focus/arm`, which auto-starts a protected
  session whenever a "focus"/"deep work" calendar block is live and none is
  running — so a session you scheduled needs zero taps to begin. (Pairs with the
  one-tap `focus/start` — leave its body empty and it infers the task from your
  top open todo.)
- **Interactive one-tap actions over ntfy** — import
  [`../deploy/n8n/interactive-nudge-ntfy.workflow.json`](../deploy/n8n/interactive-nudge-ntfy.workflow.json),
  the reference for wiring ntfy's action buttons back to the signed `/nudge/act`
  links so a tap (Made it, ⭐ Yes, Ate, …) records straight into Prefrontal.

---

## 18. Parent pack — the shared household sheet (optional)

For co-parents. One shared, always-current sheet both parents read and edit —
kids' facts, agreements/star charts, a shopping list — with load-balancing pushes.

1. **Set up the household.** Either operator-wire it (`prefrontal household add
   "<name>"` then `prefrontal household join <handle> --household <id>`), or let a
   parent self-serve: `POST /household/create`, then `POST /household/invites` →
   share the code/link → the co-parent `POST /household/invites/redeem` (CLI:
   `prefrontal household invite` / `redeem`). A single parent can run solo; the
   load-balancing lights up once a second member joins.
2. **Use it.** The editable sheet is the **`/kids`** dashboard (partner glance at
   `/family`); edit in plain English via `POST /assistant`. CLI:
   `prefrontal household show|star|balance|shopping|chore`.
3. **Schedule the sweeps** (import each, set the ntfy topic + token):
   - [`star-prompt-check.workflow.json`](../deploy/n8n/star-prompt-check.workflow.json)
     — asks both parents "did <kid> earn a star today?" on the chart's schedule.
   - [`checkin-check.workflow.json`](../deploy/n8n/checkin-check.workflow.json)
     — the optional weekly mental-load check-in.
   - [`digest-check.workflow.json`](../deploy/n8n/digest-check.workflow.json)
     — the daily delta digest: pushes each parent what the *other* changed.
   - [`chores-check.workflow.json`](../deploy/n8n/chores-check.workflow.json)
     — recurring shared chores: a lead-time reminder to the owner, and a
     miss-handoff to the *other* parent if a chore slips past due (every 15 min).

Full design and data model: [`household-sheet.md`](household-sheet.md).

---

## What's not automated yet

All six modules are wired end-to-end today — **Location-Aware Task Anchor** (the
coffee-shop nudge above), **Hyperfocus** (focus sessions, `/webhooks/focus/*`),
**Time Blindness** (departure timing + outcome capture), **Task Paralysis**
(auto-decompose, tiny first step, body-double), **Impulsivity**
(`reflective_pause` + `capture_and_defer`; only `switch_rate_feedback` is still
planned), and **Self-Care** (the opt-in "have you eaten? / had water?" meal &
water checks — set the `self_care` coaching key to `on`, delivered through the
coaching tick §15 with one-tap Ate/Drank/Snooze). The coaching agent (§15) now fans over all of them, and the
encouragement layer (§16) handles rough days. Run `prefrontal modules -v` for the
live status, and see `ROADMAP.md` for what's next. (Mail ingestion is also live —
`prefrontal mail fetch`/`sync` and `POST /webhooks/mail/sync`; see §13.)
