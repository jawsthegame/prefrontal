"""Focus-session request/response schemas.

Split from the webhooks schemas god-module (audit #408).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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
