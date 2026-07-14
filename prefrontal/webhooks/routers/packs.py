"""HTTP routes tagged "packs" — Context Pack situation tools.

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.

A Context Pack (:mod:`prefrontal.packs`) can declare read-only **situation
tools** — named, on-demand questions it answers from the caller's live data (the
Parent pack's school-run leave-by, for one). This router lists the tools the
*enabled* packs contribute and runs one on request. Every tool is read-only and
gated on its owning pack being enabled, so a tool behind a disabled pack is
invisible — it 404s exactly like one that doesn't exist.
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
)

from prefrontal.packs.registry import enabled_situations, get_situation
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.services import RouterServices


def build_router(services: RouterServices) -> APIRouter:
    """Build the "packs" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    resolved_settings = services.settings
    # The local Ollama client — deliberately *not* the per-agent provider client.
    # A situation tool's only LLM lever today is decompose_task, which (like every
    # snappy in-loop inference: window/title/kind) stays local by design, and which
    # catches only OllamaError — handing it an Anthropic client would let an
    # AnthropicError escape as a 500 instead of falling back to the heuristic.
    decompose_client = services.ollama

    @router.get("/packs/situations", tags=["packs"])
    def list_situations(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """List the situation tools the enabled packs contribute — read-only.

        Only tools whose owning pack is enabled (``PREFRONTAL_PACKS``) appear, so
        with no pack enabled this is an empty list. Each entry carries the ``tool``
        key to POST back to ``/packs/situations/{tool}``, plus its title and a
        one-line description for a menu.
        """
        return {
            "situations": [
                {"tool": t.key, "title": t.title, "description": t.description}
                for t in enabled_situations(resolved_settings)
            ]
        }

    @router.post("/packs/situations/{tool}", tags=["packs"])
    def run_situation(
        tool: str,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Run one situation tool against the caller's data — read-only.

        The tool computes a result from the store (e.g. the school-run leave-by
        from the departure engine) and writes nothing. The local Ollama client is
        passed through for tools that compose an LLM lever (the pack-the-bag
        checklist decomposes each event, falling back to a heuristic when it's
        unavailable); deterministic tools ignore it. Unknown tools and tools behind
        a disabled pack both 404 — a tool you can't currently reach should look the
        same as one that doesn't exist.
        """
        situation = get_situation(tool, resolved_settings)
        if situation is None:
            raise HTTPException(status_code=404, detail=f"Unknown situation tool: {tool!r}")
        return situation.handler(ctx.store, client=decompose_client)

    return router
