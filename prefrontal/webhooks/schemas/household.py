"""Household schemas (children, pets, facts, agreements, chores, routines, ...).

Split from the webhooks schemas god-module (audit #408).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChildCreate(BaseModel):
    """Body of ``POST /household/children`` — add a kid to the roster."""

    name: str = Field(description="The child's name (unique within the household).")
    birthday: str | None = Field(default=None, description="Optional ISO date (YYYY-MM-DD).")

class ChildRename(BaseModel):
    """Body of ``POST /household/children/{id}`` — rename / set birthday."""

    name: str = Field(description="The child's new name.")
    birthday: str | None = Field(default=None, description="Optional ISO date; omit to keep.")

class PetCreate(BaseModel):
    """Body of ``POST /household/pets`` — add a pet to the roster."""

    name: str = Field(description="The pet's name (unique within the household).")
    species: str | None = Field(default=None, description="Optional species, e.g. 'dog'.")
    birthday: str | None = Field(default=None, description="Optional ISO date (YYYY-MM-DD).")

class PetRename(BaseModel):
    """Body of ``POST /household/pets/{id}`` — rename / set species / birthday."""

    name: str = Field(description="The pet's new name.")
    species: str | None = Field(default=None, description="Optional species; omit to keep.")
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

class InviteCreate(BaseModel):
    """Body of ``POST /household/invites`` — optionally text the link to a co-parent.

    All fields optional so an empty POST still just mints a code (the original
    behaviour); supplying ``sms_to`` also texts the join link via Twilio.
    """

    sms_to: str | None = Field(
        default=None,
        description="Recipient phone number (E.164, e.g. '+14155551234') to text the invite to.",
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
    month_days: list[int] = Field(
        default_factory=list,
        description=(
            "Days of the month it recurs on: 1 … 31 (a day past a short month fires "
            "on its last day). When set, takes precedence over `days`. Empty = none."
        ),
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
    away_behavior: str = Field(
        default="keep",
        description=(
            "What happens while the household is away (see the away window): 'keep' "
            "(default — bills/meds still fire) or 'suppress' (a location-bound chore "
            "like trash/mail/plants, skipped on vacation)."
        ),
    )
    service: str | None = Field(
        default=None,
        description=(
            "Optional municipal service this chore tracks (e.g. 'trash', 'recycling') "
            "whose pickup day can shift on a holiday week; the reminder follows the "
            "shift. Null = an ordinary chore."
        ),
    )

class ChoreEnabled(BaseModel):
    """Body of ``POST /household/chores/{id}/enabled`` — pause or resume a chore."""

    enabled: bool = Field(default=True, description="True = active, false = paused.")

class ChoreDone(BaseModel):
    """Body of ``POST /household/chores/{id}/done`` and ``/undone`` — which day.

    ``days_ago`` is 0 for today or 1 for yesterday; the server resolves it to a
    local date so the client needn't know the deployment's timezone. The card's
    day selector only reaches one day back, so anything outside {0, 1} is
    rejected (422) rather than silently clamped.
    """

    days_ago: int = Field(
        default=0, ge=0, le=1, description="0 = today, 1 = yesterday."
    )

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
    month_days: list[int] = Field(
        default_factory=list,
        description=(
            "Days of the month it recurs on: 1 … 31 (a day past a short month fires "
            "on its last day). When set, takes precedence over `days`. Empty = none."
        ),
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
