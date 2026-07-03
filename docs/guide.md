# Prefrontal — Usage Guide

Prefrontal is a local-first executive-function assistant. It watches your
outings, calendar, todos, focus, and mail, learns your patterns, and nudges you
at the right moments — all running on your own machine.

This guide is the practical tour: **what each capability does, the problem it
solves, and a real-world example you can run.** For setup see
[`deployment.md`](deployment.md); for the data model see [`schema.md`](schema.md);
for the deeper design of individual agents see the specs linked from
[`README.md`](README.md). A compact [API & CLI reference](#reference) is at the end.

## How it fits together (30 seconds)

```
iOS Shortcuts ─┐                         ┌─ ntfy / Pushover / Twilio
   Scriptable  ├─► n8n (every 1–15 min) ─►│   (notifications, calls)
     widget   ─┘     │  polls + delivers  └─ Ollama (local LLM, optional)
                     ▼
              Prefrontal API (FastAPI, :8000)  ──►  SQLite (your data, on-box)
```

- **Prefrontal** is the brain: a FastAPI app over a SQLite database. Nothing
  leaves the machine unless you wire an outbound step.
- **n8n** is the muscle: it polls Prefrontal on a schedule and delivers nudges
  (ntfy pushes by default, Pushover optional, Twilio calls for the top of the
  ladder). Workflows live in [`../deploy/n8n/`](../deploy/n8n).
- **Ollama** is optional local inference for nicer phrasing and triage; every
  feature has a deterministic fallback when it's down.
- You interact via **iOS Shortcuts**, the **dashboard**, a **home-screen
  widget**, or the **CLI**.

Examples below assume two shell variables:

```bash
PF=http://localhost:8000          # or http://agent-1.tail8b0a.ts.net:8000 over Tailscale
TOK=your-X-Prefrontal-Token       # see deployment.md / "Users & access"
```

## Contents

- **Getting out the door** — [Outings](#outings-the-coffee-shop-nudge) ·
  [Departure reminders](#departure-reminders) · [Places](#places)
- **Your schedule** — [Calendar sync](#calendar-sync) ·
  [Conflicts](#conflicts-hard-and-possible)
- **Getting things done** — [Todos](#todos) (augmentation · decomposition ·
  time-fitting · avoidance)
- **Staying on task** — [Focus sessions](#focus-sessions)
- **Daily rhythm** — [Morning briefing](#morning-briefing)
- **Knowing yourself** — [Profile & learning](#behavioral-profile-and-learning)
- **Inbox** — [Mail ingestion](#mail-ingestion-and-triage)
- **Surfaces** — [Dashboard, family, kids, insights & widget](#surfaces-dashboard-family-kids-insights-and-widget)
- **Operating it** — [Users & access](#users-and-access) · [Reference](#reference)

---

## Getting out the door

### Outings (the "coffee shop nudge")

**What:** You declare a short errand and a time window; Prefrontal escalates —
soft push at 50% of the window, firm push at 100%, a phone call at 150% — to
pull you back before the errand quietly stretches.

**Solves:** Losing track of time on "quick" errands.

**How:** An iOS "Going out" Shortcut posts to `/webhooks/outing/start`; n8n polls
`/webhooks/outing/check` every minute and delivers the nudge. "I'm back" closes
it (or coming home does, if you feed location).

```bash
# Leave — the window is parsed from the text, or inferred if you don't say one
curl -s -XPOST $PF/webhooks/outing/start -H "X-Prefrontal-Token: $TOK" \
  -H 'Content-Type: application/json' \
  -d '{"intention":"Getting coffee, back in 15 minutes"}'

# Come back (logs stated-vs-actual for learning)
curl -s -XPOST $PF/webhooks/outing/return -H "X-Prefrontal-Token: $TOK" \
  -H 'Content-Type: application/json' -d '{"status":"returned"}'
```

> Real run: you say "back in 15 min." At ~7 min: *"still on track?"* At 15 min:
> *"time to wrap up."* At ~22 min, a Twilio call. Come home early and it closes
> itself (no call) if location is wired.

### Departure reminders

**What:** For calendar events with a location, Prefrontal estimates travel time
from where you are now and nudges you when it's time to leave (heads-up → soon →
go).

**Solves:** Time blindness around *leaving* — losing 10 minutes and being late.

**How:** n8n polls `/webhooks/departure/check` every few minutes (optionally with
your current location); the workflow escalates the push as leave-by approaches.

> "Dentist at 3:00, 5 km away" → with your learned 1.4× bias and a road factor,
> leave-by ≈ 2:32. You get a heads-up at 2:02, "get ready" at 2:22, and a
> high-priority **"head out now"** at 2:32.

### Places

**What:** A small table of named locations ("gym", "office", "dentist") → coords,
so travel-time estimates work **offline**, without a geocoding API.

**Solves:** Calendar locations are free text; Places turns them into coordinates
instantly.

```bash
curl -s -XPOST $PF/places -H "X-Prefrontal-Token: $TOK" \
  -H 'Content-Type: application/json' \
  -d '{"name":"gym","lat":37.7749,"lon":-122.4194}'
```

Calendar sync matches place names in event titles/locations first, then a local
cache, then optional [Nominatim](https://nominatim.org/) (off by default).

---

## Your schedule

### Calendar sync

**What:** Aggregates several calendars (personal Google, work, Outlook, family)
into one `commitments` view, every 15 minutes, with geocoding and conflict
detection.

**Solves:** Calendars scattered across platforms; double-bookings hiding in the
noise.

**How:** The [`calendar-sync`](../deploy/n8n/calendar-sync.workflow.json) n8n
workflow fetches each calendar's secret **ICS** URL, parses + dedupes them, and
POSTs the batch to `/webhooks/calendar/sync`. Events you've RSVP'd *no* to are
dropped; the same event appearing on two calendars is de-duplicated by UID.

```bash
curl -s "$PF/commitments" -H "X-Prefrontal-Token: $TOK"   # upcoming, soonest first
```

Each event is classified **self** (you attend — can conflict) vs **fyi** (just
informational). Fix a misclassification with
`POST /commitments/{id}/kind {"kind":"fyi"}`.

### Conflicts (hard and possible)

**What:** Overlaps between two *real* events are **hard** double-bookings.
Overlaps where one side is a placeholder (`Busy`, `Block`, `Hold`, `OOO`) are
**possible** conflicts — surfaced softly and **dismissable**.

**Solves:** Not seeing a clash until you're in it — without crying wolf over the
"Block" you put on your own calendar.

```bash
curl -s "$PF/commitments/conflicts" -H "X-Prefrontal-Token: $TOK"
# → { "conflicts": [...], "possible_conflicts": [{..., "key": "..."}] }

# Wave one off (sticks across re-syncs; reappears if an event moves/retitles)
curl -s -XPOST $PF/commitments/conflicts/dismiss -H "X-Prefrontal-Token: $TOK" \
  -H 'Content-Type: application/json' -d '{"key":"<key from above>"}'
```

A new conflict pushes once; a *standing* one won't re-alert every 15 minutes.

---

## Todos

Todos are "open loops" — things to do that aren't pinned to a clock. Prefrontal's
job is to make them **schedulable**, **startable**, and **honestly prioritized**.

### Augmentation — make a bare todo usable

**What:** When you add a todo, Prefrontal infers whatever you didn't supply —
**estimate, priority, energy, deadline** — via the local model, falling back to
keyword heuristics.

**Solves:** A todo with no estimate is invisible to the scheduler. "Call the
dentist" should just work.

```bash
curl -s -XPOST $PF/todos -H "X-Prefrontal-Token: $TOK" \
  -H 'Content-Type: application/json' -d '{"title":"Call the dentist tomorrow"}'
# → estimate 10m, priority normal, energy low, deadline = tomorrow,
#   "augmented": {"estimate_minutes":"llm", "deadline":"heuristic", ...}
```

Anything you *do* pass is kept as-is; the `augmented` map shows where each value
came from. (CLI: `prefrontal todo add "Call the dentist" --minutes 10`.)

### Decomposition — kill the activation wall

**What:** A big todo gets one concrete **≤5-minute first step** ("open the doc,
write three bullet headers"), with the remaining steps tucked behind a toggle.

**Solves:** *Starting* is the hard part. One obvious first move beats staring at
"write the report."

```bash
# Auto for todos over ~30 min; or on demand any time:
curl -s -XPOST $PF/todos/42/decompose -H "X-Prefrontal-Token: $TOK"
# → {"first_step":"Open a doc and write the five section titles",
#    "first_step_minutes":4, "steps":["Draft the summary", ...]}
```

Tick the first step (`POST /todos/42/steps/0/done`) and the next surfaces.

### Time-fitting — "I have 20 minutes, what fits?"

**What:** Ranks the open todos that genuinely fit a free block, applying your
learned time bias so a "10-minute" task is judged at its real length.

**Solves:** Free time is invisible; todos pile up unscheduled.

```bash
curl -s "$PF/todos/fit?minutes=20" -H "X-Prefrontal-Token: $TOK"   # CLI: prefrontal fit 20
```

The morning briefing does this proactively — one suggestion per open window.

### Avoidance — surface what you keep skipping

**What:** Flags important todos you keep *not* doing, scored by age + priority +
smallness + deadline pressure. Low-priority "someday" items are exempt.

**Solves:** Self-assigned priority is gameable; you avoid the important-but-
unpleasant thing. This counteracts the pull toward the fun/shiny task.

```bash
curl -s "$PF/todos/avoided" -H "X-Prefrontal-Token: $TOK"
# → "Call the insurance company" — open 8 days, high priority
```

It shows as a red `avoiding 8d` badge on the dashboard and a **"You keep putting
off…"** line in the briefing.

---

## Staying on task

### Focus sessions

**What:** Declare a deep-work block; Prefrontal **protects aligned focus**
(suppresses other nudges) but **interrupts** past a hard ceiling (default 180
min) for a break.

**Solves:** Hyperfocus is a superpower when aligned and a cost when it isn't —
the system protects the good kind and breaks the runaway kind.

```bash
curl -s -XPOST $PF/webhooks/focus/start -H "X-Prefrontal-Token: $TOK" \
  -H 'Content-Type: application/json' \
  -d '{"intended_task":"Write the Q3 report","planned_minutes":120}'
# n8n polls /webhooks/focus/check; close with /webhooks/focus/end
```

> Within the planned window: protected, no interruptions. Past it: a gentle
> check-in. Past the hard ceiling: *"time for a break"* — and protection lifts so
> your departure/outing nudges can fire again.

---

## Daily rhythm

### Morning briefing

**What:** A 7am digest: today's commitments, double-bookings, what slipped this
week, what you're avoiding, spare-time slots with todo suggestions, and a
coaching note (your time bias).

**Solves:** Starting the day without a picture of what's at risk and where you
have slack.

```bash
curl -s "$PF/briefing" -H "X-Prefrontal-Token: $TOK" | python3 -c 'import sys,json;print(json.load(sys.stdin)["text"])'
# CLI: prefrontal briefing   (add --llm for Ollama prose)
```

Delivered each morning by the [`morning-briefing`](../deploy/n8n/morning-briefing.workflow.json)
workflow via ntfy.

---

## Knowing yourself

### Behavioral profile and learning

**What:** Every outcome (made it / missed / partial) is logged as an *episode*. A
nightly pass derives **patterns** — chiefly your **time-estimation bias** (how
much you underestimate) — and a summarizer turns them into a coaching profile.

**Solves:** You can't see your own patterns. The system measures them and feeds
them back into every estimate.

```bash
prefrontal learn        # recompute patterns from episodes (nightly via launchd)
prefrontal summarize    # regenerate the prose profile (Ollama, heuristic fallback)
curl -s "$PF/profile" -H "X-Prefrontal-Token: $TOK"        # ?format=structured for the raw values
```

> After a dozen outings that ran long, your bias settles around 1.4× — so the
> scheduler treats a "30-min" task as 42 min, and departure reminders pad travel
> time automatically. Reset to 1.0 anytime to start learning fresh.

---

## Inbox

### Mail ingestion and triage

**What:** Pulls your inboxes, triages each message (needs action? urgency?
category?) locally, and turns real asks into todos. Newsletters and noise are
logged but not surfaced.

**Solves:** Urgent requests buried under volume across multiple inboxes.

**How:** Either an n8n Gmail node POSTs to `/webhooks/mail/sync`, or the built-in
IMAP fetcher runs on a schedule. Triage uses the local model with a keyword
fallback — **message bodies never leave the machine** (and `signals`-policy
accounts store only subject + sender).

```bash
prefrontal mail fetch --account personal     # pull unread over IMAP, triage, make todos
prefrontal mail list                          # recent mail + action items
curl -s "$PF/mail" -H "X-Prefrontal-Token: $TOK"
```

> "Can you review the proposal?" → todo *"Reply to Sarah re: proposal"* (priority
> high). A Substack digest → logged, not surfaced.

### Triage learns from what you drop

**What:** When you **Drop** a todo that mail intake created, that's a hint triage
over-flagged. Those corrections are folded back into the triage prompt, so it
stops re-flagging the same kind of mail — it adapts to *your* inbox over time.

**The catch it avoids:** a Drop is ambiguous — it can mean "this never needed
action" *or* "I avoided something I should have done" (what [Avoidance](#avoidance--surface-what-you-keep-skipping)
surfaces). Feeding every drop to the prompt would teach it to suppress
important-but-avoided mail. So a drop only counts as a correction when it's
reliable: **quick** (dropped within `PREFRONTAL_TRIAGE_QUICK_DROP_DAYS`, before
it could be avoided) or from a **repeat sender** (dropped
`PREFRONTAL_TRIAGE_REPEAT_THRESHOLD`+ times). A one-off slow drop is recorded but
never injected.

```bash
prefrontal mail learned            # repeat senders, recent drops, the exact prompt addendum
prefrontal mail learned --clear    # forget everything triage has learned
curl -s "$PF/mail/triage/learned" -H "X-Prefrontal-Token: $TOK"
curl -sX POST "$PF/mail/triage/learned/forget" -H "X-Prefrontal-Token: $TOK" -d '{"id":3}'
```

> Drop three "Daily standup notes" emails from a bot in a row → the next sync's
> triage prompt learns that sender's mail doesn't need action, and stops making
> todos for it. Wrongly attributed? `forget` that one row and it's gone.

---

## Assistant — natural-language edits

The dashboard chat box turns a plain-English message into concrete edits to your
own data. Say "reschedule the standup to 3pm, mark the dentist call done, and
lower the taxes todo to normal" and it resolves each reference to a real item and
proposes the matching actions. Two steps, on purpose:

- **`POST /assistant`** interprets the message against a snapshot of your open
  todos, upcoming commitments, dismissable conflicts, and — when you're in a
  household — the shared sheet, and returns a *proposed* action list — **nothing
  is written**. Supported ops cover todos (add / complete / drop / set priority /
  set estimate / rename / set deadline), commitments (add / cancel), dismissing a
  possible conflict, and the shared household sheet: facts, agreements, and the
  shopping list (add an item / check it off / remove it). So "add eggs, milk, and
  coffee to the shopping list" becomes three add actions you review and apply.
- **`POST /assistant/apply`** executes actions you confirm. They're re-validated
  against your current data first, so an item that moved since it was proposed is
  skipped rather than mis-edited.

Two guardrails make this safe: a **strict op whitelist** (the assistant can't run
anything outside the list above) and **id resolution from your own snapshot** (it
can only touch items that actually exist in your scoped data — never a
hallucinated or someone else's row). Parsing uses **Claude** when an
`ANTHROPIC_API_KEY` is set, otherwise the local Ollama model (local-first by
default); the response says which `provider` answered.

## Surfaces: dashboard, family, kids, insights, and widget

- **`GET /dashboard`** — your full read-and-act monitoring page: active outings
  (with escalation level), todos (with first-steps and avoidance badges),
  commitments + conflicts, the briefing, and your profile. Add/complete todos,
  close outings, dismiss conflicts. It also has an **assistant chat box**: type
  a plain-English ask ("bump the dentist call to urgent and drop the dry-cleaning
  todo") and it proposes the concrete edits, which you review and one-tap Apply.
  Open it over Tailscale; it asks for your token once and remembers it.
- **`GET /family`** — a calm, mostly read-only *shared household* glance for
  everyone in the household: kids & facts, standing plans + star progress, the
  shopping list, upcoming appointments, recent changes, and (if enabled) the
  load-balance view. The one editable spot is the **shopping list** — a quick-add
  box and tap-to-check-off, since that's the inherently collaborative list; all
  other editing lives on `/kids`.
- **`GET /kids`** — the editable face of the same shared household sheet: add
  facts, agreements, children and appointments, award stars, and drive it all
  from a plain-English assistant box.
- **`GET /stats`** — the behavioral **Insights** page: charts over your learning
  data — time-estimation accuracy (the bias multiplier, overall and per context),
  follow-through (success rate, current streak, the success/partial/miss split
  and a recent sparkline), and channel responsiveness (which nudge channels you
  actually answer). Drawn with inline SVG/CSS, no external libraries.
- **Scriptable widget** ([`../deploy/scriptable/`](../deploy/scriptable)) — a
  home-screen glance: outing status dot, next commitments, conflict/todo counts.
  Tapping opens the family view.

All of these share one light/dark theme and a common top nav, are reached over
Tailscale (e.g. `http://agent-1.tail8b0a.ts.net:8000/dashboard`), and
authenticate with your `X-Prefrontal-Token` (or a Google sign-in session).

---

## Users and access

Prefrontal is multi-tenant: one database, every row scoped to a user, each user
with their own token. Requests carry `X-Prefrontal-Token`; the matching user's
data (and notification targets) are resolved automatically.

```bash
prefrontal user add tom --display-name Tom --operator   # prints the token ONCE
prefrontal user list
prefrontal user rotate tom                               # mint a new token (old one dies)
```

- **Single-user / trusted LAN:** set `PREFRONTAL_DEFAULT_USER=tom` and skip tokens.
- **Operators** (`--operator`) can manage users over HTTP via `/admin/users`.
- Migrating an old single-user DB: `prefrontal migrate-multi-tenant`.

### Signing in: Google (browser) vs. tokens (machines)

Two ways to authenticate, used by different clients:

- **Web pages** (`/dashboard`, `/family`, `/kids`, `/stats`) — **Sign in with Google**. You click the
  button, approve Google's consent once, and a signed session cookie carries you
  after that (no token to paste). Only emails in the `GOOGLE_OAUTH_ALLOWED`
  allowlist may sign in, each mapped to a user handle.
- **Automations** (n8n, iOS Shortcuts, the widget) — the per-user
  `X-Prefrontal-Token` header, unchanged. They can't do an interactive login, so
  tokens stay for them. `resolve_user` accepts **either** a session cookie or a token.

Google sign-in is optional and off until configured. To enable it:

1. Serve the app over **HTTPS** (Google requires it): on the host,
   `tailscale serve --bg 8000` → `https://<your-tailnet-name>` (the CLI lives at
   `/Applications/Tailscale.app/Contents/MacOS/Tailscale` on macOS — alias it,
   don't symlink it). The config persists across reboots.
2. **Google Cloud Console** → Credentials → OAuth client ID (*Web application*).
   Redirect URI `https://<your-tailnet-name>/auth/google/callback`; add yourself
   as a test user on the consent screen.
3. Set `GOOGLE_OAUTH_CLIENT_ID/SECRET`, `OAUTH_BASE_URL`, `SESSION_SECRET`
   (`openssl rand -hex 32`), and `GOOGLE_OAUTH_ALLOWED="you@gmail.com=yourhandle"`
   in `.env`, then restart. The handle must match an existing user (case-sensitive).

Full design: [`multi-tenant.md`](multi-tenant.md).

---

## Reference

Every endpoint requires the `X-Prefrontal-Token` header except `/health`,
`/dashboard`, and `/family` (the page shells carry no data and prompt for the
token client-side).

### Endpoints

| Method & path | Purpose |
|---|---|
| `GET /health` | Liveness probe (no auth) |
| `GET /profile` | Behavioral profile; `?format=structured`, `?refresh=1` |
| `POST /webhooks/shortcut` | Log a one-tap outcome (made_it / missed_it / partial / log) |
| `POST /webhooks/n8n` | Receive an inbound event from n8n |
| `POST /webhooks/location` · `GET /location` | Record / read last-known position |
| `POST /webhooks/outing/start` | Declare an outing (intention + window) |
| `POST /webhooks/outing/check` | Poll for due nudges (fires escalation) |
| `POST /webhooks/outing/return` | Close an outing; logs stated-vs-actual |
| `GET /outings` | Read-only outings snapshot (no side effects) |
| `POST /webhooks/focus/start` · `/check` · `/end` | Focus session lifecycle |
| `GET /focus` | Read-only focus snapshot |
| `POST /webhooks/calendar/sync` | Sync a batch of calendar events |
| `POST /webhooks/departure/check` | Poll for a due departure nudge |
| `GET /commitments` · `POST /commitments` | List / add a commitment |
| `POST /commitments/geocode` | Resolve missing coordinates |
| `GET /commitments/conflicts` | Hard + possible conflicts |
| `POST /commitments/conflicts/dismiss` | Dismiss a possible conflict (by `key`) |
| `POST /commitments/{id}/kind` | Set `self` vs `fyi` |
| `POST /todos` · `GET /todos` | Add (auto-augmented) / list (with decomposition + avoidance) |
| `GET /todos/fit?minutes=N` | Todos that fit a free block |
| `GET /todos/now` | The one todo that fits your free time right now (avoidance-biased, low-energy-later) |
| `GET /todos/avoided` | Important todos you keep skipping |
| `POST /todos/{id}/decompose` | First step + remaining steps |
| `POST /todos/{id}/steps/{i}/done` | Tick a decomposed step |
| `POST /todos/{id}/deadline` | Move or clear a deadline |
| `POST /todos/{id}/done` · `/drop` | Complete / drop (logs an episode) |
| `POST /assistant` | Interpret a natural-language ask into proposed edits (no writes) |
| `POST /assistant/apply` | Execute previously-proposed edits (re-validated) |
| `GET /briefing` | Today's digest (structured + rendered text) |
| `GET /panic` · `POST /webhooks/panic/check` | Overwhelm triage: one-tap headline / poll for a proactive nudge |
| `POST /webhooks/coach/check` · `/ack` | Run the coaching tick / acknowledge a nudge |
| `GET /encouragement` · `POST /encouragement/sent` | Rough-day recovery message / stamp it delivered (once-a-day cursor) |
| `POST /webhooks/impulse/capture` · `/webhooks/focus/switch` · `/resolve` | Capture a deferred impulse / log & resolve a context switch |
| `GET /nudge/act` · `/nudge/dismiss` · `GET /nudges` | One-tap action buttons / dismiss / recent-nudge log |
| `POST /places` · `GET /places` | Curated location aliases |
| `POST /webhooks/mail/sync` · `GET /mail` | Ingest mail / recent + action items |
| `GET /mail/triage/learned` · `/forget` · `/clear` | Learned triage corrections (repeat/quick-drop senders) |
| `GET /household/sheet` · `POST /household/{create,facts,agreements,shopping,balance,checkin,digest,invites}` | Shared co-parent sheet — facts, agreements/star charts, shopping, load-balance, check-in, digest, invites |
| `POST /webhooks/household/{star-prompts,checkin,digest}/check` | Scheduled household sweeps (star award prompts, weekly check-in, daily delta digest) |
| `GET /dashboard` · `/family` · `/kids` · `/stats` | Web surfaces — dashboard, partner glance, editable household sheet, behavioral insights (no auth on the shell) |
| `GET /stats/data` | Aggregated behavioral insights for the /stats charts |
| `GET /auth/google/login` · `/callback` | Google sign-in (browser); 404 until configured |
| `POST /auth/logout` | Clear the browser session cookie |
| `POST /admin/users` · `GET /admin/users` | Provision / list users (operator only) |
| `POST /admin/users/{handle}/rotate` · `/disable` | Rotate token / disable user |

### CLI (`prefrontal <command>`)

| Command | Purpose |
|---|---|
| `init-db` | Create the SQLite database (structure only; users seed on `user add`) |
| `serve` | Run the API (uvicorn); `--host`, `--port` |
| `user add\|list\|rotate\|disable` | Manage users & tokens |
| `migrate-multi-tenant` | Upgrade a single-user DB in place |
| `learn` | Recompute patterns from episodes; `--user`, `--all-users` |
| `summarize` | LLM profile narrative → cache + file; `--all-users`, `--no-fallback` |
| `profile` | Print the behavioral profile |
| `briefing` | Print the morning digest; `--llm` for prose |
| `todo add\|list\|done\|drop` | Manage todos (add auto-augments) |
| `fit <minutes>` | Todos that fit a free block now |
| `mail list\|sync\|fetch` | View / ingest / IMAP-pull mail; `--account`, `--heuristic` |
| `coach [--dry-run] [--deliver]` | Run the coaching tick — what's due, on which channel |
| `encourage` | Rough-day check: today's recovery message if it's gone sideways |
| `panic` | Overwhelm triage — what's on fire + one first step; `--llm` |
| `crunch on\|off\|status` | Deadline mode: suspend the work/life time bands; `--hours N` |
| `note "…"` / `proposals list\|accept\|reject` | LLM-as-sensor: jot a note → review proposed updates |
| `household add\|join\|leave\|show\|invite\|redeem\|star\|balance\|shopping\|prompt-check\|checkin-check\|digest-check` | Co-parent household sheet |
| `modules [-v]` | List challenge-area modules and status |

Data commands (`learn`, `summarize`, `profile`, `briefing`, `todo`, `fit`,
`mail`, `coach`, `encourage`, `panic`, `crunch`, `note`/`proposals`, `household`)
take `--user HANDLE` (or `--all-users`); with a single user they resolve
automatically.

### Key configuration

Set in `.env` (see [`deployment.md`](deployment.md) for the full list):

| Variable | Default | Meaning |
|---|---|---|
| `PREFRONTAL_DB_PATH` | `prefrontal.db` | SQLite file |
| `PREFRONTAL_PORT` | `8000` | API port |
| `PREFRONTAL_DEFAULT_USER` | _(empty)_ | Tokenless requests resolve to this user (single-user mode) |
| `PREFRONTAL_MODULES` | _(all)_ | Comma-separated modules to enable |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `http://localhost:11434` / `llama3.1:8b` | Local inference (tip: set `127.0.0.1` explicitly to skip IPv6 `localhost` resolution) |
| `PREFRONTAL_MAIL_ACCOUNTS` | _(empty)_ | `account=full\|signals` retention pairs |
| `PREFRONTAL_ACCOUNT_LABELS` | _(empty)_ | `account=label:color` dashboard pills (e.g. `work=Vistar:orange`) |
| `PREFRONTAL_CALENDAR_LABELS` | _(empty)_ | `feed=label:color` calendar pills (e.g. `personal=Personal:blue`) |
| `GEOCODER_URL` | Nominatim | Geocoding endpoint (opt-in) |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | _(empty)_ | Google sign-in client (off until both set) |
| `OAUTH_BASE_URL` | _(empty)_ | Public https origin for the OAuth redirect |
| `SESSION_SECRET` | _(empty)_ | Signs the browser session cookie (`openssl rand -hex 32`) |
| `GOOGLE_OAUTH_ALLOWED` | _(empty)_ | `email=handle,…` allowlist of who may sign in |
