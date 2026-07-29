# Auto mode — a delegated todo that *does* the task

Status: **phases 1–2 shipped** — the bounded research loop
(`prefrontal/autorun.py`, handler `auto`, `unattended_tools`) and the question
round-trip (`needs_input`, `POST /todos/{id}/delegate/answers`, inline answers on
the dashboard, `prefrontal todo answer`). Phases 3–5 (delivery contracts, Drive,
third-party sends) are designed here but not built.
Author: drafted with Claude, 2026-07-29

## Question

> Add a todo for X, where that todo involves going off and completing a complex
> task in auto mode, then emailing the results, or putting something on Google
> Drive, etc.

The worked example, which drives most of the design below:

> "Generate a report on whether or not I should get a HELOC for my kitchen
> remodel." This would potentially generate a list of questions — they could be
> answered inline, or maybe the answers are fed back in async by the user — but it
> should be able to be multi-step.

Today's `agent` handler ([`delegation.py`](../../prefrontal/delegation.py)) does
*one* generation: the local model reads the todo (plus any pasted context) and
writes back a brief, draft comms, and action items. It never looks anything up
and it never delivers anything. The ask is the next rung: a todo you hand off and
walk away from, which comes back **done** — with the output somewhere useful.

Three things make that hard, and they're different problems:

1. **Multi-step work with tools** — research, lookups, several calls in sequence.
2. **Work that can't finish without you.** No amount of research answers "should
   *I* get a HELOC": it needs your equity, your rate, your risk tolerance. The run
   has to be able to *ask*, park itself, and pick up when the answers land — which
   may be minutes or days later.
3. **An unattended side effect** — a sent email, a file in Drive — with nobody
   there to confirm it.

The third is the one that can cost trust, so a good part of this document is
about it. The second is the one that decides whether auto mode is useful at all.

## The short answer

**A third delegation handler, not a new subsystem** — and the confirmation moves
*earlier* rather than disappearing.

- The loop is new (`prefrontal/autorun.py`): plan → allowlisted tool calls →
  synthesis, under a hard step and time budget, every step persisted.
- **Asking is a third move.** Alongside "call a tool" and "done," the loop may
  emit `ask` with a small set of questions. The run then parks in `needs_input`
  with its partial write-up already on the todo, and **answering re-runs it** with
  the answers as context. Async by construction — no live session to hold open.
- The delivery is **not** new. It reuses `actions.py`'s allowlist + digest gate.
  What changes is *when* the user says yes: in auto mode they authorize a
  **delivery contract** at delegate time — one tool, one fixed destination —
  instead of confirming a call mid-run. The agent fills in the *content*; it can
  never choose the tool or the recipient.
- Google Drive needs **no integration code**. Point `PREFRONTAL_MCP_SERVERS` at a
  Drive MCP server and allowlist `create_file`. Destinations become config.

Everything else already exists: the handler registry, the `todo_delegations` row
as the job record, the background-thread runner, parked-status semantics, the
slow re-surface cadence, and the inbound loop-closer.

## Why — commandments and guardrails

Read against [`roadmap-vision.md` §2–3](../roadmap-vision.md):

**The guardrail comes first: "don't promise open-ended agentic autonomy."** The
roadmap is explicit — computer-use agents fail roughly two of three real tasks,
so Prefrontal does *scoped, verifiable, API/MCP-based* actions and never drives a
browser. That is a constraint on the *action space*, not on the number of steps.
A loop that makes six typed, allowlisted, audited tool calls is still a bounded
action space; a loop that can call anything is not. So the boundary this design
defends is **which tools exist**, not **how many times we think**.

**M4's stated trap: "a single wrong autonomous send costs the trust the whole
product runs on."** This is why the delivery contract pins the destination up
front. The realistic failure of an autonomous agent isn't "it wrote a bad
paragraph" — it's "it sent the bad paragraph to the wrong person." Fixing the
recipient at authorization time removes the entire class. Content quality
degrades gracefully (a mediocre brief is a mediocre brief); recipient errors do
not.

**2 — Activation energy → zero.** This is the whole point. The dreaded
multi-step admin task is a pure initiation wall, and reminding someone about the
thing they're avoiding doesn't move it. Auto mode is the commandment applied to
its logical end: the activation cost isn't just lowered, it's transferred.

**7 — Never add maintenance burden.** Cuts two ways here. It argues *for* auto
mode (a task you hand off is upkeep removed) and *against* a mid-run confirm
queue: if walking away leaves you with four "approve this step?" prompts to
process later, we've invented the weekly-processing system the commandment
forbids. Hence: pre-authorize, or don't run unattended at all.

**9 — Silence is a feature.** A run produces at most one push, on its terminal
state — the existing `delegation_notice` behavior. No progress chatter.

