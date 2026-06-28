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
A daily digest calibrated to your preferences: what needs attention today, what went cold this week, what's on the calendar.

**Behavioral memory**
Logs outcomes — did you leave on time, did you complete the task, did you respond to the reminder — and uses that data to improve predictions and timing over time.

**Low-friction capture**
iOS Shortcuts integration for one-tap logging. The system meets you where you are rather than requiring you to go somewhere.

---

## Challenge-area modules

ADHD presents differently for everyone, so Prefrontal's support behaviors are organized into
independently enableable **modules** — one per executive-function challenge — rather than a
single fixed assistant. Enable only the ones that match your profile (an empty config enables
them all):

| Module | `key` | Addresses |
|---|---|---|
| Time Blindness | `time_blindness` | Duration estimation, departure timing, elapsed-time awareness |
| Task Paralysis | `task_paralysis` | Task initiation / activation energy (tiny first steps, decomposition) |
| Hyperfocus | `hyperfocus` | Protects *good* hyperfocus, interrupts *misdirected* hyperfocus |
| Impulsivity | `impulsivity` | A reflective pause and capture-then-defer before impulsive switches |
| Location-Aware Task Anchor | `location_anchor` | Escalating nudges (push → push → Twilio call) back to a stated intention as its time window elapses |

Each module owns its coaching-state defaults, contributes a section to the behavioral
profile, and declares the interventions it provides. Select a subset with
`PREFRONTAL_MODULES` (see `.env.example`), and list status with `prefrontal modules -v`.
Adding your own is a small subclass — see [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Architecture

Prefrontal is a system of small, focused agents rather than a single monolithic assistant. Each agent does one thing well and passes context to the next.

```
iOS Shortcuts / Webhooks
        ↓
  Ingestion Layer       ← location triggers, mail polling, calendar sync
        ↓
  Triage Agent          ← classifies, prioritizes, routes
        ↓
  Memory Layer          ← SQLite: episodes, patterns, coaching state
        ↓
  Coaching Agent        ← generates briefings, reminders, check-ins
        ↓
  Delivery Layer        ← Pushover / Ntfy / TTS / notification
```

The memory layer is the core. Everything the system learns about your behavior lives in a local SQLite database. A summarizer agent periodically compresses patterns into a profile document that gets injected into every agent's system prompt — so behavioral context travels with every interaction.

See [`docs/schema.md`](docs/schema.md) for the full database schema.

---

## What's in this repo today

Prefrontal is in early development. This repository currently implements the **foundation**:

| Layer | Module | Status |
|---|---|---|
| Memory layer (SQLite) | `prefrontal/memory/` | ✅ Implemented — episodes, patterns, coaching state |
| Webhook listener (iOS Shortcuts) | `prefrontal/webhooks/` | ✅ Implemented — FastAPI, one-tap logging |
| n8n integration | `prefrontal/integrations/n8n.py` | 🧩 Stub — bidirectional, documented TODOs |
| Profile summarizer | `prefrontal/memory/summarizer.py` | 🧩 Stub — heuristic, LLM version TODO |
| Challenge-area modules | `prefrontal/modules/` | ✅ Framework + 5 modules; most interventions are declared stubs |
| Location-Aware Task Anchor (Module 1) | `prefrontal/modules/location_anchor.py` | ✅ Wired end-to-end — outing endpoints + escalation logic + n8n/Twilio workflow |
| Triage / coaching / delivery agents | — | 🔜 Not yet built |

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

# Create and seed the SQLite memory database
prefrontal init-db

# Run the webhook listener (defaults to http://0.0.0.0:8000)
prefrontal serve

# Print the current behavioral profile assembled from memory
prefrontal profile

# See which challenge-area modules are enabled (and their interventions)
prefrontal modules -v
```

To run it always-on on a Mac mini and wire up Ollama, n8n, iOS Shortcuts, and
Pushover/Ntfy, follow [`docs/deployment.md`](docs/deployment.md). The glue files
(launchd service, importable n8n workflow, iOS Shortcut recipe) live in
[`deploy/`](deploy/).

Send a one-tap-style outcome the way an iOS Shortcut would:

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
| Google Apps Script | Work email digest (stays within Google) |
| iOS Shortcuts | Location triggers, one-tap logging |
| Pushover / Ntfy | Notification delivery |
| Tailscale | Remote access |

LLM inference uses a local model via Ollama for routing and triage. Heavier reasoning tasks can optionally use the Anthropic API — configurable per agent.

---

## Design principles

**Local first.** Your behavioral data doesn't leave your network. API calls are optional and explicit.

**Low friction or it doesn't work.** Every capture mechanism is one tap or one shortcut. If logging an outcome is harder than ignoring it, the system fails.

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
for what's stubbed and what's planned next.

---

## License

MIT
