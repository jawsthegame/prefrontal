"""HTTP routes tagged "blockers" — who's waiting on you.

CRUD for blockers (someone else is blocked until you do a thing) plus the
resolve/reopen lifecycle. A blocker feeds prioritization: it surfaces in panic
mode and the morning briefing so an unblock can outrank a shiny new task. See
:mod:`prefrontal.memory.repos.blockers` and :mod:`prefrontal.blockers`.

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from prefrontal.blockers import normalize_person
from prefrontal.webhooks.deps import ScopedRequest, resolve_user
from prefrontal.webhooks.schemas import BlockerCreate, BlockerUpdate
from prefrontal.webhooks.services import RouterServices


def build_router(services: RouterServices) -> APIRouter:
    """Build the "blockers" APIRouter (shared services injected by create_app)."""
    router = APIRouter()

    @router.post("/blockers", status_code=status.HTTP_201_CREATED, tags=["blockers"])
    def blocker_create(
        payload: BlockerCreate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Log that someone is waiting on you. ``person`` and ``what`` are required."""
        memory = ctx.store
        person = normalize_person(payload.person)
        what = (payload.what or "").strip()
        if not person:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A blocker needs a person.")
        if not what:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "A blocker needs a 'what'."
            )
        blocker_id = memory.add_blocker(
            person,
            what,
            notes=payload.notes,
            priority=payload.priority,
            deadline=payload.deadline,
            blocking_since=payload.blocking_since,
            todo_id=payload.todo_id,
        )
        return memory.get_blocker(blocker_id)

    @router.get("/blockers", tags=["blockers"])
    def blockers_list(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        include_resolved: bool = False,
    ) -> dict[str, Any]:
        """List blockers, most pressing first. Open only unless ``include_resolved=1``."""
        return {"blockers": ctx.store.list_blockers(include_resolved=include_resolved)}

    @router.get("/blockers/{blocker_id}", tags=["blockers"])
    def blocker_get(
        blocker_id: int,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """A single blocker by id."""
        blocker = ctx.store.get_blocker(blocker_id)
        if blocker is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No blocker {blocker_id}.")
        return blocker

    @router.patch("/blockers/{blocker_id}", tags=["blockers"])
    def blocker_update(
        blocker_id: int,
        payload: BlockerUpdate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Edit any subset of a blocker's fields (not its status — use resolve/reopen)."""
        memory = ctx.store
        if memory.get_blocker(blocker_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No blocker {blocker_id}.")
        fields = payload.model_dump(exclude_unset=True)
        if "person" in fields:
            person = normalize_person(fields["person"] or "")
            if not person:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "A blocker needs a person."
                )
            fields["person"] = person
        if "what" in fields:
            what = (fields["what"] or "").strip()
            if not what:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "A blocker needs a 'what'."
                )
            fields["what"] = what
        return memory.update_blocker(blocker_id, **fields)

    @router.post("/blockers/{blocker_id}/resolve", tags=["blockers"])
    def blocker_resolve(
        blocker_id: int,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Mark a blocker resolved — you delivered, the wait is over."""
        blocker = ctx.store.resolve_blocker(blocker_id)
        if blocker is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No blocker {blocker_id}.")
        return blocker

    @router.post("/blockers/{blocker_id}/reopen", tags=["blockers"])
    def blocker_reopen(
        blocker_id: int,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Reopen a resolved blocker — they're waiting on you again."""
        blocker = ctx.store.reopen_blocker(blocker_id)
        if blocker is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No blocker {blocker_id}.")
        return blocker

    return router
