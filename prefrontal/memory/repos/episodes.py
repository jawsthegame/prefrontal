"""Episode logging and queries.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory._helpers import (
    _row_to_dict,
)
from prefrontal.memory.repos._base import Repo


class EpisodesRepo(Repo):
    """Episode logging and queries."""

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
        energy: str | None = None,
        category: str | None = None,
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
            energy: The task's energy load (``low``/``medium``/``high``) when
                known, for context-conditioned bias (learning §5).
            category: The task's category when known, likewise.

        Returns:
            The auto-incremented ``id`` of the inserted episode.
        """
        columns = [
            "user_id",
            "episode_type",
            "predicted_value",
            "actual_value",
            "acknowledged",
            "channel",
            "context",
            "outcome",
            "notes",
            "energy",
            "category",
        ]
        values: list[Any] = [
            self._uid(),
            episode_type,
            predicted_value,
            actual_value,
            acknowledged,
            channel,
            context,
            outcome,
            notes,
            energy,
            category,
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

    def set_episode_outcome(
        self, episode_id: int, *, outcome: str, acknowledged: bool = True
    ) -> bool:
        """Resolve a previously-logged episode's ``outcome`` (and ``acknowledged``).

        For episodes logged in a *pending* state (``outcome`` NULL) that resolve
        later — e.g. a panic first-step nudge marked ``success`` on a one-tap
        "Did it", or swept to ``miss`` when left unanswered. Scoped to the caller;
        returns ``True`` if a row changed.

        Args:
            episode_id: The episode to update.
            outcome: One of :data:`OUTCOMES`.
            acknowledged: Whether the user responded (a tap ⇒ ``True``).
        """
        cur = self.conn.execute(
            "UPDATE episodes SET outcome = ?, acknowledged = ? "
            "WHERE id = ? AND user_id = ?",
            (outcome, acknowledged, episode_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def reclassify_episode_outcome(self, episode_id: int, *, outcome: str) -> bool:
        """Change **only** an episode's ``outcome``, leaving everything else intact.

        Unlike :meth:`set_episode_outcome` (which also stamps ``acknowledged``),
        this touches the single column — for a backfill that re-labels an already-
        resolved outcome (the one-off drop cleanup that downgrades hygiene
        todo-drops from ``miss`` to ``discarded``). Scoped to the caller; returns
        ``True`` if a row changed.
        """
        cur = self.conn.execute(
            "UPDATE episodes SET outcome = ? WHERE id = ? AND user_id = ?",
            (outcome, episode_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def clear_episode_actual_value(self, episode_id: int) -> bool:
        """Null out an episode's ``actual_value``, leaving everything else intact.

        For a backfill that retracts a mis-recorded duration from the
        time-estimation signal without deleting the episode (it may still carry a
        drift outcome) — the one-off cleanup that stops deliberately-switched focus
        blocks from feeding ``time_estimation``. Scoped to the caller; returns
        ``True`` if a row changed.
        """
        cur = self.conn.execute(
            "UPDATE episodes SET actual_value = NULL WHERE id = ? AND user_id = ?",
            (episode_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def pending_episodes(
        self, episode_type: str, *, before: str
    ) -> list[dict[str, Any]]:
        """Return unresolved episodes (``outcome`` NULL) of a type logged at/before ``before``.

        Used to sweep still-open episodes to a terminal outcome once their ack
        window has passed (see :func:`prefrontal.panic.sweep_pending_panic_steps`).

        Args:
            episode_type: One of :data:`EPISODE_TYPES`.
            before: UTC timestamp (``YYYY-MM-DD HH:MM:SS``); inclusive upper bound
                on ``timestamp`` (lexicographic compare, safe for the fixed format).
        """
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE user_id = ? AND episode_type = ? "
            "AND outcome IS NULL AND timestamp <= ? ORDER BY timestamp ASC, id ASC",
            (self._uid(), episode_type, before),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_episode(self, episode_id: int) -> dict[str, Any] | None:
        """Return a single episode by id, or ``None`` if it does not exist."""
        row = self.conn.execute(
            "SELECT * FROM episodes WHERE id = ? AND user_id = ?",
            (episode_id, self._uid()),
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
            "SELECT * FROM episodes WHERE user_id = ? "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            (self._uid(), limit),
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
            "SELECT * FROM episodes WHERE user_id = ? ORDER BY timestamp ASC, id ASC",
            (self._uid(),),
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
            "SELECT * FROM episodes WHERE user_id = ? AND timestamp >= ? "
            "ORDER BY timestamp DESC, id DESC",
            (self._uid(), since),
        ).fetchall()
        return [dict(r) for r in rows]

    def episodes_by_type(
        self, episode_type: str, limit: int = 100, *, context_prefix: str | None = None
    ) -> list[dict[str, Any]]:
        """Return recent episodes of a single ``episode_type``, newest first.

        Args:
            episode_type: One of :data:`EPISODE_TYPES`.
            limit: Maximum number of rows to return.
            context_prefix: When given, keep only episodes whose ``context`` starts
                with this literal prefix — filtered **in SQL**, so the ``limit`` is
                applied to the already-matching rows. This matters when several
                sub-kinds share an ``episode_type`` (e.g. ``reminder`` covers both
                coaching nudges — ``"coach nudge: …"`` — and other reminders): a
                caller wanting only one sub-kind's recent rows must not have them
                crowded out of the window by interleaved rows of another. Treated as
                a literal (``%``/``_``/``\\`` escaped), not a pattern.

        Returns:
            A list of matching episode dicts.
        """
        if context_prefix is None:
            rows = self.conn.execute(
                "SELECT * FROM episodes WHERE user_id = ? AND episode_type = ? "
                "ORDER BY timestamp DESC, id DESC LIMIT ?",
                (self._uid(), episode_type, limit),
            ).fetchall()
        else:
            escaped = (
                context_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            rows = self.conn.execute(
                "SELECT * FROM episodes WHERE user_id = ? AND episode_type = ? "
                "AND context LIKE ? ESCAPE '\\' "
                "ORDER BY timestamp DESC, id DESC LIMIT ?",
                (self._uid(), episode_type, f"{escaped}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]
