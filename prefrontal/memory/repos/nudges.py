"""The log of what the system last told the user.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
"""
from __future__ import annotations

from typing import Any


class NudgesRepo:
    """The log of what the system last told the user."""

    def record_nudge(
        self,
        *,
        kind: str,
        message: str,
        level: str | None = None,
        expires_at: str | None = None,
    ) -> int:
        """Record a fired nudge and return its id.

        Called by the escalation checks when they decide to nudge (``fire``),
        so every surface can show what Prefrontal last said. Purely a log — it
        has no effect on escalation state (which lives on the outing/coaching
        row); a failure to record must never block the nudge itself.

        Args:
            kind: ``"outing"`` or ``"departure"``.
            message: The delivered nudge text.
            level: The escalation level at fire time (kind-specific), if any.
            expires_at: UTC text (``YYYY-MM-DD HH:MM:SS``) after which the nudge
                is stale and should no longer surface, or ``None`` for no expiry.
                A departure nudge passes the commitment's ``start_at`` so "leave
                now" stops showing once the meeting has started.

        Returns:
            The new nudge row's id.
        """
        cur = self.conn.execute(
            "INSERT INTO nudges (user_id, kind, level, message, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (self._uid(), kind, level, message, expires_at),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_nudges(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return this user's most recent *unexpired* nudges, newest first.

        A nudge with an ``expires_at`` at or before now is omitted: a departure
        "leave now" nudge expires at its meeting's start, so an ended meeting's
        reminder never lingers on a surface (the widget). Nudges with a NULL
        ``expires_at`` (e.g. outings) are always eligible.

        Args:
            limit: Maximum number of nudges to return.

        Returns:
            A list of nudge dicts (``kind``, ``level``, ``message``,
            ``created_at``, ``expires_at``), newest first.
        """
        rows = self.conn.execute(
            "SELECT id, kind, level, message, created_at, expires_at FROM nudges "
            "WHERE user_id = ? AND (expires_at IS NULL OR expires_at > datetime('now')) "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- Task decompositions -------------------------------------------------
    #
    # ``todo_decompositions`` has no ``user_id`` of its own — it hangs off
    # ``todos`` (ON DELETE CASCADE). It is scoped *through* its parent todo: each
    # method first checks the todo belongs to this user (:meth:`_owns_todo`), so
    # one user can never read or edit another user's decomposition by id.
