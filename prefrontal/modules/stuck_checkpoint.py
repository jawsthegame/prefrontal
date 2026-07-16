"""Stuck-checkpoint module — a triage fork for the item you've avoided for *weeks*.

Avoidance is emotion regulation, not time management (Sirois & Pychyl): an aversive
task triggers negative affect, avoiding it repairs the mood short-term, the guilt
feeds more avoidance. An item that's been avoided for *weeks* sits at the bottom of
that loop — and Prefrontal's avoidance score is monotonic (older ⇒ ranks higher),
so left alone it just climbs the "worst-avoided" pile and gets re-surfaced *harder*,
which adds exactly the negative affect that sustains the loop. Pushing louder is the
wrong move.

So past a longer floor (:data:`~prefrontal.todos.DEFAULT_CHECKPOINT_MIN_DAYS`) the
right response is a **one-time triage fork**, not another nudge: break it into a
first step, defer it to a real date, or let it go guilt-free. The goal-adjustment
literature (Wrosch) is the reason "let it go" is a first-class option — consciously
dropping a stale item is adaptive, not a failure — and also the reason the checkpoint
*asks* rather than auto-deciding: avoidance duration is **not** evidence a task is
unattainable (it may be perfectly doable and just aversive), so the user makes the call.

Surfacing is calm-first. The default surface is the morning briefing's "🧭 Time to
decide" block (batched, user-initiated attention, lowest affective cost) — a
weeks-avoided item is by definition not time-sensitive, so one more day costs nothing.
This module owns the *narrow exception*: a long-avoided item that is **also** genuinely
deadline-pressured (overdue or due within two days, :func:`~prefrontal.todos.deadline_pressure`),
where "another day is fine" stops being true, earns one interruptive push.

Discipline, mirroring :mod:`~prefrontal.modules.open_window`:

- **Once per item, ever.** A per-todo marker suppresses re-firing for good once the
  checkpoint has been offered — the whole point is to *stop* the escalating siren, so
  it never re-pesters. (A deferred item is snoozed out of the avoided set anyway, so it
  can't re-trigger while parked.)
- **Yields to a claimed todo.** If a higher-value cue has already claimed this todo
  this tick (``open_window`` offering it into a free gap — a more actionable "do it
  now"), the checkpoint stands down. It reads ``claimed_todo_ids`` but never adds to
  it: it's the politest todo cue, so a coinciding ``task_paralysis`` first-step nudge
  (which complements "time to decide" rather than clashing) is left to the engine's
  debounce / daily cap to dedupe.
"""

from __future__ import annotations

from prefrontal.clock import TS_FMT, utcnow
from prefrontal.coaching import CoachContext, Cue, Decision, note_hint
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register
from prefrontal.todos import deadline_pressure, long_avoided_todos

#: Coaching-state key prefix for the once-per-item marker. ``f"{PREFIX}:{todo_id}"``
#: holds the day the checkpoint was offered for that todo; its presence (any value)
#: means "already asked — never again", so the interruption fires at most once per
#: item over its whole life. Keyed per todo so closing/re-opening a *different* item
#: is unaffected.
_MARKER_PREFIX = "stuck_checkpoint"


def _marker_key(todo_id: int) -> str:
    """The per-todo "already checkpointed" marker key."""
    return f"{_MARKER_PREFIX}:{todo_id}"


def checkpoint_message(
    *, todo_title: str, days_open: float, pressure: str, notes: str | None
) -> str:
    """Phrase the triage fork, naming the age and the deadline pressure.

    Deterministic (the engine's optional LLM rewrite never touches a ``nudge``), so
    this text ships verbatim. Presents the three outcomes as a question — the user
    acts on it in the app (break down / defer / drop); the push only prompts the
    decision. Self-compassionate by design: the evidence is specific that guilt
    framing *increases* the avoidance it's trying to shift, so it names the fact
    plainly ("sliding {n}d") without a scolding "you keep…".
    """
    deadline_phrase = "it's overdue" if pressure == "overdue" else "it's due soon"
    return (
        f"“{todo_title}” has been sliding {round(days_open)}d and now {deadline_phrase}. "
        "Worth a decision: break it into a first step, park it for a real date, or let "
        f"it go?{note_hint(notes)}"
    )


