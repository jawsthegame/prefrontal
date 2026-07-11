# Architectural Review — Prefrontal

*Reviewer's note: a full-codebase architectural review as of the `claude/architectural-review-ghkh3a`
branch. Scope: the `prefrontal/` Python package (~49k LOC, 136 modules), its test suite
(66 files, 1962 passing tests), and the deployment/glue surface. It assesses **structure** —
layering, coupling, abstractions, and the contracts between subsystems — not line-level bugs.
File references are `path:line` against the reviewed tree.*

---

## 1. Executive summary

Prefrontal is a **well-architected codebase**, not a big ball of mud. It has a clear, consistently
applied layering — domain modules → `MemoryStore` (repository mixins) → SQLite — with genuine
abstractions at the two hardest seams: **persistence** (multi-tenant scoping enforced structurally,
zero raw SQL outside the memory layer) and **LLM inference** (a provider protocol, a per-agent
local-vs-cloud resolver, and centralized tolerant JSON parsing). Documentation is unusually
thorough, error hygiene is strong (no bare `except`, no stray `print` outside the CLI), and the
test suite is integration-style against a real in-memory database with the LLM stubbed at an
injected seam — which gives **high confidence for refactoring**.

The debt that exists is concentrated and nameable rather than diffuse. In order of leverage:

1. **The coaching engine ↔ module boundary leaks in both directions** — the engine hardcodes
   specific module names, and several modules reach into the engine's private state. This is the
   single most important structural issue because the module framework is the system's core
   extensibility story.
2. **A handful of god-objects** — `MemoryStore` (17 mixins), `HouseholdRepo` (77 methods),
   `cli.py` (3812 lines), `dashboard.html` (3336 lines) — none rotten, all overgrown, each with a
   clear decomposition seam.
3. **The API contract is only half-typed** — request bodies are rigorously Pydantic-modeled;
   responses are almost entirely hand-built `dict[str, Any]` (6 `response_model=` in the whole
   web layer).
4. **Process gaps that undercut the strong test suite** — no CI runs the tests or the linter, and
   the schema-migration mechanism only handles additive changes.

None of these block the project's current single-operator, self-hosted trajectory. They are the
things that will bite as the module count, the tenant count, or the contributor count grows.

---

## 2. Architecture as built

The README describes "a system of small, focused agents." In practice the runtime shape is a
**layered monolith** with a plugin framework for behaviors:

```
CLI (argparse)  ──┐                      ┌── FastAPI routers (16, thin)
                  ├── shared domain services ──┤
n8n / Shortcuts ──┘   (coaching tick, briefing, │   deps.resolve_user → per-user scoped store
                       panic, assistant, triage) │
                                                 ▼
                       challenge-area MODULES (registry, Module ABC, evaluate())
                                                 ▼
                       MemoryStore  (17 repo mixins, dict-returning)
                                                 ▼
                       SQLite (WAL, per-thread connections, 39 tables)

           integrations/ (Generator protocol → Ollama | Anthropic; ntfy/Pushover/TTS/Twilio delivery)
```

Two entry points (CLI and HTTP) converge on the **same** domain services — most importantly
`run_coaching_tick`, which both `prefrontal coach` and `POST /webhooks/coach/check` call, with
explicit comments in both files that this is to stop them drifting. That convergence is the
codebase's best structural instinct and it is applied to the most important flow.

---

## 3. What the architecture gets right

These are worth naming because they should be **preserved** through any refactor:

- **Structural multi-tenancy.** A store is bound to a `user_id` via `scoped()` (`store.py:179`);
  `_uid()` (`store.py:189`) raises loudly if an unscoped store hits a per-user method, so a call
  site cannot silently scan everyone's rows. The web layer's single `resolve_user` dependency
  (`deps.py:103`) returns a store already scoped to the caller, so handlers *physically cannot*
  read another tenant's data. This isolation is directly tested (`test_multi_tenant.py`).
- **A real persistence boundary.** Repositories return plain `dict`s; there is **zero** inline SQL,
  `.execute(`, or `import sqlite3` anywhere in the domain layer. Storage engine choice is fully
  contained.
- **SQL safety.** Every value is bound via `?`; the few f-strings that reach SQL text interpolate
  internal literals only (allow-listed column names, generated placeholder runs). No user string is
  ever concatenated into SQL.
- **Concurrency engineering.** WAL + `busy_timeout` + one connection per FastAPI worker thread,
  with dead-thread connection reaping to avoid fd exhaustion (`store.py:203-269`, `db.py:45-64`).
  This is the most carefully-reasoned part of the memory layer.
