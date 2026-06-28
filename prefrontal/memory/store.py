"""High-level read/write API over the behavioral memory database.

:class:`MemoryStore` is the single object the rest of Prefrontal uses to touch
the memory layer. It wraps a :class:`sqlite3.Connection` and exposes intention-
revealing methods for the three tables:

- ``episodes`` — :meth:`MemoryStore.log_episode`, :meth:`MemoryStore.recent_episodes`,
  :meth:`MemoryStore.episodes_by_type`.
- ``patterns`` — :meth:`MemoryStore.upsert_pattern`, :meth:`MemoryStore.get_patterns`.
- ``coaching_state`` — :meth:`MemoryStore.get_state`, :meth:`MemoryStore.set_state`,
  :meth:`MemoryStore.all_state`.

Rows are returned as plain ``dict``\\ s so callers never have to think about
:class:`sqlite3.Row`. Writes commit immediately — the access patterns here are
low-volume, human-paced events, so per-write commits keep the on-disk state
trustworthy without any meaningful cost.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from prefrontal.memory.db import connect, init_db

#: Allowed values for ``episodes.episode_type`` (see docs/schema.md).
EPISODE_TYPES = ("departure", "task", "checkin", "reminder")
#: Allowed values for ``episodes.outcome``.
OUTCOMES = ("success", "miss", "partial")


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a :class:`sqlite3.Row` to a ``dict`` (or pass through ``None``)."""
    return dict(row) if row is not None else None


