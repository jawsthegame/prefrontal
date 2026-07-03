"""The log of what the system last told the user.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory.repos._base import Repo

# How long a nudge stays "fresh" on a surface when the caller gives no explicit
# expiry. Outing escalation nudges have no natural end time (unlike a departure,
# which expires at the meeting's start), so without a default they linger for
# hours after you're back — a stale "still on track?" on the widget. Each new
# escalation fire records a fresh nudge, so a genuinely long outing keeps
# refreshing this window; it only ages out once the fires stop.
DEFAULT_NUDGE_TTL_HOURS = 2


class NudgesRepo(Repo):
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
                is stale and should no longer surface. When ``None`` it defaults
                to ``DEFAULT_NUDGE_TTL_HOURS`` from now, so a nudge without a
                natural end time (e.g. an outing) doesn't linger for hours. A
                departure nudge passes the commitment's ``start_at`` so "leave
                now" stops showing once the meeting has started.

        Returns:
            The new nudge row's id.
        """
        cur = self.conn.execute(
            "INSERT INTO nudges (user_id, kind, level, message, expires_at) "
            "VALUES (?, ?, ?, ?, COALESCE(?, datetime('now', ?)))",
            (self._uid(), kind, level, message, expires_at, f"+{DEFAULT_NUDGE_TTL_HOURS} hours"),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_nudges(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return this user's most recent *unexpired* nudges, newest first.

        A nudge with an ``expires_at`` at or before now is omitted: a departure
        "leave now" nudge expires at its meeting's start, and every other nudge
        gets a ``DEFAULT_NUDGE_TTL_HOURS`` default at record time, so a stale
        reminder never lingers on a surface (the widget). A row with a NULL
        ``expires_at`` (only legacy rows predating the default) is always
        eligible.

        Args:
            limit: Maximum number of nudges to return.

        Returns:
            A list of nudge dicts (``kind``, ``level``, ``message``,
            ``created_at``, ``expires_at``), newest first.
        """
        return self._query_all(
            "SELECT id, kind, level, message, created_at, expires_at FROM nudges "
            "WHERE user_id = ? AND (expires_at IS NULL OR expires_at > datetime('now')) "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (self._uid(), limit),
        )

    def expire_nudges(self, kind: str) -> int:
        """Immediately expire this user's still-live nudges of a given kind.

        Called when the thing a nudge was about is resolved — closing an outing
        expires its "still on track?" so it clears from every surface at once,
        the moment you're back, instead of aging out on the default TTL hours
        later. Only touches nudges not already expired, so it never revives or
        rewrites a stale row.

        Args:
            kind: The nudge kind to expire (e.g. ``"outing"``).

        Returns:
            The number of nudges expired.
        """
        cur = self.conn.execute(
            "UPDATE nudges SET expires_at = datetime('now') "
            "WHERE user_id = ? AND kind = ? "
            "AND (expires_at IS NULL OR expires_at > datetime('now'))",
            (self._uid(), kind),
        )
        self.conn.commit()
        return int(cur.rowcount)

    # -- Task decompositions -------------------------------------------------
    #
    # ``todo_decompositions`` has no ``user_id`` of its own — it hangs off
    # ``todos`` (ON DELETE CASCADE). It is scoped *through* its parent todo: each
    # method first checks the todo belongs to this user (:meth:`_owns_todo`), so
    # one user can never read or edit another user's decomposition by id.
