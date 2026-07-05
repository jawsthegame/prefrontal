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

#: coaching_state keys the calibration pass persists. The verdict is surfaced in
#: the behavioral profile; ``SENSOR_REJECTED_KEY`` (a comma-joined list of flagged
#: ``kind:name`` targets) also feeds back into the extraction prompt so the sensor
#: stops re-proposing settings the user reliably declines. Read side reads these
#: as literal keys (as the §4 bias verdict is), so there's no import coupling.
SENSOR_ACCEPT_RATE_KEY = "sensor_accept_rate"
SENSOR_SAMPLES_KEY = "sensor_calibration_samples"
SENSOR_REJECTED_KEY = "sensor_rejected_targets"

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


def _allowlist_instructions(*, quote_hint: str, avoid_keys: frozenset[str] = frozenset()) -> str:
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


#: The assistant's name, as a caller would speak it into a hands-free capture.
#: Anchors :func:`normalize_voice_note` — only a leading phrase that *names* the
#: assistant is treated as an address to strip, never a bare capture verb (which
#: might be real content, e.g. "remember to call the dentist").
ASSISTANT_NAME = "prefrontal"

#: Matches a leading "address to the assistant" that dictation-to-message flows
#: (e.g. Ray-Ban Meta glasses' "Hey Meta, send a message to Prefrontal: …") prepend
#: to a spoken note. Optional wake word / framing phrase, the assistant's name, then
#: a separator. See :func:`normalize_voice_note`.
_VOICE_ADDRESS_RE = re.compile(
    r"^\s*"
    r"(?P<lead>(?:hey|hi|ok|okay)\s+|"
    r"(?:(?:send\s+a\s+)?message\s+to|note\s+to|tell|for|to)\s+)?"
    rf"{ASSISTANT_NAME}"
    r"(?P<sep>\s*[,:;.\-—]+\s*|\s+)"
    r"(?:that\s+)?",  # "tell Prefrontal that …" — drop the dangling connector
    re.IGNORECASE,
)


def normalize_voice_note(text: str) -> str:
    """Strip a leading spoken address to the assistant from a dictated note.

    Hands-free capture through the glasses arrives as a message dictated to a
    contact — *"Hey Meta, send a message to Prefrontal: dentist wants me back in
    six months"* lands here as ``"Prefrontal, dentist wants me back in six
    months"``. The vocative "Prefrontal," is noise: it isn't part of the note and
    would otherwise leak into the sensor's rationale. This removes it.

    Conservative on purpose — it only strips a prefix that *names* the assistant
    (optionally behind a wake word or "send a message to …" framing), and only
    when that name is set off as an address: a wake/framing lead, or a punctuation
    separator. A note that merely starts with the word (``"Prefrontal is slow
    today"``) or a bare capture verb (``"remember to call the dentist"``) is left
    untouched. If stripping would empty the note, the original is kept.
    """
    if not text or not text.strip():
        return ""
    match = _VOICE_ADDRESS_RE.match(text)
    if not match:
        return text.strip()
    lead = match.group("lead")
    sep = match.group("sep") or ""
    # Only a vocative — a wake/framing lead or punctuation — counts as an address.
    # A bare "Prefrontal <word>" (name + space only) is likely a note *about* it.
    if not lead and not re.search(r"[,:;.\-—]", sep):
        return text.strip()
    remainder = text[match.end() :].strip()
    return remainder or text.strip()


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


def _build_transcript_prompt(transcript: str, *, avoid_keys: frozenset[str] = frozenset()) -> str:
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
        + _allowlist_instructions(quote_hint="the line you drew it from", avoid_keys=avoid_keys)
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
    text = normalize_voice_note(text)  # drop a dictated "Prefrontal, …" address
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
