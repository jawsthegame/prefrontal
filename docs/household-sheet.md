# Shared household sheet — design spec

A shared, always-current "everything about the kids" sheet for two co-parents —
sizes, routines, allergies, providers, standing behavior plans, and upcoming
appointments — that either parent can read at a glance and update in plain
English, and that the system keeps *visible* rather than buried in one parent's
head.

Its reason to exist is **balancing invisible load**. In most households one
parent carries the mental inventory (shoe size, who the dentist is, the sticker
plan) while the other is willing but oblivious. A shared note helps a little; the
win here is that every entry is **stamped with who set it and when**, and the
coaching layer **pushes the deltas that matter** to the *other* parent — so the
sheet is a load-balancer, not a passive doc. The data model is taken directly
from a real co-parent tracker (the *Kids Info Tracker* spreadsheet,
[`kids-info-tracker-template.xlsx`](kids-info-tracker-template.xlsx)), whose own
tagline states the goal: *"everything you need to step in and help — any time, no
asking required."* Its nine sheets map directly onto the model below: Overview /
Clothing & Shoes / Schedules / Food & Meals / Health / School → **facts**;
Behaviour Plans → **agreements**; Key Contacts → a facts facet; Shopping Lists →
child-tagged todos (v1.1).

This is the concrete driver for **household scope** and the backbone of the
**Parent** Context Pack (see [`../ROADMAP.md`](../ROADMAP.md)). It builds on
multi-tenancy ([`multi-tenant.md`](multi-tenant.md)), the NL assistant
(`prefrontal/assistant.py`), commitments, and the coaching agent
([`coaching-agent.md`](coaching-agent.md)).

---

## 1. Goal & non-goals

**Goals**

- One shared, household-scoped store of **facts** (per-kid key/values) and
  **agreements** (standing behavior plans), readable and writable by *both*
  parents.
- Every write carries **provenance** (`updated_by`, `updated_at`) — the raw
  material for making load legible and for the delta digest.
