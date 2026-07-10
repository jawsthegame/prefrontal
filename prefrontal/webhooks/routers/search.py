"""HTTP route tagged "search".

A single cross-entity search endpoint backing the dashboard's global search box:
one query, matched against the caller's todos, commitments, focus sessions, and
projects (see :meth:`prefrontal.memory.repos.search.SearchRepo.search`).

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from prefrontal.webhooks.deps import ScopedRequest, resolve_user
from prefrontal.webhooks.services import RouterServices

#: Upper bound on per-type results a caller can request, so a hand-crafted
#: `?limit=100000` can't turn the search into a full-table dump.
_MAX_LIMIT = 50


def build_router(services: RouterServices) -> APIRouter:
    """Build the "search" APIRouter (shared services injected by create_app)."""
    router = APIRouter()

    @router.get("/search", tags=["search"])
    def search(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        q: str = "",
        limit: int = Query(default=10, ge=1, le=_MAX_LIMIT),
    ) -> dict[str, Any]:
        """Free-text search across the caller's todos/commitments/focus/projects.

        ``q`` is matched as a case-insensitive substring over each entity's
        human-authored text (title/notes/etc.). A blank ``q`` returns empty
        groups (``total = 0``) rather than 422, so the client can call it on an
        empty box without special-casing. ``limit`` caps results *per type*.
        """
        results = ctx.store.search(q, limit_per_type=limit)
        total = sum(len(v) for v in results.values())
        return {"query": q.strip(), "total": total, "results": results}

    return router
