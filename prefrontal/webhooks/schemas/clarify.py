"""Clarification-resolution schemas.

Split from the webhooks schemas god-module (audit #408).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ClarificationResolve(BaseModel):
    """Body of ``POST /clarifications/{id}/resolve`` — answer an ambiguity question.

    Provide ``option_index`` to pick one of the offered readings, or a free-text
    ``answer``. ``task_type`` is optional (usually inferred): a recognized value
    unlocks that task's guided playbook."""

    option_index: int | None = Field(
        default=None,
        ge=0,
        description="Index of the chosen candidate reading, or null for a free-text answer.",
    )
    answer: str | None = Field(
        default=None,
        description="Free-text reading, used when no 'option_index' is given.",
    )
    task_type: str | None = Field(
        default=None,
        description="Recognized task type to force (usually inferred from the answer).",
    )

class ClarificationLocalization(BaseModel):
    """Body of ``POST /clarifications/localization`` — opt in/out + set the home ZIP.

    Both fields optional: ``enabled`` toggles ZIP-localized guides, ``zip`` sets the
    home ZIP. Omit a field to leave it unchanged."""

    enabled: bool | None = Field(
        default=None, description="Turn ZIP-localized guides on/off. Omit to leave as-is."
    )
    zip: str | None = Field(
        default=None, description="Home ZIP used to localize guides. Omit to leave as-is."
    )
