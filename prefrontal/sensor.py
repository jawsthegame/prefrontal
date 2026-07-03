"""LLM-as-sensor — turn free text into *candidate* structured updates (learning §2).

Prefrontal's learning loop only learns from signal already shaped as ``episodes``
or ``coaching_state``. A thought like *"I always blow off admin on Mondays"* has
nowhere to land today. This module adds the missing path: it uses the local model
as a **sensor** that reads unstructured text and *proposes* structured updates —
never as an author that writes authoritative facts.

The safety model is the whole point:

- The model may only propose from a small **allowlist** — a handful of
  coaching_state keys (:data:`PROPOSABLE_STATE_KEYS`) and episode types
  (:data:`PROPOSABLE_EPISODE_TYPES`). Anything else is dropped in validation, so
  a hallucinated key can't reach the store.
- A proposal is **pending** until a human accepts it. Only then is it written —
  with ``source="llm_inferred"`` (distinct from ``inferred``/``explicit``), so the
  provenance of an LLM-observed fact is always legible.
- Inferred episodes carry only qualitative fields (type/outcome/context/notes) —
  never predicted/actual numbers, which must come from real measurement.

Mirrors the summarizer's grounded-prompt + graceful-fallback shape
(:mod:`prefrontal.memory.summarizer`), flipped to emit structured JSON instead of
prose. With no model reachable it returns **no candidates** (rather than guessing)
— an honest fallback: the sensor's job is to observe, not to invent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prefrontal.integrations.base import ProviderError
from prefrontal.integrations.ollama import OllamaClient

if TYPE_CHECKING:
    from prefrontal.integrations import Generator
    from prefrontal.memory.store import MemoryStore

#: coaching_state keys the sensor may propose (explicit-preference statements).
#: Small and curated on purpose — the model can only ever *suggest* a change to
#: one of these, and a human still confirms it.
PROPOSABLE_STATE_KEYS = frozenset(
    {
        "preferred_briefing_format",  # short | long
        "responsive_hours_start",  # earliest hour it's OK to nudge
        "responsive_hours_end",  # latest hour it's OK to nudge
        "self_care",  # on | off
        "encouragement",  # on | off
    }
)

#: episode types the sensor may propose (qualitative behavioral observations).
PROPOSABLE_EPISODE_TYPES = frozenset({"task", "checkin", "reminder", "departure"})

#: outcomes an inferred episode may carry (matches the drift vocabulary).
PROPOSABLE_OUTCOMES = frozenset({"success", "partial", "miss"})

SENSOR_SYSTEM_PROMPT = (
    "You are a careful note-reader for an ADHD assistant. You turn a short "
    "free-text note into STRUCTURED CANDIDATE updates that a human will review "
    "before anything is saved. You never invent numbers or facts the note "
    "doesn't state. If the note contains nothing worth recording, return an "
    "empty JSON array. Output ONLY a JSON array, no prose."
)


@dataclass(frozen=True)
class Candidate:
    """One validated candidate update the sensor extracted from free text.

    ``kind`` is ``"state"`` (payload ``{"key", "value"}``) or ``"episode"``
    (payload ``{"episode_type", "outcome"?, "context"?, "notes"?}``). ``rationale``
    is the model's short justification (ideally a quote from the note), shown to
    the human at review time.
    """

    kind: str
    payload: dict[str, Any]
    rationale: str = ""


def _build_prompt(text: str) -> str:
    """The grounded extraction prompt: the note plus the exact allowlists."""
    return (
        "From the NOTE below, extract candidate updates. Each item is a JSON "
        "object with:\n"
        '  - "kind": "state" or "episode"\n'
        '  - for "state": "key" (one of: '
        f"{', '.join(sorted(PROPOSABLE_STATE_KEYS))}) and "
        '"value" (a short string)\n'
        '  - for "episode": "episode_type" (one of: '
        f"{', '.join(sorted(PROPOSABLE_EPISODE_TYPES))}), optional "
        '"outcome" (one of: success, partial, miss), optional "context" '
        '(a short label, e.g. the task or place), optional "notes"\n'
        '  - "rationale": a short reason, ideally quoting the note\n'
        "Only propose a state key from that exact list. Do NOT include any "
        "numeric predicted/actual/duration fields on episodes. Return [] if "
        "nothing fits.\n\n"
        f"NOTE:\n{text.strip()}"
    )


def _coerce_json_array(raw: str) -> list[dict[str, Any]]:
    """Pull a JSON array out of a model reply that may be fenced or chatty."""
    if not raw:
        return []
    # Prefer a fenced block, else the first '[' … last ']'.
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
    blob = fenced.group(1) if fenced else raw
    if not fenced:
        start, end = blob.find("["), blob.rfind("]")
        if start == -1 or end <= start:
            return []
        blob = blob[start : end + 1]
    try:
        parsed = json.loads(blob)
    except ValueError:
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _validate(raw: dict[str, Any]) -> Candidate | None:
    """Turn one raw model object into a :class:`Candidate`, or drop it.

    This is the safety gate: it enforces the allowlists and strips any fields the
    sensor isn't allowed to set (e.g. fabricated durations on an episode).
    """
    kind = raw.get("kind")
    rationale = str(raw.get("rationale") or "").strip()[:280]
    if kind == "state":
        key = raw.get("key")
        value = raw.get("value")
        if key in PROPOSABLE_STATE_KEYS and isinstance(value, (str, int, float, bool)):
            return Candidate("state", {"key": key, "value": str(value).strip()}, rationale)
        return None
    if kind == "episode":
        etype = raw.get("episode_type")
        if etype not in PROPOSABLE_EPISODE_TYPES:
            return None
        payload: dict[str, Any] = {"episode_type": etype}
        outcome = raw.get("outcome")
        if outcome in PROPOSABLE_OUTCOMES:
            payload["outcome"] = outcome
        for field in ("context", "notes"):
            val = raw.get(field)
            if isinstance(val, str) and val.strip():
                payload[field] = val.strip()[:280]
        return Candidate("episode", payload, rationale)
    return None


def extract_candidates(
    text: str, *, client: Generator | None = None
) -> list[Candidate]:
    """Read free text and return validated candidate updates (never writes).

    Args:
        text: The free-text note / observation.
        client: A :class:`~prefrontal.integrations.Generator` — the local Ollama
            client, or Claude when the ``sensor`` agent is opted into the
            Anthropic provider (tests inject one). Defaults to the local server.

    Returns:
        Validated :class:`Candidate` objects. Empty when the note yields nothing,
        or when the model is unreachable (an honest no-guess fallback).
    """
    if not text or not text.strip():
        return []
    client = client or OllamaClient.from_settings()
    try:
        reply = client.generate(_build_prompt(text), system=SENSOR_SYSTEM_PROMPT)
    except ProviderError:
        return []  # no model → observe nothing rather than invent
    candidates = [c for c in (_validate(r) for r in _coerce_json_array(reply)) if c is not None]
    return candidates


def record_candidates(store: MemoryStore, candidates: list[Candidate]) -> list[int]:
    """Persist candidates as *pending* proposals; return their new ids."""
    return [
        store.add_proposal(kind=c.kind, payload=c.payload, rationale=c.rationale)
        for c in candidates
    ]


def summarize_candidate(kind: str, payload: dict[str, Any]) -> str:
    """A one-line human description of a proposal, for CLI listing / review."""
    if kind == "state":
        return f"set {payload.get('key')} = {payload.get('value')!r}"
    bits = [str(payload.get("episode_type"))]
    if payload.get("outcome"):
        bits.append(payload["outcome"])
    if payload.get("context"):
        bits.append(f"“{payload['context']}”")
    return "log episode: " + " · ".join(bits)


def apply_proposal(store: MemoryStore, proposal: dict[str, Any]) -> str:
    """Apply an accepted proposal to the store, stamped ``source='llm_inferred'``.

    Returns a human description of what was written. Raises ``ValueError`` on an
    unknown/invalid payload (a validated proposal should never hit this).
    """
    kind = proposal.get("kind")
    payload = proposal.get("payload") or {}
    if kind == "state" and payload.get("key") in PROPOSABLE_STATE_KEYS:
        store.set_state(payload["key"], str(payload["value"]), source="llm_inferred")
        return summarize_candidate(kind, payload)
    if kind == "episode" and payload.get("episode_type") in PROPOSABLE_EPISODE_TYPES:
        store.log_episode(
            payload["episode_type"],
            outcome=payload.get("outcome"),
            context=payload.get("context"),
            notes=payload.get("notes"),
        )
        return summarize_candidate(kind, payload)
    raise ValueError(f"cannot apply proposal: {proposal!r}")
