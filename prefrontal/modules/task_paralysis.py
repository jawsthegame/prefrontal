"""Task paralysis (task initiation) module.

Task paralysis is the inability to start a task despite intending to — the gap
between "I should do this" and actually beginning. Its signature in the memory
layer is ``task`` episodes that are never acknowledged or that resolve to
``miss`` without ever being started.

This module surfaces initiation friction and declares interventions that lower
the activation energy to begin (decomposition, body-doubling nudges, tiny first
steps).
"""

from __future__ import annotations

from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register


class TaskParalysisModule(Module):
    """Reduces the activation energy required to start a task."""

    key = "task_paralysis"
    title = "Task Paralysis"
    challenge = (
        "Difficulty initiating tasks despite intent — the task stalls before it "
        "ever begins, especially for ambiguous or large work."
    )
    default_state = {
        "max_first_step_minutes": "5",
        "decomposition_threshold_minutes": "30",
    }

    def interventions(self) -> list[Intervention]:
        """Declare initiation-support interventions."""
        return [
            Intervention(
                name="tiny_first_step",
                description="Reframe a stalled task as a <5-minute concrete first action.",
                trigger="a task that is due/scheduled but unacknowledged",
            ),
            Intervention(
                name="auto_decompose",
                description="Break tasks over the threshold into ordered sub-steps.",
                trigger="a new task whose estimate exceeds the decomposition threshold",
            ),
            Intervention(
                name="body_double_nudge",
                description="Offer a scheduled co-working/start-together window.",
                trigger="repeated misses on the same task",
            ),
        ]

    def profile_section(self, store: MemoryStore) -> str | None:
        """Report task initiation friction from recent task episodes."""
        tasks = store.episodes_by_type("task", limit=100)
        lines: list[str] = []
        if tasks:
            stalled = sum(
                1 for t in tasks if not t.get("acknowledged") or t.get("outcome") == "miss"
            )
            rate = stalled / len(tasks)
            lines.append(
                f"Task initiation friction: {stalled}/{len(tasks)} recent tasks "
                f"stalled or missed ({rate:.0%})."
            )
            if rate >= 0.5:
                lines.append(
                    "High stall rate — default to offering a <5-minute first step "
                    "rather than the whole task."
                )
        step = store.get_state("max_first_step_minutes")
        if step:
            lines.append(f"Keep proposed first steps under **{step} minutes**.")
        return "\n".join(f"- {line}" for line in lines) if lines else None


register(TaskParalysisModule())
