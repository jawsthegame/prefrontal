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

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from prefrontal.memory.db import connect, init_db

#: Allowed values for ``episodes.episode_type`` (see docs/schema.md).
EPISODE_TYPES = ("departure", "task", "checkin", "reminder", "mail")
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

    def all_episodes(self) -> list[dict[str, Any]]:
        """Return every episode in chronological order.

        Used by the pattern-computation pass, which aggregates the full history.
        Volume is single-user and human-paced, so loading all rows is fine.

        Returns:
            A list of episode dicts ordered by ``timestamp`` then ``id`` ascending.
        """
        rows = self.conn.execute(
            "SELECT * FROM episodes ORDER BY timestamp ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def episodes_since(self, since: str) -> list[dict[str, Any]]:
        """Return episodes at or after a UTC timestamp, newest first.

        Args:
            since: UTC timestamp (``YYYY-MM-DD HH:MM:SS``); inclusive lower bound.

        Returns:
            A list of episode dicts. Used by the morning briefing's "what slipped
            recently" section.
        """
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE timestamp >= ? ORDER BY timestamp DESC, id DESC",
            (since,),
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

    # -- outings (Location-Aware Task Anchor) --------------------------------

    def start_outing(
        self,
        intention: str,
        time_window_minutes: float,
        *,
        home_lat: float | None = None,
        home_lon: float | None = None,
        departure_at: str | None = None,
    ) -> int:
        """Record a declared outing and return its id.

        Args:
            intention: The stated mission ("getting coffee").
            time_window_minutes: The stated "back in N minutes" window.
            home_lat: Optional baseline latitude.
            home_lon: Optional baseline longitude.
            departure_at: Optional ISO timestamp for the departure; defaults to
                the DB's ``CURRENT_TIMESTAMP``. Mainly useful for tests.

        Returns:
            The new outing's ``id``.
        """
        columns = ["intention", "time_window_minutes", "home_lat", "home_lon"]
        values: list[Any] = [intention, time_window_minutes, home_lat, home_lon]
        if departure_at is not None:
            columns.append("departure_at")
            values.append(departure_at)
        placeholders = ", ".join("?" for _ in columns)
        cur = self.conn.execute(
            f"INSERT INTO outings ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_outing(self, outing_id: int) -> dict[str, Any] | None:
        """Return a single outing by id, or ``None`` if it does not exist."""
        row = self.conn.execute(
            "SELECT * FROM outings WHERE id = ?", (outing_id,)
        ).fetchone()
        return _row_to_dict(row)

    def active_outings(self) -> list[dict[str, Any]]:
        """Return active outings with a computed ``elapsed_minutes`` field.

        Elapsed time is computed in SQL against ``CURRENT_TIMESTAMP`` (both
        timestamps are UTC), so callers never have to deal with timezones.

        Returns:
            A list of outing dicts, each including ``elapsed_minutes``.
        """
        rows = self.conn.execute(
            "SELECT *, (julianday('now') - julianday(departure_at)) * 1440.0 "
            "AS elapsed_minutes FROM outings WHERE status = 'active' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def most_recent_active_outing(self) -> dict[str, Any] | None:
        """Return the newest active outing (with ``elapsed_minutes``), or ``None``."""
        row = self.conn.execute(
            "SELECT *, (julianday('now') - julianday(departure_at)) * 1440.0 "
            "AS elapsed_minutes FROM outings WHERE status = 'active' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _row_to_dict(row)

    def set_outing_level(self, outing_id: int, level: str) -> None:
        """Record the highest escalation level that has fired for an outing.

        Args:
            outing_id: The outing to update.
            level: One of ``none``/``soft``/``firm``/``call``.
        """
        self.conn.execute(
            "UPDATE outings SET last_level = ? WHERE id = ?", (level, outing_id)
        )
        self.conn.commit()

    def close_outing(
        self, outing_id: int, status: str = "returned"
    ) -> dict[str, Any] | None:
        """Close an active outing and return it with a computed ``actual_minutes``.

        Args:
            outing_id: The outing to close.
            status: Terminal status to set (``returned`` or ``abandoned``).

        Returns:
            The closed outing dict including ``actual_minutes`` (minutes between
            departure and return), or ``None`` if the outing was not active.
        """
        cur = self.conn.execute(
            "UPDATE outings SET status = ?, returned_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'active'",
            (status, outing_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        row = self.conn.execute(
            "SELECT *, (julianday(returned_at) - julianday(departure_at)) * 1440.0 "
            "AS actual_minutes FROM outings WHERE id = ?",
            (outing_id,),
        ).fetchone()
        return _row_to_dict(row)

    def recent_outings(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent outings (any status), newest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of outing dicts ordered by ``id`` descending.
        """
        rows = self.conn.execute(
            "SELECT * FROM outings ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- commitments (schedule for impact analysis) --------------------------

    def upsert_commitment(
        self,
        *,
        title: str,
        start_at: str,
        external_id: str | None = None,
        end_at: str | None = None,
        location: str | None = None,
        lead_minutes: float = 10.0,
        hardness: str = "soft",
        source: str = "calendar",
    ) -> tuple[int, bool]:
        """Insert or update a commitment, returning ``(id, created)``.

        When ``external_id`` is given and already exists, the row is updated in
        place (and re-activated) — so re-syncing a calendar is idempotent.
        Timestamps should already be normalized to UTC (see
        :func:`prefrontal.commitments.to_utc`).

        Args:
            title: Commitment title.
            start_at: UTC start timestamp (``YYYY-MM-DD HH:MM:SS``).
            external_id: Calendar event id, or ``None`` for a manual entry.
            end_at: Optional UTC end timestamp.
            location: Optional location.
            lead_minutes: Travel+prep buffer needed before ``start_at``.
            hardness: ``hard`` or ``soft``.
            source: ``calendar`` or ``manual``.

        Returns:
            ``(id, created)`` where ``created`` is ``True`` for a new row.
        """
        if external_id is not None:
            existing = self.conn.execute(
                "SELECT id FROM commitments WHERE external_id = ?", (external_id,)
            ).fetchone()
            if existing is not None:
                self.conn.execute(
                    "UPDATE commitments SET title = ?, start_at = ?, end_at = ?, "
                    "location = ?, lead_minutes = ?, hardness = ?, source = ?, "
                    "status = 'active', updated_at = CURRENT_TIMESTAMP "
                    "WHERE external_id = ?",
                    (title, start_at, end_at, location, lead_minutes, hardness,
                     source, external_id),
                )
                self.conn.commit()
                return int(existing["id"]), False

        cur = self.conn.execute(
            "INSERT INTO commitments (external_id, title, start_at, end_at, "
            "location, lead_minutes, hardness, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (external_id, title, start_at, end_at, location, lead_minutes,
             hardness, source),
        )
        self.conn.commit()
        return int(cur.lastrowid), True

    def get_commitment(self, commitment_id: int) -> dict[str, Any] | None:
        """Return a single commitment by id, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM commitments WHERE id = ?", (commitment_id,)
        ).fetchone()
        return _row_to_dict(row)

    def upcoming_commitments(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return active commitments starting now or later, soonest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of commitment dicts ordered by ``start_at`` ascending.
        """
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE status = 'active' "
            "AND start_at >= datetime('now') ORDER BY start_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def commitments_between(self, start: str, end: str) -> list[dict[str, Any]]:
        """Return active commitments starting in ``[start, end)``, soonest first.

        Args:
            start: Inclusive UTC lower bound (``YYYY-MM-DD HH:MM:SS``).
            end: Exclusive UTC upper bound.

        Returns:
            A list of commitment dicts (e.g. "today's" commitments for the briefing).
        """
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE status = 'active' "
            "AND start_at >= ? AND start_at < ? ORDER BY start_at ASC",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]

    def cancel_commitment(self, commitment_id: int) -> bool:
        """Mark a commitment cancelled. Returns ``True`` if a row changed."""
        cur = self.conn.execute(
            "UPDATE commitments SET status = 'cancelled', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'active'",
            (commitment_id,),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # -- todos (open loops fitted into free time) ----------------------------

    def add_todo(
        self,
        title: str,
        *,
        notes: str | None = None,
        estimate_minutes: float | None = None,
        priority: int = 1,
        deadline: str | None = None,
        energy: str | None = None,
    ) -> int:
        """Insert an open todo and return its id.

        Args:
            title: What needs doing.
            notes: Optional detail.
            estimate_minutes: How long it'll take (enables fitting into windows).
            priority: 0 low / 1 normal / 2 high / 3 urgent.
            deadline: Optional UTC deadline (``YYYY-MM-DD HH:MM:SS``).
            energy: Optional ``low``/``medium``/``high`` hint.

        Returns:
            The new todo's id.
        """
        cur = self.conn.execute(
            "INSERT INTO todos (title, notes, estimate_minutes, priority, "
            "deadline, energy) VALUES (?, ?, ?, ?, ?, ?)",
            (title, notes, estimate_minutes, priority, deadline, energy),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_todo(self, todo_id: int) -> dict[str, Any] | None:
        """Return a single todo by id, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM todos WHERE id = ?", (todo_id,)
        ).fetchone()
        return _row_to_dict(row)

    def open_todos(self) -> list[dict[str, Any]]:
        """Return open todos, highest priority then soonest deadline first.

        Returns:
            A list of todo dicts with ``status = 'open'``.
        """
        rows = self.conn.execute(
            "SELECT * FROM todos WHERE status = 'open' "
            "ORDER BY priority DESC, (deadline IS NULL), deadline ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def close_todo(self, todo_id: int, status: str = "done") -> bool:
        """Mark a todo ``done`` or ``dropped``. Returns ``True`` if it changed.

        Args:
            todo_id: The todo to close.
            status: ``done`` or ``dropped``.
        """
        completed = "CURRENT_TIMESTAMP" if status == "done" else "NULL"
        cur = self.conn.execute(
            f"UPDATE todos SET status = ?, completed_at = {completed}, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'open'",
            (status, todo_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def cancel_missing_calendar(self, keep_external_ids: set[str]) -> int:
        """Cancel future calendar commitments absent from a fresh sync.

        Manual commitments are never touched. Pruning is **feed-aware**: an
        ``external_id`` may be namespaced ``feed:id`` (e.g. ``personal:…``,
        ``work:…``), and only commitments whose namespace appears in this batch
        are eligible for cancellation. That way syncing one calendar never
        cancels another calendar's events. If the batch uses no namespaces, the
        legacy behavior applies (prune any missing calendar commitment).

        Args:
            keep_external_ids: The ``external_id``\\ s present in the new sync.

        Returns:
            The number of commitments cancelled.
        """
        keep = set(keep_external_ids)
        namespaces = {e.split(":", 1)[0] for e in keep if ":" in e}
        rows = self.conn.execute(
            "SELECT id, external_id FROM commitments WHERE source = 'calendar' "
            "AND status = 'active' AND start_at >= datetime('now')"
        ).fetchall()
        cancelled = 0
        for row in rows:
            eid = row["external_id"]
            if eid in keep:
                continue
            if namespaces:
                ns = eid.split(":", 1)[0] if eid and ":" in eid else None
                if ns not in namespaces:
                    continue  # belongs to a feed not part of this sync; leave it
            self.conn.execute(
                "UPDATE commitments SET status = 'cancelled', "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row["id"],),
            )
            cancelled += 1
        self.conn.commit()
        return cancelled

    # -- Dismissed possible-conflicts ----------------------------------------

    def dismiss_conflict(self, signature: str) -> None:
        """Record that the user dismissed a possible-conflict pair (idempotent)."""
        self.conn.execute(
            "INSERT OR IGNORE INTO dismissed_conflicts (signature) VALUES (?)",
            (signature,),
        )
        self.conn.commit()

    def dismissed_conflicts(self) -> set[str]:
        """Return the set of dismissed possible-conflict signatures."""
        rows = self.conn.execute(
            "SELECT signature FROM dismissed_conflicts"
        ).fetchall()
        return {r["signature"] for r in rows}

    # -- mail (ingested + triaged email) -------------------------------------

    def seen_mail_ids(self, account: str | None = None) -> set[str]:
        """Return the ``message_id``\\ s already ingested, for dedup.

        Args:
            account: If given, scope to one account's messages; otherwise return
                ids across all accounts. Dedup is account-scoped (the unique
                constraint is on ``(account, message_id)``), so callers ingesting
                one account should pass it.

        Returns:
            A set of ``message_id`` strings.
        """
        if account is None:
            rows = self.conn.execute("SELECT message_id FROM mail_messages").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT message_id FROM mail_messages WHERE account = ?", (account,)
            ).fetchall()
        return {r["message_id"] for r in rows}

    def record_mail(
        self,
        *,
        account: str,
        message_id: str,
        policy: str = "full",
        thread_id: str | None = None,
        sender_name: str | None = None,
        sender_email: str | None = None,
        subject: str | None = None,
        received_at: str | None = None,
        snippet: str | None = None,
        body: str | None = None,
        unread: bool | None = None,
        needs_action: bool = False,
        urgency: str | None = None,
        category: str | None = None,
        waiting_on: str | None = None,
        summary: str | None = None,
        triage_source: str | None = None,
        todo_id: int | None = None,
    ) -> int:
        """Insert a triaged message and return its row id.

        Dedup is the caller's responsibility (see :meth:`seen_mail_ids`); a
        duplicate ``(account, message_id)`` raises ``sqlite3.IntegrityError``.
        Body/snippet should already have been dropped for a ``signals`` account
        by the normalizer — this method stores exactly what it is given.

        Returns:
            The new ``mail_messages`` row id.
        """
        cur = self.conn.execute(
            "INSERT INTO mail_messages ("
            "account, message_id, thread_id, sender_name, sender_email, subject, "
            "received_at, snippet, body, unread, needs_action, urgency, category, "
            "waiting_on, summary, triage_source, policy, todo_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account, message_id, thread_id, sender_name, sender_email, subject,
                received_at, snippet, body, unread, needs_action, urgency, category,
                waiting_on, summary, triage_source, policy, todo_id,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_mail(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recently ingested messages, newest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of ``mail_messages`` dicts ordered by ``received_at`` (then
            ``id``) descending.
        """
        rows = self.conn.execute(
            "SELECT * FROM mail_messages "
            "ORDER BY (received_at IS NULL), received_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mail_needing_action(self) -> list[dict[str, Any]]:
        """Return ingested messages still flagged ``needs_action``, newest first.

        A message stays here until the linked todo is closed; messages whose
        ``todo_id`` todo is no longer open are excluded, so resolving the open
        loop clears the mail from the action list.

        Returns:
            A list of ``mail_messages`` dicts.
        """
        rows = self.conn.execute(
            "SELECT m.* FROM mail_messages m "
            "LEFT JOIN todos t ON m.todo_id = t.id "
            "WHERE m.needs_action = 1 "
            "AND (m.todo_id IS NULL OR t.status = 'open') "
            "ORDER BY (m.received_at IS NULL), m.received_at DESC, m.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    # -- Task decompositions -------------------------------------------------

    def set_decomposition(
        self,
        todo_id: int,
        *,
        first_step: str,
        first_step_minutes: float | None,
        steps: list[str],
        source: str,
    ) -> None:
        """Store (or replace) a todo's decomposition. ``steps`` is JSON-encoded."""
        self.conn.execute(
            "INSERT OR REPLACE INTO todo_decompositions "
            "(todo_id, first_step, first_step_minutes, steps, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (todo_id, first_step, first_step_minutes, json.dumps(steps), source),
        )
        self.conn.commit()

    def get_decomposition(self, todo_id: int) -> dict[str, Any] | None:
        """Return a todo's decomposition (``steps`` decoded), or ``None``."""
        row = self.conn.execute(
            "SELECT first_step, first_step_minutes, steps, source "
            "FROM todo_decompositions WHERE todo_id = ?",
            (todo_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["steps"] = json.loads(d["steps"]) if d["steps"] else []
        except (ValueError, TypeError):
            d["steps"] = []
        return d
