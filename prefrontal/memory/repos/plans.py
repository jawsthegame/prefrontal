"""Implementation intentions ("if-then plans").

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone. Owns
the reads/writes for the ``implementation_intentions`` table — the if-then plans
the :mod:`prefrontal.modules.implementation_intention` module surfaces at their
cue. Kept deliberately thin (a plan is a cue + a pre-decided action + status); the
cue-matching *logic* lives in the module, not here.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory.repos._base import Repo


class PlansRepo(Repo):
    """Store surface for implementation intentions (if-then plans)."""

    def add_implementation_intention(
        self,
        *,
        cue_text: str,
        action_text: str,
        cue_place: str | None = None,
        cue_window: str | None = None,
        cue_event: str | None = None,
        todo_id: int | None = None,
    ) -> int:
        """Create an if-then plan and return its id.

        A plan is a *cue* — a curated place, a local time-of-day band, a detected
        transition (``arrive_home``/``leave_home``), or any combination — paired with
        a tiny *pre-decided action*. At least one cue constraint should be set for the
        plan to ever fire; a plan with none is stored (so a half-captured plan isn't
        lost) but simply never matches until a cue is added.

        Args:
            cue_text: The trigger in the user's own words ("when I get home"), shown
                back verbatim.
            action_text: The pre-decided first step, stored as stated — the technique
                depends on it being tiny and committed in advance, so we don't rewrite it.
            cue_place: A curated ``places.name`` matched by proximity, or ``None``.
            cue_window: A local ``"HH:MM-HH:MM"`` band, or ``None``.
            cue_event: A transition event key (``arrive_home``/``leave_home``), or ``None``.
            todo_id: An optional todo this plan helps start.

        Returns:
            The new plan's ``id``.
        """
        cur = self.conn.execute(
            "INSERT INTO implementation_intentions "
            "(user_id, cue_text, cue_place, cue_window, cue_event, action_text, todo_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self._uid(), cue_text, cue_place, cue_window, cue_event, action_text, todo_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def active_implementation_intentions(self) -> list[dict[str, Any]]:
        """Every active if-then plan for the user, newest first.

        The read the coaching tick sweeps each cycle to decide which plans' cues are
        currently satisfied. Archived plans are excluded — they're the ones the user
        retired, and a retired plan must never re-fire.
        """
        return self._query_all(
            "SELECT * FROM implementation_intentions "
            "WHERE user_id = ? AND status = 'active' "
            "ORDER BY id DESC",
            (self._uid(),),
        )

    def implementation_intentions(
        self, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        """All if-then plans for the user (optionally filtered by ``status``), newest first."""
        if status is None:
            return self._query_all(
                "SELECT * FROM implementation_intentions WHERE user_id = ? "
                "ORDER BY id DESC",
                (self._uid(),),
            )
        return self._query_all(
            "SELECT * FROM implementation_intentions WHERE user_id = ? AND status = ? "
            "ORDER BY id DESC",
            (self._uid(), status),
        )

    def get_implementation_intention(self, plan_id: int) -> dict[str, Any] | None:
        """A single if-then plan by id, or ``None`` if it isn't this user's."""
        return self._query_one(
            "SELECT * FROM implementation_intentions WHERE id = ? AND user_id = ?",
            (plan_id, self._uid()),
        )

    def set_implementation_intention_fired(
        self, plan_id: int, fired_at: str
    ) -> None:
        """Stamp when a plan's cue last surfaced it (best-effort, scoped to the user).

        Advisory only — the engine's debounce, not this stamp, prevents re-firing.
        It powers the profile's "last nudged" read and a future "which plans actually
        get triggered" learning signal. Silently no-ops on an unknown/foreign id.
        """
        self.conn.execute(
            "UPDATE implementation_intentions SET last_fired_at = ? "
            "WHERE id = ? AND user_id = ?",
            (fired_at, plan_id, self._uid()),
        )
        self.conn.commit()

    def archive_implementation_intention(self, plan_id: int) -> bool:
        """Retire an if-then plan so it stops firing; ``True`` if a row changed.

        Forgiving by design: retiring a plan is a neutral "done with this," not a
        failure — there's no streak to break. Scoped to the user; ``False`` when
        there's no such active plan.
        """
        cur = self.conn.execute(
            "UPDATE implementation_intentions SET status = 'archived' "
            "WHERE id = ? AND user_id = ? AND status = 'active'",
            (plan_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0
