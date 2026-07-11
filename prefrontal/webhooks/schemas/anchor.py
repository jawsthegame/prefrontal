"""Location-anchor (outing) request/response schemas.

Split from the webhooks schemas god-module (audit #408).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class OutingStart(BaseModel):
    """Body of ``POST /webhooks/outing/start`` — declaring an intention."""

    intention: str = Field(description="The stated mission, e.g. 'getting coffee'.")
    time_window_minutes: float | None = Field(
        default=None,
        description="Stated window. If omitted, parsed from the intention text.",
    )
    home_lat: float | None = Field(default=None, description="Baseline latitude.")
    home_lon: float | None = Field(default=None, description="Baseline longitude.")
    domain: str | None = Field(
        default=None,
        description="Optional life-domain for focus balance (shop/work/home/kids/personal).",
    )

class OutingDomain(BaseModel):
    """Body of ``POST /webhooks/outing/domain`` — set/clear an outing's life-domain."""

    outing_id: int = Field(description="The outing to (re)file into a life-domain.")
    domain: str | None = Field(
        default=None,
        description="Life-domain (shop/work/home/kids/personal); omit or null to clear.",
    )

class OutingStarted(BaseModel):
    """Response after an outing is started."""

    outing_id: int
    intention: str
    time_window_minutes: float
    time_window_source: str = Field(
        default="explicit",
        description=(
            "How the window was determined: 'explicit' (given), 'parsed' (from "
            "the text), 'llm'/'heuristic'/'default' (inferred when none stated)."
        ),
    )
    domain: str | None = Field(
        default=None,
        description=(
            "The life-domain the outing was pre-filed under (given, or inferred "
            "from the intention); null when neither was decisive."
        ),
    )
    confirmation: str = Field(
        default="",
        description=(
            "Speakable one-line read-back a thin client (iOS Shortcut) can show "
            "verbatim — flags an estimated window so the user can correct it."
        ),
    )

class OutingReturn(BaseModel):
    """Body of ``POST /webhooks/outing/return`` — closing an outing."""

    outing_id: int | None = Field(
        default=None,
        description="Outing to close. Defaults to the most recent active outing.",
    )
    status: Literal["returned", "abandoned"] = "returned"