- **The LLM abstraction.** A `Generator` protocol both backends satisfy structurally; a shared
  `ProviderError` base so any call site can fall back regardless of backend; `ProviderResolver`
  as the single home for the `ANTHROPIC_AGENTS` per-agent policy; and `llm_json.extract_json` /
  `integrations/summarize.py` centralizing the tolerant-parse and render→rewrite→fallback idioms
  (whose docstrings document the copy-paste bugs they eliminated). This is textbook.
- **Consistent domain idiom.** `briefing`, `panic`, `encouragement`, `household`, and mail triage
  all follow the same `build_* → render_* → summarize_*` three-layer shape (pure data →
  deterministic markdown → optional LLM prose with heuristic fallback). Strong cohesion, not
  copy-paste.
- **Test strategy.** 1962 tests against a real `MemoryStore.open(":memory:")` with the LLM injected
  via `client=` (never monkeypatched) and an autouse fixture forcing the default model offline.
  Behavior is pinned, internals are not — the suite supports aggressive refactoring.

---

## 4. Cross-cutting findings

Severity reflects architectural risk for the project's stated trajectory (self-hosted, growing
module/tenant count), not immediate breakage.

### 4.1 The coaching engine ↔ module boundary leaks both ways — **High**

The `Module` ABC (`modules/base.py:64`) is a clean, minimal contract, and the tick engine
(`coaching.py`) has good failure isolation for `evaluate()`/hooks. But the abstraction is porous at
exactly the seam that matters:

- **Engine → module (naming).** `run_coaching_tick`'s docstring claims "the engine never names a
  module," yet it imports `is_focus_protected` from `hyperfocus` by name (`coaching.py:729`),
  hardcodes `_PROTECTION_PIERCING = {"self_care", "hyperfocus"}` (`:89`) for which cues may pierce
  focus, and hardcodes `_TRACKABLE_TARGET` (`:452`) for which context keys get channel-outcome
  learning. It also hardcodes `task_paralysis`'s sweeps (`sweep_avoided_decompositions`,
  `sweep_ambiguous_items`, `:746`) inline — even though the `before_collect`/`after_fire` hooks
  were introduced specifically to remove such hardcoding (and `self_care` was migrated to them).
  A new module that needs to pierce focus, get ack-tracking, or run a pre-collection sweep cannot
  do so without editing `coaching.py`.
- **Module → engine (private state).** Modules import the engine-private `_fired_key` to
  self-gate on the engine's own debounce stamp — verified in `impulsivity.py:15` and
  `self_care.py:78` (the modules review also flags `projects` and `delegation_checkin`). There is
  no public "when did this dedup_key last fire?" accessor, so these modules couple to the exact
  `coach_fired:{key}` string format `record_fired` writes. Change the debounce scheme and several
  modules break silently.
- **Not failure-isolated:** the pre-collection sweeps and `is_focus_protected` run *before* the
  try/except-guarded `collect_cues` (`coaching.py:737-755`), so a raise in any of them sinks the
  whole tick — the exact outcome the guards elsewhere exist to prevent.

**Direction:** give the engine a small public surface for what modules currently reach around —
a `Module.pierces_protection` flag, a `Module.tracks_channel_outcome` declaration, a public
`store.last_fired(dedup_key)` accessor, and route the remaining sweeps through `before_collect`.
Then the engine's suppression/learning logic becomes genuinely module-agnostic and the private
imports disappear.

### 4.2 God-objects with clear decomposition seams — **Medium**

- **`MemoryStore` — 17 repo mixins (`store.py:78`).** Decomposed into files but one class exposing
  the entire memory API; repos are not independently usable because of cross-mixin `self.` calls
  (`todos.set_todo_project → self.get_project`, etc.). This is the accepted cost of dropping the
  old store Protocol, but it means the "generic" memory layer knows every module's domain.
- **`HouseholdRepo` — 77 methods, 69 KB (`repos/household.py`).** vs. schedule 30 / todos 34. The
  clearest split candidate: children / facts / agreements / shopping / routines / chores / stars.
  `household/__init__.py` (1050 lines) and `chores.py` (871) are the heaviest domain files.
- **`cli.py` — 3812 lines.** The top level is clean (one `build_parser`, a 3-line `main`), but
  every multi-action command re-dispatches manually via `if args.X_action == "...":` chains — 41
  such branches across 10 namespaces. Promoting these to argparse subparser `func=` bindings is the
  natural seam that shatters the file into ~15 command modules.
