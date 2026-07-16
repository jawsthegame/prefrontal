"""Scoped-action (MCP tool-call) request schemas (roadmap M4)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ActionPreviewRequest(BaseModel):
    """Body of ``POST /actions/preview`` — dry-run a scoped MCP tool-call."""

    server: str = Field(description="The configured MCP server's name.")
    tool: str = Field(description="The tool to call (must be on the server's allowlist).")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="Arguments to pass to the tool."
    )


class ActionRunRequest(BaseModel):
    """Body of ``POST /actions/run`` — confirm and run the previewed tool-call.

    ``preview_digest`` is the digest from the matching preview; the run refuses if
    the call drifted since then, so a stale confirmation can't fire.
    """

    server: str = Field(description="The configured MCP server's name.")
    tool: str = Field(description="The tool to call (must be on the server's allowlist).")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Arguments for the tool.")
    preview_digest: str = Field(
        min_length=1, description="The digest from the matching preview (pins the exact call)."
    )
