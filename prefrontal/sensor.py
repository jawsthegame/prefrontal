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
from collections import defaultdict
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

#: Minimum resolved (accepted + rejected) proposals before the sensor's
#: accept-rate is worth reporting — below this it's noise. Mirrors §4's
#: ``MIN_CALIBRATION_SAMPLES`` sample gate: report "not enough data yet" rather
#: than a verdict drawn from two decisions.
MIN_SENSOR_CALIBRATION_SAMPLES = 5

#: Minimum resolved proposals for a single *target* (one state key or episode
#: type) before its accept-rate is trusted enough to flag as chronically rejected.
MIN_TARGET_SAMPLES = 3

#: A target at/below this accept-rate (with ≥ ``MIN_TARGET_SAMPLES`` decisions) is
#: "chronically rejected" — the sensor keeps proposing something the human keeps
#: declining. ``0.34`` ≈ rejected two times out of three or worse.
LOW_PRECISION_ACCEPT_RATE = 0.34

#: Minimum distinct accepted *state* keys the durability check needs before it
#: reports a verdict — below this, "held up vs reversed" is noise (mirrors the
#: precision sample gate).
MIN_DURABILITY_SAMPLES = 3

#: coaching_state keys the calibration pass persists. The verdict is surfaced in
#: the behavioral profile; ``SENSOR_REJECTED_KEY`` (a comma-joined list of flagged
#: ``kind:name`` targets) also feeds back into the extraction prompt so the sensor
#: stops re-proposing settings the user reliably declines. Read side reads these
#: as literal keys (as the §4 bias verdict is), so there's no import coupling.
SENSOR_ACCEPT_RATE_KEY = "sensor_accept_rate"
SENSOR_SAMPLES_KEY = "sensor_calibration_samples"
SENSOR_REJECTED_KEY = "sensor_rejected_targets"

#: coaching_state keys the *durability* check persists — the post-acceptance
#: outcome half of learning §2. ``SENSOR_DURABILITY_RATE_KEY`` is the fraction of
#: accepted state settings still standing; ``SENSOR_REVERSED_KEY`` a comma-joined
#: list of ``state:<key>`` targets the user later changed away. Surfaced in the
#: profile; a *diagnostic*, not (yet) an auto-act (see ``compute_proposal_durability``).
SENSOR_DURABILITY_RATE_KEY = "sensor_durability_rate"
SENSOR_DURABILITY_SAMPLES_KEY = "sensor_durability_samples"
SENSOR_REVERSED_KEY = "sensor_reversed_targets"

