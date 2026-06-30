"""FastAPI webhook listener for iOS Shortcut and n8n triggers.

This is Prefrontal's ingestion surface. iOS Shortcuts (and anything else that can
make an HTTP request) POST low-friction outcome reports here, and the listener
turns them into rows in the behavioral memory layer.

Routes:

- ``GET  /health`` — liveness probe, no auth.
- ``GET  /profile`` — the current behavioral profile (for n8n to feed Ollama).
- ``POST /webhooks/shortcut`` — one-tap outcome logging from an iOS Shortcut.
- ``POST /webhooks/n8n`` — inbound events pushed by an n8n workflow.
- ``POST /webhooks/location`` — the phone's current position (iOS Shortcut).
- ``GET  /location`` — the last-known position, for debugging/monitoring.
- ``POST /webhooks/mail/sync`` — ingest + triage a batch of mail for an account.
- ``GET  /mail`` — recent triaged mail and the open action items.
- ``POST /webhooks/outing/start`` — declare an intention + time window.
- ``POST /webhooks/outing/check`` — n8n polls this for due escalation nudges.
- ``POST /webhooks/outing/return`` — close an outing; logs intention vs actual.
- ``POST /webhooks/focus/start`` — declare a focus session (task + optional plan).
- ``POST /webhooks/focus/check`` — n8n polls this for due interrupts + protect state.
- ``POST /webhooks/focus/end`` — close a session; logs planned vs actual.
- ``GET  /focus`` — read-only snapshot of focus sessions (active + recent).
- ``POST /webhooks/calendar/sync`` — n8n syncs upcoming calendar events.
- ``POST /webhooks/departure/check`` — n8n polls this for due departure nudges.
- ``GET  /commitments`` / ``POST /commitments`` — list / manually add a commitment.
- ``POST /commitments/geocode`` — backfill destination coords for commitments.
- ``GET  /commitments/conflicts`` — double-bookings among upcoming commitments.
- ``GET  /places`` / ``POST /places`` — curated destination aliases for geocoding.
- ``GET  /briefing`` — today's morning digest (commitments, conflicts, slips).
- ``GET/POST /todos`` (+ ``/todos/{id}/done|drop``) — open loops to fit into time.
- ``GET  /todos/fit?minutes=N`` — open todos that fit a free block right now.
- ``GET  /dashboard`` — read-only monitoring UI (self-contained HTML shell).
- ``GET  /family`` — calm, non-technical family view (right now + today).

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
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from prefrontal.briefing import build_briefing, render_briefing
from prefrontal.commitments import (
    conflict_dismissal_key,
    find_conflicts,
    normalize_event,
    partition_conflicts,
    sync_calendar,
    to_utc,
)
from prefrontal.config import Settings, get_settings
from prefrontal.departure import (
    DEFAULT_HEADS_UP_MINUTES,
    DEFAULT_PREP_MINUTES,
    DEFAULT_ROAD_FACTOR,
    DEFAULT_SOON_MINUTES,
    DEFAULT_TRAVEL_SPEED_KMH,
    build_departure_message,
    next_departure,
    plan_departure,
)
from prefrontal.geocode import enrich_commitments, normalize_query
from prefrontal.impact import (
    analyze_impact,
    at_risk,
    impact_phrase,
    project_free_time,
    utcnow,
)
from prefrontal.integrations.n8n import N8nClient, parse_inbound_event
from prefrontal.integrations.nominatim import NominatimGeocoder
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.mail import ingest_messages
from prefrontal.memory.store import MemoryStore
from prefrontal.memory.summarizer import build_profile
from prefrontal.modules.hyperfocus import (
    DEFAULT_FOCUS_ABANDON_RATIO,
    DEFAULT_HARD_INTERRUPT_MINUTES,
    DEFAULT_SOFT_BLOCK_MINUTES,
    build_focus_message,
    focus_level,
    record_focus_abandoned,
    record_focus_end,
    should_protect,
)
from prefrontal.modules.hyperfocus import is_abandoned as focus_is_abandoned
from prefrontal.modules.hyperfocus import level_rank as focus_level_rank
from prefrontal.modules.location_anchor import (
    DEFAULT_ABANDON_RATIO,
    DEFAULT_HOME_RADIUS_M,
    build_message,
    escalation_level,
    haversine_m,
    infer_time_window,
    is_abandoned,
    is_at_home,
    level_rank,
    parse_time_window,
    record_outing_abandoned,
    record_outing_return,
)
from prefrontal.scheduling import fit_todos
from prefrontal.todos import (
    DEFAULT_MAX_FIRST_STEP_MINUTES,
    augment_todo,
    avoided_todos,
    decompose_task,
    record_todo_closed,
)

#: Maps a one-tap shortcut action to the resulting ``episodes.outcome`` value.
ACTION_OUTCOME: dict[str, str] = {
    "made_it": "success",
    "missed_it": "miss",
    "partial": "partial",
}

#: The self-contained monitoring page, read once at import (like ``schema.sql``).
DASHBOARD_HTML = (Path(__file__).with_name("dashboard.html")).read_text(encoding="utf-8")
#: The calm, read-only family view (a friendly subset of the dashboard).
FAMILY_HTML = (Path(__file__).with_name("family.html")).read_text(encoding="utf-8")


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
    time_window_source: str = Field(
        default="explicit",
        description=(
            "How the window was determined: 'explicit' (given), 'parsed' (from "
            "the text), 'llm'/'heuristic'/'default' (inferred when none stated)."
        ),
    )


class OutingReturn(BaseModel):
    """Body of ``POST /webhooks/outing/return`` — closing an outing."""

    outing_id: int | None = Field(
        default=None,
        description="Outing to close. Defaults to the most recent active outing.",
    )
    status: Literal["returned", "abandoned"] = "returned"


class FocusStart(BaseModel):
    """Body of ``POST /webhooks/focus/start`` — declaring a focus session."""

    intended_task: str = Field(description="What you're getting into, e.g. 'the API refactor'.")
    planned_minutes: float | None = Field(
        default=None,
        description="Optional intended duration; the point past which a gentle check fires.",
    )
    aligned: bool = Field(
        default=True,
        description="Whether this is the thing you meant to be doing (the protect bit).",
    )


class FocusStarted(BaseModel):
    """Response after a focus session is started."""

    session_id: int
    intended_task: str
    planned_minutes: float | None
    aligned: bool


class FocusEnd(BaseModel):
    """Body of ``POST /webhooks/focus/end`` — closing a focus session."""

    session_id: int | None = Field(
        default=None,
        description="Session to close. Defaults to the most recent active session.",
    )
    status: Literal["ended", "abandoned"] = "ended"
    outcome: Literal["worth_it", "should_have_stopped", "pulled_off"] | None = Field(
        default=None, description="Optional one-tap rating of how the block went."
    )
    breadcrumb: str | None = Field(
        default=None, description="Optional 'where I was / next step' note for cheap re-entry."
    )


class CalendarEvent(BaseModel):
    """One event in a ``POST /webhooks/calendar/sync`` batch."""

    title: str = Field(description="Event title.")
    start_at: str = Field(description="ISO-8601 start (offset-aware or UTC).")
    external_id: str | None = Field(default=None, description="Calendar event id.")
    end_at: str | None = Field(default=None, description="ISO-8601 end.")
    location: str | None = Field(default=None, description="Event location.")
    dest_lat: float | None = Field(
        default=None, description="Destination latitude (enables travel estimation)."
    )
    dest_lon: float | None = Field(default=None, description="Destination longitude.")
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
    dest_lat: float | None = Field(
        default=None, description="Destination latitude (enables travel estimation)."
    )
    dest_lon: float | None = Field(default=None, description="Destination longitude.")
    lead_minutes: float | None = None
    hard: bool = False


class LocationPing(BaseModel):
    """Body of ``POST /webhooks/location`` — the phone's current position."""

    lat: float = Field(description="Current latitude in degrees.")
    lon: float = Field(description="Current longitude in degrees.")
    accuracy_m: float | None = Field(
        default=None, description="Optional reported accuracy radius in metres."
    )


