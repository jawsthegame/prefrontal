<p align="center">
  <img src="brand/png/prefrontal-app-icon-256.png" width="120" height="120" alt="Prefrontal">
</p>

# Prefrontal — a behavioral executive-function assistant

*A white paper on the problem, the principles, and the architecture.*

> **Status.** Prefrontal is in early development. This paper describes the
> system's design and direction; where something is built versus planned, it
> says so. The authoritative build status lives in [`README.md`](../README.md)
> and [`ROADMAP.md`](../ROADMAP.md).

---

## Abstract

Prefrontal is a self-hosted assistant for people whose executive function —
the brain's system for planning, initiating, timing, and switching between
tasks — doesn't reliably do those things on its own. It watches your context
(location, calendar, time of day, recent activity), nudges at the right moment
through the right channel, and learns from whether each nudge actually worked.
Over time it builds a behavioral model of how *you* operate and gets better the
longer you use it. It runs on your own network, and your behavioral data does
not leave it unless you decide it should.

---

## 1. The problem

**Executive dysfunction is not a motivation problem.** People with ADHD (and
adjacent conditions) routinely know *what* to do, *why* it matters, and *how* to
do it — and still cannot reliably start it, time it, or stop the wrong thing at
the right moment. The gap is not desire or knowledge; it is the regulatory layer
between them.

Most productivity software makes this worse in a specific way: **it assumes
you'll remember to use it.** A to-do app is a filing cabinet for intentions you
already couldn't act on. A calendar alert fires once and is gone. A habit
tracker asks the impaired faculty — self-directed follow-through — to power the
very tool meant to compensate for it. When the tool requires the capacity it's
supposed to replace, it fails exactly when it's needed most.

There is also an **invisible-load** dimension. Much of the executive work of a
household or a job is *inventory held in one person's head* — who needs picking
up, what size shoe, which deadline is really the deadline. That load is heaviest
precisely for the person least equipped to hold it, and it is illegible to
everyone else until it's dropped.

Prefrontal starts from a different assumption: **the system should carry the
regulatory load, not delegate it back to the user.** It should notice, decide,
and reach out — and it should learn, because a nudge that doesn't fit its person
is just noise.

---

## 2. Design principles

These five commitments shape every part of the system.

1. **Local-first.** Your behavioral data stays on your network. It lives in a
   local SQLite database on a machine you control. LLM inference runs on-device
   via a local model; calls to any external API are optional, per-agent, and
   explicit. Privacy here is not a feature bolted on — it's the default and the
   architecture.

2. **Low-friction or it doesn't work.** Every capture is one tap or one
   shortcut. If logging an outcome is harder than ignoring it, people ignore it,
   and the learning loop starves. The system meets you where you are (a
   lock-screen reply, an iOS Shortcut) rather than asking you to come to it.

3. **Escalation is not optional.** A single notification is trivially missed —
   and "missed once" is the default failure mode of the population being served.
   Every time-critical reminder has an escalation path (notification → sound →
   voice call) that continues until acknowledged. The point is not to nag; it's
   that "one ping and done" is indistinguishable from no system at all.

4. **Learn from failure, not just success.** Missed reminders, late departures,
   dropped tasks — these are not gaps in the data, they *are* the data. The
   system logs them and adjusts its timing, wording, and channel. A miss at 4pm
   teaches it something a success never could.

5. **A model of you, not a to-do list.** The goal is not a prettier list of
   intentions. It's an evolving behavioral model — your real departure lag, the
   channel you stop reading after 3pm, how much you underestimate a "quick"
   task — that makes every future nudge land better.

---

## 3. The primitives

Prefrontal is built as a system of small, single-purpose parts over a shared
behavioral memory, rather than one monolithic assistant. The primitives:

- **Behavioral memory (the core).** A local SQLite store of *episodes* (what
  happened — did you leave on time, complete the task, answer the nudge),
  *patterns* (what a learning pass derives from those episodes), and *coaching
  state* (per-person, per-situation tuning). Everything the system knows about
  you lives here, scoped per user so nothing leaks between people.

- **Commitments & todos.** Calendar sync (Google + ICS) with double-booking
  detection, plus open-loop todos that get *fitted* into your real free windows
  rather than dumped on a flat list. A stall-prone todo can be **decomposed**
  into a tiny first step plus the rest. And because self-assigned priority is
  gameable, it prioritizes *honestly*: it surfaces the important todo you keep
  **not** doing, pins the task you're mid-flight on to the top, and flags when
  you're heads-down on something less important than a task you're avoiding.

- **The pipeline: capture → triage → memory → coaching → delivery.** Signals
  come in (location triggers, mail polling, calendar sync, one-tap captures); a
  triage step classifies and prioritizes; memory records and informs; a coaching
  step decides what to say and when; a delivery layer sends it through the right
  channel with escalation.

