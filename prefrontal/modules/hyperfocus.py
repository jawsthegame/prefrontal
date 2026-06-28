"""Hyperfocus module.

Hyperfocus is sustained, intense concentration. It is double-edged: *productive*
hyperfocus on the thing that matters is a superpower; *misdirected* hyperfocus
(on the wrong task, or past the point of eating/sleeping/leaving) is costly. The
distinction is the whole point of this module — it should protect good
hyperfocus and gently interrupt bad hyperfocus, never blanket-interrupt both.

Its signature in the memory layer is long ``task`` blocks where ``actual_value``
(duration) greatly exceeds ``predicted_value``, combined with whether the block
was on an intended task (``context``) and whether nudges were ``acknowledged``.
"""

from __future__ import annotations

from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register


class HyperfocusModule(Module):
    """Protects productive hyperfocus and interrupts misdirected hyperfocus."""

    key = "hyperfocus"
    title = "Hyperfocus"
    challenge = (
        "Sustained intense focus that is a strength when aimed at the right task "
        "but a liability when misdirected or when it overruns basic needs and "
        "commitments."
    )
    default_state = {
        "hyperfocus_block_minutes": "90",
        "protect_aligned_hyperfocus": "true",
        "hard_interrupt_minutes": "180",
    }

    def interventions(self) -> list[Intervention]:
        """Declare the asymmetric protect-vs-interrupt interventions."""
        return [
            Intervention(
                name="alignment_check",
                description="Classify a long block as aligned (protect) or off-task (redirect).",
                trigger="a focus block exceeding the soft block length",
            ),
            Intervention(
                name="protect_window",
                description="Suppress non-critical nudges while aligned hyperfocus is healthy.",
                trigger="an aligned block under the hard-interrupt ceiling",
            ),
            Intervention(
                name="biological_break",
                description="Escalating interrupt for water/food/movement past the ceiling.",
                trigger="any block exceeding the hard-interrupt length",
            ),
        ]

    def profile_section(self, store: MemoryStore) -> str | None:
        """Summarize how hyperfocus is being handled and recent long blocks."""
        lines: list[str] = []
        soft = store.get_state("hyperfocus_block_minutes")
        hard = store.get_state("hard_interrupt_minutes")
        protect = (store.get_state("protect_aligned_hyperfocus") or "true").lower() == "true"
        if soft and hard:
            stance = (
                "Protect aligned hyperfocus" if protect else "Interrupt on the soft cadence"
            )
            lines.append(
                f"{stance}: soft check at **{soft} min**, hard break at **{hard} min**."
            )
        lines.append(
            "Distinguish good vs bad hyperfocus before interrupting — only redirect "
            "off-task or need-overrunning blocks."
        )

        long_blocks = [
            t
            for t in store.episodes_by_type("task", limit=100)
            if (t.get("actual_value") or 0) and (t.get("predicted_value") or 0)
            and t["actual_value"] >= 2 * t["predicted_value"]
        ]
        if long_blocks:
            lines.append(
                f"{len(long_blocks)} recent block(s) ran 2x+ their estimate — "
                "candidate hyperfocus episodes to review."
            )
        return "\n".join(f"- {line}" for line in lines) if lines else None


register(HyperfocusModule())
