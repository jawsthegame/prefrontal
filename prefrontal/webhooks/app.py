"""FastAPI webhook listener for iOS Shortcut and n8n triggers.

This is Prefrontal's ingestion surface. iOS Shortcuts (and anything else that can
make an HTTP request) POST low-friction outcome reports here, and the listener
turns them into rows in the behavioral memory layer.

Routes:

- ``GET  /health`` — liveness probe, no auth.
- ``GET  /profile`` — the current behavioral profile (for n8n to feed Ollama).
- ``POST /webhooks/shortcut`` — one-tap outcome logging from an iOS Shortcut.
- ``POST /webhooks/n8n`` — inbound events pushed by an n8n workflow.
- ``POST /webhooks/outing/start`` — declare an intention + time window.
- ``POST /webhooks/outing/check`` — n8n polls this for due escalation nudges.
- ``POST /webhooks/outing/return`` — close an outing; logs intention vs actual.

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
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from prefrontal.config import Settings, get_settings
from prefrontal.integrations.n8n import N8nClient, parse_inbound_event
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.memory.summarizer import build_profile
from prefrontal.modules.location_anchor import (
    build_message,
    escalation_level,
    haversine_m,
    level_rank,
    parse_time_window,
)

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


class OutingStart(BaseModel):
    """Body of ``POST /webhooks/outing/start`` — declaring an intention."""

    intention: str = Field(description="The stated mission, e.g. 'getting coffee'.")
    time_window_minutes: float | None = Field(
        default=None,
        description="Stated window. If omitted, parsed from the intention text.",
    )
    home_lat: float | None = Field(default=None, description="Baseline latitude.")
    home_lon: float | None = Field(default=None, description="Baseline longitude.")


class OutingStarted(BaseModel):
    """Response after an outing is started."""

    outing_id: int
    intention: str
    time_window_minutes: float


class OutingReturn(BaseModel):
    """Body of ``POST /webhooks/outing/return`` — closing an outing."""

    outing_id: int | None = Field(
        default=None,
        description="Outing to close. Defaults to the most recent active outing.",
    )
    status: Literal["returned", "abandoned"] = "returned"


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

    @app.get("/profile", response_class=PlainTextResponse, tags=["memory"])
    def profile(
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> str:
        """Return the current behavioral profile as Markdown.

        This is the HTTP equivalent of ``prefrontal profile``. n8n fetches it to
        prepend to an Ollama (or Anthropic) prompt so behavioral context travels
        with every generated reminder/briefing. Auth-guarded like the webhooks.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        return build_profile(memory)

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

    # -- Location-Aware Task Anchor (Coffee Shop Nudge) ----------------------

    @app.post(
        "/webhooks/outing/start",
        response_model=OutingStarted,
        status_code=status.HTTP_201_CREATED,
        tags=["anchor"],
    )
    def outing_start(
        payload: OutingStart,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> OutingStarted:
        """Declare an outing: a stated intention and a time window.

        The window is taken from ``time_window_minutes`` if given, otherwise
        parsed from the intention text ("back in 15 minutes"). If neither yields
        a window, responds 422 so the caller can ask explicitly.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        window = payload.time_window_minutes
        if window is None:
            window = parse_time_window(payload.intention)
        if window is None or window <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Could not determine a time window. Include "
                    "'time_window_minutes' or phrase it like 'back in 15 minutes'."
                ),
            )
        outing_id = memory.start_outing(
            payload.intention,
            window,
            home_lat=payload.home_lat,
            home_lon=payload.home_lon,
        )
        return OutingStarted(
            outing_id=outing_id, intention=payload.intention, time_window_minutes=window
        )

    @app.post("/webhooks/outing/check", tags=["anchor"])
    async def outing_check(
        request: Request,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Evaluate active outings and report which nudges are due.

        n8n polls this on a schedule. For each active outing it computes the
        elapsed-time escalation level; when a *new* level has been crossed since
        the last poll it sets ``fire=true``, records the level so it fires once,
        and includes the user-facing ``message``. The body may optionally carry
        ``current_lat``/``current_lon`` to annotate each outing with
        ``distance_m`` from home (not used to gate nudges in v1).
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        cur_lat = body.get("current_lat")
        cur_lon = body.get("current_lon")
        name = memory.get_state("user_name", "") or ""

        active: list[dict[str, Any]] = []
        for outing in memory.active_outings():
            elapsed = outing["elapsed_minutes"] or 0.0
            window = outing["time_window_minutes"]
            level = escalation_level(elapsed, window)
            fire = level_rank(level) > level_rank(outing["last_level"])
            if fire:
                memory.set_outing_level(outing["id"], level)
            distance_m = None
            if (
                cur_lat is not None
                and cur_lon is not None
                and outing["home_lat"] is not None
                and outing["home_lon"] is not None
            ):
                distance_m = round(
                    haversine_m(outing["home_lat"], outing["home_lon"], cur_lat, cur_lon)
                )
            active.append(
                {
                    "outing_id": outing["id"],
                    "intention": outing["intention"],
                    "elapsed_minutes": round(elapsed, 1),
                    "time_window_minutes": window,
                    "level": level,
                    "fire": fire,
                    "message": build_message(
                        level,
                        elapsed_minutes=elapsed,
                        window_minutes=window,
                        name=name,
                    )
                    if fire
                    else "",
                    "distance_m": distance_m,
                }
            )
        return {"active": active}

    @app.post("/webhooks/outing/return", tags=["anchor"])
    def outing_return(
        payload: OutingReturn,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Close an outing and log intention-vs-actual for pattern tracking.

        Records the actual time out as a ``task`` episode (predicted = stated
        window, actual = minutes out, outcome = ``success`` if within the window
        else ``miss``) so the time-blindness learning can use it later.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        outing_id = payload.outing_id
        if outing_id is None:
            recent = memory.most_recent_active_outing()
            if recent is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No active outing to return.",
                )
            outing_id = recent["id"]

        closed = memory.close_outing(outing_id, status=payload.status)
        if closed is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Outing {outing_id} is not active.",
            )

        actual = closed.get("actual_minutes")
        window = closed.get("time_window_minutes")
        outcome = None
        episode_id = None
        if payload.status == "returned" and actual is not None:
            outcome = "success" if actual <= window else "miss"
            episode_id = memory.log_episode(
                "task",
                predicted_value=window,
                actual_value=round(actual, 1),
                acknowledged=True,
                context=f"outing: {closed.get('intention')}",
                outcome=outcome,
            )
        return {
            "outing_id": outing_id,
            "status": payload.status,
            "actual_minutes": round(actual, 1) if actual is not None else None,
            "time_window_minutes": window,
            "outcome": outcome,
            "episode_id": episode_id,
        }

    return app


#: Default application instance for ``uvicorn prefrontal.webhooks.app:app``.
app = create_app()
