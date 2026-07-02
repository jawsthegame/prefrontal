# Coaching agent — design spec

Status: **partially built** (rollout §12 steps 1 + 2 + 3 + both v1 evaluators).
Shipped: the pure engine (`prefrontal/coaching.py` — `Cue`, `CoachContext`,
`Decision`, `collect_cues`, `choose_channel`, `suppressed`/debounce+quiet-hours,
`decide`), the per-module `evaluate()` hook (`modules/base.py`, default `[]`), the
`TaskParalysisModule.evaluate` producer, the `LocationAnchorModule.evaluate`
producer (the `outing/check` per-outing decision + side effects lifted into a
shared `evaluate_outing` / `apply_outing_evaluation` that both the endpoint and
the evaluator call — the legacy endpoint is byte-identical), a `prefrontal coach
[--dry-run]` CLI, and the **`POST /webhooks/coach/check`** tick endpoint (§7) with
its `deploy/n8n/coach-check.workflow.json` delivery workflow. **Not yet built:**
the optional LLM phrasing pass (§5), outcome-logging correlation ids (§8), the
encouragement layer (§9), and deprecating `outing/check` (§13) — those remain
intended design. This is the implementation spec
for the **Coaching Agent** —
the component in the README architecture that sits between the memory layer and
the delivery layer and turns *what Prefrontal knows* into *the right message, on
the right channel, at the right moment*. It supersedes the one-line "Coaching
agent" bullet in [`ROADMAP.md`](../ROADMAP.md) ("generate reminders/check-ins
from the profile — the morning briefing is the first slice of this") with
concrete abstractions, an API, and a rollout. If this and the roadmap disagree,
this file wins.

```
  Memory Layer          ← SQLite: episodes, patterns, coaching state
        ↓
  Coaching Agent        ← THIS SPEC: decides what to say, when, on which channel
        ↓
  Delivery Layer        ← Pushover / Ntfy / TTS / Twilio (n8n today)
```

---

## 1. Goal & non-goals

**Goal.** A single decision engine that, on a tick or an event, asks every
enabled module "do you have anything to say right now?", and for each thing
worth saying decides **whether to fire now**, **what to say**, and **on which
channel** — then hands a ready-to-deliver message to the delivery layer and logs
the outcome so the next decision is better calibrated. It is the engine that
finally *executes* the `Intervention`s that modules only *declare* today
(`modules/base.py:30`; most ship `status="planned"`).

**The two slices that already exist** (this spec generalizes them, it does not
replace them):

- **Morning briefing** (`briefing.py`) — a *scheduled digest*. The
  `build → render → summarize` shape (deterministic structure, optional Ollama
  prose, heuristic fallback) is exactly the phrasing pattern the coaching agent
  adopts for every message.
- **Outing escalation** (`/webhooks/outing/check`, `app.py:711`) — an
  *event/elapsed-time* coaching loop: poll → compute level → decide `fire` →
  build `message` → return it for n8n to deliver → log an episode on return. This
  is the coaching agent in miniature, hard-wired to one module. The spec lifts
  this decision shape out of the endpoint into a reusable engine and a
  module-provided evaluator.

**Non-goals (v1).**

- **No new delivery transport.** Delivery stays in n8n (poll an endpoint, read a
  `message`/`channel`, send via Pushover/Ntfy/Twilio). A first-class Python
  delivery layer remains "beyond v1" in the roadmap. The coaching agent *chooses*
  the channel; n8n still *sends*.
  *(Update: the first-class Python delivery layer has since shipped —
  `prefrontal/integrations/delivery.py`, exercised by `prefrontal coach
  --deliver`. It's an **addition**, not a replacement: n8n delivery still works,
  and the engine's channel-choice/suppression contract here is unchanged. The
  client just gives the box a native, n8n-free path that renders ntfy's action
  buttons and honors per-user routing (§6.5).)*
- **No always-on Python scheduler.** Cadence is driven by n8n cron + the existing
  poll model, mirroring `outing/check` and the briefing workflow. The agent is a
  pure decision function plus a thin HTTP/CLI surface — no background threads.
- **No cross-module conversation or memory of dialogue.** A cue is one-shot:
  decide, deliver, log. Multi-turn check-ins ("did that help?") are noted as a v2
  in §11.
- **No change to how patterns are learned.** The agent *reads* the profile and
  patterns and *writes* outcome episodes; the learning pass (`patterns.py`) is
  unchanged.

