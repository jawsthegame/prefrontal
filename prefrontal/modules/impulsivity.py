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

from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register


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