- **`dashboard.html` — 3336 lines** of inlined fetch/render/state logic, assembled by
  `str.replace()` on comment tokens (`_common.py:33-114`) standing in for a templating engine, with
  the theme CSS duplicated per page. Defensible for data-free pages served to one operator over
  Tailscale; a liability the moment the UI or the team grows.

### 4.3 The API contract is only half-typed — **Medium/High**

Request bodies are rigorously Pydantic-modeled, but responses are almost entirely hand-built
`dict[str, Any]` — only **6** `response_model=` declarations exist across the whole web layer
(verified: `anchor` ×1, `focus` ×1, `impulsivity` ×3, `ingestion` ×1). Every GET and all of
`schedule`/`todos`/`household`/`admin`/`system` return unvalidated, undocumented dicts. Response
shapes are guaranteed only by tests and discipline; the OpenAPI docs the README advertises are
half-blank. This is the biggest asymmetry in an otherwise disciplined layer.

### 4.4 Misplaced time abstraction — **Medium**

The core timezone primitives — `local_datetime`, `local_hour_of`, `local_day_bounds`,
`local_time_utc` — live in the domain module `scheduling.py:269-312` and are imported *downward*
into the memory layer (`memory/patterns.py:48`) and the engine (`coaching.py:36`), plus 10 other
modules. A dedicated `clock.py` leaf already exists and is the natural home. As written, touching
"how the app tells local time" means importing a domain scheduling module into the persistence
layer — an inverted dependency. Moving these four functions to `clock.py` removes an upward import
from the memory layer and one of the recurring cycle pressures.

### 4.5 Cycle pressure from "modules" that are really domain packages — **Medium**

A challenge-area module is a `Module` subclass *plus* a broad domain surface consumed across the
app: `nudges.py` imports action handlers from `self_care`/`hyperfocus`/`location_anchor`;
`departure.py` and `trips.py` import `haversine_m` from `location_anchor`; `briefing.py` imports
from `impulsivity`; the delivery layer (`integrations/delivery.py:62`) imports button-builders
*up* from `webhooks.notify`. The result is frequent lazy-import-to-dodge-a-cycle (each commented as
such). It works, but the density of cycle-dodges is a symptom that the module/domain layering isn't
cleanly acyclic. Shared geo/action primitives want to live in neutral leaf modules, not inside a
specific module.

### 4.6 Config: centralized and documented, but hand-rolled — **Medium/Low**

A single frozen `Settings` dataclass with ~70 fields, all hand-parsed from strings (no
pydantic-settings), a reimplemented dotenv loader, six bespoke `_parse_*` helpers, and inconsistent
env-var prefixes (`PREFRONTAL_*` vs bare `OLLAMA_URL`/`N8N_*`/`TWILIO_*`). Access *is* centralized
(`get_settings`, few direct `os.getenv` elsewhere) and `.env.example` is excellent — but a
malformed int/bool raises an unfriendly `ValueError` at load with no validation layer.
`pydantic-settings` would collapse the parsers and add validation.

### 4.7 Duplicated dispatch/pattern that invites drift — **Medium**

- **Assistant execute vs. validate.** Validation is a table-driven `_VALIDATORS` registry
  (`assistant.py:1256`) from which `ALLOWED_OPS` is derived so they can't drift — but execution is a
  hand-written 34-branch `if/elif` ladder (`_execute_one`, `:1410-1645`). A validated op with no
  executor branch silently "changes nothing." Make the executor registry-driven too.
- **Action-button builders forked.** Two parallel context-key→button-kind maps exist —
  `delivery.py:_actions_for_cue` (native path) and `routers/coaching.py:_actions` (n8n path) — and
  they already disagree on coverage. One shared `actions_for_cue` should back both.
