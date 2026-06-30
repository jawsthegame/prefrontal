# Multi-tenant Prefrontal — design spec

Status: **proposed**. This is the implementation spec for turning Prefrontal from
single-tenant into a multi-user system. It supersedes the short "Multiple users"
sketch in [`ROADMAP.md`](../ROADMAP.md) with concrete schema, API, and migration
detail. If this and the roadmap disagree, this file wins.

---

## 1. Goal & non-goals

**Goal.** Let one Prefrontal deployment serve several people — e.g. a household,
a small team, or a few friends sharing a Mac mini — so that each person has their
own episodes, outings, commitments, todos, behavioral profile, learned bias,
auth credential, and notification target, with no data crossing between them.

**Non-goals (v1).**

- No web signup / self-service onboarding. Users are provisioned by an operator
  (a CLI command). This is a self-hosted system for a handful of trusted people,
  not a SaaS.
- No roles/permissions beyond "owns their own data" + one operator. No sharing,
  no household-level aggregate views (the existing `/family` view stays a
  read-only window onto **one** user; see §9).
- No per-user module *code* — modules stay global; only their *state* becomes
  per-user (§5).
- No move off SQLite. The "local first, few moving parts" promise holds.

**Success test.** Two users, two tokens. Each can drive the full outing →
escalation → return → learn loop independently; a calendar sync or `prefrontal
learn` for one never reads or writes the other's rows; a nudge for user A is
delivered to A's phone, not B's.

---

## 2. Decision: shared DB, row-level scoping

The open question in the roadmap was **DB-per-user** vs **shared DB with a
`user_id` column**. We choose **shared DB with row-level scoping**.

| | Shared DB + `user_id` (chosen) | DB-per-user |
|---|---|---|
| Operate | One file, one backup, one migration | N files, fan-out backup/migrate |
| Cross-user ops (admin list, household view) | Trivial query | Open every DB |
| Connection model | Keeps today's single shared conn | Conn pool keyed by user, or open/close per request |
| Risk | A missing `WHERE user_id=?` leaks data | Path/handle mix-up leaks data |
| Migration from today | Backfill one column | Split the file |

The decisive factor is the **leak-risk mitigation**: with a shared DB we can make
scoping *structural* rather than *remembered* — the store is constructed bound to
a `user_id` and injects it into every statement, so no call site can forget it
(§4). DB-per-user pushes the same risk into connection routing, which is just as
easy to get wrong and worse to operate.

---

## 3. Schema changes

All changes stay in [`prefrontal/memory/schema.sql`](../prefrontal/memory/schema.sql),
which remains idempotent. A migration path for the existing single-tenant DB is
in §8.

### 3.1 New `users` table

```sql
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    handle       TEXT    NOT NULL UNIQUE,        -- short login-ish name: 'tom', 'sam'
    display_name TEXT,                            -- shown in nudges/briefings
    token_hash   TEXT    NOT NULL UNIQUE,         -- sha256(token); raw token never stored
    status       TEXT    NOT NULL DEFAULT 'active', -- active | disabled
    is_operator  BOOLEAN NOT NULL DEFAULT 0,      -- may call admin endpoints
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_users_token ON users (token_hash);
```

- **Tokens are never stored in plaintext.** We store `sha256(token)`; the raw
  token is shown once at creation time and never again (like an API key). Lookup
  on a request is `WHERE token_hash = sha256(provided)`, which is also the
  fast-path index. (sha256 of a high-entropy random token is fine here — these
  are not human passwords, so no need for bcrypt/argon2.)
- `handle` is the stable identifier used by CLI (`--user tom`) and operator
  tooling; `display_name` is what appears in messages ("Tom, you said 15 min…").

### 3.2 `user_id` on every per-user table

Add `user_id INTEGER NOT NULL REFERENCES users(id)` to: `episodes`, `patterns`,
`coaching_state`, `outings`, `commitments`, `todos`, `dismissed_conflicts`.

Index it everywhere, and **fold it into the existing uniqueness constraints** —
this is the subtle part:

