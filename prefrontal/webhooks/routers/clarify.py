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
    _known_task_type as infer_task_type,
)
from prefrontal.clarify import (
    candidate_view,
    detect_clarification,
    known_task_types,
    playbook_view,
    resolve_playbook,
)
from prefrontal.memory.repos.clarifications import TARGET_COMMITMENT, TARGET_TODO
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.schemas import ClarificationResolve
from prefrontal.webhooks.services import RouterServices

#: Cap on how many ambiguous items one sweep looks at, so a tick fires a bounded
#: number of model calls (worst-avoided/soonest first). Matches the spirit of the
#: decomposition sweep's ``max_attempts``.
MAX_SWEEP_ITEMS = 8


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

        Sweeps open todos and upcoming commitments the user hasn't been asked
        about yet (:meth:`clarified_target_ids` — skips pending, answered, *and*
        dismissed items so nothing is re-asked), scores each for ambiguity, and
        for the vague ones proposes a clarifying question via
        :func:`~prefrontal.clarify.detect_clarification` (local model, heuristic
        fallback). Bounded to ``limit`` detections per call. n8n/launchd can tick
        this like the other ``/check`` sweeps. Returns the created questions.
        """
        memory = ctx.store
        created: list[dict[str, Any]] = []
        # Todos first (priority-ordered), then upcoming commitments — both loose
        # refs, each skipped if it already has any clarification history.
        seen_todos = memory.clarified_target_ids(TARGET_TODO)
        seen_commits = memory.clarified_target_ids(TARGET_COMMITMENT)
        candidates: list[tuple[str, dict[str, Any]]] = (
            [(TARGET_TODO, t) for t in memory.open_todos() if t["id"] not in seen_todos]
            + [
                (TARGET_COMMITMENT, c)
                for c in memory.upcoming_commitments(limit=50)
                if c["id"] not in seen_commits and c.get("kind") != "fyi"
            ]
        )
        checked = 0
        for target_type, item in candidates:
            if checked >= limit:
                break
            checked += 1
            candidate = detect_clarification(item.get("title") or "", client=ollama_client)
            if candidate is None:
                continue
            cid = memory.add_clarification(
                target_type=target_type,
                target_id=item["id"],
                title=candidate.title,
                question=candidate.question,
                options=candidate_view(candidate)["options"],
                source=candidate.source,
            )
            row = memory.get_clarification(cid)
            if row is not None:
                created.append(_pending_view(row))
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
        options = row.get("options") or []
        answer: str | None = None
        task_type: str | None = None
        if payload.option_index is not None:
            if not (0 <= payload.option_index < len(options)):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"option_index {payload.option_index} is out of range.",
                )
            chosen = options[payload.option_index]
            answer = str(chosen.get("label") or "").strip()
            task_type = chosen.get("task_type")
        elif payload.answer and payload.answer.strip():
            answer = payload.answer.strip()
            # A free-text answer may still name a recognized task ("file my taxes").
            task_type = payload.task_type or infer_task_type(answer)
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Provide an 'option_index' or a non-empty 'answer'.",
            )
        # Only a recognized type unlocks a guide; anything else is a plain reading.
        if task_type not in known_task_types():
            task_type = None

        memory.resolve_clarification(clarification_id, answer=answer, task_type=task_type)

        # Hone the item itself for a todo: append the chosen reading to its notes
        # (non-destructive — the title is left alone so a calendar sync can't clash).
        if row["target_type"] == TARGET_TODO:
            todo = memory.get_todo(row["target_id"])
            if todo is not None and todo.get("status") == "open":
                existing = (todo.get("notes") or "").strip()
                note = f"Clarified: {answer}"
                memory.set_todo_notes(
                    row["target_id"], f"{existing}\n{note}" if existing else note
                )

        playbook = resolve_playbook(task_type)
        return {
            "id": clarification_id,
            "status": "resolved",
            "answer": answer,
            "task_type": task_type,
            "playbook": playbook_view(playbook) if playbook is not None else None,
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
        re-answering. Auth-gated like every surface, though the steps themselves
        are the same static playbook for everyone.
        """
        playbook = resolve_playbook(task_type)
        if playbook is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No playbook for task type {task_type!r}.",
            )
        return playbook_view(playbook)

    return router
