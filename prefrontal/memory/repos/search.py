"""Cross-entity free-text search for the dashboard's global search box.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.

One method, :meth:`SearchRepo.search`, runs a case-insensitive substring match
over the user's todos, commitments, focus sessions, and projects and returns the
hits grouped by type, **ranked by relevance**. Unlike the per-card list methods
(``open_todos``, ``upcoming_commitments``, …) it deliberately searches *all* rows
regardless of status/time window — the point of a global search is to surface a
done todo or a past commitment you half-remember, not just what's on today's
surfaces.

Ranking (see :func:`_match_quality` / :meth:`SearchRepo._rank`): a hit's score is
the best field match it has — a match in a title/name outweighs one buried in
notes, and a whole-word or prefix hit outweighs a mid-word substring — plus a
small boost for a still-live row (an open todo, an active project) so stale items
sink. Matching runs in SQL (cheap ``LIKE``); scoring/sorting runs in Python over
the (small, personal-scale) candidate set.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory.repos._base import Repo

#: Cap per entity type in the *returned* results so one noisy table can't drown
#: the others in the panel. The dashboard shows these grouped; a user narrows
#: with a longer query.
_DEFAULT_LIMIT = 10

#: Safety cap on rows *scanned* per type before ranking. Well above any real
#: per-user table, it just stops a one-character query from loading everything
#: into memory to score it.
_SCAN_CAP = 200

# Field-class weights: a title/name hit is worth much more than a notes hit.
_PRIMARY = 3.0    # the row's name: title / name / intended_task
_SECONDARY = 1.5  # short structured text: category / domain / location
_TERTIARY = 1.0   # long free text: notes / description / breadcrumb / outcome

#: Nudge a still-live row above an equally-relevant stale one (done/archived/past).
_LIVE_BOOST = 0.5


def _like_term(query: str) -> str:
    """Wrap ``query`` as a ``LIKE`` term, escaping its own wildcards.

    ``%`` and ``_`` are the SQL wildcards and ``\\`` our chosen escape char, so a
    user typing any of them searches for the literal character (paired with
    ``ESCAPE '\\'`` in the statements below) rather than triggering a match-all.
    """
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _match_quality(text: str | None, q: str) -> float:
    """Score how well ``q`` (already lowercased) matches ``text``, in ``[0, 1]``.

    Exact equality beats a prefix beats a word-boundary hit beats a bare
    mid-word substring — so searching "tile" ranks a todo *titled* "Tile" over
    one that merely mentions "reptile" somewhere in its notes.
    """
    if not text:
        return 0.0
    t = text.lower()
    if t == q:
        return 1.0
    if t.startswith(q):
        return 0.8
    idx = t.find(q)
    if idx < 0:
        return 0.0
    # A match right after a non-alphanumeric char reads as a whole-word hit.
    return 0.6 if not t[idx - 1].isalnum() else 0.4


class SearchRepo(Repo):
    """Free-text search across the user's todos/commitments/focus/projects."""

    @staticmethod
    def _rank(
        rows: list[dict[str, Any]],
        q: str,
        fields: list[tuple[str, float]],
        *,
        live: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Sort ``rows`` by relevance to ``q`` and keep the top ``limit``.

        Args:
            fields: ``(column, weight)`` pairs to match against; a row's text
                score is the best single field match, weighted.
            live: the ``status`` value that counts as "still live" (e.g.
                ``"open"``/``"active"``) and earns :data:`_LIVE_BOOST`.
            limit: how many rows to return after ranking.

        The sort is stable, so rows of equal score keep the SQL order they came
        in with (each query's ``ORDER BY`` is a sensible tiebreak — priority,
        recency, active-first).
        """
        ql = q.lower()

        def score(row: dict[str, Any]) -> float:
            text = max(_match_quality(row.get(col), ql) * w for col, w in fields)
            return text + (_LIVE_BOOST if row.get("status") == live else 0.0)

        return sorted(rows, key=score, reverse=True)[:limit]

    def search(
        self, query: str, *, limit_per_type: int = _DEFAULT_LIMIT
    ) -> dict[str, list[dict[str, Any]]]:
        """Return rows matching ``query``, grouped by type and ranked by relevance.

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
            limit_per_type: Max rows per group after ranking.

        Returns:
            ``{"todos": [...], "commitments": [...], "focus_sessions": [...],
            "projects": [...]}`` — each a list of plain row dicts, best match
            first, possibly empty.
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
            (uid, term, term, term, term, _SCAN_CAP),
        )
        commitments = self._query_all(
            "SELECT id, title, start_at, end_at, location, notes, domain, status "
            "FROM commitments WHERE user_id = ? AND hidden = 0 AND ("
            "title LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\' OR "
            "notes LIKE ? ESCAPE '\\') "
            "ORDER BY start_at DESC LIMIT ?",
            (uid, term, term, term, _SCAN_CAP),
        )
        focus_sessions = self._query_all(
            "SELECT id, intended_task, breadcrumb, outcome, status, started_at, "
            "ended_at FROM focus_sessions WHERE user_id = ? AND ("
            "intended_task LIKE ? ESCAPE '\\' OR breadcrumb LIKE ? ESCAPE '\\' OR "
            "outcome LIKE ? ESCAPE '\\') "
            "ORDER BY started_at DESC LIMIT ?",
            (uid, term, term, term, _SCAN_CAP),
        )
        projects = self._query_all(
            "SELECT id, name, description, notes, domain, status, color FROM projects "
            "WHERE user_id = ? AND ("
            "name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' OR "
            "notes LIKE ? ESCAPE '\\') "
            "ORDER BY (status = 'active') DESC, id DESC LIMIT ?",
            (uid, term, term, term, _SCAN_CAP),
        )
        return {
            "todos": self._rank(
                todos, q,
                [("title", _PRIMARY), ("category", _SECONDARY),
                 ("domain", _SECONDARY), ("notes", _TERTIARY)],
                live="open", limit=limit_per_type,
            ),
            "commitments": self._rank(
                commitments, q,
                [("title", _PRIMARY), ("location", _SECONDARY), ("notes", _TERTIARY)],
                live="active", limit=limit_per_type,
            ),
            "focus_sessions": self._rank(
                focus_sessions, q,
                [("intended_task", _PRIMARY), ("breadcrumb", _TERTIARY),
                 ("outcome", _TERTIARY)],
                live="active", limit=limit_per_type,
            ),
            "projects": self._rank(
                projects, q,
                [("name", _PRIMARY), ("description", _TERTIARY), ("notes", _TERTIARY)],
                live="active", limit=limit_per_type,
            ),
        }