- **Per-module self-gating cadence** is hand-rolled three times (`projects`, `delegation_checkin`,
  `impulsivity`); belongs in a `Module` base helper (which also fixes 4.1's `_fired_key` leak).

### 4.8 Encryption & auth details — **Medium/Low**

- **Single global encryption key** (`crypto.py`) seals all source secrets (IMAP passwords, ICS
  URLs, Google refresh tokens); no per-tenant key derivation and no rotation path (rotating orphans
  every secret — honestly documented). Fine for one household; a concern as a multi-tenant story
  matures.
- **`get_user_by_token_hash` does `SELECT * FROM users` and loops** (`users.py:91`) on every
  authenticated request, despite `idx_users_token` existing — *and the docstring claims it "goes
  through the indexed lookup,"* which is false. O(n) per auth, and the loop duration leaks the user
  count. A parameterized `WHERE token_hash = ?` uses the index and is still constant-time per row.
- **Signed-link nudge endpoints re-implement identity resolution by hand** (`anchor.py:374-435`),
  a second auth path parallel to `deps.py`.
- **Legacy shared `webhook_secret` grants operator** to the first operator user — a bootstrap
  convenience worth a deprecation path.

### 4.9 Process & tooling gaps — **Medium**

- **No CI for tests or lint.** The only GitHub Actions workflow is `desktop-dmg.yml` (Electron DMG
  build). 66 test files and a configured `ruff` exist but nothing runs them on push/PR — for a
  project "running in daily use," a green suite is currently a local-only guarantee.
- **Migrations are additive-only.** Exactly one versioned step (single-tenant → multi-tenant,
  `MULTI_TENANT_VERSION = 1`); everything after is an idempotent "diff `schema.sql` vs. live DB and
  `ADD COLUMN`" backfill (`migrate.py:112`). There is no forward path for a non-additive change
  (constraint/type/uniqueness change, rename, drop, or a `NOT NULL` column without a constant
  default). The first such change needs net-new machinery.

---

## 5. Lower-severity notes

- Route correctness depends on registering literal routes before path-param routes
  (`/projects/board` before `/projects/{id}`), enforced only by comments; a reordered decorator
  silently breaks routing. This also forces the todo→project endpoint to live in the *todos* router
  for ordering reasons, not domain reasons.
- Silent `try: await request.json() except: {}` on webhook endpoints turns malformed bodies into
  no-ops rather than 400s.
- `RouterServices` is over-provisioned (every router gets the full bundle); `settings` is reachable
  both via the bundle and `request.app.state` and handlers mix the two.
- `MemoryStore` read-modify-write sequences (`award_stars`, `reorder_projects`) aren't wrapped in an
  explicit transaction — safe at human pace under WAL's single writer, technically racy.
- `todo_decompositions` / `todo_delegations` carry no `user_id`; tenancy rides entirely on a
  repeated `_owns_todo` guard + cascade — consistent today, the highest-risk spot for a future leak.
- The `store` fixture is copy-pasted verbatim in ~42 test files despite `conftest.py` already
  hosting `scoped_default`; missing `test_ics.py` / `test_nudges.py`.
- Two interface definitions for a module (`coaching.ModuleLike` Protocol vs `modules.base.Module`
  ABC) can drift; `encouragement` behaves as a module but isn't a `Module` subclass and gets
  bespoke engine special-casing.

---

## 6. Prioritized recommendations

**Tier 1 — structural leverage**

1. **Close the engine↔module boundary (4.1).** Public `store.last_fired()`, module-declared
   `pierces_protection` / `tracks_channel_outcome`, and route remaining sweeps through
   `before_collect`; wrap sweeps in the same per-module try/except. Removes the private-import leak
   *and* the engine's hardcoded module names in one move.
2. **Stand up CI (4.9).** A GitHub Actions job running `pytest` + `ruff` on push/PR. Cheapest
   high-value change; the suite is already strong — make it a gate.

**Tier 2 — contract & decomposition**

3. **Type the responses (4.3).** Add `response_model=` to the read/CRUD endpoints, starting with
   the highest-traffic routers, so the API contract is fully typed and self-documenting.
4. **Split `cli.py` via subparser `func=` bindings (4.2)** and **decompose `HouseholdRepo`** along
   its natural domain lines.
5. **Move the timezone primitives to `clock.py` (4.4)** and lift shared geo/action primitives out
   of specific modules into neutral leaves (4.5) to drain the cycle pressure.

**Tier 3 — consistency & hardening**

6. Make the assistant executor registry-driven (4.7); unify the two action-button maps.
7. Fix `get_user_by_token_hash` to a parameterized indexed lookup and correct its docstring (4.8).
8. Adopt `pydantic-settings` for config validation (4.6); plan a versioned migration ladder before
   the first non-additive schema change (4.9).

---

## 7. Bottom line

The fundamentals are genuinely good: a real persistence boundary with structural tenant isolation,
a clean LLM provider abstraction, a consistent domain idiom, and a large behavior-focused test
suite that makes refactoring safe. The debt is concentrated at the coaching engine↔module seam and
in a few overgrown-but-healthy god-objects, plus two process gaps (no CI, additive-only
migrations). Address Tier 1 and the system's core extensibility story — "add your own module" —
becomes true in structure and not just in the README.
