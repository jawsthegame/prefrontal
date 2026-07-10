"""Todo request schemas (create, edits, decomposition, delegation).

Split from the webhooks schemas god-module (audit #408).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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

class TodoNotesUpdate(BaseModel):
    """Body of ``POST /todos/{id}/notes`` — set or clear a todo's notes."""

    notes: str | None = Field(
        default=None,
        description=(
            "Free-text detail consulted when a nudge is built for this todo "
            "(e.g. the 'you keep putting this off' initiation nudge); null/empty "
            "clears it."
        ),
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

class TodoSchedule(BaseModel):
    """Body of ``POST /todos/{id}/schedule`` — block time for a todo as a commitment.

    Both fields optional: ``at`` pins an explicit UTC/ISO start (else the earliest
    free window today that fits is used); ``minutes`` overrides the block length
    (else the bias-adjusted estimate)."""

    at: str | None = Field(
        default=None,
        description="Explicit UTC/ISO start; if omitted, the earliest fitting free window today.",
    )
    minutes: float | None = Field(
        default=None,
        gt=0,
        description="Block length in minutes; if omitted, the todo's bias-adjusted estimate.",
    )

class StepDone(BaseModel):
    """Body of ``POST /todos/{id}/steps/{i}/done`` — tick a decomposed step."""

    done: bool = Field(
        default=True, description="True to mark the step done, false to clear it."
    )

class AutoDecomposeConfig(BaseModel):
    """Body of ``POST /todos/auto-decompose`` — the automatic-breakdown master switch."""

    enabled: bool = Field(
        description="True to auto-break-down avoided todos; false (default) to leave "
        "them alone. The on-demand 'Break it down' button is unaffected either way."
    )

class DismissDecomposition(BaseModel):
    """Body of ``POST /todos/{id}/decompose/dismiss`` — reject a breakdown.

    ``not_useful`` = the steps don't help (folds back to improve future
    breakdowns); ``not_needed`` = the task didn't need breaking down (learn when
    not to auto-decompose).
    """

    reason: Literal["not_useful", "not_needed"] = Field(
        description="Why the breakdown was dismissed.",
    )

class DelegateTodo(BaseModel):
    """Body of ``POST /todos/{id}/delegate`` — hand a todo to an assistant.

    ``handler='agent'`` runs the in-app AI assistant (local model writes a brief +
    drafts back onto the todo); ``handler='email'`` mails the brief to a human VA
    at ``destination`` over the user's SMTP source.
    """

    handler: Literal["agent", "email"] = Field(
        default="agent",
        description="Who does the prep: 'agent' (in-app AI) or 'email' (human VA).",
    )
    destination: str | None = Field(
        default=None,
        description="The VA's email address (required for handler='email'; ignored for 'agent').",
    )
    context: str | None = Field(
        default=None,
        description=(
            "Optional free-text context to give the assistant more to work with — "
            "e.g. output pasted from another AI agent that has access to your work "
            "email. Fed into the prep for both handlers."
        ),
    )
    note: str | None = Field(
        default=None,
        description=(
            "Optional personal cover note shown at the top of the email to a human "
            "VA (handler='email' only; ignored for 'agent')."
        ),
    )
