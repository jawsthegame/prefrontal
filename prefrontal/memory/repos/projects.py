"""Projects — a user-created grouping across todos/commitments/focus sessions.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone. A
project is nested under exactly one ``domain`` (the finer grain within a life
sphere). Assigning an entity to a project writes the project's domain through to
that entity's own ``domain`` column (see the ``set_*_project`` methods on the
todo/schedule/session repos), so the scheduler's work/life guardrails keep
working without any project awareness.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory._helpers import _row_to_dict
from prefrontal.memory.repos._base import Repo


class ProjectsRepo(Repo):
    """CRUD for user projects and the accessor the triage suggester reads."""

    def add_project(
        self,
        name: str,
        domain: str,
        *,
        description: str | None = None,
        notes: str | None = None,
        color: str | None = None,
    ) -> int:
        """Insert an active project and return its id.

        Args:
            name: The project's display name (unique among the user's active
                projects — the caller checks for a clash first).
            domain: The life sphere this project sits under (``work``/``home``/…);
                required. Callers snap it onto the canonical vocabulary via
                :func:`prefrontal.focus_balance.normalize_focus_domain` first.
            description: Short blurb matched (with ``name``) to auto-suggest this
                project for a triaged item.
            notes: Longer free-text detail; not used for matching.
            color: Optional UI accent.

        Returns:
            The new project's id.
        """
        cur = self.conn.execute(
            "INSERT INTO projects (user_id, name, description, domain, notes, color) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self._uid(), name, description, domain, notes, color),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_project(self, project_id: int) -> dict[str, Any] | None:
        """Return a single project by id, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (project_id, self._uid()),
        ).fetchone()
        return _row_to_dict(row)

    def list_projects(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        """Return the user's projects, active first then newest.

        Args:
            include_archived: When ``False`` (default), only ``active`` projects.
        """
        status_clause = "" if include_archived else " AND status = 'active'"
        return self._query_all(
            f"SELECT * FROM projects WHERE user_id = ?{status_clause} "
            "ORDER BY (status = 'active') DESC, id DESC",
            (self._uid(),),
        )

    def active_projects(self) -> list[dict[str, Any]]:
        """Active projects as ``id``/``name``/``description``/``domain`` rows.

        This is the vocabulary the triage suggester
        (:func:`prefrontal.projects.suggest_project`) offers the model — the
        projects analog of :meth:`TodosRepo.todo_categories`.
        """
        return self._query_all(
            "SELECT id, name, description, domain FROM projects "
            "WHERE user_id = ? AND status = 'active' ORDER BY id ASC",
            (self._uid(),),
        )

    def project_name_in_use(self, name: str, *, exclude_id: int | None = None) -> bool:
        """Whether an *active* project already has ``name`` (case-insensitive).

        Mirrors the unique-name index (active rows only). ``exclude_id`` skips the
        project being renamed so a no-op rename isn't rejected as a clash.
        """
        row = self.conn.execute(
            "SELECT 1 FROM projects WHERE user_id = ? AND status = 'active' "
            "AND LOWER(name) = LOWER(?) AND id IS NOT ? LIMIT 1",
            (self._uid(), name, exclude_id),
        ).fetchone()
        return row is not None

    def update_project(self, project_id: int, **fields: Any) -> dict[str, Any] | None:
        """Set any of ``name``/``description``/``domain``/``notes``/``color``/``status``.

        Only recognized columns are written (the keys are internal literals, never
        user input, so the interpolation is injection-safe — matching the todo
        field-setter). When ``domain`` changes, the new domain is cascaded onto the
        project's assigned todos and commitments (focus sessions carry no domain),
        so the work/life guardrail stays consistent with the container. Returns the
        updated row, or ``None`` if no such project.
        """
        allowed = {"name", "description", "domain", "notes", "color", "status"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_project(project_id)
        assignments = ", ".join(f"{col} = ?" for col in updates)
        params = (*updates.values(), project_id, self._uid())
        cur = self.conn.execute(
            f"UPDATE projects SET {assignments}, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ?",
            params,
        )
        if cur.rowcount == 0:
            self.conn.commit()
            return None
        if "domain" in updates:
            self._cascade_project_domain(project_id, updates["domain"])
        self.conn.commit()
        return self.get_project(project_id)

    def archive_project(self, project_id: int) -> bool:
        """Soft-delete a project (status='archived'). Returns ``True`` if changed.

        Assigned todos/commitments/sessions keep their ``project_id`` (archive is
        not delete — the history stays labeled); the unique-name index only covers
        active rows, so the name frees up for reuse.
        """
        cur = self.conn.execute(
            "UPDATE projects SET status = 'archived', updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ? AND status = 'active'",
            (project_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def _cascade_project_domain(self, project_id: int, domain: str | None) -> None:
        """Write ``domain`` onto every todo/commitment assigned to this project.

        Called from :meth:`update_project` when a project changes life sphere, so
        the domain the scheduler reads on each assigned entity stays in step with
        its container. Scoped to the user; focus sessions have no domain column.
        """
        for table in ("todos", "commitments"):
            self.conn.execute(
                f"UPDATE {table} SET domain = ? WHERE project_id = ? AND user_id = ?",
                (domain, project_id, self._uid()),
            )