**4 — Radically forgiving.** A run that hits its budget, loses the model, or
meets something outside its authorization must land somewhere non-punitive and
*useful*: the brief-so-far, plus what it managed to gather. Never a bare "failed."

**Local-first.** A local Ollama model runs the loop when it can. But note the
honest limit: the PR #438 incident (a 58 KB transcript silently truncated by the
default `num_ctx`, an 8B model answering prose instead of JSON) is exactly the
failure profile of a small local model asked to hold a multi-turn tool loop. Auto
mode is therefore the clearest case yet for the opt-in cloud provider — and it
degrades to today's single-shot prep when no capable model is reachable.

## The design

### Lifecycle: reuse the statuses, add one

No new parked semantics. A run lives in the existing lifecycle:

| Status | Meaning for `auto` | Parked? |
|---|---|---|
| `in_prep` | the run is executing | yes (already) |
| `prepped` | run finished, output on the todo | yes (already) |
| `needs_input` | it asked you questions and is waiting | **no** — ball's with you |
| `needs_confirm` *(phase 3)* | it wants an effect outside its authorization | **no** — ball's with you |
| `failed` | budget/model/tool gave out; partial output kept | no (already) |
| `returned` | you marked it done | no (already) |

`in_prep` already means "the agent is still working," is already in
`PARKED_STATUSES`, and already yields a silent check-in with a long safety
fallback. That's precisely right for a run in flight, so a run *in flight* needs
no lifecycle change at all.

The two new statuses share a shape, and it's the important one: **when the run
can't proceed on its own, it stops and hands back something useful** — never a
bare "waiting" and never a guess. `needs_input` is the questions case;
`needs_confirm` is the "it wants an effect the contract doesn't cover" case.
Neither is parked: the ball is with you, same as `returned`/`failed`, so the
existing check-in cadence surfaces them on the item's own cadence.

### The question round-trip

The HELOC report is the general case, not an edge case: any genuinely useful
"should I…" run hits a wall that only the user can clear. So `ask` is a
first-class move in the loop, not an error path.

```
run → {"action": "ask", "questions": [{"text": …, "why": …}, …]}
     → write up what it *has* got, persist the questions, status needs_input
     → (minutes or days later) answers arrive
     → re-run with Q&A folded into the context
```

Design calls, and why:

- **Answering re-runs the loop; it does not resume a suspended one.** There's no
  paused interpreter to revive, no half-open MCP session, nothing to expire. A
  re-run with better inputs also *should* be free to take a different path than the
  blind alley it was down when it stopped. `set_delegation` is already
  `INSERT OR REPLACE`, so re-delegation is the existing, tested mechanism — this
  rides it. The durable state that must survive is small and it's just data: the
  questions and their answers.
