# Design: Projects — a user-created container for todos, commitments & focus sessions

**Status:** proposal · **Author:** (drafted with Claude) · **Date:** 2026-07-10

## Motivation

Today Prefrontal has no way to say *"these todos, these calendar commitments, and
this week's focus sessions all belong to the **Kitchen Remodel**."* Work is grouped
only by two thin, free-text axes:

- **`domain`** (`work`/`home`/`kids`/…) — a fixed "life sphere" label on
  `todos`, `commitments`, and `trips`. Not on `focus_sessions`.
- **`category`** — free-text, **todos-only**, derived vocabulary capped at 20.

The only cross-entity FK is `focus_sessions.todo_id` (a session optionally points at
one todo). Nothing lets a user create an arbitrary, named grouping that spans the
three planning entities.

A **project** is that container: a named, user-created grouping that a todo, a
commitment, and a focus session can each belong to.

## Decision: projects are nested under a domain

Each project **belongs to exactly one `domain`** (its `domain` column is required).
A project is a finer grain *within* a life sphere — "Kitchen Remodel" lives under
`home`, "Q3 Launch" under `work`. This keeps projects consistent with the work/life
guardrails the scheduler already enforces via `domain`, and gives a project a
sensible default time-band without inventing a parallel axis.

Consequence: when a todo/commitment is assigned to a project, its own `domain` is
**inherited from the project** (and kept in sync if the project moves domains). The
per-row `domain` field stays as the source of truth the scheduler reads — assigning
a project just writes through to it — so no scheduler code has to learn about
projects to respect the guardrails.

## Current state (grounding)

- **DB:** SQLite, no ORM. Single source of truth is
  `prefrontal/memory/schema.sql` (idempotent `CREATE TABLE IF NOT EXISTS`).
- **Migrations:** `prefrontal/memory/migrate.py`. `backfill_added_columns()` diffs
  every live table against `schema.sql` and `ALTER TABLE ADD COLUMN`s whatever is
  missing. **A new nullable column or a new table requires only a `schema.sql`
  edit** — no hand-written migration step (as long as any added column is nullable
  or has a constant default, which `project_id` is).
- **Data access:** repository pattern in `prefrontal/memory/repos/`
  (`todos.py`, `schedule.py` = commitments, `sessions.py` = focus sessions).
- **API:** FastAPI routers in `prefrontal/webhooks/routers/`
  (`todos.py`, `schedule.py`, `focus.py`).
- **UI:** server-rendered static pages + JSON API; main surface is
  `prefrontal/webhooks/dashboard.html`.

## Data model

### New table (add to `schema.sql`)

```sql
-- User-created grouping across todos/commitments/focus sessions. A project is
-- nested under exactly one `domain` (work/home/kids/…) — the finer grain within a
-- life sphere. Assigning an entity to a project writes the project's domain through
-- to that entity's own `domain` column, so the scheduler's work/life guardrails
-- keep working without knowing projects exist.
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    name        TEXT    NOT NULL,
    description TEXT,                                -- short user blurb; matched (with name) to auto-suggest a project for triaged items
    domain      TEXT    NOT NULL,                    -- the life sphere this project sits under
    notes       TEXT,                                -- longer free-text detail (not used for matching)
    color       TEXT,                                -- optional UI accent (hex/token)
    status      TEXT    NOT NULL DEFAULT 'active',   -- active | archived
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_projects_user_status ON projects (user_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_user_name
    ON projects (user_id, name) WHERE status = 'active';   -- no two live projects share a name
```

`projects` must also be added to `migrate.py`'s `_USER_TABLES` tuple so a legacy
single-tenant DB gets the same `user_id` backfill treatment as the other per-user
tables (defensive — fresh installs get it from `schema.sql` directly).

### New FK columns (nullable — add to each existing `CREATE TABLE`)

```sql
-- todos:          project_id INTEGER REFERENCES projects(id)
-- commitments:    project_id INTEGER REFERENCES projects(id)
-- focus_sessions: project_id INTEGER REFERENCES projects(id)
```

All three are **nullable** → unassigned is the default and every existing row is
valid. `backfill_added_columns()` adds them to live DBs automatically.

Focus sessions can derive their project two ways: explicit `project_id`, or via
their linked `todo_id`'s project. Store `project_id` explicitly (denormalized) so a
session started against a todo still records the project even if the todo is later
reassigned — mirror the existing choice to snapshot rather than always-join.

