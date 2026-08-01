# Delegation workspace — lift the agent's work out of the todo card

Status: **proposal**
Author: drafted with Claude, 2026-08-01

## Problem

A delegated todo stopped being an annotation on a checklist item and quietly
became a *job record*. The `todo_delegations` row now carries a **brief**
(markdown), **draft comms** (full email/message bodies), **action items**, the
**research steps** (every `auto` tool call + its result), and the **questions and
answers** of a back-and-forth. All of it renders *inside the todo card*, whose
entire job is to say "here's one small next step."

The result is clunky, and the code already admits it:

- **Web** (`prefrontal/webhooks/dashboard.html`) stacks the whole thing into one
  collapsible `<details>` panel on the row — brief, N draft blocks, action list,
  an inline per-question answer form, a steps disclosure, and a context
  disclosure. The CSS caps the pasted-context view at `max-height: 340px` with a
  comment that it's "the 'at least give me a show-more' fix … so a long
  transcript never floods the card." When you're writing "never floods the card"
  in a stylesheet, the card is the wrong container.
- **iOS** (`ios/Prefrontal/Views/TodosView.swift`) is worse: its `delegationPanel`
  renders only the brief, actions, and drafts. It **drops questions and steps
  entirely** — the `Delegation.swift` model decodes them, but the view ignores
  them and `Endpoints.swift` has no call to `/delegate/answers`. So an `auto` run
  that parks in `needs_input` shows a "❓ Needs you" chip on iOS with **no
  questions visible and no way to answer** — a dead end.

Two distinct complaints hide in "it doesn't maintain state or allow follow-ups":

1. **Presentation.** The content outgrew the todo card on web, and on iOS the
   card can't even host the interaction (no answer path).
2. **Interaction model.** What looks like a conversation is a *structured Q&A
   round-trip*, not a thread you can send a free-form message to. And the `auto`
   loop **restarts** on each answer rather than resuming — the original context
   and the answered Q&A persist, but the prior run's findings are thrown away and
   re-gathered (`todos.py:1037`, by design). So "state" is partial by
   construction, and there's no free-form follow-up channel at all.

These want different fixes, and conflating them is a trap. The first is solved by
moving the same data to its own surface. The second is a genuine product bet on
resumable agent conversations. This note proposes **both, staged** — presentation
first, because it's cheap and fixes most of the pain, then the interaction model
as a separate, deliberately-scoped step.

## What we have today

The mechanics are sound; only the container is wrong. Worth stating precisely so
the redesign preserves what works.

**Data model.** One `todo_delegations` row per todo (`prefrontal/memory` repos),
carrying `handler` (`agent`/`auto`/`email`/`sms`), `status`, `brief`, `drafts`,
`actions`, `steps`, `questions` (`{text, why, answer}`), and the user-supplied
`context`.

**Handlers** (`prefrontal/delegation.py`):

- `agent` — one generation, on-box. Reads the todo + pasted context, writes brief
  / drafts / actions. **No round-trip**: it never asks and never re-runs.
- `auto` — a superset: a bounded, allowlisted tool loop
  (`prefrontal/autorun.py:run_research`) gathers material, then the same
  `generate_prep` writes it up. It may **ask** — parking in `needs_input` with a
  partial write-up and its questions on the row.

**The round-trip** (`POST /todos/{id}/delegate/answers`, `todos.py`): answers are
recorded positionally onto the stored questions, status flips to `in_prep`, and
the run is **re-invoked on a background thread**. There is no suspended run to
resume — `answered_context` renders the Q&A as plain facts and the loop starts
over with them folded in (`autorun.py:answered_context`, `merge_questions`). What
persists across the round-trip: the **original `context`** and the **answered
Q&A**. What doesn't: the previous run's **findings/steps** (regenerated each
time).

**Endpoints** (`prefrontal/webhooks/routers/todos.py`): `POST /todos/{id}/delegate`
(create), `POST /todos/{id}/delegate/answers` (answer + re-run), `POST
/todos/{id}/delegate/return` (mark done). All delegation data reaches clients on
the existing `GET /todos` payload, nested under each todo's `delegation` object.

**Rendering:** web renders the full set inline (`dashboard.html`
`delegationPanel` / `questionsBlock`); iOS renders a read-mostly subset with no
question/answer/steps surface (`TodosView.swift` `delegationPanel`).

**A useful precedent:** the *clarifications* feature already has its own dedicated
answer screen on iOS (`ios/Prefrontal/Views/ClarifyView.swift`) posting to
`resolveClarification`. Delegation answers should follow that shape rather than
inventing a new one.

## Phase 1 — the presentation split (near-term)

A dedicated **delegation detail surface** on each client. **No change to the data
model, handlers, or endpoints** — this is purely where the existing content is
shown.

