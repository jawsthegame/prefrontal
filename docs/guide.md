# Prefrontal — Usage Guide

Prefrontal is a local-first executive-function assistant. It watches your
outings, calendar, todos, focus, and mail, learns your patterns, and nudges you
at the right moments — all running on your own machine.

This guide is the practical tour: **what each capability does, the problem it
solves, and a real-world example you can run.** For setup see
[`deployment.md`](deployment.md); for the data model see [`schema.md`](schema.md);
for the deeper design of individual agents see the specs linked from
[`README.md`](README.md). A compact [API & CLI reference](#reference) is at the end.

> **Reading this in a browser?** Your running instance serves this same guide at
> **`/manual`** (e.g. `http://mac-mini.tailnet.ts.net:8000/manual` over Tailscale),
> rendered live from this file — no auth, no personal data.

## How it fits together (30 seconds)

```
iOS app (App Intents / geofence / widget) ─┐
Shortcuts (free-signing fallback) ─────────┴─►  Prefrontal API (FastAPI, :8000)  ─►  SQLite (on-box)
                                                         │
                                                         ├─►  native APNs push (product) / Twilio call
              coach --deliver / n8n ─────────────────────┘     (ntfy dev shim on free-signing builds)
                       ▲
                       └── Ollama (local LLM, optional) composes prose
```

Your phone's clients (App Intents, geofences, or a fallback Shortcut) **POST
directly** to the Prefrontal API; the native `prefrontal coach --deliver` tick
(or, optionally, **n8n**) polls the data on a schedule, composes nudges with
Ollama, and delivers them out.

- **Prefrontal** is the brain: a FastAPI app over a SQLite database. Nothing
  leaves the machine unless you wire an outbound step.
- **Delivery** is native: `prefrontal coach --deliver` publishes each nudge over
  **native APNs push** (the product transport — real notification action buttons),
  with **Twilio** calls for the top of the escalation ladder and local TTS on the
  host. A free-signing build with no APNs falls back to the **ntfy dev shim**
  (`PREFRONTAL_NTFY_DEV=1`). **n8n** stays as optional orchestration muscle
  (workflows live in [`../deploy/n8n/`](../deploy/n8n)).
- **Ollama** is optional local inference for nicer phrasing and triage; every
  feature has a deterministic fallback when it's down. Heavier-reasoning agents
  can optionally use **Claude** instead — see "Inference providers" below.
- You interact via the **native iOS app** (App Intents on Siri / Action Button,
  the **home-screen widget**), the **dashboard**, or the **CLI** — with **iOS
  Shortcuts** as the free-signing fallback.

Examples below assume two shell variables:

```bash
PF=http://localhost:8000          # or http://mac-mini.tailnet.ts.net:8000 over Tailscale
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
- **Getting started** — [If-then plans](#implementation-intentions--the-cue-is-the-reminder)
  (implementation intentions)
- **Daily rhythm** — [Morning briefing](#morning-briefing)
- **In a hard moment** — [Emotion regulation](#emotion-regulation)
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

**How:** The app's "Going out" App Intent (or a fallback Shortcut) posts to
`/webhooks/outing/start`; n8n polls `/webhooks/outing/check` every minute and
delivers the nudge. "I'm back" closes it (or coming home does, if you feed
location).

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

**How:** Add each calendar's secret **ICS** feed URL per user with `prefrontal
calendar add-source` (URLs are sealed at rest), then the `com.prefrontal-calendar`
launchd job runs `prefrontal calendar sync --all-users` every 15 min: it fetches
each feed, parses + dedupes them, and upserts the batch into `commitments`.
Events you've RSVP'd *no* to are dropped; the same event on two calendars is
de-duplicated by matching title + start/end time (the feed-namespaced UIDs differ
across calendars, so the match is on the event's identity, not its id).

```bash
curl -s "$PF/commitments" -H "X-Prefrontal-Token: $TOK"   # upcoming, soonest first
```

Each event is classified **self** (you attend — can conflict), **fyi** (just
informational — where someone else will be), or **child** (a kid's appointment on
the shared household sheet). Fix a misclassification with
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
# On demand any time (breakdown is help for a stall, not done at creation):
curl -s -XPOST $PF/todos/42/decompose -H "X-Prefrontal-Token: $TOK"
# → {"first_step":"Open a doc and write the five section titles",
#    "first_step_minutes":4, "steps":["Draft the summary", ...]}
```

Tick the first step (`POST /todos/42/steps/0/done`) and the next surfaces.
Automatic breakdown of a todo you're *avoiding* is **opt-in (off by default)** —
enable it (`auto_decompose_enabled`) and the coaching sweep breaks one down only
once it's being avoided, and only when the model judges it worth it. The on-demand
call above always works regardless.

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

### In-progress & focus conflict — stay on the right thing

**What:** Marking a todo started (`POST /todos/{id}/start`, undo with `/unstart`)
pins it to the **top** of the list, so the thing you're mid-flight on stays
visible instead of sinking under a higher-priority item you haven't begun. And
when *everything* you've started ranks below an important task you're **avoiding
and haven't started**, `GET /todos` returns a `focus_conflict` object.

**Solves:** The in-progress task is where your attention already is — keep it in
view. And self-assigned priority is gameable in the other direction too: it's easy
to be busy on a minor thing while the important one waits.

```bash
curl -s "$PF/todos" -H "X-Prefrontal-Token: $TOK"
# → started todos first; "focus_conflict": {"working_on":"Tidy inbox",
#    "instead":"Renew passport", "days_open":12, ...}  (null when there's no conflict)
```

Started todos show an `▶ in progress` badge; a focus conflict renders as a gentle
**"you're mid-task on X, but Y is more important — worth switching?"** banner atop
the list. (CLI: `prefrontal todo start`/`unstart`.)

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

**Optional time checks (for time blindness):** if losing track of time in a block
is the problem, set the `elapsed_callout_minutes` coaching-state key (e.g. `30`)
and the coaching tick sends a gentle *"~N min on this so far"* on each interval —
separate from the alignment check/break above. Off by default (`0`); it only
fires while a focus session is active.

**One-tap start (don't make it a chore):** remembering to declare a session is
itself friction at the worst moment. So `intended_task` is optional — POST an
empty body and Prefrontal infers the task from your **top open todo**
(most-avoided first, carrying its estimate as the planned length), falling back
to a generic block when nothing's open. Trigger it from the **StartFocus** App
Intent (Siri / Action Button / widget / Apple Watch) — or a fallback Shortcut —
so dropping into focus is a single tap:

```bash
# one tap → "start focus on whatever I've been avoiding"
curl -s -XPOST $PF/webhooks/focus/start -H "X-Prefrontal-Token: $TOK" \
  -H 'Content-Type: application/json' -d '{}'
```

**Zero taps — calendar-armed:** the most frictionless start is none. Put a
**"Deep work: the RFC"** (or bare **"Focus time"**) block on your calendar, and
`POST /webhooks/focus/arm` — polled by n8n every few minutes — auto-starts a
protected session while that block is live (the event's end is the planned
duration; a bare "Focus time" protects your top todo). It won't stack on a
session you're already in. You scheduled the intention once; it arms itself.

```bash
curl -s -XPOST $PF/webhooks/focus/arm -H "X-Prefrontal-Token: $TOK"
# {"armed": true, "intended_task": "the RFC", "planned_minutes": 55, ...}
```

**Want a nudge to start? (opt-in):** if you'd like Prefrontal to *offer* a focus
block rather than wait for you, set the `suggest_focus_start` coaching-state key
to `on`. The coaching tick (§ delivered by the coach-check workflow) then emits
one gentle "want to protect a block today?" nudge — only in the focus-friendly
hours (`focus_suggest_start_hour`–`focus_suggest_until_hour`, default 8–15), only
when nothing's running, and only if you haven't focused yet today (once/day).
Off by default, so it never becomes "go focus" nagging.

**Forgot to start one? Log it after (so it still counts):** the flip side of
one-tap starting is that you'll sometimes just… start working. `POST
/webhooks/focus/log` records a block that already happened — it's logged as
ended `minutes` ago, so a forgotten stretch still feeds the learning loop
instead of vanishing. Blank task infers from your top todo, like the start.

```bash
curl -s -XPOST $PF/webhooks/focus/log -H "X-Prefrontal-Token: $TOK" \
  -H 'Content-Type: application/json' -d '{"minutes":90,"intended_task":"the RFC"}'
```

And to be *asked*: turn on `suggest_focus_recap` and, on an evening with nothing
logged, the tick offers a one-tap "log a block I didn't see?" — the same opt-in,
once-a-day, evening bookend to the morning start-suggestion.

---

## Getting started (if-then plans)

### Implementation intentions — the cue *is* the reminder

The single most strongly-evidenced ADHD self-regulation technique isn't a
to-do list — it's an **implementation intention**: pre-deciding one tiny action
for one concrete cue ("**if** I sit down at my desk after lunch, **then** I open
the tax form and set a 5-minute timer"). The payoff is entirely about *when* it's
surfaced: a plan sitting in a list is just another todo; a plan re-shown **at the
moment its cue is detected** offloads getting-started from willpower onto the
environment.

Capture one in a sentence through the assistant — "when I get home, take out the
recycling" — and the coaching tick re-shows the action the moment the cue fires.
A cue is any combination of:

- a **place** you're at (a curated location, matched by proximity — "at my desk"),
- a **time-of-day band** ("after lunch" ≈ 12:30–14:00), and/or
- a **home-crossing event** — `arrive_home` / `leave_home`, detected as you
  actually cross (so "when I get home" fires *at* the door, not on a timer).

```bash
# Capture via the natural-language assistant (previewed before it saves):
curl -s -XPOST $PF/assistant -H "X-Prefrontal-Token: $TOK" \
  -H 'Content-Type: application/json' \
  -d '{"message":"when I get home, take out the recycling"}'
# → previews: add_if_then {cue_text:"when I get home", action_text:"take out the
#   recycling", event:"arrive_home"}. Apply, and it surfaces at your next arrival.
```

Forgiving by design: there's no streak and no "you missed it." A plan you're done
with is simply archived — the technique works by lowering the cost of starting,
and guilt raises it.

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

Delivered each morning by `prefrontal briefing --deliver --all-users` on a
launchd timer — a native APNs push through each user's own route (the legacy
[`morning-briefing`](../deploy/n8n/morning-briefing.workflow.json) n8n workflow is
its predecessor).

---

## In a hard moment

### Emotion regulation

Emotional dysregulation is a core, large-effect part of ADHD — the fast, huge
spike of overwhelm, frustration, or the sting of perceived rejection — and it's
the *feeling* side of a hard moment that the task tools (panic-mode triage) and
the day tools (rough-day recovery) don't reach. Ask for support in the moment —
one tap, or a few words about what's going on — and Prefrontal offers **one**
brief, evidence-matched micro-skill fitted to the feeling: an ACT acceptance move
(name it → let it be → one values-aligned step), a DBT distress-tolerance skill
(paced breathing, 5-4-3-2-1 grounding, a cold-water reset), or self-compassion
framing for a rejection-sensitive moment.

```bash
# One-tap (no words) — a fitting skill for right now:
curl -s -XPOST $PF/emotion/support -H "X-Prefrontal-Token: $TOK" -d '{}'
# Or say what's going on, and it routes to a matching skill:
curl -s -XPOST $PF/emotion/support -H "X-Prefrontal-Token: $TOK" \
  -H 'Content-Type: application/json' -d '{"text":"I feel completely overwhelmed"}'
# → {"kind":"skill","state":"overwhelm","text":"…paced breathing: in for 4, out for 6…"}
```

**This is general-wellness support, not therapy or crisis intervention.** If what
you type suggests self-harm or crisis, Prefrontal doesn't answer with a coping
skill — it responds only with resources and an urge to reach a person (in the US,
988). It's opt-in, forgiving, and never keeps score. You can also fold a single
gentle acceptance line into the rough-day recovery message with the
`emotion_recovery_acceptance` key.

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
hallucinated or someone else's row). Parsing uses **Claude** when the
`assistant` agent is opted into the Anthropic provider (see below), otherwise the
local Ollama model (local-first by default); the response says which `provider`
answered.

### Voice brain-dump → structured items

Capture is where the whole learning loop starts, so the lowest-friction input —
speaking a rambling thought out loud — gets its own front door. **`POST /braindump`**
(and `prefrontal braindump "…"`) takes one unstructured dump and fans it out to
*both* capture paths at once, because a real ramble mixes two kinds of signal:

- **Actionable items** — "call the dentist, book the flights, we're out of milk" —
  go through the same editing assistant as above and come back as a *previewable*
  action list (todos, commitments, shopping, if-then plans, household facts).
- **Behavioral asides** — "…and honestly I keep blowing off admin on Mondays" — go
  through the LLM **sensor** and come back as *pending* candidate updates.

Nothing is written on capture. The actions apply through `POST /assistant/apply`
and the sensor candidates through `POST /proposals/{id}/accept`, so both halves
keep their existing review step — a rambling, imperfect dump can never silently
change your data. The CLI reads the dump from an argument, a `--file`, or stdin
(`--file -`), and `--apply` executes the edits immediately (behavioral candidates
always stay pending for review).

### Photo → structured items

A photo is the same low-friction capture for anything already *written* down — a
whiteboard after a meeting, a school newsletter, a scribbled list, a receipt.
**`POST /vision`** (and `prefrontal vision PATH`) reads the image to text with a
multimodal model, then runs that transcript through the exact brain-dump fan-out
above — so it has the same response shape and the same safety model (actions are
previewed, behavioral candidates land pending). A misread photo can never
silently change your data.

Vision is **local-first**: it reads with the on-device multimodal model when you
configure one (`OLLAMA_VISION_MODEL`, e.g. `llava` — remember to `ollama pull` it)
and it's installed, otherwise it falls back to the cloud Anthropic model. With
neither configured, `POST /vision` returns 503 rather than a misleading empty
result. A blank image or an unsupported `media_type` (jpeg/png/gif/webp) is a 422.

## Inference providers (local-first, opt-in Claude per agent)

Reasoning runs on the local Ollama model by default — nothing leaves the host.
When you set `ANTHROPIC_API_KEY` (and `pip install -e '.[anthropic]'`), you can
opt **individual agents** into Claude with `ANTHROPIC_AGENTS`, leaving everything
else local. The selectable agents are the reasoning-heavy ones, where a stronger
model earns its cost:

| Agent | What it does | Where |
|---|---|---|
| `assistant` | Natural-language edits to todos/commitments | `POST /assistant`, dashboard chat |
| `summarizer` | The prioritized-prose behavioral profile | `prefrontal summarize`, `GET /profile?refresh=1` |
| `briefing` | The morning briefing rewritten as prose | `prefrontal briefing --llm` |
| `sensor` | Free-text note or conversation transcript → candidate structured updates | `prefrontal note`, `POST /observe` |
| `triage` | Mail triage (urgency/category/needs-action) | `POST /webhooks/mail/sync` |
| `vision` | Read a photo → transcript (feeds the brain-dump fan-out) | `POST /vision`, `prefrontal vision` |

`vision` is the one agent that's **local-first even for the cloud toggle**: it
prefers the on-device multimodal model when one is installed and only reaches for
Claude as a fallback. Listing `vision` in `ANTHROPIC_AGENTS` inverts that —
preferring Claude when a key is set (still falling back to local without one).

`ANTHROPIC_AGENTS` is a comma-separated list (`summarizer,triage`), the sentinel
`all`, or an empty value for all-local. Unset keeps the historical default —
only the `assistant`. The snappy in-loop inferences (window/title/kind guessing,
todo decomposition) always stay local for latency. Selection is local-first and
safe: an agent falls back to Ollama when Claude is unavailable *or* a call fails,
and every path keeps its deterministic fallback when both are down.

## Surfaces: dashboard, family, kids, insights, and widget

- **`GET /dashboard`** — your full read-and-act monitoring page: active outings
  (with escalation level), todos (with first-steps and avoidance badges,
  in-progress items pinned to the top, and an honest-prioritization banner when
  you're mid-task on something less important than what you're avoiding),
  commitments + conflicts, the briefing, and your profile. Add/complete todos,
  close outings, dismiss conflicts. It also has an **assistant chat box**: type
  a plain-English ask ("bump the dentist call to urgent and drop the dry-cleaning
  todo") and it proposes the concrete edits, which you review and one-tap Apply.
  When you're in a household it also shows a **Shopping** card — a direct quick-add
  box and tap-to-check-off (no model needed), alongside the assistant path. A
  **Triage — recent decisions** card shows what the triage agent did with recent
  inbound signals (kind, where it routed, and *why* — so a drop is explainable).
  Each card carries small controls to **reorder** it (▲▼, move earlier/later) and,
  for the list cards, **cap how many items it shows** ("max N", with a "+ N more"
  toggle) — so you arrange the dashboard around what you look at; the layout is
  remembered per device. Operators also get **Update** / **Restart** buttons in
  the header (shown only when `PREFRONTAL_SELF_UPDATE` is on) — one tap to pull +
  deploy + restart, or just restart. Open it over Tailscale; it asks for your
  token once and remembers it.
- **`GET /household`** — the editable **Household** hub: the one writable surface
  for the shared sheet. Add kids, pets, facts, agreements, children and
  appointments, award stars, manage shopping and routines, and drive it all from
  a plain-English assistant box. Every edit happens here.
- **`GET /kids`** and **`GET /pets`** — focused, **read-only lenses** over the
  same shared sheet. `/kids` shows the roster & facts, standing plans + star
  progress, and upcoming appointments; `/pets` shows the pet roster and their
  facts (meds, vet/groomer, food). No edit forms — those live on `/household`.
  (`GET /family` is retired and redirects to `/household`.)
- **`GET /stats`** — the behavioral **Insights** page: charts over your learning
  data — time-estimation accuracy (the bias multiplier, overall and per context),
  follow-through (success rate, current streak, the success/partial/miss split
  and a recent sparkline), and channel responsiveness (which nudge channels you
  actually answer). Drawn with inline SVG/CSS, no external libraries.
- **iOS widget** ([`../ios/PrefrontalWidgets/`](../ios/PrefrontalWidgets)) — a
  native WidgetKit Home Screen / Lock Screen glance: your next departure, the one
  todo to start right now, the next commitment, and tap-to-log self-care, plus a
  Live Activity for an active outing/focus. A separate **"one next thing"** widget
  shows the *single* honest next action and nothing else — the mid-flight task
  you're in, a commitment to leave for, the worst clock-bound fire, or the
  avoided-but-important todo you keep skipping — with everything else withheld
  behind a "+N more can wait" line (`GET /next`). Tapping opens the app.

All of these share one light/dark theme and a common top nav, are reached over
Tailscale (e.g. `http://mac-mini.tailnet.ts.net:8000/dashboard`), and
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

- **Web pages** (`/dashboard`, `/household`, `/kids`, `/pets`, `/stats`) — **Sign in with Google**. You click the
  button, approve Google's consent once, and a signed session cookie carries you
  after that (no token to paste). Only emails in the `GOOGLE_OAUTH_ALLOWED`
  allowlist may sign in, each mapped to a user handle.
- **Automations** (n8n, the app's App Intents / widget, and fallback iOS
  Shortcuts) — the per-user `X-Prefrontal-Token` header, unchanged. They can't do an interactive login, so
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

Every endpoint requires the `X-Prefrontal-Token` header except `/health` and the
page shells — `/dashboard`, `/household`, `/kids`, `/pets`, `/calendar`, `/stats`,
`/review`, `/settings`, etc. (they carry no data and prompt for the token
client-side; `/family` now 308-redirects to `/household`).

### Endpoints

| Method & path | Purpose |
|---|---|
| `GET /health` | Liveness probe (no auth) |
| `GET /profile` | Behavioral profile; `?format=structured`, `?refresh=1` |
| `POST /webhooks/shortcut` | Log a one-tap outcome (made_it / missed_it / partial / log) |
| `POST /webhooks/n8n` | Triage an inbound n8n event (classify → route → maybe nudge) |
| `POST /triage` · `GET /triage/recent` | Triage one normalized signal / recent decisions + reasons |
| `POST /webhooks/location` · `GET /location` | Record / read last-known position |
| `POST /webhooks/outing/start` | Declare an outing (intention + window) |
| `POST /webhooks/outing/check` | Poll for due nudges (fires escalation) |
| `POST /webhooks/outing/return` | Close an outing; logs stated-vs-actual |
| `GET /outings` | Read-only outings snapshot (no side effects) |
| `POST /webhooks/focus/start` · `/check` · `/end` | Focus session lifecycle (`start` with an empty body = one-tap inferred start) |
| `POST /webhooks/focus/arm` | Auto-start a session from a live "focus"/"deep work" calendar block (n8n polls) |
| `POST /webhooks/focus/log` | Log a *past* focus block you forgot to start (records it as already ended) |
| `GET /focus` | Read-only focus snapshot |
| `POST /webhooks/calendar/sync` | Sync a batch of calendar events |
| `POST /webhooks/departure/check` | Poll for a due departure nudge |
| `GET /commitments` · `POST /commitments` | List / add a commitment |
| `POST /commitments/geocode` | Resolve missing coordinates |
| `GET /commitments/conflicts` | Hard + possible conflicts |
| `POST /commitments/conflicts/dismiss` | Dismiss a possible conflict (by `key`) |
| `POST /commitments/{id}/kind` | Set `self` / `fyi` / `child` |
| `POST /commitments/{id}/hardness` | Set `hard` vs `soft` (a user override; sticky across re-syncs) |
| `POST /commitments/{id}/notes` | Set / clear a note (folded into the departure nudge; kept across re-syncs) |
| `POST /todos` · `GET /todos` | Add (auto-augmented) / list (in-progress pinned first, with decomposition, avoidance, and a `focus_conflict` flag) |
| `POST /todos/{id}/start` · `/unstart` | Mark/unmark a todo in-progress (`started_at`) — pins it to the top |
| `GET /todos/fit?minutes=N` | Todos that fit a free block |
| `GET /todos/now` | The one todo that fits your free time right now (avoidance-biased, low-energy-later) |
| `GET /todos/avoided` | Important todos you keep skipping |
| `POST /todos/{id}/decompose` | First step + remaining steps |
| `POST /todos/{id}/steps/{i}/done` | Tick a decomposed step |
| `POST /todos/{id}/deadline` | Move or clear a deadline |
| `POST /todos/{id}/notes` | Set / clear a note (folded into the "you keep putting this off" nudge) |
| `POST /todos/{id}/done` · `/drop` | Complete / drop (logs an episode) |
| `POST /assistant` | Interpret a natural-language ask into proposed edits (no writes) |
| `POST /assistant/apply` | Execute previously-proposed edits (re-validated) |
| `POST /braindump` | Fan one free-text/voice ramble out to both capture paths → previewable edits + pending sensor proposals |
| `POST /vision` | Read a photo (on-device model, cloud fallback) → transcript fanned out like `/braindump` |
| `GET /briefing` | Today's digest (structured + rendered text) |
| `POST /observe` | Feed a free-text note **or** a conversation `transcript` to the LLM sensor → pending candidate updates |
| `GET /proposals?status=` · `POST /proposals/{id}/accept\|reject` | Review / apply / dismiss sensor proposals |
| `GET /panic` · `POST /webhooks/panic/check` | Overwhelm triage: one-tap headline / poll for a proactive nudge |
| `GET /next` | The single honest next thing to do right now (powers the "one next thing" widget) |
| `POST /webhooks/coach/check` · `/ack` | Run the coaching tick / acknowledge a nudge |
| `GET /encouragement` · `POST /encouragement/sent` | Rough-day recovery message / stamp it delivered (once-a-day cursor) |
| `POST /emotion/support` | In-the-moment emotion regulation: one micro-skill fitted to the feeling (`{}` for one-tap, or `{"text":"…"}`); crisis language → resources, not a skill |
| `POST /webhooks/impulse/capture` · `/webhooks/focus/switch` · `/resolve` | Capture a deferred impulse / log & resolve a context switch |
| `GET /nudge/act` · `/nudge/dismiss` · `GET /nudges` | One-tap action buttons / dismiss / recent-nudge log |
| `POST /places` · `GET /places` | Curated location aliases |
| `POST /webhooks/mail/sync` · `GET /mail` | Ingest mail / recent + action items |
| `GET /mail/triage/learned` · `/forget` · `/clear` | Learned triage corrections (repeat/quick-drop senders) |
| `GET /household/sheet` · `GET /household/shopping` · `POST /household/{create,facts,agreements,shopping,chores,routines,balance,checkin,digest,invites}` | Shared co-parent sheet — facts, agreements/star charts, shopping (list/add/check/remove), chores, routines (grouping + accountability), load-balance (doing + carrying), check-in, digest, invites |
| `POST /webhooks/household/{star-prompts,checkin,digest,chores}/check` | Scheduled household sweeps (star award prompts, weekly check-in, daily delta digest, chore reminders/miss-handoffs) |
| `GET /dashboard` · `/household` · `/kids` · `/pets` · `/stats` · `/review` | Web surfaces — dashboard, editable household hub, read-only kids/pets lenses, behavioral insights, LLM-sensor review (no auth on the shell). `/family` redirects to `/household`. |
| `GET /manual` · `/guide` | This usage guide, rendered as a page / the per-module new-user walkthrough (no auth) |
| `GET /stats/data` | Aggregated behavioral insights for the /stats charts |
| `GET /auth/google/login` · `/callback` | Google sign-in (browser); 404 until configured |
| `POST /auth/logout` | Clear the browser session cookie |
| `POST /admin/users` · `GET /admin/users` | Provision / list users (operator only) |
| `POST /admin/users/{handle}/rotate` · `/disable` | Rotate token / disable user |
| `POST /admin/update` · `/admin/restart` | Pull + reinstall + migrate + restart / restart only (operator; off until `PREFRONTAL_SELF_UPDATE=on`) |
| `GET /admin/whoami` | The signed-in user's `is_operator` + `self_update_enabled` (lets the UI gate operator controls) |

### CLI (`prefrontal <command>`)

| Command | Purpose |
|---|---|
| `init-db` | Create the SQLite database (structure only; users seed on `user add`) |
| `serve` | Run the API (uvicorn); `--host`, `--port` |
| `update` | Pull latest code, reinstall + migrate, then restart; `--no-restart` |
| `restart` | Restart the service (no code update) |
| `user add\|list\|rotate\|disable` | Manage users & tokens |
| `migrate-multi-tenant` | Upgrade a single-user DB in place |
| `learn` | Recompute patterns from episodes; `--user`, `--all-users` |
| `summarize` | LLM profile narrative → cache + file; `--all-users`, `--no-fallback` |
| `profile` | Print the behavioral profile |
| `briefing` | Print the morning digest; `--llm` for prose |
| `todo add\|list\|start\|unstart\|done\|drop\|domain` | Manage todos (add auto-augments; `start`/`unstart` set in-progress) |
| `fit <minutes>` | Todos that fit a free block now |
| `place add\|list\|remove` | Curated destination aliases (name → coords) for geocoding |
| `balance` | Out-of-home time by life-sphere (shop/work/home/kids/personal) |
| `clarify check\|list\|resolve\|dismiss\|guide` | Ambiguous-item clarifications |
| `mail list\|sync\|fetch\|add-source\|…` | View / ingest / IMAP-pull mail; `--account`, `--heuristic` |
| `calendar add-source\|list-sources\|remove-source\|sync` | Private ICS feeds → commitments (native, no-n8n); `--all-users` |
| `coach [--dry-run] [--deliver]` | Run the coaching tick — what's due, on which channel |
| `encourage` | Rough-day check: today's recovery message if it's gone sideways |
| `panic` | Overwhelm triage — what's on fire + one first step; `--llm` |
| `crunch on\|off\|status` | Deadline mode: suspend the work/life time bands; `--hours N` |
| `note "…"` (or `note --transcript turns.json`) / `proposals list\|stats\|accept\|reject` | LLM-as-sensor: jot a note or feed a conversation → review proposed updates; `stats` shows the sensor's accept-rate precision |
| `household add\|join\|leave\|show\|invite\|redeem\|star\|balance\|shopping\|chore\|routine\|prompt-check\|checkin-check\|digest-check\|chores-check` | Co-parent household sheet (chores + routines feed the two-facet balance) |
| `modules [-v]` | List challenge-area modules and status |

Data commands (`learn`, `summarize`, `profile`, `briefing`, `todo`, `fit`,
`place`, `balance`, `clarify`, `mail`, `calendar`, `coach`, `encourage`, `panic`,
`crunch`, `note`/`proposals`, `household`) take `--user HANDLE` (or `--all-users`);
with a single user they resolve automatically.

### Key configuration

Set in `.env` (see [`deployment.md`](deployment.md) for the full list):

| Variable | Default | Meaning |
|---|---|---|
| `PREFRONTAL_DB_PATH` | `prefrontal.db` | SQLite file |
| `PREFRONTAL_PORT` | `8000` | API port |
| `PREFRONTAL_DEFAULT_USER` | _(empty)_ | Tokenless requests resolve to this user (single-user mode) |
| `PREFRONTAL_MODULES` | _(all)_ | Comma-separated modules to enable |
| `PREFRONTAL_PACKS` | _(none)_ | Comma-separated Context Packs (life-context layers, e.g. `parent`); each switches on modules + seeds vocabulary. Earlier-listed wins a conflict. `prefrontal packs -v` |
| `PREFRONTAL_TRIAGE_LLM` / `_DROP` | `true` / `0` | Triage: use the model for ambiguous signals / confidence below which "noise" is surfaced not dropped |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `http://localhost:11434` / `llama3.1:8b` | Local inference (tip: set `127.0.0.1` explicitly to skip IPv6 `localhost` resolution) |
| `OLLAMA_VISION_MODEL` | _(empty)_ | Local multimodal model for on-device vision capture (e.g. `llava`); blank falls back to the cloud model for `/vision` |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | _(empty)_ / `claude-opus-4-8` | Opt-in cloud reasoning; blank keeps every agent local. Needs `pip install -e '.[anthropic]'` |
| `ANTHROPIC_AGENTS` | `assistant` | Which agents prefer Claude when a key is set: `assistant,summarizer,briefing,sensor,triage,vision`, `all`, or empty for all-local (`vision` is local-first — listing it flips it to prefer cloud) |
| `PREFRONTAL_MAIL_ACCOUNTS` | _(empty)_ | `account=full\|signals` retention pairs |
| `PREFRONTAL_ACCOUNT_LABELS` | _(empty)_ | `account=label:color` dashboard pills (e.g. `work=Acme:orange`) |
| `PREFRONTAL_CALENDAR_LABELS` | _(empty)_ | `feed=label:color` calendar pills (e.g. `personal=Personal:blue`) |
| `GEOCODER_URL` | Nominatim | Geocoding endpoint (opt-in) |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | _(empty)_ | Google sign-in client (off until both set) |
| `OAUTH_BASE_URL` | _(empty)_ | Public https origin for the OAuth redirect |
| `SESSION_SECRET` | _(empty)_ | Signs the browser session cookie (`openssl rand -hex 32`) |
| `GOOGLE_OAUTH_ALLOWED` | _(empty)_ | `email=handle,…` allowlist of who may sign in |
| `PREFRONTAL_SELF_UPDATE` | `off` | Enable `POST /admin/update` · `/admin/restart` (operator-only; runs deployed code) |
| `PREFRONTAL_REPO_DIR` | _(repo root)_ | Where the update runs `git` |
| `PREFRONTAL_UPDATE_CMD` | `bash deploy/update.sh` | Override the update command |
| `PREFRONTAL_RESTART_CMD` | `launchctl kickstart -k gui/<uid>/com.prefrontal` | Override the restart command (systemd/docker/etc.) |