## Repo methods

New `prefrontal/memory/repos/projects.py` (mirrors the shape of `todos.py`):

- `create_project(user_id, name, domain, notes=None, color=None) -> int`
- `list_projects(user_id, *, include_archived=False) -> list[Row]`
- `get_project(project_id) -> Row | None`
- `update_project(project_id, **fields) -> bool`
- `archive_project(project_id) -> bool`  (soft delete; keeps history intact)
- `project_rollup(project_id) -> {open_todos, next_commitment, focus_minutes_7d, …}`

Assignment setters (on the existing repos, mirroring `set_todo_domain`/`set_todo_category`):

- `TodosRepo.set_todo_project(todo_id, project_id)` — also writes the project's
  `domain` through to the todo.
- `ScheduleRepo.set_commitment_project(commitment_id, project_id)` — likewise; and,
  like `notes`/`hidden`/`domain`, **survives calendar re-sync** (it's a user field).
- `SessionsRepo.set_session_project(session_id, project_id)`.

When a project's `domain` changes, cascade-update the `domain` of its assigned
todos/commitments (single `UPDATE … WHERE project_id = ?`).

## API (new router `prefrontal/webhooks/routers/projects.py`)

CRUD:

- `POST   /projects`                     — create `{name, domain, notes?, color?}`
- `GET    /projects`                     — list (`?include_archived=1`), each with rollup counts
- `GET    /projects/{id}`                — detail + its todos / upcoming commitments / recent sessions
- `PATCH  /projects/{id}`                — rename / re-notes / recolor / **change domain** (cascades)
- `POST   /projects/{id}/archive`        — soft delete

Assignment (mirror the existing `/todos/{id}/domain` endpoint style — `ScopedRequest`
+ `resolve_user`, 404 on missing entity):

- `POST /todos/{todo_id}/project`             `{project_id | null}`
- `POST /commitments/{commitment_id}/project` `{project_id | null}`
- `POST /focus/{session_id}/project`          `{project_id | null}`

Validation: a project's `domain` is normalized with the same
`normalize_focus_domain()` (`prefrontal/focus_balance.py`) that commitments already
snap to on write (`commitments.py:421`), so projects share one canonical vocabulary
with the rest of the system. Name uniqueness among active projects returns 409 (same
pattern as the category cap).

Add the corresponding Pydantic models to `prefrontal/webhooks/schemas.py`
(`ProjectCreate`, `ProjectUpdate`, `EntityProjectUpdate`) and register the router in
`create_app`.

## UI (`dashboard.html` + JSON API)

Phase-appropriate, minimal:

1. A **Projects** section/tab on the dashboard listing active projects with rollup
   badges (open todos · next commitment · focus minutes this week), colored by
   `color`, grouped under their `domain` heading.
2. A **project picker** on the todo/commitment create+edit rows (a dropdown of the
   user's active projects, filtered to — or defaulting from — the row's domain).
3. A **project detail view**: the project's open todos, upcoming commitments, and
   recent focus sessions on one page (this is the payoff of the container).

## Auto-suggesting a project for triaged items

When triage turns an email/signal into a todo (or commitment), it should suggest the
best-matching project, matched against each project's **name + `description`**. This
mirrors the existing category inference exactly.

**Where.** `prefrontal/triage.py::_route_signal` (`triage.py:380`) already calls
`augment_todo(...)` then `store.add_todo(...)` on the `todo` route (and
`upsert_commitment` on the `commitment` route). The suggestion slots in right there;
`mail/ingest.py` has the two parallel todo-creation sites (`ingest.py:136`, `:348`)
that reuse the same helper.

**How — mirror category inference.** A new pure function in a `projects` helper:

```python
def suggest_project(title, notes, projects, *, client) -> tuple[int | None, float]:
    """Best-matching project id for a triaged item, plus confidence in [0,1].

    Two layers, exactly like todos.augment_todo / triage's classifier:
      1. LLM (one generate_json call): the item text + a compact list of
         `- id: name — description` lines → {"project_id": <id|null>, "confidence": <0..1>}.
      2. Keyword-overlap heuristic over name+description tokens as the fallback
         when the model is unavailable (mirrors todos.heuristic_category).
    Returns (None, 0.0) when nothing clears the threshold — projects are opt-in,
    an unmatched item stays unassigned.
    """
```

- Reuses `generate_json()` (`prefrontal/llm_json.py:44`) — the same
  tolerate-a-provider-failure helper the rest of the system uses. `description` is
  the field that carries the matching signal ("permits, contractor quotes, tile" →
  *Kitchen Remodel*); `notes` is not fed to the matcher.
- Only **active** projects are candidates, and (given projects are nested under a
  domain) candidates can be pre-filtered to the item's inferred domain, or left
  domain-open and let the model choose — recommend domain-open with domain shown in
  the list, since a triaged item's domain is itself inferred and may be wrong.

**Propose vs apply.** The triaged todo is *created regardless* (the `todo` route
already applies). On a **confident** match (≥ threshold) set the new todo's
`project_id` directly — it's a suggestion the user sees on the dashboard and can
clear/reassign in one tap via `POST /todos/{id}/project`. Below threshold, leave it
unassigned. This keeps a single `project_id` column (no pending-suggestion column)
while honoring "easily overridable." If we later want an explicit
review-before-apply step, add a nullable `suggested_project_id` — noted as a
follow-up, not built in v1.

## Behavior / integration notes

- **Scheduler:** unchanged. Because assignment writes the project's `domain` through
  to each entity's own `domain`, `window_for_todo` (`scheduling.py`) and the
  work/life guardrails keep working with zero project awareness.
- **Focus balance / stats:** `focus_sessions.project_id` unlocks per-project
  time-spent rollups (`focus_balance.py`, `stats.py`) — natural follow-on, not
  required for v1.
- **Archive, don't delete:** archiving preserves the FK so historical todos/sessions
  keep their project label; the unique-name index only applies to active rows, so a
  name can be reused after archiving.
- **Household:** out of scope. Projects are per-user (`user_id`); shared/co-parent
  projects would be a later extension mirroring the `household_*` tables.

## Suggested phasing

- **Phase 1 — data + API + suggestion:** `projects` table (with `description`),
  three nullable FKs, `projects` repo, CRUD + assignment endpoints, schemas, and the
  `suggest_project()` helper wired into triage's `todo` route. Fully usable via
  CLI/API. (Migration is free — just the `schema.sql` edit.)
- **Phase 2 — dashboard:** projects list, pickers on todo/commitment rows, detail
  view, and surfacing the triage-suggested project inline.
- **Phase 3 — rollups & insight:** per-project focus-minutes and follow-through
  stats; "you haven't touched *Kitchen Remodel* in 3 weeks" style nudges.

## Decisions

1. **Single membership.** A todo/commitment/session belongs to at most one project
   (a single nullable `project_id`) — no many-to-many join table. *(confirmed)*
2. **Nested under one domain.** Each project has a required `domain`; assignment
   writes that domain through to the entity so the scheduler stays project-unaware.
   *(confirmed)*
3. **Archive, not delete.** Archiving preserves FKs and leaves assigned todos in
   place; the unique-name index only covers active rows, so a name frees up.
4. **Forced priority rank.** Active projects carry a contiguous `rank` (1..N, no
   ties) — a single stack-rank *across* domains, set by dragging on the board.
   Forcing distinct ranks makes the priority objective: you can't mark everything
   "high". New projects append at the bottom; archiving compacts the rest.
   *(confirmed)*
   - **Feeds "what to fit in" softly.** `fit_todos` sorts by deadline → todo
     priority → **project rank** → shortest. A deadline still wins and a todo's own
     priority still outranks its project's rank, so a project-less todo is judged
     purely on deadline+priority (only ranked-project todos get the boost, and only
     as a tiebreak). Rank isn't denormalized onto todos (it changes on every
     reorder); the fit/now/briefing surfaces opt into an `open_todos(...,
     with_project_rank=True)` left-join instead, keeping the scheduler otherwise
     project-unaware. A *hard* rank-first order (project rank ahead of todo
     priority) was rejected — it can't coexist with "orphans judged by priority"
     without either sinking orphans or creating a non-transitive sort.

## Open questions / follow-ups (tracked as issues)

- Phase 2 — dashboard surface (list, pickers, detail view, surface suggestion). → #442
- Phase 3 — per-project focus-minutes & follow-through rollups, staleness nudges,
  optional project `deadline`. → #443
- Explicit review-before-apply for triage suggestions (a `suggested_project_id`
  pending column) vs. the v1 confident-auto-assign. → #444
- Shared / household (co-parent) projects, mirroring `household_*`. → #445
