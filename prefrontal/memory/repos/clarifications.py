"""Ambiguity clarifications — pending "what does this mean?" questions for items.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone. A
clarification is a proposed question about one ambiguous todo/commitment (see
:mod:`prefrontal.clarify`). Like a sensor proposal it stays **pending** until the
human answers: resolving it records the chosen reading (and any recognized task
type, which unlocks a guided playbook), while dismissing it marks the item "not
ambiguous" so the detector never re-asks. The target is referenced loosely by
``(target_type, target_id)`` — a calendar re-sync can replace a commitment row,
so this deliberately does not hard-foreign-key the target.
"""
from __future__ import annotations

import json
from typing import Any

from prefrontal.memory.repos._base import Repo

#: The kinds of item a clarification can hang off.
TARGET_TODO = "todo"
TARGET_COMMITMENT = "commitment"


class ClarificationsRepo(Repo):
    """Pending/resolved/dismissed ambiguity clarifications for todos & commitments."""

    def add_clarification(
        self,
        *,
        target_type: str,
        target_id: int,
        title: str,
        question: str,
        options: list[dict[str, Any]],
        source: str = "heuristic",
    ) -> int:
        """Record a pending clarification and return its id.

        Args:
            target_type: ``"todo"`` or ``"commitment"``.
            target_id: The referenced item's row id.
            title: Snapshot of the item's title at detection time.
            question: The clarifying question to show inline.
            options: Candidate readings, each ``{"label", "task_type"?}`` — stored
                as JSON.
            source: ``"llm"`` or ``"heuristic"`` — how the question was phrased.

        Returns:
            The new clarification row's id.
        """
        cur = self.conn.execute(
            "INSERT INTO clarifications "
            "(user_id, target_type, target_id, title, question, options, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._uid(),
                target_type,
                target_id,
                title,
                question,
                json.dumps(options),
                source,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_clarifications(
        self, status: str = "pending", limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return this user's clarifications (newest first), ``options`` parsed.

        Args:
            status: Filter to this status (``pending``/``resolved``/``dismissed``),
                or ``""`` for any status.
            limit: Maximum rows to return.
        """
        rows = self.conn.execute(
            "SELECT id, target_type, target_id, title, question, options, source, "
            "status, answer, task_type, created_at, resolved_at "
            "FROM clarifications WHERE user_id = ? AND (? = '' OR status = ?) "
            "ORDER BY id DESC LIMIT ?",
            (self._uid(), status, status, limit),
        ).fetchall()
        return [self._clarify_row(r) for r in rows]

    def get_clarification(self, clarification_id: int) -> dict[str, Any] | None:
        """Return one of this user's clarifications by id, or ``None``."""
        row = self.conn.execute(
            "SELECT id, target_type, target_id, title, question, options, source, "
            "status, answer, task_type, created_at, resolved_at "
            "FROM clarifications WHERE user_id = ? AND id = ?",
            (self._uid(), clarification_id),
        ).fetchone()
        return self._clarify_row(row) if row is not None else None

    def clarified_target_ids(self, target_type: str) -> set[int]:
        """Target ids of ``target_type`` that already have *any* clarification.

        Used by the detection sweep to skip items already asked about — whether
        the earlier question is still pending, was answered, or was dismissed as
        not-actually-ambiguous — so it never re-asks the same item.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT target_id FROM clarifications "
            "WHERE user_id = ? AND target_type = ?",
            (self._uid(), target_type),
        ).fetchall()
        return {int(r["target_id"]) for r in rows}

    def resolve_clarification(
        self, clarification_id: int, *, answer: str, task_type: str | None
    ) -> bool:
        """Resolve a *pending* clarification with the chosen reading.

        Only a pending row moves (so a double-resolve is a no-op), which keeps any
        follow-on write idempotent. ``task_type`` is the recognized playbook key
        when the chosen reading maps to one, else ``None``. Returns ``True`` if a
        row was updated.
        """
        cur = self.conn.execute(
            "UPDATE clarifications SET status = 'resolved', answer = ?, task_type = ?, "
            "resolved_at = datetime('now') "
            "WHERE user_id = ? AND id = ? AND status = 'pending'",
            (answer, task_type, self._uid(), clarification_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def dismiss_clarification(self, clarification_id: int) -> bool:
        """Dismiss a *pending* clarification ("this isn't ambiguous"). Idempotent.

        Marks it ``dismissed`` and resolved-now; the item stays in
        :meth:`clarified_target_ids` so the sweep won't ask again. Returns ``True``
        if a row was updated.
        """
        cur = self.conn.execute(
            "UPDATE clarifications SET status = 'dismissed', resolved_at = datetime('now') "
            "WHERE user_id = ? AND id = ? AND status = 'pending'",
            (self._uid(), clarification_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _clarify_row(row: Any) -> dict[str, Any]:
        d = dict(row)
        try:
            d["options"] = json.loads(d["options"]) if d.get("options") else []
        except (ValueError, TypeError):
            d["options"] = []
        return d
