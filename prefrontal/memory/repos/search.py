"""Cross-entity free-text search for the dashboard's global search box.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.

One method, :meth:`SearchRepo.search`, runs a case-insensitive substring match
over the user's todos, commitments, focus sessions, and projects and returns the
hits grouped by type. Unlike the per-card list methods (``open_todos``,
``upcoming_commitments``, …) it deliberately searches *all* rows regardless of
status/time window — the point of a global search is to surface a done todo or a
past commitment you half-remember, not just what's on today's surfaces.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory.repos._base import Repo

#: Cap per entity type so one noisy table can't drown the others in the results
#: panel. The dashboard shows these grouped; a user narrows with a longer query.
_DEFAULT_LIMIT = 10


def _like_term(query: str) -> str:
    """Wrap ``query`` as a ``LIKE`` term, escaping its own wildcards.

    ``%`` and ``_`` are the SQL wildcards and ``\\`` our chosen escape char, so a
    user typing any of them searches for the literal character (paired with
    ``ESCAPE '\\'`` in the statements below) rather than triggering a match-all.
    """
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class SearchRepo(Repo):
    """Free-text search across the user's todos/commitments/focus/projects."""

    def search(
        self, query: str, *, limit_per_type: int = _DEFAULT_LIMIT
    ) -> dict[str, list[dict[str, Any]]]:
        """Return rows matching ``query``, grouped by entity type.

        The match is a case-insensitive substring (SQLite ``LIKE``) over each
        entity's human-authored text fields — the same fields the triage/matcher
        layers treat as meaningful:

        - **todos**: title, notes, category, domain
        - **commitments**: title, location, notes
        - **focus_sessions**: intended_task, breadcrumb, outcome
        - **projects**: name, description, notes

        Args:
            query: The raw search string. Blank/whitespace returns empty groups
                (so the endpoint can no-op cheaply rather than match everything).
            limit_per_type: Max rows per group (newest/most-relevant first).

        Returns:
            ``{"todos": [...], "commitments": [...], "focus_sessions": [...],
            "projects": [...]}`` — each a list of plain row dicts, possibly empty.
        """
        q = (query or "").strip()
        empty: dict[str, list[dict[str, Any]]] = {
            "todos": [], "commitments": [], "focus_sessions": [], "projects": []
        }
        if not q:
            return empty
        term = _like_term(q)
        uid = self._uid()

        todos = self._query_all(
            "SELECT id, title, notes, status, priority, deadline, category, domain, "
            "project_id FROM todos WHERE user_id = ? AND ("
            "title LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\' OR "
            "category LIKE ? ESCAPE '\\' OR domain LIKE ? ESCAPE '\\') "
            "ORDER BY (status = 'open') DESC, priority DESC, id DESC LIMIT ?",
            (uid, term, term, term, term, limit_per_type),
        )
        commitments = self._query_all(
            "SELECT id, title, start_at, end_at, location, notes, domain, status "
            "FROM commitments WHERE user_id = ? AND hidden = 0 AND ("
            "title LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\' OR "
            "notes LIKE ? ESCAPE '\\') "
            "ORDER BY start_at DESC LIMIT ?",
            (uid, term, term, term, limit_per_type),
        )
        focus_sessions = self._query_all(
            "SELECT id, intended_task, breadcrumb, outcome, status, started_at, "
            "ended_at FROM focus_sessions WHERE user_id = ? AND ("
            "intended_task LIKE ? ESCAPE '\\' OR breadcrumb LIKE ? ESCAPE '\\' OR "
            "outcome LIKE ? ESCAPE '\\') "
            "ORDER BY started_at DESC LIMIT ?",
            (uid, term, term, term, limit_per_type),
        )
        projects = self._query_all(
            "SELECT id, name, description, domain, status, color FROM projects "
            "WHERE user_id = ? AND ("
            "name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' OR "
            "notes LIKE ? ESCAPE '\\') "
            "ORDER BY (status = 'active') DESC, id DESC LIMIT ?",
            (uid, term, term, term, limit_per_type),
        )
        return {
            "todos": todos,
            "commitments": commitments,
            "focus_sessions": focus_sessions,
            "projects": projects,
        }