class StuckCheckpointModule(Module):
    """A one-time triage fork for a weeks-avoided item that's now up against a deadline."""

    key = "stuck_checkpoint"
    title = "Stuck Checkpoint"
    challenge = (
        "An important task avoided for weeks doesn't need to be pushed harder — that "
        "just adds the pressure that keeps it stuck. It needs a decision: make it "
        "smaller, defer it honestly, or let it go."
    )

    def channel_targets(self) -> dict[str, str]:
        """Checkpoint offers are ack-trackable, keyed by ``todo_id``.

        Its own trackable context (distinct from ``open_window``/``task_paralysis``)
        so channel learning calibrates how welcome *this* cue is on its own terms.
        """
        return {"stuck_checkpoint": "todo_id"}

    def interventions(self) -> list[Intervention]:
        """Declare the one intervention: the deadline-pressured triage fork (wired)."""
        return [
            Intervention(
                name="triage_fork",
                description=(
                    "When an item you've avoided for weeks is also up against a "
                    "deadline, offer once to decide its fate — break it down, defer "
                    "it to a real date, or drop it — instead of nagging you again."
                ),
                trigger="a long-avoided todo that's overdue or due within two days",
                status="active",
            ),
        ]

    def _offer(self, store: MemoryStore, ctx: CoachContext) -> Cue | None:
        """The pure per-tick decision: the checkpoint to raise now, or ``None``.

        Walks the long-avoided items worst-first and returns a cue for the first that
        is (a) genuinely deadline-pressured, (b) not already claimed by a
        higher-value cue this tick, and (c) not already checkpointed. Returns ``None``
        at every early-out so the caller treats "no offer" uniformly. A pure read —
        the marker write is deferred to :meth:`after_fire`.
        """
        candidates = long_avoided_todos(store.open_todos(exclude_delegated=True), ctx.now)
        for a in candidates:
            todo = a["todo"]
            tid = todo["id"]
            pressure = deadline_pressure(todo, ctx.now)
            if pressure is None:
                continue  # not time-critical → the calm briefing surface handles it
            if tid in ctx.claimed_todo_ids:
                continue  # a more actionable cue already owns this todo this tick
            if store.get_state(_marker_key(tid)):
                continue  # already asked once — never re-pester
            return Cue(
                module=self.key,
                intervention="triage_fork",
                urgency="nudge",
                text=checkpoint_message(
                    todo_title=todo["title"],
                    days_open=a["days_open"],
                    pressure=pressure,
                    notes=todo.get("notes"),
                ),
                context_key="stuck_checkpoint",
                dedup_key=f"stuck_checkpoint:{tid}",
                ref={"todo_id": tid, "days_open": a["days_open"], "pressure": pressure},
            )
        return None

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Emit the single checkpoint for right now, or nothing.

        Runs after every module's ``before_collect``, so ``claimed_todo_ids`` is fully
        populated — an ``open_window`` gap offer for the same todo (claimed in its
        ``before_collect``) is already visible and this stands down for it.
        """
        offer = self._offer(store, ctx)
        return [offer] if offer is not None else []

    def after_fire(
        self, store: MemoryStore, decisions: list[Decision], ctx: CoachContext
    ) -> None:
        """Stamp the once-per-item marker for a checkpoint that just fired.

        Held back from :meth:`evaluate` so that stays side-effect-free, and keyed on
        *fire* (not mere production) so a held (quiet-hours / debounced) offer still
        gets its one shot once it can land.
        """
        for d in decisions:
            if d.cue.module != self.key:
                continue
            tid = (d.cue.ref or {}).get("todo_id")
            if tid is None:
                continue
            store.set_state(_marker_key(int(tid)), ctx.now.strftime(TS_FMT), source="inferred")

    def profile_section(self, store: MemoryStore) -> str | None:
        """Note how many long-avoided items are awaiting a decision (light context)."""
        stuck = long_avoided_todos(store.open_todos(exclude_delegated=True), utcnow())
        if not stuck:
            return None
        return (
            f"- {len(stuck)} todo(s) avoided long enough to warrant a decision (break "
            "down / defer / drop) rather than another nudge; the checkpoint raises the "
            "deadline-pressured one and the briefing surfaces the rest calmly."
        )


register(StuckCheckpointModule())
