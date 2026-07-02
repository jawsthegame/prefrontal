"""Shared surface for the webhook layer's per-tag routers.

The routes live in per-tag modules under :mod:`prefrontal.webhooks.routers`, and
the app is assembled by :func:`prefrontal.webhooks.app.create_app`. This module
holds everything those routers share, so each router imports from one place
rather than re-declaring it:

- **Pydantic request/response models** (``ShortcutPayload``, ``TodoCreate``, …).
- **Request dependencies & identity** — :class:`ScopedRequest`, :func:`get_store`,
  :func:`resolve_user`, :func:`require_operator`. Authentication resolves the
  ``X-Prefrontal-Token`` header to a user (its ``sha256`` is matched against the
  ``users`` table) and scopes the request to them, so one person never sees
  another's data. ``PREFRONTAL_DEFAULT_USER`` lets tokenless requests resolve to
  one user (single-user / trusted-LAN mode); the legacy
  ``PREFRONTAL_WEBHOOK_SECRET`` still works as a bootstrap operator token.
- **Small formatting/URL helpers** shared across routers (nudge read-backs,
  dismiss links, ``_delivery_fields``) and a couple of constants
  (``DASHBOARD_HTML``, ``APP_VERSION``, …).
- A wide **re-export of names** the routers use (stdlib/FastAPI/pydantic plus the
  domain functions), kept in ``__all__`` so the routers' explicit imports resolve
  from here.

It deliberately holds no routes and no ``create_app`` — see ``app.py`` for those.
"""

from __future__ import annotations

