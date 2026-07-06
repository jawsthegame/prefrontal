"""HTTP routes tagged "clarify" — ambiguity clarification + guided playbooks.

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`. Surfaces the
task-initiation lever from :mod:`prefrontal.clarify`: a sweep that notices vague
todos/commitments and files a pending clarifying question, the inline
answer/dismiss actions the dashboard renders, and the guided playbook a resolved
item unlocks. Nothing is asked about a clear item, and a question is only written
until the human answers — the same propose-then-confirm shape as the LLM sensor.
"""
from __future__ import annotations

from typing import (
    Annotated,
    Any,
)

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    status,
)

from prefrontal.clarify import (
    MAX_SWEEP_ITEMS,
    apply_clarification_answer,
    known_task_types,
    localized_zip,
    playbook_view,
    resolve_playbook,
    sweep_ambiguous_items,
)
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.schemas import ClarificationResolve
from prefrontal.webhooks.services import RouterServices


def build_router(services: RouterServices) -> APIRouter:
    """Build the "clarify" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    # Snappy in-loop inference (one short JSON call per item) — local by design,
    # like the todo augment/decompose paths; heuristic fallback when it's down.
    ollama_client = services.ollama

    def _pending_view(row: dict[str, Any]) -> dict[str, Any]:
        """A dashboard-ready view of a pending clarification (question + options)."""
        known = known_task_types()
        return {
            "id": row["id"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "title": row["title"],
            "question": row["question"],
            "source": row.get("source"),
            "options": [
                {
                    "label": o.get("label"),
                    "task_type": o.get("task_type"),
                    "has_playbook": o.get("task_type") in known,
                }
                for o in (row.get("options") or [])
            ],
        }

    def _guided_view(row: dict[str, Any]) -> dict[str, Any] | None:
        """A resolved clarification that unlocked a playbook, or ``None``."""
        playbook = resolve_playbook(row.get("task_type"))
        if playbook is None:
            return None
        return {
            "id": row["id"],
            "title": row["title"],
            "answer": row.get("answer"),
            "task_type": row["task_type"],
            "playbook_title": playbook.title,
        }

    @router.get("/clarifications", tags=["clarify"])
    def list_clarifications(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Pending clarifying questions, plus resolved items that have a guide.

        ``clarifications`` is the inline review queue — one question per ambiguous
        item with its candidate readings (each flagged ``has_playbook``).
        ``guided`` lists recently-resolved items whose chosen reading maps to a
        built-in playbook, so the dashboard can re-open the walkthrough.
        """
        memory = ctx.store
        pending = [_pending_view(r) for r in memory.list_clarifications("pending")]
        guided = [
            g for r in memory.list_clarifications("resolved", limit=20)
            if (g := _guided_view(r)) is not None
        ]
        return {"clarifications": pending, "guided": guided[:10]}

    @router.post("/clarifications/check", tags=["clarify"])
    def clarifications_check(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        limit: Annotated[int, Query(ge=1, le=MAX_SWEEP_ITEMS)] = MAX_SWEEP_ITEMS,
    ) -> dict[str, Any]:
        """Detect newly-ambiguous todos/commitments and file pending questions.

        The manual twin of the coaching-tick sweep: it runs the same
        :func:`~prefrontal.clarify.sweep_ambiguous_items` (skips items with any
        clarification history so nothing is re-asked, scores each for ambiguity,
        and files ONE question for the vague ones — local model, heuristic
        fallback), bounded to ``limit`` detections. Also wired into
        ``POST /webhooks/coach/check`` so the queue fills passively; this endpoint
        is the on-demand "check now" button. Returns the created questions.
        """
        memory = ctx.store
        ids = sweep_ambiguous_items(memory, ollama_client, limit=limit)
        created = [
            _pending_view(row)
            for cid in ids
            if (row := memory.get_clarification(cid)) is not None
        ]
        return {"created": len(created), "clarifications": created}

    @router.post("/clarifications/{clarification_id}/resolve", tags=["clarify"])
    def clarification_resolve(
        clarification_id: int,
        payload: ClarificationResolve,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Answer a pending clarification — honing the item in — and return its guide.

        Provide ``option_index`` (pick one of the offered readings) or a free-text
        ``answer``. The chosen reading is recorded on the clarification; when it
        maps to a recognized task type the response carries that type's
        **playbook** so the dashboard can open the guided walkthrough. A ``todo``
        target also gets the reading appended to its notes, so the honed meaning
        reads clearly everywhere — not just on the question. 404 if there's no
        pending clarification by that id; 422 if neither an option nor an answer
        is given.
        """
        memory = ctx.store
        row = memory.get_clarification(clarification_id)
        if row is None or row["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No pending clarification {clarification_id}.",
            )
        try:
            result = apply_clarification_answer(
                memory,
                row,
                option_index=payload.option_index,
                answer=payload.answer,
                task_type=payload.task_type,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        playbook = result["playbook"]
        return {
            "id": clarification_id,
            "status": "resolved",
            "answer": result["answer"],
            "task_type": result["task_type"],
            "playbook": (
                playbook_view(playbook, zip_code=localized_zip(memory))
                if playbook is not None
                else None
            ),
        }

    @router.post("/clarifications/{clarification_id}/dismiss", tags=["clarify"])
    def clarification_dismiss(
        clarification_id: int,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Dismiss a pending clarification — "this isn't ambiguous, leave it."

        The item is remembered as asked-about, so the sweep won't question it
        again. 404 if there's no pending clarification by that id.
        """
        memory = ctx.store
        if not memory.dismiss_clarification(clarification_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No pending clarification {clarification_id}.",
            )
        return {"id": clarification_id, "status": "dismissed"}

    @router.get("/clarifications/playbooks/{task_type}", tags=["clarify"])
    def get_playbook(
        task_type: str,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Return the guided walkthrough for a recognized task type (404 otherwise).

        Lets the dashboard re-open a guide for an already-resolved item without
        re-answering. Auth-gated like every surface; the steps are localized to the
        user's home ZIP when they've opted into localization (else generic).
        """
        playbook = resolve_playbook(task_type)
        if playbook is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No playbook for task type {task_type!r}.",
            )
        return playbook_view(playbook, zip_code=localized_zip(ctx.store))

    return router
