"""Calendar-sync, commitment, and place schemas.

Split from the webhooks schemas god-module (audit #408).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


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
    notes: str | None = Field(
        default=None,
        description=(
            "Optional free-text detail (e.g. 'bring the insurance card'). "
            "Consulted when a nudge is built for this commitment, like the "
            "departure reminder."
        ),
    )
    dest_lat: float | None = Field(
        default=None, description="Destination latitude (enables travel estimation)."
    )
    dest_lon: float | None = Field(default=None, description="Destination longitude.")
    lead_minutes: float | None = None
    hard: bool = False
    domain: str | None = Field(
        default=None,
        description=(
            "Optional life-sphere (shop/work/home/kids/personal) — the same axis "
            "todos and trips carry. Snapped onto the canonical vocab (e.g. "
            "child/family → kids), so a kid's appointment is domain='kids'."
        ),
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

    kind: str = Field(
        description=(
            "`self` (your commitment), `child` (a kid's appointment — also shows on "
            "the household sheet), or `fyi` (where someone else will be)."
        )
    )

class CommitmentHardness(BaseModel):
    """Body of ``POST /commitments/{id}/hardness`` — set a commitment's firmness."""

    hardness: str = Field(
        description=(
            "`hard` (a firm, must-happen obligation) or `soft` (an elastic/optional "
            "block). Marks a user override that survives calendar re-syncs."
        ),
    )

class CommitmentHidden(BaseModel):
    """Body of ``POST /commitments/{id}/hidden`` — hide or un-hide a commitment."""

    hidden: bool = Field(
        default=True,
        description="`true` hides the commitment from every surface; `false` un-hides it.",
    )

class CommitmentOutcome(BaseModel):
    """Body of ``POST /commitments/{id}/outcome`` — record made/missed on a past commitment."""

    outcome: str | None = Field(
        description="`made` or `missed` (self-report on an elapsed commitment); `null` clears it.",
    )

class CommitmentPrepared(BaseModel):
    """Body of ``POST /commitments/{id}/prepared`` — the "felt prepared?" reflection."""

    prepared: str | None = Field(
        description=(
            "`yes` or `no` (did you feel prepared for this work commitment?); "
            "`null` clears it."
        ),
    )

class CommitmentNotes(BaseModel):
    """Body of ``POST /commitments/{id}/notes`` — set or clear a commitment's notes."""

    notes: str | None = Field(
        default=None,
        description=(
            "Free-text detail consulted when a nudge is built for this commitment "
            "(e.g. the departure reminder); `null`/empty clears it. Survives "
            "calendar re-syncs."
        ),
    )

class CommitmentDomain(BaseModel):
    """Body of ``POST /commitments/{id}/domain`` — set or clear a commitment's life-sphere."""

    domain: str | None = Field(
        default=None,
        description=(
            "Life-sphere (shop/work/home/kids/personal), snapped onto the canonical "
            "vocab (e.g. child/family → kids); `null`/empty clears it. A user field "
            "kept across calendar re-syncs."
        ),
    )