SENSOR_SYSTEM_PROMPT = (
    "You are a careful note-reader for an ADHD assistant. You turn a free-text "
    "note or a conversation transcript into STRUCTURED CANDIDATE updates that a "
    "human will review before anything is saved. You never invent numbers or "
    "facts the source doesn't state. If it contains nothing worth recording, "
    "return an empty JSON array. Output ONLY a JSON array, no prose."
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


def _allowlist_instructions(
    *, quote_hint: str, avoid_keys: frozenset[str] = frozenset()
) -> str:
    """The shared "here's the JSON shape + the exact allowlists" body.

    Identical for a note and a transcript — the safety model (allowlist,
    no-fabricated-numbers) doesn't change with the input shape; only the framing
    of the source and the ``quote_hint`` (what a good rationale quotes) differ.

    ``avoid_keys`` closes the sensor's calibration feedback loop: state keys the
    user has *chronically rejected* (per :func:`compute_sensor_calibration`) are
    named here so the model stops volunteering them. It's a soft de-emphasis, not
    a hard block — the key stays on the allowlist and a clearly-stated request
    still proposes it; the human still confirms either way.
    """
    body = (
        "Each item is a JSON object with:\n"
        '  - "kind": "state" or "episode"\n'
        '  - for "state": "key" (one of: '
        f"{', '.join(sorted(PROPOSABLE_STATE_KEYS))}) and "
        '"value" (a short string)\n'
        '  - for "episode": "episode_type" (one of: '
        f"{', '.join(sorted(PROPOSABLE_EPISODE_TYPES))}), optional "
        '"outcome" (one of: success, partial, miss), optional "context" '
        '(a short label, e.g. the task or place), optional "notes"\n'
        f'  - "rationale": a short reason, ideally quoting {quote_hint}\n'
        "Only propose a state key from that exact list. Do NOT include any "
        "numeric predicted/actual/duration fields on episodes. Return [] if "
        "nothing fits."
    )
    if avoid_keys:
        body += (
            "\nThe user has repeatedly declined changes to these settings — do NOT "
            "propose them unless the source explicitly and unambiguously asks: "
            f"{', '.join(sorted(avoid_keys))}."
        )
    return body


def _build_prompt(text: str, *, avoid_keys: frozenset[str] = frozenset()) -> str:
    """The grounded extraction prompt: the note plus the exact allowlists."""
    return (
        "From the NOTE below, extract candidate updates. "
        + _allowlist_instructions(quote_hint="the note", avoid_keys=avoid_keys)
        + f"\n\nNOTE:\n{text.strip()}"
    )


def render_transcript(turns: list[dict[str, Any]]) -> str:
    """Flatten conversation ``turns`` into a ``Speaker: line`` transcript string.

    Each turn is a dict with a ``speaker`` (or ``role``) label and ``text`` (or
    ``content``). Blank-text turns are skipped; a missing speaker renders as
    ``?``. This is the exact text the model reads, so it's a pure, testable
    rendering with no model call.
    """
    lines: list[str] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        speaker = str(turn.get("speaker") or turn.get("role") or "?").strip() or "?"
        text = str(turn.get("text") or turn.get("content") or "").strip()
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def _build_transcript_prompt(
    transcript: str, *, avoid_keys: frozenset[str] = frozenset()
) -> str:
    """The grounded extraction prompt for a multi-speaker conversation.

    Same allowlist body as :func:`_build_prompt`, but framed to attribute signal
    to *the user* the assistant supports — a transcript may quote other people,
    and their facts must not be recorded as the user's.
    """
    return (
        "From the CONVERSATION below, extract candidate updates ABOUT THE USER "
        "(the person this assistant supports). The transcript may include other "
        "speakers; only record things that reflect the USER's own preferences, "
        "behavior, or state — never facts about other participants. "
        + _allowlist_instructions(
            quote_hint="the line you drew it from", avoid_keys=avoid_keys
        )
        + f"\n\nCONVERSATION:\n{transcript}"
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
    text: str,
    *,
    client: Generator | None = None,
    avoid_keys: frozenset[str] | None = None,
) -> list[Candidate]:
    """Read free text and return validated candidate updates (never writes).

    Args:
        text: The free-text note / observation.
        client: A :class:`~prefrontal.integrations.Generator` — the local Ollama
            client, or Claude when the ``sensor`` agent is opted into the
            Anthropic provider (tests inject one). Defaults to the local server.
        avoid_keys: State keys the user has chronically rejected — the sensor's
            calibration feedback (see :func:`avoided_state_keys`). Named in the
            prompt so the model stops volunteering them; validation is unchanged.

    Returns:
        Validated :class:`Candidate` objects. Empty when the note yields nothing,
        or when the model is unreachable (an honest no-guess fallback).
    """
    if not text or not text.strip():
        return []
    client = client or OllamaClient.from_settings()
    prompt = _build_prompt(text, avoid_keys=avoid_keys or frozenset())
    try:
        reply = client.generate(prompt, system=SENSOR_SYSTEM_PROMPT)
    except ProviderError:
        return []  # no model → observe nothing rather than invent
    candidates = [c for c in (_validate(r) for r in _coerce_json_array(reply)) if c is not None]
    return candidates


def extract_candidates_from_transcript(
    turns: list[dict[str, Any]],
    *,
    client: Generator | None = None,
    avoid_keys: frozenset[str] | None = None,
) -> list[Candidate]:
    """Read a conversation transcript and return validated candidate updates.

    The transcript counterpart of :func:`extract_candidates`: it reads a
    multi-turn conversation (``{"speaker", "text"}`` turns) instead of a single
    note, so behavioral signal spread across a back-and-forth — a journaling
    dialogue, a captured chat, a coaching conversation — can be observed. The
    prompt attributes signal to the *user* the assistant supports, not to other
    speakers in the transcript.

    The safety model is identical to :func:`extract_candidates`: the same
    allowlist and validation gate every candidate, everything lands **pending**,
    and an unreachable model yields no candidates rather than a guess.

    Args:
        turns: Conversation turns, each a dict with a ``speaker`` (or ``role``)
            label and ``text`` (or ``content``). Rendered via
            :func:`render_transcript`.
        client: A :class:`~prefrontal.integrations.Generator` (Ollama, or Claude
            when the ``sensor`` agent is opted into Anthropic). Defaults to local.
        avoid_keys: State keys the user has chronically rejected — de-emphasized
            in the prompt (see :func:`extract_candidates` / :func:`avoided_state_keys`).

    Returns:
        Validated :class:`Candidate` objects (possibly empty).
    """
    transcript = render_transcript(turns)
    if not transcript.strip():
        return []
    client = client or OllamaClient.from_settings()
    prompt = _build_transcript_prompt(transcript, avoid_keys=avoid_keys or frozenset())
    try:
        reply = client.generate(prompt, system=SENSOR_SYSTEM_PROMPT)
    except ProviderError:
        return []  # no model → observe nothing rather than invent
    return [c for c in (_validate(r) for r in _coerce_json_array(reply)) if c is not None]


def record_candidates(store: MemoryStore, candidates: list[Candidate]) -> list[int]:
    """Persist candidates as *pending* proposals; return their new ids."""
    return [
        store.add_proposal(kind=c.kind, payload=c.payload, rationale=c.rationale)
        for c in candidates
    ]


@dataclass(frozen=True)
class TargetPrecision:
    """Accept/reject tally for one proposal *target* (a state key or episode type)."""

    target: str  # a ``kind:name`` label, e.g. "state:responsive_hours_end", "episode:task"
    accepted: int
    rejected: int

    @property
    def resolved(self) -> int:
        return self.accepted + self.rejected

    @property
    def accept_rate(self) -> float:
        return self.accepted / self.resolved if self.resolved else 0.0


@dataclass(frozen=True)
class SensorCalibration:
    """Is the LLM sensor proposing things worth keeping? (learning §2 feedback.)

    The honest quality signal for a *sensor* is its **precision**: of the
    proposals a human has resolved, how many were accepted vs rejected — overall
    and per target. Unlike §4's numeric walk-forward
    (:func:`prefrontal.memory.patterns.bias_calibration`), this needs no
    predicted/actual pairs, so it works uniformly across state and episode
    proposals. A target the user keeps rejecting is surfaced in ``flagged`` and
    fed back into the extraction prompt so the sensor stops proposing it — a
    self-correcting loop grounded entirely in recorded accept/reject decisions,
    nothing inferred. ``status="insufficient"`` (not an exception) when too few
    proposals have been resolved to say anything, so callers surface "not enough
    data yet".
    """

    status: str  # "ok" | "insufficient"
    resolved: int  # accepted + rejected decisions seen
    accepted: int = 0
    rejected: int = 0
    accept_rate: float | None = None
    by_target: tuple[TargetPrecision, ...] = ()
    flagged: tuple[str, ...] = ()  # ``kind:name`` targets chronically rejected


def _proposal_target(kind: str, payload: dict[str, Any]) -> str:
    """A stable ``kind:name`` label a proposal's accept/reject tally groups under."""
    if kind == "state":
        return f"state:{payload.get('key')}"
    if kind == "episode":
        return f"episode:{payload.get('episode_type')}"
    return f"{kind}:?"


def compute_sensor_calibration(
    proposals: list[dict[str, Any]],
    *,
    min_samples: int = MIN_SENSOR_CALIBRATION_SAMPLES,
    min_target_samples: int = MIN_TARGET_SAMPLES,
    low_precision: float = LOW_PRECISION_ACCEPT_RATE,
) -> SensorCalibration:
    """Measure the sensor's precision from resolved proposals (pure, no store).

    Counts accepted vs rejected over the resolved proposals, overall and grouped
    by target (``_proposal_target``), and flags targets with enough decisions
    whose accept-rate is at/below ``low_precision``. Pending rows are ignored.
    Returns ``status="insufficient"`` below ``min_samples`` resolved decisions.

    Args:
        proposals: Proposal dicts (any status; only resolved ones are counted).
        min_samples: Minimum resolved decisions before reporting a verdict.
        min_target_samples: Minimum decisions for a target before it can be flagged.
        low_precision: Accept-rate at/below which a well-sampled target is flagged.

    Returns:
        A :class:`SensorCalibration`.
    """
    resolved_rows = [p for p in proposals if p.get("status") in ("accepted", "rejected")]
    n = len(resolved_rows)
    accepted = sum(1 for p in resolved_rows if p["status"] == "accepted")
    rejected = n - accepted
    if n < min_samples:
        return SensorCalibration(
            status="insufficient", resolved=n, accepted=accepted, rejected=rejected
        )

    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # target -> [accepted, rejected]
    for p in resolved_rows:
        target = _proposal_target(p["kind"], p.get("payload") or {})
        tally[target][0 if p["status"] == "accepted" else 1] += 1
    by_target = tuple(
        TargetPrecision(target=t, accepted=a, rejected=r) for t, (a, r) in sorted(tally.items())
    )
    flagged = tuple(
        tp.target
        for tp in by_target
        if tp.resolved >= min_target_samples and tp.accept_rate <= low_precision
    )
    return SensorCalibration(
        status="ok",
        resolved=n,
        accepted=accepted,
        rejected=rejected,
        accept_rate=round(accepted / n, 2),
        by_target=by_target,
        flagged=flagged,
    )


def recompute_sensor_calibration(store: MemoryStore) -> SensorCalibration:
    """Measure the sensor's precision and persist the verdict (learning §2 feedback).

    The learning-pass counterpart for the sensor, run alongside
    :func:`~prefrontal.memory.patterns.recompute_patterns`. Reads the full
    resolved-proposal history, computes accept-rate overall and per target, and
    persists a compact verdict — ``sensor_accept_rate``,
    ``sensor_calibration_samples``, and ``sensor_rejected_targets`` (the flagged
    ``kind:name`` list) — with ``source='inferred'``. The verdict shows in the
    behavioral profile, and the flagged list feeds back into the extraction
    prompt via :func:`avoided_state_keys`. Below the sample gate it persists
    nothing (an honest "not enough data yet"). Returns the full result for CLI use.
    """
    calibration = compute_sensor_calibration(store.all_resolved_proposals())
    if calibration.status == "ok":
        store.set_state(SENSOR_ACCEPT_RATE_KEY, str(calibration.accept_rate), source="inferred")
        store.set_state(SENSOR_SAMPLES_KEY, str(calibration.resolved), source="inferred")
        store.set_state(SENSOR_REJECTED_KEY, ",".join(calibration.flagged), source="inferred")
    return calibration


@dataclass(frozen=True)
class ProposalDurability:
    """Did accepted proposals actually *hold up*? (learning §2, the outcome half.)

    Sensor **precision** (:class:`SensorCalibration`) asks whether a proposal was
    accepted *at review time*. Durability asks the follow-on, in-hindsight question:
    of the settings a human accepted (written ``source="llm_inferred"``), how many
    are **still standing**, versus later changed away by an explicit user edit? A
    setting accepted in the moment and then reverted is a quality miss precision
    can't see — the closest honest analog, on the sensor's non-numeric allowlist, to
    §4's "did the adaptation actually help?" walk-forward.

    Scope and honesty (deliberately narrow, so the number means what it says):

    - **State proposals only.** An accepted *episode* proposal is a one-off log, not
      a setting, so it can't be "reversed" — episodes are covered by precision alone.
    - **Coaching state keeps no history** (``coaching_state`` upserts in place), so
      this compares each key's *current* value against the *latest* accepted proposal
      for it — one bit per key, a snapshot, not a timeline. It therefore can't tell a
      chronically-reversed key from a once-reversed one, which is exactly why the
      flagged list is **surfaced as a diagnostic and not auto-fed** into
      :func:`avoided_state_keys` (unlike precision's ``flagged``): there isn't enough
      signal to justify suppressing a key on this basis. Left as-is, the way §4
      leaves drift a surfaced diagnostic rather than an adaptation.
    - Confounds are real (the user may change a setting for reasons unrelated to
      whether the sensor was right), so this is reported, never acted on silently.

    ``status="insufficient"`` (not an exception) below :data:`MIN_DURABILITY_SAMPLES`
    evaluated keys, so callers can say "not enough data yet".
    """

    status: str  # "ok" | "insufficient"
    evaluated: int  # distinct accepted state keys checked
    held_up: int = 0
    reversed: int = 0
    durability_rate: float | None = None
    reversed_targets: tuple[str, ...] = ()  # ``state:<key>`` later changed away


def compute_proposal_durability(
    resolved_proposals: list[dict[str, Any]],
    current_state: dict[str, dict[str, Any]],
    *,
    min_samples: int = MIN_DURABILITY_SAMPLES,
) -> ProposalDurability:
    """Measure whether accepted state settings still hold (pure, no store).

    For each distinct state key an *accepted* proposal set, compares the key's
    current coaching-state value against the **latest** accepted value for it: equal
    ⇒ *held up*; different or gone ⇒ *reversed* (a later explicit edit moved it).
    Grouping by the latest accepted proposal means the sensor revising its own
    earlier suggestion isn't counted as a reversal — only a change *after* the most
    recent acceptance is. See :class:`ProposalDurability` for scope and caveats.

    Args:
        resolved_proposals: Resolved proposal dicts (accepted + rejected;
            oldest-first, as :meth:`all_resolved_proposals` returns). Only accepted
            ``state`` rows are considered.
        current_state: ``key -> row`` map (``all_state()``), read for the live value.
        min_samples: Minimum distinct keys before a verdict is reported.

    Returns:
        A :class:`ProposalDurability`.
    """
    # Latest accepted value per state key (oldest-first input ⇒ last write wins).
    latest_accepted: dict[str, str] = {}
    for p in resolved_proposals:
        if p.get("status") != "accepted" or p.get("kind") != "state":
            continue
        payload = p.get("payload") or {}
        key = payload.get("key")
        if key in PROPOSABLE_STATE_KEYS and "value" in payload:
            latest_accepted[key] = str(payload["value"])

    evaluated = len(latest_accepted)
    if evaluated < min_samples:
        return ProposalDurability(status="insufficient", evaluated=evaluated)

    held, reversed_keys = 0, []
    for key, accepted_value in sorted(latest_accepted.items()):
        current = current_state.get(key)
        if current is not None and str(current.get("value")) == accepted_value:
            held += 1
        else:
            reversed_keys.append(f"state:{key}")

    n_reversed = len(reversed_keys)
    return ProposalDurability(
        status="ok",
        evaluated=evaluated,
        held_up=held,
        reversed=n_reversed,
        durability_rate=round(held / evaluated, 2),
        reversed_targets=tuple(reversed_keys),
    )


def recompute_proposal_durability(store: MemoryStore) -> ProposalDurability:
    """Measure accepted-proposal durability and persist the verdict (learning §2).

    The post-acceptance-outcome counterpart to
    :func:`recompute_sensor_calibration`, run in the same learning pass. Reads the
    resolved-proposal history and the live coaching state, computes how many
    accepted settings still stand, and persists a compact verdict —
    :data:`SENSOR_DURABILITY_RATE_KEY`, :data:`SENSOR_DURABILITY_SAMPLES_KEY`, and
    :data:`SENSOR_REVERSED_KEY` (the reversed ``state:<key>`` list) — with
    ``source='inferred'``. Surfaced in the profile; below the sample gate it
    persists nothing. Returns the full result for CLI use.
    """
    durability = compute_proposal_durability(
        store.all_resolved_proposals(), store.all_state()
    )
    if durability.status == "ok":
        store.set_state(
            SENSOR_DURABILITY_RATE_KEY, str(durability.durability_rate), source="inferred"
        )
        store.set_state(
            SENSOR_DURABILITY_SAMPLES_KEY, str(durability.evaluated), source="inferred"
        )
        store.set_state(
            SENSOR_REVERSED_KEY, ",".join(durability.reversed_targets), source="inferred"
        )
    return durability


def avoided_state_keys(store: MemoryStore) -> frozenset[str]:
    """State keys the sensor should stop volunteering, from the last calibration pass.

    Reads the persisted ``sensor_rejected_targets`` list, keeps the ``state:``
    targets, and returns their (still-allowlisted) key names — the set to pass as
    ``extract_candidates(..., avoid_keys=…)`` so the prompt de-emphasizes settings
    the user reliably declines. Empty until a calibration pass flags something.
    """
    raw = store.get_state(SENSOR_REJECTED_KEY) or ""
    keys = {t.split(":", 1)[1] for t in raw.split(",") if t.startswith("state:")}
    return frozenset(k for k in keys if k in PROPOSABLE_STATE_KEYS)


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


def describe_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    """A compact, review-ready view of a stored proposal row (no raw-payload noise).

    Shared by every surface that lists proposals for review — ``GET /proposals``,
    ``POST /observe``, and the ``POST /braindump`` fan-out — so they present a
    pending candidate identically.
    """
    return {
        "id": proposal["id"],
        "kind": proposal["kind"],
        "summary": summarize_candidate(proposal["kind"], proposal["payload"]),
        "rationale": proposal.get("rationale") or "",
        "status": proposal["status"],
        "created_at": proposal.get("created_at"),
    }


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
