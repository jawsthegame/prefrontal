# Prefrontal

A self-hosted executive function assistant that learns from your behavior to help you manage time, attention, and task switching.

Prefrontal runs on your local network — always on, always watching, never sending your data anywhere you haven't decided it should go.

---

## What it does

Most productivity tools assume you'll remember to use them. Prefrontal doesn't.

It monitors your context (location, calendar, time of day, recent activity), surfaces reminders at the right moment through the right channel, and — critically — learns from whether those reminders actually worked. Over time it builds a behavioral model of how *you* operate: when you go quiet, when you underestimate time, which notification channel you ignore after 3pm.

It gets better the longer you use it.

---

## Core capabilities

**Departure reminders**
Knows where you need to be and when, factors in real-time traffic, and escalates if you don't respond to the first nudge. Learns your actual departure lag over time.

**Mail monitoring**
Aggregates personal email accounts into a normalized stream, triages by urgency, and surfaces what needs action — without you having to open every inbox.

**Morning briefing**
A daily digest calibrated to your preferences: what needs attention today, what went cold this week, what's on the calendar. When the encouragement layer is on, it closes with a sentence or two tuned to the shape of the day — steady reassurance on a packed or recently-rough stretch ("you've got this; space things out"), and on a wide-open day a relax-vs-accomplish choice that shapes the plan around your answer.

**Panic mode**
For the moments you're too overwhelmed to think. On demand, it cuts through everything and answers three questions: what's *actually* on fire right now (calendar, todos, and mail ranked into "already behind" / "bearing down soon" / "piling up", each tagged work vs. home), what you can safely ignore, and — the important part — one concrete, tiny first step to break the freeze. Not the whole plan. Just where to put your hands in the next five minutes.

