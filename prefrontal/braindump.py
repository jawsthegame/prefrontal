"""Voice brain-dump → structured items (roadmap M1, "capture at the speed of thought").

A rambling voice note is the lowest-friction capture there is — "just open your
mouth" — but one ramble mixes two kinds of signal Prefrontal already knows how to
handle *separately*:

- **Actionable items** — todos, commitments, shopping, if-then plans, household
  facts — which the natural-language editing assistant
  (:mod:`prefrontal.assistant`) turns into a validated, **preview-before-write**
  action list.
- **Behavioral asides** — "I always blow off admin on Mondays", "keep the
  briefings short" — which the LLM-as-sensor (:mod:`prefrontal.sensor`) turns into
  **pending candidate** updates for review.

A brain-dump is just that one utterance fanned out to *both* paths and merged into
a single review surface. This module owns no new capability and no new safety
model: it composes the two existing propose→confirm pipelines so a caller (the
``POST /braindump`` endpoint, the ``prefrontal braindump`` CLI) gets both halves
from one ramble, and applies each half through its existing confirm path
(``POST /assistant/apply`` for actions; ``POST /proposals/{id}/accept`` for
candidates).

**Planning writes nothing.** The assistant actions are validated and previewed but
executed only on Apply; the sensor candidates are extracted here and *recorded
pending* by the caller, applied only on accept. Both halves keep the same
human-in-the-loop guarantee they have on their own — a rambling, imperfect voice
dump can never silently mutate the store.

The two halves use independent model clients because they're independently
provider-selectable agents (``assistant`` vs ``sensor``); the caller resolves each
and passes it in. A missing/unreachable client degrades that half to empty rather
than failing the whole dump — the assistant returns a graceful reply with no
actions, the sensor observes nothing rather than inventing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from prefrontal.assistant import ValidatedAction
from prefrontal.assistant import plan as _assistant_plan
from prefrontal.assistant import plan_preparsed as _assistant_plan_preparsed
from prefrontal.sensor import Candidate, extract_candidates, validate_observations

if TYPE_CHECKING:
    from prefrontal.integrations import Generator


@dataclass
class OnDeviceParse:
    """A brain-dump already parsed into structure by the client's own model.

    The native app can run the ramble through the **on-device Foundation Model**
    (Apple Foundation Models / Gemini Nano; roadmap M1) and post the result here
    instead of raw text — the cheap/private/offline path, with no server-side
    inference. The two halves mirror the model outputs the server would otherwise
    produce, so they flow through the *same* validation and confirm gates:

    Attributes:
        actions: Wire-format editing actions (``{"op", ...}``) for the assistant
            half — validated and previewed, written only on ``/assistant/apply``.
        observations: Raw sensor candidate objects (``{"kind", ...}``) for the
            behavioral half — allowlist-checked and recorded **pending**.
        reply: The on-device model's short acknowledgement of the actionable half
            (falls back to a deterministic one when blank).

    An on-device parse is *untrusted input* just like a server model's reply:
    nothing here bypasses validation or the human-in-the-loop confirm step.
    """

    actions: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    reply: str = ""


@dataclass
class BrainDumpPlan:
    """The result of fanning one ramble out to both capture paths.

    Attributes:
        reply: The assistant's short natural-language acknowledgement of the
            actionable half (what it *will* do once applied — never "done").
        actions: Validated, previewable editing actions (todos, commitments,
            shopping, if-then, household facts). Nothing is written until they're
            echoed to ``POST /assistant/apply``.
        errors: Human-readable reasons individual actions were dropped in
            validation.
        candidates: Behavioral candidate updates the sensor observed. The caller
            records these as *pending* proposals (reviewed via ``GET /proposals``).
    """

    reply: str
    actions: list[ValidatedAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)


def plan_braindump(
    text: str,
    memory: Any,
    *,
    assistant_client: Generator | None = None,
    sensor_client: Generator | None = None,
    now: datetime | None = None,
    tz: str = "UTC",
    avoid_keys: frozenset[str] | None = None,
    parse: OnDeviceParse | None = None,
) -> BrainDumpPlan:
    """Turn one ramble into a combined, previewable plan (no writes).

    Two ways in, one merged review surface and one safety model:

    - **Server parse** (``parse`` is ``None``): runs ``text`` through both existing
      model pipelines — :func:`prefrontal.assistant.plan` for the actionable half
      and :func:`prefrontal.sensor.extract_candidates` for the behavioral half.
      This is the escalation path: the opt-in cloud agent (or the local model) does
      the hard reasoning.
    - **On-device parse** (``parse`` provided): the client already parsed the ramble
      with its **on-device Foundation Model** (roadmap M1), so **no server model is
      called** — the supplied structure is run through the *same* downstream halves
      (:func:`prefrontal.assistant.plan_preparsed`,
      :func:`prefrontal.sensor.validate_observations`). Cheap, private, offline; the
      preview/confirm guarantees are untouched, so an on-device parse can't write.

    Args:
        text: The rambling voice transcript / free-text dump. Ignored when ``parse``
            is supplied (the client already consumed it on-device).
        memory: A **scoped** store (one user), for the assistant's snapshot.
        assistant_client: Model client for the editing assistant (Claude when the
            ``assistant`` agent is opted into Anthropic, else local Ollama). Unused
            on the on-device path.
        sensor_client: Model client for the sensor (the ``sensor`` agent). When
            ``None`` the behavioral half is skipped — an honest "no sensor,
            observe nothing" rather than a guess. Unused on the on-device path.
        now: Current instant as naive UTC (defaults to the assistant's own clock),
            anchoring relative dates/times in the ramble.
        tz: The user's IANA timezone, so relative times resolve in *their* zone and
            are emitted as local wall-clock (see :func:`prefrontal.assistant.plan`).
        avoid_keys: State keys the sensor should stop volunteering (the calibration
            feedback loop; see :func:`prefrontal.sensor.avoided_state_keys`).
        parse: A structure the client already extracted on-device. When present, the
            server validates it instead of calling any model (see above).

    Returns:
        A :class:`BrainDumpPlan`. Empty/whitespace text (with no ``parse``)
        short-circuits to an empty plan without a model call.
    """
    if parse is not None:
        ap = _assistant_plan_preparsed(parse.actions, memory, reply=parse.reply)
        return BrainDumpPlan(
            reply=ap.reply,
            actions=ap.actions,
            errors=ap.errors,
            candidates=validate_observations(parse.observations),
        )
    if not text or not text.strip():
        return BrainDumpPlan(reply="")
    if assistant_client is None:
        # No actionable client on the server path — degrade that half to empty,
        # symmetric with a ``None`` sensor_client, rather than crash.
        ap = _assistant_plan_preparsed([], memory)
    else:
        ap = _assistant_plan(text, memory, client=assistant_client, now=now, tz=tz)
    candidates: list[Candidate] = []
    if sensor_client is not None:
        candidates = extract_candidates(
            text, client=sensor_client, avoid_keys=avoid_keys
        )
    return BrainDumpPlan(
        reply=ap.reply,
        actions=ap.actions,
        errors=ap.errors,
        candidates=candidates,
    )
