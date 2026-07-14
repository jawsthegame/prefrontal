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
from prefrontal.sensor import Candidate, extract_candidates

if TYPE_CHECKING:
    from prefrontal.integrations import Generator


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
    assistant_client: Generator,
    sensor_client: Generator | None = None,
    now: datetime | None = None,
    tz: str = "UTC",
    avoid_keys: frozenset[str] | None = None,
) -> BrainDumpPlan:
    """Turn one free-text ramble into a combined, previewable plan (no writes).

    Runs the same ``text`` through both existing pipelines and merges the result:

    1. :func:`prefrontal.assistant.plan` for the actionable half — a validated,
       id-resolved action list plus a natural-language reply, nothing written.
    2. :func:`prefrontal.sensor.extract_candidates` for the behavioral half —
       allowlisted candidate updates, nothing recorded (the caller records them
       pending).

    Args:
        text: The rambling voice transcript / free-text dump.
        memory: A **scoped** store (one user), for the assistant's snapshot.
        assistant_client: Model client for the editing assistant (Claude when the
            ``assistant`` agent is opted into Anthropic, else local Ollama).
        sensor_client: Model client for the sensor (the ``sensor`` agent). When
            ``None`` the behavioral half is skipped — an honest "no sensor,
            observe nothing" rather than a guess.
        now: Current instant as naive UTC (defaults to the assistant's own clock),
            anchoring relative dates/times in the ramble.
        tz: The user's IANA timezone, so relative times resolve in *their* zone and
            are emitted as local wall-clock (see :func:`prefrontal.assistant.plan`).
        avoid_keys: State keys the sensor should stop volunteering (the calibration
            feedback loop; see :func:`prefrontal.sensor.avoided_state_keys`).

    Returns:
        A :class:`BrainDumpPlan`. Empty/whitespace text yields the assistant's
        graceful empty reply and no candidates.
    """
    ap = _assistant_plan(text, memory, client=assistant_client, now=now, tz=tz)
    candidates: list[Candidate] = []
    if sensor_client is not None and text and text.strip():
        candidates = extract_candidates(
            text, client=sensor_client, avoid_keys=avoid_keys
        )
    return BrainDumpPlan(
        reply=ap.reply,
        actions=ap.actions,
        errors=ap.errors,
        candidates=candidates,
    )
