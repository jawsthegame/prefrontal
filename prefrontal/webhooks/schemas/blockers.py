"""Blocker request schemas (create/update) — who's waiting on you.

A blocker records that someone *else* is blocked until you do a thing; see
:mod:`prefrontal.memory.repos.blockers` and :mod:`prefrontal.blockers`.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class BlockerCreate(BaseModel):
    """Body of ``POST /blockers`` — log that someone is waiting on you."""

    person: str = Field(description="Who is blocked / waiting on you (a name).")
    what: str = Field(description="The thing they need from you.")
    priority: int = Field(
        default=1, ge=0, le=3, description="0 low · 1 normal · 2 high · 3 urgent."
    )
    deadline: str | None = Field(
        default=None,
        description='Optional "needs it by", "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS" (UTC).',
    )
    blocking_since: str | None = Field(
        default=None,
        description=(
            "When they started waiting (UTC). Defaults to now; pass an earlier "
            "value when the wait began before you captured it."
        ),
    )
    notes: str | None = Field(default=None, description="Optional free-text detail.")
    todo_id: int | None = Field(
        default=None, description="Optional link to the open loop that clears this."
    )


class BlockerUpdate(BaseModel):
    """Body of ``PATCH /blockers/{id}`` — edit any subset of a blocker's fields.

    Every field is optional; only those supplied are changed. Status transitions
    use ``POST /blockers/{id}/resolve`` / ``/reopen`` instead.
    """

    person: str | None = None
    what: str | None = None
    priority: int | None = Field(default=None, ge=0, le=3)
    deadline: str | None = None
    notes: str | None = None
    todo_id: int | None = None
