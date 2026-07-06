# Per-user email & calendar sources

Status: **proposed** (plan for review, no code yet)
Author: drafted with Claude, 2026-07-06

## Problem

Mail and calendar both have **correct per-user storage but global / single-user
source configuration**. The `--user` flag scopes *where ingested data is
written*, never *whose account is read*.

- **Mail:** IMAP credentials come only from global env
  (`MAIL_IMAP_{USER,PASSWORD,HOST,MAILBOX}_<ACCOUNT>`, `PREFRONTAL_MAIL_ACCOUNTS`),
  resolved by `ImapAccount.from_env(account)` (`prefrontal/mail/imap.py:72`).
  The user handle is never consulted. Two users running `mail fetch` read the
  *same* mailbox and merely file it under different `user_id`s
  (`cli.py:2161` fetch, `cli.py:2177` scope-on-write). There is **no per-user
  credential store and no secrets-at-rest** anywhere.
- **Calendar:** ingested by a single n8n workflow
  (`deploy/n8n/calendar-sync.workflow.json`) with hard-coded ICS URLs and one
  shared `X-Prefrontal-Token`, so every event lands in one user's scope. The
  Google OAuth flow (`prefrontal/webhooks/oauth.py`) is **login-only**
  (`openid email`) and stores no token. Storage (`commitments`, scoped) is
  correct; the source config lives outside the app entirely.
- **Scheduling:** only `learn`/`summarize` have `--all-users`; `coach`,
  `mail fetch`, and calendar have no fan-out, so multiple users need duplicated
  launchd jobs.

The recently shipped mail-fetch `--user Tom` fix is a band-aid: it pins the one
global mailbox to Tom. It gives no other user their own inbox.

## Goal

Each user connects **their own, and only their own** email and calendar.

## Decisions (locked)

1. **Secrets at rest:** Fernet-encrypted, stored in the DB.
2. **Calendar:** real per-user **Google Calendar OAuth**
   (`calendar.readonly` + refresh tokens), not ICS.
3. Deliver as a **phased** build; this doc is the plan reviewed first.

## Architecture: a per-user source registry

The missing primitive both subsystems need. One new table, one repo, one crypto
utility.

### New table `sources` (in `prefrontal/memory/schema.sql`)

Follows the existing per-user table convention (`id`, `user_id NOT NULL
REFERENCES users(id)`, timestamps, composite unique on `user_id`). Reaches
production automatically via `init_db`'s `CREATE TABLE IF NOT EXISTS`
(`memory/db.py:99-100`) — **no migration runner change needed** for a new table
(`backfill_added_columns` handles any *future column* additions,
`memory/migrate.py:99-123`).

```sql
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    kind        TEXT NOT NULL,              -- 'imap' | 'gcal'
    account     TEXT NOT NULL,              -- logical name: 'personal','work' (imap); 'google' (gcal)
    config      TEXT NOT NULL DEFAULT '{}', -- JSON: imap {host,mailbox,important_only,retention};
                                            --       gcal {calendar_ids[], namespace, me_emails[]}
    secret_enc  BLOB,                       -- Fernet(token): IMAP password | Google refresh token
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, kind, account)
);
CREATE INDEX IF NOT EXISTS idx_sources_user_kind ON sources (user_id, kind);
```

### `SourcesRepo` (`prefrontal/memory/repos/sources.py`)

Mirror `ScheduleRepo`: subclass `Repo`, thread `self._uid()` into every
statement, `self.conn.commit()` after writes. Wire into `MemoryStore` with one
import + one tuple entry (`memory/store.py` ~L54 / ~L81). Methods:
`list_sources(kind=None)`, `get_source(kind, account)`, `upsert_source(...)`,
`set_enabled(...)`, `delete_source(...)`. Repo stores/returns raw bytes; **all
encryption lives one layer up** (service, below) so the crypto boundary is
explicit and testable.

### Crypto utility (`prefrontal/crypto.py`) — new capability