- **Plain-English editing** through the existing assistant ("Sam's shoe size is
  13 now", "add a dentist appt for Sam next Tuesday 3pm") with the same
  validate → preview → confirm safety as todo/commitment edits.
- A single **visible sheet** rendered into the existing `/family` view — reusing
  the profile-render pattern, *not* a new bespoke UI.

**Non-goals (for v1)**

- No new front-end framework. The sheet is server-rendered Markdown/HTML in the
  family view.
- No real-time collaboration / operational-transform merge. Household data is
  low-volume and human-paced; **last-write-wins + provenance** is enough.
- No self-serve invite flow. Household membership is operator-set in v1 (§8).
- No per-parent-private fields. Everything in a household is visible to both
  members (that's the point).
- The proactive **delta digest** to the other parent is **v2** (§7); v1 ships the
  visible sheet.

---

## 2. Decision: a `household` scope alongside the per-user scope

Multi-tenancy scopes every row to one `user_id` via `MemoryStore.scoped(user_id)`
so no read/write can leak across people. A household sheet is the deliberate
*exception*: two users must see the **same** rows. Rather than weaken per-user
scoping, we add a **second, parallel scope**.

- A new `households` table; `users` gains a nullable `household_id`. A user
  belongs to **0 or 1** household (v1).
- The new tables (`children`, `household_facts`, `household_agreements`) carry
  **`household_id NOT NULL`** instead of `user_id`. They are reached through
  household-scoped store methods that resolve the caller's `household_id` from
  their user row and inject it into every statement — the exact mirror of
  `_uid()`/`.scoped()`, on the household column.
- A user with **no** household calling a household method gets a loud
  `RuntimeError` (like the unscoped-store guard), never a silent empty read.

This keeps the "structural scoping, no call site can forget the `WHERE`" property
that multi-tenancy established — just on `household_id` for these tables.

---

## 3. Schema changes

New tables are added to `schema.sql` (idempotent `CREATE TABLE IF NOT EXISTS`).
The one change to an **existing** table — `users.household_id` — is a later-added
column, so it rides the unified migration back-fill
(`prefrontal/memory/migrate.py::_ADDED_COLUMNS` / `backfill_added_columns`); it is
nullable, so there is no data migration.

> **`child_id` convention.** SQLite treats `NULL`s as distinct in a `UNIQUE`
> constraint, which would let duplicate household-wide rows through. So
> household-wide facts/agreements use the sentinel **`child_id = 0`** (not `NULL`)
> and every table defaults `child_id` to `0`. A real child is a positive
> `children.id`.

### 3.1 `households`

```sql
CREATE TABLE IF NOT EXISTS households (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### 3.2 `users.household_id` (added column)

```sql
ALTER TABLE users ADD COLUMN household_id INTEGER REFERENCES households(id);
```

| Column | Type | Description |
|---|---|---|
| `household_id` | INTEGER NULL → `households(id)` | The household this user co-parents in, or `NULL` (not in one). |

### 3.3 `children`

The roster. Stable identity only (name + birthday); everything else is a fact.

```sql
CREATE TABLE IF NOT EXISTS children (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id),
    name         TEXT NOT NULL,
    birthday     DATE,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (household_id, name)
);
```

### 3.4 `household_facts`

The categorized key/value grid — the template's *Category / Item × Child* sheets
(Overview, Clothing & Shoes, Schedules, Food & Meals, Health, School).

```sql
CREATE TABLE IF NOT EXISTS household_facts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id),
    child_id     INTEGER NOT NULL DEFAULT 0,       -- 0 = household-wide
    category     TEXT NOT NULL,                    -- see FACT_CATEGORIES
    item         TEXT NOT NULL,                    -- "shoe size", "wake-up time"
    value        TEXT,
    updated_by   INTEGER REFERENCES users(id),
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (household_id, child_id, category, item)
);
```

| Column | Type | Description |
|---|---|---|
| `child_id` | INTEGER | `children.id`, or `0` for a household-wide fact. |
| `category` | TEXT | A small controlled vocab (`sizes`, `routine`, `food`, `health`, `school`, `contact`), mirroring `todos.KNOWN_CATEGORIES`' single-source-of-truth pattern. |
| `item` | TEXT | The specific field, normalized (lowercased/trimmed) like `normalize_category`. |
| `value` | TEXT | Free text (`"13"`, `"7:15am"`, `"peanuts — EpiPen in backpack"`). |
| `updated_by` / `updated_at` | — | Provenance. The whole point. |

### 3.5 `household_agreements`

The *Behaviour Plans* sheet — standing plans both parents follow. Light
structure: a plain-language `body` plus an optional `structured` JSON for the
star/points charts (thresholds → rewards, earn-only rules).

```sql
CREATE TABLE IF NOT EXISTS household_agreements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id),
    child_id     INTEGER NOT NULL DEFAULT 0,       -- 0 = whole household
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'consistency',  -- reward | consistency | routine
    body         TEXT,                             -- the plan in plain language (Markdown)
    structured   TEXT,                             -- optional JSON, e.g. star thresholds
    updated_by   INTEGER REFERENCES users(id),
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (household_id, child_id, title)
);
```

Example `structured` for a star chart (from the template):

```json
{"unit": "star", "earn_only": true,
 "thresholds": [{"stars": 10, "reward": "small Lego set"},
                {"stars": 30, "reward": "big Lego set"}]}
