"""Tests for the low-level connection/init helpers (prefrontal/memory/db.py)."""

from __future__ import annotations

from prefrontal.memory.db import connect


def test_connect_expands_user_home(tmp_path, monkeypatch):
    """A '~'-prefixed db_path opens under the real home, not a literal '~' dir.

    Regression: mkdir expanded the path but sqlite3.connect got the raw db_path,
    so '~/prefrontal.db' created the home directory yet opened a file literally
    named '~/prefrontal.db' relative to the CWD.
    """
    monkeypatch.setenv("HOME", str(tmp_path))  # expanduser() resolves '~' via $HOME
    monkeypatch.chdir(tmp_path)  # so a literal '~' dir (the bug) would land here

    conn = connect("~/nested/prefrontal.db")
    try:
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
    finally:
        conn.close()

    # Opened under the expanded home…
    assert (tmp_path / "nested" / "prefrontal.db").exists()
    # …and did NOT create a literal '~' directory under the CWD.
    assert not (tmp_path / "~").exists()


def test_connect_memory_is_untouched():
    """':memory:' is passed through verbatim (no path expansion, no mkdir)."""
    conn = connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (x)")
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()