No encryption exists in the codebase today (confirmed: no `Fernet`/`keyring`
usage). Add `cryptography>=42` to `pyproject.toml` (only genuinely new dep —
everything else uses `httpx`, already present).

- `seal(plaintext: str) -> bytes` / `open(token: bytes) -> str` via Fernet.
- Key from `settings.secret_key` (`PREFRONTAL_SECRET_KEY`), with a
  `PREFRONTAL_SECRET_KEY_FILE` fallback (root-readable keyfile, chmod 600).
- `prefrontal secrets init` generates a key if none exists and prints setup
  guidance. **Losing the key makes stored secrets unrecoverable** (IMAP
  passwords must be re-entered; Google must be re-authorized) — call this out in
  docs and back the key up.

### Service layer (`prefrontal/sources.py`)

Thin functions that own the encrypt/decrypt boundary and shape config:
`put_imap_source(store, account, host, user, password, ...)`,
`resolve_imap(store, account) -> ImapAccount | None`,
`put_gcal_source(store, refresh_token, calendar_ids, ...)`,
`resolve_gcal(store) -> GcalCreds | None`.

## Config additions (`prefrontal/config.py`)

`Settings` is a frozen dataclass; add fields + wire in `load_settings()`
(boolean idiom per `tts_enabled` at config.py:396):

- `secret_key: str = ""`  ← `PREFRONTAL_SECRET_KEY`
- `secret_key_file: str = ""`  ← `PREFRONTAL_SECRET_KEY_FILE`
- `google_calendar_enabled: bool = False`  ← gate for the calendar OAuth scope

Document all three in `.env.example`.

---

## Phase 0 — Foundations ✅ DONE
*Registry + crypto, no behavior change yet.*

- ✅ `cryptography>=42` dep; `prefrontal/crypto.py` (`seal`/`unseal`/`generate_key`/
  `secret_key_configured`, key from `PREFRONTAL_SECRET_KEY` or `_FILE`);
  `prefrontal secrets init|status` CLI.
- ✅ `sources` table in `schema.sql`; `SourcesRepo` (CRUD, `_uid()`-scoped) wired
  into `MemoryStore`.
- ✅ `prefrontal/sources.py` service layer (owns the seal/unseal boundary;
  `put_imap_source`/`resolve_imap`/`imap_accounts`, `put_gcal_source`/`resolve_gcal`).
- ✅ Config fields (`secret_key`, `secret_key_file`, `google_calendar_enabled`);
  `.env.example` documented.
- ✅ **Tests:** `test_crypto.py` (round-trip, encrypted-at-rest, missing/malformed/
  wrong-key errors, keyfile), `test_sources.py` (repo CRUD + service round-trips +
  password-not-in-`secret_enc`), isolation case in `test_multi_tenant.py`,
  `secrets` registration in `test_cli.py`. Full suite green except 3 failures
  pre-existing on `main` and unrelated (live-Ollama clarify test, real-`.env`
  notify test, a tz-guard offender in `time_blindness.py:342`).

## Phase 1 — Per-user mail

- `ImapAccount` resolution: new `sources.resolve_imap(store, account)` checks
  the DB first, **falls back to `from_env` during transition** so nothing breaks
  mid-migration.
- `cli.py` mail fetch: resolve per-user; account list from the user's `imap`
  sources when present (else global env). Add `--all-users`.
- CLI: `prefrontal mail add-source | list-sources | remove-source`, and a
  one-time `prefrontal mail import-env-sources --user <handle>` that reads the
  current `MAIL_IMAP_*` env + `PREFRONTAL_MAIL_ACCOUNTS` and writes encrypted
  rows (migrates Tom off env in one shot).