| Table | Today's constraint | Becomes |
|---|---|---|
| `patterns` | `UNIQUE (pattern_type, context_key)` | `UNIQUE (user_id, pattern_type, context_key)` |
| `coaching_state` | `key TEXT UNIQUE` | `UNIQUE (user_id, key)` (drop column-level UNIQUE) |
| `commitments` | `UNIQUE(external_id) WHERE external_id NOT NULL` | `UNIQUE(user_id, external_id) WHERE external_id NOT NULL` |
| `dismissed_conflicts` | `signature PRIMARY KEY` | composite PK `(user_id, signature)` |

If we miss one of these, two users collide on upsert (e.g. both users' calendars
share a Google `external_id` namespace, or both have a `time_estimation_bias`
key). Every `ON CONFLICT` target in `store.py` must be updated to match
(`upsert_pattern`, `set_state`, `upsert_commitment`, `dismiss_conflict`).

New per-table index pattern, e.g.:

```sql
CREATE INDEX IF NOT EXISTS idx_episodes_user ON episodes (user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_outings_user_status ON outings (user_id, status);
CREATE INDEX IF NOT EXISTS idx_commitments_user_start ON commitments (user_id, start_at);
CREATE INDEX IF NOT EXISTS idx_todos_user_status ON todos (user_id, status);
```

(Replace the existing single-column indexes; the leading `user_id` makes them
serve the scoped queries.)

### 3.3 Seed rows

Today `schema.sql` seeds global `coaching_state` defaults via `INSERT OR IGNORE`.
With per-user state, **seed-on-user-creation** instead of seed-in-schema: when a
user is created, the same defaults (plus each enabled module's
`default_state()` — see `modules/base.py:85`) are written scoped to that user.
The schema's global seed block is removed. This keeps `schema.sql` purely
structural and makes "a fresh user looks like a fresh install" the single code
path (`provision_user()` in §6).

---

## 4. Store: structural scoping

This is the heart of the leak-proofing. Today `MemoryStore` wraps a connection
and every method runs an unscoped query. We make **the user_id a property of the
store instance**, injected into every statement — so a scoped store *cannot*
read or write another user's rows.

```python
class MemoryStore:
    def __init__(self, conn: sqlite3.Connection, *, user_id: int | None = None) -> None:
        self.conn = conn
        self.user_id = user_id   # None = unscoped (admin/migration only)

    def scoped(self, user_id: int) -> "MemoryStore":
        """Return a lightweight store bound to one user, sharing the connection."""
        return MemoryStore(self.conn, user_id=user_id)
```

- Every per-user method gains `AND user_id = ?` on reads and `user_id` in the
  column list on writes, sourced from `self.user_id` — **not** from a parameter,
  so call sites are unchanged and can't pass the wrong id.
- A guard makes the failure loud, not silent: per-user methods call
  `self._uid()` which raises if `user_id is None`. So an accidentally-unscoped
  store throws `RuntimeError("store is not bound to a user")` rather than
  scanning everyone's data.
- The connection is still shared and long-lived (one per process, as today); a
  `scoped()` store is a cheap wrapper created per request. Connection lifecycle
  is unchanged — `MemoryStore.open()` and the FastAPI lifespan still own the one
  connection.

Methods that stay unscoped (operator-only, explicit): user CRUD
(`create_user`, `get_user_by_token_hash`, `list_users`, `set_user_status`) and
`each_user()` for the learning fan-out (§7). These live on the unscoped store and
never touch per-user tables except via `scoped()`.

The pure-core functions (`analyze_impact`, `fit_todos`, `escalation_level`,
`build_profile`, the pattern math) already take a store or plain values and don't
know about users — they keep working unchanged, just handed a scoped store.

---

## 5. Coaching-state: per-user, with two wrinkles

`coaching_state` becomes per-user (§3.2). Two call sites need care:

1. **Sync cursors stored as state.** `commitments.py:215-224` stashes
   `last_conflict_signature` / `last_possible_signature` in `coaching_state` to
   detect *new* conflicts across syncs. These must scope per-user automatically
   once state is scoped — no logic change, but confirm the scoped store is the
   one passed into `sync_calendar`.
