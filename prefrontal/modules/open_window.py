"""Calendar-gap-aware "open window" nudge module (M3).

Time blindness cuts both ways: the *departure* side (a commitment you'll be late
for) is Time Blindness's job, but the quieter failure is the **empty stretch that
slips by unused** — a genuine 30-minute gap before your next thing, spent
scrolling, while the form you've dodged twice sits open. This module watches for
exactly that shape: *right now* you're free for a real window before your next
commitment, and there's a worthwhile todo you've been avoiding that fits — so it
offers, once, to fill the gap with it ("You've got ~30 min before your 2pm — good
time to knock out the form you've dodged twice?").

It is the proactive *push* twin of the ``GET /todos/now`` pull widget and reuses
that endpoint's recipe wholesale — :func:`~prefrontal.scheduling.work_window_now`
for the waking-hours bound, :func:`~prefrontal.scheduling.available_now` for the
minutes free until the next commitment, :func:`~prefrontal.scheduling.filter_suggestible`
+ :func:`~prefrontal.scheduling.fit_todos` (with the same context-conditioned
:func:`~prefrontal.memory.patterns.task_bias_resolver`) for what fits, and
:func:`~prefrontal.scheduling.pick_now` over the :func:`~prefrontal.todos.avoided_todos`
ids for the honest pick. No new scheduling math — pure composition.

The cue is never critical (a free window is an *offer*, not an alarm), and every
downstream discipline — receptivity gate, dosage cap, quiet hours, channel choice,
ack/episode logging — is applied by the coaching engine to it like any other cue.
Two behaviors this module owns beyond emitting the cue:

- **Anti-spam.** A long free afternoon must not re-fire every tick. The per-todo
  ``dedup_key`` plus the engine's debounce cover the short term; on top of that a
  small last-offered marker (``todo_id``:``next_commitment_id``) suppresses an
  *identical* re-offer indefinitely, so the same gap is only pitched once until the
  situation actually changes (the todo closes, or the next commitment does).
- **Precedence over Task Paralysis.** ``task_paralysis`` also nudges the
  worst-avoided todo, regardless of free time. When this module offers a todo into
  a gap this tick it claims that todo id in the shared
  :class:`~prefrontal.coaching.CoachContext` (in :meth:`before_collect`, so the
  claim is visible whatever the module collection order), and ``task_paralysis``
  stands down for it — the gap-anchored cue is strictly more informative.
"""

from __future__ import annotations

from prefrontal.clock import TS_FMT, local_datetime, local_hour_of, parse_ts, utcnow
from prefrontal.coaching import CoachContext, Cue, Decision, note_hint
from prefrontal.config import get_settings
from prefrontal.memory.behavioral import behavior_nudge_clause
from prefrontal.memory.patterns import task_bias_resolver
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register
from prefrontal.scheduling import (
    DEFAULT_FIT_CAP_MINUTES,
    available_now,
    filter_suggestible,
    fit_todos,
    pick_now,
    window_config_for,
    work_window_now,
)
from prefrontal.todos import avoided_todos

#: Coaching-state key: fewest free minutes before the next commitment worth a
#: proactive gap offer. Below it the window is too short to start anything, so the
#: module stays quiet. Read via ``store.get_float`` with :data:`DEFAULT_GAP_MIN_MINUTES`
#: as the fallback, mirroring how the engine reads ``coach_daily_nudge_cap`` etc.
COACH_GAP_MIN_KEY = "coach_gap_min_minutes"

#: Default for :data:`COACH_GAP_MIN_KEY`. Fifteen minutes is enough to make a real
#: dent in a stalled task (the point of the offer) without pitching a window that's
#: gone before you've context-switched into it.
DEFAULT_GAP_MIN_MINUTES = 15.0

#: Coaching-state key holding the last *fired* offer's signature
#: (``"<todo_id>:<next_commitment_id>"``). An identical signature on a later tick is
#: the same pitch for the same gap, so it's held — the belt to the ``dedup_key`` +
#: debounce suspenders (see the module docstring's anti-spam note).
_LAST_OFFER_KEY = "coach_open_window_last"


def _offer_signature(todo_id: int, next_commitment_id: int | None) -> str:
    """The stable key identifying "this exact offer" for the anti-spam marker."""
    return f"{todo_id}:{next_commitment_id}"


def _local_clock(start_at: str | None, tz: str) -> str:
    """Render a commitment's UTC ``start_at`` as a local wall-clock time.

    Best-effort: an unparseable/absent timestamp yields ``""`` so the caller phrases
    the offer without a time rather than raising.
    """
    parsed = parse_ts(start_at)
    if parsed is None:
        return ""
    # %I is zero-padded and portable; strip the lead zero for a natural "2:00 PM".
    return local_datetime(parsed, tz).strftime("%I:%M %p").lstrip("0")