**Success test.** With `task_paralysis` and `location_anchor` enabled, a single
scheduled `POST /webhooks/coach/check` (a) returns the outing escalation cue it
returns today, unchanged, now routed through the shared channel/phrasing path,
and (b) returns a new `tiny_first_step` cue for a stalled open todo — each with a
chosen channel, a grounded message, and, once delivered, an episode logged so
`channel_response` learns whether it worked.

---

## 2. The core abstraction: a `Cue`

Everything the agent produces is a **cue** — one thing worth saying, with enough
metadata for the agent to decide *if* and *how* to deliver it. This is the
generalization of the per-outing dict that `outing/check` returns today.

```python
@dataclass(frozen=True)
class Cue:
    """One coaching message a module wants to deliver."""
    module: str            # owning module key, e.g. "location_anchor"
    intervention: str      # the declared Intervention.name, e.g. "firm_nudge"
    urgency: str           # "ambient" | "nudge" | "urgent" | "critical"
    text: str              # the deterministic message (phrasing layer may rewrite)
    context_key: str       # for channel_response learning, e.g. "departure" / "todo"
    dedup_key: str         # stable id so we fire a given thing once (debounce, §6)
    ref: dict              # opaque payload for outcome logging (outing_id, todo_id…)
    suggested_channel: str | None = None  # module hint; agent may override (§5)
```

`urgency` is a small fixed ladder, deliberately decoupled from any one module's
notion of "level":

| urgency | meaning | default channel floor |
|---|---|---|
| `ambient` | nice to know, never interrupts | digest only (folded into briefing) |
| `nudge` | a normal reminder | push (Pushover/Ntfy) |
| `urgent` | time-critical, must be seen | push, escalates to sound |
| `critical` | will be missed otherwise | voice (Twilio), bypasses quiet hours |

The outing ladder maps onto this: `soft → nudge`, `firm → urgent`,
`call → critical`. Mapping in one place keeps "escalation is not optional" a
property of the *agent*, not of each module.

---

## 3. Module evaluator hook (the structural change)

Modules declare interventions today but contain no code that decides *when* one
is due — that logic lives inline in `outing/check`. We add **one optional method**
to `Module` (`modules/base.py`) so a module can answer "what's due right now?":

```python
class Module(ABC):
    def evaluate(self, store: MemoryStore, ctx: "CoachContext") -> list[Cue]:
        """Return cues that are due given current memory + context. Default: none."""
        return []
```

- `CoachContext` carries the request-time facts a module needs without each one
  re-reading them: `now` (naive UTC, like `impact.utcnow()`), optional
  `current_lat`/`current_lon`, and the cached `coaching_state`/`bias` the agent
  already loaded. (`outing/check` reads exactly these today — they become the
  context object.)
- A module's `evaluate` is **pure-ish**: it reads the store and may *advance its
  own one-fire state* (as `outing/check` calls `set_outing_level`), but it must
  not deliver — it only returns cues. This keeps delivery and channel choice in
  the agent, where escalation policy and learning live.
- Default returns `[]`, so the four still-`planned` modules light up
  incrementally: implementing a module's coaching is now "fill in `evaluate` +
  flip its `Intervention.status` to `active`", with nothing else to wire.

**First two evaluators (v1):**

- `LocationAnchorModule.evaluate` — *move* the body of the per-outing loop in
  `outing/check` (lines 760–829) here: location-gate, abandon-close, compute
  level, emit a `firm`/`soft`/`call` cue with the impact phrase. The endpoint
  becomes a thin call into the agent (§7). Behavior is identical; the code just
  has a home.
- `TaskParalysisModule.evaluate` — finally *fire* its already-declared
  `tiny_first_step` intervention (`modules/task_paralysis.py:37`, today
  `status="planned"`): for a stalled `open_todos` item, emit a `nudge` cue that
  reframes it as a <5-minute first action — using the first sub-step from
  `store.get_decomposition` when one exists. This is the new capability the
  success test exercises; flip the intervention to `status="active"` with it.
  *(Avoidance detection has since shipped: its `avoided_todos` signal — "open
  N days and skipped" (`prefrontal/todos.py`, `GET /todos/avoided`) — is the
  natural input for picking *which* stalled todo to surface, so the evaluator
  reads it instead of a plain staleness threshold.)*

---

## 4. The agent loop