- **Async is the default, inline is the fast path.** The same persisted questions
  render as a form on the dashboard card (answer now, while you're looking at it)
  or get answered later — from iOS, or in prose through the NL assistant ("tell it
  my rate is 6.2% and I've got about 180k of equity"). Nothing about the run cares
  which arrived, or when. This is commandment 5's cue-based logic applied to a
  question queue: the answer arrives when you're in front of the thing, not when
  the agent happens to be running.
- **Questions are capped and justified.** A handful, each carrying a one-line
  *why*. Twenty questions is a form, and a form is the thing commandment 2 exists
  to prevent. The cap is also what keeps a stuck model from turning a run into an
  interrogation.
- **Partial output ships with the questions.** The write-up so far goes on the
  todo *before* the questions block. Answering is then a choice, not a toll gate:
  a user who reads the partial report and decides that's enough has still got
  value out of the run.
- **Answers accumulate.** Each round's Q&A stays in the delegation's context, so a
  second round of questions can't re-ask what you already told it.

### The loop: bounded, budgeted, recorded

`prefrontal/autorun.py`, one JSON turn at a time:

```
observation → model emits {"action": "call"|"ask"|"done", …}
           → call: actions.run_action(...)  [allowlisted + unattended-declared]
           → ask:  stop, park in needs_input with the questions
           → append observation, repeat
```

Deliberate choices:

- **A JSON-step loop, not native tool-calling.** It satisfies the existing
  `Generator` protocol (`generate(prompt, system=, num_ctx=, timeout=)`), so
  *either* provider works today with no new client capability, and it reuses
  `llm_json.extract_json_object` and the house pattern of tolerant parsing +
  honest fallback. Native tool-use can replace the inner turn later without
  changing the loop's contract.
- **Budgets are hard.** `max_steps`, a wall-clock deadline, and a per-observation
  character cap. A budget exhaustion is a normal outcome that keeps its partial
  findings — not an error.
- **Every step is persisted** to a new `todo_delegations.steps` JSON column
  (nullable, so the schema-diff migration back-fills it — no hand-written
  `ALTER`). A run that dies at step 4 is inspectable, not vanished. `actions.py`
  independently logs its inert `action` audit episode per call, so there are two
  traces: one per-run, one global.
- **Synthesis reuses `generate_prep`.** The loop gathers findings, appends them to
  the context, and hands off to the existing generator. The brief, drafts, and
  action items therefore come out in the shape every surface already renders —
  dashboard, iOS, check-ins, "add as todo."

### The safety boundary: two gates, not one

`actions.py` today asks one question: *is this tool allowlisted on a configured
server?* Unattended execution needs a second, because "the user may call this
after previewing it" is a weaker claim than "this may fire with nobody watching."

So `McpServerConfig` gains **`unattended_tools`** — a subset of `allowed_tools`
the auto loop may call on its own. Empty by default, which means an existing
deployment gains **zero** new autonomy when this ships: auto mode with no
unattended tools is just today's prep with a loop around it.

The name is honest on purpose. It's tempting to call this `read_only_tools`, but
Prefrontal cannot *verify* that a remote MCP tool is read-only — inferring it
from the tool's name would be a safety hole dressed as a feature. What the config
actually records is an operator's declaration that these are safe to run
unattended. Phase 1 ships with the *guidance* that they should be read-only and
the *mechanism* that nothing is callable unless named.

### Delivery: a contract, authorized before the run

Phase 2. At delegate time the user picks an outcome channel, which resolves to
exactly one tool plus a **fixed destination**, digest-pinned via
`action_digest` before the run starts:

```
{server: "drive", tool: "create_file", arguments: {folder: "Research", …}}
{native: "smtp",  to: "<the user's own address>"}
```

The agent supplies only content-shaped arguments. Recipients, folders, and the
tool itself are frozen at authorization. Defaults stay **inert**: a file in your
own Drive, mail to *yourself*. Anything addressed to a third party stays a draft
and goes out through the existing `preview_send` → `send_prepared_draft` gate,
with a human tap.

This is the same line `mcp_server.py` already draws for `place_call` — it dials
only the caller's own configured number, never an arbitrary recipient. Auto mode
generalizes that rule rather than inventing one.

**Idempotency is per effect, not per run.** `_already_sent` is the right shape
(a durable status+detail pairing that a retry can read) and must generalize, so a
resumed or re-delegated run cannot double-send or double-upload.

### Where it runs

Phase 1 reuses the existing background thread (`app.state.delegation_async`).
One constraint from experience: each thread holds its own SQLite connection, and
per-thread connections against a 256-fd limit are exactly what took the server
down once before. So **one run in flight at a time**. The natural upgrade — and
the thing that also buys crash recovery — is a launchd worker polling for
`in_prep` rows, which the persisted step trail already makes possible.

## Phasing

1. **Research loop, read-only, no delivery.** `auto` handler + `autorun.py` +
   step budget + step trail + `unattended_tools`. Output lands as the brief.
   Zero new outbound risk: it is `agent` with tools.
2. **The question round-trip.** `ask` as a loop move, `needs_input`, persisted
   questions, and the answer path that re-runs with the Q&A in context. This is
   what makes the HELOC-report class of task actually finishable, so it ships with
   phase 1 rather than after it.
3. **One delivery contract:** email-to-self, pre-authorized and digest-pinned.
   Adds `needs_confirm`.
4. **A second channel over MCP** (Drive `create_file`) — proves the contract
   abstraction holds for a non-native destination.
5. **Third-party sends stay human-gated.** Indefinitely, by choice.

## Open questions

- **Which model runs the loop.** Phase 1 accepts whatever the provider resolver
  hands it and degrades honestly. Whether auto mode should *require* a capable
  provider (rather than quietly producing a thin brief on an 8B local model) is a
  real product question, not a technical one. The HELOC example sharpens it: that
  report is only worth reading if the model behind it is good.
- **Where a long report lives.** A brief is a few sentences; "generate a report" is
  pages. The `brief` column will hold it, but the dashboard card and the iOS
  detail view are built for a paragraph. A report probably wants to *be* the
  phase-3 delivery (a Drive doc, an email to yourself) rather than a card.
- **Per-user MCP sources.** MCP servers are operator-level config today, unlike
  SMTP (per-user, encrypted). Multi-tenant auto mode wants the SMTP treatment —
  already an open M4 remainder.
- **Attachments.** A run that produces a file wants somewhere on the todo to put
  it; still the deferred "attachments to todos" item.
- **Should answering re-run automatically, or wait for a tap?** Auto-re-running is
  the zero-friction read of commandment 2; it also spends model time on a run the
  user may have lost interest in. Currently: answering re-runs.
