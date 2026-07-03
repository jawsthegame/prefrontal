# Triage agent — design spec

Status: **shipped** (was: proposed). This is the implementation spec for the
**Triage Agent** that sits in the [README architecture](../README.md#architecture)
between the ingestion layer and the memory layer ("classifies, prioritizes,
routes"). It shipped in six slices — classifier core → `triage_log` schema +
store → `apply` → wiring (`/webhooks/n8n`, `POST /triage`, `GET /triage/recent`)
→ briefing surface + dashboard panel → n8n templates — landing in
`prefrontal/triage.py`, `memory/repos/triage.py`, and the ingestion router. It
superseded the one-line "Triage agent" entry in [`ROADMAP.md`](../ROADMAP.md);
this file is the design of record.

---

## 1. Goal & non-goals

**Goal.** Turn the inbound-event stub
([`parse_inbound_event`](../prefrontal/integrations/n8n.py) behind
`POST /webhooks/n8n`) into a real triage step: given a raw inbound *signal* —
an email, a calendar change, an n8n event, a manual capture — decide three
things and act on them:

1. **Classify** — what kind of thing is this, and is it actionable?
2. **Prioritize** — how urgent is it (does it need a nudge now, today, or never)?
3. **Route** — where does it go: the memory layer (an episode, a todo, a
   commitment, a coaching-state update), the delivery layer (an n8n event), or
   the floor (drop it)?

The first concrete ingestion source is **mail monitoring** (README: "aggregates
personal email accounts into a normalized stream, triages by urgency, and
surfaces what needs action"), but triage is source-agnostic: anything that can
be normalized into a `Signal` flows through the same path.

> **Reality note.** The general `Signal` / `TriageDecision` / `triage_log` core
> described here is now **built** (`prefrontal/triage.py`): `/webhooks/n8n`
> classifies + routes, `POST /triage` takes a normalized signal directly, and
> `GET /triage/recent` explains what it did. The older **mail** path
> (`prefrontal/mail/`: `normalize_message` → Ollama+heuristic triage → a `todos`
> row in `mail_messages`) still runs as its own slice — in effect this spec's
> `route=todo` hard-wired to one source. The two now coexist; a remaining
> follow-up is to **absorb** the mail path into a `Signal` adapter feeding the
> shared `classify`/`apply` (its `mail_messages.todo_id` already mirrors
> `routed_ref`), rather than running two parallel triages.

**Non-goals (v1).**

- **Not** a new inference engine or a long-running daemon. Triage is a pure
  function plus a thin store/delivery shim, invoked per inbound request — same
  shape as [`augment_todo`](../prefrontal/todos.py) and
  [`build_briefing`](../prefrontal/briefing.py), not a background loop.
- Mail *fetching* is optional either way. An in-Python IMAP fetcher has since
  shipped (`prefrontal mail fetch --account …`, `prefrontal/mail/imap.py`, with
  `MAIL_IMAP_*_<ACCOUNT>` env vars) for hosts that prefer it; n8n polling (or a
  Google Apps Script digest) remains an equally-supported path. Either way triage
  itself consumes already-normalized signals. Local-first still holds: an IMAP
  fetch reaches only *your* mail server, and nothing is written back.
- **No** auto-reply, auto-archive, or any write back to the user's inbox. Triage
  reads signals and writes to *Prefrontal's* memory/delivery only.
- **No** new model dependency. Triage classifies heuristically by default and
  uses the **already-injected** Ollama client for the hard cases, exactly like
  todo augmentation — with a deterministic fallback so a down model never drops
  a signal (§6).

**Success test.** An inbound email signal "Dentist confirmation: Tue 3pm" is
classified `commitment`, parsed to a dated `commitment` row, and surfaces in the
next briefing; a newsletter is classified `noise` and dropped with a logged
reason; an email "Re: overdue invoice — please pay today" is classified
`action` at `high` urgency, written as a deadline-bearing `todo`, and fires one
`triage.urgent` n8n event — all with the model off (heuristic path) and again
with it on, deterministically in tests.

---

## 2. Where it sits

Today the inbound path is a classify-only stub:

```
n8n workflow ──POST /webhooks/n8n──▶ parse_inbound_event(body)
                                       └─▶ {event, handled: False, payload}   # TODO: route
```

`parse_inbound_event` already carries the TODO this spec discharges: *"Route
recognized events to real handlers — e.g. an `episode` event should write to the
memory layer, a `state` event should update coaching state — and set
`handled=True` accordingly."* Triage **is** that router, generalized from n8n
events to any normalized signal:

```
ingestion (n8n mail poll / calendar sync / shortcut / Apps Script)
        │  normalized Signal
        ▼
  triage.classify(signal, store, ollama?)  ──▶ TriageDecision
        │                                        ├─ kind, urgency, route, reason
        ▼                                        └─ confidence, source ("heuristic"|"llm")
  triage.apply(decision, store, n8n)
        ├─ store.add_todo / upsert_commitment / log_episode / set_state
        ├─ n8n.trigger("triage.urgent", …)   # only when urgency warrants a nudge
        └─ drop (noise) — logged, never silent
```

The triage *core* (`classify`) is pure and model-optional and lives in a new
`prefrontal/triage.py`; the *apply* step is the only part that touches the store
and n8n, mirroring how `briefing.py` keeps `build_briefing` pure and
`summarize_briefing` eff+model-bearing.

---

## 3. Data model

### 3.1 `Signal` — normalized input

A frozen dataclass; ingestion adapters build it, triage consumes it. Source
adapters (mail, calendar, shortcut) each map their raw payload onto these
fields so the classifier never sees provider-specific shapes.

```python
@dataclass(frozen=True)
class Signal:
    source: str            # "mail" | "calendar" | "shortcut" | "n8n" | "manual"
    title: str             # subject line / event title / short capture
    body: str = ""         # email body, event notes, etc. (may be empty)
    sender: str = ""       # from-address / origin, for sender-trust heuristics
    received_at: str = ""  # ISO8601; defaults to utcnow() at apply time
    external_id: str = ""  # provider id for idempotency (e.g. gmail msg id)
    meta: dict[str, Any] = field(default_factory=dict)  # raw extras, untyped
```

### 3.2 `TriageDecision` — output

```python
@dataclass(frozen=True)
class TriageDecision:
    kind: str          # see §4.1
    urgency: str       # see §4.2
    route: str         # see §5 — the action to take
    reason: str        # one line, human-readable, always populated
    confidence: float  # 0.0–1.0
    source: str        # "heuristic" | "llm" — which path decided (cf. augment_todo)
    fields: dict[str, Any] = field(default_factory=dict)  # extracted slots (deadline, when, …)
```

`reason` is mandatory and surfaced in `GET /triage/recent` and the dashboard so a
dropped or down-ranked signal is always explainable — triage never silently
eats input (echoing the roadmap's "no silent caps" instinct).

---

## 4. Taxonomy

### 4.1 `kind` — what is this signal?

| `kind` | Meaning | Typical route (§5) |
|---|---|---|
| `commitment` | A dated/timed event — appointment, meeting, flight | `commitment` |
| `action` | An open loop needing work but not clock-pinned | `todo` |
| `outcome` | Evidence about something already predicted (e.g. "left at 9:15") | `episode` |
| `preference` | The user stating how they want to be coached | `state` |
| `info` | Worth surfacing once, but no action and no memory write | `surface` |
| `noise` | Newsletters, receipts, automated cruft | `drop` |

This deliberately maps onto the memory layer that already exists
(`commitments` → `commitment`, `todos` → `action`, `episodes` → `outcome`,
`coaching_state` → `preference`) so triage has somewhere concrete to route to on
day one. It generalizes the stub's own examples (`episode` event → memory,
`state` event → coaching state).

### 4.2 `urgency` — how soon does it matter?

| `urgency` | Meaning | Delivery behavior |
|---|---|---|
| `now` | Needs a nudge in the next minutes (overdue, imminent departure) | fire `triage.urgent` n8n event |
| `today` | Belongs in today's plan | no immediate nudge; lands in the briefing |
| `later` | Future-dated or low priority | stored, no nudge |
| `none` | `info`/`noise` — nothing to schedule | no nudge |

Urgency is decoupled from `kind`: a `commitment` two weeks out is `later`; an
`action` with "today" in the body is `today`; an `outcome` is almost always
`none`. The mapping is the classifier's job (§6), not a fixed function of kind.

---

## 5. Routing table

`route` names the side effect `triage.apply` performs. Each route is a thin call
into the **existing** `MemoryStore` / `N8nClient` API — triage adds no new
storage primitives.

| `route` | Action | Store / delivery call |
|---|---|---|
| `commitment` | Create a calendar item | `store.upsert_commitment(...)` (external_id = signal id, so re-delivery is idempotent — reuses the calendar-sync dedup path) |
| `todo` | Create an open loop, then **augment** it | `augment_todo(title, …, client)` → `store.add_todo(...)` (so inferred estimate/priority/energy/deadline come for free) |
| `episode` | Log an outcome | `store.log_episode(...)` |
| `state` | Update a coaching preference | `store.set_state(key, value, source="inferred")` |
| `surface` | Hold for the next briefing only | append to a lightweight `triage_log` (§7); no core-table write |
| `drop` | Discard | `triage_log` only, with `reason` |

When (and only when) `urgency == "now"`, `apply` additionally fires
`n8n.trigger("triage.urgent", {...})` — reusing the outbound stub that already
backs escalations, so delivery routing stays in n8n where it lives today. A
down/!configured n8n is a no-op and never breaks the capture path (existing
`N8nResult` contract).

---

## 6. The classifier: heuristic-first, model-assisted

Triage follows the **exact** two-layer pattern the codebase already uses twice —
`todos.augment_todo` (LLM JSON with keyword-heuristic fallback, recording a
`field → "stated"|"llm"|"heuristic"` source map) and
`briefing.summarize_briefing` (Ollama prose over a deterministic base, falling
back on `OllamaError`).

```python
def classify(signal: Signal, *, client: _Generator | None = None) -> TriageDecision:
    base = _heuristic_classify(signal)          # always runs; deterministic, testable
    if client is None or base.confidence >= HEURISTIC_TRUST:
        return base                             # cheap path / model disabled
    refined = _llm_classify(signal, client)     # one JSON call; may raise/return junk
    return refined or base                      # fall back to heuristic on any failure
```

- **`_heuristic_classify`** — keyword + structural rules, in the spirit of
  `heuristic_priority` / `heuristic_deadline`:
  - sender-trust: known automated senders (`no-reply@`, bulk list headers in
    `meta`) → `noise`/`none`;
  - date/time tokens in title/body + words like "appointment", "meeting",
    "confirmed" → `commitment`, with `heuristic_deadline`-style parsing into
    `fields["when"]`;
  - imperative/overdue language ("please", "due", "overdue", "today",
    "ASAP") → `action`, urgency bumped by the same date math;
  - first-person outcome phrasing ("I left", "made it", "missed") → `outcome`;
  - "remind me", "I prefer", "stop …ing me" → `preference`.
  Confidence is rule-strength-based and capped below `HEURISTIC_TRUST` for the
  genuinely ambiguous, so those — and only those — pay for a model call.
- **`_llm_classify`** — one non-streamed `client.generate` call returning strict
  JSON (`kind`, `urgency`, `reason`, optional `fields`), coerced/validated like
  `_coerce_llm` in `todos.py`; any `OllamaError`, timeout, or malformed JSON →
  return `None` → heuristic wins. The injected-client seam means tests pass a
  fake generator and assert deterministic decisions with the model "on".

This keeps the **local-first** promise (model off → still fully functional) and
adds zero new infra: the same `OllamaClient.from_settings()` the summarizer and
briefing already use.

---

## 7. API & schema surface

### 7.1 Wiring the existing route

`POST /webhooks/n8n` stops being a classify-only echo. It builds a `Signal` from
the body, runs `classify` (with the app's Ollama client if available), calls
`apply`, and returns the decision with `handled=True` — discharging the stub's
TODO. `parse_inbound_event` is retained as the body→`Signal` normalizer.

### 7.2 New routes

- `POST /triage` *(tag: `ingestion`, auth: token)* — classify+apply a single
  signal posted directly (lets the iOS Shortcut and the Apps Script mail digest
  post normalized signals without going through n8n). Body = `Signal` fields;
  response = `TriageDecision`.
- `GET /triage/recent?limit=N` *(tag: `ingestion`)* — the last N decisions with
  `reason`, for the dashboard and for explainability ("why was this dropped?").

Both go through `_verify_token` like every other inbound route, and resolve the
store via the `get_store` dependency.

### 7.3 Schema: one new table

Triage routes into the existing core tables; it needs only an audit/idempotency
log of its own. Added to [`schema.sql`](../prefrontal/memory/schema.sql)
(idempotent, same as every other table):

```sql
CREATE TABLE IF NOT EXISTS triage_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  DATETIME NOT NULL,
    source       TEXT NOT NULL,           -- Signal.source
    external_id  TEXT,                     -- for idempotent re-delivery
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    urgency      TEXT NOT NULL,
    route        TEXT NOT NULL,
    reason       TEXT NOT NULL,
    confidence   REAL NOT NULL,
    decided_by   TEXT NOT NULL,            -- "heuristic" | "llm"
    routed_ref   TEXT                      -- e.g. "todo:42", "commitment:7", or NULL on drop
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_triage_external
    ON triage_log(source, external_id) WHERE external_id <> '';
```

The partial unique index makes re-delivery of the same email a no-op (triage
checks it before routing), so a flaky n8n poll that double-sends never creates
two todos. `routed_ref` ties each decision back to the row it created, which is
what `GET /triage/recent` and the dashboard render. This stays compatible with
the multi-tenant spec: when that lands, `triage_log` takes a `user_id` column
and the index becomes `(user_id, source, external_id)` like every other per-user
table in [`multi-tenant.md`](./multi-tenant.md) §3.2.

---

## 8. Config

Two new settings on `Settings`
([`config.py`](../prefrontal/config.py)), both with safe defaults so an
unconfigured deployment behaves exactly as today:

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `triage_use_llm` | `PREFRONTAL_TRIAGE_LLM` | `true` | When false, skip `_llm_classify` entirely (pure heuristic) — useful when Ollama is busy or absent |
| `triage_drop_threshold` | `PREFRONTAL_TRIAGE_DROP` | `0.0` | Below this confidence, route `info` instead of `drop`, so low-confidence "noise" is surfaced rather than discarded |

No new credentials: mail access lives in n8n/Apps Script, never in `.env`.

---

## 9. Touch list (where the work lands)

| File | Change |
|---|---|
| `prefrontal/triage.py` *(new)* | `Signal`, `TriageDecision`, `classify`, `_heuristic_classify`, `_llm_classify`, `apply` |
| `prefrontal/integrations/n8n.py` | `parse_inbound_event` → returns a `Signal` (or a normalizer beside it); drop the `handled=False` stub note |
| `prefrontal/webhooks/app.py` | Wire `/webhooks/n8n` to classify+apply; add `POST /triage`, `GET /triage/recent` |
| `prefrontal/memory/schema.sql` | `triage_log` table + partial unique index |
| `prefrontal/memory/store.py` | `log_triage(...)`, `recent_triage(limit)`, `triage_seen(source, external_id)` |
| `prefrontal/config.py` + `.env.example` | `triage_use_llm`, `triage_drop_threshold` |
| `prefrontal/briefing.py` | Pull `route == "surface"` items into the briefing's "needs attention" section |
| `prefrontal/webhooks/dashboard.html` | A "Triage" panel showing recent decisions + reasons |
| `deploy/n8n/` | A mail-poll → `POST /triage` workflow; a `triage.urgent` → Pushover workflow |
| `ROADMAP.md` | Collapse the "Triage agent" bullet to "shipped — see `docs/triage-agent.md`" |

The pure cores it reuses — `augment_todo`, `heuristic_deadline`,
`OllamaClient`, `MemoryStore`, `N8nClient` — are unchanged.

---

## 10. Testing strategy

- **Heuristic table tests** — a corpus of representative signals (dentist
  confirmation, overdue invoice, newsletter, "I left at 9:15", "stop texting me
  after 3pm") asserted to a fixed `(kind, urgency, route)`, model **off**. This
  is the contract; it must pass with no Ollama.
- **LLM path with a fake generator** — inject a `_Generator` stub returning
  canned JSON (and malformed JSON, and one raising `OllamaError`) to prove
  refinement, coercion, and fallback-to-heuristic — same technique as the
  existing `augment_todo` / `summarize_briefing` tests.
- **Routing/apply tests** — against an in-memory store: each `route` creates
  exactly the expected row; `drop`/`surface` create none; `urgency == "now"`
  fires exactly one n8n event (assert via a fake `N8nClient`).
- **Idempotency** — posting the same `external_id` twice yields one row and the
  second response reports the prior decision.
- **Endpoint tests** — `/webhooks/n8n` returns `handled=True`; `/triage` and
  `/triage/recent` honor the token; unauthenticated calls 401.

---

## 11. Rollout sequence

1. `triage.py` core + heuristic classifier + table tests (no wiring yet).
2. `triage_log` schema + store methods + idempotency.
3. `apply` + LLM path (fake-generator tests) — still only callable in tests.
4. Wire `/webhooks/n8n`; add `POST /triage` + `GET /triage/recent`.
5. Briefing "surface" integration + dashboard panel.
6. n8n mail-poll workflow + `triage.urgent` delivery workflow; mark the roadmap
   item shipped.

Each step is independently shippable and leaves the system working; nothing
before step 4 changes any existing endpoint's behavior.

---

## 12. Open questions

- **Where does mail normalization live** — an n8n Function node, or a small
  Google Apps Script digest (the roadmap mentions the latter)? Either way the
  contract is the `Signal` JSON posted to `/triage`; the choice is operational,
  not a code decision here.
- **Should `outcome` triage be allowed to *create* episodes**, or only annotate
  existing predicted ones? v1 proposes create-on-strong-match, but the
  false-positive cost (polluting the learning signal) argues for gating it
  behind higher confidence than the other kinds.
- **`surface` retention** — do surfaced-but-unactioned items expire, or sit in
  `triage_log` indefinitely? Proposed: the briefing only pulls `surface` items
  from the last 24h; older ones age out of view without deletion.
- **Sender-trust list** — hard-coded heuristics vs. a learned `coaching_state`
  list of muted senders. Start hard-coded; promote to learned once there's
  episode data on which surfaced items the user actually acts on.
