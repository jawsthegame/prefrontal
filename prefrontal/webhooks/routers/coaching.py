"""HTTP routes tagged "coaching".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`. Exposes the
coaching agent's tick endpoint (``docs/coaching-agent.md`` §7) — the sibling of
``/webhooks/outing/check`` that fans over *every* enabled module.
"""
from __future__ import annotations

from prefrontal.coaching import build_context, collect_cues, decide, record_fired
from prefrontal.modules import enabled_modules
from prefrontal.webhooks._common import (
    Annotated,
    Any,
    Depends,
    Request,
    ScopedRequest,
    resolve_user,
    utcnow,
)


def build_router(
    *,
    resolved_settings,
    n8n,
    ollama_client,
    summarizer_client,
    geocoder_client,
    _run_geocode,
):
    """Build the "coaching" APIRouter (shared services injected by create_app)."""
    from fastapi import APIRouter

    router = APIRouter()

    @router.post("/webhooks/coach/check", tags=["coaching"])
    async def coach_check(
        request: Request,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Run one coaching tick: the cues due right now, each with a channel.

        n8n polls this like ``/webhooks/outing/check``, but it fans over *every*
        enabled module's :meth:`~prefrontal.modules.base.Module.evaluate` rather
        than being hard-wired to one. For each cue it applies channel choice and
        suppression (quiet hours + per-``dedup_key`` debounce), returns the
        fire-worthy decisions, and stamps each as fired so a standing cue won't
        repeat next poll (the general form of the anchor's fire-once guard).

        Body (optional): ``current_lat``/``current_lon`` ride into the
        :class:`~prefrontal.coaching.CoachContext` so location-aware evaluators
        keep working. Response: ``{"cues": [{module, intervention, urgency,
        channel, fire, text, context_key, dedup_key, ref}]}`` — n8n delivers each
        ``text`` on its ``channel``.
        """
        memory = ctx.store
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        now = utcnow()
        coach_ctx = build_context(
            memory,
            now=now,
            timezone=resolved_settings.timezone,
            current_lat=body.get("current_lat"),
            current_lon=body.get("current_lon"),
        )
        modules = enabled_modules(resolved_settings)
        cues = collect_cues(memory, modules, coach_ctx)
        decisions = decide(memory, cues, coach_ctx)
        # Recorded at check time (not on confirmed delivery), matching how
        # outing/check advances last_level when it decides to fire.
        record_fired(memory, decisions, now)
        return {
            "cues": [
                {
                    "module": d.cue.module,
                    "intervention": d.cue.intervention,
                    "urgency": d.cue.urgency,
                    "channel": d.channel,
                    "fire": True,
                    "text": d.text,
                    "context_key": d.cue.context_key,
                    "dedup_key": d.cue.dedup_key,
                    "ref": d.cue.ref,
                }
                for d in decisions
            ]
        }

    return router
