"""People request schemas — roster CRUD and the mention review queue.

The people queue turns names that ingested items keep mentioning into an
identified, categorized roster that feeds learning and prioritization; see
:mod:`prefrontal.people` and :mod:`prefrontal.memory.repos.people`.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from prefrontal.people import RELATIONSHIPS

#: Reused literal-ish description of the relationship vocabulary.
_REL_DESC = "One of: " + " · ".join(RELATIONSHIPS)


class PersonCreate(BaseModel):
    """Body of ``POST /people`` — add someone to the roster directly."""

    name: str = Field(description="The person's name.")
    relationship: str = Field(default="unknown", description=_REL_DESC)
    importance: int = Field(
        default=1, ge=0, le=3, description="0 low · 1 normal · 2 high · 3 top."
    )
    aliases: list[str] = Field(
        default_factory=list, description="Other spellings/nicknames to match to them."
    )
    notes: str | None = Field(default=None, description="Who they are / why they matter.")


class PersonUpdate(BaseModel):
    """Body of ``PATCH /people/{id}`` — recategorize any subset of fields.

    Every field is optional; only those supplied change. Use
    ``POST /people/{id}/archive`` for the status transition.
    """

    name: str | None = None
    relationship: str | None = Field(default=None, description=_REL_DESC)
    importance: int | None = Field(default=None, ge=0, le=3)
    aliases: list[str] | None = None
    notes: str | None = None


class MentionIdentify(BaseModel):
    """Body of ``POST /people/mentions/{id}/identify`` — resolve a queued name.

    Either link an existing roster person with ``person_id``, **or** omit it to
    create a new person from the mention's name (plus the category fields here).
    A supplied ``person_id`` wins; ``name`` overrides the mention's captured name
    when creating.
    """

    person_id: int | None = Field(
        default=None, description="Link this existing roster person (else create new)."
    )
    name: str | None = Field(
        default=None, description="Override the captured name when creating a new person."
    )
    relationship: str = Field(default="unknown", description=_REL_DESC)
    importance: int = Field(
        default=1, ge=0, le=3, description="0 low · 1 normal · 2 high · 3 top."
    )
    notes: str | None = Field(default=None, description="Who they are / why they matter.")


class PeopleExtract(BaseModel):
    """Body of ``POST /people/extract`` — scan free text for names to queue.

    The on-demand twin of the ingestion hook: extract candidate people-names from
    ``text`` and queue the unknown ones for review (touching any already on the
    roster). ``use_llm`` augments the deterministic extraction with the model.
    """

    text: str = Field(description="Free text to scan for people-names.")
    source: str = Field(default="manual", description="Provenance tag for the mentions.")
    use_llm: bool = Field(default=False, description="Also use the model to find names.")