2. **Module defaults.** `modules/base.py:85 default_state()` writes defaults if
   absent. Today it runs once globally; now it runs per user at provision time
   (§3.3). Module *code* and *enablement* (`PREFRONTAL_MODULES`) stay global —
   only the state values are per-user. (Per-user module enablement is a possible
   v2; out of scope here.)

`user_name` (read at `app.py:554` for nudge personalization) is replaced by the
user's `display_name` from the `users` row, which the scoped request already has
— one fewer state key.

---

## 6. Auth & request scoping

### 6.1 Token → user resolution

Replace the single shared-secret check (`_verify_token`, `app.py:248`) with a
per-user resolver implemented as a FastAPI dependency:

```python
def resolve_user(request, x_prefrontal_token) -> ScopedRequest:
    token = x_prefrontal_token
    if not token:
        raise HTTPException(401, "Missing X-Prefrontal-Token")
    row = request.app.state.store.get_user_by_token_hash(sha256_hex(token))
    if row is None or row["status"] != "active":
        raise HTTPException(401, "Invalid token")
    return ScopedRequest(user=row, store=request.app.state.store.scoped(row["id"]))
```

- The header name **stays `X-Prefrontal-Token`** — iOS Shortcuts, the dashboard,
  the n8n workflows, and the widget all send it today. Only the *value* becomes
  per-user. This keeps the client side a one-line change (swap the token).
