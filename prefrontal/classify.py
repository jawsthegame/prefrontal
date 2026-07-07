"""Classify a commitment as *yours*, a *kid's*, or *FYI* with the local LLM.

Some calendar events aren't your commitments — they just tell you where someone
else will be (a partner's "Harlequin Brow Appt"). Others are a *child's*
appointment (a dentist, a school event) that belongs on the shared household
sheet so both co-parents see it. The rest are your own. This module makes that
three-way call:

- ``self`` — your own commitment; can clash with another of yours.
- ``child`` — a kid's appointment/activity you're responsible for. Still a real,
  attendable obligation (it consumes your time and can conflict, like ``self``),
  but it also surfaces on the household sheet's "Upcoming appointments"
  (see :mod:`prefrontal.household`).
- ``fyi`` — informational only; where someone else will be. Never a conflict.

Two design choices keep it honest:

- **A deterministic roster pass first.** When an event title names one of the
  household's kids (:func:`roster_child_match`), it's tagged ``child`` outright —
  offline, no model needed — so "Sam — dentist" reliably lands on the shared
  sheet. The model is only consulted for titles the roster doesn't catch.
- **Graceful degradation.** If the model is unreachable or replies with junk we
  fall back to ``self`` — the conservative default, since treating a real
  commitment as FYI would hide a genuine conflict.
- **An evolving prompt.** Every time the user corrects a verdict in the UI, that
  correction is stored (see :meth:`MemoryStore.record_kind_feedback`) and folded
  back in here as a few-shot example, so the classifier drifts toward the user's
  own judgement over time.
"""

from __future__ import annotations

import re
from typing import Any

from prefrontal.commitments import KIND_CHILD, KIND_FYI, KIND_SELF, KINDS
from prefrontal.integrations import Generator
from prefrontal.integrations.ollama import OllamaError

#: Cap on how many learned examples to fold into the prompt. Most-recent first,
#: so newer corrections win; bounded to keep the prompt small and fast.
MAX_EXAMPLES = 20

_BASE_SYSTEM_PROMPT = (
    "You classify a single calendar event title as SELF, CHILD, or FYI.\n"
    "SELF = the user's own commitment: something they personally attend or do, "
    "and which could clash with another of their commitments.\n"
    "CHILD = a child's or dependent's appointment or activity that the user is "
    "responsible for — a dentist/doctor visit, school event, or lesson for a kid. "
    "Prefer CHILD over SELF whenever the event is about a child.\n"
    "FYI = informational only: an event that exists so the user knows where "
    "another adult (e.g. a partner) will be. The user does not attend it "
    "themselves. Examples: another person's beauty/grooming or medical appointments.\n"
    "Answer with exactly one word: SELF, CHILD, or FYI."
)




def build_system_prompt(examples: list[dict[str, Any]] | None = None) -> str:
    """Compose the classifier's system prompt, folding in learned examples.

    Args:
        examples: Feedback rows (``display`` title + corrected ``kind``), most
            useful first. Each becomes a labeled few-shot line so the model
            learns the user's corrections. ``None``/empty yields the base prompt.

    Returns:
        The system prompt string.
    """
    rows = (examples or [])[:MAX_EXAMPLES]
    if not rows:
        return _BASE_SYSTEM_PROMPT
    lines = [
        _BASE_SYSTEM_PROMPT,
        "",
        "Apply these user-confirmed examples (they override your priors):",
    ]
    for ex in rows:
        title = (ex.get("display") or ex.get("title") or "").strip()
        kind = (ex.get("kind") or "").strip().lower()
        if title and kind in KINDS:
            lines.append(f"- {title!r} => {kind.upper()}")
    return "\n".join(lines)


def parse_kind_reply(reply: str) -> str | None:
    """Extract ``self``/``child``/``fyi`` from a model reply, or ``None`` if unclear.

    The reply is meant to be a single word, but we tolerate surrounding prose:
    whichever label token appears *earliest* wins. The tokens don't overlap
    (none is a substring of another), so first-occurrence is unambiguous.
    """
    lowered = (reply or "").strip().lower()
    if not lowered:
        return None
    positions = {
        kind: at
        for kind in (KIND_SELF, KIND_CHILD, KIND_FYI)
        if (at := lowered.find(kind)) != -1
    }
    if not positions:
        return None
    return min(positions, key=positions.get)


def roster_child_match(title: str, child_names: list[str] | None) -> bool:
    """Whether ``title`` names a household child (case-insensitive, word-boundary).

    The deterministic, offline signal that a synced event is a kid's — so
    "Sam — dentist" is tagged ``child`` (and reaches the shared sheet) without
    the model. Word-boundary matching keeps "Sam" from firing on "Samuelson",
    while still catching the possessive "Sam's".
    """
    if not title or not child_names:
        return False
    for name in child_names:
        n = (name or "").strip()
        if n and re.search(rf"\b{re.escape(n)}\b", title, re.IGNORECASE):
            return True
    return False


def classify_kind(
    title: str,
    *,
    client: Generator | None = None,
    examples: list[dict[str, Any]] | None = None,
    child_names: list[str] | None = None,
) -> tuple[str, str]:
    """Classify a commitment title as ``self``, ``child``, or ``fyi``.

    Args:
        title: The event title to classify.
        client: An Ollama-like client; ``None`` skips the model.
        examples: Learned feedback examples to fold into the prompt.
        child_names: Household kids' names. When the title names one, the event is
            tagged ``child`` deterministically (source ``"roster"``) — no model
            call — so a kid's appointment reaches the shared household sheet even
            when Ollama is down.

    Returns:
        ``(kind, source)`` where ``source`` is ``"roster"`` (matched a kid's name),
        ``"llm"`` (the model decided), or ``"default"`` (fallback). A blank title
        is ``("self", "default")``.
    """
    if not title or not title.strip():
        return (KIND_SELF, "default")
    if roster_child_match(title, child_names):
        return (KIND_CHILD, "roster")
    if client is not None:
        try:
            reply = client.generate(
                title.strip(), system=build_system_prompt(examples)
            )
            kind = parse_kind_reply(reply)
        except OllamaError:
            kind = None
        if kind is not None:
            return (kind, "llm")
    return (KIND_SELF, "default")
