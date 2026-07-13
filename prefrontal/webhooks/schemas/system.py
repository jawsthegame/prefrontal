"""System schemas (self-care checks, outbound SMTP).

Split from the webhooks schemas god-module (audit #408).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class SelfCareCheckConfig(BaseModel):
    """One check's settings in ``POST /self-care``. All optional — a partial
    update writes only the fields present, leaving the rest as they were."""

    enabled: bool | None = Field(default=None, description="Turn this check on/off.")
    target: int | None = Field(
        default=None, ge=1, description="Daily target (confirms that end the check for the day)."
    )
    start_hour: int | None = Field(
        default=None, ge=0, le=23, description="Local hour the check starts nudging."
    )
    end_hour: int | None = Field(
        default=None, ge=0, le=23,
        description=(
            "Local hour the check stops nudging for the day. Only applies to a "
            "window-bounded check (bio breaks, wind-down); ignored for the others."
        ),
    )
    interval_minutes: int | None = Field(
        default=None, ge=1, description="Minutes between nudges (re-ask / recurring cadence)."
    )
    bypass_quiet_hours: bool | None = Field(
        default=None,
        description=(
            "Whether this check's cue skips the shared daytime quiet-hours window. "
            "Only applies to an evening check (wind-down); ignored for the others."
        ),
    )

class SelfCareReviewConfig(BaseModel):
    """The end-of-day gap review's settings in ``POST /self-care``. All optional."""

    enabled: bool | None = Field(
        default=None, description="Turn the opt-in evening gap-review push on/off."
    )
    hour: int | None = Field(
        default=None, ge=0, le=23, description="Local hour the review fires (evening)."
    )
    bypass_quiet_hours: bool | None = Field(
        default=None,
        description=(
            "Whether the review skips the daytime quiet-hours window (on by default "
            "so an end-of-day recap actually lands)."
        ),
    )

class SelfCareConfig(BaseModel):
    """Body of ``POST /self-care`` — adjust the self-care settings from the dashboard.

    Every field is optional so the UI can send a partial update (e.g. just the
    master switch, or just one check's target). ``checks`` is keyed by check key
    (``meal`` / ``water`` / ``meds`` / ``biobreak`` / ``winddown`` /
    ``movement``); unknown keys are ignored server-side. ``review`` carries the
    end-of-day gap review's module-level settings.
    """

    enabled: bool | None = Field(default=None, description="Master self-care switch.")
    checks: dict[str, SelfCareCheckConfig] = Field(default_factory=dict)
    review: SelfCareReviewConfig | None = Field(default=None)

class SelfCareMark(BaseModel):
    """Body of ``POST /self-care/mark`` — log a confirm for one check today.

    Backs the dashboard card's "mark what I did today" buttons: a signed-in
    user records a check they completed (e.g. after missing the notification),
    which counts one toward that check's daily target exactly as a one-tap
    notification confirm would.
    """

    key: str = Field(
        description="Which check to confirm: meal / water / meds / biobreak / winddown / movement."
    )
    undo: bool = Field(
        default=False,
        description="If true, reduce today's count by one (floored at zero) "
        "instead of logging a confirm — the chip's shift-click mis-tap correction.",
    )
    reset: bool = Field(
        default=False,
        description="If true, reset today's count to zero instead of logging a "
        "confirm — the chip's tap-at-max wrap-around (mobile has no shift-click). "
        "Takes precedence over ``undo``.",
    )

class GuideProgress(BaseModel):
    """Body of ``POST /guide/seen`` — mark one module's walkthrough done (or not).

    Backs the in-app Guide's per-module "Got it ✓ / Mark as new" toggle. Purely a
    read-your-own-progress convenience — it records nothing about behavior and
    never changes whether a module is enabled, so a user can re-read any guide as
    often as they like.
    """

    key: str = Field(description="The module key whose walkthrough to mark (e.g. 'hyperfocus').")
    seen: bool = Field(
        default=True,
        description="True to mark this module's guide as read; false to mark it new again.",
    )


class ApnsTokenRegistration(BaseModel):
    """Body of ``POST /route/apns-token`` — register this device for native push.

    The iOS app posts the APNs device token it got from
    ``registerForRemoteNotifications``; the server stores it as the user's
    per-user ``apns_token`` route, so :class:`DeliveryClient` can deliver via
    Apple Push. Send an empty ``token`` to unregister (e.g. on sign-out).
    """

    token: str = Field(
        description="The hex APNs device token, or '' to clear (unregister).",
    )


class SmtpConfig(BaseModel):
    """Body of ``POST /smtp`` — one of the user's outbound-email (SMTP) accounts.

    A partial-update: an omitted/blank ``password`` leaves the stored one in place
    (so the operator can edit the host without retyping the secret). The password
    is sealed at rest and never returned by ``GET /smtp``.
    """

    account: str = Field(
        default="default",
        description="Outbox name — 'default', or a mail account / domain "
        "(work/personal) so a delegated todo auto-sends from the matching identity.",
    )
    host: str = Field(description="SMTP server host, e.g. smtp.gmail.com.")
    port: int = Field(default=587, ge=1, le=65535, description="SMTP port (587 for STARTTLS).")
    username: str = Field(default="", description="SMTP login username (often the full email).")
    password: str | None = Field(
        default=None,
        description="SMTP password / app password. Omit or leave blank to keep the stored one.",
    )
    sender: str = Field(
        default="", description="From address (defaults to the username when blank)."
    )
    use_tls: bool = Field(default=True, description="Use STARTTLS (the norm for port 587).")
    enabled: bool = Field(default=True, description="Whether the email handler may send.")
