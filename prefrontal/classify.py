"""Classify a commitment as *yours* or *FYI* with the local LLM.

Some calendar events aren't your commitments — they just tell you where someone
else will be (a partner's "Harlequin Brow Appt", a kid's lesson). Those should
show up so you have context, but they must never register as a double-booking.

This module asks the local Ollama model to make that call. Two design choices
keep it honest:

- **Graceful degradation.** If the model is unreachable or replies with junk we
  fall back to ``self`` — the conservative default, since treating a real
  commitment as FYI would hide a genuine conflict.
- **An evolving prompt.** Every time the user corrects a verdict in the UI, that
  correction is stored (see :meth:`MemoryStore.record_kind_feedback`) and folded
  back in here as a few-shot example, so the classifier drifts toward the user's
  own judgement over time.
"""

from __future__ import annotations

from typing import Any

from prefrontal.commitments import KIND_FYI, KIND_SELF
from prefrontal.integrations import Generator
from prefrontal.integrations.ollama import OllamaError

#: Cap on how many learned examples to fold into the prompt. Most-recent first,
#: so newer corrections win; bounded to keep the prompt small and fast.
MAX_EXAMPLES = 20

_BASE_SYSTEM_PROMPT = (
    "You classify a single calendar event title as either SELF or FYI.\n"
    "SELF = the user's own commitment: something they personally attend or do, "
    "and which could clash with another of their commitments.\n"
    "FYI = informational only: an event that exists so the user knows where "
    "someone else (a partner, child, family member) will be. The user does not "
    "attend it themselves. Examples: another person's beauty/grooming, medical, "
    "or lesson appointments.\n"
    "Answer with exactly one word: SELF or FYI."
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
        if title and kind in (KIND_SELF, KIND_FYI):
            lines.append(f"- {title!r} => {kind.upper()}")
    return "\n".join(lines)


def parse_kind_reply(reply: str) -> str | None:
    """Extract ``self``/``fyi`` from a model reply, or ``None`` if unclear."""
    lowered = (reply or "").strip().lower()
    if not lowered:
        return None
    # Prefer an explicit standalone token; tolerate surrounding prose/quotes.
    if "fyi" in lowered and KIND_SELF not in lowered:
        return KIND_FYI
    if KIND_SELF in lowered and "fyi" not in lowered:
        return KIND_SELF
    # Both or neither present — take the first one that appears.
    fyi_at = lowered.find("fyi")
    self_at = lowered.find(KIND_SELF)
    if fyi_at == -1 and self_at == -1:
        return None
    if self_at == -1:
        return KIND_FYI
    if fyi_at == -1:
        return KIND_SELF
    return KIND_FYI if fyi_at < self_at else KIND_SELF


def classify_kind(
    title: str,
    *,
    client: Generator | None = None,
    examples: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Classify a commitment title as ``self`` or ``fyi``.

    Args:
        title: The event title to classify.
        client: An Ollama-like client; ``None`` skips the model.
        examples: Learned feedback examples to fold into the prompt.

    Returns:
        ``(kind, source)`` where ``source`` is ``"llm"`` when the model decided,
        else ``"default"``. A blank title is ``("self", "default")``.
    """
    if not title or not title.strip():
        return (KIND_SELF, "default")
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
