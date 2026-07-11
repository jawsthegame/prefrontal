"""Ingestion webhook payloads (shortcut, location, trips, mail, triage).

Split from the webhooks schemas god-module (audit #408).
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
    domain: str | None = Field(
        default=None,
        description="Optional life-domain for focus balance (shop/work/home/kids/personal).",
    )

class TripDomain(BaseModel):
    """Body of ``POST /webhooks/trip/domain`` — set/clear a trip's life-domain."""

    trip_id: int = Field(description="The trip to (re)file into a life-domain.")
    domain: str | None = Field(
        default=None,
        description="Life-domain (shop/work/home/kids/personal); omit or null to clear.",
    )

class TripReflect(BaseModel):
    """Body of ``POST /webhooks/trip/reflect`` — an honest 'how it went' note."""

    trip_id: int = Field(description="The completed trip to reflect on.")
    reflection: str = Field(description="Plain-English note on how the trip went.")
    outcome: Literal["success", "partial", "miss"] | None = Field(
        default=None,
        description="Optional explicit outcome; omit to let it be classified from the note.",
    )

class TripRetro(BaseModel):
    """Body of ``POST /webhooks/trip/retro`` — close a trip's whole retrospective.

    The one-interaction form of label + domain + reflection: an iOS Shortcut can
    gather all three and post once, instead of chaining ``/trip/label``,
    ``/trip/domain``, and ``/trip/reflect``. Every field is optional (send only
    what you captured), but at least one of ``label``/``domain``/``reflection`` is
    required. ``trip_id`` defaults to the most recent completed trip still awaiting
    a label, so a bare-tap Shortcut needn't know the id.
    """

    trip_id: int | None = Field(
        default=None,
        description="The trip to close out; omit to use the newest unlabeled trip.",
    )
    label: str | None = Field(default=None, description="What the trip was, e.g. 'Target run'.")
    category: str | None = Field(
        default=None,
        description="Optional category (errand/social/work/health/family/leisure/other).",
    )
    domain: str | None = Field(
        default=None,
        description="Optional life-domain for focus balance (shop/work/home/kids/personal).",
    )
    reflection: str | None = Field(
        default=None, description="Optional plain-English 'how it went' note."
    )
    outcome: Literal["success", "partial", "miss"] | None = Field(
        default=None,
        description="Optional explicit outcome; omit to classify it from the note.",
    )

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