**Encouragement & recovery**
The counterweight to a system whose job is nudging. When a day genuinely goes rough — a missed hard commitment, a pile of misses — it stops nudging and shifts to reassurance: acknowledges the rough day without judgment, then hands back a concrete plan (what still fits, what's safe to move, and one tiny next step). Opt-in, tone-calibrated, and capped at once a day so it never becomes a pile-on.

**Self-care checks**
The nudges that are *supposed* to interrupt. In deep focus you forget to eat or drink — so from mid-morning it asks "have you eaten?", and through the day nudges you to drink water toward a daily target, deliberately overriding the protect-the-flow stance that silences everything else. One-tap **Ate** / **Drank** / **Snooze** action buttons on the native push, respects your responsive hours, and each check goes quiet once you've hit its target for the day. Opt-in. At day's end it can read those taps back as a *timeline* and name the gaps a raw tally hides — you drank all six glasses, but your first wasn't until 3pm; six hours passed between two bio breaks — as an opt-in evening recap (and on demand via `prefrontal self-care review` / `GET /self-care/review`).

**Blocked-on-me tracker**
The mirror image of a todo: capture, in one line, when someone *else* is blocked
until you do a thing ("Sam's waiting on the budget numbers"). It's not busywork —
it feeds prioritization directly. Panic mode ranks who's waiting alongside your
own fires (a person past-due on you edges out your own overdue todo), and the
morning briefing leads with **🙋 Waiting on you**, longest wait first. The
counterweight to shiny-object syndrome: before you chase the new thing, you see
whose day is stalled until you move.

**Behavioral memory**
Logs outcomes — did you leave on time, did you complete the task, did you respond to the reminder — and uses that data to improve predictions and timing over time.

**Low-friction capture**
The native iOS app's App Intents (Siri / Action Button / widgets) give one-tap logging with nothing to paste; iOS Shortcuts remain a free-signing fallback. The system meets you where you are rather than requiring you to go somewhere.

---

## Challenge-area modules

ADHD presents differently for everyone, so Prefrontal's support behaviors are organized into
independently enableable **modules** — mostly one per executive-function challenge, plus a
self-care check for the basic needs a focus state drops — rather than a single fixed
assistant. Enable only the ones that match your profile (an empty config enables them all):

| Module | `key` | Addresses |
|---|---|---|
| Time Blindness | `time_blindness` | Duration estimation, departure timing, elapsed-time awareness, an evening "early start tomorrow — set an alarm" heads-up (one-tap into an iOS alarm Shortcut; its cutoff self-tunes from your late mornings) |
| Task Paralysis | `task_paralysis` | Task initiation / activation energy (tiny first steps, decomposition, honing ambiguous items so they can be started) |
| Hyperfocus | `hyperfocus` | Protects *good* hyperfocus, interrupts *misdirected* hyperfocus |
| Impulsivity | `impulsivity` | A reflective pause and capture-then-defer before impulsive switches |
| Location-Aware Task Anchor | `location_anchor` | Escalating nudges (push → push → Twilio call) back to a stated intention as its time window elapses |
| Implementation Intentions | `implementation_intention` | Pre-committed *if-then* plans (a cue — a place, a time-of-day band, and/or a home-crossing event like arrive/leave — paired with a tiny pre-decided action) surfaced the moment their cue is detected, offloading task initiation onto the cue. Capture in one utterance ("when I get home, take out the recycling"; "at my desk after lunch, open the tax form"); forgiving (no streak/guilt) |
| Closed-Loop Trip Tracking | `trip_tracking` | Passively logs undeclared round trips (leave home → return), then asks for a label, category, life-domain, and an honest plain-English "how it went" that feeds the learning engine. Rolls out-of-home time up by life-sphere (shop/work/home/kids/personal) into a **focus-balance** read, with an opt-in "light on kids/personal this week" nudge |
| Self-Care | `self_care` | Basic-needs checks that pierce flow — "have you eaten?" (once/day), "drink some water" (to a daily target), an opt-in "taken your meds?" (`meds_enabled`), and an opt-in daily-movement floor — "time to move / stretch" (`movement_enabled`) — re-asked until met (opt-in via the `self_care` key). Plus an opt-in **end-of-day gap review** (`self_care_review_enabled`) that reads the day's clicks back as a timeline and names the gaps a tally hides (a late first glass, a long stretch between bio breaks) |
| Emotion Regulation | `emotion_regulation` | The *feeling* side of a hard moment (emotional dysregulation is a core, large-effect part of ADHD). On demand — one tap or a few words (`POST /emotion/support`) — it offers **one** brief, evidence-matched micro-skill (ACT acceptance, a DBT distress-tolerance move like paced breathing / grounding / a cold reset, or self-compassion framing) fitted to the feeling. General-wellness support, not therapy: crisis language is screened first and answered with resources, never a coping skill. Optional acceptance line folded into the rough-day recovery (`emotion_recovery_acceptance`) |

Each module owns its coaching-state defaults, contributes a section to the behavioral
profile, and declares the interventions it provides. Select a subset with
`PREFRONTAL_MODULES` (see `.env.example`), and list status with `prefrontal modules -v`.
Adding your own is a small subclass — see [`CONTRIBUTING.md`](CONTRIBUTING.md).

New here? Every enabled module has a short, plain-English walkthrough — what it
helps with and how it'll show up — in the in-app **Guide** (`/guide`, in the nav),
re-readable any time. The same content is available offline via `prefrontal
modules --tutorial`.

---

## Architecture

Prefrontal is a system of small, focused agents rather than a single monolithic assistant. Each agent does one thing well and passes context to the next.

```
Native iOS app (App Intents / geofence / push) · Shortcuts (fallback) · Webhooks
        ↓
  Ingestion Layer       ← location triggers, mail polling, calendar sync
        ↓
  Triage Agent          ← classifies, prioritizes, routes
        ↓
  Memory Layer          ← SQLite: episodes, patterns, coaching state
        ↓
  Coaching Agent        ← generates briefings, reminders, check-ins
        ↓
  Delivery Layer        ← native APNs push (one-tap action buttons) / Twilio voice call / TTS
                          (ntfy is a dev-only shim for free-signing builds)
```

The memory layer is the core. Everything the system learns about your behavior lives in a local SQLite database. A summarizer agent periodically compresses patterns into a profile document that gets injected into every agent's system prompt — so behavioral context travels with every interaction.

See [`docs/schema.md`](docs/schema.md) for the full database schema.

---

## What's in this repo today

Prefrontal is in active development — multi-tenant (every row scoped per user; see
[`docs/multi-tenant.md`](docs/multi-tenant.md)) and running in daily use. What's implemented:

| Layer | Module | Status |
|---|---|---|
| Memory layer (SQLite) | `prefrontal/memory/` | ✅ Implemented — episodes, patterns, coaching state + feature tables |
| Learning pass (episodes → patterns) | `prefrontal/memory/patterns.py` | ✅ Implemented — `prefrontal learn` derives patterns + bias, and tunes the early-start alarm cutoff from late morning departures |
| Schedule / calendar ingestion | `prefrontal/commitments.py` | ✅ Calendar sync (private ICS feeds, incl. Google/Outlook secret-address URLs) + cross-feed dedup + double-booking detection |
| Commitment geocoding | `prefrontal/geocode.py` | ✅ Places aliases → cache → opt-in Nominatim, for travel-time estimates |
| Impact analysis | `prefrontal/impact.py` | ✅ Predicts at-risk commitments when running behind; surfaced in the nudge |
| Morning briefing | `prefrontal/briefing.py` | ✅ Daily digest (today, conflicts, slips, coaching note); closes with a day-shaped encouragement line when the layer's on (packed/rough reassurance, open-day relax-vs-accomplish choice); `prefrontal briefing` |
| Panic mode | `prefrontal/panic.py` | ✅ Overwhelm triage — ranks live pressures (calendar/todos/mail) + one first step. On-demand (`prefrontal panic`, `GET /panic`, dashboard/family button, one tap from the phone) **and** proactive (`POST /webhooks/panic/check` — nudges when the plate tips into overwhelm) |
| Coaching agent | `prefrontal/coaching.py` | ✅ Tick engine — fans over every module's `evaluate()`, picks channel (urgency floor → learned bump), suppresses on quiet hours + debounce; `prefrontal coach`, `POST /webhooks/coach/check` |
| Vacation mode | `prefrontal/vacation.py` | ✅ Ease off the nudges while away — a *receptivity profile*, not a kill switch: holds every discretionary cue (as quiet hours does) while letting `critical` calendar obligations and on-demand surfaces (panic, emotion) through. **Manual** entry (`prefrontal vacation on\|off\|status`, `GET`/`POST /vacation`) is the escape hatch; returning inside the home radius **auto-resumes** it via the trip state machine (so a forgotten toggle can't leave you muted). See [`docs/design/vacation-mode.md`](docs/design/vacation-mode.md) |
| Encouragement & recovery | `prefrontal/encouragement.py` | ✅ Rough-day tone shift — scores today's signals, builds a recovery plan (re-fit / defer / one small step); opt-in, once/day. Also woven into the morning brief as a day-shaped closing line (`briefing_note`). `prefrontal encourage` / `open-day`, `GET /encouragement`, `POST /briefing/open-day` |
| Freeform calendar assistant | `prefrontal/availability.py` · `webhooks/routers/assistant.py` | ✅ "Find me a time" from free text: parses duration + timeframe + who's-involved (LLM with an offline-heuristic fallback), asks one clarifying question when the ask is too vague, then finds open slots over `find_slots`. **Participant-aware** — a partner's FYI events ("where someone else will be") block only when the plan involves them, so "just me" ignores items that are only your wife's. `POST /assistant/find-time`, `prefrontal find-time "…"` |
| Todos + time-fitting | `prefrontal/scheduling.py` | ✅ Open loops fitted into free windows; `prefrontal todo` / `fit`, woven into the briefing. Honest prioritization: surfaces the important todo you keep skipping, pins in-progress todos to the top, and flags when you're mid-task on something less important than what you're avoiding (`focus_conflict`) |
| Todo decomposition | `prefrontal/todos.py` | ✅ Breaks a stall-prone todo into a tiny first step + remaining steps |
| Blockers (who's waiting on you) | `prefrontal/blockers.py` · `webhooks/routers/blockers.py` | ✅ Capture when someone else is blocked on you (one line / `POST /blockers` / NL `add_blocker` / dashboard **Waiting on you** card / iOS **Today** card); feeds prioritization — panic buckets it above the equivalent todo, the briefing leads with "🙋 Waiting on you". Resolved not deleted; `prefrontal blocked add/list/resolve/reopen` |
| Ambiguity clarification | `prefrontal/clarify.py` · `webhooks/routers/clarify.py` | ✅ A vague todo/commitment ("Tax", "Mom") that stalls because it can't be *named* gets one inline clarifying question in the dashboard (candidate readings, LLM-phrased with a heuristic fallback); answering hones it in, and a reading that maps to a recognized task type (e.g. tax filing) opens a step-by-step guided overlay. A Task-Paralysis initiation lever — the detection sweep runs on the coaching tick (`sweep_ambiguous_items`), with `POST /clarifications/check` as the on-demand twin, plus `GET /clarifications`, resolve/dismiss, and a `prefrontal clarify check/list/resolve/dismiss/guide/localize` CLI. Guides for the recognized task types (tax filing, passport, DMV/license, vehicle registration, insurance claim, home repair, finding a provider, appointments) **localize to your home ZIP** when you opt in — from the dashboard's clarify card or `prefrontal clarify localize on` (both write `POST /clarifications/localization`) |
| Mail ingestion + triage | `prefrontal/mail/` | ✅ Normalize → triage (Ollama + heuristic) → surface as action items; `prefrontal mail`, `POST /webhooks/mail/sync` |
| Webhook listener (native app / iOS Shortcuts) | `prefrontal/webhooks/` | ✅ Implemented — FastAPI, one-tap logging (App Intents primary; Shortcuts fallback) |
| Behavioral insights UI | `prefrontal/stats.py` · `webhooks/stats.html` | ✅ `GET /stats` — time-estimation bias, follow-through + streak, channel responsiveness; inline SVG/CSS charts over your episodes (shared light/dark theme + nav) |
| Profile summarizer | `prefrontal/memory/summarizer.py` | ✅ Structured profile + cached LLM (Ollama) summary with heuristic fallback, served by `GET /profile` |
| LLM-as-sensor | `prefrontal/sensor.py` | ✅ Free text → *candidate* structured updates (allowlisted, `source=llm_inferred`), held pending until you accept; `prefrontal note` / `proposals` |
| Voice brain-dump → structured items | `prefrontal/braindump.py` | ✅ One rambling voice/free-text dump fanned out to **both** capture paths — the NL assistant (actionable items → a previewable action list) *and* the sensor (behavioral asides → pending proposals) — merged into one review. Nothing writes on capture (apply via `/assistant/apply` and `/proposals/{id}/accept`). `POST /braindump`, `prefrontal braindump "…"` (`--file`/stdin, `--apply`) |
| n8n integration | `prefrontal/integrations/n8n.py` | 🧩 Outbound client works; inbound event router still a documented stub |
| Ollama inference client | `prefrontal/integrations/ollama.py` | ✅ Implemented — local generate + availability check |
| Challenge-area modules | `prefrontal/modules/` | ✅ Framework + 7 modules, all wired end-to-end (5 EF challenges + closed-loop trip tracking + an opt-in Self-Care meal/water check) |
| Shared household sheet / Parent pack | `prefrontal/household.py`, `webhooks/routers/household.py` | ✅ Co-parent facts, agreements & star charts, shared shopping list, load-balance view, daily delta digest, weekly mental-load check-in, self-serve invites; `/kids` + `/family` views, `prefrontal household …` |
| Recurring shared chores | `prefrontal/household.py`, `webhooks/routers/household.py` | ✅ Owner-assigned daily jobs (run the dishwasher, pack lunches) with a lead-time reminder to the owner and — if it slips past due — a gentle heads-up to the *other* parent so the morning isn't a surprise; one-tap **Done**, `POST /webhooks/household/chores/check`, `prefrontal household chore` |
| Native delivery | `prefrontal/delivery.py` | ✅ Publishes native APNs push (one-tap action buttons) / Twilio voice call / TTS; ntfy is a dev-only shim (`PREFRONTAL_NTFY_DEV=1`) for free-signing builds; `prefrontal coach --deliver` |
| Location-Aware Task Anchor | `prefrontal/modules/location_anchor.py` | ✅ Wired end-to-end — escalation + location-gating + auto-close + n8n/Twilio workflow |
| Implementation Intentions | `prefrontal/modules/implementation_intention.py` | ✅ Wired end-to-end — `implementation_intentions` table, cue-matcher on the coaching tick (curated place via `geo.nearest_place` and/or a `HH:MM-HH:MM` band), one-utterance capture through the NL assistant (`add_if_then`); delivered through the shared channel/debounce path, no new endpoint |
| Closed-Loop Trip Tracking | `prefrontal/trips.py`, `prefrontal/modules/trip_tracking.py` | ✅ Wired end-to-end — passive home-radius loop detection on `POST /webhooks/location` (set home via `POST /webhooks/home`), retrospective `POST /webhooks/trip/{label,domain,reflect}`, `GET /trips`; the honest reflection classifies to an outcome that feeds the learning loop. Surfaced visually on the **Trips** page (`/trips/board`) |
| Focus balance (life-domains) | `prefrontal/focus_balance.py` | ✅ Rolls **all** out-of-home time — passive trips **and** declared outings that returned — up by life-sphere (shop/work/home/kids/personal) over a week: `GET /balance`, `prefrontal balance`, a briefing line + profile section, and the bar chart on the **Trips** page (`/trips/board`); opt-in weekly "light on kids/personal" nudge gated on per-domain targets the **Parent pack** seeds. One-tap 🏠/🧒/🙋 buttons on the trip-label ask file a trip's sphere from the notification |
| Hyperfocus | `prefrontal/modules/hyperfocus.py` | ✅ Wired end-to-end — focus sessions, protect-vs-interrupt, `POST /webhooks/focus/*` |
| Visual day-shape (timeline) | `prefrontal/day_shape.py` · `webhooks/day.html` | ✅ Today as a proportional **timeline** (the Structured/Tiimo pattern) — fixed commitment blocks, open todos fitted into the forward gaps, quiet free time between; block height tracks length so the day's *shape* is glanceable. Built for how ADHD readers read a chart (CHI-2024): meaning never rides on colour alone — a `kind` word + glyph + solid/dashed/dotted edge on every block, and a monochrome CLI render. Past dimmed, not scored. `GET /day`, `/day/board`, `prefrontal day` |
| Home Screen & Lock Screen widget | `ios/PrefrontalWidgets/` | ✅ Native WidgetKit glance — next departure, the one todo to start now, next commitment, tap-to-log self-care + Live Activity |
| Source-agnostic triage agent | `prefrontal/triage.py` | ✅ Classify → route → nudge for any inbound signal (mail/calendar/n8n/manual): `POST /webhooks/n8n` · `/triage` · `GET /triage/recent`, routing into commitments/todos/episodes + a `triage_log`, surfaced in the briefing + a dashboard panel. See `docs/triage-agent.md`. |
| People queue (names in ingested items) | `prefrontal/people.py` · `webhooks/routers/people.py` | ✅ Names mentioned in ingested items are extracted on the triage path and, when not yet known, queued for you to **identify** (link/create a person) and **categorize** (relationship + importance). The resulting roster feeds **learning** (a "Key people" section in the profile) and **prioritization** (a todo naming a high-importance person gets a priority bump). `GET /people/queue`, `POST /people/mentions/{id}/identify\|dismiss`, `/people` CRUD, `POST /people/extract`; `prefrontal people queue/identify/dismiss/list/add/categorize/scan` |

If you're exploring the code, start with `docs/schema.md`, then `prefrontal/memory/store.py`,
then `prefrontal/webhooks/app.py`.

---

## Quick start

Requires Python 3.10+.

```bash
# Install (editable, with dev tooling)
pip install -e ".[dev]"

# Configure — copy the example and edit
cp .env.example .env

# Create the SQLite memory database (structure only — no global seed)
prefrontal init-db

# Provision yourself (multi-tenant: every data command is scoped to a user).
# Prints a one-time token; seeds this user's coaching-state + module defaults.
prefrontal user add me --operator

# Run the webhook listener (defaults to http://0.0.0.0:8000)
prefrontal serve

# CLI data commands are per-user — pass --user <handle> (defaults to the sole
# user when there's only one).

# Learn: recompute derived patterns + the time-estimation bias from episodes
prefrontal learn

# Print the structured behavioral profile assembled from memory
prefrontal profile

# Summarize it into prioritized prose via a local Ollama model. Caches the
# narrative (served by GET /profile) and writes profile-<handle>.md. Falls back to the
# structured profile if Ollama isn't running. Run nightly after `learn`.
prefrontal summarize

# Print today's morning briefing (add --llm for friendly prose via Ollama)
prefrontal briefing

# Overwhelmed and frozen? Triage what's actually on fire + one first step
# (add --llm for a steadying rewrite via Ollama)
prefrontal panic

# Capture open loops, then fit them into spare time
prefrontal todo add "Call dentist" --minutes 10 --priority 2
prefrontal fit 20      # "with 20 min free, you could knock out…"

# See the whole day's *shape* — commitments + fitted todos + free time, as a
# monochrome timeline (the visual /day/board page renders the same data)
prefrontal day

# Capture when someone else is blocked on YOU (feeds panic + briefing priorities)
prefrontal blocked add "Sam" "the budget numbers" --priority 2
prefrontal blocked list           # who's waiting, longest wait first
prefrontal blocked resolve 1      # you delivered — clear the wait

# Find a time from free text (the calendar assistant). Parses how long, when, and
# who's involved; asks one question if the ask is too vague to answer.
prefrontal find-time "45 min for coffee with Sam this week"
prefrontal find-time "when are my wife and I both free for dinner tomorrow evening"
#   → a partner's FYI items block only when the plan involves them; solo asks
#     ignore items that are just hers. Add --llm to parse with the model.

# Hone in a vague item ("Tax") so it can actually be started (also runs on the tick)
prefrontal clarify check                 # flag ambiguous todos/commitments
prefrontal clarify resolve <id> --option 0   # pick a reading; prints a guide if recognized
prefrontal clarify guide tax_filing      # preview a task's step-by-step walkthrough
prefrontal clarify localize on --zip 19027   # opt in: localize guides to your ZIP (off by default)

# Where did the week's out-of-home time go? (shop/work/home/kids/personal)
prefrontal balance            # last 7 days by life-sphere, vs your weekly aims
prefrontal balance --days 30  # zoom out to a month

# End-of-day self-care gap review — the day's clicks read back as a timeline
# ("you drank enough, but your first glass wasn't until 3pm"; "6h between bio
# breaks"). On demand here; opt into the evening push with self_care_review_enabled.
prefrontal self-care review

# See which challenge-area modules are enabled (and their interventions)
prefrontal modules -v

# New user? Read the per-module walkthrough (the same content as the in-app /guide)
prefrontal modules --tutorial            # every enabled module
prefrontal modules --tutorial hyperfocus # just one
```

To run it always-on on a Mac mini and wire up Ollama, n8n, the native iOS app
(with iOS Shortcuts as the free-signing fallback), and native APNs push, follow
[`docs/deployment.md`](docs/deployment.md). The glue files (launchd service,
importable n8n workflow, iOS Shortcut fallback recipe) live in
[`deploy/`](deploy/).

Send a one-tap-style outcome the way the app's App Intent (or a fallback Shortcut) would:

```bash
curl -X POST http://localhost:8000/webhooks/shortcut \
  -H "X-Prefrontal-Token: $PREFRONTAL_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"action": "made_it", "episode_type": "departure", "channel": "notification"}'
```

Interactive API docs are available at `http://localhost:8000/docs` while the server runs.

---

## Stack

| Component | Role |
|---|---|
| Mac mini (Apple Silicon) | Always-on host |
| n8n | Workflow orchestration |
| Ollama | Local model inference |
| SQLite | Behavioral memory |
| Google Apps Script | Work email digest (stays within Google) — *planned, not yet implemented* |
| Native iOS app (Shortcuts fallback) | App Intents, geofences, widgets, push; one-tap logging (Shortcuts on free-signing installs) |
| APNs (native push) | Notification delivery — one-tap action buttons (ntfy is a dev-only shim for free-signing builds) |
| Twilio | Voice-call escalation (the `voice`/critical channel) |
| Tailscale | Remote access |

LLM inference uses a local model via Ollama for routing and triage. Heavier reasoning tasks can optionally use the Anthropic API — configurable per agent via `ANTHROPIC_AGENTS` (assistant / summarizer / briefing / sensor / triage), with Ollama as the fallback. See "Inference providers" in [`docs/guide.md`](docs/guide.md).

---

## Design principles

**Local first.** Your behavioral data doesn't leave your network. API calls are optional and explicit.

**Low friction or it doesn't work.** Every capture mechanism is one tap — an App Intent (Siri / Action Button / widget) or, on a free-signing install, a Shortcut. If logging an outcome is harder than ignoring it, the system fails.

**Escalation is not optional.** A single notification is easy to miss. Every time-critical reminder has an escalation path — notification → sound → voice — that continues until acknowledged.

**Learn from failure, not just success.** Missed reminders, late departures, and dropped tasks are data. The system logs them and adjusts.

**Modular by design.** Each agent is a discrete, replaceable component. Swap the notification layer, the inference provider, or the orchestration tool without rebuilding everything.

---

## Contributing

Contributions, feedback, and lived experience with executive function challenges are welcome.
See [`CONTRIBUTING.md`](CONTRIBUTING.md) for project layout, local setup, and conventions.

---

## Status

Early development. Not yet ready for general use. See [`ROADMAP.md`](ROADMAP.md)
for what's planned next, and [`CHANGELOG.md`](CHANGELOG.md) for what's shipped.

> **Note:** This project is 100% write-only — the codebase is written entirely
> by AI (Claude), not hand-authored line-by-line. Read it with that in mind.

---

## License

MIT
