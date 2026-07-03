"""Shared base for the per-domain repository mixins.

Every repo in this package is mixed into
:class:`~prefrontal.memory.store.MemoryStore`, which supplies the connection and
the per-user scoping guard the repos rely on (``self.conn`` / ``self._uid()``).
This base gives them the few read/write *shapes* they had each re-implemented by
hand, so a repo method reads as SQL + intent rather than boilerplate — and
row→dict mapping is one idiom instead of the ``[dict(r) for r in …]`` /
``_row_to_dict(…)`` mix that grew up across the repos.

The helpers are deliberately thin wrappers over ``self.conn``; they hold no
scoping logic of their own (the caller still passes ``self._uid()`` in the
params), so nothing about the multi-tenant guarantee moves here.
"""

from __future__ import annotations

import sqlite3
from typing import Any


class Repo:
    """Mixin base for the memory repositories: shared query shapes.

    ``conn`` (and the ``_uid()`` scoping guard the query params use) are provided
    by :class:`~prefrontal.memory.store.MemoryStore`, which mixes every repo — and
    therefore this base — together. Declared here only so the helpers read clearly.
    """

    conn: sqlite3.Connection

    def _query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run ``sql`` and return every row as a plain ``dict``."""
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        """Run ``sql`` and return the first row as a ``dict``, or ``None``."""
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def _upsert_returning_id(
        self,
        insert_sql: str,
        params: tuple[Any, ...],
        *,
        select_sql: str,
        select_params: tuple[Any, ...],
    ) -> int:
        """``INSERT … ON CONFLICT`` then read the row id back; commit once.

        ``cursor.lastrowid`` is unreliable on the ``ON CONFLICT DO UPDATE`` path
        (it may hold a stale rowid rather than the conflicted row's), so the id is
        always fetched with the follow-up ``SELECT`` on the unique key — exactly
        what the repos that upsert did by hand. Returns ``0`` if the select finds
        nothing (it always should, right after the insert/update).
        """
        self.conn.execute(insert_sql, params)
        self.conn.commit()
        row = self.conn.execute(select_sql, select_params).fetchone()
        return int(row[0]) if row is not None else 0
