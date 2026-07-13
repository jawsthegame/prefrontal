"""Migrate an existing single-tenant database into the multi-tenant schema.

The deployed ``prefrontal.db`` predates multi-tenancy: its per-user tables have
no ``user_id`` column and their uniqueness constraints are global (e.g.
``coaching_state.key UNIQUE``). This module brings such a database up to the
shape in :mod:`prefrontal.memory.schema` **non-destructively and idempotently**:

1. Create the ``users`` table (idempotent).
2. If there is no user yet but per-user data exists, create the **legacy user**
   from ``coaching_state.user_name`` (or a supplied handle, default ``me``) with
   a freshly generated token, printed once by the caller. Capture its id.
3. ``ALTER TABLE … ADD COLUMN user_id`` on every per-user table that lacks it,
   then ``UPDATE … SET user_id = <legacy_id>``.
4. Rebuild the tables whose *uniqueness* changed (``coaching_state``,
   ``patterns``, ``dismissed_conflicts``, ``kind_feedback``, ``mail_messages``,
   ``places``, ``profile_cache``) using SQLite's standard table-rebuild dance,
   so the new composite uniques/indexes match ``schema.sql``.
5. Stamp ``schema_version`` so the migration is a no-op on the next run.

Fresh installs never hit this — ``schema.sql`` already has the final shape and
:func:`prefrontal.memory.store.provision_user` seeds the first user.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from prefrontal.memory.db import SCHEMA_PATH
from prefrontal.memory.store import (
    DEFAULT_COACHING_STATE,
    MemoryStore,
    generate_token,
    seed_user_state,
)

#: Schema version stamped once this migration has run. Bumping the on-disk
#: ``user_version`` pragma to this value marks the database multi-tenant.
MULTI_TENANT_VERSION = 1

#: Tables that gained a ``user_id`` column. The ones in ``_REBUILD`` additionally
#: changed a uniqueness constraint and so need the full rebuild dance; the rest
#: only need an ``ADD COLUMN`` + backfill.
_USER_TABLES = (
    "episodes",
    "patterns",
    "coaching_state",
    "outings",
    "focus_sessions",
    "commitments",
    "todos",
    "projects",
    "dismissed_conflicts",
    "kind_feedback",
    "mail_messages",
    "places",
    "profile_cache",
)


def _reference_columns() -> dict[str, list[tuple[str, str]]]:
    """The authoritative per-table column list, derived from ``schema.sql`` itself.

    Applies ``schema.sql`` to a throwaway in-memory database and reads each
    table's columns via ``PRAGMA table_info``, reconstructing each column's DDL
    (``TYPE [NOT NULL] [DEFAULT x]``). ``schema.sql`` is therefore the **single
    source of truth** for the shape of a table — there is no hand-maintained
    ledger of "columns added later" to drift out of sync with it. Callers diff
    this against a live database and add whatever it is missing (see
    :func:`backfill_added_columns`).
    """
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(SCHEMA_PATH.read_text())
        tables = [
            row[0]
            for row in ref.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        columns: dict[str, list[tuple[str, str]]] = {}
        for table in tables:
            defs: list[tuple[str, str]] = []
            for _cid, name, col_type, notnull, dflt, _pk in ref.execute(
                f"PRAGMA table_info({table})"
            ):
                decl = col_type or ""
                if notnull:
                    decl += " NOT NULL"
                if dflt is not None:
                    decl += f" DEFAULT {dflt}"
                defs.append((name, decl.strip()))
            columns[table] = defs
        return columns
    finally:
        ref.close()


def backfill_added_columns(conn: sqlite3.Connection) -> None:
    """Back-fill columns a live table is missing versus ``schema.sql`` (idempotent).

    ``CREATE TABLE IF NOT EXISTS`` (in ``schema.sql``) never alters an existing
    table, so a column introduced *after* a database was created must be added
    with ``ALTER TABLE``. Rather than track those columns by hand, this diffs
    every existing table against the shape ``schema.sql`` declares
    (:func:`_reference_columns`) and adds any it lacks — so adding a column to a
    ``CREATE TABLE`` is the *only* edit needed for both fresh and existing
    databases. A table that does not exist yet is skipped: ``schema.sql`` creates
    it at its final shape.

    (SQLite's ``ADD COLUMN`` forbids a ``NOT NULL`` column without a constant
    default and non-constant defaults like ``CURRENT_TIMESTAMP`` — the same
    constraint the old hand-maintained ledger lived under, so any newly-added
    column must still be one ``ADD COLUMN`` can express.)
    """
    for table, columns in _reference_columns().items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table absent (PRAGMA returns no rows) — schema.sql creates it
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of :func:`migrate_to_multi_tenant`.

    Attributes:
        migrated: ``True`` if the database was upgraded on this call; ``False``
            if it was already multi-tenant (a no-op).
        legacy_handle: The handle of the legacy user, when one was created.
        token: The legacy user's raw token (shown once), when one was created.
        rows_backfilled: ``{table: row_count}`` of rows assigned to the legacy
            user, for the caller to report.
    """

    migrated: bool
    legacy_handle: str | None = None
    token: str | None = None
    rows_backfilled: dict[str, int] | None = None