import hmac
import html
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from prefrontal.assistant import (
    build_snapshot,
    execute_actions,
    validate_actions,
)
from prefrontal.assistant import (
    plan as assistant_plan_message,
)
from prefrontal.briefing import build_briefing, render_briefing
from prefrontal.classify import classify_kind
from prefrontal.commitments import (
    KINDS,
    conflict_dismissal_key,
    find_conflicts,
    normalize_event,
    partition_conflicts,
    sync_calendar,
    to_utc,
)
from prefrontal.config import Settings, get_settings
from prefrontal.departure import (
    DEFAULT_DEPARTURE_GRACE_MINUTES,
    DEFAULT_HEADS_UP_MINUTES,
    DEFAULT_PREP_MINUTES,
    DEFAULT_ROAD_FACTOR,
    DEFAULT_SOON_MINUTES,
    DEFAULT_TRAVEL_SPEED_KMH,
    attribute_departure,
    build_departure_message,
    classify_departure,
    next_departure,
    plan_departure,
    record_departure_outcome,
)
from prefrontal.geocode import enrich_commitments, normalize_query
from prefrontal.impact import (
    analyze_impact,
    at_risk,
    impact_phrase,
    project_free_time,
    utcnow,
)
from prefrontal.integrations.anthropic import AnthropicClient
from prefrontal.integrations.n8n import N8nClient, parse_inbound_event
from prefrontal.integrations.nominatim import NominatimGeocoder
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.mail import ingest_messages
from prefrontal.mail.feedback import (
    learned_corrections,
    learned_denylist,
    record_drop_feedback,
)
from prefrontal.memory.store import MemoryStore, feed_label, provision_user, sha256_hex
from prefrontal.memory.summarizer import (
    build_profile,
    cache_is_stale,
    load_cached_summary,
    refresh_profile_cache,
)
from prefrontal.modules.hyperfocus import (
    DEFAULT_FOCUS_ABANDON_RATIO,
    DEFAULT_HARD_INTERRUPT_MINUTES,
    DEFAULT_SOFT_BLOCK_MINUTES,
    build_focus_message,
    focus_level,
    record_focus_abandoned,
    record_focus_end,
    record_focus_switched,
    should_protect,
)
from prefrontal.modules.hyperfocus import is_abandoned as focus_is_abandoned
from prefrontal.modules.hyperfocus import level_rank as focus_level_rank
from prefrontal.modules.impulsivity import (
    DEFAULT_PAUSE_SECONDS,
    SWITCH_ACTIONS,
    build_pause_message,
    infer_capture_title,
    pause_seconds,
    switch_response,
)
from prefrontal.modules.location_anchor import (
    DEFAULT_ABANDON_RATIO,
    DEFAULT_HOME_RADIUS_M,
    LEVELS,
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
from prefrontal.modules.registry import is_enabled as module_enabled
from prefrontal.modules.task_paralysis import (
    DEFAULT_BODY_DOUBLE_MIN_MISSES,
    body_double_message,
    repeat_stalled_tasks,
)
from prefrontal.panic import (
    DEFAULT_ALERT_COOLDOWN_MINUTES,
    DEFAULT_ALERT_MIN_PRESSING,
    build_panic,
    overwhelm_level,
    panic_alert_message,
    render_panic,
)
from prefrontal.scheduling import (
    DEFAULT_DAY_END,
    DEFAULT_DAY_START,
    DEFAULT_FIT_CAP_MINUTES,
    DEFAULT_MIN_WINDOW_MINUTES,
    available_now,
    filter_suggestible,
    fit_todos,
    format_window,
    local_datetime,
    local_hour_of,
    parse_window,
    pick_now,
    window_config_for,
    work_window_now,
)
from prefrontal.todos import (
    DEFAULT_MAX_FIRST_STEP_MINUTES,
    MAX_CATEGORIES,
    at_category_cap,
    augment_todo,
    avoided_todos,
    category_stats,
    decompose_task,
    normalize_category,
    record_todo_closed,
)
from prefrontal.webhooks.oauth import (
    register_oauth_routes,
    session_user,
    sign_dismiss,
    verify_dismiss,
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
    confirmation: str = Field(
        default="",
        description=(
            "Speakable one-line read-back a thin client (iOS Shortcut) can show "
            "verbatim — flags an estimated window so the user can correct it."
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
    confirmation: str = Field(
        default="",
        description="Speakable one-line read-back a thin client can show verbatim.",
    )


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


class CaptureImpulse(BaseModel):
    """Body of ``POST /webhooks/impulse/capture`` — park an impulse as a todo."""

    impulse_text: str = Field(
        description="The raw, half-formed impulse, e.g. 'ooh reorganize the font folder'."
    )
    priority: int = Field(
        default=1, description="0 low / 1 normal / 2 high / 3 urgent (defaults to normal)."
    )


class ImpulseCaptured(BaseModel):
    """Response after an impulse is parked as a ``source='impulse'`` todo."""

    todo_id: int
    title: str = Field(description="The cleaned-up title (LLM with heuristic fallback).")
    raw: str = Field(description="The verbatim impulse text, kept in the todo's notes.")
    confirmation: str = Field(
        default="",
        description="Speakable one-line read-back a thin client can show verbatim.",
    )


class SwitchImpulse(BaseModel):
    """Body of ``POST /webhooks/focus/switch`` — signalling the pull to switch."""

    session_id: int | None = Field(
        default=None,
        description="Focus session the impulse fires against; defaults to the active one.",
    )


class SwitchPause(BaseModel):
    """Response to a switch-impulse — the reflective-pause directive."""

    session_id: int
    intended_task: str
    elapsed_minutes: float
    pause_seconds: float = Field(
        description="How long the client should hold before offering options."
    )
    message: str
    options: list[str] = Field(description="Resolutions the client should present.")


class SwitchResolve(BaseModel):
    """Body of ``POST /webhooks/focus/resolve`` — how a switch-impulse was resolved."""

    session_id: int | None = Field(
        default=None, description="Defaults to the active session."
    )
    action: str = Field(description="One of 'return' / 'defer' / 'switch'.")
    impulse_text: str | None = Field(
        default=None, description="For 'defer': the impulse to park as a todo."
    )


class SwitchResolved(BaseModel):
    """Response after a switch-impulse is resolved."""

    session_id: int
    action: str
    todo_id: int | None = Field(
        default=None, description="The parked impulse's todo id (only for 'defer')."
    )
    session_status: str = Field(description="The session's status after resolving.")
    confirmation: str = Field(
        default="", description="Speakable one-line read-back a thin client shows verbatim."
    )


class CalendarEvent(BaseModel):
    """One event in a ``POST /webhooks/calendar/sync`` batch."""

    title: str = Field(description="Event title.")
    start_at: str = Field(description="ISO-8601 start (offset-aware, UTC, or naive).")
    external_id: str | None = Field(default=None, description="Calendar event id.")
    end_at: str | None = Field(default=None, description="ISO-8601 end.")
    tzid: str | None = Field(
        default=None,
        description=(
            "Source timezone for a naive start_at/end_at (IANA or Windows name, "
            "e.g. the ICS DTSTART;TZID=…). Ignored when the timestamp is "
            "offset-aware."
        ),
    )
    end_tzid: str | None = Field(
        default=None, description="Source zone for a naive end_at (defaults to tzid)."
    )
    rrule: str | None = Field(
        default=None,
        description=(
            "ICS RRULE of a recurring master (e.g. 'FREQ=WEEKLY;BYDAY=WE'). "
            "Expanded server-side into concrete occurrences within the sync window."
        ),
    )
    exdate: list[str] | None = Field(
        default=None, description="Occurrence start times excluded from the RRULE."
    )
    recurrence_id: str | None = Field(
        default=None,
        description=(
            "Original start of a modified single occurrence; suppresses the "
            "generated occurrence so this instance stands in for it."
        ),
    )
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


class CommitmentKind(BaseModel):
    """Body of ``POST /commitments/{id}/kind`` — correct a commitment's kind."""

    kind: str = Field(description="`self` (your commitment) or `fyi` (where someone will be).")


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


class TriageForget(BaseModel):
    """Body of ``POST /mail/triage/learned/forget`` — drop one learned correction."""

    id: int = Field(description="The `triage_feedback` row id to forget (from the learned list).")


class UserCreate(BaseModel):
    """Body of ``POST /admin/users`` — provision a user (operator-only)."""

    handle: str = Field(description="Unique short handle, e.g. 'sam'.")
    display_name: str | None = Field(
        default=None, description="Name shown in nudges/briefings."
    )
    is_operator: bool = Field(
        default=False, description="Whether the user may call the admin surface."
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
    category: str | None = Field(
        default=None, description="Topic (short label). Omit to infer; capped at 20."
    )
    time_window: str | None = Field(
        default=None,
        description=(
            'Optional per-todo suggestion window "HH:MM-HH:MM" (local), overriding '
            "the category/source/default window. Omit to use the category's window."
        ),
    )


class TodoDeadlineUpdate(BaseModel):
    """Body of ``POST /todos/{id}/deadline`` — move or clear a todo's deadline."""

    deadline: str | None = Field(
        default=None,
        description="New ISO-8601 deadline, or null to clear it entirely.",
    )


class TodoCategoryUpdate(BaseModel):
    """Body of ``POST /todos/{id}/category`` — set or clear a todo's category."""

    category: str | None = Field(
        default=None,
        description="New category label, or null to clear it (uncategorized).",
    )


class TodoWindowUpdate(BaseModel):
    """Body of ``POST /todos/{id}/window`` — set or clear a todo's time window."""

    time_window: str | None = Field(
        default=None,
        description=(
            'New suggestion window "HH:MM-HH:MM" (local), or null to clear it so the '
            "todo falls back to its category window."
        ),
    )


class StepDone(BaseModel):
    """Body of ``POST /todos/{id}/steps/{i}/done`` — tick a decomposed step."""

    done: bool = Field(
        default=True, description="True to mark the step done, false to clear it."
    )


class AssistantMessage(BaseModel):
    """Body of ``POST /assistant`` — a natural-language editing request."""

    message: str = Field(description="Free-text ask, e.g. 'bump the dentist call to urgent'.")


class AssistantApply(BaseModel):
    """Body of ``POST /assistant/apply`` — the proposed actions to execute.

    The client echoes back the ``actions`` returned by ``POST /assistant``. They
    are re-validated against the *current* store before executing, so a stale or
    tampered action simply drops rather than acting on the wrong row.
    """

    actions: list[dict[str, Any]] = Field(
        default_factory=list, description="Wire-format actions from POST /assistant."
    )


def _parse_dt_or_none(ts: str | None) -> Any:
    """Parse a stored ``YYYY-MM-DD HH:MM:SS`` (naive UTC) timestamp, or ``None``."""
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _fmt_minutes(value: float | None) -> str:
    """Render a minutes value without a trailing ``.0`` (30.0 -> "30", 12.5 -> "12.5")."""
    if value is None:
        return "?"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


#: Window sources that mean the user stated the duration; anything else was
#: guessed by the server (and the confirmation says so, so the user can correct).
_EXACT_WINDOW_SOURCES = {"explicit", "parsed"}


def _outing_started_confirmation(intention: str, minutes: float, source: str) -> str:
    """One-line, speakable read-back for a started outing — flags a guessed window."""
    mins = _fmt_minutes(minutes)
    if source in _EXACT_WINDOW_SOURCES:
        return f"Tracking “{intention}” for {mins} min — I'll nudge you to head back."
    return (
        f"Tracking “{intention}” for ~{mins} min (estimated — say “back in N min” "
        "to set it exactly). I'll nudge you to head back."
    )


def _outing_return_confirmation(
    status: str, actual: float | None, window: float | None, outcome: str
) -> str:
    """One-line read-back for a closed outing."""
    if status != "returned":
        return "Outing closed (abandoned) — no worries, logged it."
    out = _fmt_minutes(actual)
    planned = _fmt_minutes(window)
    verdict = "on time 👍" if outcome == "success" else f"over the {planned} min you planned"
    return f"Welcome back — out {out} min, {verdict}."


def _focus_started_confirmation(task: str, minutes: float | None, aligned: bool) -> str:
    """One-line read-back for a started focus session."""
    bits = [f"Focus on “{task}” started"]
    if minutes is not None:
        bits.append(f"planned {_fmt_minutes(minutes)} min")
    bits.append("protected from nudges" if aligned else "not flagged as your intended task")
    return " — ".join(bits) + "."


def _focus_end_confirmation(status: str, actual: float | None, planned: float | None) -> str:
    """One-line read-back for a closed focus session."""
    if status != "ended":
        return "Focus session closed (abandoned) — logged it."
    out = _fmt_minutes(actual)
    if planned is not None:
        return f"Focus ended — {out} min on it (planned {_fmt_minutes(planned)})."
    return f"Focus ended — {out} min on it."


def _impulse_captured_confirmation(title: str) -> str:
    """One-line read-back for a captured-and-deferred impulse."""
    return f"Parked “{title}” — it's safe in your list. Back to what you were doing."


def _switch_resolved_confirmation(action: str, task: str, title: str | None) -> str:
    """One-line read-back for how a switch-impulse was resolved."""
    if action == "return":
        return f"Good call — back to “{task}.”"
    if action == "defer":
        return f"Parked “{title}” for later — staying on “{task}.”"
    return f"Switched off “{task}.” Logged it — go do the new thing."


#: Per-user delivery routing keys read from coaching state and returned to n8n
#: so a nudge reaches the right user's phone (the credentials stay global in
#: n8n; only the destination is per-user). See docs/multi-tenant.md §6.5.
_DELIVERY_KEYS = ("pushover_user_key", "twilio_to", "twilio_from", "ntfy_topic")


def _delivery_fields(memory: MemoryStore) -> dict[str, Any]:
    """Return ``{delivery: {...}, delivery_configured: bool}`` for the scoped user.

    n8n reads ``delivery`` to route a nudge to this user's target instead of a
    hardcoded credential; ``delivery_configured`` is ``False`` when none is set
    (so the dashboard can warn and n8n can fall back to the operator default).
    """
    delivery = {k: memory.get_state(k) for k in _DELIVERY_KEYS}
    return {
        "delivery": delivery,
        "delivery_configured": any(v for v in delivery.values()),
    }


def _dismiss_url(
    settings: Settings, handle: str, kind: str, target_id: int
) -> str:
    """Build a signed one-tap dismiss link for a nudge, or ``""`` if unavailable.

    Returns ``""`` unless both a public origin (``oauth_base_url``) and a signing
    key (``session_secret``) are configured — the link is opened from the phone
    off-box, so it needs the Tailscale HTTPS origin, and it must be signed so a
    tap can't be forged. n8n drops it into the Pushover ``url`` field.
    """
    if not settings.oauth_base_url or not settings.session_secret:
        return ""
    token = sign_dismiss(handle, kind, target_id, settings.session_secret)
    return f"{settings.oauth_base_url}/nudge/dismiss?t={token}"


def _dismiss_page(headline: str) -> str:
    """A tiny self-contained confirmation page shown after a one-tap dismiss."""
    safe = html.escape(headline)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Prefrontal</title></head>"
        "<body style='font-family:-apple-system,system-ui,sans-serif;"
        "display:flex;min-height:90vh;align-items:center;justify-content:center;"
        "text-align:center;color:#222;margin:0;padding:1.5rem'>"
        f"<div><div style='font-size:2.5rem'>✓</div><p style='font-size:1.15rem'>{safe}</p>"
        "<p style='color:#888;font-size:.9rem'>You can close this page.</p></div>"
        "</body></html>"
    )


def _decompose_and_store(
    memory: MemoryStore, todo_id: int, title: str, client: Any
) -> dict[str, Any]:
    """Generate a todo's first-step decomposition, persist it, and return it."""
    max_first = memory.get_float(
        "max_first_step_minutes", DEFAULT_MAX_FIRST_STEP_MINUTES
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


@dataclass(frozen=True)
class ScopedRequest:
    """The resolved identity + per-user store for an authenticated request.

    Produced by the :func:`resolve_user` dependency. ``store`` is already scoped
    to ``user`` (it injects ``user["id"]`` into every statement), so a handler
    cannot accidentally read or write another user's rows.
    """

    user: dict[str, Any]
    store: MemoryStore


def get_store(request: Request) -> MemoryStore:
    """FastAPI dependency returning the app's **unscoped** memory store.

    Used by the user-resolution layer and the admin surface. Defined at module
    level (not a closure) so that, with ``from __future__ import annotations`` in
    effect, FastAPI can resolve the ``Depends(get_store)`` annotation via
    ``get_type_hints``.
    """
    return request.app.state.store


def _resolve_user_row(
    request: Request, token: str | None
) -> dict[str, Any]:
    """Resolve an ``X-Prefrontal-Token`` value to an active user row.

    Resolution order:

    1. A blank/absent token resolves to ``PREFRONTAL_DEFAULT_USER`` when one is
       configured (the single-user / trusted-LAN compatibility mode); without a
       default user a token is required.
    2. A token whose ``sha256`` matches an active user resolves to that user.
    3. The legacy ``PREFRONTAL_WEBHOOK_SECRET`` resolves to the first operator
       user — a bootstrap so an operator can provision the first real tokens.

    Raises:
        HTTPException: 401 if no active user can be resolved.
    """
    store: MemoryStore = request.app.state.store
    settings: Settings = request.app.state.settings

    if not token:
        # Browser surfaces (dashboard/family) carry a Google sign-in session
        # cookie instead of a token header.
        cookie_user = session_user(request)
        if cookie_user is not None:
            return cookie_user
        if settings.default_user:
            row = store.get_user(settings.default_user)
            if row is not None and row["status"] == "active":
                return row
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Prefrontal-Token header.",
        )

    row = store.get_user_by_token_hash(sha256_hex(token))
    if row is not None and row["status"] == "active":
        return row

    # Bootstrap: the legacy shared secret maps to the first operator user, so a
    # fresh deployment can authenticate before any per-user token is minted.
    if settings.webhook_secret and hmac.compare_digest(token, settings.webhook_secret):
        for candidate in store.list_users():
            if candidate["is_operator"] and candidate["status"] == "active":
                return store.get_user(candidate["handle"])

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing X-Prefrontal-Token header.",
    )


def resolve_user(
    request: Request,
    x_prefrontal_token: Annotated[str | None, Header()] = None,
) -> ScopedRequest:
    """FastAPI dependency: resolve the request's token to a scoped store + user.

    Replaces the old shared-secret check on every data endpoint. Returns a
    :class:`ScopedRequest` carrying the user row and a store already scoped to
    that user, so handlers neither see another user's data nor have to remember
    a ``WHERE user_id = ?``.
    """
    user = _resolve_user_row(request, x_prefrontal_token)
    return ScopedRequest(user=user, store=request.app.state.store.scoped(user["id"]))


def require_operator(
    ctx: Annotated[ScopedRequest, Depends(resolve_user)],
) -> ScopedRequest:
    """Like :func:`resolve_user` but also requires the user be an operator (403)."""
    if not ctx.user.get("is_operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operator privileges required.",
        )
    return ctx


#: Per-request timeout (seconds) for the hot-path window inference. Kept short:
#: ``/outing/start`` is interactive (an iOS Shortcut waits on it), so a slow or
#: unreachable model must degrade to the heuristic fast rather than hang the tap.
INFER_TIMEOUT_SECONDS = 10.0

APP_VERSION = "0.1.0"

__all__ = [
    "ACTION_OUTCOME",
    "APP_VERSION",
    "Annotated",
    "AnthropicClient",
    "Any",
    "AssistantApply",
    "AssistantMessage",
    "BaseModel",
    "CalendarEvent",
    "CalendarSync",
    "CaptureImpulse",
    "CommitmentCreate",
    "CommitmentKind",
    "ConflictDismiss",
    "DASHBOARD_HTML",
    "DEFAULT_ABANDON_RATIO",
    "DEFAULT_ALERT_COOLDOWN_MINUTES",
    "DEFAULT_ALERT_MIN_PRESSING",
    "DEFAULT_BODY_DOUBLE_MIN_MISSES",
    "DEFAULT_DAY_END",
    "DEFAULT_DAY_START",
    "DEFAULT_FIT_CAP_MINUTES",
    "DEFAULT_FOCUS_ABANDON_RATIO",
    "DEFAULT_HARD_INTERRUPT_MINUTES",
    "DEFAULT_DEPARTURE_GRACE_MINUTES",
    "DEFAULT_HEADS_UP_MINUTES",
    "DEFAULT_HOME_RADIUS_M",
    "DEFAULT_MAX_FIRST_STEP_MINUTES",
    "DEFAULT_MIN_WINDOW_MINUTES",
    "DEFAULT_PAUSE_SECONDS",
    "DEFAULT_PREP_MINUTES",
    "DEFAULT_ROAD_FACTOR",
    "DEFAULT_SOFT_BLOCK_MINUTES",
    "DEFAULT_SOON_MINUTES",
    "DEFAULT_TRAVEL_SPEED_KMH",
    "Depends",
    "EpisodeCreated",
    "FAMILY_HTML",
    "FastAPI",
    "Field",
    "FocusEnd",
    "FocusStart",
    "FocusStarted",
    "HTMLResponse",
    "HTTPException",
    "Header",
    "INFER_TIMEOUT_SECONDS",
    "ImpulseCaptured",
    "KINDS",
    "LEVELS",
    "Literal",
    "LocationPing",
    "MAX_CATEGORIES",
    "MailSync",
    "MemoryStore",
    "N8nClient",
    "NominatimGeocoder",
    "OllamaClient",
    "OutingReturn",
    "OutingStart",
    "OutingStarted",
    "Path",
    "PlaceCreate",
    "PlainTextResponse",
    "Query",
    "Request",
    "Response",
    "SWITCH_ACTIONS",
    "ScopedRequest",
    "Settings",
    "ShortcutPayload",
    "StepDone",
    "SwitchImpulse",
    "SwitchPause",
    "SwitchResolve",
    "SwitchResolved",
    "TodoCategoryUpdate",
    "TodoCreate",
    "TodoDeadlineUpdate",
    "TodoWindowUpdate",
    "TriageForget",
    "UserCreate",
    "_DELIVERY_KEYS",
    "_EXACT_WINDOW_SOURCES",
    "_decompose_and_store",
    "_delivery_fields",
    "_dismiss_page",
    "_dismiss_url",
    "_fmt_minutes",
    "_focus_end_confirmation",
    "_focus_started_confirmation",
    "_impulse_captured_confirmation",
    "_outing_return_confirmation",
    "_outing_started_confirmation",
    "_parse_dt_or_none",
    "_resolve_user_row",
    "_switch_resolved_confirmation",
    "analyze_impact",
    "assistant_plan_message",
    "asynccontextmanager",
    "at_category_cap",
    "at_risk",
    "attribute_departure",
    "augment_todo",
    "available_now",
    "filter_suggestible",
    "avoided_todos",
    "build_briefing",
    "build_departure_message",
    "classify_departure",
    "record_departure_outcome",
    "build_focus_message",
    "body_double_message",
    "build_message",
    "build_panic",
    "build_pause_message",
    "build_profile",
    "build_snapshot",
    "cache_is_stale",
    "category_stats",
    "classify_kind",
    "conflict_dismissal_key",
    "dataclass",
    "datetime",
    "decompose_task",
    "enrich_commitments",
    "escalation_level",
    "execute_actions",
    "feed_label",
    "find_conflicts",
    "fit_todos",
    "focus_is_abandoned",
    "focus_level",
    "focus_level_rank",
    "format_window",
    "get_settings",
    "get_store",
    "haversine_m",
    "hmac",
    "html",
    "impact_phrase",
    "infer_capture_title",
    "infer_time_window",
    "ingest_messages",
    "is_abandoned",
    "is_at_home",
    "learned_corrections",
    "learned_denylist",
    "level_rank",
    "load_cached_summary",
    "local_datetime",
    "local_hour_of",
    "module_enabled",
    "next_departure",
    "normalize_category",
    "normalize_event",
    "normalize_query",
    "overwhelm_level",
    "panic_alert_message",
    "parse_inbound_event",
    "parse_time_window",
    "parse_window",
    "partition_conflicts",
    "pause_seconds",
    "pick_now",
    "plan_departure",
    "project_free_time",
    "provision_user",
    "record_drop_feedback",
    "record_focus_abandoned",
    "record_focus_end",
    "record_focus_switched",
    "record_outing_abandoned",
    "record_outing_return",
    "record_todo_closed",
    "refresh_profile_cache",
    "register_oauth_routes",
    "render_briefing",
    "render_panic",
    "repeat_stalled_tasks",
    "require_operator",
    "resolve_user",
    "session_user",
    "sha256_hex",
    "should_protect",
    "sign_dismiss",
    "status",
    "switch_response",
    "sync_calendar",
    "timedelta",
    "to_utc",
    "utcnow",
    "validate_actions",
    "verify_dismiss",
    "window_config_for",
    "work_window_now",
]
