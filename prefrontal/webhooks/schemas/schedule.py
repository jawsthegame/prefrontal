"""Calendar-sync, commitment, and place schemas.

Split from the webhooks schemas god-module (audit #408).
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from prefrontal.scheduling import WEEKDAYS

#: Regex for a zero-padded 24-hour ``HH:MM`` clock time. Zero-padding lets the
#: start < end check be a plain string comparison (lexical == chronological).
_HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


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

class RescheduleRequest(BaseModel):
    """Body of ``POST /commitments/conflicts/reschedule``.

    Resolve a double-booking by asking one side to move: identify the conflicting
    pair by its ``key`` (from ``GET /commitments/conflicts``), optionally choose
    which listed side to move, and draft — or, with ``send``, actually email — a
    polite reschedule request to the other party.
    """

    key: str = Field(description="The conflict pair's `key` from the conflicts list.")
    move: str | None = Field(
        default=None,
        description=(
            "Which side of the listed pair to move: `a` or `b`. Omit to let the "
            "server suggest one (it prefers moving the softer/later appointment)."
        ),
    )
    to: str | None = Field(
        default=None,
        description=(
            "Recipient email for the reschedule notice. Required to actually `send`; "
            "for a preview (the default) it may be omitted."
        ),
    )
    recipient_name: str | None = Field(
        default=None, description="The other party's display name, used in the draft."
    )
    note: str | None = Field(
        default=None,
        description="Optional cover note from the user folded into the request.",
    )
    offer_slots: bool = Field(
        default=True,
        description="Offer a few of your open times as alternatives in the draft.",
    )
    send: bool = Field(
        default=False,
        description=(
            "`false` (default) previews the draft without sending — the "
            "confirm-first path. `true` emails it over your SMTP (requires `to`) and, "
            "on success, dismisses the conflict so it stops re-alerting."
        ),
    )

    @field_validator("move")
    @classmethod
    def _valid_side(cls, value: str | None) -> str | None:
        if value is not None and value.strip().lower() not in ("a", "b"):
            raise ValueError("move must be 'a' or 'b'")
        return value.strip().lower() if value is not None else None


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

class TravelPadding(BaseModel):
    """Body of ``POST /departure/padding`` — the distance-relative travel cushion."""

    percent: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "Safety padding added to every travel estimate, as a percentage of the "
            "drive (so a longer trip gets proportionally more). E.g. `15` pads a "
            "40-min drive by 6 min and a 10-min one by 1.5. `0` turns it off. Stored "
            "as the `travel_pad_fraction` coaching key (percent ÷ 100), marked an "
            "explicit user choice. Required unless `auto` is set."
        ),
    )
    auto: bool = Field(
        default=False,
        description=(
            "Hand the pad back to the learner: clear the manual override so the "
            "learning pass sets it from your departure lateness again. When true, "
            "`percent` is ignored."
        ),
    )
    autolearn: bool | None = Field(
        default=None,
        description=(
            "Master switch for auto-populating the pad from your departure "
            "lateness. `false` freezes it; `true` re-enables learning. When set, "
            "`percent`/`auto` are ignored — it toggles the setting only."
        ),
    )


class DayAvailability(BaseModel):
    """One weekday's availability in ``GET/POST /schedule/available-hours``.

    An available day carries a within-day ``start``/``end`` waking band that the
    slot-finder (and todo suggester) work inside for that weekday; an unavailable
    day (``available=false``) yields no slots at all. ``start``/``end`` are kept
    even when unavailable so toggling a day back on restores its last band.
    """

    available: bool = Field(
        default=True,
        description="Whether anything is scheduled on this weekday at all.",
    )
    start: str = Field(
        default="09:00",
        pattern=_HHMM_PATTERN,
        description="Local start of the available window, `HH:MM` (24-hour).",
    )
    end: str = Field(
        default="17:00",
        pattern=_HHMM_PATTERN,
        description="Local end of the available window, `HH:MM`; must be after `start`.",
    )

    @model_validator(mode="after")
    def _end_after_start(self) -> DayAvailability:
        # Only meaningful for an available day; an off day's band is inert. Both
        # values are zero-padded HH:MM, so a string compare is chronological.
        if self.available and self.start >= self.end:
            raise ValueError("`end` must be later than `start` for an available day")
        return self


class AvailableHours(BaseModel):
    """Body/response of ``/schedule/available-hours`` — the per-weekday schedule.

    ``days`` maps weekday keys (`mon`…`sun`, the canonical
    :data:`prefrontal.scheduling.WEEKDAYS`) to their :class:`DayAvailability`. A
    write may be partial: only the weekdays present are updated, the rest keep
    their stored value. A read always returns all seven.
    """

    days: dict[str, DayAvailability] = Field(
        default_factory=dict,
        description="Weekday (`mon`…`sun`) → that day's availability.",
    )

    @field_validator("days")
    @classmethod
    def _known_weekdays(cls, value: dict[str, DayAvailability]) -> dict[str, DayAvailability]:
        unknown = sorted(set(value) - set(WEEKDAYS))
        if unknown:
            raise ValueError(
                f"unknown weekday key(s): {', '.join(unknown)}; "
                f"expected any of {', '.join(WEEKDAYS)}"
            )
        return value
