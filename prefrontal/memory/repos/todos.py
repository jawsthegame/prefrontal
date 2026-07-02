"""Open-loop todos and their tiny-first-step decompositions.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
"""
from __future__ import annotations

import json
from typing import Any

from prefrontal.memory._helpers import (
    _row_to_dict,
)


class TodosRepo:
    """Open-loop todos and their tiny-first-step decompositions."""

    def add_todo(
        self,
        title: str,
        *,
        notes: str | None = None,
        estimate_minutes: float | None = None,
        priority: int = 1,
        deadline: str | None = None,
        energy: str | None = None,
        source: str = "manual",
    ) -> int:
        """Insert an open todo and return its id.

        Args:
            title: What needs doing.
            notes: Optional detail.
            estimate_minutes: How long it'll take (enables fitting into windows).
            priority: 0 low / 1 normal / 2 high / 3 urgent.
            deadline: Optional UTC deadline (``YYYY-MM-DD HH:MM:SS``).
            energy: Optional ``low``/``medium``/``high`` hint.
            source: Where the todo came from — ``manual`` or ``impulse`` (a
                captured-and-deferred impulse). Lets surfaces distinguish the
                impulse inbox from deliberately-added loops.

        Returns:
            The new todo's id.
        """
        cur = self.conn.execute(
            "INSERT INTO todos (user_id, title, notes, estimate_minutes, priority, "
            "deadline, energy, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self._uid(), title, notes, estimate_minutes, priority, deadline, energy, source),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_todo(self, todo_id: int) -> dict[str, Any] | None:
        """Return a single todo by id, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM todos WHERE id = ? AND user_id = ?",
            (todo_id, self._uid()),
        ).fetchone()
        return _row_to_dict(row)

    def open_todos(self) -> list[dict[str, Any]]:
        """Return open todos, highest priority then soonest deadline first.

        Returns:
            A list of todo dicts with ``status = 'open'``.
        """
        rows = self.conn.execute(
            "SELECT * FROM todos WHERE user_id = ? AND status = 'open' "
            "ORDER BY priority DESC, (deadline IS NULL), deadline ASC, id ASC",
            (self._uid(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_todo_deadline(self, todo_id: int, deadline: str | None) -> bool:
        """Set (or clear) an open todo's deadline. Returns ``True`` if it changed.

        Plans drift; a deadline set when the todo was created — or inferred from
        its title — often needs to move. Only open todos are editable (a closed
        todo's deadline is moot), so this no-ops on a done/dropped/absent todo.

        Args:
            todo_id: The todo to update.
            deadline: A UTC deadline (``YYYY-MM-DD HH:MM:SS``), or ``None`` to clear it.
        """
        cur = self.conn.execute(
            "UPDATE todos SET deadline = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ? AND status = 'open'",
            (deadline, todo_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def close_todo(self, todo_id: int, status: str = "done") -> bool:
        """Mark a todo ``done`` or ``dropped``. Returns ``True`` if it changed.

        Args:
            todo_id: The todo to close.
            status: ``done`` or ``dropped``.
        """
        completed = "CURRENT_TIMESTAMP" if status == "done" else "NULL"
        cur = self.conn.execute(
            f"UPDATE todos SET status = ?, completed_at = {completed}, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ? AND status = 'open'",
            (status, todo_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def _owns_todo(self, todo_id: int) -> bool:
        """Return whether ``todo_id`` belongs to this scoped user."""
        row = self.conn.execute(
            "SELECT 1 FROM todos WHERE id = ? AND user_id = ?",
            (todo_id, self._uid()),
        ).fetchone()
        return row is not None

    def set_decomposition(
        self,
        todo_id: int,
        *,
        first_step: str,
        first_step_minutes: float | None,
        steps: list[str],
        source: str,
    ) -> None:
        """Store (or replace) a todo's decomposition. ``steps`` is JSON-encoded.

        Replacing a decomposition leaves ``done_steps`` unset (NULL), which resets
        any per-step progress — the steps themselves changed, so old check-offs no
        longer apply. No-ops if the todo is not this user's.
        """
        if not self._owns_todo(todo_id):
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO todo_decompositions "
            "(todo_id, first_step, first_step_minutes, steps, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (todo_id, first_step, first_step_minutes, json.dumps(steps), source),
        )
        self.conn.commit()

    def get_decomposition(self, todo_id: int) -> dict[str, Any] | None:
        """Return a todo's decomposition, or ``None``.

        ``steps`` is decoded to a list of strings and ``done_steps`` to a sorted
        list of completed step indices (0 = ``first_step``, 1..N = ``steps``).
        Returns ``None`` if the todo is not this user's.
        """
        if not self._owns_todo(todo_id):
            return None
        row = self.conn.execute(
            "SELECT first_step, first_step_minutes, steps, source, done_steps "
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
        d["done_steps"] = self._decode_done_steps(d.get("done_steps"))
        return d

    @staticmethod
    def _decode_done_steps(raw: Any) -> list[int]:
        """Decode the stored ``done_steps`` JSON into a sorted list of ints."""
        try:
            value = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            return []
        if not isinstance(value, list):
            return []
        return sorted({int(i) for i in value if isinstance(i, int) and not isinstance(i, bool)})

    def set_step_done(self, todo_id: int, step_index: int, done: bool = True) -> bool:
        """Mark one decomposed step done (or undone). Returns ``True`` if valid.

        Steps are indexed with ``0`` = ``first_step`` and ``1..N`` = the remaining
        ``steps``, so a decomposition with M remaining steps has indices ``0..M``.
        Ticking a step off is its own small win — visible progress is what keeps a
        broken-down task moving. No-ops (returns ``False``) when the todo has no
        decomposition or ``step_index`` is out of range.

        Args:
            todo_id: The todo whose decomposition to update.
            step_index: Which step (``0`` = first step).
            done: ``True`` to mark done, ``False`` to clear it.
        """
        if not self._owns_todo(todo_id):
            return False
        row = self.conn.execute(
            "SELECT steps, done_steps FROM todo_decompositions WHERE todo_id = ?",
            (todo_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            steps = json.loads(row["steps"]) if row["steps"] else []
        except (ValueError, TypeError):
            steps = []
        total = 1 + (len(steps) if isinstance(steps, list) else 0)
        if step_index < 0 or step_index >= total:
            return False
        done_set = set(self._decode_done_steps(row["done_steps"]))
        if done:
            done_set.add(step_index)
        else:
            done_set.discard(step_index)
        self.conn.execute(
            "UPDATE todo_decompositions SET done_steps = ? WHERE todo_id = ?",
            (json.dumps(sorted(done_set)), todo_id),
        )
        self.conn.commit()
        return True