- The todo row keeps only a compact **status chip** ("🤖 prepped", "❓ needs
  you", "… prepping", "✉ sent") that **deep-links** into the delegation surface.
  The brief/drafts/actions/steps/questions leave the card.
- **Web:** a delegation detail view (route or modal) rendering the same
  `delegationPanel` content with room to breathe — brief up top, an **answer
  panel** for outstanding questions, drafts and action items as first-class
  blocks, research steps and pasted context as collapsible sections. The row's
  inline `<details>` panel and the `max-height: 340px` context clamp go away.
- **iOS:** a real delegation detail screen modeled on `ClarifyView` — and this is
  where Phase 1 **closes the current dead end**: add the questions list, an
  answer input, a `delegateAnswers` client method posting to `/delegate/answers`,
  and the `steps` trail. This is the highest-value single piece of the whole
  note, because today iOS `needs_input` is unanswerable.

**Why this is most of the win:** the clunkiness is a container problem. The Q&A
round-trip, the artifacts, and the status semantics all already work — they're
just wedged into a checklist item. Give them a room and the "embedded in the
todo" complaint largely dissolves, at low risk and with no autonomy implications.

## Phase 2 — resumable conversations (the deeper bet)

The bigger ask: talk to a delegated agent in a **free-form thread** and have it
**resume** rather than restart. This is a real product decision, not a UI tweak,
so it's deliberately separated.

**What changes:**

1. **A message/turn log**, not just a `questions` array. The delegation gains an
   ordered transcript of user messages and agent turns (asks, findings summaries,
   write-ups). The detail surface from Phase 1 becomes the natural host for it —
   artifacts on one side, the thread on the other. (Keep the distinction: the
   **artifacts** are the deliverable; the **thread** is how you steer it. A pure
   chat transcript that buries the brief would be a regression.)
2. **Resume, not restart.** Today the loop is stateless across rounds on purpose.
   Free-form follow-ups ("actually, focus on the 15-year option") only make sense
   if the run carries its prior findings forward. That means persisting enough run
   state — gathered material, the plan-so-far — to continue instead of
   re-gathering. This is the substantive engineering cost.
3. **Free-form input** alongside structured answers: a message box that appends to
   the thread and re-invokes the loop with the full context.

**The guardrail — addressed head-on.** `roadmap-vision.md` is explicit: *don't
promise open-ended agentic autonomy* — Prefrontal does **scoped, verifiable,
API/MCP-based** actions, never a browser agent. Resumable conversations are **not**
in tension with that, and it's important to say why: the guardrail constrains the
**action space** (which tools the loop may call — the allowlist in
`autorun.build_toolbox`), *not* the number of turns or whether state resumes. A
thread that runs ten turns of allowlisted tool calls is still a bounded action
space; the same doc already draws this line ("a constraint on the action space,
not on the number of steps"). Phase 2 must hold the allowlist exactly as it is —
what it adds is memory and a message channel, not new powers.

**Honest limits.** Resumable multi-turn loops are the *worst* case for a small
local model (see the PR #438 incident: a large transcript silently truncated,
prose where JSON was needed). Growing thread context makes it worse. Phase 2 is
the clearest case yet for the opt-in cloud provider, and must degrade gracefully
to the single round-trip when no capable model is reachable. Cost and context
growth also need a bound (summarize-and-compact the thread, cap retained
findings).

## Data model & API implications

- **Phase 1:** none. Same row, same endpoints, same `GET /todos` payload. iOS
  gains a `delegateAnswers` endpoint binding to the existing route.
- **Phase 2:** a delegation **messages/turns** table (or JSON column) for the
  transcript; persisted **run state** to resume (retained findings + plan); a new
  `POST /todos/{id}/delegate/message` (free-form) that appends and re-invokes. The
  answer round-trip becomes a special case of a message. Consider whether a
  delegation this rich still belongs 1:1 under a todo, or graduates to its own
  addressable resource (`/delegations/{id}`) that a todo *references* — which also
  cleans up the deep-link in Phase 1.

## Open questions

1. **Web detail surface: route or modal?** A dedicated route (`/delegations/{id}`)
   sets up Phase 2's addressable resource; a modal is faster to ship. Leaning
   route.
2. **Does the delegation graduate off the todo?** A 1:1 row is fine for Phase 1;
   Phase 2's transcript argues for a first-class `/delegations/{id}` the todo
   links to. Deciding early avoids a migration later.
3. **Thread vs artifacts layout** on the detail surface — how to keep the brief
   the star while the conversation stays available, not dominant.
4. **Resume state scope** — how much of a run to persist to make "continue" feel
   continuous without unbounded storage/context growth.
5. **`agent` (non-auto) delegations** — do they get the thread too (turning the
   one-shot into a conversation), or does Phase 2 remain `auto`-only?

## Recommendation

Ship **Phase 1** on its own — it's low-risk, needs no model or data changes, and
fixes both the "floods the card" clunk and the iOS `needs_input` dead end. Treat
**Phase 2** as a separate, explicitly-scoped project decided on its own merits,
with the action-space allowlist held fixed so "resumable conversation" never
drifts into "open-ended agent."
