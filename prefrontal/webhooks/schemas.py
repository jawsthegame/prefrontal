"""Pydantic request/response models for the webhook layer.

Every ``/webhooks`` and dashboard endpoint's request/response shape lives here —
extracted from the shared ``_common`` module so the models have a home of their
own rather than swelling the router grab-bag. Routers import them (today still
re-exported through ``_common`` for compatibility); nothing here imports the
domain layer, so this module stays a leaf with a tiny import graph.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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

    intended_task: str = Field(
        default="",
        description=(
            "What you're getting into, e.g. 'the API refactor'. Leave blank for a "
            "one-tap start — the server infers it from your top open todo."
        ),
    )
    planned_minutes: float | None = Field(
        default=None,
        description="Optional intended duration; the point past which a gentle check fires.",
    )
    aligned: bool = Field(
        default=True,
        description="Whether this is the thing you meant to be doing (the protect bit).",
    )
    todo_id: int | None = Field(
        default=None,
        description=(
            "Optional id of the todo this block is working. Its energy/category "
            "tag the close episode so time-estimation bias can condition on them."
        ),
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


class FocusLog(BaseModel):
    """Body of ``POST /webhooks/focus/log`` — recording a *past* focus block.

    For a session you forgot to start: it's logged as already finished (started
    ``minutes`` ago, closed now) so the block still feeds the learning loop.
    """

    minutes: float = Field(gt=0, le=1440, description="How long you were heads-down, in minutes.")
    intended_task: str = Field(
        default="", description="What you were on; blank infers it from your top open todo."
    )
    aligned: bool = Field(default=True, description="Was it the thing you meant to be doing?")
    outcome: Literal["worth_it", "should_have_stopped", "pulled_off"] | None = Field(
        default=None, description="Optional one-tap rating of how the block went."
    )
    todo_id: int | None = Field(default=None, description="Optional linked todo id.")


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
    actions: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Signed one-tap ntfy action buttons (Stay / Park it / Switch anyway); "
            "empty unless a public origin + signing key are configured."
        ),
    )


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


class HomeSet(BaseModel):
    """Body of ``POST /webhooks/home`` — the home coordinate for trip detection."""

    lat: float = Field(description="Home latitude in degrees.")
    lon: float = Field(description="Home longitude in degrees.")


class TripLabel(BaseModel):
    """Body of ``POST /webhooks/trip/label`` — name and categorize a closed trip."""

    trip_id: int = Field(description="The completed trip to label.")
    label: str = Field(description="What the trip was, e.g. 'Target run'.")
    category: str | None = Field(
        default=None,
        description="Optional category (errand/social/work/health/family/leisure/other).",
    )


class TripReflect(BaseModel):
    """Body of ``POST /webhooks/trip/reflect`` — an honest 'how it went' note."""

    trip_id: int = Field(description="The completed trip to reflect on.")
    reflection: str = Field(description="Plain-English note on how the trip went.")
    outcome: Literal["success", "partial", "miss"] | None = Field(
        default=None,
        description="Optional explicit outcome; omit to let it be classified from the note.",
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


class TriageIn(BaseModel):
    """Body of ``POST /triage`` — one normalized inbound signal to classify + route."""

    title: str = Field(description="Subject line / event title / short capture.")
    body: str = Field(default="", description="Email body, event notes, etc.")
    source: str = Field(default="manual", description="mail | calendar | shortcut | n8n | manual.")
    sender: str = Field(default="", description="From-address / origin (sender-trust heuristics).")
    external_id: str = Field(default="", description="Provider id, for idempotent re-delivery.")
    received_at: str = Field(default="", description="ISO8601 receipt time; defaults to now.")
    meta: dict[str, Any] = Field(default_factory=dict, description="Raw provider extras.")


class UserCreate(BaseModel):
    """Body of ``POST /admin/users`` — provision a user (operator-only)."""

    handle: str = Field(description="Unique short handle, e.g. 'sam'.")
    display_name: str | None = Field(
        default=None, description="Name shown in nudges/briefings."
    )
    is_operator: bool = Field(
        default=False, description="Whether the user may call the admin surface."
    )


class HouseholdCreate(BaseModel):
    """Body of ``POST /admin/households`` — create a household (operator-only)."""

    name: str = Field(description="Household name, e.g. 'The Kims'.")


class HouseholdMember(BaseModel):
    """Body of ``POST /admin/households/{id}/members`` — add a user (operator-only)."""

    handle: str = Field(description="Handle of the user to put into the household.")


class ChildCreate(BaseModel):
    """Body of ``POST /household/children`` — add a kid to the roster."""

    name: str = Field(description="The child's name (unique within the household).")
    birthday: str | None = Field(default=None, description="Optional ISO date (YYYY-MM-DD).")


class ChildRename(BaseModel):
    """Body of ``POST /household/children/{id}`` — rename / set birthday."""

    name: str = Field(description="The child's new name.")
    birthday: str | None = Field(default=None, description="Optional ISO date; omit to keep.")


class FactSet(BaseModel):
    """Body of ``POST /household/facts`` — upsert one per-kid (or household-wide) fact."""

    category: str = Field(description="One of the controlled fact categories.")
    item: str = Field(description="The field, e.g. 'shoe size' (normalized).")
    value: str | None = Field(default=None, description="Free-text value; null clears the value.")
    child_id: int = Field(default=0, description="A children.id, or 0 for household-wide.")


class FactClear(BaseModel):
    """Body of ``POST /household/facts/clear`` — delete one fact."""

    category: str = Field(description="The fact's category.")
    item: str = Field(description="The fact's item.")
    child_id: int = Field(default=0, description="A children.id, or 0 for household-wide.")


class AgreementSet(BaseModel):
    """Body of ``POST /household/agreements`` — upsert a standing plan."""

    title: str = Field(description="Plan title (unique per child within the household).")
    body: str | None = Field(default=None, description="The plan in plain language.")
    kind: str = Field(default="consistency", description="reward | consistency | routine.")
    child_id: int = Field(default=0, description="A children.id, or 0 for the whole household.")
    structured: dict[str, Any] | None = Field(
        default=None, description="Optional star/points chart JSON (thresholds → rewards)."
    )


class StarAward(BaseModel):
    """Body of ``POST /household/agreements/{id}/stars`` — record earned stars."""

    delta: int = Field(
        default=1,
        description="Stars to add (negative to correct, unless the chart is earn-only).",
    )
    note: str | None = Field(
        default=None, description="Optional 'what for' note, e.g. 'tidied room unprompted'."
    )


class PromptConfig(BaseModel):
    """Body of ``POST /household/agreements/{id}/prompt`` — the award-prompt schedule."""

    enabled: bool = Field(default=True, description="Whether the recurring prompt fires.")
    days: list[int] = Field(
        default_factory=list,
        description="Weekdays to ask on: 0=Mon … 6=Sun (all seven = daily).",
    )
    time: str = Field(description="Local time of day, 'HH:MM' 24-hour, e.g. '19:30'.")
    question: str | None = Field(
        default=None, description="Optional custom question; a default is used if omitted."
    )


class TierConfig(BaseModel):
    """Body of ``POST /household/agreements/{id}/tiers`` — the reward-tier spec."""

    tiers: str = Field(
        description="Comma-separated 'count=reward' tiers, e.g. '7=small LEGO, 30=large'.",
    )


class CheckinConfig(BaseModel):
    """Body of ``POST /household/checkin`` — the weekly mental-load check-in schedule."""

    enabled: bool = Field(default=False, description="Opt in to the gentle weekly check-in.")
    day: int | None = Field(default=None, description="Weekday to ask on: 0=Mon … 6=Sun.")
    time: str | None = Field(default=None, description="Local time of day, 'HH:MM' 24-hour.")


class DigestConfig(BaseModel):
    """Body of ``POST /household/digest`` — toggle the opt-in daily delta digest."""

    enabled: bool = Field(default=False, description="Opt in to the daily 'what changed' digest.")


class BalanceConfig(BaseModel):
    """Body of ``POST /household/balance`` — toggle the opt-in load-balance view."""

    enabled: bool = Field(
        default=False, description="Opt in to the gentle 'who's keeping the sheet up' view."
    )


class InviteRedeem(BaseModel):
    """Body of ``POST /household/invites/redeem`` — join a household with a code."""

    code: str = Field(description="The invite code shared by a co-parent, e.g. 'PLUM-7F2Q'.")


class ShoppingAdd(BaseModel):
    """Body of ``POST /household/shopping`` — add a thing to buy."""

    item: str = Field(description="What to buy, e.g. 'shoes'.")
    spec: str | None = Field(default=None, description="Size / brand / details.")
    where_to_buy: str | None = Field(default=None, description="Where to get it.")
    child_id: int = Field(default=0, description="A children.id, or 0 for household-wide.")


class ShoppingGot(BaseModel):
    """Body of ``POST /household/shopping/{id}/got`` — check an item off (or un-check)."""

    got: bool = Field(default=True, description="True = bought, false = still needed.")


class ChoreSet(BaseModel):
    """Body of ``POST /household/chores`` — upsert a recurring shared chore."""

    title: str = Field(description="What has to happen, e.g. 'run the dishwasher'.")
    due_time: str = Field(
        default="",
        description=(
            "Local time it should be done by, 'HH:MM' 24-hour. Blank = inherit the "
            "routine's time, or run untimed (a checklist chore, no reminder)."
        ),
    )
    days: list[int] = Field(
        default_factory=list,
        description="Weekdays it recurs on: 0=Mon … 6=Sun. Empty = inherit routine / every day.",
    )
    owner_id: int | None = Field(
        default=None,
        description="RACI 'R' — a member's user id whose job it is; null = either parent.",
    )
    routine_id: int | None = Field(
        default=None,
        description="Routine this chore belongs to (inherits its schedule); null = stands alone.",
    )
    remind_before: int = Field(
        default=30, description="Minutes before the due time to nudge the owner."
    )
    impact: str | None = Field(
        default=None,
        description="Why it matters if it slips, e.g. 'it makes the morning harder'.",
    )
    enabled: bool = Field(default=True, description="Whether the chore's reminders fire.")


class ChoreEnabled(BaseModel):
    """Body of ``POST /household/chores/{id}/enabled`` — pause or resume a chore."""

    enabled: bool = Field(default=True, description="True = active, false = paused.")


class RoutineSet(BaseModel):
    """Body of ``POST /household/routines`` — upsert a routine (grouping + accountability)."""

    title: str = Field(description="What the routine is, e.g. 'Monday pickup prep'.")
    accountable_id: int | None = Field(
        default=None,
        description="RACI 'A' — the member who holds the mental load; null = unassigned.",
    )
    due_time: str = Field(
        default="",
        description="Local 'HH:MM' its chores inherit; blank = not time-tied (just a grouping).",
    )
    days: list[int] = Field(
        default_factory=list,
        description="Weekdays it recurs on: 0=Mon … 6=Sun. Empty = every day.",
    )
    impact: str | None = Field(
        default=None, description="Why the routine matters if it slips."
    )
    enabled: bool = Field(
        default=True, description="Whether the routine (and its inherited schedule) is active."
    )


class RoutineEnabled(BaseModel):
    """Body of ``POST /household/routines/{id}/enabled`` — pause or resume a routine."""

    enabled: bool = Field(default=True, description="True = active, false = paused.")


class AppointmentCreate(BaseModel):
    """Body of ``POST /household/appointments`` — add a kid appointment.

    Stored as a ``kind='child'`` commitment on the acting parent's calendar, which
    the shared sheet then surfaces in its 'upcoming' section.
    """

    title: str = Field(description="e.g. 'Sam dentist'.")
    start_at: str = Field(description="ISO-8601 start (local unless offset-aware).")
    end_at: str | None = Field(default=None, description="Optional ISO-8601 end.")
    location: str | None = Field(default=None, description="Optional location.")


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


class TodoDomainUpdate(BaseModel):
    """Body of ``POST /todos/{id}/domain`` — set or clear a todo's life domain."""

    domain: str | None = Field(
        default=None,
        description=(
            "Life domain (work / home / …) — the work/life guardrail; it outranks "
            "the category for the time band. Null clears it."
        ),
    )