```

### 3.6 What reuses existing tables (no new schema)

- **Appointments** (dental/doctor) → **`commitments`**, tagged with the `child`
  `kind` and a `health` category. The sheet just *surfaces* the upcoming ones;
  the calendar/commitment machinery is unchanged.
- **Rendering** is deterministic assembly (cheap), so — unlike `profile_cache` —
  there is **no cache table**; the sheet is built on request.

> **Deferred to v1.1 (noted, not built here):** *Key Contacts* and *Shopping
> Lists* from the template. Contacts fit `household_facts` (`category='contact'`,
> `item=role`) for v1; a dedicated `household_shopping` list (item · child · spec
> · where-to-buy · got-it, household-scoped) is the natural follow-on. Shopping
> is intentionally **not** shoehorned into per-user `todos` — those are
> per-user-scoped and carry scheduling weight a shared checklist doesn't want.

---

## 4. Store: household-scoped access

Add household methods to `MemoryStore`, resolving the scope from the bound user.

```python
def _household_id(self) -> int:
    """The caller's household id, or raise (mirrors _uid())."""
    hid = self.get_user_field("household_id")   # from the user's row
    if hid is None:
        raise RuntimeError("store's user is not in a household")
    return hid

# facts
def set_fact(self, *, child_id, category, item, value, updated_by) -> None
def clear_fact(self, *, child_id, category, item) -> bool
def facts(self) -> list[dict]                 # all, for the render
# agreements
def set_agreement(self, *, child_id, title, kind, body, structured, updated_by) -> int
def remove_agreement(self, agreement_id) -> bool
def agreements(self) -> list[dict]
# roster
def add_child(self, *, name, birthday=None) -> int
def children(self) -> list[dict]
```

Every method injects `WHERE household_id = self._household_id()`. `set_fact`
upserts on the composite key and stamps `updated_by`/`updated_at` — the same
upsert shape as `set_state`, on the household scope. Belongs in a new
`prefrontal/memory/repos/household.py` mixin (the repo layer split in
[`multi-tenant.md`](multi-tenant.md)/the store decomposition).

---

## 5. Editing in plain English (assistant ops)

The assistant (`prefrontal/assistant.py`) already turns a message into a
validated, previewable action list against a whitelist (`ALLOWED_OPS`). Extend it
with household ops; the security model (whitelist + per-scope store + preview →
confirm) is unchanged.

| New op | Fields | Notes |
|---|---|---|
| `set_fact` | `child`, `category`, `item`, `value` | `child` resolved to a `children.id` from the snapshot (or `0`); `category` validated against the controlled vocab; `item` normalized. |
| `clear_fact` | `child`, `category`, `item` | Removes a fact. |
| `set_agreement` | `title`, `kind?`, `body`, `child?`, `structured?` | `kind` ∈ {reward, consistency, routine}. |
| `remove_agreement` | `agreement_id` | Id resolved from the snapshot. |
| `add_commitment` | *(existing)* | Gains optional `child` + `kind='self'`→`child` for appts. |

- **Snapshot.** `build_snapshot` gains the household's children (id/name), fact
  categories, and open agreements so the model resolves "Sam" / "the dentist
  plan" to real ids and never invents one (same discipline as today's
  todo/commitment snapshot).
- **Validation.** `updated_by` is always the acting user (never model-supplied);
  values are length-capped; an unknown `child`/`category` is dropped with a
  human-readable reason, exactly like the existing `_ActionError` path.
- **Provenance for free.** Because the acting user is known, every LLM-driven
  edit is attributed — which is what powers §7.

---

## 6. The visible sheet (`/family` render)

Assemble the household's data into a single rendered section in the existing
`/family` view (server-rendered; no new UI framework). Deterministic — built on
request from `facts()` / `agreements()` / `children()` / upcoming child
`commitments`.

Sections, in order:

1. **Recently changed** *(the load-balancing surface)* — the last N facts/appts
   by `updated_at`, each showing *what · who · when* ("Sam shoe size → 13 ·
   updated by Dana · 2d ago"). This is the first thing the oblivious parent sees.
2. **Per-child facts** — grouped by category (sizes, routine, food, health,
   school), one block per child.
3. **Standing agreements** — behavior plans, star charts rendered from
   `structured` (progress + next reward) with the plan `body`.
4. **Upcoming appointments** — child commitments in the near window, with who (if
   known) is on pickup.

Reachable at `GET /family` (already exists) for both parents; optionally a
`GET /household/sheet` JSON for machine clients / the widget.

---

## 7. Balancing the load (v2 — the delta digest)

The visible sheet is passive; the differentiator is **push**. Once the coaching
agent ([`coaching-agent.md`](coaching-agent.md)) is the delivery path, add a
household cue source:

- On a tick, compare each member's "last seen" stamp against recent
  `household_facts`/appointment changes and near-term child appts, and emit a cue
  to the member(s) who **haven't** touched them: *"3 things changed since
  Tuesday: Sam's shoe size, the new sticker plan, dentist Thu 3pm — you're on
  pickup."*
- A periodic **balance view** built from `updated_by` counts ("this month: Dana
  updated 14, Alex 1") — gentle, not accusatory; opt-in and tone-calibrated like
  the encouragement layer ([`encouragement.md`](encouragement.md)).

This reuses the existing cue → channel → debounce machinery; no new delivery
mechanism.

---

## 8. Household membership (v1: operator-set)

- `create_household(name) -> id` and `set_user_household(handle, household_id)` on
  the unscoped (operator) store; surfaced via the `prefrontal` CLI
  (`prefrontal household add`, `prefrontal household join <handle>`) and an
  operator endpoint, mirroring `provision_user` / `POST /admin/users`.
- v1 assumes an operator (one of the parents, or the deployer) wires the two
  users into one household once. A self-serve invite/accept flow is **open**
  (§10).

---

## 9. Touch list (where the work lands)

| Area | Change |
|---|---|
| `prefrontal/memory/schema.sql` | `households`, `children`, `household_facts`, `household_agreements`; `users.household_id`. |
| `prefrontal/memory/migrate.py` | `users.household_id` in `_ADDED_COLUMNS` (back-fill). |
| `prefrontal/memory/repos/household.py` | New repo mixin: `_household_id()`, facts/agreements/children methods. |
| `prefrontal/memory/store.py` | Mix in the household repo; `set_user_household`, `create_household` on the unscoped store. |
| `prefrontal/household.py` | Pure render (`build_sheet()` → structured; `render_sheet()` → Markdown), mirroring `briefing.py`/`panic.py`. |
| `prefrontal/assistant.py` | New ops in `ALLOWED_OPS`; snapshot + validators + executors. |
| `prefrontal/webhooks/…` | `/family` render section; optional `GET /household/sheet`; operator household endpoints. |
| `prefrontal/cli.py` | `prefrontal household add/join/show`. |
| `docs/schema.md` | Document the four new tables + the added column. |

---

## 10. Open questions

- **Membership model.** Operator-set (v1) vs. a self-serve invite/accept flow.
  Can a user belong to more than one household (v1: no)? What happens to a
  household's rows when a member leaves?
- **Children identity.** A `children` roster (this spec) vs. a free-text child
  label on facts. The roster is cleaner for appointments and per-child agreements
  but adds a table and an add-child step.
- **Conflict handling.** Last-write-wins + provenance is the v1 call; is a visible
  "Dana changed this 5 min ago" ever needed, or is that overkill at co-parent
  scale?
- **Contacts & shopping (v1.1).** Contacts-as-facts vs. a `household_contacts`
  table; a dedicated `household_shopping` list vs. reusing todos.
- **Privacy.** v1 makes everything household-visible. Is there ever a need for a
  per-parent-private annotation, or does that undermine the shared-sheet premise?

---

## 11. Rollout sequence

1. Schema + migration (`households`, `children`, `household_facts`,
   `household_agreements`, `users.household_id`) + the household repo, with unit
   tests: two users in one household see the same rows; a user with no household
   raises; two households don't leak.
2. Operator wiring (`create_household`, `set_user_household`, CLI/endpoint).
3. Pure render (`prefrontal/household.py`) + the `/family` sheet section.
4. Assistant ops (`set_fact`/`set_agreement`/…) with validation + snapshot.
5. *(v2)* the coaching-agent delta digest + balance view.

Ship 1–4 as the visible, plain-English-editable shared sheet; 5 turns it into the
active load-balancer.
