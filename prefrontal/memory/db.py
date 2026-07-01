"""SQLite connection management and schema initialization.

This module is intentionally thin: it knows how to open a correctly configured
connection and how to apply ``schema.sql``. All higher-level reads and writes
live in :mod:`prefrontal.memory.store`.

Design choices:

- We use the Python standard library :mod:`sqlite3` rather than an ORM. The
  schema is small, stable, and hand-tuned, and avoiding a dependency keeps the
  "local first, few moving parts" promise of the project.
- Connections use :class:`sqlite3.Row` so callers get mapping-style rows that
  :class:`~prefrontal.memory.store.MemoryStore` converts into plain ``dict``\\ s.
- Foreign-key enforcement is enabled on every connection (SQLite defaults it
  off for backward compatibility).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: Path to the bundled schema file, resolved relative to this module so it works
#: regardless of the current working directory or installation location.
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: str) -> sqlite3.Connection:
    """Open a configured SQLite connection.

    The connection returns :class:`sqlite3.Row` rows and has foreign-key
    enforcement enabled. The parent directory of ``db_path`` is created if it
    does not yet exist.

    Args:
        db_path: Filesystem path to the database file. The special value
            ``":memory:"`` opens a private in-memory database (used by tests).

    Returns:
        An open :class:`sqlite3.Connection`. The caller owns it and is
        responsible for closing it (directly or via a ``with`` block).
    """
    if db_path != ":memory:":
        Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False is needed because the webhook server hands a
    # store's connection between threadpool tasks. It only disables sqlite3's
    # owning-thread *check* — it does not make a connection safe for concurrent
    # use. A single connection used from several threads at once interleaves
    # statements and corrupts result sets, so the server opens one connection
    # per thread (see MemoryStore.threaded); within a thread, access is serial.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if db_path != ":memory:":
        # WAL lets readers and a single writer proceed concurrently across
        # connections, which the per-thread webhook store relies on. It also
        # avoids the rollback-journal deadlock where two connections each hold a
        # shared lock and both try to upgrade to a write lock (which a busy
        # timeout cannot break). WAL is a persistent, per-file setting; applying
        # it on every connect is idempotent and harmless for the CLI and tests.
        conn.execute("PRAGMA journal_mode = WAL")
    # A writer briefly excludes other writers; wait for it rather than raising
    # "database is locked" immediately. Writes are short and human-paced.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: str) -> sqlite3.Connection:
    """Create and seed the memory database, returning an open connection.

    Runs the ordered migration ladder
    (:func:`prefrontal.memory.migrate.run_migrations`) and then applies
    ``schema.sql``. The migrations bring an existing database up to the current
    shape *before* the schema is (re)applied — a legacy single-tenant database is
    upgraded in place (a legacy user is created and every per-user row is
    backfilled to it) and any columns added to existing tables since their
    original definition are back-filled. Both are idempotent and no-ops on fresh
    / already-current databases.

    ``schema.sql`` itself is idempotent (``CREATE TABLE IF NOT EXISTS``), so
    calling this repeatedly is safe and never clobbers existing data. The
    auto-created legacy user's one-time token is *not* surfaced here; an operator
    rotates it with ``prefrontal user rotate`` (or runs the explicit
    ``prefrontal migrate-multi-tenant`` first to capture it).

    Args:
        db_path: Filesystem path to the database file (or ``":memory:"``).

    Returns:
        An open :class:`sqlite3.Connection` with the schema applied.
    """
    conn = connect(db_path)
    # All schema evolution lives in one ordered ladder in the migrate module, run
    # before schema.sql (the multi-tenant step must precede it — the new schema's
    # indexes reference ``user_id``). Imported lazily to avoid an import cycle
    # (migrate imports the store, which imports this module).
    from prefrontal.memory.migrate import run_migrations

    run_migrations(conn)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    return conn