def _schema_version(conn: sqlite3.Connection) -> int:
    """Return the stamped ``user_version`` pragma (0 on an unstamped DB)."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the column names of ``table`` (empty set if it does not exist)."""
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def needs_migration(conn: sqlite3.Connection) -> bool:
    """Report whether ``conn`` holds a *legacy* single-tenant database to migrate.

    True only when a per-user table already exists **without** a ``user_id``
    column. A brand-new, empty database (no tables yet) is *not* legacy — there
    is nothing to backfill, and ``schema.sql`` will create the final shape — so
    this returns ``False`` for it (and for an already-migrated DB).
    """
    if _schema_version(conn) >= MULTI_TENANT_VERSION:
        return False
    cols = _table_columns(conn, "episodes")
    return bool(cols) and "user_id" not in cols


def migrate_to_multi_tenant(
    conn: sqlite3.Connection, *, handle: str | None = None
) -> MigrationResult:
    """Upgrade a single-tenant database in place. Idempotent.

    Self-contained: it creates the ``users`` table, adds the ``user_id`` column
    to every per-user table, backfills it to a single legacy user, and rebuilds
    the tables whose uniqueness changed. It does **not** depend on ``schema.sql``
    having run (``init_db`` calls this *before* applying the new schema, because
    the new indexes reference ``user_id``). Runs the rebuild inside a transaction
    with foreign keys disabled, per SQLite's documented procedure.

    Args:
        conn: An open connection to the database to migrate.
        handle: Handle for the legacy user. Defaults to ``coaching_state.user_name``
            if set, else ``me``.

    Returns:
        A :class:`MigrationResult` describing what happened.
    """
    if not needs_migration(conn):
        return MigrationResult(migrated=False)

    # Read the legacy user's name (pre-migration coaching_state has no user_id,
    # so a plain key lookup works) before we touch the table.
    if handle is None:
        row = conn.execute(
            "SELECT value FROM coaching_state WHERE key = 'user_name'"
        ).fetchone()
        handle = (row["value"].strip() if row and row["value"] else "") or "me"

    token = generate_token()
    from prefrontal.memory.store import sha256_hex

    # Create the users table up front (the migration's INSERT needs it; a fresh
    # schema.sql run later is an idempotent no-op over it).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, handle TEXT NOT NULL UNIQUE, "
        "display_name TEXT, token_hash TEXT NOT NULL UNIQUE, "
        "status TEXT NOT NULL DEFAULT 'active', "
        "is_operator BOOLEAN NOT NULL DEFAULT 0, "
        "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:  # one transaction; commits on success, rolls back on error
            legacy_id = conn.execute(
                "INSERT INTO users (handle, display_name, token_hash, is_operator) "
                "VALUES (?, ?, ?, 1)",
                (handle, handle, sha256_hex(token)),
            ).lastrowid

            counts: dict[str, int] = {}
            for table in _USER_TABLES:
                cols = _table_columns(conn, table)
                if not cols:
                    continue  # table absent in this DB — nothing to migrate
                if "user_id" not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
                cur = conn.execute(
                    f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL",
                    (legacy_id,),
                )
                counts[table] = cur.rowcount

            # Rebuild the tables whose *uniqueness* changed, so the new composite
            # uniques replace the legacy global ones. (The leftover single-column
            # indexes are harmless and get superseded by schema.sql's user-scoped
            # ones; we drop the ones that would otherwise duplicate.)
            _rebuild_constraints(conn, legacy_id)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    conn.execute(f"PRAGMA user_version = {MULTI_TENANT_VERSION}")
    conn.commit()
    # Seed any coaching defaults / module state the legacy user is missing
    # (their existing values are preserved by seed_user_state's non-clobbering
    # writes), so a migrated user looks like a freshly provisioned one.
    seed_user_state(MemoryStore(conn).scoped(int(legacy_id)))
    return MigrationResult(
        migrated=True,
        legacy_handle=handle,
        token=token,
        rows_backfilled=counts,
    )


