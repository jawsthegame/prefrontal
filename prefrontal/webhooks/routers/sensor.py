"""HTTP routes tagged "sensor" — the LLM-as-sensor surface (learning §2).

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`. Mirrors the
``prefrontal note`` / ``prefrontal proposals`` CLI over HTTP so a phone shortcut,
n8n, or the dashboard can feed a free-text note and review the resulting
*pending* candidate updates. Nothing here writes an authoritative fact: the
sensor only proposes from a small allowlist, and a human accept is what applies
a proposal (stamped ``source='llm_inferred'``). See :mod:`prefrontal.sensor`.
"""
from __future__ import annotations

from fastapi import APIRouter

from prefrontal.webhooks._common import (
    Annotated,
    Any,
    Depends,
    HTTPException,
    ObserveRequest,
    Query,
    ScopedRequest,
    apply_proposal,
    extract_candidates,
    record_candidates,
    resolve_user,
    status,
    summarize_candidate,
)
from prefrontal.webhooks.services import RouterServices


def build_router(services: RouterServices) -> APIRouter:
    """Build the "sensor" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    # Claude when the ``sensor`` agent is opted into Anthropic, else local Ollama.
    sensor_client = services.provider.client("sensor")

    def _describe(proposal: dict[str, Any]) -> dict[str, Any]:
        """A compact, review-ready view of a proposal row (no raw payload noise)."""
        return {
            "id": proposal["id"],
            "kind": proposal["kind"],
            "summary": summarize_candidate(proposal["kind"], proposal["payload"]),
            "rationale": proposal.get("rationale") or "",
            "status": proposal["status"],
            "created_at": proposal.get("created_at"),
        }

    @router.post("/observe", status_code=status.HTTP_201_CREATED, tags=["sensor"])
    def observe(
        payload: ObserveRequest,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Read a free-text note and record any candidate updates as *pending*.

        The HTTP twin of ``prefrontal note``. The local model reads the note and
        proposes allowlisted `state`/`episode` candidates (:mod:`prefrontal.sensor`);
        each lands as a pending `proposals` row for review at ``GET /proposals``.
        Nothing authoritative is written here — accept a proposal to apply it.

        Returns the created proposals (compact form) and their count. When the
        note yields nothing, or the model is unreachable, ``count`` is 0 and
        ``proposals`` is empty (an honest no-guess result, not an error).
        """
        memory = ctx.store
        candidates = extract_candidates(payload.text, client=sensor_client)
        ids = record_candidates(memory, candidates)
        # Read back the freshly-stored rows so the response matches GET /proposals.
        created = [p for p in memory.list_proposals("pending") if p["id"] in set(ids)]
        return {"count": len(ids), "proposals": [_describe(p) for p in created]}

    @router.get("/proposals", tags=["sensor"])
    def list_proposals(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        proposal_status: Annotated[
            str,
            Query(
                alias="status",
                description="Filter by status (pending/accepted/rejected), or 'all'.",
            ),
        ] = "pending",
    ) -> dict[str, Any]:
        """List the caller's sensor proposals (newest first), compact form.

        Defaults to ``pending`` — the review queue. Pass ``status=all`` for the
        full history, or a specific status.
        """
        memory = ctx.store
        query = "" if proposal_status == "all" else proposal_status
        rows = memory.list_proposals(query)
        return {"proposals": [_describe(p) for p in rows]}

    @router.post("/proposals/{proposal_id}/{action}", tags=["sensor"])
    def resolve_proposal(
        proposal_id: int,
        action: str,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Accept or reject a pending proposal by id.

        ``accept`` applies the update to the store (``source='llm_inferred'``) and
        marks the row accepted; ``reject`` just resolves it. Only a still-pending
        proposal moves, so a double-accept is a no-op 404 rather than a re-apply.
        """
        if action not in ("accept", "reject"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown action {action!r}."
            )
        memory = ctx.store
        proposal = memory.get_proposal(proposal_id)
        if proposal is None or proposal["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No pending proposal {proposal_id}.",
            )
        if action == "accept":
            applied = apply_proposal(memory, proposal)
            memory.set_proposal_status(proposal_id, "accepted")
            return {"id": proposal_id, "status": "accepted", "applied": applied}
        memory.set_proposal_status(proposal_id, "rejected")
        return {"id": proposal_id, "status": "rejected"}

    return router