- Deploy: switch `deploy/mail-fetch.sh` + `com.prefrontal-mail.plist` to a single
  `mail fetch --all-users` job (accounts come from each user's sources); retire
  the hard-coded `personal work` arg list and the `PREFRONTAL_USER` pin added in
  the band-aid fix.
- Onboarding: add a "connect a mailbox" step to the `onboard-user` skill.
- **Tests:** per-user resolution, env fallback, `--all-users` independence.

## Phase 2 — Per-user calendar (Google OAuth)

- `oauth.py`: when `google_calendar_enabled`, extend the login authorize URL
  (`oauth.py:290,294-295`) with `calendar.readonly`, `access_type=offline`,
  `prompt=consent`. In `_exchange_code_for_email` (`oauth.py:240-274`) capture
  the `refresh_token` currently discarded at L259; in the callback
  (`oauth.py:316-329`) Fernet-seal it and store as a `gcal` source. No Google
  client library — reuse the existing httpx-direct pattern.
- `prefrontal/calendar/google.py`: exchange refresh token → access token
  (`grant_type=refresh_token`), call Calendar `events.list` for the selected
  calendars, map into the event shape the existing ingestion already consumes
  (`commitments.sync_calendar` / `store.upsert_commitment`,
  `commitments.py:518`, `:576`). Same `external_id` namespacing as today.
- `prefrontal calendar sync --all-users`; new `deploy/com.prefrontal-calendar.plist`
  (StartInterval 900). `install-launchd.sh` already auto-discovers new templates.
- Calendar selection: `prefrontal calendar list-remote` / `enable <id>`
  (default to `primary`), stored in the `gcal` source `config`.
- Retire the n8n `calendar-sync` workflow; **keep** the push webhook
  (`POST /webhooks/calendar/sync`) for other/legacy sources.
- Onboarding: "connect your calendar" → visit `/auth/google/login`.
- **Tests:** token-refresh (mock httpx), event mapping, fan-out, isolation.

## Phase 3 — Scheduling unification & cleanup

- Extract the copy-pasted fan-out block (`cli.py:921-930` / `:1100-1108`) into a
  shared `_user_targets(store, args)` helper; apply to `coach`, `mail`,
  `calendar`, `learn`, `summarize`.
- `coach --all-users`; collapse per-user launchd duplication into one job each.
- Docs, `.env.example`, update the `deployment-state` memory.

---

## Risks / gotchas to confirm before Phase 2

1. **Google sensitive-scope verification.** `calendar.readonly` is a *sensitive*
   scope. An unverified app in **"Testing"** mode issues refresh tokens that
   **expire after 7 days** — always-on sync would then break weekly and need
   re-auth. Options: (a) publish + verify the OAuth app (removes the 7-day
   expiry; verification review for a personal Gmail project can take days), or
   (b) accept weekly re-consent for the household, or (c) if any account is
   Google Workspace, use an "Internal" app (no 7-day limit). **This is the
   single biggest operational decision in the calendar phase** — flag before
   building.
2. **Secret-key durability.** Key loss = all stored IMAP passwords + Google
   refresh tokens unrecoverable. Back up `PREFRONTAL_SECRET_KEY`; document
   recovery (re-enter mail creds, re-run OAuth).
3. **Migration ordering.** Keep the env → DB fallback for IMAP until
   `import-env-sources` has run and been verified, so a half-migrated deploy
   still fetches Tom's mail.

## Files touched (summary)

New: `prefrontal/crypto.py`, `prefrontal/sources.py`,
`prefrontal/memory/repos/sources.py`, `prefrontal/calendar/google.py`,
`deploy/com.prefrontal-calendar.plist`, tests (`test_sources.py`, additions to
`test_multi_tenant.py`, `test_cli.py`).
Edited: `pyproject.toml`, `prefrontal/config.py`, `prefrontal/memory/schema.sql`,
`prefrontal/memory/store.py`, `prefrontal/mail/imap.py`, `prefrontal/cli.py`,
`prefrontal/webhooks/oauth.py`, `deploy/mail-fetch.sh`,
`deploy/com.prefrontal-mail.plist`, `.env.example`, onboarding skill.
Retired: `deploy/n8n/calendar-sync.workflow.json` (calendar push kept).