class ConversationTurn(BaseModel):
    """One turn of a conversation transcript fed to ``POST /observe``."""

    speaker: str = Field(
        default="",
        description="Who spoke this turn (e.g. 'me', 'coach'). Blank renders as '?'.",
    )
    text: str = Field(description="What was said this turn.")


class ObserveRequest(BaseModel):
    """Body of ``POST /observe`` — a note *or* a transcript for the LLM sensor.

    Provide ``text`` (a single free-text note) or ``transcript`` (a multi-turn
    conversation); a transcript, when present and non-empty, takes precedence.
    Either way the sensor only *proposes* allowlisted candidate updates that land
    as pending proposals for human review — it never writes authoritative facts.
    """

    text: str = Field(
        default="",
        description=(
            "A short free-text note / observation. Optional when ``transcript`` is "
            "given."
        ),
    )
    transcript: list[ConversationTurn] = Field(
        default_factory=list,
        description=(
            "A conversation transcript (turns of speaker + text). When non-empty "
            "the sensor reads the whole conversation and attributes signal to the "
            "user, instead of reading ``text``."
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


__all__ = [
    "AgreementSet",
    "AppointmentCreate",
    "AssistantApply",
    "AssistantMessage",
    "BalanceConfig",
    "CalendarEvent",
    "CalendarSync",
    "CaptureImpulse",
    "CheckinConfig",
    "ChildCreate",
    "ChildRename",
    "ChoreEnabled",
    "ChoreSet",
    "CommitmentCreate",
    "CommitmentKind",
    "ConflictDismiss",
    "ConversationTurn",
    "DigestConfig",
    "EpisodeCreated",
    "FactClear",
    "FactSet",
    "FocusEnd",
    "FocusLog",
    "FocusStart",
    "FocusStarted",
    "HomeSet",
    "HouseholdCreate",
    "HouseholdMember",
    "ImpulseCaptured",
    "InviteRedeem",
    "LocationPing",
    "MailSync",
    "ObserveRequest",
    "OutingReturn",
    "OutingStart",
    "OutingStarted",
    "PlaceCreate",
    "PromptConfig",
    "RoutineEnabled",
    "RoutineSet",
    "ShoppingAdd",
    "ShoppingGot",
    "ShortcutPayload",
    "StarAward",
    "StepDone",
    "SwitchImpulse",
    "SwitchPause",
    "SwitchResolve",
    "SwitchResolved",
    "TierConfig",
    "TodoCategoryUpdate",
    "TodoCreate",
    "TodoDeadlineUpdate",
    "TodoDomainUpdate",
    "TodoWindowUpdate",
    "TriageForget",
    "TriageIn",
    "TripLabel",
    "TripReflect",
    "UserCreate",
]