- **The coaching tick engine.** On each tick it fans out over every enabled
  module's `evaluate()` hook, collects the cues they raise, chooses a channel
  (an urgency floor, bumped by what it has learned works for you), and suppresses
  on quiet hours and debounce so it stays a coach, not an alarm clock. One
  misbehaving module can't sink the tick.

- **The natural-language assistant.** Edit your day in plain English ("move
  Sam's dentist to Thursday and add cleats to the shopping list"). Input is
  turned into a validated, **previewed** action list against a whitelist of
  allowed operations — you confirm before anything is written. No forms.

- **Delivery & escalation.** Pushover / ntfy today, text-to-speech and voice
  (via a workflow tool) for the top of the escalation ladder, all reachable
  remotely over Tailscale.

---

## 4. The learning loop

The learning loop is what separates Prefrontal from a scheduler.

Every nudge is an experiment. The system records the outcome — acted-on,
ignored, acknowledged-then-missed — against the *context* it fired in (time of
day, channel, situation). A periodic learning pass turns those episodes into
patterns and biases:

- **Time estimation.** It measures how much you actually underestimate specific
  kinds of tasks and travel, and corrects future estimates toward reality.
- **Departure lag.** It learns the real gap between "time to leave" and when you
  actually move, and pads the nudge accordingly.
- **Channel response.** It learns which channel you answer *when* — and stops
  spending a channel you've quietly stopped reading after a certain hour.
- **Quiet windows.** It learns when to say nothing.

A summarizer compresses these patterns into a **profile document** that is
injected into every agent's prompt, so behavioral context travels with each
interaction rather than being re-derived. And crucially, a **miss is a
first-class signal**: the loop is designed so that the failures which make other
tools give up are exactly what make this one improve.

A necessary counterweight: a system whose job is nudging needs a brake. On a day
that genuinely goes rough, an **encouragement/recovery** mode stops nudging,
acknowledges the day without judgment, and hands back a concrete, smaller plan —
opt-in, tone-calibrated, and capped once a day so it never becomes a pile-on.

---

## 5. Modules — by target dysfunction

ADHD isn't one thing, so Prefrontal isn't one nudge. Support behaviors are
organized into independently enableable **modules**, one per executive-function
challenge. Enable only the ones that fit your profile. Each module owns its
coaching-state defaults, contributes a section to the behavioral profile, and
declares the interventions it provides.

| Dysfunction | Module | What it does |
|---|---|---|
| Time perception & estimation | **Time Blindness** | Corrects estimates from your history, pads departures with a learned buffer, calls out elapsed time. |
| Task initiation | **Task Paralysis** | Shrinks work to a 5-minute first step, can auto-break-down a task you're *avoiding* (opt-in), offers a body-double check-in. |
| Focus regulation | **Hyperfocus** | Protects focus when it's aimed right; checks alignment and enforces breaks when it isn't. |
| Response inhibition | **Impulsivity** | A reflective pause before a snap switch; capture-and-defer the distraction. |
| Prospective memory, in the field | **Location-Aware Task Anchor** | Escalating nudges (soft → firm → voice call) back to a stated intention as its window elapses, gated by actually being on-site. |
| Task initiation, pre-committed | **Implementation Intentions** | Pairs a concrete cue (a place and/or time-of-day band) with a tiny pre-decided action and re-shows it the moment the cue is detected — the strongest-evidenced ADHD self-regulation technique (Gollwitzer & Sheeran), delivered at the trigger rather than on a clock. Forgiving: no streak, no guilt. |
| Basic needs, dropped in flow | **Self-Care** | Opt-in "have you eaten? / had water?" checks that pierce a focus block — not an EF dysfunction itself but a downstream casualty of one; adaptive cadence to a daily target. |
| Undeclared trips & out-of-home balance | **Closed-Loop Trip Tracking** | Captures a round trip you didn't declare, asks for a one-tap label + honest reflection, and flags when out-of-home time skews away from a life-sphere you've set a target for. |

*(See [`docs/one-sheet.pdf`](one-sheet.pdf) for the reader-facing version of
this breakdown. Build status per module is tracked in `README.md`.)*

Adding your own module is a small subclass — the module abstraction is the
extension point.

---

## 6. Composition: Context Packs

Modules answer *how your ADHD shows up*. A **Context Pack** answers *what life
you're managing* — Parent, Caregiver, Grad student, New job.

A Pack is deliberately **not** a module subclass. Modules sit on the
executive-function axis; a role like "parent" is an orthogonal *life-context*
axis (a parent still has time blindness — "parent" isn't a peer of
"hyperfocus"). A Pack is a higher **composition layer** that is mostly
declarative plus a small tool registry: it (1) switches on a relevant subset of
challenge modules, (2) seeds domain vocabulary — commitment kinds, todo
categories, default windows, (3) registers **situation tools** that are thin
compositions of existing primitives (todos, commitments, departure, panic,
decomposition, the NL-assistant whitelist), and (4) tailors surfaces like the
`/kids` household dashboard. Because it's declarative and composed from shipped
primitives, a Pack is cheap to add and shareable — "install the Parent pack."

The **Parent Pack** is the first, and much of it has **shipped**. Its backbone is
a **shared household sheet**: one always-current place two co-parents both read
and write in plain English — sizes, routines, allergies, providers, standing
behavior plans (star charts), a shopping list, appointments. Every entry is
stamped with who set it and when, which makes the invisible load *legible*; a
daily **delta digest** pushes the changes that matter to the parent who didn't
make them, a **load-balance view** shows who's keeping it up, and an optional
weekly **mental-load check-in** asks. This drove a **household scope** alongside
the per-user scope — two people, one set of rows, every write attributed —
reached at `/kids` (and a `/family` glance), with self-serve **invites** so a
co-parent can join. (Full design: [`docs/household-sheet.md`](household-sheet.md);
reader-facing overview: [`docs/parent-pack.pdf`](parent-pack.pdf).)

---

## 7. Architecture

```
iOS Shortcuts / Webhooks
        ↓
  Ingestion Layer     ← location triggers, mail polling, calendar sync
        ↓
  Triage              ← classifies, prioritizes, routes
        ↓
  Memory Layer        ← SQLite: episodes, patterns, coaching state (per-user scoped)
        ↓
  Coaching tick       ← fans over modules' evaluate(), picks channel, gates on quiet hours
        ↓
  Delivery Layer      ← Pushover / ntfy / TTS / voice, with escalation
```

| Component | Role |
|---|---|
| Always-on host (e.g. a Mac mini) | Runs the service continuously on your network. |
| **SQLite** | Behavioral memory — the core everything learns into. |
| **Ollama** | Local model inference for routing, triage, and prose. |
| **FastAPI** | The webhook/API surface (one-tap capture, dashboards, panic, family view). |
| **n8n** | Workflow orchestration and the top-of-ladder voice path. |
| **iOS Shortcuts** | Location triggers and one-tap outcome logging. |
| **Tailscale** | Private remote access — no public exposure. |
| **Pushover / ntfy** | Notification delivery. |
| Anthropic API (optional) | Heavier reasoning, per-agent, opt-in. |

Two properties are worth calling out:

- **Multi-tenancy by construction.** Every row is scoped to a user via the store
  layer, so no read or write can forget its `WHERE` and leak across people. The
  household scope is a deliberate, parallel exception on its own column — not a
  weakening of per-user scoping.
- **Replaceable parts.** The notification layer, the inference provider, and the
  orchestration tool are each swappable without rebuilding the rest. Modules and
  Packs are opt-in extension points, not forks.

---

## 8. Privacy & trust

For this population, trust is not a nice-to-have — the system is watching your
location, your calendar, your mail, and your failures. That only works if the
data stays yours.

- Behavioral data lives in a **local SQLite database** on **your** hardware.
- Inference defaults to a **local model**; any external API call is **optional,
  explicit, and per-agent**.
- Remote access is over **Tailscale** (your private network), not a public
  endpoint.
- Nothing leaves your network unless you decide it should.

---

## 9. Where it's going

Prefrontal is in active, daily use. A lot is in place — multi-tenant memory and
the learning pass, calendar/todo handling, briefing, panic mode, encouragement,
mail triage, the coaching tick engine, all seven challenge modules, native ntfy
delivery, the LLM-as-sensor capture path, and the Parent Pack backbone (shared
household sheet, star charts, shopping, delta digest, load-balance view, weekly
check-in, self-serve invites). What's still ahead:

- **Context Packs as a first-class abstraction** — a formal Pack registry and
  merge rules when two Packs overlap (the Parent backbone already ships).
- **Sharper learning** — deeper context-conditioned patterns and learned channel
  response, building on today's time-of-day bias and recency-weighted pass.
- **A source-agnostic triage agent** — routing arbitrary inbound events (mail
  triage is the first slice); native Pushover/TTS beyond today's ntfy default.

See [`ROADMAP.md`](../ROADMAP.md) for the detailed, honestly-scoped list of what's
planned, and [`CHANGELOG.md`](../CHANGELOG.md) for what's already shipped.

---

## Appendix — further reading

| Document | What it covers |
|---|---|
| [`README.md`](../README.md) | Capabilities, current build status, quick start. |
| [`ROADMAP.md`](../ROADMAP.md) | What's planned and still open. |
| [`CHANGELOG.md`](../CHANGELOG.md) | What's already shipped (release notes). |
| [`docs/schema.md`](schema.md) | The full database schema. |
| [`docs/household-sheet.md`](household-sheet.md) | The shared co-parent sheet design spec. |
| [`docs/coaching-agent.md`](coaching-agent.md) · [`docs/triage-agent.md`](triage-agent.md) | Agent designs. |
| [`docs/one-sheet.pdf`](one-sheet.pdf) · [`docs/parent-pack.pdf`](parent-pack.pdf) | Reader-facing one-sheets. |
| [`docs/brand/`](brand/) | The brand mark and assets. |

---

*Prefrontal is MIT-licensed and built with lived executive-function experience,
not against it.*
