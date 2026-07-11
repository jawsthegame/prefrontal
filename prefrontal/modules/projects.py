"""Project staleness module.

A project groups todos/commitments/focus sessions toward some outcome — but a
project with open work that hasn't been *touched* in weeks is exactly the kind of
open loop that quietly rots. This module re-surfaces it: "you haven't touched
*Kitchen Remodel* in 3 weeks — still on it?"

"Touched" is the most recent of any assigned focus session, todo update, or
commitment update (see :meth:`ProjectsRepo.stale_project_candidates`). At most one
project per tick (the stalest), and no more than once a week per project, so a pile
of neglected projects trickles out rather than nagging in a burst. The cutoff is
``project_stale_days`` in coaching state (default 21). Local-first and pure — like
every module, it only returns cues; the engine decides channel/quiet-hours.
"""

from __future__ import annotations

from typing import Any

from prefrontal.clock import parse_ts as _parse_ts
from prefrontal.coaching import (
    CoachContext,
    Cue,
    _fired_key,
    note_hint,
    stamp_pending_engagement,
    sweep_engagement,
)
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register

#: coaching_state key holding the "gone quiet" cutoff, in days.
STALE_DAYS_KEY = "project_stale_days"
DEFAULT_STALE_DAYS = 21
#: Don't re-nudge the same project more than once per this many days.
RENUDGE_DAYS = 7
#: episode_type for a staleness nudge's engagement outcome (see :meth:`before_collect`).
ENGAGE_EPISODE = "project"


def _stale_days(store: MemoryStore) -> int:
    """The configured staleness cutoff in days (default, on a bad value too)."""
    try:
        return int(store.get_state(STALE_DAYS_KEY) or DEFAULT_STALE_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_STALE_DAYS


class ProjectStalenessModule(Module):
    """Re-surface an active project with open work that has gone quiet."""

    key = "projects"
    title = "Projects"
    challenge = (
        "A project is a bet on finishing something; but one with open todos that "
        "hasn't been touched in weeks slips out of mind while still feeling like a "
        "commitment — the quiet, guilt-accruing kind of open loop."
    )
    default_state = {STALE_DAYS_KEY: str(DEFAULT_STALE_DAYS)}

    def interventions(self) -> list[Intervention]:
        return [
            Intervention(
                name="staleness",
                description=(
                    "Gently re-surface an active project with open todos that hasn't "
                    "been touched (focus/todo/commitment) past the staleness cutoff — "
                    "'still on it, or should this be parked?'"
                ),
                trigger="a project with open work has gone quiet past project_stale_days",
                status="active",
            ),
        ]

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Emit at most one nudge — the stalest project past the cutoff.

        Candidates come back stalest-first; the first one past the cutoff that
        hasn't been nudged within :data:`RENUDGE_DAYS` is surfaced. The engine
        still applies quiet hours and its own debounce downstream.
        """
        cutoff = _stale_days(store)
        for project in store.stale_project_candidates():
            last = _parse_ts(project.get("last_activity_at"))
            if last is None:
                continue
            days = (ctx.now - last).days
            if days < cutoff:
                continue  # not stale enough (list is stalest-first; rest are fresher)
            dedup_key = f"project_stale:{project['id']}"
            fired = _parse_ts(store.get_state(_fired_key(dedup_key)))
            if fired is not None and (ctx.now - fired).days < RENUDGE_DAYS:
                continue  # nudged about this project recently enough
            n = project["open_todos"]
            text = (
                f"You haven't touched *{project['name']}* in {days} days — still on "
                f"it? ({n} open todo{'s' if n != 1 else ''})"
            )
            return [
                Cue(
                    module=self.key,
                    intervention="staleness",
                    urgency="nudge",
                    text=text + note_hint(project.get("notes")),
                    context_key="project",
                    dedup_key=dedup_key,
                    ref={"project_id": project["id"]},
                )
            ]
        return []

    def before_collect(self, store: MemoryStore, ctx: CoachContext) -> None:
        """Mature prior staleness nudges into engagement outcomes.

        A staleness nudge has no tappable answer, so its honest signal is whether
        the project got *touched* afterward. Once the re-nudge window
        (:data:`RENUDGE_DAYS`) has passed, log the outcome — ``success`` if the
        project saw activity after we nudged (or dropped out of the stale set by
        being worked, completed, or archived), ``ignored`` if it's still sitting
        untouched — via the engine's ``before_collect`` hook. This is the learning
        signal a bare ``evaluate`` never fed back.
        """
        candidates = {c["id"]: c for c in store.stale_project_candidates()}

        def verdict(target: str, fired: Any) -> str | None:
            if (ctx.now - fired).days < RENUDGE_DAYS:
                return None  # still inside the window we'd wait before re-nudging
            project = candidates.get(int(target))
            if project is None:
                return "engaged"  # worked / completed / archived out of the stale set
            last = _parse_ts(project.get("last_activity_at"))
            return "engaged" if (last is not None and last > fired) else "ignored"

        sweep_engagement(
            store, ctx.now, module=self.key, episode_type=ENGAGE_EPISODE, verdict=verdict
        )

    def after_fire(self, store: MemoryStore, decisions: list[Any], ctx: CoachContext) -> None:
        """Mark each fired staleness nudge as awaiting an engagement verdict."""
        stamp_pending_engagement(
            store, decisions, ctx.now, module=self.key, target_key="project_id"
        )

    def profile_section(self, store: MemoryStore) -> str | None:
        """Note how many active projects carry open work (light context).

        The precise "gone quiet" day math needs the tick clock (``ctx.now``), which
        the profile pass doesn't have, so this stays a coarse count of projects with
        open todos plus the configured cutoff — enough context without duplicating
        :meth:`evaluate`'s per-project date logic.
        """
        candidates = store.stale_project_candidates()
        if not candidates:
            return None
        lines = [
            f"- {len(candidates)} active project(s) with open todos "
            f"(staleness nudges fire past {_stale_days(store)} days quiet)."
        ]
        # Fold in how those nudges have landed (from before_collect's outcomes) so
        # the profile reflects whether re-surfacing a stale project actually moves it.
        outcomes = store.episodes_by_type(ENGAGE_EPISODE, limit=50)
        if outcomes:
            engaged = sum(1 for e in outcomes if e.get("outcome") == "success")
            lines.append(
                f"- Staleness nudges land {engaged}/{len(outcomes)} of the time "
                "(project touched afterward)."
            )
        return "\n".join(lines)


register(ProjectStalenessModule())