class PlaceCreate(BaseModel):
    """Body of ``POST /places`` — a curated destination alias."""

    name: str = Field(description="Alias to match against a location/title, e.g. 'gym'.")
    lat: float = Field(description="Latitude in degrees.")
    lon: float = Field(description="Longitude in degrees.")
    label: str | None = Field(default=None, description="Optional display spelling.")


class ConflictDismiss(BaseModel):
    """Body of ``POST /commitments/conflicts/dismiss`` — a possible-conflict key."""

    key: str = Field(description="The possible-conflict's `key` from the conflicts list.")


class MailSync(BaseModel):
    """Body of ``POST /webhooks/mail/sync`` — a batch of messages for one account.

    n8n's Gmail node (or the stdlib IMAP fetcher) posts the current batch; the
    endpoint normalizes, dedups, triages, and stores them. ``messages`` are
    loosely-shaped dicts (see ``prefrontal.mail.models.normalize_message``).
    """

    account: str = Field(description="Logical account name (selects the retention policy).")
    messages: list[dict[str, Any]] = Field(default_factory=list)
    policy: Literal["full", "signals"] | None = Field(
        default=None,
        description="Override the account's configured retention policy for this batch.",
    )


class TodoCreate(BaseModel):
    """Body of ``POST /todos`` — an open loop to fit into free time."""

    title: str
    notes: str | None = None
    estimate_minutes: float | None = Field(
        default=None, description="How long it'll take (enables time-fitting)."
    )
    priority: int | None = Field(
        default=None, ge=0, le=3, description="0 low … 3 urgent. Omit to infer."
    )
    deadline: str | None = Field(default=None, description="Optional ISO-8601 deadline.")
    energy: str | None = Field(default=None, description="low | medium | high.")


class TodoDeadlineUpdate(BaseModel):
    """Body of ``POST /todos/{id}/deadline`` — move or clear a todo's deadline."""

    deadline: str | None = Field(
        default=None,
        description="New ISO-8601 deadline, or null to clear it entirely.",
    )


class StepDone(BaseModel):
    """Body of ``POST /todos/{id}/steps/{i}/done`` — tick a decomposed step."""

    done: bool = Field(
        default=True, description="True to mark the step done, false to clear it."
    )


