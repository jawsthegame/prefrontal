"""Task paralysis (task initiation) module.

Task paralysis is the inability to start a task despite intending to — the gap
between "I should do this" and actually beginning. Its signature in the memory
layer is ``task`` episodes that are never acknowledged or that resolve to
``miss`` without ever being started.

This module surfaces initiation friction and owns the initiation *policy*. All
three interventions are wired:

- **auto_decompose** — on the coaching tick, breaks a todo the user is *avoiding*
  into a tiny first step plus collapsed remaining steps. **Off by default** — the
  user opts in via Settings (``auto_decompose_enabled``); the on-demand "Break it
  down" button is always available regardless.
- **tiny_first_step** — ``POST /todos/{id}/decompose`` (and the panic /
  ``/todos/now`` surfaces) reframe a stalled task as one <5-minute action.
- **body_double_nudge** — :func:`repeat_stalled_tasks` finds tasks you keep
  bailing on (repeated ``miss`` episodes, not since resolved) and surfaces a
  start-together suggestion (``GET /todos/stuck`` and the profile).

The decomposition machinery itself lives in :mod:`prefrontal.todos`
(:func:`~prefrontal.todos.decompose_task`); this module owns the thresholds, the
stall detector, and the profile narrative.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from prefrontal.coaching import CoachContext, Cue, note_hint
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register
from prefrontal.todos import avoided_todos, focus_conflict

#: A task missed at least this many times (and not since resolved) is treated as
#: chronically stuck — the signal a solo start isn't working and body-doubling (a
#: start-together window) is worth offering. Tunable via ``body_double_min_misses``.
DEFAULT_BODY_DOUBLE_MIN_MISSES = 2


def _task_title(context: str | None) -> str | None:
    """Recover a todo's title from a ``task`` episode's context, or ``None``.

    Todo closes log ``context="todo done|dropped: <title>"`` (see
    :func:`prefrontal.todos.todo_episode_fields`); other ``task`` episodes
    (outings) carry no todo title and are ignored here.
    """
    if not context:
        return None
    for prefix in ("todo done:", "todo dropped:"):
        if context.startswith(prefix):
            return context[len(prefix):].strip() or None
    return None


def repeat_stalled_tasks(
    episodes: list[dict[str, Any]],
    *,
    min_misses: int = DEFAULT_BODY_DOUBLE_MIN_MISSES,
) -> list[dict[str, Any]]:
    """Find tasks the user keeps bailing on — the body-double signal.

    Groups ``task`` episodes by todo title and flags any title with at least
    ``min_misses`` ``miss`` outcomes whose *most recent* episode is still a miss
    — a title that was ultimately completed is considered resolved and dropped,
    so a since-finished task never nags. Pure and order-independent (recency is
    decided by episode ``id``).

    Args:
        episodes: ``task`` episodes (as from ``episodes_by_type("task")``).
        min_misses: Miss count at/above which a task counts as chronically stuck.

    Returns:
        ``[{"title", "misses", "attempts"}]``, most-missed first.
    """
    by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        title = _task_title(e.get("context"))
        if title:
            by_title[title].append(e)

    out: list[dict[str, Any]] = []
    for title, eps in by_title.items():
        misses = sum(1 for e in eps if e.get("outcome") == "miss")
        if misses < min_misses:
            continue
        latest = max(eps, key=lambda e: e.get("id") or 0)
        if latest.get("outcome") == "success":
            continue  # ultimately done — no longer stuck
        out.append({"title": title, "misses": misses, "attempts": len(eps)})
    out.sort(key=lambda x: (-x["misses"], -x["attempts"], x["title"]))
    return out


def body_double_message(title: str, misses: int) -> str:
    """A start-together suggestion for a task the user keeps abandoning."""
    return (
        f"You've bailed on “{title}” {misses} times — this one resists a solo "
        "start. Schedule a start-together window: 15 minutes, someone (or just a "
        "timer) alongside you, and only the first tiny step."
    )


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
        "body_double_min_misses": str(DEFAULT_BODY_DOUBLE_MIN_MISSES),
        # Automatic breakdown of avoided todos is opt-in — off unless the user
        # turns it on in Settings. The manual "Break it down" button is unaffected.
        "auto_decompose_enabled": "off",
    }

    def interventions(self) -> list[Intervention]:
        """Declare initiation-support interventions (all wired)."""
        return [
            Intervention(
                name="tiny_first_step",
                description=(
                    "Reframe a stalled task as one <5-minute concrete first "
                    "action (POST /todos/{id}/decompose; also fed to panic and "
                    "the /todos/now widget)."
                ),
                trigger="on demand, or a task you're about to start",
                status="active",
            ),
            Intervention(
                name="auto_decompose",
                description=(
                    "Break a task the user is *avoiding* into a tiny first step + "
                    "collapsed remaining steps — but only if the model judges it "
                    "worth decomposing (it can decline). Runs on the coaching tick "
                    "(sweep_avoided_decompositions), not at todo creation. Off by "
                    "default (auto_decompose_enabled); opt in via Settings. The "
                    "on-demand 'Break it down' button works regardless."
                ),
                trigger="a todo that has reached 'avoided' status (when opted in)",
                status="active",
            ),
            Intervention(
                name="body_double_nudge",
                description=(
                    "Surface a task you keep bailing on and suggest a "
                    "start-together window (GET /todos/stuck; named in the profile)."
                ),
                trigger="repeated misses on the same task",
                status="active",
            ),
            Intervention(
                name="clarify_ambiguous",
                description=(
                    "Notice a vague todo/commitment ('Tax', 'Mom') that can't be "
                    "started because it can't be named, ask ONE inline clarifying "
                    "question, and — once it resolves to a recognized task — offer "
                    "a guided walkthrough (prefrontal/clarify.py). The detection "
                    "sweep runs on the coaching tick (sweep_ambiguous_items, beside "
                    "the decomposition sweep) and fills the dashboard clarify card; "
                    "POST /clarifications/check is the on-demand twin."
                ),
                trigger="an ambiguous item on the schedule or todo list",
                status="active",
            ),
            Intervention(
                name="refocus",
                description=(
                    "When you're actively working on a lower-priority todo than one "
                    "you're avoiding-and-haven't-started (focus_conflict), gently "
                    "flag it and suggest switching — honest prioritization in the "
                    "moment, the push twin of the dashboard's focus-conflict banner. "
                    "Held during protected hyperfocus (is_focus_protected) and quiet "
                    "hours; deduped per conflict so it fires once, not every tick."
                ),
                trigger="a started todo ranks below a task you're avoiding",
                status="active",
            ),
        ]

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Fire the sharpest honest-prioritization nudge for right now.

        Two producers, sharpest first — one ``nudge`` cue at most:

        1. ``refocus`` — if you're *actively working on* a lower-priority todo than
           one you're avoiding (:func:`~prefrontal.todos.focus_conflict`), flag it
           and suggest a switch. This subsumes ``tiny_first_step`` for the tick: it
           names not just what you're skipping but that you're mid-flight on the
           wrong thing. Like every non-critical cue it's held while an aligned
           hyperfocus block is protected — flow that's on-task shouldn't be yanked
           — but that gate now lives centrally in the engine's
           :func:`~prefrontal.coaching.suppressed`, not here. Deduped per (working,
           instead) pair so it fires once per distinct conflict.
        2. ``tiny_first_step`` — otherwise, pick the worst-avoided open todo
           (``avoided_todos`` — age × priority, honest prioritization over the shiny
           thing) and reframe it as one <5-minute first action, preferring the
           stored decomposition's first step when it exists.

        Any note the user attached to the target todo is folded onto the end
        (:func:`~prefrontal.coaching.note_hint`), so the context they left rides
        along. Quiet hours + debounce are applied downstream by the coaching engine.
        """
        open_todos = store.open_todos(exclude_delegated=True)
        conflict = focus_conflict(open_todos, ctx.now)
        if conflict is not None:
            working = conflict["working_on"]
            instead = conflict["instead"]
            text = (
                f"You're mid-task on “{working['title']}”, but “{instead['title']}” "
                f"is more important and has sat {round(conflict['days_open'])}d. "
                f"Worth switching?{note_hint(instead.get('notes'))}"
            )
            return [
                Cue(
                    module=self.key,
                    intervention="refocus",
                    urgency="nudge",
                    text=text,
                    context_key="todo",
                    dedup_key=f"refocus:{working['id']}:{instead['id']}",
                    ref={"todo_id": instead["id"], "working_on_id": working["id"]},
                )
            ]

        avoided = avoided_todos(open_todos, ctx.now)
        if not avoided:
            return []
        top = avoided[0]
        todo = top["todo"]
        decomp = store.get_decomposition(todo["id"])
        first = (decomp or {}).get("first_step")
        opener = (
            f"Start tiny: {first}"
            if first
            else f"Start tiny — set a 5-minute timer and just open “{todo['title']}.”"
        )
        text = (
            f"You keep putting off “{todo['title']}” ({round(top['days_open'])}d and "
            f"counting). {opener}{note_hint(todo.get('notes'))}"
        )
        return [
            Cue(
                module=self.key,
                intervention="tiny_first_step",
                urgency="nudge",
                text=text,
                context_key="todo",
                dedup_key=f"tiny_first_step:{todo['id']}",
                ref={"todo_id": todo["id"]},
            )
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
            # Body-double signal: name the tasks that keep getting abandoned so the
            # coaching prose can suggest a start-together window for them.
            try:
                min_misses = int(
                    store.get_state("body_double_min_misses")
                    or DEFAULT_BODY_DOUBLE_MIN_MISSES
                )
            except (TypeError, ValueError):
                min_misses = DEFAULT_BODY_DOUBLE_MIN_MISSES
            stuck = repeat_stalled_tasks(tasks, min_misses=min_misses)
            if stuck:
                top = ", ".join(f"“{s['title']}” (×{s['misses']})" for s in stuck[:3])
                lines.append(
                    f"Keeps bailing on: {top}. A solo start isn't working — offer "
                    "a body-double / start-together window, not another reminder."
                )
        # Ambiguous items stall before they even become a "task" — surface the
        # ones awaiting a clarifying answer so coaching honesty covers naming, not
        # just starting (see prefrontal/clarify.py).
        pending = store.list_clarifications("pending", limit=20)
        if pending:
            titles = ", ".join(f"“{c['title']}”" for c in pending[:3])
            lines.append(
                f"Ambiguous, not yet honed: {titles} ({len(pending)} awaiting a "
                "clarifying answer). These stall on being *named*, not started — "
                "surface the inline question, don't just nudge to begin."
            )
        step = store.get_state("max_first_step_minutes")
        if step:
            lines.append(f"Keep proposed first steps under **{step} minutes**.")
        return "\n".join(f"- {line}" for line in lines) if lines else None


register(TaskParalysisModule())
