"""Communication-translation request schemas (roadmap M4)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TranslateMessage(BaseModel):
    """Body of ``POST /communicate/translate`` — decode/draft/soften one message.

    Text-only and side-effect-free: the response is writing the user reads, copies,
    or edits — nothing is sent, booked, or stored.
    """

    # The wire field is ``register``, but that name shadows ``BaseModel.register``
    # (the ABC-registration classmethod), so the attribute is ``register_`` with an
    # alias — clients still send/read ``register``.
    model_config = ConfigDict(populate_by_name=True)

    text: str = Field(
        description=(
            "The message to work on: the email/message received (decode), a short "
            "description of what you want to say (draft), or the message you wrote "
            "(soften)."
        )
    )
    mode: str = Field(
        default="decode",
        description=(
            "What to do: 'decode' (explain what a received message really means), "
            "'draft' (write a reply from your description), or 'soften' (rewrite "
            "your message in a chosen register)."
        ),
    )
    register_: str | None = Field(
        default=None,
        alias="register",
        description=(
            "Target tone for draft/soften: professional | warm | firm | concise | "
            "friendly (default professional). Ignored for decode."
        ),
    )
