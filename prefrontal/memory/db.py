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
    # check_same_thread=False lets a single shared connection be used from the
    # webhook server's threadpool (FastAPI runs sync endpoints off-loop). Writes
    # are low-volume, human-paced events and SQLite serializes them internally,
    # so a shared connection is safe here.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> sqlite3.Connection:
    """Create and seed the memory database, returning an open connection.

    Applies ``schema.sql`` against ``db_path``. The script is idempotent
    (``CREATE TABLE IF NOT EXISTS`` plus ``INSERT OR IGNORE`` seed rows), so
    calling this repeatedly is safe and will not clobber existing data.

    Args:
        db_path: Filesystem path to the database file (or ``":memory:"``).

    Returns:
        An open :class:`sqlite3.Connection` with the schema applied.
    """
    conn = connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    return conn
