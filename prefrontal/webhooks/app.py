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
- ``POST /webhooks/calendar/sync`` — n8n syncs upcoming calendar events.
- ``GET  /commitments`` / ``POST /commitments`` — list / manually add a commitment.
- ``GET  /commitments/conflicts`` — double-bookings among upcoming commitments.

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

from prefrontal.commitments import find_conflicts, normalize_event, sync_calendar
from prefrontal.config import Settings, get_settings
from prefrontal.impact import (
    analyze_impact,
    at_risk,
    impact_phrase,
    project_free_time,
)
from prefrontal.integrations.n8n import N8nClient, parse_inbound_event
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.memory.summarizer import build_profile
from prefrontal.modules.location_anchor import (
    DEFAULT_ABANDON_RATIO,
    DEFAULT_HOME_RADIUS_M,
    build_message,
    escalation_level,
    haversine_m,
    is_abandoned,
    is_at_home,
    level_rank,
    parse_time_window,
    record_outing_abandoned,
    record_outing_return,
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


class CalendarEvent(BaseModel):
    """One event in a ``POST /webhooks/calendar/sync`` batch."""

    title: str = Field(description="Event title.")
    start_at: str = Field(description="ISO-8601 start (offset-aware or UTC).")
    external_id: str | None = Field(default=None, description="Calendar event id.")
    end_at: str | None = Field(default=None, description="ISO-8601 end.")
    location: str | None = Field(default=None, description="Event location.")
    lead_minutes: float | None = Field(
        default=None, description="Travel+prep buffer before start (default 10)."
    )
    hard: bool = Field(default=False, description="A hard deadline vs a soft one.")


class CalendarSync(BaseModel):
    """Body of ``POST /webhooks/calendar/sync`` — a full upcoming-events batch."""

    events: list[CalendarEvent] = Field(default_factory=list)


class CommitmentCreate(BaseModel):
    """Body of ``POST /commitments`` — a single manual commitment."""

    title: str
    start_at: str = Field(description="ISO-8601 start (offset-aware or UTC).")
    end_at: str | None = None
    location: str | None = None
    lead_minutes: float | None = None
    hard: bool = False


def _state_float(memory: MemoryStore, key: str, default: float) -> float:
    """Read a coaching-state value as a float, falling back on missing/bad data."""
    raw = memory.get_state(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


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

        n8n polls this on a schedule. For each active outing:

        - If the body carries ``current_lat``/``current_lon`` and the user is
          within the home radius, the outing is **passively closed** as returned
          (location confirms the return) and no nudge fires — so a forgotten
          "I'm back" tap, or coming home early, never triggers a call.
        - Otherwise, if elapsed time has blown past the abandon ratio, the outing
          is **auto-closed** as abandoned so it stops lingering active.
        - Otherwise the elapsed-time escalation level is computed; when a *new*
          level is crossed since the last poll it sets ``fire=true``, records the
          level so it fires once, and includes the ``message``.

        For an active outing, **impact analysis** runs: the realistic free-time
        is projected from the stated window and the learned time bias, and any
        upcoming commitments that would be at risk are listed (and named in the
        nudge message). Each returned item reports its post-check ``status``
        (``active``/``returned``/``abandoned``); n8n acts on ``fire == true``.
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
        home_radius = _state_float(memory, "home_radius_m", DEFAULT_HOME_RADIUS_M)
        abandon_ratio = _state_float(memory, "abandon_after_ratio", DEFAULT_ABANDON_RATIO)
        bias = _state_float(memory, "time_estimation_bias", 1.0)
        commitments = memory.upcoming_commitments()

        results: list[dict[str, Any]] = []
        for outing in memory.active_outings():
            elapsed = outing["elapsed_minutes"] or 0.0
            window = outing["time_window_minutes"]

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
            at_home = is_at_home(distance_m, home_radius) if distance_m is not None else None

            level, fire, message, outcome = "none", False, "", None
            impacts: list[dict[str, Any]] = []
            if at_home:
                # Location confirms the user is home — passive return.
                closed = memory.close_outing(outing["id"], status="returned")
                outcome = record_outing_return(memory, closed)["outcome"]
                outing_status = "returned"
            elif is_abandoned(elapsed, window, abandon_ratio):
                closed = memory.close_outing(outing["id"], status="abandoned")
                outcome = record_outing_abandoned(memory, closed)["outcome"]
                outing_status = "abandoned"
            else:
                level = escalation_level(elapsed, window)
                fire = level_rank(level) > level_rank(outing["last_level"])
                # Impact analysis: project realistic free-time from the bias and
                # see which upcoming commitments are now at risk.
                risky = []
                if commitments:
                    projected = project_free_time(outing["departure_at"], window, bias)
                    risky = at_risk(analyze_impact(projected, commitments))
                    impacts = [
                        {
                            "commitment_id": i.commitment["id"],
                            "title": i.commitment["title"],
                            "start_at": i.commitment["start_at"],
                            "slack_minutes": i.slack_minutes,
                            "hardness": i.commitment.get("hardness"),
                        }
                        for i in risky
                    ]
                if fire:
                    memory.set_outing_level(outing["id"], level)
                    message = build_message(
                        level, elapsed_minutes=elapsed, window_minutes=window, name=name
                    ) + impact_phrase(risky)
                outing_status = "active"

            results.append(
                {
                    "outing_id": outing["id"],
                    "intention": outing["intention"],
                    "elapsed_minutes": round(elapsed, 1),
                    "time_window_minutes": window,
                    "level": level,
                    "fire": fire,
                    "message": message,
                    "distance_m": distance_m,
                    "at_home": at_home,
                    "status": outing_status,
                    "outcome": outcome,
                    "impact": impacts,
                    "hard_conflict": any(i["hardness"] == "hard" for i in impacts),
                }
            )
        return {"active": results}

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

        if payload.status == "returned":
            recorded = record_outing_return(memory, closed)
        else:
            recorded = record_outing_abandoned(memory, closed)
        actual = closed.get("actual_minutes")
        return {
            "outing_id": outing_id,
            "status": payload.status,
            "actual_minutes": round(actual, 1) if actual is not None else None,
            "time_window_minutes": closed.get("time_window_minutes"),
            "outcome": recorded["outcome"],
            "episode_id": recorded["episode_id"],
        }

    # -- Commitments (schedule for impact analysis) --------------------------

    @app.post("/webhooks/calendar/sync", tags=["schedule"])
    def calendar_sync(
        payload: CalendarSync,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Sync a batch of upcoming calendar events into ``commitments``.

        n8n posts the current window of events on a schedule; this upserts them
        (by ``external_id``) and prunes calendar events that disappeared. A bad
        timestamp rejects the whole batch with 422 (never partially applies).
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        try:
            summary = sync_calendar(
                memory, [e.model_dump() for e in payload.events]
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        return {
            "added": summary.added,
            "updated": summary.updated,
            "cancelled": summary.cancelled,
            "upcoming": summary.upcoming,
            "conflicts": summary.conflicts,
        }

    @app.post(
        "/commitments", status_code=status.HTTP_201_CREATED, tags=["schedule"]
    )
    def commitment_create(
        payload: CommitmentCreate,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Add a single commitment manually (source ``manual``)."""
        _verify_token(resolved_settings, x_prefrontal_token)
        try:
            fields = normalize_event({**payload.model_dump(), "source": "manual"})
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        commitment_id, _ = memory.upsert_commitment(**fields)
        return {"commitment_id": commitment_id}

    @app.get("/commitments", tags=["schedule"])
    def commitments_list(
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """List active upcoming commitments, soonest first."""
        _verify_token(resolved_settings, x_prefrontal_token)
        return {"commitments": memory.upcoming_commitments()}

    @app.get("/commitments/conflicts", tags=["schedule"])
    def commitments_conflicts(
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Report double-bookings among upcoming commitments.

        Returns overlapping pairs (most useful across merged personal + work
        calendars). n8n can poll this and push a double-booking alert.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        conflicts = find_conflicts(memory.upcoming_commitments())
        return {
            "conflicts": [
                {
                    "a": {"id": c.a["id"], "title": c.a["title"], "start_at": c.a["start_at"]},
                    "b": {"id": c.b["id"], "title": c.b["title"], "start_at": c.b["start_at"]},
                    "overlap_minutes": c.overlap_minutes,
                }
                for c in conflicts
            ]
        }

    return app


#: Default application instance for ``uvicorn prefrontal.webhooks.app:app``.
app = create_app()