def _state_float(memory: MemoryStore, key: str, default: float) -> float:
    """Read a coaching-state value as a float, falling back on missing/bad data."""
    raw = memory.get_state(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _decompose_and_store(
    memory: MemoryStore, todo_id: int, title: str, client: Any
) -> dict[str, Any]:
    """Generate a todo's first-step decomposition, persist it, and return it."""
    max_first = _state_float(
        memory, "max_first_step_minutes", DEFAULT_MAX_FIRST_STEP_MINUTES
    )
    d = decompose_task(title, max_first_minutes=max_first, client=client)
    memory.set_decomposition(
        todo_id,
        first_step=d.first_step,
        first_step_minutes=d.first_step_minutes,
        steps=d.steps,
        source=d.source,
    )
    return {
        "first_step": d.first_step,
        "first_step_minutes": d.first_step_minutes,
        "steps": d.steps,
        "source": d.source,
    }


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


#: Per-request timeout (seconds) for the hot-path window inference. Kept short:
#: ``/outing/start`` is interactive (an iOS Shortcut waits on it), so a slow or
#: unreachable model must degrade to the heuristic fast rather than hang the tap.
INFER_TIMEOUT_SECONDS = 10.0


def create_app(
    *,
    store: MemoryStore | None = None,
    settings: Settings | None = None,
    ollama: OllamaClient | None = None,
    geocoder: NominatimGeocoder | None = None,
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
    # Client used to infer a window when a start states none. Built from settings
    # unless injected (tests pass a mock-transport client to stay offline).
    ollama_client = ollama or OllamaClient(
        base_url=resolved_settings.ollama_url,
        model=resolved_settings.ollama_model,
        timeout=INFER_TIMEOUT_SECONDS,
    )
    # Forward-geocoder for commitment destinations. Built from settings unless
    # injected (tests pass a stub). Only consulted when the runtime
    # ``geocoding_enabled`` flag is on — see ``_run_geocode`` below.
    geocoder_client = geocoder or NominatimGeocoder.from_settings(resolved_settings)

    def _run_geocode(memory: MemoryStore, *, limit: int = 25) -> dict[str, int]:
        """Enrich commitments with destination coords (best-effort).

        Curated places and the cache are always consulted (offline); the network
        geocoder is passed only when the ``geocoding_enabled`` coaching-state flag
        is on, so a fresh install never reaches off-host by default.
        """
        enabled = (memory.get_state("geocoding_enabled", "0") or "0").strip() in (
            "1",
            "true",
            "True",
        )
        return enrich_commitments(
            memory, geocoder=geocoder_client if enabled else None, limit=limit
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Open the memory store on startup; close it on shutdown if we own it."""
        owns_store = store is None
        # Per-thread connections: FastAPI runs the sync endpoints in a
        # threadpool, and a single shared connection is not safe across threads
        # (it interleaves statements and returns garbled result sets).
        active_store = store or MemoryStore.threaded(resolved_settings.db_path)
        app.state.store = active_store
        app.state.settings = resolved_settings
        app.state.n8n = n8n
        try:
            yield
        finally:
            if owns_store:
                active_store.close()

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

    @app.get("/dashboard", response_class=HTMLResponse, tags=["system"])
    def dashboard() -> str:
        """Serve the read-only monitoring page (a self-contained HTML shell).

        The page itself is unauthenticated — it carries no data. It prompts for
        the ``X-Prefrontal-Token`` once (kept in the browser's localStorage) and
        sends it on every fetch to the auth-guarded ``GET`` endpoints
        (``/outings``, ``/todos``, ``/commitments``, ``/briefing``, ``/profile``),
        which it polls and refreshes. Reachable over Tailscale from any device.
        """
        return DASHBOARD_HTML

    @app.get("/family", response_class=HTMLResponse, tags=["system"])
    def family() -> str:
        """Serve the family view — a calm, non-technical, read-only page.

        Like ``/dashboard`` the shell is unauthenticated and carries no data; it
        prompts once for the access code (the ``X-Prefrontal-Token``) and polls
        only the gentle, read-only endpoints (``/outings`` for "right now" and
        ``/briefing`` for today's plan). No levels, profile, or action buttons —
        meant for a partner to glance at over Tailscale.
        """
        return FAMILY_HTML

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

    @app.post("/webhooks/location", tags=["ingestion"])
    def location_ping(
        payload: LocationPing,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Record the phone's current position (from an iOS Shortcut).

        This is the single source of "where am I now" for the rest of the
        system: ``/webhooks/outing/check`` reads it to gate the coffee-shop
        nudge without a Home Assistant feed, and ``/webhooks/departure/check``
        reads it to estimate travel time to upcoming commitments. A location
        automation ("when I leave Home", on a schedule, or one-tap) can POST it.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        memory.set_location(payload.lat, payload.lon, payload.accuracy_m)
        return {"stored": True, "lat": payload.lat, "lon": payload.lon}

    @app.get("/location", tags=["ingestion"])
    def location_get(
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Return the last-known position (or ``{"location": null}`` if unset)."""
        _verify_token(resolved_settings, x_prefrontal_token)
        return {"location": memory.get_location()}
    # -- Mail ingestion ------------------------------------------------------

    @app.post(
        "/webhooks/mail/sync", status_code=status.HTTP_201_CREATED, tags=["ingestion"]
    )
    def mail_sync(
        payload: MailSync,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Ingest a batch of messages for one account: dedup, triage, store.

        The retention policy comes from the account's configuration
        (``PREFRONTAL_MAIL_ACCOUNTS``) unless ``policy`` overrides it for this
        batch. Triage runs on the local Ollama model, falling back to the
        keyword heuristic if it's unavailable — so a down model never blocks the
        sync. Re-posting the same messages is idempotent (dedup on message id).
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        if not payload.account.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="mail sync requires a non-empty 'account'.",
            )
        policy = payload.policy or resolved_settings.policy_for(payload.account)
        summary = ingest_messages(
            memory,
            payload.messages,
            account=payload.account,
            policy=policy,
            client=ollama_client,
        )
        return {
            "account": summary.account,
            "policy": summary.policy,
            "received": summary.received,
            "ingested": summary.ingested,
            "skipped": summary.skipped,
            "invalid": summary.invalid,
            "needs_action": summary.needs_action,
            "todos_created": summary.todos_created,
            "triaged_by_llm": summary.triaged_by_llm,
        }

    @app.get("/mail", tags=["ingestion"])
    def mail_list(
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Read-only snapshot: recent triaged mail and the open action items.

        ``needs_action`` lists messages still awaiting a reply/action (their
        linked todo is still open); ``recent`` is the latest ingested mail for a
        dashboard. No side effects — safe to poll.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        return {
            "needs_action": memory.mail_needing_action(),
            "recent": memory.recent_mail(limit=30),
        }

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

        The window is resolved in order of confidence: ``time_window_minutes`` if
        given ('explicit'), else parsed from the text like "back in 15 minutes"
        ('parsed'), else **inferred** from the intention — the local model first,
        then a keyword heuristic, then a default ('llm'/'heuristic'/'default').
        Only a blank intention (nothing to reason from) responds 422.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        window = payload.time_window_minutes
        source = "explicit"
        if window is None:
            parsed = parse_time_window(payload.intention)
            if parsed is not None:
                window, source = parsed, "parsed"
        if window is None:
            inferred = infer_time_window(payload.intention, client=ollama_client)
            if inferred is not None:
                window, source = inferred
        if window is None or window <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Could not determine a time window: the intention is empty. "
                    "Send a non-empty intention, or include 'time_window_minutes'."
                ),
            )
        outing_id = memory.start_outing(
            payload.intention,
            window,
            home_lat=payload.home_lat,
            home_lon=payload.home_lon,
        )
        return OutingStarted(
            outing_id=outing_id,
            intention=payload.intention,
            time_window_minutes=window,
            time_window_source=source,
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
        # Fall back to the phone's last-known position (POSTed to
        # /webhooks/location) when the poll body carries no explicit coordinates
        # — so location-gating works without a Home Assistant feed.
        if cur_lat is None or cur_lon is None:
            last = memory.get_location()
            if last is not None:
                cur_lat, cur_lon = last["lat"], last["lon"]
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

    @app.get("/outings", tags=["anchor"])
    def outings_list(
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Read-only snapshot of outings for monitoring — **no side effects**.

        Unlike ``/webhooks/outing/check`` (which fires nudges and auto-closes
        abandoned outings), this never mutates state, so a dashboard can poll it
        freely. Active outings carry their current elapsed-time escalation
        ``level`` (computed, not recorded); recent outings are returned newest
        first for history.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        active = [
            {
                "outing_id": o["id"],
                "intention": o["intention"],
                "elapsed_minutes": round(o.get("elapsed_minutes") or 0.0, 1),
                "time_window_minutes": o["time_window_minutes"],
                "level": escalation_level(
                    o.get("elapsed_minutes") or 0.0, o["time_window_minutes"]
                ),
                "departure_at": o["departure_at"],
            }
            for o in memory.active_outings()
        ]
        recent = [
            {
                "outing_id": o["id"],
                "intention": o["intention"],
                "status": o["status"],
                "time_window_minutes": o["time_window_minutes"],
                "departure_at": o["departure_at"],
                "returned_at": o.get("returned_at"),
            }
            for o in memory.recent_outings(limit=20)
        ]
        return {"active": active, "recent": recent}

    # -- Focus sessions (Hyperfocus) -----------------------------------------

    @app.post(
        "/webhooks/focus/start",
        response_model=FocusStarted,
        status_code=status.HTTP_201_CREATED,
        tags=["focus"],
    )
    def focus_start(
        payload: FocusStart,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> FocusStarted:
        """Declare a focus session: a stated task and an optional planned length.

        Unlike an outing, no window is *required* — the soft alignment check
        falls back to the ``hyperfocus_block_minutes`` default when no
        ``planned_minutes`` is given, and the hard biological break is keyed off
        ``hard_interrupt_minutes`` regardless. Only a blank task is rejected.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        if not payload.intended_task.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Send a non-empty intended_task.",
            )
        session_id = memory.start_focus_session(
            payload.intended_task.strip(),
            planned_minutes=payload.planned_minutes,
            aligned=payload.aligned,
        )
        return FocusStarted(
            session_id=session_id,
            intended_task=payload.intended_task.strip(),
            planned_minutes=payload.planned_minutes,
            aligned=payload.aligned,
        )

    @app.post("/webhooks/focus/check", tags=["focus"])
    def focus_check(
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Evaluate active focus sessions and report which interrupts are due.

        n8n polls this on a schedule. For each active session:

        - If elapsed time has blown far past the hard ceiling (the abandon
          ratio), the session is **auto-closed** as abandoned so a forgotten
          exit tap stops lingering active.
        - Otherwise the interrupt level is computed (``none``/``check``/
          ``break``); when a *new* level is crossed since the last poll it sets
          ``fire=true``, records the level so it fires once, and includes the
          ``message``.
        - ``protect`` reports whether an aligned, healthy block should currently
          shield the user from other modules' non-critical nudges — the
          asymmetry that makes this hyperfocus support rather than a timer.

        n8n acts on ``fire == true``; a delivery layer can consult ``protect``
        (or :func:`prefrontal.modules.hyperfocus.is_focus_protected`) to gate
        other nudges. A ``break`` is never suppressed.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        name = memory.get_state("user_name", "") or ""
        soft = _state_float(memory, "hyperfocus_block_minutes", DEFAULT_SOFT_BLOCK_MINUTES)
        hard = _state_float(memory, "hard_interrupt_minutes", DEFAULT_HARD_INTERRUPT_MINUTES)
        abandon_ratio = _state_float(
            memory, "focus_abandon_after_ratio", DEFAULT_FOCUS_ABANDON_RATIO
        )
        protect_enabled = (
            memory.get_state("protect_aligned_hyperfocus", "true") or "true"
        ).lower() == "true"

        results: list[dict[str, Any]] = []
        for session in memory.active_focus_sessions():
            elapsed = session["elapsed_minutes"] or 0.0
            planned = session["planned_minutes"]
            aligned = bool(session["aligned"])

            level, fire, message, outcome, protect = "none", False, "", None, False
            if focus_is_abandoned(elapsed, hard, abandon_ratio):
                closed = memory.close_focus_session(session["id"], status="abandoned")
                outcome = record_focus_abandoned(memory, closed)["outcome"]
                session_status = "abandoned"
            else:
                level = focus_level(
                    elapsed,
                    planned_minutes=planned,
                    soft_block_minutes=soft,
                    hard_interrupt_minutes=hard,
                )
                fire = focus_level_rank(level) > focus_level_rank(session["last_level"])
                protect = should_protect(
                    level, aligned=aligned, protect_enabled=protect_enabled
                )
                if fire:
                    memory.set_focus_session_level(session["id"], level)
                    message = build_focus_message(
                        level,
                        task=session["intended_task"],
                        elapsed_minutes=elapsed,
                        aligned=aligned,
                        name=name,
                    )
                session_status = "active"

            results.append(
                {
                    "session_id": session["id"],
                    "intended_task": session["intended_task"],
                    "elapsed_minutes": round(elapsed, 1),
                    "planned_minutes": planned,
                    "aligned": aligned,
                    "level": level,
                    "fire": fire,
                    "message": message,
                    "protect": protect,
                    "status": session_status,
                    "outcome": outcome,
                }
            )
        return {"active": results, "protect": any(r["protect"] for r in results)}

    @app.post("/webhooks/focus/end", tags=["focus"])
    def focus_end(
        payload: FocusEnd,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Close a focus session and log planned-vs-actual for pattern tracking.

        Records the actual time spent as a ``task`` episode (predicted = planned
        duration, actual = minutes spent) so the time-blindness learning can use
        it. A captured ``breadcrumb`` and one-tap ``outcome`` rating ride along
        on the session row and the episode notes.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        session_id = payload.session_id
        if session_id is None:
            recent = memory.most_recent_active_focus_session()
            if recent is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No active focus session to end.",
                )
            session_id = recent["id"]

        closed = memory.close_focus_session(
            session_id,
            status=payload.status,
            breadcrumb=payload.breadcrumb,
            outcome=payload.outcome,
        )
        if closed is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Focus session {session_id} is not active.",
            )

        if payload.status == "ended":
            recorded = record_focus_end(
                memory, closed, outcome=payload.outcome, breadcrumb=payload.breadcrumb
            )
        else:
            recorded = record_focus_abandoned(memory, closed)
        actual = closed.get("actual_minutes")
        return {
            "session_id": session_id,
            "status": payload.status,
            "actual_minutes": round(actual, 1) if actual is not None else None,
            "planned_minutes": closed.get("planned_minutes"),
            "breadcrumb": closed.get("breadcrumb"),
            "outcome": recorded["outcome"],
            "episode_id": recorded["episode_id"],
        }

    @app.get("/focus", tags=["focus"])
    def focus_list(
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Read-only snapshot of focus sessions for monitoring — **no side effects**.

        Unlike ``/webhooks/focus/check`` (which fires interrupts and auto-closes
        abandoned sessions), this never mutates state, so a dashboard can poll it
        freely. Active sessions carry their current interrupt ``level`` and
        ``protect`` flag (computed, not recorded); recent sessions are returned
        newest first for history.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        soft = _state_float(memory, "hyperfocus_block_minutes", DEFAULT_SOFT_BLOCK_MINUTES)
        hard = _state_float(memory, "hard_interrupt_minutes", DEFAULT_HARD_INTERRUPT_MINUTES)
        protect_enabled = (
            memory.get_state("protect_aligned_hyperfocus", "true") or "true"
        ).lower() == "true"
        active = []
        for s in memory.active_focus_sessions():
            level = focus_level(
                s.get("elapsed_minutes") or 0.0,
                planned_minutes=s["planned_minutes"],
                soft_block_minutes=soft,
                hard_interrupt_minutes=hard,
            )
            active.append(
                {
                    "session_id": s["id"],
                    "intended_task": s["intended_task"],
                    "elapsed_minutes": round(s.get("elapsed_minutes") or 0.0, 1),
                    "planned_minutes": s["planned_minutes"],
                    "aligned": bool(s["aligned"]),
                    "level": level,
                    "protect": should_protect(
                        level, aligned=bool(s["aligned"]), protect_enabled=protect_enabled
                    ),
                    "started_at": s["started_at"],
                }
            )
        recent = [
            {
                "session_id": s["id"],
                "intended_task": s["intended_task"],
                "status": s["status"],
                "planned_minutes": s["planned_minutes"],
                "started_at": s["started_at"],
                "ended_at": s.get("ended_at"),
                "outcome": s.get("outcome"),
                "breadcrumb": s.get("breadcrumb"),
            }
            for s in memory.recent_focus_sessions(limit=20)
        ]
        return {"active": active, "recent": recent}

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
        # Fill in destination coordinates for events that arrived with only a
        # free-text location, so the departure reminder can estimate travel time.
        # Curated places + the cache are offline; the network geocoder is used
        # only when geocoding_enabled is on. Best-effort: never blocks the sync.
        geocoded = _run_geocode(memory)
        return {
            "added": summary.added,
            "updated": summary.updated,
            "cancelled": summary.cancelled,
            "upcoming": summary.upcoming,
            "conflicts": summary.conflicts,
            "new_conflict": summary.new_conflict,
            "possible_conflicts": summary.possible_conflicts,
            "new_possible_conflict": summary.new_possible_conflict,
            "geocoded": geocoded["resolved"],
        }

    @app.post("/webhooks/departure/check", tags=["schedule"])
    async def departure_check(
        request: Request,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Evaluate upcoming commitments and report whether to nudge a departure.

        n8n polls this on a schedule (the Departure Reminder workflow). For each
        upcoming commitment it computes a leave-by time: from the phone's
        last-known location and the commitment's ``dest_lat``/``dest_lon`` when
        both are known (a local, bias-adjusted travel estimate), otherwise from
        the commitment's static ``lead_minutes``. The most urgent reminder-worthy
        commitment is returned; ``fire`` is ``true`` only when its
        ``(commitment, level)`` pair is new since the last poll, so a standing
        reminder doesn't re-alert every cycle. Current coordinates may be
        overridden in the request body (``current_lat``/``current_lon``).
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
        if cur_lat is None or cur_lon is None:
            last = memory.get_location()
            if last is not None:
                cur_lat, cur_lon = last["lat"], last["lon"]

        bias = _state_float(memory, "time_estimation_bias", 1.0)
        name = memory.get_state("user_name", "") or ""
        plans = [
            plan_departure(
                c,
                current_lat=cur_lat,
                current_lon=cur_lon,
                bias=bias,
                speed_kmh=_state_float(memory, "travel_speed_kmh", DEFAULT_TRAVEL_SPEED_KMH),
                road_factor=_state_float(memory, "travel_road_factor", DEFAULT_ROAD_FACTOR),
                prep_minutes=_state_float(memory, "departure_prep_minutes", DEFAULT_PREP_MINUTES),
                heads_up_minutes=_state_float(
                    memory, "departure_heads_up_minutes", DEFAULT_HEADS_UP_MINUTES
                ),
                soon_minutes=_state_float(
                    memory, "departure_soon_minutes", DEFAULT_SOON_MINUTES
                ),
            )
            for c in memory.upcoming_commitments()
        ]
        top = next_departure(plans)

        fire, message, reminder = False, "", None
        if top is not None:
            reminder = {
                "commitment_id": top.commitment["id"],
                "title": top.commitment["title"],
                "location": top.commitment.get("location"),
                "start_at": top.commitment["start_at"],
                "leave_by": top.leave_by,
                "minutes_until_leave": top.minutes_until_leave,
                "travel_minutes": top.travel_minutes,
                "basis": top.basis,
                "level": top.level,
            }
            signature = f"{top.commitment['id']}:{top.level}"
            fire = signature != memory.get_state("last_departure_signature", "")
            if fire:
                memory.set_state("last_departure_signature", signature, source="inferred")
                message = build_departure_message(top, name=name)

        return {
            "fire": fire,
            "message": message,
            "reminder": reminder,
            "location_known": cur_lat is not None and cur_lon is not None,
        }

    @app.post(
        "/commitments", status_code=status.HTTP_201_CREATED, tags=["schedule"]
    )
    def commitment_create(
        payload: CommitmentCreate,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Add a single commitment manually (source ``manual``).

        If a ``location`` is given without explicit coordinates, a geocode pass
        runs (curated places + cache always; network geocoder only when
        ``geocoding_enabled`` is on) so the departure reminder can use travel time.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        try:
            fields = normalize_event({**payload.model_dump(), "source": "manual"})
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        commitment_id, _ = memory.upsert_commitment(**fields)
        if fields.get("dest_lat") is None and fields.get("location"):
            _run_geocode(memory)
        commitment = memory.get_commitment(commitment_id)
        return {
            "commitment_id": commitment_id,
            "dest_lat": commitment.get("dest_lat") if commitment else None,
            "dest_lon": commitment.get("dest_lon") if commitment else None,
        }

    @app.post("/commitments/geocode", tags=["schedule"])
    def commitments_geocode(
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Run a geocoding pass over commitments missing coordinates.

        Useful for backfilling after enabling geocoding or adding curated places.
        Curated places + the cache are always consulted; the network geocoder is
        used only when ``geocoding_enabled`` is on.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        return _run_geocode(memory)

    @app.post("/places", status_code=status.HTTP_201_CREATED, tags=["schedule"])
    def place_create(
        payload: PlaceCreate,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Add (or update) a curated place alias used before any geocoding.

        The ``name`` is normalized to a match key; re-posting the same name
        updates its coordinates. Matched against a commitment's location and
        title, so "gym" resolves "Gym session" instantly and offline.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        name = normalize_query(payload.name)
        if not name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Place 'name' is empty after normalization.",
            )
        place_id = memory.add_place(
            name, payload.lat, payload.lon, label=payload.label or payload.name
        )
        return {"place_id": place_id, "name": name}

    @app.get("/places", tags=["schedule"])
    def places_list(
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """List curated place aliases (most specific name first)."""
        _verify_token(resolved_settings, x_prefrontal_token)
        return {"places": memory.places()}

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
        """Report overlaps among upcoming commitments, split by firmness.

        ``conflicts`` are firm double-bookings (two real events overlap).
        ``possible_conflicts`` are soft — a placeholder (Busy/Block/Hold)
        overlapping a real event — excluding any the user has dismissed; each
        carries a ``key`` to dismiss it via ``POST /commitments/conflicts/dismiss``.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        hard, possible = partition_conflicts(
            find_conflicts(memory.upcoming_commitments()), memory.dismissed_conflicts()
        )

        def pair(c: Any) -> dict[str, Any]:
            return {
                "a": {"id": c.a["id"], "title": c.a["title"], "start_at": c.a["start_at"]},
                "b": {"id": c.b["id"], "title": c.b["title"], "start_at": c.b["start_at"]},
                "overlap_minutes": c.overlap_minutes,
            }

        return {
            "conflicts": [pair(c) for c in hard],
            "possible_conflicts": [
                {**pair(c), "key": conflict_dismissal_key(c)} for c in possible
            ],
        }

    @app.post("/commitments/conflicts/dismiss", tags=["schedule"])
    def dismiss_possible_conflict(
        payload: ConflictDismiss,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Dismiss a possible conflict by its ``key`` (from the conflicts list).

        The dismissal sticks across re-syncs but lapses if either event moves or
        is retitled (the key is derived from start time + title).
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        memory.dismiss_conflict(payload.key)
        return {"dismissed": payload.key}

    @app.get("/briefing", tags=["schedule"])
    def briefing(
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Return today's morning briefing as structured data plus rendered text.

        n8n can deliver the ``text`` directly, or feed it to Ollama for prose
        (or call ``prefrontal summarize``-style). Always fast and model-free here.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        b = build_briefing(memory)
        return {
            "date": b.date,
            "format": b.format,
            "today": b.today,
            "conflicts": b.conflicts,
            "slips": b.slips,
            "coaching": b.coaching,
            "spare": b.spare,
            "text": render_briefing(b),
        }

    # -- Todos (open loops fitted into free time) ----------------------------

    @app.post("/todos", status_code=status.HTTP_201_CREATED, tags=["todos"])
    def todo_create(
        payload: TodoCreate,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Add an open todo, auto-filling any fields you didn't supply.

        Missing ``estimate_minutes``/``priority``/``energy``/``deadline`` are
        inferred (local model → keyword heuristic → default) so the todo is
        schedulable and honestly sortable. Supplied values are kept as-is; the
        response's ``augmented`` map says where each field came from.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        # A user-supplied deadline is validated strictly (422 on garbage); an
        # inferred one is best-effort (dropped if it somehow won't parse).
        user_deadline = None
        if payload.deadline:
            try:
                user_deadline = to_utc(payload.deadline)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Bad deadline: {exc}",
                ) from exc

        aug = augment_todo(
            payload.title,
            estimate_minutes=payload.estimate_minutes,
            priority=payload.priority,
            energy=payload.energy,
            deadline=payload.deadline,
            client=ollama_client,
        )
        deadline = user_deadline
        if user_deadline is None and aug.deadline:
            try:
                deadline = to_utc(aug.deadline)
            except ValueError:
                deadline = None

        todo_id = memory.add_todo(
            payload.title,
            notes=payload.notes,
            estimate_minutes=aug.estimate_minutes,
            priority=aug.priority,
            deadline=deadline,
            energy=aug.energy,
        )
        # Big tasks stall on starting — auto-decompose into a tiny first step.
        threshold = _state_float(memory, "decomposition_threshold_minutes", 30.0)
        decomposition = None
        if aug.estimate_minutes >= threshold:
            decomposition = _decompose_and_store(
                memory, todo_id, payload.title, ollama_client
            )
        return {
            "todo_id": todo_id,
            "estimate_minutes": aug.estimate_minutes,
            "priority": aug.priority,
            "energy": aug.energy,
            "deadline": deadline,
            "augmented": aug.sources,
            "decomposition": decomposition,
        }

    @app.get("/todos", tags=["todos"])
    def todos_list(
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """List open todos with decompositions and an avoidance flag."""
        _verify_token(resolved_settings, x_prefrontal_token)
        todos = memory.open_todos()
        avoided = {a["todo"]["id"]: a for a in avoided_todos(todos, utcnow())}
        for todo in todos:
            todo["decomposition"] = memory.get_decomposition(todo["id"])
            hit = avoided.get(todo["id"])
            todo["avoidance"] = (
                {"days_open": hit["days_open"], "score": hit["score"]} if hit else None
            )
        return {"todos": todos}

    @app.get("/todos/avoided", tags=["todos"])
    def todos_avoided(
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """The important todos you keep skipping, worst-avoided first.

        Honest prioritization: surfaces what's been sitting (high enough priority,
        open a while) so the fun/shiny task doesn't quietly win. Pure heuristic
        over age/priority/size/deadline — no extra tracking.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        items = avoided_todos(memory.open_todos(), utcnow())
        return {
            "avoided": [
                {
                    "todo_id": a["todo"]["id"],
                    "title": a["todo"]["title"],
                    "days_open": a["days_open"],
                    "score": a["score"],
                    "priority": a["todo"].get("priority"),
                    "estimate_minutes": a["todo"].get("estimate_minutes"),
                    "deadline": a["todo"].get("deadline"),
                }
                for a in items
            ]
        }

    @app.post("/todos/{todo_id}/decompose", tags=["todos"])
    def todo_decompose(
        todo_id: int,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Break an open todo into a tiny first step (+ remaining steps).

        On-demand counterpart to the auto-decompose on big todos — call it the
        moment you're about to start something and need a way in. Regenerates
        and replaces any existing decomposition.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        todo = memory.get_todo(todo_id)
        if todo is None or todo["status"] != "open":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Todo {todo_id} is not open.",
            )
        decomposition = _decompose_and_store(
            memory, todo_id, todo["title"], ollama_client
        )
        return {"todo_id": todo_id, "decomposition": decomposition}

    @app.post("/todos/{todo_id}/deadline", tags=["todos"])
    def todo_set_deadline(
        todo_id: int,
        payload: TodoDeadlineUpdate,
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Move (or clear) an open todo's deadline.

        Plans drift — a deadline set at creation or inferred from the title often
        needs to change. A non-empty ``deadline`` is normalized to UTC (422 on
        garbage); ``null``/empty clears it. Declared before the ``{action}`` route
        so "deadline" isn't mistaken for a done/drop action.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        deadline = None
        if payload.deadline:
            try:
                deadline = to_utc(payload.deadline)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Bad deadline: {exc}",
                ) from exc
        if not memory.update_todo_deadline(todo_id, deadline):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Todo {todo_id} is not open.",
            )
        return {"todo_id": todo_id, "deadline": deadline}

    @app.post("/todos/{todo_id}/steps/{step_index}/done", tags=["todos"])
    def todo_step_done(
        todo_id: int,
        step_index: int,
        memory: Annotated[MemoryStore, Depends(get_store)],
        payload: StepDone | None = None,
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Tick a single decomposed step done (or clear it).

        Index ``0`` is the first step and ``1..N`` are the remaining steps.
        Checking steps off one at a time turns a stalled task into visible
        progress. Body ``{"done": false}`` un-ticks a step; the body is optional
        and defaults to marking it done. Returns the refreshed decomposition.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        done = payload.done if payload is not None else True
        if not memory.set_step_done(todo_id, step_index, done=done):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Todo {todo_id} has no step {step_index}.",
            )
        return {
            "todo_id": todo_id,
            "step_index": step_index,
            "done": done,
            "decomposition": memory.get_decomposition(todo_id),
        }

    @app.post("/todos/{todo_id}/{action}", tags=["todos"])
    def todo_close(
        todo_id: int,
        action: Literal["done", "drop"],
        memory: Annotated[MemoryStore, Depends(get_store)],
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Mark a todo done or drop it.

        Closing logs a ``task`` episode (done ⇒ ``success``, drop ⇒ ``miss``) so
        the outcome feeds the learning pass like every other touchpoint — the
        moment an avoided todo finally resolves is captured, not discarded.
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        new_status = "done" if action == "done" else "dropped"
        if not memory.close_todo(todo_id, status=new_status):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Todo {todo_id} is not open.",
            )
        closed = memory.get_todo(todo_id)
        episode_id = (
            record_todo_closed(memory, closed, now=utcnow())["episode_id"]
            if closed is not None
            else None
        )
        return {"todo_id": todo_id, "status": new_status, "episode_id": episode_id}

    @app.get("/todos/fit", tags=["todos"])
    def todos_fit(
        memory: Annotated[MemoryStore, Depends(get_store)],
        minutes: float,
        x_prefrontal_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Rank the open todos that fit in ``minutes`` of free time, right now.

        Applies the learned time bias, so a "10-minute" todo is judged at its
        realistic length. Great for "I have 20 minutes — what can I knock out?"
        """
        _verify_token(resolved_settings, x_prefrontal_token)
        bias = _state_float(memory, "time_estimation_bias", 1.0)
        fits = fit_todos(minutes, memory.open_todos(), bias)
        return {
            "available_minutes": minutes,
            "fits": [
                {
                    "todo_id": f["todo"]["id"],
                    "title": f["todo"]["title"],
                    "estimate_minutes": f["todo"].get("estimate_minutes"),
                    "effective_minutes": f["effective_minutes"],
                    "priority": f["todo"].get("priority"),
                }
                for f in fits
            ],
        }

    return app


#: Default application instance for ``uvicorn prefrontal.webhooks.app:app``.
app = create_app()
