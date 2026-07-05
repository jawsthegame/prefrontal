"""Open-loop todos and their tiny-first-step decompositions.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
"""
from __future__ import annotations

import json
from typing import Any

from prefrontal.memory._helpers import (
    _row_to_dict,
)
from prefrontal.memory.repos._base import Repo


class TodosRepo(Repo):
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
        category: str | None = None,
        time_window: str | None = None,
        domain: str | None = None,
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
            category: Optional topic (inferred upstream by ``augment_todo``);
                editable later via :meth:`set_todo_category`.
            time_window: Optional per-todo suggestion window ``"HH:MM-HH:MM"``
                (local), overriding the domain/category/source/default window used
                by :func:`prefrontal.scheduling.todo_allowed_at`. Editable later
                via :meth:`set_todo_window`.
            domain: Optional life sphere (``work``/``home``/…) — the work/life
                guardrail. Outranks ``category`` when resolving the time band, so
                a work-mailbox todo is held to work hours whatever its category.
                Mail ingestion stamps it from the account's configured domain.
            source: Where the todo came from — ``manual`` or ``impulse`` (a
                captured-and-deferred impulse). Lets surfaces distinguish the
                impulse inbox from deliberately-added loops.

        Returns:
            The new todo's id.
        """
        cur = self.conn.execute(
            "INSERT INTO todos (user_id, title, notes, estimate_minutes, priority, "
            "deadline, energy, category, time_window, domain, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self._uid(), title, notes, estimate_minutes, priority, deadline, energy,
             category, time_window, domain, source),
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

    def _update_todo_field(
        self, todo_id: int, column: str, value: Any, *, open_only: bool = True
    ) -> bool:
        """Set one column on a todo, touching ``updated_at``. Returns ``True`` if changed.

        The single-field todo setters below are all the same one-column update; this
        is the one place that SQL lives. ``open_only`` (the default) restricts the
        write to open todos — a closed todo's deadline/priority/estimate/title/window
        is moot; ``set_todo_category`` passes ``False`` because recategorizing a
        finished todo still corrects the historical rollup. ``column`` is always an
        internal literal (never caller/user input), so the f-string interpolation is
        injection-safe, exactly like the escalating-session helpers.
        """
        open_clause = " AND status = 'open'" if open_only else ""
        cur = self.conn.execute(
            f"UPDATE todos SET {column} = ?, updated_at = CURRENT_TIMESTAMP "
            f"WHERE id = ? AND user_id = ?{open_clause}",
            (value, todo_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def update_todo_deadline(self, todo_id: int, deadline: str | None) -> bool:
        """Set (or clear) an open todo's deadline. Returns ``True`` if it changed.

        Plans drift; a deadline set when the todo was created — or inferred from
        its title — often needs to move. Only open todos are editable (a closed
        todo's deadline is moot), so this no-ops on a done/dropped/absent todo.

        Args:
            todo_id: The todo to update.
            deadline: A UTC deadline (``YYYY-MM-DD HH:MM:SS``), or ``None`` to clear it.
        """
        return self._update_todo_field(todo_id, "deadline", deadline)

    def set_todo_priority(self, todo_id: int, priority: int) -> bool:
        """Set an open todo's priority (0 low … 3 urgent). Returns ``True`` if changed.

        Like :meth:`update_todo_deadline`, only open todos are editable, so this
        no-ops on a closed or absent todo. The natural-language assistant uses
        this to honor "bump the dentist call to urgent"; callers clamp the value.

        Args:
            todo_id: The todo to update.
            priority: 0 low / 1 normal / 2 high / 3 urgent.
        """
        return self._update_todo_field(todo_id, "priority", priority)

    def set_todo_estimate(self, todo_id: int, estimate_minutes: float | None) -> bool:
        """Set (or clear) an open todo's minute estimate. Returns ``True`` if changed.

        A better estimate changes whether — and where — the todo fits into free
        time, so the assistant surfaces this for "that'll only take 10 minutes".
        Only open todos are editable; ``None`` clears the estimate.

        Args:
            todo_id: The todo to update.
            estimate_minutes: Realistic minutes, or ``None`` to clear it.
        """
        return self._update_todo_field(todo_id, "estimate_minutes", estimate_minutes)

    def set_todo_title(self, todo_id: int, title: str) -> bool:
        """Rename an open todo. Returns ``True`` if it changed.

        Only open todos are editable (a closed todo's wording is history). Used by
        the assistant for "reword that todo to …". Callers should reject blank
        titles before calling.

        Args:
            todo_id: The todo to update.
            title: The new title.
        """
        return self._update_todo_field(todo_id, "title", title)

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

    def set_todo_category(self, todo_id: int, category: str | None) -> bool:
        """Set (or clear) a todo's category. Returns ``True`` if a row changed.

        Editable at any status — recategorizing a finished todo still corrects
        the historical rollup. The caller (``augment_todo`` / the endpoint) is
        responsible for clamping to the cap; this just writes the value.
        """
        return self._update_todo_field(todo_id, "category", category, open_only=False)

    def set_todo_window(self, todo_id: int, time_window: str | None) -> bool:
        """Set (or clear) a todo's per-todo suggestion window. Returns ``True`` if changed.

        The value is a local ``"HH:MM-HH:MM"`` range (e.g. ``"06:00-22:00"``) that
        overrides the category/source/default window when the scheduler decides
        whether a todo is suggestible right now; ``None`` clears the override so it
        falls back to its category's window. Only open todos are editable (a closed
        todo's window is moot), matching :meth:`update_todo_deadline`. The caller is
        responsible for validating the format; this just writes the value.
        """
        return self._update_todo_field(todo_id, "time_window", time_window)

    def set_todo_domain(self, todo_id: int, domain: str | None) -> bool:
        """Set (or clear) a todo's life-sphere domain. Returns ``True`` if changed.

        The domain (``work``/``home``/…) is the work/life guardrail: it outranks
        the category when the scheduler resolves the todo's time band, so a
        work-mailbox todo stays inside work hours whatever its category. Editable
        at any status so a misfiled item can be corrected.
        """
        return self._update_todo_field(todo_id, "domain", domain, open_only=False)

    def todo_categories(self) -> list[str]:
        """Distinct categories in use, most-common first (drives cap + hints).

        Ordered by frequency so :func:`prefrontal.todos.augment_todo` can offer
        the model the user's real vocabulary and, at the cap, fall back to a
        well-populated bucket. Excludes NULL/blank.
        """
        rows = self.conn.execute(
            "SELECT category FROM todos "
            "WHERE user_id = ? AND category IS NOT NULL AND TRIM(category) != '' "
            "GROUP BY category ORDER BY COUNT(*) DESC, category ASC",
            (self._uid(),),
        ).fetchall()
        return [r["category"] for r in rows]

    def all_todos(self) -> list[dict[str, Any]]:
        """Return every todo for this user (any status), newest first.

        Used for the category rollup (:func:`prefrontal.todos.category_stats`),
        which needs closed todos too for completion rates. Personal-scale, so an
        unpaginated read is fine.
        """
        rows = self.conn.execute(
            "SELECT * FROM todos WHERE user_id = ? ORDER BY id DESC",
            (self._uid(),),
        ).fetchall()
        return [dict(r) for r in rows]

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

    def delete_decomposition(self, todo_id: int) -> bool:
        """Remove a todo's decomposition. Returns ``True`` if a row was deleted.

        Used when the user dismisses a breakdown; the dismissal is captured
        separately via :meth:`record_decomposition_dismissal` before this drops it.
        No-ops (returns ``False``) if the todo isn't this user's or had none.
        """
        if not self._owns_todo(todo_id):
            return False
        cur = self.conn.execute(
            "DELETE FROM todo_decompositions WHERE todo_id = ?", (todo_id,)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def record_decomposition_dismissal(
        self,
        *,
        todo_id: int | None,
        title: str | None,
        reason: str,
        source: str | None = None,
        first_step: str | None = None,
        steps: list[str] | None = None,
        category: str | None = None,
        estimate_minutes: float | None = None,
    ) -> int:
        """Record that the user dismissed a breakdown, and return the new row id.

        ``reason`` is ``not_useful`` (the steps didn't help — fed back as negative
        examples to the decomposer) or ``not_needed`` (the task didn't need
        breaking down — repeated, it suppresses auto-decompose). The rest is a
        snapshot of what was dismissed, for the learning readers.
        """
        cur = self.conn.execute(
            "INSERT INTO decomposition_feedback (user_id, todo_id, title, reason, "
            "source, first_step, steps, category, estimate_minutes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._uid(),
                todo_id,
                title,
                reason,
                source,
                first_step,
                json.dumps(steps) if steps is not None else None,
                category,
                estimate_minutes,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def decomposition_feedback_list(
        self, *, reason: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Recent decomposition dismissals (newest first), optionally by reason.

        ``steps`` is decoded back to a list. Scoped to this user.
        """
        sql = (
            "SELECT id, todo_id, title, reason, source, first_step, steps, category, "
            "estimate_minutes, created_at FROM decomposition_feedback WHERE user_id = ?"
        )
        params: list[Any] = [self._uid()]
        if reason is not None:
            sql += " AND reason = ?"
            params.append(reason)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            try:
                d["steps"] = json.loads(d["steps"]) if d["steps"] else []
            except (ValueError, TypeError):
                d["steps"] = []
            out.append(d)
        return out

    def decomposition_dismissed_count(self, *, reason: str | None = None) -> int:
        """How many decomposition dismissals this user has logged (optionally by reason)."""
        sql = "SELECT COUNT(*) AS n FROM decomposition_feedback WHERE user_id = ?"
        params: list[Any] = [self._uid()]
        if reason is not None:
            sql += " AND reason = ?"
            params.append(reason)
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return int(row["n"])

    def decomposition_feedback_todo_ids(self) -> set[int]:
        """Todo ids that already have a recorded decomposition decision.

        Any todo the user dismissed a breakdown for, or the model declined to
        break down — so the avoided-task sweep skips them and doesn't re-ask.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT todo_id FROM decomposition_feedback "
            "WHERE user_id = ? AND todo_id IS NOT NULL",
            (self._uid(),),
        ).fetchall()
        return {int(r["todo_id"]) for r in rows}