- Token comparison uses `hmac.compare_digest` on the hashes to avoid timing
  leaks (matching today's intent of a constant-ish check).
- The dependency returns both the user row and a **store already scoped to that
  user**. Endpoints take `Depends(resolve_user)` and use `ctx.store` /
  `ctx.user["display_name"]` — replacing both `Depends(get_store)` and the
  `_verify_token(...)` first line in every handler. Net per-endpoint change:
  delete one line, swap one dependency.

### 6.2 Unauthenticated routes

`/health`, `/dashboard`, `/family` stay unauthenticated (they carry no data; the
HTML shells prompt for the token and call the scoped GET endpoints). No change.

### 6.3 The "no auth configured" mode

Today an empty `PREFRONTAL_WEBHOOK_SECRET` disables auth entirely (trusted-LAN
convenience). In multi-tenant that's incoherent — without a token we can't
resolve a user. Replacement: a **single-user compatibility mode**. If the deploy
sets `PREFRONTAL_DEFAULT_USER=<handle>`, requests with no/blank token resolve to
that user. This preserves the trusted-LAN ergonomics for a solo deployment while
keeping the data model multi-tenant. Documented as LAN-only, like today.

### 6.4 Operator endpoints

A small admin surface, guarded by `is_operator` on the resolved user:

- `POST /admin/users` `{handle, display_name}` → creates a user, returns the
  **raw token once**.
- `GET  /admin/users` → list (handles, display names, status — never tokens).
- `POST /admin/users/{handle}/rotate` → new token, returns it once.
- `POST /admin/users/{handle}/disable` → set `status='disabled'`.

These are thin HTTP wrappers over the same functions the CLI uses (§6.5), so the
operator can provision over Tailscale or on the box.

### 6.5 Delivery routing (the part outside Python)

Today the n8n workflows hardcode one Pushover user key and one Twilio To/From.
For multi-tenant, **delivery identifiers live in per-user `coaching_state`**
(operator sets them at provision time): `pushover_user_key`, `twilio_to`,
`twilio_from`, `ntfy_topic`. The check endpoints already return per-outing data;
we add the **routing fields to the response** so n8n delivers to the right
target without its own user lookup:

- `/webhooks/outing/check` response gains `delivery: {pushover_user_key, twilio_to, …}`
  per active outing (read from the scoped store).
- The n8n workflows change from "hardcoded credential" to "use the `delivery`
  fields from the response." This is an n8n-side edit documented in
  [`docs/deployment.md`](deployment.md); the Twilio/Pushover *credentials*
  (API tokens) stay global, only the *destination* becomes per-user.

A user with no delivery fields set falls back to the operator's default target
(so a half-provisioned user still reaches *someone*), and the response flags
`delivery_configured: false` so the dashboard can warn.

---

## 7. Learning & summarizer: fan-out over users

`prefrontal learn` and `prefrontal summarize` (and any scheduled nightly job)
operate on "the data" globally today. They become **per-user fan-outs**:

```python
for user in store.each_user(status="active"):
    s = store.scoped(user["id"])
    recompute_patterns(s)      # patterns.py, unchanged internally
    bias = recompute_bias(s)   # writes user-scoped time_estimation_bias
    build_profile(s)           # summarizer.py, unchanged internally
```

- The pattern math, bias computation, and profile assembly are already
  store-driven — handing them a scoped store is the entire change; their
  internals don't move.
- `profile.md` artifact (written by `prefrontal summarize`) becomes
  **per-user**: `profile-<handle>.md`, or a `profiles/<handle>.md` dir. The
  `GET /profile` endpoint already runs per-request, so it just uses the scoped
  store and needs no path logic.
- CLI commands that act on data (`learn`, `summarize`, `briefing`, `profile`,
  `todo`, `fit`) gain a `--user <handle>` flag. `learn`/`summarize` additionally
  accept `--all-users` (the nightly default) to fan out. Commands error clearly
  if neither is given and more than one user exists.

---

## 8. Migration of the existing single-tenant DB

The deployed `prefrontal.db` has real single-user data. Migration must be
non-destructive and idempotent.

A new `prefrontal migrate-multi-tenant` command (and a versioned SQL step):

1. Create the `users` table (idempotent).
2. If `users` is empty and per-user tables have rows, create the **legacy user**
   from `coaching_state.user_name` (or `--handle`, default `me`) with a freshly
   generated token (printed once). Capture its `id`.
3. `ALTER TABLE … ADD COLUMN user_id INTEGER` on each per-user table (SQLite
   supports add-column), then `UPDATE … SET user_id = <legacy_id> WHERE user_id
   IS NULL`.
4. Build the new composite indexes/uniques. (SQLite can't add a column with a
   `NOT NULL REFERENCES` + alter a UNIQUE in place, so the indexes are created
   separately and the `NOT NULL` is enforced going forward by the store + a
   `CHECK` added on the next table rebuild; the backfill guarantees no NULLs.)
5. Stamp a `schema_version` so the migration won't re-run.

Because SQLite's `ALTER` is limited (no drop-constraint), tables whose
*uniqueness* changes (`coaching_state`, `patterns`, `dismissed_conflicts`) use
the standard SQLite **rebuild dance**: `CREATE … _new` with the new schema,
`INSERT INTO _new SELECT …, <legacy_id>`, `DROP`, `ALTER … RENAME`. This runs
inside one transaction with `foreign_keys=OFF` for the duration, per SQLite's
documented procedure. The migration is covered by a dedicated test that loads a
fixture single-tenant DB and asserts the row counts and scoping survive.

Fresh installs skip all this — `schema.sql` already has the final shape and
`provision_user()` seeds the first user.

---

## 9. The `/family` view

Today `/family` is a calm read-only window onto the single user. Multi-tenant
options:

- **v1 (chosen):** `/family` resolves a user like every other endpoint (its HTML
  shell already prompts for a token). A partner is given **the user's family-
  scoped token** — same data, now explicitly scoped. Simplest, no new concepts.
- **v2 (noted, out of scope):** a read-only *viewer* credential type that can see
  one named user's family view but drive nothing. Needs a `scope` column on
  `users` or a separate `view_grants` table. Deferred.

---

## 10. Touch list (where the work lands)

| Area | Files | Change |
|---|---|---|
| Schema | `memory/schema.sql` | `users` table, `user_id` columns + composite uniques/indexes, drop global seeds |
| Store scoping | `memory/store.py` | `user_id` on `__init__`, `scoped()`, `_uid()` guard, `AND user_id=?` on every per-user query, fix every `ON CONFLICT` |
| User CRUD | `memory/store.py` (or new `memory/users.py`) | `create_user`, `get_user_by_token_hash`, `list_users`, `set_user_status`, `each_user` |
| DB/migration | `memory/db.py`, new `memory/migrate.py` | rebuild-dance migration, `schema_version` |
| Auth/scoping | `webhooks/app.py` | `resolve_user` dependency, drop `_verify_token`, swap `get_store`→scoped ctx on ~18 endpoints, `delivery` fields in check response, `/admin/users*` |
| Config | `config.py`, `.env.example` | `PREFRONTAL_DEFAULT_USER`; retire `PREFRONTAL_WEBHOOK_SECRET` (kept only as the operator-bootstrap token) |
| Provisioning | new `provision_user()` | seed coaching defaults + module defaults + delivery fields per user |
| Learning fan-out | `cli.py`, `memory/patterns.py` caller, `memory/summarizer.py` caller | `--user`/`--all-users`, per-user `profile-<handle>.md` |
| Delivery | `deploy/n8n/*.workflow.json`, `docs/deployment.md` | read `delivery` fields from responses instead of hardcoded creds |
| Pure cores | `impact.py`, `scheduling.py`, `briefing.py`, `modules/*` | **no change** — already user-agnostic |
| Tests | `tests/` | scoping isolation tests, migration test, auth tests, fan-out tests |

---

## 11. Testing strategy

The single highest-value test: **isolation**. A parametrized test that creates
two users, writes the full set of objects for each (episode, outing, commitment,
todo, state key, dismissed conflict, pattern), then asserts every read method on
user A's scoped store returns *only* A's rows and that an unscoped per-user call
raises. Add:

- **`ON CONFLICT` isolation:** both users upsert the same `external_id`, the same
  `coaching_state` key, the same pattern `(type, context_key)`, the same
  dismissed signature — assert two distinct rows, no clobber.
- **Auth:** valid/invalid/disabled token; `PREFRONTAL_DEFAULT_USER` fallback;
  operator vs non-operator on `/admin/*`.
- **Migration:** load a fixture single-tenant DB → migrate → assert all rows now
  belong to the legacy user, counts unchanged, re-running is a no-op.
- **Fan-out:** `learn --all-users` updates each user's bias independently from
  their own episodes.

Existing tests largely keep working if their fixtures provision a single user and
hand endpoints/stores that user's scoped store — wrap the in-memory
`create_app(store=…)` fixture to create user 1 and default to it.

---

## 12. Rollout sequence

1. Schema + store scoping + user CRUD + isolation tests. (No behavior change yet
   — everything runs as one implicit user in tests.)
2. Migration command + migration test; run it against a **copy** of the
   deployed DB and verify.
3. Auth dependency + `/admin/users` + `PREFRONTAL_DEFAULT_USER` compat mode.
   Deploy with the existing single user mapped to a token; nothing else changes.
4. Learning fan-out + per-user profiles + CLI `--user` flags.
5. Per-user delivery fields + n8n workflow edits + deployment doc. Provision the
   second user end-to-end as the acceptance test (§1).

Steps 1–3 are shippable without anyone noticing (one user, one token). Steps 4–5
light up the actual second user.

---

## 13. Open questions

- **Operator bootstrap.** How does the *first* operator user get created on a
  fresh install? Proposal: `prefrontal init-db` followed by `prefrontal user add
  <handle> --operator` printing the token; or the legacy `PREFRONTAL_WEBHOOK_SECRET`
  acts as a bootstrap operator token until at least one operator user exists.
- **Per-user timezone.** Briefings/commitments normalize to UTC and render
  "today" — a multi-user household is probably one timezone, but a `timezone`
  coaching-state key per user is the clean fix if not. Flag, don't block.
- **Token in URL for widgets.** The Scriptable widget and dashboard hold the
  token; per-user tokens mean each device holds its user's token. Rotation
  (`/admin/users/{h}/rotate`) invalidates old devices — acceptable, document it.
- **Per-user module enablement** (v2) — would move `PREFRONTAL_MODULES` into a
  per-user state key and have the registry resolve per request.
