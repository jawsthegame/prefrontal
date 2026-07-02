"""Derived behavioral patterns (upsert + read).

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
"""
from __future__ import annotations

from typing import Any


class PatternsRepo:
    """Derived behavioral patterns (upsert + read)."""

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
                user_id, pattern_type, context_key, observed_value, predicted_value,
                variance, sample_size, confidence, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id, pattern_type, context_key) DO UPDATE SET
                observed_value  = excluded.observed_value,
                predicted_value = excluded.predicted_value,
                variance        = excluded.variance,
                sample_size     = excluded.sample_size,
                confidence      = excluded.confidence,
                last_updated    = CURRENT_TIMESTAMP
            """,
            (
                self._uid(),
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
            "SELECT id FROM patterns "
            "WHERE user_id = ? AND pattern_type = ? AND context_key = ?",
            (self._uid(), pattern_type, context_key),
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
                "SELECT * FROM patterns WHERE user_id = ? "
                "ORDER BY confidence DESC, id ASC",
                (self._uid(),),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM patterns WHERE user_id = ? AND pattern_type = ? "
                "ORDER BY confidence DESC, id ASC",
                (self._uid(), pattern_type),
            ).fetchall()
        return [dict(r) for r in rows]
