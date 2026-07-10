"""HTTP routes tagged "projects".

CRUD for user projects plus the per-entity assignment endpoints
(``POST /{entity}/{id}/project``). A project is nested under one ``domain``;
assigning an entity writes that domain through, so the scheduler stays
project-unaware (see :mod:`prefrontal.memory.repos.projects`).

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from prefrontal.focus_balance import normalize_focus_domain
from prefrontal.projects import normalize_project_name
from prefrontal.webhooks.deps import ScopedRequest, resolve_user
from prefrontal.webhooks.schemas import (
    EntityProjectUpdate,
    ProjectCreate,
    ProjectUpdate,
)
from prefrontal.webhooks.services import RouterServices


def build_router(services: RouterServices) -> APIRouter:
    """Build the "projects" APIRouter (shared services injected by create_app)."""
    router = APIRouter()

    @router.post("/projects", status_code=status.HTTP_201_CREATED, tags=["projects"])
    def project_create(
        payload: ProjectCreate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Create a project under a life domain.

        The name is trimmed and must be free among the user's *active* projects
        (409 otherwise; an archived project may share the name). The domain is
        required and snapped onto the canonical vocabulary where obvious; an
        unknown value is kept as free text (like todos/trips), only a blank domain
        is rejected (422).
        """
        memory = ctx.store
        name = normalize_project_name(payload.name)
        if not name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A project needs a name.")
        domain = normalize_focus_domain(payload.domain)
        if not domain:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "A project needs a domain."
            )
        if memory.project_name_in_use(name):
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"A project named '{name}' already exists."
            )
        project_id = memory.add_project(
            name, domain, description=payload.description, notes=payload.notes,
            color=payload.color,
        )
        return memory.get_project(project_id)

    @router.get("/projects", tags=["projects"])
    def projects_list(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """List the user's projects with dashboard rollups (open todos · next
        commitment · focus minutes this week), active only unless
        ``include_archived=1``, grouped-ready (ordered by domain)."""
        return {
            "projects": ctx.store.list_projects_with_rollup(
                include_archived=include_archived
            )
        }

    @router.get("/projects/{project_id}", tags=["projects"])
    def project_get(
        project_id: int,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """A project's detail: its record plus its open todos, upcoming commitments,
        and recent focus sessions."""
        memory = ctx.store
        project = memory.get_project(project_id)
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No project {project_id}.")
        todos = [t for t in memory.open_todos() if t.get("project_id") == project_id]
        commitments = [
            c for c in memory.upcoming_commitments() if c.get("project_id") == project_id
        ]
        sessions = [
            s for s in memory.recent_focus_sessions() if s.get("project_id") == project_id
        ]
        return {
            "project": project,
            "todos": todos,
            "commitments": commitments,
            "focus_sessions": sessions,
        }

    @router.patch("/projects/{project_id}", tags=["projects"])
    def project_update(
        project_id: int,
        payload: ProjectUpdate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Edit any subset of a project's fields.

        A new ``name`` is trimmed and re-checked for a clash (409); a new
        ``domain`` is normalized (422 if unrecognized) and cascaded to the
        project's assigned todos/commitments.
        """
        memory = ctx.store
        if memory.get_project(project_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No project {project_id}.")
        fields = payload.model_dump(exclude_unset=True)
        if "name" in fields:
            name = normalize_project_name(fields["name"] or "")
            if not name:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A project needs a name.")
            if memory.project_name_in_use(name, exclude_id=project_id):
                raise HTTPException(
                    status.HTTP_409_CONFLICT, f"A project named '{name}' already exists."
                )
            fields["name"] = name
        if "domain" in fields:
            domain = normalize_focus_domain(fields["domain"])
            if not domain:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "A project needs a domain."
                )
            fields["domain"] = domain
        return memory.update_project(project_id, **fields)

    @router.post("/projects/{project_id}/archive", tags=["projects"])
    def project_archive(
        project_id: int,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Archive a project (soft delete). Assigned items keep their label."""
        if not ctx.store.archive_project(project_id):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"No active project {project_id}."
            )
        return {"project_id": project_id, "status": "archived"}

    def _resolve_project(memory: Any, project_id: int | None) -> None:
        """422 if a non-null project_id doesn't belong to this user."""
        if project_id is not None and memory.get_project(project_id) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"No project {project_id}.")

    # NOTE: the todo→project assignment endpoint lives in the *todos* router,
    # declared before that router's greedy `/todos/{id}/{action}` catch-all so
    # "project" isn't read as an action (same discipline as category/domain).
    # Commitments and focus sessions have no such catch-all, so their assignment
    # endpoints live here.

    @router.post("/commitments/{commitment_id}/project", tags=["projects"])
    def commitment_set_project(
        commitment_id: int,
        payload: EntityProjectUpdate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Assign a commitment to a project (or clear it). Survives calendar re-sync."""
        memory = ctx.store
        _resolve_project(memory, payload.project_id)
        updated = memory.set_commitment_project(commitment_id, payload.project_id)
        if updated is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No commitment {commitment_id}.")
        return updated

    @router.post("/focus/{session_id}/project", tags=["projects"])
    def focus_set_project(
        session_id: int,
        payload: EntityProjectUpdate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Assign a focus session to a project (or clear it)."""
        memory = ctx.store
        _resolve_project(memory, payload.project_id)
        if not memory.set_focus_session_project(session_id, payload.project_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No focus session {session_id}.")
        return {"session_id": session_id, "project_id": payload.project_id}

    return router