`prefrontal/coaching.py` holds the pure core (testable, no I/O beyond the store):

```python
def collect_cues(store, modules, ctx) -> list[Cue]:
    cues = []
    for m in modules:
        cues.extend(m.evaluate(store, ctx))   # never let one module break the rest
    return cues

def decide(store, cues, ctx) -> list[Decision]:
    out = []
    for cue in cues:
        if suppressed(store, cue, ctx):        # debounce + quiet hours (§6)
            continue
        channel = choose_channel(store, cue, ctx)   # §5
        text    = phrase(store, cue, ctx)            # §5 (deterministic here)
        out.append(Decision(cue=cue, channel=channel, text=text, fire=True))
    return out
```

- One module raising must not sink the tick: `collect_cues` wraps each
  `evaluate` in a try/except that logs and skips (a coaching miss is better than
  a dead loop), mirroring how the registry tolerates bad module keys.
- `decide` returns `Decision`s; the HTTP layer serializes them, n8n delivers, and
  acknowledged/ignored outcomes flow back as episodes (§8).

---

## 5. Channel selection & phrasing

**Channel — deterministic, learned-pattern-aware.** `choose_channel(store, cue, ctx)`:

1. Start at the **urgency floor** (§2 table).
2. Consult the `channel_response` patterns (`patterns.py` already derives these
   per channel) — if the user reliably ignores `push` after their
   `responsive_hours_end`, bump a `nudge`/`urgent` up a rung or pick a
   better-responded channel. This is the README's "which notification channel you
   ignore after 3pm" made real.
3. Respect a `critical` cue's right to escalate to voice regardless.
4. Honor explicit per-user routing fields when present (the multi-tenant spec's
   `pushover_user_key`/`twilio_to`/`ntfy_topic` in `coaching_state`,
   [`docs/multi-tenant.md`](multi-tenant.md) §6.5) — the agent emits the *channel
   class*; the delivery fields say *where*.

**Phrasing — deterministic core + optional LLM, with fallback.** Exactly the
briefing/summarizer pattern (`summarize_briefing`, `briefing.py:244`):

- `cue.text` is the deterministic, testable message (per-intervention templates,
  e.g. `build_message` for the anchor — reused as-is).
- An optional `phrase_llm(cue, profile)` rewrites it through Ollama using the
  **behavioral profile as system context** (`build_profile` /
  `summarize_profile`, `memory/summarizer.py`) so the tone is calibrated to the
  user and grounded in real numbers — falling back to `cue.text` on any
  `OllamaError` or empty reply. Off by default for `nudge`/`urgent` (latency on a
  time-critical path is bad); on for `ambient`/digest. Controlled by a
  `coach_llm_phrasing` coaching-state key.

