"""HTTP routes tagged "people" — the name-mention review queue + roster.

Ingested items keep naming people; triage drops the unknown ones into a review
queue (:mod:`prefrontal.people`). This router is where the user works that queue —
identify a name (link an existing person or create + categorize a new one) or
dismiss it — and manages the resulting roster, whose relationships/importance feed
the profile (learning) and todo prioritization. See
:mod:`prefrontal.memory.repos.people`.

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from prefrontal.people import (
    RELATIONSHIPS,
    enqueue_mentions,
    name_key,
    normalize_name,
)
from prefrontal.webhooks.deps import ScopedRequest, resolve_user
from prefrontal.webhooks.schemas import (
    MentionIdentify,
    PeopleExtract,
    PersonCreate,
    PersonUpdate,
)
from prefrontal.webhooks.services import RouterServices


def _validate_relationship(relationship: str) -> None:
    """422 on an unknown relationship, keeping the roster's vocab consistent."""
    if relationship not in RELATIONSHIPS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"relationship must be one of: {', '.join(RELATIONSHIPS)}.",
        )


def build_router(services: RouterServices) -> APIRouter:
    """Build the "people" APIRouter (shared services injected by create_app)."""
    router = APIRouter()
    # Model for the on-demand name-extraction path; the ingestion hook stays
    # heuristic-only (see prefrontal.triage). Reuses the sensor agent's client.
    people_client = services.provider.client("sensor")

    # --- roster --------------------------------------------------------------

    @router.get("/people", tags=["people"])
    def people_list(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        person_status: Annotated[
            str,
            Query(alias="status", description="active/archived, or 'all'."),
        ] = "active",
    ) -> dict[str, Any]:
        """List the roster (most-mentioned first). Defaults to active people."""
        query = "" if person_status == "all" else person_status
        return {"people": ctx.store.list_people(status=query)}

    @router.post("/people", status_code=status.HTTP_201_CREATED, tags=["people"])
    def person_create(
        payload: PersonCreate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Add someone to the roster directly (bypassing the queue)."""
        _validate_relationship(payload.relationship)
        name = normalize_name(payload.name)
        if not name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A person needs a name.")
        memory = ctx.store
        if memory.find_person(name_key(name)) is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, f"{name!r} is already on the roster.")
        person_id = memory.add_person(
            name=name,
            name_key=name_key(name),
            relationship=payload.relationship,
            importance=payload.importance,
            aliases=[normalize_name(a) for a in payload.aliases if normalize_name(a)],
            notes=payload.notes,
        )
        return memory.get_person(person_id)

    @router.patch("/people/{person_id}", tags=["people"])
    def person_update(
        person_id: int,
        payload: PersonUpdate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Recategorize a person — relationship, importance, name, aliases, notes."""
        memory = ctx.store
        if memory.get_person(person_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No person {person_id}.")
        fields = payload.model_dump(exclude_unset=True)
        if "relationship" in fields:
            _validate_relationship(fields["relationship"])
        if "name" in fields:
            new_name = normalize_name(fields["name"] or "")
            if not new_name:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A person needs a name.")
            existing = memory.find_person(name_key(new_name))
            if existing is not None and int(existing["id"]) != person_id:
                raise HTTPException(
                    status.HTTP_409_CONFLICT, f"{new_name!r} is already on the roster."
                )
            fields["name"] = new_name
            fields["name_key"] = name_key(new_name)
        if "aliases" in fields and fields["aliases"] is not None:
            fields["aliases"] = [normalize_name(a) for a in fields["aliases"] if normalize_name(a)]
        return memory.update_person(person_id, **fields)

    @router.post("/people/{person_id}/archive", tags=["people"])
    def person_archive(
        person_id: int,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Archive a person — kept for history, dropped from active surfaces."""
        memory = ctx.store
        if memory.get_person(person_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No person {person_id}.")
        return memory.update_person(person_id, status="archived")

    # --- the review queue ----------------------------------------------------

    @router.get("/people/queue", tags=["people"])
    def queue_list(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        mention_status: Annotated[
            str,
            Query(alias="status", description="pending/identified/dismissed, or 'all'."),
        ] = "pending",
    ) -> dict[str, Any]:
        """List queued name-mentions. Defaults to the pending review queue."""
        query = "" if mention_status == "all" else mention_status
        return {"mentions": ctx.store.list_person_mentions(status=query)}

    @router.post("/people/extract", tags=["people"])
    def extract(
        payload: PeopleExtract,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Scan free text for people-names and queue the unknown ones for review.

        The on-demand twin of the ingestion hook. Known people are touched (the
        recurrence signal); unknown names become pending mentions. ``use_llm``
        augments the deterministic extraction with the model.
        """
        client = people_client if payload.use_llm else None
        result = enqueue_mentions(
            ctx.store, text=payload.text, source=payload.source, client=client
        )
        return {"queued": len(result["queued"]), "known": len(result["known"]), **result}

    @router.post("/people/mentions/{mention_id}/identify", tags=["people"])
    def mention_identify(
        mention_id: int,
        payload: MentionIdentify,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Identify a queued name: link an existing person, or create + categorize one.

        Pass ``person_id`` to link an existing roster person; omit it to create a
        new person from the mention's name (with the category fields). Either way
        the mention is marked ``identified`` and the person is touched (recurrence
        signal). Only a still-pending mention resolves, so a double-identify 404s.
        """
        memory = ctx.store
        mention = memory.get_person_mention(mention_id)
        if mention is None or mention["status"] != "pending":
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"No pending mention {mention_id}."
            )
        _validate_relationship(payload.relationship)
        if payload.person_id is not None:
            person = memory.get_person(payload.person_id)
            if person is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, f"No person {payload.person_id}."
                )
            person_id = int(person["id"])
            # Learn the mentioned spelling as an alias if it differs from the name.
            m_key = mention["name_key"]
            if m_key != person["name_key"] and m_key not in {
                str(a).lower() for a in person.get("aliases", [])
            }:
                memory.update_person(
                    person_id,
                    aliases=[*person.get("aliases", []), normalize_name(mention["name"])],
                )
        else:
            name = normalize_name(payload.name or mention["name"])
            if not name:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A person needs a name.")
            existing = memory.find_person(name_key(name))
            if existing is not None:
                person_id = int(existing["id"])
            else:
                person_id = memory.add_person(
                    name=name,
                    name_key=name_key(name),
                    relationship=payload.relationship,
                    importance=payload.importance,
                    notes=payload.notes,
                )
        memory.identify_person_mention(mention_id, person_id)
        memory.touch_person(person_id)
        return {
            "mention_id": mention_id,
            "status": "identified",
            "person": memory.get_person(person_id),
        }

    @router.post("/people/mentions/{mention_id}/dismiss", tags=["people"])
    def mention_dismiss(
        mention_id: int,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Dismiss a queued name — not a person, or not worth tracking."""
        memory = ctx.store
        mention = memory.get_person_mention(mention_id)
        if mention is None or mention["status"] != "pending":
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"No pending mention {mention_id}."
            )
        memory.dismiss_person_mention(mention_id)
        return {"mention_id": mention_id, "status": "dismissed"}

    return router
