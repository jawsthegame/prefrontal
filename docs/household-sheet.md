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
the shared **`household_shopping`** checklist (§3.6.4).

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
- A single **visible sheet** rendered at the **`/kids` dashboard** (with a calm
  read-only partner glance at `/family`), backed by `GET /household/sheet` —
  reusing the profile-render pattern, *not* a new bespoke UI.

**Non-goals (for v1)**

- No new front-end framework. The sheet is server-rendered Markdown/HTML in the
  family view.
- No real-time collaboration / operational-transform merge. Household data is
  low-volume and human-paced; **last-write-wins + provenance** is enough.
- ~~No self-serve invite flow.~~ **Shipped** — a member invites a co-parent with a
  shareable code/link; the operator path also remains (§8).
- No per-parent-private fields. Everything in a household is visible to both
  members (that's the point).
- ~~The proactive **delta digest** to the other parent is **v2**.~~ **Shipped** —
  the daily delta digest (§3.6.2) and the load-balance view (§3.6.3) both push/
  surface what the other parent changed.

---

## 2. Decision: a `household` scope alongside the per-user scope

Multi-tenancy scopes every row to one `user_id` via `MemoryStore.scoped(user_id)`
so no read/write can leak across people. A household sheet is the deliberate
*exception*: two users must see the **same** rows. Rather than weaken per-user
scoping, we add a **second, parallel scope**.

- A new `households` table; `users` gains a nullable `household_id`. A user
  belongs to **0 or 1** household (v1).
- A household of **one is fully supported** — a single parent is a first-class
  case, not a degenerate two-parent one. Kid-facing features (facts, agreements,
  star charts + their prompts and congratulations) work unchanged with one member;
  only the *load-balancing* features — which are inherently *between* two
  co-parents (the mental-load check-in §3.6.1, the delta digest §3.6.2) — gate on
  `household_member_count() >= 2` (`is_shared_household()`), so they hide in the UI
  and their sweeps no-op for a solo household, then light up on their own when a
  second parent joins.
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

### 3.6 `household_stars` — the reward-chart ledger

A star chart's `structured` (§3.5) declares the **goals** (thresholds → rewards);
`household_stars` is the running **earnings** against them. Each grant is one
append-only row, so who awarded what — and when — stays as legible as a fact's
provenance (the same load-balancing point).

```sql
CREATE TABLE IF NOT EXISTS household_stars (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id),
    agreement_id INTEGER NOT NULL REFERENCES household_agreements(id),
    child_id     INTEGER NOT NULL DEFAULT 0,       -- copied from the agreement
    delta        INTEGER NOT NULL,                 -- earned (+) or corrected (-)
    note         TEXT,
    awarded_by   INTEGER REFERENCES users(id),
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

A chart's total is `SUM(delta)` over its `agreement_id` (derived, never stored),
so awarding is a plain insert. The **goal-crossing** logic is pure and lives in
`prefrontal/household.py` (`newly_reached_goals(structured, before, after)`,
`next_goal`, `star_congrats_text`), fed by the `before`/`after` totals
`award_stars()` returns — a goal `n` fires exactly once, when a grant carries the
total from `before < n` to `>= n`. When one or more goals are reached, the write
layer congratulates and pushes to **both** co-parents at once via
`deliver_to_household()` (`prefrontal/integrations/delivery.py`), which resolves
each member's own `Route` (their ntfy topic / Pushover key). This is the first
concrete slice of the "push the delta to the *other* parent" idea in §7 — a goal
hit is never something only the tracking parent knows.

Surfaced as `POST /household/agreements/{id}/stars` (and `prefrontal household
star`); the shared sheet's agreement block shows the running total + how many to
the next reward, and recent grants join the "recently changed" load surface.
Earn-only charts reject a negative `delta` at the write layer. All four award
paths — the endpoint, the CLI, the one-tap button, and the scheduled prompt —
funnel through one service, `award_stars_and_notify()` in `prefrontal/household.py`,
so the "crossed a goal → tell both parents" rule lives in exactly one place.

#### Scheduled award prompts

Giving a star only helps if someone remembers to. A chart can carry a **prompt
schedule** in its `structured` JSON — `{enabled, days:[0=Mon … 6=Sun],
time:"HH:MM", question}` — a recurring "did &lt;kid&gt; earn a star for
&lt;chart&gt; today?" check-in on chosen weekdays at a time of day (configured on
the `/kids` dashboard, weekday picker + time; validated by `normalize_prompt`).
No new table: the schedule rides the chart, and a `last_prompted_at` column dedups
it to once per local day.

A periodic trigger (n8n / launchd, like the coach & panic checks) POSTs
`/webhooks/household/star-prompts/check`; the sweep (`prompt_due` /
`prompt_question`, pure) finds every due chart and pushes **both** parents a
notification carrying signed one-tap **⭐ Yes / Not today** buttons (context
`"star"` in `delivery.py`, kind `"star"` in `notify.py`, actions in `oauth.py`).
Tapping **⭐ Yes** hits `GET /nudge/act` and awards a star with no app switch —
attributed to whoever tapped, congratulating both parents if it crosses a reward
goal. The CLI twin is `prefrontal household prompt-check`; the importable n8n
workflow is `deploy/n8n/star-prompt-check.workflow.json`.

### 3.6.1 `household_checkins` — the weekly mental-load check-in

The sheet makes *objective* load legible (who set what, via `updated_by`). This is
the **subjective** companion: an opt-in, once-a-week, deliberately gentle check-in
that asks each parent how the invisible load has *felt for them* — because the
felt experience and the tally can diverge, and honesty needs a welcoming frame.

- **Requires a shared household** (≥2 members) — it's a two-parent concern, so a
  solo household hides the card and the sweep no-ops (see §2).
- **Opt-in schedule** on the household row (`checkin_enabled`, `checkin_day`,
  `checkin_time`, `checkin_last_sent_at`) — one weekday + time both parents share,
  set on the `/kids` dashboard; off by default.
- **The ask** — a periodic trigger (`POST /webhooks/household/checkin/check`, and
  `prefrontal household checkin-check`; n8n workflow
  `deploy/n8n/checkin-check.workflow.json`) sends **both** parents a warm push
  once per ISO week (`checkin_due` / `week_key`, pure) with three one-tap
  self-report buttons — **Felt light 🙂 / Balanced ⚖️ / Carried a lot 🫠** (three,
  since ntfy renders at most three actions; kind `"load"` in `notify.py`, actions
  in `oauth.py`, context `"checkin"` in `delivery.py`). Framed as *your* week, no
  wrong answers, not about keeping score.
- **The answer** — a tap hits `/nudge/act`, recording that parent's `response` for
  the week in `household_checkins` (never who-did-what). Once **both** have
  replied, `checkin_summary` builds a gentle shared note delivered to both:
  affirming if aligned, a soft "might be worth a chat / trade a task" if one felt
  heavier (**never naming who**), a be-kind-to-each-other nudge if both felt heavy.
- **Tone is the feature.** Every string is warm and non-accusatory by design, so
  people answer honestly; the summary is aggregate, never a callout.

This is the opt-in, tone-calibrated realization of the "balance view" sketched in
§7 — subjective perception first, before any `updated_by`-count scoreboard.

### 3.6.2 The delta digest — the active push (no new table)

The sheet + check-in are still *pull* (someone has to look) or *self-report*. The
delta digest is the **push** half of §7: a daily, opt-in nudge that tells each
parent what the *other* parent changed on the sheet since they last looked.

- **Requires a shared household** (≥2 members) — with one parent there's no "other
  parent" to catch up on, so the toggle hides and the sweep no-ops (see §2).
- **Opt-in** household toggle `households.digest_enabled` (off by default), set on
  the `/kids` dashboard.
- **"Seen" vs "digested"** — two per-parent `coaching_state` stamps:
  `household_seen_at` (bumped whenever they open the sheet — `GET /household/sheet`
  — or tap **Caught up 👍**) and `household_digested_at` (bumped when we last told
  them). The diff (`unseen_changes`, pure) unions facts, agreements, and star
  grants **not authored by the viewer** with a timestamp after `max(seen,
  digested)` — so a parent is never told about their own edits, nor told twice.
- **Self-suppressing** — the sweep (`POST /webhooks/household/digest/check`,
  `prefrontal household digest-check`, n8n `deploy/n8n/digest-check.workflow.json`)
  sends each parent their own catch-up *only* when there's something new and it's
  been ~a day since their last (`digest_interval_ok`, the once/day cap). Silent
  otherwise, so a frequent poll is safe. Delivered per parent via
  `deliver_to_member` (context `"digest"`, kind `"digest"`, action `digest_seen`).
- The dashboard also shows a passive "✨ N new since you last looked" line, then
  marks the sheet seen — opening it *is* catching up.

Tone is informational and warm ("here's what your co-parent updated"), never a
callout. This lands the §7 delta digest; the `updated_by`-count balance view
remains a possible follow-on.

### 3.6.3 The load-balance view — the objective companion (no new table)

The check-in (§3.6.1) captures how the load *felt*; this shows what the sheet
*records* — a gentle, opt-in "who's been keeping it up" split from provenance
counts, so felt and actual can be seen side by side.

- **Opt-in** household toggle `households.balance_enabled` (off by default),
  **shared-only** (hidden for a solo household, §2). No push — it's a passive
  panel on `/kids` (and `prefrontal household balance`).
- **Derived on read** — `contribution_counts(since)` tallies each active member's
  `household_facts.updated_by` + `household_agreements.updated_by` +
  `household_stars.awarded_by` over `BALANCE_WINDOW_DAYS` (30); every member is
  included (even a zero) so both parents always show. `balance_view` (pure) turns
  that into shares + a caption.
- **Tone is the whole game** — `balance_caption` affirms when even, and when
  lopsided names only the *carrier* ("Dana has been carrying most of the sheet
  lately… no blame, just a gentle heads-up"), never the low contributor, and
  frames imbalance as a season. A raw "Dana 14, Alex 1" scoreboard is exactly
  what it avoids.

### 3.6.4 The shared shopping list — `household_shopping`

The template's *Shopping Lists* sheet: a running, child-tagged list of things to
buy that either parent can add to and check off, so nobody buys the same thing
twice. **Not** load-balancing — it's available to every household including a
solo one — and **not** per-user `todos` (those are user-scoped and carry
scheduling weight a shared checklist doesn't want), so it's its own
household-scoped table.

- Columns: `item`, `spec` (size/brand), `where_to_buy`, `got` (0/1), plus
  provenance on both **add** (`added_by`/`created_at`) and **buy**
  (`got_by`/`got_at`), and `child_id` (0 = household-wide).
- Repo: `add_shopping_item`, `set_shopping_got` (check off / un-check), 
  `remove_shopping_item`, `shopping_items` (still-needed first). It rides the
  shared sheet — `build_sheet`/`render_sheet` gain a **Shopping** section
  (unchecked/ticked boxes), and `counts["shopping"]` is the still-needed tally.
- Surfaces: `POST /household/shopping`, `…/{id}/got`, `…/{id}/remove`; the `/kids`
  dashboard's checklist; the **`/family`** view's quick-add + tap-to-check-off (the
  one editable spot on the otherwise read-only shared glance); the natural-language
  **assistant** (`add_shopping` / `check_shopping` / `remove_shopping`, so "add
  eggs and milk to the list" works from the dashboard or `/kids` box); and
  `prefrontal household shopping`.

### 3.6.5 Recurring shared chores — `household_chores` + `household_chore_log`

The **active heart of shared load**: a chore is one parent's recurring job (run
the dishwasher, pack lunches) whose whole point is that forgetting it lands on
the *other* parent. Unlike a star chart (about the kids), a chore is about the
co-parents' own load, so it carries no `child_id` — just an `owner_id`, a
schedule, and the plain reason it matters.

The notification flow reads the schedule twice a day (via a periodic sweep, like
the star-award prompt and check-in):

- A **lead-time reminder** to the *owner* `remind_before` minutes before `due_time`
  on a scheduled, still-undone day — "Time to run the dishwasher — ideally by
  10:00pm. If it slips, it makes the morning harder. Tap Done once it's sorted."
- If the due time passes with the chore still undone, a **miss-handoff**: the
  owner gets a no-blame "still isn't done" nudge, and the *other* parent gets a
  gentle heads-up — "Heads up — 'run the dishwasher' didn't get done today …
  flagging it so it's not a morning surprise" — so the slip isn't discovered cold
  the next morning. An unassigned chore (`owner_id` NULL) reminds/nudges everyone.

Details:

- **`household_chores`**: `title` (unique per household), `owner_id` (NULL =
  either parent), `days` (weekday-int CSV; empty = every day), `due_time`
  (`HH:MM` local), `remind_before` (minutes), `impact` (the "why"), `enabled`,
  provenance, and two local-date cursors — `last_reminded_on` / `last_missed_on` —
  that dedup each stage to once per local day (mirrors
  `household_agreements.last_prompted_at`).
- **`household_chore_log`**: one row per chore per local day it was completed
  (`done_on`, `done_by`), so "done today?" is a lookup and *who actually did it*
  stays legible — the loop closes even when the **other** parent picks up a
  slipped chore. The `UNIQUE (household_id, chore_id, done_on)` key makes "mark
  done" idempotent.
- **Pure logic** (`prefrontal.household`): `normalize_chore`, `reminder_due` /
  `miss_due` (fed now / done-today / last-fired, so timing is unit-tested with no
  store or clock), the message builders, and `run_chores_check` — the one
  orchestration path (like `award_stars_and_notify`) the endpoint and CLI share.
- **Surfaces**: `POST /household/chores`, `…/{id}/done`, `…/{id}/enabled`,
  `…/{id}/remove`, the `POST /webhooks/household/chores/check` sweep, a one-tap
  **✓ Done** ntfy button (`chore_done` → `/nudge/act`, attributed to whoever
  taps), a **Shared chores** section on the sheet (today's done/pending status),
  and `prefrontal household chore` / `chores-check`.

### 3.7 What reuses existing tables (no new schema)

- **Appointments** (dental/doctor) → **`commitments`**, tagged with the `child`
  `kind` and a `health` category. The sheet just *surfaces* the upcoming ones;
  the calendar/commitment machinery is unchanged.
- **Rendering** is deterministic assembly (cheap), so — unlike `profile_cache` —
  there is **no cache table**; the sheet is built on request.

> **Deferred to v1.1 (noted, not built here):** *Key Contacts* from the template
> fit `household_facts` (`category='contact'`, `item=role`) for v1; a dedicated
> `household_contacts` table is the natural follow-on if they outgrow that.

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

## 6. The visible sheet (`/kids` render)

Assemble the household's data into a single rendered view — the editable **`/kids`**
dashboard, with a read-only partner glance at **`/family`** (server-rendered; no
new UI framework). Deterministic — built on request from `facts()` /
`agreements()` / `children()` / upcoming child `commitments`.

Sections, in order:

1. **Recently changed** *(the load-balancing surface)* — the last N facts/appts
   by `updated_at`, each showing *what · who · when* ("Sam shoe size → 13 ·
   updated by Dana · 2d ago"). This is the first thing the oblivious parent sees.
2. **Per-child facts** — grouped by category (sizes, routine, food, health,
   school), one block per child.
3. **Standing agreements** — behavior plans, star charts rendered from
   `structured` (progress + next reward) with the plan `body`. The chart's
   running total (from the `household_stars` ledger, §3.6) and how many stars to
   the next reward render inline; recent grants join the "recently changed"
   surface.
4. **Upcoming appointments** — child commitments in the near window, with who (if
   known) is on pickup.

Reachable at `GET /kids` (editable) and `GET /family` (glance) for both parents;
`GET /household/sheet` returns the same assembly as JSON for machine clients / the
widget.

---

## 7. Balancing the load (the delta digest) — **shipped**

The visible sheet is passive; the differentiator is **push**, and it shipped as a
household cue source over the existing delivery path (§3.6.2/§3.6.3):

- On a tick, compare each member's "last seen" stamp against recent
  `household_facts`/appointment changes and near-term child appts, and emit a cue
  to the member(s) who **haven't** touched them: *"3 things changed since
  Tuesday: Sam's shoe size, the new sticker plan, dentist Thu 3pm — you're on
  pickup."*
- A **balance view** built from `updated_by` counts ("this month: Dana updated 14,
  Alex 1") — gentle, not accusatory; opt-in and tone-calibrated like the
  encouragement layer ([`encouragement.md`](encouragement.md)). **Shipped** as an
  opt-in, shared-only panel — see §3.6.3.

This reuses the existing cue → channel → debounce machinery; no new delivery
mechanism.

---

## 8. Household membership

**Operator path (still supported).** `create_household(name) -> id` and
`set_user_household(handle, household_id)` on the unscoped (operator) store;
surfaced via `prefrontal household add` / `join` and the `POST /admin/households`
endpoints, mirroring `provision_user` / `POST /admin/users`.

**Self-serve path (shipped).** An authenticated user needs no operator to set up:

- `POST /household/create` (`create_own_household`) makes a household and joins
  the caller — for a user in no household yet (409 if they're already in one).
- `POST /household/invites` (`create_invite`) mints a short, unambiguous,
  expiring code (`household_invites`), rendered on `/kids` as both a code and a
  `…/kids?invite=CODE` link to text the co-parent. `pending_invites` /
  `revoke_invite` manage outstanding ones.
- `POST /household/invites/redeem` (`redeem_invite`) joins the caller to the
  invite's household — one-time, with friendly errors (invalid / used / expired /
  already in a household). CLI parity: `prefrontal household invite` / `redeem`.
- Account provisioning stays operator-controlled (you need a user to log in at
  all); the invite flow is about *household* setup, not account creation.

This combines with the single-parent support (§2): a solo parent self-creates a
household and everything works; when they later invite a co-parent, the
load-balancing features light up on their own.

---

## 9. Touch list (where the work lands)

| Area | Change |
|---|---|
| `prefrontal/memory/schema.sql` | `households` (+ `checkin_*`/`digest_enabled`/`balance_enabled`), `children`, `household_facts`, `household_agreements`, `household_stars`, `household_checkins`, `household_shopping`, `household_invites`; `users.household_id`. |
| `prefrontal/memory/migrate.py` | `users.household_id`, `household_agreements.last_prompted_at`, `households.checkin_*` in `_ADDED_COLUMNS` (back-fill). |
| `prefrontal/memory/repos/household.py` | Repo mixin: `_household_id()`, `household_member_count()`/`is_shared_household()` (the single-parent switch), facts/agreements/children methods, the star ledger + `mark_prompted`, the check-in, the digest toggle, and the balance view (`get`/`set_balance_enabled`, `contribution_counts`). |
| `prefrontal/memory/store.py` | Mix in the household repo; `set_user_household`, `create_household` on the unscoped store. |
| `prefrontal/household.py` | Pure render + goal logic + prompt logic + the `award_stars_and_notify` service + the check-in logic + the digest logic (`unseen_changes`/`digest_message`/`digest_interval_ok`). |
| `prefrontal/integrations/delivery.py` | `deliver_to_household()` + `deliver_to_member()`; `household_notice()`, `household_prompt_notice()` (⭐), `household_checkin_notice()` (self-report), `household_digest_notice()` (Caught up). |
| `prefrontal/webhooks/{notify,oauth}.py` | `star` + `load` + `digest` nudge kinds and their actions (`star_award`/`star_skip`, `load_*`, `digest_seen`); handled in `routers/anchor.py`'s `/nudge/act`. |
| `prefrontal/assistant.py` | New ops in `ALLOWED_OPS`; snapshot + validators + executors. |
| `prefrontal/webhooks/…` | `/family` render section; `GET /household/sheet` (+ `checkin` + `digest`, marks seen); star endpoints; `POST /household/{checkin,digest}` + `POST /webhooks/household/{checkin,digest}/check`; the `/kids` star-chart + check-in + digest UI; operator household endpoints. |
| `prefrontal/cli.py` | `prefrontal household add/join/show/star/prompt-check/checkin-check/digest-check/balance/shopping/invite/redeem`. |
| `deploy/n8n/*.workflow.json` | Scheduled triggers for the award prompts, the weekly check-in, and the daily digest. |
| `docs/schema.md` | Document the five new tables + the added column. |

---

## 10. Open questions

- **Membership model.** ~~Operator-set vs. self-serve.~~ **Resolved** — both ship
  (§8): operator wiring *and* self-serve create + invite-code redeem.
  Still open: can a user belong to more than one household (v1: no)? What happens to a
  household's rows when a member leaves?
- **Children identity.** A `children` roster (this spec) vs. a free-text child
  label on facts. The roster is cleaner for appointments and per-child agreements
  but adds a table and an add-child step.
- **Conflict handling.** Last-write-wins + provenance is the v1 call; is a visible
  "Dana changed this 5 min ago" ever needed, or is that overkill at co-parent
  scale?
- **Contacts.** Contacts-as-facts vs. a dedicated `household_contacts` table —
  still open. *(Shopping is resolved: it shipped as a dedicated `household_shopping`
  table, §3.6.4, rather than reusing todos.)*
- **Privacy.** v1 makes everything household-visible. Is there ever a need for a
  per-parent-private annotation, or does that undermine the shared-sheet premise?

---

## 11. Rollout sequence

1. Schema + migration (`households`, `children`, `household_facts`,
   `household_agreements`, `users.household_id`) + the household repo, with unit
   tests: two users in one household see the same rows; a user with no household
   raises; two households don't leak.
2. Operator wiring (`create_household`, `set_user_household`, CLI/endpoint).
3. Pure render (`prefrontal/household.py`) + the `/kids` sheet + `/family` glance.
4. Assistant ops (`set_fact`/`set_agreement`/…) with validation + snapshot.
5. The delta digest + balance view + weekly check-in.

All five shipped: 1–4 are the visible, plain-English-editable shared sheet; 5 turns
it into the active load-balancer.
