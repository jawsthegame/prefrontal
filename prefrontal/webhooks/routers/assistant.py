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
from prefrontal.clock import utcnow
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.schemas import (
    AssistantApply,
    AssistantMessage,
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
        results = execute_actions(
            memory, actions, timezone=resolved_settings.timezone
        )
        applied = sum(1 for r in results if r["ok"])
        return {"applied": applied, "results": results, "errors": errors}

    return router
