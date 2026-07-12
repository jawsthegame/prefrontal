"""HTTP routes tagged "assistant" — natural-language edits to todos/commitments.

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from typing import (
    Annotated,
    Any,
)

from fastapi import (
    APIRouter,
    Depends,
)

from prefrontal.assistant import (
    build_snapshot,
    execute_actions,
    validate_actions,
)
from prefrontal.assistant import (
    plan as assistant_plan_message,
)
from prefrontal.availability import plan_availability, render_plan
from prefrontal.clock import local_datetime, parse_ts, utcnow
from prefrontal.scheduling import window_config_for
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.schemas import (
    AssistantApply,
    AssistantMessage,
    FindTimeMessage,
)
from prefrontal.webhooks.services import RouterServices


def build_router(services: RouterServices) -> APIRouter:
    """Build the "assistant" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    resolved_settings = services.settings
    provider = services.provider

    @router.post("/assistant", tags=["assistant"])
    def assistant_interpret(
        payload: AssistantMessage,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Turn a natural-language message into a **proposed** action list.

        Nothing is written. The response's ``actions`` are validated against the
        caller's current data (ids resolved, ops whitelisted); the client shows
        them and, on the user's confirmation, echoes them back to
        ``POST /assistant/apply``. ``errors`` explains anything that couldn't be
        turned into a supported edit.

        Uses Claude when the ``assistant`` agent is configured for the Anthropic
        provider and a key is available, otherwise the local Ollama model. A
        message that can't be provided at all yields 503.
        """
        client, provider_name = provider.select("assistant")
        result = assistant_plan_message(
            payload.message,
            ctx.store,
            client=client,
            now=utcnow(),
            tz=resolved_settings.timezone,
        )
        return {
            "reply": result.reply,
            "actions": [a.to_wire() for a in result.actions],
            "errors": result.errors,
            "provider": provider_name,
        }

    @router.post("/assistant/apply", tags=["assistant"])
    def assistant_apply(
        payload: AssistantApply,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Execute previously-proposed actions after re-validating them.

        The actions are re-checked against the *current* store (ids may have
        moved since they were proposed) before executing, and the scoped store
        re-verifies ownership on every write — so this can only ever touch the
        caller's own rows, and a stale action reports "nothing changed" rather
        than acting on the wrong item.
        """
        memory = ctx.store
        snapshot = build_snapshot(memory)
        actions, errors = validate_actions(payload.actions, snapshot)
        # A delegate_todo action writes its prep brief with the model; hand the same
        # provider-selected client the interpret step used (falls back to a heuristic
        # brief when unavailable). Other ops ignore it.
        client, _ = provider.select("assistant")
        results = execute_actions(
            memory, actions, timezone=resolved_settings.timezone, client=client
        )
        applied = sum(1 for r in results if r["ok"])
        return {"applied": applied, "results": results, "errors": errors}

    @router.post("/assistant/find-time", tags=["assistant"])
    def assistant_find_time(
        payload: FindTimeMessage,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Find open calendar slots from a free-text ask — read-only.

        "Find 45 minutes for coffee with Sam this week", or "when are my wife and
        I both free for dinner tomorrow evening?" The message is parsed into a
        duration + timeframe + who's-involved (Claude when the ``assistant`` agent
        is configured for Anthropic, else the local Ollama model, else an offline
        heuristic), and open windows are computed from the calendar.

        **Who's involved shapes the constraints.** Your own commitments always
        block. A partner's whereabouts — the *FYI* events (where someone else will
        be) — block **only** when the plan involves them: "just me" ignores items
        that are only your partner's, while "the two of us" treats their calendar
        as a hard constraint too. ``ignored_fyi`` reports how many FYI items were
        left out so the client can offer to include them.

        When the ask is too vague to answer (almost always a missing duration),
        ``question`` is set and ``slots`` is empty — the caller should put the
        question back to the user and re-ask. Nothing is written either way.
        """
        client, provider_name = provider.select("assistant")
        memory = ctx.store
        tz = resolved_settings.timezone
        awake = window_config_for(resolved_settings, memory).awake_band()
        plan = plan_availability(
            payload.message,
            memory,
            client=client,
            now=utcnow(),
            tz=tz,
            awake_band=awake,
        )

        def dump_slot(w: Any) -> dict[str, Any]:
            start_local = local_datetime(parse_ts(w.start), tz)
            end_local = local_datetime(parse_ts(w.end), tz)
            return {
                "start_at": w.start,
                "end_at": w.end,
                "minutes": w.minutes,
                "day": start_local.strftime("%a %b %-d"),
                "start": start_local.strftime("%-I:%M %p"),
                "end": end_local.strftime("%-I:%M %p"),
            }

        request = plan.request
        return {
            "question": plan.question,
            "request": (
                {
                    "minutes": request.minutes,
                    "days": request.days,
                    "with_partner": request.with_partner,
                    "title": request.title,
                    "time_window": list(request.time_window) if request.time_window else None,
                }
                if request is not None
                else None
            ),
            "slots": [dump_slot(w) for w in plan.slots],
            "with_partner": plan.with_partner,
            "considered": plan.considered,
            "ignored_fyi": plan.ignored_fyi,
            "text": render_plan(plan, tz),
            "provider": provider_name,
        }

    return router
