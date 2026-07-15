"""Photo → structured items (roadmap: "capture at the speed of thought", vision).

A photo is the lowest-friction capture there is for anything already written down
— a whiteboard after a meeting, a school newsletter, a scribbled shopping list, a
receipt. This module turns one such image into the *same* structured items a voice
brain-dump produces, by composing two capabilities that already exist:

- **Vision** — :meth:`~prefrontal.integrations.anthropic.AnthropicClient.describe_image`
  reads the image into plain text (a transcript of what's on it). This is the
  cloud multimodal path; routing it through an *on-device* multimodal model is
  still ahead on the roadmap, and native camera/Photos capture in the iOS app
  feeding this endpoint is the client half of the same milestone.
- **Capture** — :func:`prefrontal.braindump.plan_braindump` fans that transcript
  out to both existing propose→confirm pipelines (the NL editing assistant for
  actionable items, the LLM sensor for behavioral asides).

So the image is just a transcript feeding the brain-dump fan-out. This module owns
**no new capability and no new safety model**: the actionable half is previewed
and written only on Apply, the behavioral half lands *pending* and applies only on
accept — a blurry, misread photo can never silently mutate the store, exactly as a
rambling voice dump can't.

Vision is Anthropic-only today (the local model can't see), so unlike the text
pipelines there's no local fallback: an unavailable or failing vision client
degrades to an *empty transcript* (and therefore an empty plan) rather than
guessing at pixels it never read. The caller decides whether "no vision backend at
all" is a hard error (the ``POST /vision`` endpoint returns 503) or a soft empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from prefrontal.assistant import ValidatedAction
from prefrontal.braindump import plan_braindump
from prefrontal.integrations.base import ProviderError
from prefrontal.sensor import Candidate

if TYPE_CHECKING:
    from prefrontal.integrations import Generator

#: Default transcription instruction. It asks for a faithful, commentary-free
#: reading because the *structuring* is the brain-dump's job downstream — the
#: vision step should surface what's on the image as if the user had spoken it,
#: not decide what's actionable.
DEFAULT_PROMPT = (
    "Read this image and write out, as plain text, everything on it that a person "
    "might want to remember or act on: list items, tasks, notes, names, dates, "
    "times, amounts, and any handwriting you can make out. Preserve list structure "
    "line by line. Do not add commentary, headings, or guesses — if part is "
    "unreadable, skip it. If the image has no such content, reply with nothing."
)


class ImageDescriber(Protocol):
    """The slice of the vision backend this module needs.

    :class:`~prefrontal.integrations.anthropic.AnthropicClient` satisfies it; tests
    pass a fake. Kept separate from :class:`~prefrontal.integrations.Generator`
    because vision is Anthropic-only and off that shared text protocol.
    """

    def available(self) -> bool: ...

    def describe_image(
        self,
        image_base64: str,
        *,
        prompt: str,
        media_type: str = ...,
        system: str | None = ...,
        max_tokens: int | None = ...,
    ) -> str: ...


@dataclass
class VisionPlan:
    """The result of reading one image and fanning its transcript out to capture.

    Attributes:
        transcript: What the vision model read off the image (empty when vision
            was unavailable, the image was blank/unreadable, or the model
            refused). Everything below is derived from it, so an empty transcript
            means an empty plan.
        reply: The assistant's short natural-language acknowledgement of the
            actionable half (what it *will* do once applied — never "done").
        actions: Validated, previewable editing actions. Nothing is written until
            they're echoed to ``POST /assistant/apply``.
        errors: Human-readable reasons individual actions were dropped.
        candidates: Behavioral candidate updates the sensor observed in the
            transcript; the caller records these *pending*.
    """

    transcript: str = ""
    reply: str = ""
    actions: list[ValidatedAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)


def transcribe_image(
    image_base64: str,
    media_type: str,
    *,
    vision_client: ImageDescriber,
    prompt: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """Read an image to text, degrading to ``""`` rather than raising.

    A missing/unavailable client, an unsupported media type, or a transport/auth
    failure all resolve to an empty transcript — vision has no local fallback, so
    "couldn't read it" is honestly *nothing read*, never a guess. Callers that want
    to distinguish "no backend" from "backend read nothing" should check
    :meth:`ImageDescriber.available` first (the endpoint does, to return 503).

    Args:
        image_base64: The image bytes, base64-encoded (no ``data:`` prefix).
        media_type: The image's MIME type (see
            :data:`~prefrontal.integrations.anthropic.SUPPORTED_IMAGE_MEDIA_TYPES`).
        vision_client: The multimodal backend.
        prompt: Override the transcription instruction (defaults to
            :data:`DEFAULT_PROMPT`).
        max_tokens: Output-token cap for the vision call (defaults to the client's).
    """
    if not image_base64:
        return ""
    if not vision_client.available():
        return ""
    try:
        return vision_client.describe_image(
            image_base64,
            prompt=prompt or DEFAULT_PROMPT,
            media_type=media_type,
            max_tokens=max_tokens,
        ).strip()
    except ProviderError:
        # No local vision fallback — an unreachable/erroring backend reads nothing.
        return ""


def plan_vision(
    image_base64: str,
    media_type: str,
    memory: Any,
    *,
    vision_client: ImageDescriber,
    assistant_client: Generator,
    sensor_client: Generator | None = None,
    prompt: str | None = None,
    now: datetime | None = None,
    tz: str = "UTC",
    avoid_keys: frozenset[str] | None = None,
    max_tokens: int | None = None,
) -> VisionPlan:
    """Read an image and fan its transcript out to both capture paths (no writes).

    Two steps, no new safety model:

    1. :func:`transcribe_image` reads the image to plain text (empty on any
       failure — vision has no local fallback).
    2. :func:`prefrontal.braindump.plan_braindump` runs that transcript through the
       existing assistant + sensor fan-out — a validated, previewable action list
       plus behavioral candidates the caller records pending.

    Args:
        image_base64: The image bytes, base64-encoded (no ``data:`` prefix).
        media_type: The image's MIME type.
        memory: A **scoped** store (one user), for the assistant's snapshot.
        vision_client: The multimodal backend (Anthropic today).
        assistant_client: Model client for the editing assistant half.
        sensor_client: Model client for the sensor half; ``None`` skips it.
        prompt: Override the transcription instruction.
        now: Current instant as naive UTC (anchors relative dates in the image).
        tz: The user's IANA timezone for resolving relative times.
        avoid_keys: State keys the sensor should stop volunteering.
        max_tokens: Output-token cap for the vision call.

    Returns:
        A :class:`VisionPlan`. An empty image, an unavailable vision backend, or an
        unreadable/blank image short-circuits to an empty plan without touching the
        brain-dump pipeline.
    """
    if not image_base64:
        return VisionPlan()
    transcript = transcribe_image(
        image_base64,
        media_type,
        vision_client=vision_client,
        prompt=prompt,
        max_tokens=max_tokens,
    )
    if not transcript:
        return VisionPlan(transcript="")
    dump = plan_braindump(
        transcript,
        memory,
        assistant_client=assistant_client,
        sensor_client=sensor_client,
        now=now,
        tz=tz,
        avoid_keys=avoid_keys,
    )
    return VisionPlan(
        transcript=transcript,
        reply=dump.reply,
        actions=dump.actions,
        errors=dump.errors,
        candidates=dump.candidates,
    )
