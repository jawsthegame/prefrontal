"""Shared surface for the webhook layer's per-tag routers.

The routes live in per-tag modules under :mod:`prefrontal.webhooks.routers`, and
the app is assembled by :func:`prefrontal.webhooks.app.create_app`. This module
holds everything those routers share, so each router imports from one place
rather than re-declaring it:

- **Pydantic request/response models** — now defined in
  :mod:`prefrontal.webhooks.schemas` and re-exported here for the routers (the
  first slice of unwinding this grab-bag; dependencies and helpers follow next).
- **Request dependencies & identity** — now in :mod:`prefrontal.webhooks.deps`
  (:class:`ScopedRequest`, :func:`resolve_user`, :func:`require_operator`,
  :func:`require_member`, :func:`get_store`) and re-exported here for the routers.
- **Small formatting/URL helpers** — now in :mod:`prefrontal.webhooks.helpers`
  (nudge read-backs, dismiss links, ``_delivery_fields``) and re-exported here.
  A couple of constants (``DASHBOARD_HTML``, ``APP_VERSION``, …) still live here.
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
from prefrontal.clock import parse_ts as _parse_dt_or_none
from prefrontal.coaching import in_quiet_hours, resolve_ack
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
    DEFAULT_WORK_LEAD_MINUTES,
    attribute_departure,
    build_departure_message,
    classify_departure,
    next_departure,
    plan_departure,
    record_departure_outcome,
)
from prefrontal.geocode import enrich_commitments, normalize_query
from prefrontal.household import build_sheet, render_sheet
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
    focus_task_from_title,
    infer_focus_start,
    is_focus_intent_title,
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
    apply_outing_evaluation,
    build_message,
    escalation_level,
    evaluate_outing,
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
    DEFAULT_PANIC_STEP_ACK_WINDOW_MINUTES,
    build_panic,
    overwhelm_level,
    panic_alert_message,
    record_panic_step_sent,
    render_panic,
    resolve_panic_step,
    sweep_pending_panic_steps,
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
from prefrontal.sensor import (
    apply_proposal,
    extract_candidates,
    extract_candidates_from_transcript,
    record_candidates,
    summarize_candidate,
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
from prefrontal.triage import (
    apply as triage_apply,
)
from prefrontal.triage import (
    classify as triage_classify,
)
from prefrontal.triage import (
    signal_from_payload,
)
from prefrontal.trips import (
    TRIP_CATEGORIES,
    apply_reflection,
    normalize_trip_category,
    process_location,
)
from prefrontal.webhooks.deps import (  # noqa: F401 (re-exported for routers)
    ScopedRequest,
    get_store,
    require_member,
    require_operator,
    resolve_user,
)
from prefrontal.webhooks.helpers import (  # noqa: F401 (re-exported for routers)
    _decompose_and_store,
    _delivery_fields,
    _dismiss_page,
    _dismiss_url,
    _fmt_minutes,
    _focus_end_confirmation,
    _focus_started_confirmation,
    _impulse_captured_confirmation,
    _nudge_actions,
    _outing_return_confirmation,
    _outing_started_confirmation,
    _switch_resolved_confirmation,
)
from prefrontal.webhooks.notify import nudge_actions, panic_actions
from prefrontal.webhooks.oauth import (
    NUDGE_ACTIONS,
    register_oauth_routes,
    session_user,
    sign_dismiss,
    verify_action,
    verify_dismiss,
)
from prefrontal.webhooks.schemas import (  # noqa: F401 (re-exported for routers)
    AgreementSet,
    AppointmentCreate,
    AssistantApply,
    AssistantMessage,
    BalanceConfig,
    CalendarEvent,
    CalendarSync,
    CaptureImpulse,
    CheckinConfig,
    ChildCreate,
    ChildRename,
    ChoreEnabled,
    ChoreSet,
    CommitmentCreate,
    CommitmentKind,
    ConflictDismiss,
    ConversationTurn,
    DigestConfig,
    EpisodeCreated,
    FactClear,
    FactSet,
    FocusEnd,
    FocusLog,
    FocusStart,
    FocusStarted,
    HomeSet,
    HouseholdCreate,
    HouseholdMember,
    ImpulseCaptured,
    InviteRedeem,
    LocationPing,
    MailSync,
    ObserveRequest,
    OutingReturn,
    OutingStart,
    OutingStarted,
    PlaceCreate,
    PromptConfig,
    RoutineEnabled,
    RoutineSet,
    ShoppingAdd,
    ShoppingGot,
    ShortcutPayload,
    StarAward,
    StepDone,
    SwitchImpulse,
    SwitchPause,
    SwitchResolve,
    SwitchResolved,
    TierConfig,
    TodoCategoryUpdate,
    TodoCreate,
    TodoDeadlineUpdate,
    TodoDomainUpdate,
    TodoWindowUpdate,
    TriageForget,
    TriageIn,
    TripLabel,
    TripReflect,
    UserCreate,
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
#: The editable kids dashboard for the shared household sheet.
KIDS_HTML = (Path(__file__).with_name("kids.html")).read_text(encoding="utf-8")
#: The behavioral Insights page (charts over episodes; reads GET /stats/data).
STATS_HTML = (Path(__file__).with_name("stats.html")).read_text(encoding="utf-8")
#: The LLM-sensor review page (jot a note → confirm proposals; reads/writes
#: GET /proposals + POST /observe + POST /proposals/{id}/accept|reject).
REVIEW_HTML = (Path(__file__).with_name("review.html")).read_text(encoding="utf-8")



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
    "DEFAULT_PANIC_STEP_ACK_WINDOW_MINUTES",
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
    "DEFAULT_WORK_LEAD_MINUTES",
    "Depends",
    "EpisodeCreated",
    "ConversationTurn",
    "ObserveRequest",
    "apply_proposal",
    "extract_candidates",
    "extract_candidates_from_transcript",
    "record_candidates",
    "summarize_candidate",
    "FAMILY_HTML",
    "FastAPI",
    "Field",
    "FocusEnd",
    "FocusLog",
    "FocusStart",
    "FocusStarted",
    "AgreementSet",
    "AppointmentCreate",
    "ChildCreate",
    "RoutineSet",
    "RoutineEnabled",
    "ChildRename",
    "FactClear",
    "FactSet",
    "HTMLResponse",
    "HTTPException",
    "Header",
    "HouseholdCreate",
    "HouseholdMember",
    "InviteRedeem",
    "KIDS_HTML",
    "STATS_HTML",
    "REVIEW_HTML",
    "INFER_TIMEOUT_SECONDS",
    "ImpulseCaptured",
    "KINDS",
    "LEVELS",
    "Literal",
    "LocationPing",
    "HomeSet",
    "TripLabel",
    "TripReflect",
    "TRIP_CATEGORIES",
    "normalize_trip_category",
    "process_location",
    "apply_reflection",
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
    "NUDGE_ACTIONS",
    "Request",
    "Response",
    "SWITCH_ACTIONS",
    "ScopedRequest",
    "Settings",
    "ShortcutPayload",
    "ShoppingAdd",
    "ShoppingGot",
    "BalanceConfig",
    "CheckinConfig",
    "DigestConfig",
    "PromptConfig",
    "TierConfig",
    "StarAward",
    "StepDone",
    "SwitchImpulse",
    "SwitchPause",
    "SwitchResolve",
    "SwitchResolved",
    "TodoCategoryUpdate",
    "TodoCreate",
    "TodoDomainUpdate",
    "TodoDeadlineUpdate",
    "TodoWindowUpdate",
    "TriageForget",
    "TriageIn",
    "UserCreate",
    "_decompose_and_store",
    "_delivery_fields",
    "_dismiss_page",
    "_dismiss_url",
    "_nudge_actions",
    "_fmt_minutes",
    "_focus_end_confirmation",
    "_focus_started_confirmation",
    "_impulse_captured_confirmation",
    "_outing_return_confirmation",
    "_outing_started_confirmation",
    "_parse_dt_or_none",
    "_switch_resolved_confirmation",
    "analyze_impact",
    "apply_outing_evaluation",
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
    "infer_focus_start",
    "is_focus_intent_title",
    "focus_task_from_title",
    "body_double_message",
    "build_message",
    "build_panic",
    "build_sheet",
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
    "evaluate_outing",
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
    "in_quiet_hours",
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
    "nudge_actions",
    "overwhelm_level",
    "panic_actions",
    "panic_alert_message",
    "record_panic_step_sent",
    "resolve_panic_step",
    "sweep_pending_panic_steps",
    "parse_inbound_event",
    "signal_from_payload",
    "triage_apply",
    "triage_classify",
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
    "render_sheet",
    "repeat_stalled_tasks",
    "require_operator",
    "require_member",
    "resolve_ack",
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
    "verify_action",
    "verify_dismiss",
    "window_config_for",
    "work_window_now",
]
