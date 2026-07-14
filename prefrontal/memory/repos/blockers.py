"""Blockers — records that someone *else* is waiting on *you*.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone. A
blocker is the mirror image of a todo: a todo is your own open loop, whereas a
blocker names another person who is blocked until you do a specific thing (the
ball is in your court). It exists to feed prioritization — panic mode
(:mod:`prefrontal.panic`) and the morning briefing (:mod:`prefrontal.briefing`)
surface who's waiting on you, so an unblock can outrank a shiny new task.

Deliberately lightweight CRUD: capture is one line (person + what), and a blocker
is *resolved* rather than deleted (``status='resolved'``) so the history stays.
Aging is derived from ``blocking_since`` by the pure helpers in
:mod:`prefrontal.blockers`; the repo just returns raw rows in a sensible order
(most pressing first).
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory._helpers import _row_to_dict
from prefrontal.memory.repos._base import Repo

#: Order for a blockers listing — open before resolved, then by priority
#: (urgent first), then longest-waiting first (oldest ``blocking_since``). Shared
#: by :meth:`BlockersRepo.list_blockers` and :meth:`BlockersRepo.open_blockers`
#: so both agree on "most pressing first".
_ORDER = (
    "ORDER BY (status = 'open') DESC, priority DESC, "
    "blocking_since ASC, id ASC"
)


class BlockersRepo(Repo):
    """CRUD for blockers — who's waiting on you, and for what."""

    def add_blocker(
        self,
        person: str,
        what: str,
        *,
        notes: str | None = None,
        priority: int = 1,
        deadline: str | None = None,
        blocking_since: str | None = None,
        todo_id: int | None = None,
    ) -> int:
        """Log that ``person`` is blocked on you for ``what``; return its id.

        Args:
            person: Who is waiting on you (a name, e.g. ``"Sam"``).
            what: The thing they need from you (e.g. ``"the budget numbers"``).
            notes: Optional free-text detail.
            priority: ``0`` low … ``3`` urgent (matches todos).
            deadline: Optional "needs it by" timestamp (naive UTC), or ``None``.
            blocking_since: When they started waiting (naive UTC). Defaults to now
                (``CURRENT_TIMESTAMP``) — pass an earlier value when logging a wait
                that started before you got around to capturing it.
            todo_id: Optional link to the open loop that will clear this blocker.

        Returns:
            The new blocker's id.
        """
        if blocking_since is None:
            cur = self.conn.execute(
                "INSERT INTO blockers "
                "(user_id, person, what, notes, priority, deadline, todo_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self._uid(), person, what, notes, priority, deadline, todo_id),
            )
        else:
            cur = self.conn.execute(
                "INSERT INTO blockers "
                "(user_id, person, what, notes, priority, deadline, blocking_since, todo_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self._uid(), person, what, notes, priority, deadline, blocking_since, todo_id),
            )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_blocker(self, blocker_id: int) -> dict[str, Any] | None:
        """Return a single blocker by id, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM blockers WHERE id = ? AND user_id = ?",
            (blocker_id, self._uid()),
        ).fetchone()
        return _row_to_dict(row)

    def list_blockers(self, *, include_resolved: bool = False) -> list[dict[str, Any]]:
        """Return the user's blockers, most pressing first.

        Ordered open-before-resolved, then by priority (urgent first), then
        longest-waiting first. Only ``open`` unless ``include_resolved`` is set.
        """
        status_clause = "" if include_resolved else " AND status = 'open'"
        return self._query_all(
            f"SELECT * FROM blockers WHERE user_id = ?{status_clause} {_ORDER}",
            (self._uid(),),
        )

    def open_blockers(self) -> list[dict[str, Any]]:
        """The open blockers, most pressing first — what prioritization reads.

        The subset panic mode and the morning briefing surface (someone is still
        waiting), ordered like :meth:`list_blockers`.
        """
        return self._query_all(
            f"SELECT * FROM blockers WHERE user_id = ? AND status = 'open' {_ORDER}",
            (self._uid(),),
        )

    def count_open_blockers(self) -> int:
        """How many people are currently waiting on you (open blockers)."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM blockers WHERE user_id = ? AND status = 'open'",
            (self._uid(),),
        ).fetchone()
        return int(row[0])

    def update_blocker(self, blocker_id: int, **fields: Any) -> dict[str, Any] | None:
        """Set any of ``person``/``what``/``notes``/``priority``/``deadline``/``todo_id``.

        Only recognized columns are written (the keys are internal literals, never
        user input, so the interpolation is injection-safe — matching the todo /
        project field-setters). Status transitions go through :meth:`resolve_blocker`
        / :meth:`reopen_blocker` instead, so ``resolved_at`` stays consistent.
        Returns the updated row, or ``None`` if no such blocker.
        """
        allowed = {"person", "what", "notes", "priority", "deadline", "todo_id"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_blocker(blocker_id)
        assignments = ", ".join(f"{col} = ?" for col in updates)
        params = (*updates.values(), blocker_id, self._uid())
        cur = self.conn.execute(
            f"UPDATE blockers SET {assignments}, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ?",
            params,
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_blocker(blocker_id)

    def resolve_blocker(self, blocker_id: int) -> dict[str, Any] | None:
        """Mark a blocker resolved (you delivered); return the row, or ``None``.

        A no-op on an already-resolved blocker returns the row unchanged. Records
        ``resolved_at`` so the history keeps *when* the wait ended.
        """
        cur = self.conn.execute(
            "UPDATE blockers SET status = 'resolved', "
            "resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ? AND status = 'open'",
            (blocker_id, self._uid()),
        )
        self.conn.commit()
        if cur.rowcount == 0 and self.get_blocker(blocker_id) is None:
            return None
        return self.get_blocker(blocker_id)

    def reopen_blocker(self, blocker_id: int) -> dict[str, Any] | None:
        """Reopen a resolved blocker (they're waiting again); clears ``resolved_at``."""
        cur = self.conn.execute(
            "UPDATE blockers SET status = 'open', resolved_at = NULL, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ? AND status = 'resolved'",
            (blocker_id, self._uid()),
        )
        self.conn.commit()
        if cur.rowcount == 0 and self.get_blocker(blocker_id) is None:
            return None
        return self.get_blocker(blocker_id)
