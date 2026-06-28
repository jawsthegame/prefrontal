"""FastAPI webhook listener for iOS Shortcut and n8n triggers.

This is Prefrontal's ingestion surface. iOS Shortcuts (and anything else that can
make an HTTP request) POST low-friction outcome reports here, and the listener
turns them into rows in the behavioral memory layer.

Routes:

- ``GET  /health`` — liveness probe, no auth.
- ``POST /webhooks/shortcut`` — one-tap outcome logging from an iOS Shortcut.
- ``POST /webhooks/n8n`` — inbound events pushed by an n8n workflow.

Authentication is a shared secret in the ``X-Prefrontal-Token`` header, checked
against :attr:`prefrontal.config.Settings.webhook_secret`. iOS Shortcuts can set
custom headers, so this stays low-friction. If no secret is configured, auth is
disabled — only appropriate on a fully trusted local network.

The app is built by :func:`create_app`, a factory so tests can inject an
in-memory store. Importing :data:`app` builds a default instance from the
environment for ``uvicorn prefrontal.webhooks.app:app`` and the ``prefrontal
serve`` CLI command.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from prefrontal.config import Settings, get_settings
from prefrontal.integrations.n8n import N8nClient, parse_inbound_event
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore

#: Maps a one-tap shortcut action to the resulting ``episodes.outcome`` value.
ACTION_OUTCOME: dict[str, str] = {
    "made_it": "success",
    "missed_it": "miss",
    "partial": "partial",
}


class ShortcutPayload(BaseModel):
    """Body of a ``POST /webhooks/shortcut`` request.

    Modeled on a one-tap iOS Shortcut: the only required field is ``action``.
    Everything else is optional context the Shortcut can attach if available.
    """

    action: Literal["made_it", "missed_it", "partial", "log"] = Field(
        description="One-tap outcome. Use 'log' to supply an explicit `outcome`.",
    )
    episode_type: Literal["departure", "task", "checkin", "reminder"] = Field(
        default="departure",
        description="What kind of interaction this outcome is about.",
    )
    predicted_value: float | None = Field(default=None, description="What the agent estimated.")
    actual_value: float | None = Field(default=None, description="What actually happened.")
    acknowledged: bool | None = Field(
        default=None, description="Whether the user responded to the trigger."
    )
    channel: str | None = Field(default=None, description="notification | sound | tts | sms.")
    context: str | None = Field(default=None, description="Free-text context.")
    outcome: str | None = Field(
        default=None, description="Explicit outcome; required when action='log'."
    )
    notes: str | None = Field(default=None, description="Optional annotation.")


class EpisodeCreated(BaseModel):
    """Response returned after an episode is logged."""

    episode_id: int
    outcome: str
    n8n_delivered: bool


def get_store(request: Request) -> MemoryStore:
    """FastAPI dependency returning the app's memory store.

    Defined at module level (not as a closure) so that, with
    ``from __future__ import annotations`` in effect, FastAPI can resolve the
    ``Depends(get_store)`` annotation via ``get_type_hints``.

    Args:
        request: The incoming request, used to reach ``app.state.store``.

    Returns:
        The process-wide :class:`MemoryStore`.
    """
    return request.app.state.store


def _verify_token(settings: Settings, provided: str | None) -> None:
    """Enforce the shared-secret header, raising HTTP 401 on mismatch.

    Args:
        settings: Active settings (provides the expected secret).
        provided: The value of the ``X-Prefrontal-Token`` header, if any.

    Raises:
        HTTPException: 401 if auth is enabled and the token is missing or wrong.
    """
    if not settings.auth_enabled:
        return
    if provided != settings.webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Prefrontal-Token header.",
        )


def create_app(
    *, store: MemoryStore | None = None, settings: Settings | None = None
) -> FastAPI:
    """Build and return the Prefrontal webhook application.

    Args:
        store: An optional pre-built :class:`MemoryStore`. When provided (e.g. by
            tests with an in-memory database) it is used as-is and its lifecycle
            is the caller's responsibility. When omitted, the app opens and
            initializes a store from ``settings.db_path`` on startup and closes
            it on shutdown.
        settings: Optional settings override. Defaults to :func:`get_settings`.

    Returns:
        A configured :class:`fastapi.FastAPI` instance.
    """
    resolved_settings = settings or get_settings()
    n8n = N8nClient(
        webhook_url=resolved_settings.n8n_webhook_url,
        token=resolved_settings.n8n_webhook_token,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Open the memory store on startup; close it on shutdown if we own it."""
        owns_store = store is None
        active_store = store or MemoryStore(init_db(resolved_settings.db_path))
        app.state.store = active_store
        app.state.settings = resolved_settings
        app.state.n8n = n8n
        try:
            yield
        finally:
            if owns_store:
                active_store.conn.close()

    app = FastAPI(
        title="Prefrontal Webhooks",
        version="0.1.0",
        summary="Low-friction ingestion for iOS Shortcut and n8n triggers.",
        lifespan=lifespan,
    )

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        """Liveness probe. Returns ``{"status": "ok"}`` with no auth required."""
        return {"status": "ok", "service": "prefrontal", "version": app.version}

    @app.post(
        "/webhooks/shortcut",
        response_model=EpisodeCreated,
        status_code=status.HTTP_201_CREATED,
        tags=["ingestion"],
    )
    def shortcut(
        payload: ShortcutPayload,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> EpisodeCreated:
        """Log a one-tap outcome from an iOS Shortcut.

        Maps ``made_it``/``missed_it``/``partial`` to the corresponding episode
        ``outcome``; ``log`` uses the explicit ``outcome`` field. Best-effort
        fires an ``episode.logged`` event to n8n (a no-op unless configured).
        """
        _verify_token(resolved_settings, x_prefrontal_token)

        if payload.action == "log":
            if not payload.outcome:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="action='log' requires an explicit 'outcome'.",
                )
            outcome = payload.outcome
        else:
            outcome = ACTION_OUTCOME[payload.action]

        # Sensible default for acknowledgement when the Shortcut doesn't say:
        # a one-tap report means the user engaged, so success/partial imply ack.
        acknowledged = payload.acknowledged
        if acknowledged is None and payload.action in ("made_it", "partial"):
            acknowledged = True

        episode_id = memory.log_episode(
            payload.episode_type,
            predicted_value=payload.predicted_value,
            actual_value=payload.actual_value,
            acknowledged=acknowledged,
            channel=payload.channel,
            context=payload.context,
            outcome=outcome,
            notes=payload.notes,
        )

        result = app.state.n8n.trigger(
            "episode.logged",
            {"episode_id": episode_id, "episode_type": payload.episode_type, "outcome": outcome},
        )
        return EpisodeCreated(
            episode_id=episode_id, outcome=outcome, n8n_delivered=result.delivered
        )

    @app.post("/webhooks/n8n", tags=["ingestion"])
    async def n8n_inbound(
        request: Request,
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Receive an event pushed by an n8n workflow.

        Currently classifies the payload via
        :func:`prefrontal.integrations.n8n.parse_inbound_event` and echoes the
        routing decision. Concrete per-event handlers are a documented TODO.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {"value": body}
        return parse_inbound_event(body)

    return app


#: Default application instance for ``uvicorn prefrontal.webhooks.app:app``.
app = create_app()
