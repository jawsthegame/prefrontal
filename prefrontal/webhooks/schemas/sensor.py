"""Passive-observation (sensor) schemas.

Split from the webhooks schemas god-module (audit #408).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ConversationTurn(BaseModel):
    """One turn of a conversation transcript fed to ``POST /observe``."""

    speaker: str = Field(
        default="",
        description="Who spoke this turn (e.g. 'me', 'coach'). Blank renders as '?'.",
    )
    text: str = Field(description="What was said this turn.")

class ObserveRequest(BaseModel):
    """Body of ``POST /observe`` — a note *or* a transcript for the LLM sensor.

    Provide ``text`` (a single free-text note) or ``transcript`` (a multi-turn
    conversation); a transcript, when present and non-empty, takes precedence.
    Either way the sensor only *proposes* allowlisted candidate updates that land
    as pending proposals for human review — it never writes authoritative facts.
    """

    text: str = Field(
        default="",
        description=(
            "A short free-text note / observation. Optional when ``transcript`` is "
            "given."
        ),
    )
    transcript: list[ConversationTurn] = Field(
        default_factory=list,
        description=(
            "A conversation transcript (turns of speaker + text). When non-empty "
            "the sensor reads the whole conversation and attributes signal to the "
            "user, instead of reading ``text``."
        ),
    )
