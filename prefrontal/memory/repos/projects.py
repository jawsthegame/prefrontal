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

        The project is appended at the bottom of the user's forced priority order
        (``rank`` = current max active rank + 1); reorder it with
        :meth:`reorder_projects`.

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
            "INSERT INTO projects (user_id, name, description, domain, notes, color, rank) "
            "VALUES (?, ?, ?, ?, ?, ?, "
            "  (SELECT COALESCE(MAX(rank), 0) + 1 FROM projects "
            "     WHERE user_id = ? AND status = 'active'))",
            (self._uid(), name, description, domain, notes, color, self._uid()),
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
        """Return the user's projects in forced priority order (rank 1 first).

        Active projects come first, ordered by their forced ``rank`` (top priority
        first); archived projects (rank NULL) sort last, newest first.

        Args:
            include_archived: When ``False`` (default), only ``active`` projects.
        """
        status_clause = "" if include_archived else " AND status = 'active'"
        return self._query_all(
            f"SELECT * FROM projects WHERE user_id = ?{status_clause} "
            "ORDER BY (status = 'active') DESC, (rank IS NULL), rank ASC, id DESC",
            (self._uid(),),
        )

    def list_projects_with_rollup(
        self, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        """Like :meth:`list_projects`, but each row carries dashboard rollup counts.

        Adds ``open_todos`` (open todos in the project), ``next_commitment_at``
        (soonest upcoming active, non-hidden commitment, or ``None``), and
        ``focus_minutes_7d`` (heads-down minutes on the project in the last 7 days,
        from focus-session start→end, using ``now`` for a still-open session). All
        computed in one query via correlated subqueries — personal-scale, so the
        subqueries are cheap and keep the rollup consistent with a single read.
        """
        status_clause = "" if include_archived else " AND status = 'active'"
        return self._query_all(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM todos t WHERE t.project_id = p.id "
            "  AND t.user_id = p.user_id AND t.status = 'open') AS open_todos, "
            "(SELECT MIN(c.start_at) FROM commitments c WHERE c.project_id = p.id "
            "  AND c.user_id = p.user_id AND c.status = 'active' AND c.hidden = 0 "
            "  AND c.start_at >= datetime('now')) AS next_commitment_at, "
            "(SELECT COALESCE(SUM((julianday(COALESCE(f.ended_at, 'now')) "
            "  - julianday(f.started_at)) * 1440.0), 0) FROM focus_sessions f "
            "  WHERE f.project_id = p.id AND f.user_id = p.user_id "
            "  AND f.started_at >= datetime('now', '-7 days')) AS focus_minutes_7d "
            f"FROM projects p WHERE p.user_id = ?{status_clause} "
            "ORDER BY (p.rank IS NULL), p.rank ASC, p.id DESC",
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

    def stale_project_candidates(self) -> list[dict[str, Any]]:
        """Active projects that have open work but have gone quiet.

        Returns one row per active project with ≥1 open todo, carrying
        ``open_todos`` and ``last_activity_at`` — the most recent of any assigned
        focus session (start), todo (update), or commitment (update), falling back
        to the project's own ``created_at`` so a brand-new project is never "stale"
        from birth. The staleness threshold itself lives in the coaching module
        (:class:`prefrontal.modules.projects.ProjectStalenessModule`), which reads
        this and applies the cutoff; the repo just surfaces the raw signal.
        """
        return self._query_all(
            "SELECT p.id, p.name, p.domain, p.notes, "
            "(SELECT COUNT(*) FROM todos t WHERE t.project_id = p.id "
            "  AND t.user_id = p.user_id AND t.status = 'open') AS open_todos, "
            "MAX(p.created_at, "
            "  COALESCE((SELECT MAX(f.started_at) FROM focus_sessions f "
            "    WHERE f.project_id = p.id AND f.user_id = p.user_id), p.created_at), "
            "  COALESCE((SELECT MAX(t.updated_at) FROM todos t "
            "    WHERE t.project_id = p.id AND t.user_id = p.user_id), p.created_at), "
            "  COALESCE((SELECT MAX(c.updated_at) FROM commitments c "
            "    WHERE c.project_id = p.id AND c.user_id = p.user_id), p.created_at) "
            ") AS last_activity_at "
            "FROM projects p WHERE p.user_id = ? AND p.status = 'active' "
            "AND EXISTS (SELECT 1 FROM todos t WHERE t.project_id = p.id "
            "  AND t.user_id = p.user_id AND t.status = 'open') "
            "ORDER BY last_activity_at ASC",
            (self._uid(),),
        )

    def project_stats(self, project_id: int) -> dict[str, Any]:
        """Follow-through + focus rollup for one project (for the detail view).

        ``open_todos``/``done_todos`` and a ``follow_through`` ratio (done / done+open,
        or ``None`` when there's nothing closed-or-open to rate), plus total
        ``focus_minutes`` across the project's focus sessions (start→end, ``now`` for
        a still-open one). Scoped to the user; a project with no todos rates ``None``.
        """
        row = self.conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM todos t WHERE t.project_id = ? AND t.user_id = ? "
            "  AND t.status = 'open') AS open_todos, "
            "(SELECT COUNT(*) FROM todos t WHERE t.project_id = ? AND t.user_id = ? "
            "  AND t.status = 'done') AS done_todos, "
            "(SELECT COALESCE(SUM((julianday(COALESCE(f.ended_at, 'now')) "
            "  - julianday(f.started_at)) * 1440.0), 0) FROM focus_sessions f "
            "  WHERE f.project_id = ? AND f.user_id = ?) AS focus_minutes",
            (project_id, self._uid(), project_id, self._uid(), project_id, self._uid()),
        ).fetchone()
        stats = dict(row)
        rated = stats["open_todos"] + stats["done_todos"]
        stats["follow_through"] = (stats["done_todos"] / rated) if rated else None
        return stats

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
        active rows, so the name frees up for reuse. The project drops out of the
        forced priority order (its ``rank`` is cleared and the remaining active
        projects are re-numbered contiguously), so no gap is left behind.
        """
        cur = self.conn.execute(
            "UPDATE projects SET status = 'archived', rank = NULL, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ? AND status = 'active'",
            (project_id, self._uid()),
        )
        if cur.rowcount > 0:
            self._normalize_ranks()
        self.conn.commit()
        return cur.rowcount > 0

    def reorder_projects(self, ordered_ids: list[int]) -> list[dict[str, Any]]:
        """Set the forced 1..N priority order of the user's active projects.

        ``ordered_ids`` must be exactly the user's active project ids, each once,
        in the desired priority order (index 0 → rank 1 = top). This is how the
        "objective priority" is *forced*: every active project gets a distinct
        rank, so no two can tie. Raises :class:`ValueError` if the id set doesn't
        match the active set (stale client — the caller maps it to a 422).

        Returns the projects with rollups in the new order (ready to re-render).
        """
        active = {
            r["id"]
            for r in self._query_all(
                "SELECT id FROM projects WHERE user_id = ? AND status = 'active'",
                (self._uid(),),
            )
        }
        if len(ordered_ids) != len(set(ordered_ids)) or set(ordered_ids) != active:
            raise ValueError(
                "reorder must list every active project exactly once "
                f"(got {len(ordered_ids)} ids for {len(active)} active projects)"
            )
        for rank, project_id in enumerate(ordered_ids, start=1):
            self.conn.execute(
                "UPDATE projects SET rank = ? WHERE id = ? AND user_id = ?",
                (rank, project_id, self._uid()),
            )
        self.conn.commit()
        return self.list_projects_with_rollup()

    def project_rank_map(self) -> dict[int, int]:
        """Map ``project_id → rank`` for the user's active projects.

        The scheduler stays project-unaware, so the fit/now surfaces stamp each
        open todo's ``project_rank`` from this map before ranking
        (:func:`prefrontal.scheduling.fit_todos`) — a todo in a higher-priority
        project (lower rank) breaks ties ahead of one in a lower-priority project.
        """
        return {
            r["id"]: r["rank"]
            for r in self._query_all(
                "SELECT id, rank FROM projects "
                "WHERE user_id = ? AND status = 'active' AND rank IS NOT NULL",
                (self._uid(),),
            )
        }

    def _normalize_ranks(self) -> None:
        """Re-number the user's active projects to a contiguous 1..N by current rank.

        Called after an archive removes a project from the order, so the remaining
        ranks have no gaps. Does not commit (the caller does). NULL ranks (should
        only be a just-archived row) sort last but archived rows are excluded here.
        """
        rows = self.conn.execute(
            "SELECT id FROM projects WHERE user_id = ? AND status = 'active' "
            "ORDER BY (rank IS NULL), rank ASC, id ASC",
            (self._uid(),),
        ).fetchall()
        for rank, row in enumerate(rows, start=1):
            self.conn.execute(
                "UPDATE projects SET rank = ? WHERE id = ?", (rank, row["id"])
            )

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
