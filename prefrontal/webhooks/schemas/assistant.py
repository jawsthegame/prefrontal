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


class BrainDumpMessage(BaseModel):
    """Body of ``POST /braindump`` — an unstructured voice/free-text ramble.

    One rambling dump is fanned out to both capture paths: the editing assistant
    (actionable items → a previewable action list) and the LLM sensor (behavioral
    asides → pending candidate updates). Nothing authoritative is written by the
    call — actions are previewed and applied via ``POST /assistant/apply``; the
    recorded proposals are reviewed via ``GET /proposals``.
    """

    text: str = Field(
        description=(
            "The brain-dump — a rambling voice transcript or free-text note, e.g. "
            "'ok so I need to call the dentist, book flights for the trip, we're "
            "out of milk, and honestly I keep blowing off admin on Mondays'."
        )
    )


class VisionMessage(BaseModel):
    """Body of ``POST /vision`` — one photo to read into structured items.

    A photo of anything already written down (a whiteboard, a school newsletter, a
    scribbled list, a receipt) is read to text by the multimodal model and then
    fanned out through the exact same brain-dump paths: the editing assistant
    (actionable items → a previewable action list) and the LLM sensor (behavioral
    asides → pending candidates). Nothing authoritative is written by the call —
    actions apply via ``POST /assistant/apply``; proposals via
    ``POST /proposals/{id}/accept``. Vision is Anthropic-only today, so the call
    returns 503 when no Anthropic key/SDK is configured.
    """

    image_base64: str = Field(
        description=(
            "The photo's bytes, base64-encoded. A ``data:`` URI prefix "
            "(``data:image/jpeg;base64,``) is accepted and stripped."
        )
    )
    media_type: str = Field(
        default="image/jpeg",
        description=(
            "The image's MIME type: one of image/jpeg, image/png, image/gif, "
            "image/webp."
        ),
    )
    prompt: str | None = Field(
        default=None,
        description=(
            "Optional override for the transcription instruction. Omit to use the "
            "default faithful, commentary-free reading."
        ),
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
