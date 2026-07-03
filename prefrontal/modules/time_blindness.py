"""Time blindness module.

Time blindness is difficulty perceiving how much time has passed or how long
something will take. Its signature in the memory layer is the ``time_estimation``
pattern and the ``time_estimation_bias`` coaching value: tasks and departures
consistently take longer than predicted.

This module turns that data into concrete guidance (apply the learned multiplier,
add departure buffers) and declares the interventions that act on it.
"""

from __future__ import annotations

from prefrontal.coaching import CoachContext, Cue
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register

#: Default interval (minutes) between elapsed-time callouts during a focus block.
#: ``0`` disables them — off unless the user opts in, since not everyone wants a
#: periodic ping (and an aligned deep block is otherwise protected by Hyperfocus).
DEFAULT_ELAPSED_CALLOUT_MINUTES = 0


def elapsed_callout_message(task: str, minutes: int, name: str = "") -> str:
    """A gentle 'you've been on this N min' time check (no judgment, no ask)."""
    greeting = f"{name}, a" if name else "A"
    task_clause = f" on '{task}'" if task else ""
    return (
        f"⏳ {greeting} time check: about {minutes} min{task_clause} so far. "
        "Just so it doesn't slip by — carry on if you're in a good groove."
    )


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
        # Off by default; set to e.g. 30 to get a gentle time check every 30 min
        # during a focus block (elapsed_time_callouts).
        "elapsed_callout_minutes": str(DEFAULT_ELAPSED_CALLOUT_MINUTES),
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
                status="active",
            ),
            Intervention(
                name="elapsed_time_callouts",
                description="Periodic 'you've been on this N minutes' callouts during a block.",
                trigger="an active focus/work block",
                status="active",
            ),
        ]

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Emit a gentle elapsed-time callout at each interval of an active focus block.

        Opt-in: silent unless ``elapsed_callout_minutes`` > 0. For each active
        focus session, once it's run at least one interval, fires a low-key "N min
        so far" cue on each interval boundary — deduped per elapsed *bucket* so a
        tick running every few minutes fires each mark exactly once. This is the
        time-awareness counterpart to Hyperfocus's interrupts: Hyperfocus owns the
        alignment *check* and biological *break* (on ``/webhooks/focus/check``);
        this only surfaces elapsed time for someone who's opted in to piercing
        their own time blindness. ``nudge`` urgency — a gentle push, not a digest.
        """
        interval = int(store.get_float("elapsed_callout_minutes", DEFAULT_ELAPSED_CALLOUT_MINUTES))
        if interval <= 0:
            return []
        cues: list[Cue] = []
        for session in store.active_focus_sessions():
            elapsed = session.get("elapsed_minutes") or 0.0
            if elapsed < interval:
                continue  # not past the first mark yet
            bucket = int(elapsed // interval)  # 1 at the first interval, 2 at the second…
            minutes = bucket * interval  # the mark we're calling out (stable per bucket)
            cues.append(
                Cue(
                    module=self.key,
                    intervention="elapsed_time_callouts",
                    urgency="nudge",
                    text=elapsed_callout_message(
                        session.get("intended_task") or "", minutes, ctx.display_name
                    ),
                    context_key="focus",
                    dedup_key=f"elapsed_callout:{session['id']}:{bucket}",
                    ref={"session_id": session["id"], "elapsed_minutes": minutes},
                    suggested_channel="push",
                )
            )
        return cues

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
        callout = int(store.get_float("elapsed_callout_minutes", 0))
        if callout > 0:
            lines.append(f"Send a gentle time check every **{callout} min** during a focus block.")

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
