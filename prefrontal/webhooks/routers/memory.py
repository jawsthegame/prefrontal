"""HTTP routes tagged "memory".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter

from prefrontal.webhooks._common import (
    Annotated,
    Depends,
    PlainTextResponse,
    Query,
    Response,
    ScopedRequest,
    build_profile,
    cache_is_stale,
    load_cached_summary,
    refresh_profile_cache,
    resolve_user,
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
    """Build the "memory" APIRouter (shared services injected by create_app)."""
    router = APIRouter()

    @router.get("/profile", response_class=PlainTextResponse, tags=["memory"])
    def profile(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        response: Response,
        refresh: Annotated[
            bool,
            Query(description="Regenerate the narrative via Ollama now and cache it."),
        ] = False,
        fmt: Annotated[
            Literal["narrative", "structured"],
            Query(alias="format", description="'narrative' (cached prose) or 'structured'."),
        ] = "narrative",
    ) -> str:
        """Return the behavioral profile, preferring the cached LLM narrative.

        By default this serves the prioritized coaching prose produced by
        ``prefrontal summarize`` (cached in ``profile_cache``), so n8n can fetch
        it on every reminder/briefing without paying for a model round-trip. The
        ``X-Profile-*`` response headers report provenance (``Source``,
        ``Model``, ``Generated-At``) and whether the cache has gone ``Stale``
        relative to the current facts.

        Query params:

        - ``refresh=1`` — regenerate the narrative via Ollama now and cache it
          (falls back to the structured profile if the model is down).
        - ``format=structured`` — return the raw structured profile (the model's
          input), bypassing the cache. This is the old behavior.

        When nothing is cached yet (and ``refresh`` was not requested), it falls
        back to the structured profile so the endpoint always returns something.
        Auth-guarded like the webhooks.
        """
        memory = ctx.store

        if fmt == "structured":
            response.headers["X-Profile-Source"] = "structured"
            return build_profile(memory)

        if refresh:
            summary = refresh_profile_cache(memory, client=summarizer_client)
        else:
            summary = load_cached_summary(memory)

        if summary is None:
            # Nothing summarized yet — serve the structured profile rather than
            # an empty body, and flag that no narrative has been cached.
            response.headers["X-Profile-Source"] = "uncached"
            return build_profile(memory)

        response.headers["X-Profile-Source"] = summary.source
        if summary.model:
            response.headers["X-Profile-Model"] = summary.model
        if summary.generated_at:
            response.headers["X-Profile-Generated-At"] = summary.generated_at
        response.headers["X-Profile-Stale"] = (
            "true" if cache_is_stale(memory, summary) else "false"
        )
        return summary.text

    return router
