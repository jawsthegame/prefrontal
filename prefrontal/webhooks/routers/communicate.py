"""HTTP routes tagged "communicate" — communication translation (roadmap M4).

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`. Exposes the
text-only, side-effect-free communication tool: decode a received message, draft
a reply in a register, or soften a message the user wrote. It writes nothing and
sends nothing — the "does the thing" side of M4 (autonomous, confirmed actions)
lives elsewhere; this is the ready-now, zero-risk half.
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

from prefrontal.communication_translation import translate
from prefrontal.webhooks.deps import (
    ScopedRequest,
    resolve_user,
)
from prefrontal.webhooks.schemas import TranslateMessage
from prefrontal.webhooks.services import RouterServices


def build_router(services: RouterServices) -> APIRouter:
    """Build the "communicate" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    provider = services.provider

    @router.post("/communicate/translate", tags=["communicate"])
    def communicate_translate(
        payload: TranslateMessage,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Decode, draft, or soften a work message — text only, nothing sent.

        Three modes (``mode``): **decode** explains what a received message really
        means (the ask, the subtext, the urgency); **draft** writes a reply from a
        short description; **soften** rewrites the user's own message in a chosen
        ``register``. The response is writing to read/copy/edit — this endpoint
        never sends, books, or stores anything.

        Uses Claude when the ``assistant`` agent is configured for the Anthropic
        provider and a key is available, otherwise the local Ollama model. When no
        model is reachable, decode/draft return an honest "model unavailable"
        note (``offline: true``) rather than a fabricated result; soften degrades
        to the user's own text with a register note.
        """
        client, provider_name = provider.select("assistant")
        result = translate(
            payload.text,
            payload.mode,
            payload.register_,
            client=client,
        )
        return {
            "mode": result.mode,
            "register": result.register,
            "output": result.output,
            "note": result.note,
            "offline": result.offline,
            "provider": provider_name,
        }

    return router
