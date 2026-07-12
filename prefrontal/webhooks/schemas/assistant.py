"""Natural-language assistant request schemas.

Split from the webhooks schemas god-module (audit #408).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AssistantMessage(BaseModel):
    """Body of ``POST /assistant`` — a natural-language editing request."""

    message: str = Field(description="Free-text ask, e.g. 'bump the dentist call to urgent'.")

class FindTimeMessage(BaseModel):
    """Body of ``POST /assistant/find-time`` — a free-text "find me a slot" ask."""

    message: str = Field(
        description=(
            "Free-text scheduling ask, e.g. 'find 45 min for coffee with Sam this "
            "week' or 'when are my wife and I both free for dinner tomorrow evening?'."
        )
    )


class AssistantApply(BaseModel):
    """Body of ``POST /assistant/apply`` — the proposed actions to execute.

    The client echoes back the ``actions`` returned by ``POST /assistant``. They
    are re-validated against the *current* store before executing, so a stale or
    tampered action simply drops rather than acting on the wrong row.
    """

    actions: list[dict[str, Any]] = Field(
        default_factory=list, description="Wire-format actions from POST /assistant."
    )
