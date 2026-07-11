"""Project request schemas (create/update/reorder, entity assignment).

Split from the webhooks schemas god-module (audit #408).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    """Body of ``POST /projects`` — create a project."""

    name: str = Field(description="Project name (unique among your active projects).")
    domain: str = Field(
        description=(
            "Life sphere the project sits under (work / home / kids / …). A project "
            "is nested under one domain; its members inherit it."
        )
    )
    description: str | None = Field(
        default=None,
        description=(
            "Short blurb, matched together with the name to auto-suggest this "
            "project for triaged items."
        ),
    )
    notes: str | None = Field(default=None, description="Longer detail (not used for matching).")
    color: str | None = Field(default=None, description="Optional UI accent (hex/token).")

class ProjectUpdate(BaseModel):
    """Body of ``PATCH /projects/{id}`` — edit any subset of a project's fields.

    Every field is optional; only those supplied are changed. Changing ``domain``
    cascades to the project's assigned todos/commitments.
    """

    name: str | None = None
    domain: str | None = None
    description: str | None = None
    notes: str | None = None
    color: str | None = None

class ProjectReorder(BaseModel):
    """Body of ``POST /projects/reorder`` — the full forced priority order.

    Must list every one of the user's *active* project ids exactly once, top
    priority first. That distinctness is what forces an objective priority: no
    two projects can share a rank.
    """

    order: list[int] = Field(
        description=(
            "Active project ids in priority order, top first. Every active project "
            "must appear exactly once (a stale/partial list is rejected 422)."
        )
    )

class EntityProjectUpdate(BaseModel):
    """Body of the ``POST /{entity}/{id}/project`` assignment endpoints."""

    project_id: int | None = Field(
        default=None,
        description="Project to assign the item to, or null to clear the assignment.",
    )
