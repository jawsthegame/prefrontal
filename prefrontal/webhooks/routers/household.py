"""HTTP routes tagged "household" — the shared co-parent sheet.

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter

from prefrontal.webhooks._common import (
    Annotated,
    Any,
    Depends,
    HTTPException,
    ScopedRequest,
    build_sheet,
    render_sheet,
    resolve_user,
    status,
)


def build_router(
    *,
    resolved_settings,
    n8n,
    ollama_client,
    summarizer_client,
    geocoder_client,
    _run_geocode,
) -> APIRouter:
    """Build the "household" APIRouter (shared services injected by create_app)."""
    router = APIRouter()

    @router.get("/household/sheet", tags=["household"])
    def household_sheet(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Return the caller's shared household sheet — structured + rendered Markdown.

        Both co-parents see the same rows (the sheet is household-scoped, not
        per-user). Deterministic assembly, built on request — no cache. A caller
        who isn't in a household gets 404 (there's nothing shared to show), rather
        than the store's raw scope error.
        """
        if ctx.store.household_id_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="You're not set up in a household.",
            )
        sheet = build_sheet(ctx.store)
        return {"sheet": asdict(sheet), "markdown": render_sheet(sheet)}

    return router