The system prompt reuses the existing voice ("Prefrontal's … voice for someone
with ADHD … warm, second-person … use the numbers as given; do not invent
events") so all surfaces sound like one assistant.

---

## 6. Don't over-nudge: debounce + quiet hours

The single biggest failure mode of a coaching agent is becoming noise (there's
already a `cta-debounce` track in flight). `suppressed(store, cue, ctx)`:

- **Debounce / fire-once** — keyed on `cue.dedup_key`. A small
  `coaching_log` table (or a reuse of `coaching_state` for the last-fired
  timestamp per key) records when a cue last fired; the same cue won't refire
  within `coach_debounce_minutes` (per-urgency default). This generalizes the
  anchor's `last_level` "fire once per level" guard so every module gets it free.
- **Quiet hours** — outside `responsive_hours_start`..`responsive_hours_end`
  (existing coaching keys), suppress `ambient`/`nudge` entirely and downgrade
  `urgent` to "hold until the window opens" — *except* `critical`, which always
  goes through (a missed hard commitment at 6am still warrants the call). This is
  the inverse of the briefing already honoring `preferred_briefing_format`.
- **Rate ceiling** — a per-tick and per-day cap on non-critical cues so a bad day
  (many avoided todos + slips) doesn't produce a barrage; overflow folds into the
  next briefing as `ambient`. Cap lives in a coaching key.

---

## 7. API & CLI surface

**`POST /webhooks/coach/check`** (`tags=["coaching"]`) — the tick endpoint n8n
polls, the direct sibling of `outing/check`:

- Auth: `X-Prefrontal-Token` via `_verify_token` (and a scoped store under the
  multi-tenant spec — the agent takes a store, so it inherits scoping for free).
- Body (optional): `current_lat`/`current_lon` (forwarded into `CoachContext` so
  the anchor's location-gating keeps working).
- Response: `{ "cues": [ { module, intervention, urgency, channel, fire, text,
  ref, delivery? } ] }`. n8n acts on `fire == true`, sends `text` on `channel`,
  then POSTs the outcome back (§8). The `delivery` block (per-user routing
  fields) appears under the multi-tenant spec.

**`/webhooks/outing/check` is refactored, not removed.** Its loop body moves into
`LocationAnchorModule.evaluate`; the endpoint either (a) stays as a thin
anchor-only wrapper for the existing n8n workflow, or (b) is folded into
`coach/check` with the workflow updated. **Recommend (a) for v1** — zero
deployment change — and deprecate it once `coach/check` is wired
(`docs/deployment.md` note). Its response shape is preserved.

**CLI** — `prefrontal coach [--dry-run] [--user <handle>]`: runs one tick and
prints the decisions (or, with `--dry-run`, the cues *before* suppression/channel
choice) for debugging and for a cron that prefers CLI over HTTP. Mirrors
`prefrontal briefing`. `--user`/fan-out semantics match the multi-tenant spec §7.

---

## 8. Closing the loop: outcome logging

Coaching only improves if outcomes come back. Every fired cue is delivered with a
correlation id; n8n (or an iOS Shortcut tap, the existing `/webhooks/shortcut`
path) reports the result, and the agent logs an **episode** so the learning pass
folds it into `channel_response` and `drift`:

- `acknowledged=True/False`, `channel`, `context=cue.context_key`,
  `outcome="success"|"miss"` — the same `log_episode` contract the anchor uses on
  return (`record_outing_return`, `location_anchor.py:404`).
- This is what makes "learn from failure, not just success" true for *all*
  coaching, not just outings: an ignored nudge is a `channel_response` miss that
  shifts future channel choice (§5.2).
- No new learning code — `patterns.py` already derives `channel_response` from
  episodes; it just gets more (and more varied) episodes to chew on.

---

## 9. The encouragement / recovery layer (folds in here)

The roadmap's "Encouragement & recovery layer" is **a special cue source**, not a
separate system. A detector reads signals the agent already has in `CoachContext`
(briefing's "what slipped", rising `drift`, a missed *hard* commitment, repeated
long outings); past a threshold it emits a single `ambient`/`nudge` cue that
shifts tone from nudging to reassurance + one concrete next step (re-fit todos
via the existing `fit_todos`). Opt-in via a coaching key, tone-calibrated through
the same LLM phrasing path (§5) with a heuristic fallback. It is literally
another `evaluate`-style producer; no new plumbing.

> **Reconciliation with [`docs/encouragement.md`](encouragement.md).** That spec
> details the same feature but proposes shipping it **standalone first** — a pure
> `prefrontal/encouragement.py` core plus a `GET /encouragement` endpoint — rather
> than as a coaching-agent cue, because the coaching agent isn't built yet and the
> encouragement layer can land independently. The two are not in conflict on
> *behavior* (same signals, same deterministic-plan-then-optional-prose shape);
> they differ only on *where it plugs in*. Intended path: build it standalone per
> `encouragement.md`, then, when the coaching agent lands, wrap `assess_day` as one
> more `evaluate`-style cue source so delivery/debounce/channel choice route
> through the shared engine. Whichever ships, there should be **one** `assess_day`
> implementation, not two.

---

## 10. Touch list (where the work lands)

| Area | Files | Change |
|---|---|---|
| Core engine | new `prefrontal/coaching.py` | `Cue`, `CoachContext`, `Decision`, `collect_cues`, `decide`, `choose_channel`, `phrase`, `suppressed` |
| Module hook | `modules/base.py` | add `evaluate(store, ctx) -> list[Cue]` defaulting to `[]` |
| Anchor evaluator | `modules/location_anchor.py` | move `outing/check`'s per-outing decision body into `evaluate`; map level→urgency |
| Initiation evaluator | `modules/task_paralysis.py` | new `evaluate` firing `tiny_first_step` over a stalled `open_todos` item; flip its `Intervention.status` to `active` |
| Endpoint | `webhooks/app.py` | new `POST /webhooks/coach/check`; refactor `outing/check` to call the anchor evaluator |
| Phrasing | reuse `memory/summarizer.py` + `briefing.py` voice | shared system prompt + Ollama-with-fallback helper |
| Debounce state | `memory/schema.sql`, `memory/store.py` | `coaching_log` (or last-fired keys) + fire-once/debounce helpers |
| CLI | `cli.py` | `prefrontal coach [--dry-run] [--user]` |
| Outcome path | `webhooks/app.py` (`/webhooks/shortcut`, n8n) | correlation id in/out, `log_episode` on report |
| Delivery | `deploy/n8n/*.workflow.json`, `docs/deployment.md` | a `coach-check` workflow polling the new endpoint; route by `channel` |
| Pure cores | `impact.py`, `scheduling.py`, `patterns.py` | **no change** — read by evaluators as-is |
| Tests | `tests/` | evaluator, channel-choice, debounce/quiet-hours, phrasing-fallback, outcome-logging, endpoint |

---

## 11. Testing strategy

The deterministic core makes this almost entirely model-free:

- **Evaluator parity:** a golden test that the refactored `LocationAnchorModule.
  evaluate` produces byte-identical cues to today's `outing/check` loop across the
  soft/firm/call/at-home/abandoned cases (lock in "no behavior change").
- **Channel choice:** with seeded `channel_response` patterns + responsive hours,
  assert a `nudge` after `responsive_hours_end` bumps channel; a `critical` always
  picks voice; explicit routing fields are honored.
- **Debounce / quiet hours:** the same cue twice within the window fires once;
  `ambient`/`nudge` suppressed outside responsive hours; `critical` never
  suppressed; the daily rate ceiling caps non-critical cues.
- **Phrasing fallback:** Ollama down → `Decision.text == cue.text`; up → rewritten
  prose, profile passed as system context (same harness as `summarize_briefing`).
- **Outcome loop:** a reported "ignored" cue writes a `channel_response` miss
  episode that the next `learn` shifts the channel on.
- **Endpoint:** auth required; `current_lat`/`current_lon` reach the anchor;
  response shape stable.

Existing briefing/outing tests keep passing — the briefing is untouched and the
outing endpoint's response is preserved.

---

## 12. Rollout sequence

1. **Engine + hook, no callers.** Add `coaching.py` (`Cue`/`decide`/
   `choose_channel`/`suppressed`) and `Module.evaluate` default. Pure unit tests.
   Nothing fires yet.
2. **Anchor parity.** Move the `outing/check` body into
   `LocationAnchorModule.evaluate`; endpoint calls it. Golden parity test green →
   deploy with **zero** observable change.
3. **`coach/check` endpoint + n8n workflow** polling it, anchor-only at first.
   Delivery routes by `channel`. Now the agent owns the outing nudges end-to-end.
4. **Avoidance evaluator** + debounce/quiet-hours. First *new* coaching capability
   ships (the success-test cue).
5. **LLM phrasing on ambient/digest**, then encouragement layer as another
   evaluator.

Steps 1–2 are invisible; 3 swaps the transport without changing behavior; 4–5
are where the user feels new coaching.

---

## 13. Open questions

- **One tick endpoint vs. keep `outing/check` forever.** Recommend folding into
  `coach/check` after step 4 and deprecating `outing/check`, but the location
  workflow is the one live feature — keep the old endpoint until the new path has
  run clean for a while. Flag, don't rush.
- **Where debounce state lives.** A dedicated `coaching_log` table (queryable,
  good for a future "what did you tell me today?" view) vs. reusing
  `coaching_state` last-fired keys (no schema change). Lean `coaching_log` if we
  want the audit trail; `coaching_state` if we want the smallest diff. Decide at
  step 4.
- **Cue priority when the rate ceiling bites.** When more cues are due than the
  per-tick cap allows, what wins — highest urgency, then oldest `dedup_key`? Needs
  a deterministic ordering so tests are stable; propose `(urgency desc, module
  registration order, dedup_key)`.
- **Per-user coaching cadence.** A household may want different responsive hours /
  quiet hours per person — already per-user under the multi-tenant spec's
  per-user `coaching_state`, so no new work, but the n8n poll is global; confirm a
  single tick fanning out over users (multi-tenant §7) is enough.
- **Synchronous LLM on the critical path.** Even with fallback, an Ollama call
  inside a polled endpoint adds latency. Proposal: phrase `ambient`/digest cues
  with the model, keep `nudge`/`urgent`/`critical` on deterministic templates, and
  revisit only if prose is wanted on nudges.