def open_window_message(
    *,
    free_minutes: int,
    todo_title: str,
    days_open: float,
    next_title: str | None,
    next_clock: str,
    notes: str | None,
    continuity: str = "",
) -> str:
    """Phrase the gap offer, naming the free minutes and (when known) the next thing.

    The concrete ask ("knock out X") and the numbers (free minutes, days open) are
    load-bearing — the engine's optional LLM phrasing pass never touches a ``nudge``,
    so this deterministic text is exactly what ships. The reschedule/snooze
    ``continuity`` clause ("you've circled this before") and any note on the todo
    ride along (:func:`~prefrontal.memory.behavioral.behavior_nudge_clause` /
    :func:`~prefrontal.coaching.note_hint`); both are leading-space-prefixed, so an
    empty value appends cleanly.
    """
    if next_title:
        when = f" before {next_title}" + (f" at {next_clock}" if next_clock else "")
    else:
        when = " right now"
    return (
        f"You've got ~{free_minutes} min{when}. Good time to knock out "
        f"“{todo_title}”? You've been putting it off ({round(days_open)}d and "
        f"counting).{continuity}{note_hint(notes)}"
    )


class OpenWindowModule(Module):
    """Offers a genuine free window, right now, for a todo you've been avoiding."""

    key = "open_window"
    title = "Open Window"
    challenge = (
        "Real free stretches between commitments slip by unused while an avoided "
        "task sits open — the gap is there, but nothing connects it to the thing "
        "worth doing in it."
    )
    default_state = {
        # Fewest free minutes before the next commitment worth a proactive offer.
        COACH_GAP_MIN_KEY: str(int(DEFAULT_GAP_MIN_MINUTES)),
    }

    def channel_targets(self) -> dict[str, str]:
        """Open-window offers are ack-trackable, keyed by ``todo_id``.

        Declares ``open_window`` as its own trackable context (distinct from
        ``task_paralysis``'s ``todo``) so channel-response — and, through it, the
        receptivity gate — learn how welcome *this* cue is on its own terms, rather
        than pooling it with other todo nudges (see
        :func:`prefrontal.coaching.channel_targets_for`).
        """
        return {"open_window": "todo_id"}

    def interventions(self) -> list[Intervention]:
        """Declare the one intervention: the calendar-gap offer (wired)."""
        return [
            Intervention(
                name="gap_offer",
                description=(
                    "On the coaching tick, when there's a real free window before "
                    "your next commitment and an avoided todo that fits, offer once "
                    "to fill the gap with it — the proactive push twin of the "
                    "/todos/now widget."
                ),
                trigger="a free window now + an avoided-but-fitting todo",
                status="active",
            ),
        ]

    def _offer(self, store: MemoryStore, ctx: CoachContext) -> Cue | None:
        """The pure per-tick decision: the gap offer to make now, or ``None``.

        Mirrors ``GET /todos/now`` step for step — waking-hours bound, minutes free
        until the next commitment, suggestible + fitting todos with the
        context-conditioned bias, honest pick — but fires **only** for a genuinely
        *avoided* pick (``pick_now`` reason ``"avoided"``): a proactive push earns a
        higher bar than the on-demand widget, and it's the avoided pile the offer is
        for. Returns ``None`` at every early-out (outside hours, no real gap, nothing
        avoided fits, or the same pitch was already made — the anti-spam marker), so
        the caller can treat "no offer" uniformly.

        Called from both :meth:`before_collect` (to claim the todo) and
        :meth:`evaluate` (to emit the cue); deterministic between the two, exactly as
        ``location_anchor`` recomputes its per-outing decision in each phase.
        """
        window_config = window_config_for(get_settings(), store)
        day_start, day_end = window_config.awake_band()
        within, horizon = work_window_now(
            ctx.now, ctx.timezone,
            cap_minutes=DEFAULT_FIT_CAP_MINUTES, day_start=day_start, day_end=day_end,
        )
        if not within:
            return None

        # Overlap-aware (see ``active_commitments_between``): a still-running
        # multi-day / all-day block that *started* before now reads as busy rather
        # than a free window — the same recipe ``GET /todos/now`` uses. No bespoke
        # lookback to guess how far back an in-progress block might have started.
        commitments = store.active_commitments_between(
            ctx.now.strftime(TS_FMT), horizon.strftime(TS_FMT)
        )
        free = available_now(commitments, ctx.now, horizon)
        gap_min = store.get_float(COACH_GAP_MIN_KEY, DEFAULT_GAP_MIN_MINUTES)
        if free < gap_min:
            return None  # busy now, or the window's too short to start anything

        now_local = local_datetime(ctx.now, ctx.timezone)
        open_todos = filter_suggestible(
            store.open_todos(exclude_delegated=True, with_project_rank=True),
            now_local, window_config,
        )
        fits = fit_todos(
            free, open_todos, bias_fn=task_bias_resolver(store, local_hour=now_local.hour)
        )
        if not fits:
            return None

        avoided = avoided_todos(open_todos, ctx.now)
        picked = pick_now(
            fits, [a["todo"]["id"] for a in avoided], local_hour_of(ctx.now, ctx.timezone)
        )
        # Push only for a genuinely-avoided pick — a "fits" best-guess belongs on the
        # pull widget, not an unprompted interruption.
        if picked is None or picked["reason"] != "avoided":
            return None

        todo = picked["todo"]
        days_open = next((a["days_open"] for a in avoided if a["todo"]["id"] == todo["id"]), 0.0)
        upcoming = store.upcoming_commitments(limit=1, now=ctx.now)
        next_c = upcoming[0] if upcoming else None
        next_id = next_c["id"] if next_c else None

        # Anti-spam: don't re-pitch an identical (todo, next commitment) offer. The
        # marker is written in after_fire only when the cue actually fired, so a held
        # (quiet-hours / debounced) offer still gets its shot once the window opens.
        if store.get_state(_LAST_OFFER_KEY) == _offer_signature(todo["id"], next_id):
            return None

        free_minutes = round(free)
        return Cue(
            module=self.key,
            intervention="gap_offer",
            urgency="nudge",
            text=open_window_message(
                free_minutes=free_minutes,
                todo_title=todo["title"],
                days_open=days_open,
                next_title=next_c["title"] if next_c else None,
                next_clock=_local_clock(next_c["start_at"], ctx.timezone) if next_c else "",
                notes=todo.get("notes"),
                continuity=behavior_nudge_clause(store, todo["id"]),
            ),
            context_key="open_window",
            dedup_key=f"open_window:{todo['id']}",
            ref={
                "todo_id": todo["id"],
                "gap_minutes": free_minutes,
                "next_commitment_id": next_id,
            },
        )

    def before_collect(self, store: MemoryStore, ctx: CoachContext) -> None:
        """Claim the todo this module will offer, so Task Paralysis stands down for it.

        Runs before *any* module's ``evaluate`` (the engine calls every module's
        ``before_collect`` first), so the claim in ``ctx.claimed_todo_ids`` is visible
        to ``task_paralysis`` regardless of collection order — the robust answer to
        "if collection order matters, handle it". Computes the same decision
        :meth:`evaluate` will (deterministic; nothing between the two hooks mutates a
        todo or commitment), and claims only when there's a real offer to make.
        """
        offer = self._offer(store, ctx)
        if offer is not None:
            ctx.claimed_todo_ids.add(offer.ref["todo_id"])

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Emit the single gap offer for right now, or nothing.

        Pure read (per the base contract): the anti-spam marker write is deferred to
        :meth:`after_fire`, which only stamps it once the cue has actually fired.
        """
        offer = self._offer(store, ctx)
        return [offer] if offer is not None else []

    def after_fire(
        self, store: MemoryStore, decisions: list[Decision], ctx: CoachContext
    ) -> None:
        """Stamp the last-offered marker for a gap offer that just fired.

        Held back from :meth:`evaluate` so that stays side-effect-free. Marking on
        *fire* (past suppression) — not on mere production — means a held offer
        re-pitches once the window opens, matching how the engine records fires.
        """
        for d in decisions:
            if d.cue.module != self.key:
                continue
            ref = d.cue.ref or {}
            todo_id = ref.get("todo_id")
            if todo_id is None:
                # This module always sets todo_id, but guard the marker anyway: a
                # missing id would stamp a "None:…" signature that could wedge the
                # anti-spam gate shut and suppress every future offer.
                continue
            next_id = ref.get("next_commitment_id")
            store.set_state(
                _LAST_OFFER_KEY,
                _offer_signature(int(todo_id), None if next_id is None else int(next_id)),
                source="inferred",
            )

    def profile_section(self, store: MemoryStore) -> str | None:
        """Note how many todos are waiting for a free window (light context).

        Coarse on purpose — the precise gap math needs the tick clock the profile
        pass doesn't have. Reports how many todos currently look avoided (the pool
        the offer draws from) so the coaching prose knows there's a backlog to pitch
        into free windows.
        """
        avoided = avoided_todos(store.open_todos(exclude_delegated=True), utcnow())
        if not avoided:
            return None
        return (
            f"- {len(avoided)} avoided todo(s) waiting for a free window; the open-"
            "window nudge offers the worst one when a real gap opens before your "
            "next commitment."
        )


register(OpenWindowModule())
