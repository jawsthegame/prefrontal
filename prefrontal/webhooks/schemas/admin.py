"""Admin user/household provisioning schemas.

Split from the webhooks schemas god-module (audit #408).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    """Body of ``POST /admin/users`` — provision a user (operator-only)."""

    handle: str = Field(description="Unique short handle, e.g. 'sam'.")
    display_name: str | None = Field(
        default=None, description="Name shown in nudges/briefings."
    )
    is_operator: bool = Field(
        default=False, description="Whether the user may call the admin surface."
    )
    email: str | None = Field(
        default=None,
        description="Verified Google email that signs this user in (optional).",
    )

class UserEmail(BaseModel):
    """Body of ``POST /admin/users/{handle}/email`` — set/clear a sign-in email."""

    email: str | None = Field(
        default=None,
        description="Google email for sign-in; blank/null clears it.",
    )

class HouseholdCreate(BaseModel):
    """Body of ``POST /admin/households`` — create a household (operator-only)."""

    name: str = Field(description="Household name, e.g. 'The Kims'.")

class HouseholdMember(BaseModel):
    """Body of ``POST /admin/households/{id}/members`` — add a user (operator-only)."""

    handle: str = Field(description="Handle of the user to put into the household.")
