"""Time blindness module.

Time blindness is difficulty perceiving how much time has passed or how long
something will take. Its signature in the memory layer is the ``time_estimation``
pattern and the ``time_estimation_bias`` coaching value: tasks and departures
consistently take longer than predicted.

This module turns that data into concrete guidance (apply the learned multiplier,
add departure buffers) and declares the interventions that act on it.
"""

from __future__ import annotations

from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register


class TimeBlindnessModule(Module):
    """Corrects for underestimated durations and missed departure times."""

    key = "time_blindness"
    title = "Time Blindness"
    challenge = (
        "Difficulty sensing elapsed time and estimating task/travel duration, "
        "leading to chronic underestimation and late departures."
    )
    default_state = {
        "time_estimation_bias": "1.4",
        "departure_buffer_minutes": "10",
    }

    def interventions(self) -> list[Intervention]:
        """Declare time-awareness interventions."""
        return [
            Intervention(
                name="estimate_correction",
                description="Multiply the agent's raw time estimates by the learned bias.",
                trigger="any prediction of task or travel duration",
                status="active",
            ),
            Intervention(
                name="departure_buffer",
                description="Add a learned buffer and nudge earlier than the naive leave time.",
                trigger="an upcoming calendar event with a location",
            ),
            Intervention(
                name="elapsed_time_callouts",
                description="Periodic 'you've been on this N minutes' callouts during a block.",
                trigger="an active focus/work block",
            ),
        ]

    def profile_section(self, store: MemoryStore) -> str | None:
        """Summarize the learned time bias and any time-estimation patterns."""
        lines: list[str] = []
        bias = store.get_state("time_estimation_bias")
        if bias:
            try:
                pct = round((float(bias) - 1.0) * 100)
                lines.append(
                    f"Apply a **{bias}x** multiplier to all time estimates "
                    f"(historical ~{pct}% underestimate)."
                )
            except ValueError:
                pass
        buffer = store.get_state("departure_buffer_minutes")
        if buffer:
            lines.append(f"Add a **{buffer}-minute** buffer before departure nudges.")

        patterns = store.get_patterns("time_estimation")
        for p in patterns:
            variance = p.get("variance")
            if variance:
                lines.append(
                    f"`{p['context_key']}` runs {abs(variance)} over estimate "
                    f"(confidence {(p.get('confidence') or 0):.0%})."
                )
        return "\n".join(f"- {line}" for line in lines) if lines else None


register(TimeBlindnessModule())