def _rebuild_constraints(conn: sqlite3.Connection, legacy_id: int) -> None:
    """Rebuild the tables whose *uniqueness* changed into per-user composites.

    SQLite cannot alter a column-level ``UNIQUE`` in place, so these tables use
    the create-copy-drop-rename dance. The leftover single-column indexes on the
    other tables are dropped (``schema.sql``, applied right after the migration,
    adds the leading-``user_id`` replacements). Must run with foreign keys off,
    inside the migration's transaction.
    """
    # Drop legacy indexes that schema.sql replaces with user-scoped equivalents,
    # so there is no stale/duplicate index after the upgrade.
    for legacy_index in (
        "idx_episodes_timestamp",
        "idx_outings_status",
        "idx_focus_sessions_status",
        "idx_commitments_external",
        "idx_commitments_start",
        "idx_todos_status",
        "idx_mail_account",
        "idx_mail_needs_action",
        "idx_mail_received",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {legacy_index}")

    _rebuild_table(
        conn,
        "coaching_state",
        legacy_id,
        new_table=(
            "CREATE TABLE coaching_state_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id INTEGER NOT NULL REFERENCES users(id), "
            "key TEXT NOT NULL, value TEXT, "
            "last_updated DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "source TEXT, UNIQUE (user_id, key))"
        ),
        columns="id, user_id, key, value, last_updated, source",
    )
    _rebuild_table(
        conn,
        "patterns",
        legacy_id,
        new_table=(
            "CREATE TABLE patterns_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id INTEGER NOT NULL REFERENCES users(id), "
            "pattern_type TEXT NOT NULL, context_key TEXT NOT NULL, "
            "observed_value REAL, predicted_value REAL, variance REAL, "
            "sample_size INTEGER DEFAULT 0, confidence REAL DEFAULT 0.0, "
            "last_updated DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "UNIQUE (user_id, pattern_type, context_key))"
        ),
        columns=(
            "id, user_id, pattern_type, context_key, observed_value, "
            "predicted_value, variance, sample_size, confidence, last_updated"
        ),
    )
    _rebuild_table(
        conn,
        "dismissed_conflicts",
        legacy_id,
        new_table=(
            "CREATE TABLE dismissed_conflicts_new ("
            "user_id INTEGER NOT NULL REFERENCES users(id), "
            "signature TEXT NOT NULL, "
            "dismissed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (user_id, signature))"
        ),
        columns="user_id, signature, dismissed_at",
    )
    _rebuild_table(
        conn,
        "places",
        legacy_id,
        new_table=(
            "CREATE TABLE places_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id INTEGER NOT NULL REFERENCES users(id), "
            "name TEXT NOT NULL, label TEXT, lat REAL NOT NULL, lon REAL NOT NULL, "
            "domain TEXT, "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "UNIQUE (user_id, name))"
        ),
        # domain is new (nullable): a legacy row has none, so it isn't copied and
        # defaults NULL in the rebuilt table.
        columns="id, user_id, name, label, lat, lon, created_at",
    )
    _rebuild_table(
        conn,
        "kind_feedback",
        legacy_id,
        new_table=(
            "CREATE TABLE kind_feedback_new ("
            "user_id INTEGER NOT NULL REFERENCES users(id), "
            "title TEXT NOT NULL, display TEXT NOT NULL, kind TEXT NOT NULL, "
            "llm_kind TEXT, "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (user_id, title))"
        ),
        columns="user_id, title, display, kind, llm_kind, created_at, updated_at",
    )
    _rebuild_table(
        conn,
        "mail_messages",
        legacy_id,
        new_table=(
            "CREATE TABLE mail_messages_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id INTEGER NOT NULL REFERENCES users(id), "
            "account TEXT NOT NULL, message_id TEXT NOT NULL, thread_id TEXT, "
            "sender_name TEXT, sender_email TEXT, subject TEXT, received_at DATETIME, "
            "snippet TEXT, body TEXT, unread BOOLEAN, "
            "needs_action BOOLEAN NOT NULL DEFAULT 0, urgency TEXT, category TEXT, "
            "waiting_on TEXT, summary TEXT, triage_source TEXT, "
            "policy TEXT NOT NULL DEFAULT 'full', "
            "todo_id INTEGER REFERENCES todos (id), "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "UNIQUE (user_id, account, message_id))"
        ),
        columns=(
            "id, user_id, account, message_id, thread_id, sender_name, sender_email, "
            "subject, received_at, snippet, body, unread, needs_action, urgency, "
            "category, waiting_on, summary, triage_source, policy, todo_id, created_at"
        ),
    )
    _rebuild_table(
        conn,
        "profile_cache",
        legacy_id,
        new_table=(
            "CREATE TABLE profile_cache_new ("
            "user_id INTEGER PRIMARY KEY REFERENCES users(id), "
            "text TEXT NOT NULL, source TEXT NOT NULL, model TEXT, "
            "structured TEXT, structured_hash TEXT, "
            "generated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        ),
        columns="user_id, text, source, model, structured, structured_hash, generated_at",
    )


def _rebuild_table(
    conn: sqlite3.Connection,
    table: str,
    legacy_id: int,
    *,
    new_table: str,
    columns: str,
) -> None:
    """Run SQLite's create-copy-drop-rename rebuild for one table.

    A no-op if ``table`` does not exist (e.g. a partial fixture DB). Any row that
    still has a NULL ``user_id`` after the backfill (there should be none) is
    coalesced to ``legacy_id`` so the new ``NOT NULL`` constraint holds.
    """
    if not _table_columns(conn, table):
        return
    conn.execute(f"DROP TABLE IF EXISTS {table}_new")
    conn.execute(new_table)
    select_cols = ", ".join(
        f"COALESCE(user_id, {legacy_id})" if c == "user_id" else c
        for c in (col.strip() for col in columns.split(","))
    )
    conn.execute(
        f"INSERT INTO {table}_new ({columns}) SELECT {select_cols} FROM {table}"
    )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")


def backfill_coaching_state_defaults(conn: sqlite3.Connection) -> None:
    """Seed any missing :data:`DEFAULT_COACHING_STATE` keys for every existing user.

    ``seed_user_state`` only runs at *provision* time, so a default key introduced
    **after** a user was created (e.g. ``home_zip``) never reaches them. This
    closes that gap idempotently: for each user, it inserts any absent default key
    without clobbering a value the user or a learner already set — the same
    absent-only contract ``seed_user_state`` uses. A fresh/empty database has no
    users yet (schema.sql + ``provision_user`` seed those), so it's a no-op there;
    a legacy pre-multi-tenant ``coaching_state`` is skipped until
    :func:`migrate_to_multi_tenant` has given it a ``user_id``.
    """
    if "user_id" not in _table_columns(conn, "coaching_state"):
        return  # legacy shape — the multi-tenant step runs first and adds user_id
    if not _table_columns(conn, "users"):
        return  # brand-new DB: no users to backfill yet
    store = MemoryStore(conn)
    for user in store.each_user():
        scoped = store.scoped(int(user["id"]))
        existing = scoped.all_state()
        for key, value, source in DEFAULT_COACHING_STATE:
            if key not in existing:
                scoped.set_state(key, value, source=source)


def backfill_project_ranks(conn: sqlite3.Connection) -> None:
    """Give every active project a forced ``rank`` if it lacks one (idempotent).

    The ``rank`` column (:file:`schema.sql`) is a contiguous 1..N priority order
    among each user's *active* projects. :func:`backfill_added_columns` adds the
    column to an existing database but leaves it NULL; this assigns ranks to any
    active project still missing one — per user, appended after whatever is
    already ranked, oldest (lowest id) first. Only touches NULL ranks, so it never
    disturbs an order the user has already set, and it is a no-op on a fresh/empty
    database (no projects yet) or before the column exists.
    """
    if "rank" not in _table_columns(conn, "projects"):
        return  # column not added yet (older DB mid-upgrade) — backfill runs first
    user_rows = conn.execute(
        "SELECT DISTINCT user_id FROM projects "
        "WHERE status = 'active' AND rank IS NULL"
    ).fetchall()
    for (user_id,) in user_rows:
        base = conn.execute(
            "SELECT COALESCE(MAX(rank), 0) FROM projects "
            "WHERE user_id = ? AND status = 'active'",
            (user_id,),
        ).fetchone()[0]
        unranked = conn.execute(
            "SELECT id FROM projects WHERE user_id = ? AND status = 'active' "
            "AND rank IS NULL ORDER BY id ASC",
            (user_id,),
        ).fetchall()
        for offset, (project_id,) in enumerate(unranked, start=1):
            conn.execute(
                "UPDATE projects SET rank = ? WHERE id = ?", (base + offset, project_id)
            )
    conn.commit()


def run_migrations(conn: sqlite3.Connection) -> MigrationResult:
    """Apply every pending schema migration, in order, before ``schema.sql`` runs.

    This is the single entry point for schema evolution — the one ordered ladder
    that replaces the two ad-hoc mechanisms that used to straddle ``schema.sql``
    (the multi-tenant upgrade ran before it, the column back-fill after, in
    :func:`prefrontal.memory.db.init_db`). Every step guards its own
    applicability, so the ladder is idempotent and safe to run on any database —
    fresh, legacy single-tenant, or already current.

    Steps, in order:

    1. **Multi-tenant scoping** — :func:`migrate_to_multi_tenant`, a no-op unless
       the database is a legacy single-tenant one.
    2. **Added-column back-fill** — :func:`backfill_added_columns`.
    3. **Coaching-state default back-fill** — :func:`backfill_coaching_state_defaults`,
       so a default key added after a user was provisioned (e.g. ``home_zip``)
       reaches existing users too. Absent-only; a no-op on a fresh/empty DB.
    4. **Project-rank back-fill** — :func:`backfill_project_ranks`, giving existing
       active projects a forced ``rank`` (the column added in step 2 arrives NULL).
       Absent-only; a no-op on a fresh/empty DB.

    All steps run *before* ``schema.sql`` is (re)applied: step 1 must, because the
    new schema's indexes reference ``user_id``; steps 2–4 safely can, because they
    only touch tables that already exist (fresh tables are created by ``schema.sql``
    at their final shape, and steps 3–4 no-op when there is nothing to back-fill yet).
    ``schema.sql`` then fills in any missing tables, indexes, and seed rows.

    Args:
        conn: An open connection to the database to upgrade.

    Returns:
        The :class:`MigrationResult` from the multi-tenant step (``migrated``
        is ``False`` when it was a no-op), so a caller that wants to surface the
        legacy user's one-time token — the explicit ``prefrontal
        migrate-multi-tenant`` command — can. :func:`prefrontal.memory.db.init_db`
        discards it.
    """
    result = MigrationResult(migrated=False)
    if needs_migration(conn):
        result = migrate_to_multi_tenant(conn)
    backfill_added_columns(conn)
    backfill_coaching_state_defaults(conn)
    backfill_project_ranks(conn)
    return result
