"""Impulsivity module.

Impulsivity is acting before reflecting — context-switching away from the current
task, snap decisions, interrupting a focus block to chase a new shiny thing. The
support pattern is *friction*: a brief, dismissible pause between impulse and
action, plus capturing the impulse somewhere so it isn't lost (which is often the
real anxiety driving the switch).

Its signature in the memory layer is ``context_switch`` patterns and frequent,
short ``task`` episodes that end without an ``outcome``.
"""

from __future__ import annotations

from typing import Protocol

from prefrontal.integrations.ollama import OllamaError
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register

#: Words past this many in the raw impulse get dropped from the heuristic title.
_CAPTURE_TITLE_MAX_WORDS = 8

#: System prompt for LLM-titling a captured impulse. Kept tight so a local model
#: returns a clean label, never a paragraph; the heuristic covers it if it doesn't.
CAPTURE_TITLE_SYSTEM_PROMPT = (
    "Rewrite the user's raw, half-formed note as a short todo title — an "
    "imperative phrase of at most 8 words, no quotes, no trailing punctuation. "
    "Reply with the title only."
)


class _Generator(Protocol):
    """The slice of :class:`~prefrontal.integrations.ollama.OllamaClient` used here."""

    def generate(self, prompt: str, *, system: str | None = None) -> str: ...


def heuristic_capture_title(impulse_text: str) -> str:
    """A clean-ish title from raw impulse text without a model.

    Takes the first few meaningful words and tidies them — the fallback when the
    LLM is down (mirrors ``location_anchor.heuristic_time_window``'s role). Always
    returns *something* non-empty for non-blank input so a capture never fails on
    titling.
    """
    words = impulse_text.strip().split()
    if not words:
        return "Captured impulse"
    title = " ".join(words[:_CAPTURE_TITLE_MAX_WORDS])
    if len(words) > _CAPTURE_TITLE_MAX_WORDS:
        title += "…"
    return title[:1].upper() + title[1:]


def infer_capture_title(
    impulse_text: str,
    *,
    client: _Generator | None = None,
    fallback: bool = True,
) -> str | None:
    """Title a captured impulse via the local LLM, falling back to the heuristic.

    Mirrors ``infer_time_window``: the model is consulted when a ``client`` is
    given, and a usable one-line reply wins; otherwise (model absent, erroring,
    or empty) it degrades to :func:`heuristic_capture_title`.

    Returns:
        A title string, or ``None`` for blank input (or a model-only miss with
        ``fallback=False``).
    """
    if not impulse_text or not impulse_text.strip():
        return None

    if client is not None:
        try:
            reply = client.generate(impulse_text.strip(), system=CAPTURE_TITLE_SYSTEM_PROMPT)
        except OllamaError:
            reply = ""
        cleaned = " ".join((reply or "").split()).strip().strip("\"'.")
        if cleaned:
            words = cleaned.split()
            if len(words) > _CAPTURE_TITLE_MAX_WORDS:
                cleaned = " ".join(words[:_CAPTURE_TITLE_MAX_WORDS])
            return cleaned
        if not fallback:
            return None

    return heuristic_capture_title(impulse_text)


class ImpulsivityModule(Module):
    """Adds a reflective pause between impulse and action."""

    key = "impulsivity"
    title = "Impulsivity"
    challenge = (
        "Acting before reflecting — abrupt context switches and snap decisions "
        "that pull attention off the intended task."
    )
    default_state = {
        "pause_seconds": "20",
        "capture_then_defer": "true",
    }

    def interventions(self) -> list[Intervention]:
        """Declare friction/capture interventions."""
        return [
            Intervention(
                name="reflective_pause",
                description="Insert a short, dismissible pause before an impulsive switch.",
                trigger="a context switch away from an active intended task",
            ),
            Intervention(
                name="capture_and_defer",
                description="Log the new impulse to the inbox so it's safe to return to later.",
                trigger="a captured idea/task during a focus block",
                status="active",
            ),
            Intervention(
                name="switch_rate_feedback",
                description="Surface how often switches happened today vs the baseline.",
                trigger="the end-of-day check-in",
            ),
        ]

    def profile_section(self, store: MemoryStore) -> str | None:
        """Summarize switch behavior and the configured pause."""
        lines: list[str] = []
        pause = store.get_state("pause_seconds")
        if pause:
            lines.append(
                f"Insert a **{pause}-second** reflective pause before honoring an "
                "impulsive context switch."
            )
        if (store.get_state("capture_then_defer") or "true").lower() == "true":
            lines.append(
                "Capture new impulses to the inbox and defer them rather than acting "
                "immediately — the capture itself relieves the urgency."
            )
        for p in store.get_patterns("context_switch"):
            observed = p.get("observed_value")
            if observed is not None:
                lines.append(
                    f"`{p['context_key']}`: ~{observed} switches observed "
                    f"(confidence {(p.get('confidence') or 0):.0%})."
                )
        return "\n".join(f"- {line}" for line in lines) if lines else None


register(ImpulsivityModule())