class MemoryStore:
    """A high-level, dict-returning interface to the Prefrontal memory tables.

    A ``MemoryStore`` borrows an open connection; it does not close it on its
    own unless created via :meth:`open`, whose context manager does. This lets
    long-lived processes (the webhook server) share one connection while tests
    and the CLI use the convenient context-managed form.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Wrap an already-open connection.

        Args:
            conn: A connection produced by :func:`prefrontal.memory.db.connect`
                or :func:`prefrontal.memory.db.init_db`.
        """
        self.conn = conn

    # -- construction helpers ------------------------------------------------

    @classmethod
    @contextmanager
    def open(cls, db_path: str, *, initialize: bool = True) -> Iterator[MemoryStore]:
        """Open a store as a context manager, closing the connection on exit.

        Args:
            db_path: Path to the database file (or ``":memory:"``).
            initialize: If ``True`` (default), apply the schema first so the
                tables and seed rows are guaranteed to exist.

        Yields:
            A :class:`MemoryStore` bound to a fresh connection.
        """
        conn = init_db(db_path) if initialize else connect(db_path)
        try:
            yield cls(conn)
        finally:
            conn.close()

    # -- episodes ------------------------------------------------------------

    def log_episode(
        self,
        episode_type: str,
        *,
        predicted_value: float | None = None,
        actual_value: float | None = None,
        acknowledged: bool | None = None,
        channel: str | None = None,
        context: str | None = None,
        outcome: str | None = None,
        notes: str | None = None,
        timestamp: str | None = None,
    ) -> int:
        """Insert a raw outcome record and return its new id.

        Args:
            episode_type: One of :data:`EPISODE_TYPES` — the kind of interaction.
            predicted_value: What the agent estimated (e.g. minutes).
            actual_value: What actually happened.
            acknowledged: Whether the user responded to the trigger.
            channel: Delivery channel (``notification``, ``sound``, ``tts``, ``sms``).
            context: Free-text context — location, time of day, task type.
            outcome: One of :data:`OUTCOMES`.
            notes: Optional agent or user annotation.
            timestamp: Optional ISO timestamp; defaults to the DB's
                ``CURRENT_TIMESTAMP`` when omitted.

        Returns:
            The auto-incremented ``id`` of the inserted episode.
        """
        columns = [
            "episode_type",
            "predicted_value",
            "actual_value",
            "acknowledged",
            "channel",
            "context",
            "outcome",
            "notes",
        ]
        values: list[Any] = [
            episode_type,
            predicted_value,
            actual_value,
            acknowledged,
            channel,
            context,
            outcome,
            notes,
        ]
        if timestamp is not None:
            columns.append("timestamp")
            values.append(timestamp)
        placeholders = ", ".join("?" for _ in columns)
        cur = self.conn.execute(
            f"INSERT INTO episodes ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_episode(self, episode_id: int) -> dict[str, Any] | None:
        """Return a single episode by id, or ``None`` if it does not exist."""
        row = self.conn.execute(
            "SELECT * FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        return _row_to_dict(row)

    def recent_episodes(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent episodes, newest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of episode dicts ordered by ``timestamp`` descending.
        """
        rows = self.conn.execute(
            "SELECT * FROM episodes ORDER BY timestamp DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def episodes_by_type(
        self, episode_type: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return recent episodes of a single ``episode_type``, newest first.

        Args:
            episode_type: One of :data:`EPISODE_TYPES`.
            limit: Maximum number of rows to return.

        Returns:
            A list of matching episode dicts.
        """
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE episode_type = ? "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            (episode_type, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- patterns ------------------------------------------------------------

    def upsert_pattern(
        self,
        pattern_type: str,
        context_key: str,
        *,
        observed_value: float | None = None,
        predicted_value: float | None = None,
        variance: float | None = None,
        sample_size: int = 0,
        confidence: float = 0.0,
    ) -> int:
        """Insert or update the derived pattern for ``(pattern_type, context_key)``.

        There is one pattern row per ``(pattern_type, context_key)`` pair (a
        unique constraint), so the summarizer can recompute and write in place.
        ``last_updated`` is refreshed to ``CURRENT_TIMESTAMP`` on every call.

        Args:
            pattern_type: e.g. ``time_estimation``, ``channel_response``,
                ``drift``, ``context_switch``.
            context_key: What the pattern applies to (e.g. ``departure``,
                ``morning``, ``work_block``).
            observed_value: Average or median observed.
            predicted_value: What was being estimated.
            variance: Difference; positive means the agent underestimated.
            sample_size: Number of episodes the pattern is derived from.
            confidence: 0.0–1.0; low until the sample size is meaningful.

        Returns:
            The ``id`` of the inserted or updated pattern row.
        """
        self.conn.execute(
            """
            INSERT INTO patterns (
                pattern_type, context_key, observed_value, predicted_value,
                variance, sample_size, confidence, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (pattern_type, context_key) DO UPDATE SET
                observed_value  = excluded.observed_value,
                predicted_value = excluded.predicted_value,
                variance        = excluded.variance,
                sample_size     = excluded.sample_size,
                confidence      = excluded.confidence,
                last_updated    = CURRENT_TIMESTAMP
            """,
            (
                pattern_type,
                context_key,
                observed_value,
                predicted_value,
                variance,
                sample_size,
                confidence,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM patterns WHERE pattern_type = ? AND context_key = ?",
            (pattern_type, context_key),
        ).fetchone()
        return int(row["id"])

    def get_patterns(
        self, pattern_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Return derived patterns, optionally filtered by type.

        Args:
            pattern_type: If given, only patterns of this type are returned.

        Returns:
            A list of pattern dicts, highest ``confidence`` first.
        """
        if pattern_type is None:
            rows = self.conn.execute(
                "SELECT * FROM patterns ORDER BY confidence DESC, id ASC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM patterns WHERE pattern_type = ? "
                "ORDER BY confidence DESC, id ASC",
                (pattern_type,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- coaching state ------------------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        """Return a coaching-state value by key.

        Args:
            key: The preference name.
            default: Value to return if the key is absent.

        Returns:
            The stored value, or ``default`` if the key does not exist.
        """
        row = self.conn.execute(
            "SELECT value FROM coaching_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else default

    def set_state(self, key: str, value: str, source: str = "inferred") -> None:
        """Insert or update a coaching-state preference.

        Args:
            key: The preference name (unique).
            value: The value to store (stored as text).
            source: ``explicit`` if the user set it, ``inferred`` if the agent
                derived it.
        """
        self.conn.execute(
            """
            INSERT INTO coaching_state (key, value, source, last_updated)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET
                value        = excluded.value,
                source       = excluded.source,
                last_updated = CURRENT_TIMESTAMP
            """,
            (key, value, source),
        )
        self.conn.commit()

    def all_state(self) -> dict[str, dict[str, Any]]:
        """Return the entire coaching state keyed by preference name.

        Returns:
            A mapping of ``key`` -> the full row dict (``value``, ``source``,
            ``last_updated``, ...), convenient for the summarizer.
        """
        rows = self.conn.execute(
            "SELECT * FROM coaching_state ORDER BY key ASC"
        ).fetchall()
        return {r["key"]: dict(r) for r in rows}
