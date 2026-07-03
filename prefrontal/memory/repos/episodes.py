"""Episode logging and queries.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory._helpers import (
    _row_to_dict,
)


class EpisodesRepo:
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
            "SELECT * FROM episodes WHERE user_id = ? AND episode_type = ? "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            (self._uid(), episode_type, limit),
        ).fetchall()
        return [dict(r) for r in rows]
