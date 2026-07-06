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
2. **Calendar:** per-user **private ICS feed URLs**, synced in-app.
   *(Revised from the original Google Calendar OAuth plan: OAuth's sensitive-scope
   verification + 7-day-refresh-token-in-testing-mode friction wasn't worth it
   when a sealed ICS "secret address" per feed gives the same result and reuses
   the existing ICS ingestion. The feed URL is itself a bearer secret, so Phase 0
   crypto still earns its keep.)*
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

## Phase 1 — Per-user mail ✅ CODE DONE (deploy + onboarding deferred)

- ✅ Resolution bridge in `prefrontal/sources.py`: `resolve_mail_fetch(store,
  account)` returns `MailFetchSource(imap, policy)` from the DB source
  (credentials decrypted, retention from config), **falling back to
  `ImapAccount.from_env` + `Settings.policy_for`** so a not-yet-migrated deploy
  keeps working; `mail_fetch_accounts(store)` = the user's enabled sources else
  the global env accounts.
- ✅ `cli.py` mail fetch: `--account` now optional (omit = all the user's
  accounts), `--all-users` fan-out via a shared `_user_targets` helper, per-user/
  per-account resolve → fetch → ingest (`_ingest_mail`/`_print_mail_summary`).
- ✅ CLI: `mail add-source | list-sources | remove-source` (passwords sealed;
  list never reveals them) and `mail import-env-sources` (seals the current
  `MAIL_IMAP_*` accounts into the user's registry).
- ✅ **Tests** (`test_mail_sources.py`): DB-preferred vs env-fallback resolution,
  disabled-source fall-through, account listing, add/list/remove round-trip
  (password sealed, not in output/DB), import-env, `--all-users` fan-out
  isolation, missing-credential exit code.
- ⏳ **Deferred to a follow-up (after #319/#320 merge):** switch
  `deploy/mail-fetch.sh` + `com.prefrontal-mail.plist` to a single
  `mail fetch --all-users` job and retire the `PREFRONTAL_USER=Tom` band-aid.
  Held back to avoid churn/conflicts with the band-aid PR #319.
- ⏳ **Deferred:** "connect a mailbox" step in the `onboard-user` skill.
- **Known limitation:** Gmail deep-linking (`Settings.gmail_accounts`) is still
  derived from env account names, so a DB-only Gmail account may not get Gmail
  deep-links on its todos. Cosmetic; fold into a later polish pass.

## Phase 2 — Per-user calendar (private ICS feeds) ✅ CODE DONE

- ✅ `prefrontal/ics.py`: `fetch_ics(url)` (httpx) + `parse_ics(text, namespace,
  me_emails)` — a faithful in-app port of the old n8n "Parse ICS" nodes (line
  unfolding, `DTSTART;TZID`, `EXDATE`/`RECURRENCE-ID`, namespaced `external_id`,
  declined-attendee drop; also drops `STATUS:CANCELLED`). Emits the event dicts
  `commitments.sync_calendar` already consumes, so recurrence expansion + TZID→UTC
  + conflict detection are reused unchanged.
- ✅ `ics` connector in `prefrontal/sources.py`: `IcsSource`, `put_ics_source`
  (seals the feed URL — a bearer secret), `ics_sources`. Config carries
  `namespace`, `me_emails`, `label`.
- ✅ `prefrontal calendar sync [--all-users]` (fetch → parse all a user's feeds →
  one `sync_calendar` call, classify via Ollama when up, best-effort geocode) +
  `calendar add-source/list-sources/remove-source`.
- ✅ `deploy/calendar-sync.sh` + `deploy/com.prefrontal-calendar.plist`
  (StartInterval 900, `--all-users`); `install-launchd.sh` auto-discovers it.
- ✅ Retired the n8n `calendar-sync` workflow; the push webhook
  (`POST /webhooks/calendar/sync`) is **kept** for other/legacy sources.
- ✅ **Tests** (`test_calendar_sources.py`): parser (TZID/all-day/UTC/declined/
  cancelled/unfolding/namespacing), URL sealed at rest, add/list/remove CLI,
  `sync --all-users` isolation, no-feeds + missing-key exits.
- ⏳ **Deferred:** onboarding "add a calendar feed" step in the `onboard-user`
  skill.

## Phase 3 — Scheduling unification & cleanup ✅ CODE DONE

- ✅ `learn` + `summarize` now route through the shared `_user_targets(store, args)`
  helper (the copy-pasted fan-out blocks are gone); `mail`/`calendar` already did.
- ✅ `coach --all-users` (+ `_coach_tick` extracted so the tick is per-user); the
  `com.prefrontal-coach` job runs `coach --deliver --all-users`, so one job covers
  the household. **Delivery is safe multi-user:** `resolve_route` gives a user
  with no own ntfy/Pushover target an empty target on a multi-user box (no leak to
  the operator's device), so source-less/demo users are computed but not delivered
  to.
- ✅ **Mail `--all-users` env-fallback gate** — under a fan-out the global
  `MAIL_IMAP_*` env is NOT used as a fallback (it's one mailbox; inheriting it for
  every source-less user would fetch that inbox into everyone's scope). A user
  with no DB `imap` source is skipped. Single-user/explicit `--user` keeps the env
  fallback (legacy).
- ⏳ **Deferred (needs the operator's migration first):** flipping the *mail*
  launchd job to `mail fetch --all-users`. It requires each real user to have run
  `mail import-env-sources` (or `add-source`) first — otherwise the env-fallback
  gate skips them and their mail pauses. One-line change to `mail-fetch.sh` +
  plist once sources are imported; left pinned for now to avoid a silent
  mail-fetch regression on deploy.
- ⏳ Onboarding "connect a mailbox / add a calendar feed" steps.

---

## Risks / gotchas

1. **ICS feed URL is a bearer secret.** Anyone with the "secret address" URL can
   read the calendar, so it's Fernet-sealed at rest and never printed by
   `list-sources`. (This is why the ICS route still needs Phase 0 crypto.)
2. **Secret-key durability.** Key loss = stored IMAP passwords + ICS feed URLs
   unrecoverable. Back up `PREFRONTAL_SECRET_KEY`; recovery = re-enter mail creds
   / re-add calendar feeds.
3. **Migration ordering (mail).** Keep the env → DB fallback for IMAP until
   `import-env-sources` has run and been verified, so a half-migrated deploy
   still fetches Tom's mail.
4. **Cutover double-ingest.** Deactivate the n8n `calendar-sync` workflow in the
   n8n UI before enabling the launchd job, or events land twice (harmless when
   feed slugs match — `sync_calendar` upserts by `external_id` — but avoid it).

## Files touched (summary)

New: `prefrontal/crypto.py`, `prefrontal/sources.py`,
`prefrontal/memory/repos/sources.py`, `prefrontal/ics.py`,
`deploy/calendar-sync.sh`, `deploy/com.prefrontal-calendar.plist`, tests
(`test_crypto.py`, `test_sources.py`, `test_mail_sources.py`,
`test_calendar_sources.py`, additions to `test_multi_tenant.py`, `test_cli.py`).
Edited: `pyproject.toml`, `prefrontal/config.py`, `prefrontal/memory/schema.sql`,
`prefrontal/memory/store.py`, `prefrontal/cli.py`, `deploy/mail-fetch.sh`,
`deploy/com.prefrontal-mail.plist`, `.env.example`, `deploy/README.md`, docs.
Retired: `deploy/n8n/calendar-sync.workflow.json` (the push webhook is kept).
Deferred: onboarding "connect a mailbox / add a calendar feed" steps.
