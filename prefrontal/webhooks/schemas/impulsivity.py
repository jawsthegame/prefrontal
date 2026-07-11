"""Impulse-capture and focus-switch schemas.

Split from the webhooks schemas god-module (audit #408).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
