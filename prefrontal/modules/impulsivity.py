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

from prefrontal.coaching import CoachContext, Cue, last_fired
from prefrontal.integrations import Generator
from prefrontal.integrations.ollama import OllamaError
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register

#: Default reflective pause, in seconds (also the ``pause_seconds`` state default).
DEFAULT_PAUSE_SECONDS = 20.0

#: Valid resolutions of a switch-impulse, in the order the Shortcut menu shows them.
SWITCH_ACTIONS = ("return", "defer", "switch")

#: coaching_state key holding how many *un-parked* switch-impulses in one focus
#: block trip the proactive drift nudge (see :func:`switch_drift`).
SWITCH_DRIFT_THRESHOLD_KEY = "switch_drift_threshold"
#: Default for :data:`SWITCH_DRIFT_THRESHOLD_KEY`.
DEFAULT_DRIFT_THRESHOLD = 3

#: Fewest still-open parked impulses before a weekly retro is worth surfacing —
#: below this the inbox isn't cluttered enough to be worth a review.
CAPTURE_RETRO_MIN = 3
#: A parked impulse must have sat at least this many days before the retro counts
#: it, so a fresh capture isn't immediately dragged into a "was this noise?" ask.
CAPTURE_RETRO_MIN_AGE_DAYS = 2.0
#: How many parked impulses the retro read considers at once.
CAPTURE_RETRO_LIMIT = 30

#: Words past this many in the raw impulse get dropped from the heuristic title.
_CAPTURE_TITLE_MAX_WORDS = 8

#: Don't surface a learned switch tendency in the profile below this confidence.
_SWITCH_CONFIDENCE_FLOOR = 0.3


def pause_seconds(
    elapsed_minutes: float,
    planned_minutes: float | None,
    base_seconds: float = DEFAULT_PAUSE_SECONDS,
) -> float:
    """Reflective-pause length (seconds) for a switch-impulse.

    A switch *early* in a block is the most disruptive — there's the most to
    lose by abandoning it — so the pause is longest then and shrinks toward
    ``base_seconds`` as the block nears its planned end. With no planned
    duration there's no progress to scale against, so it returns ``base_seconds``
    flat. Bounded to ``[base_seconds, 3 * base_seconds]``.

    Args:
        elapsed_minutes: Minutes since the focus block started.
        planned_minutes: The stated intended duration, or ``None``.
        base_seconds: The configured baseline pause.

    Returns:
        The pause length in seconds, rounded to one decimal.
    """
    if not planned_minutes or planned_minutes <= 0:
        return base_seconds
    frac = min(max(elapsed_minutes / planned_minutes, 0.0), 1.0)  # 0 early → 1 at plan end
    factor = 3.0 - 2.0 * frac  # 3x at the start, 1x once the plan is spent
    return round(base_seconds * min(max(factor, 1.0), 3.0), 1)


def switch_response(action: str) -> str:
    """Validate and normalize a chosen switch resolution.

    Args:
        action: One of ``return`` / ``defer`` / ``switch`` (any case).

    Returns:
        The lower-cased action.

    Raises:
        ValueError: If ``action`` is not a known resolution.
    """
    normalized = (action or "").strip().lower()
    if normalized not in SWITCH_ACTIONS:
        raise ValueError(f"action must be one of {SWITCH_ACTIONS}, got {action!r}")
    return normalized


def build_pause_message(intended_task: str, elapsed_minutes: float, *, name: str = "") -> str:
    """The reminder shown during the reflective pause — re-surfaces the current task.

    Args:
        intended_task: What the user declared they're working on, quoted back.
        elapsed_minutes: Minutes into the block (rendered whole).
        name: Optional first name; included when set.

    Returns:
        A short, concrete message naming the task and the time already invested.
    """
    elapsed = round(elapsed_minutes)
    who = f" {name}" if name else ""
    return (
        f"Hold on{who} — you're {elapsed} min into “{intended_task}.” "
        "Does the new thing actually need you now, or can it wait?"
    )

def switch_rate_feedback(
    sessions: list[dict], *, baseline: float | None = None
) -> str | None:
    """A one-line end-of-day reflection on switch-impulses across focus sessions.

    Sums the switch-impulses and deferrals over ``sessions`` (closed focus-session
    dicts carrying ``switch_impulses`` / ``switches_deferred``) and frames the
    deferrals as the win. Returns ``None`` when there's nothing to say — no
    sessions, or no impulses were signalled — so the caller simply omits the line
    (matching the briefing's "quiet when there's no signal" habit).

    Args:
        sessions: Closed focus-session dicts to summarize (e.g. the last day's).
        baseline: Optional learned mean *honored* impulses per session (the
            ``context_switch`` pattern's ``variance``); when given, a gentle
            comparison is appended so today reads against the norm.

    Returns:
        A short phrase like ``"6 switch-impulses, 4 deferred"`` (optionally with a
        baseline clause), or ``None``.
    """
    if not sessions:
        return None
    impulses = sum(int(s.get("switch_impulses") or 0) for s in sessions)
    if impulses <= 0:
        return None
    deferred = sum(int(s.get("switches_deferred") or 0) for s in sessions)
    unit = "impulse" if impulses == 1 else "impulses"
    line = f"{impulses} switch-{unit}, {deferred} deferred"
    if baseline is not None and baseline >= 0:
        line += f" (you usually honor ~{baseline:g}/session)"
    return line


def _drift_threshold(store: MemoryStore) -> int:
    """The configured un-parked-pull cutoff (default, and on a bad value too)."""
    try:
        return int(store.get_state(SWITCH_DRIFT_THRESHOLD_KEY) or DEFAULT_DRIFT_THRESHOLD)
    except (TypeError, ValueError):
        return DEFAULT_DRIFT_THRESHOLD


def switch_drift(session: dict, threshold: int = DEFAULT_DRIFT_THRESHOLD) -> bool:
    """Whether an active focus block shows a proactive-nudge-worthy drift streak.

    The signal is *un-parked* pulls: ``switch_impulses - switches_deferred``. A
    deferral means the idea was captured (the healthy move the module wants), so
    parking impulses never grows this count — only repeatedly feeling the pull
    *without* capturing does. Once that reaches ``threshold``, the block has drifted
    enough to be worth one gentle "park it instead" nudge.

    Args:
        session: An active focus-session dict (carrying ``switch_impulses`` /
            ``switches_deferred``).
        threshold: Un-parked pulls that trip the nudge (>= 1; treated as 1 below).

    Returns:
        ``True`` when the block has drifted past the threshold.
    """
    acted = int(session.get("switch_impulses") or 0) - int(
        session.get("switches_deferred") or 0
    )
    return acted >= max(threshold, 1)


def build_drift_message(intended_task: str, acted: int, *, name: str = "") -> str:
    """The proactive drift nudge — names the pattern and points at capture-and-defer.

    Args:
        intended_task: What the block is for, quoted back.
        acted: Un-parked switch-impulses so far in the block (rendered whole).
        name: Optional first name; included when set.

    Returns:
        A short, gentle one-liner that reframes repeated resisting as capturing.
    """
    who = f" {name}" if name else ""
    times = f"{acted}×" if acted != 1 else "once"
    return (
        f"Hey{who} — you've felt the pull away from “{intended_task}” {times} this "
        "block. The ideas are safe if you jot them down; try parking the next one "
        "instead of chasing or white-knuckling it."
    )


def captured_impulse_retro_text(impulses: list[dict], *, name: str = "") -> str | None:
    """The weekly captured-impulse retro message, or ``None`` when there's too little.

    Closes the loop ``capture_and_defer`` opens ("the ideas are safe if you jot
    them down"): a handful of still-open parked impulses, surfaced as a batch to
    triage — keep the ones that matter, drop the noise — which both empties the
    inbox and *proves the capture worked*, so parking stays trustworthy enough to
    keep choosing over chasing. Returns ``None`` below :data:`CAPTURE_RETRO_MIN`
    so a near-empty inbox is left alone.

    Args:
        impulses: Open ``source='impulse'`` todos (as from ``parked_impulses``).
        name: Optional first name for a warmer lead.

    Returns:
        The ambient retro text, or ``None``.
    """
    if len(impulses) < CAPTURE_RETRO_MIN:
        return None
    who = f"{name}, a" if name else "A"
    sample = ", ".join(f"“{t.get('title') or 'a note'}”" for t in impulses[:3])
    more = f" (+{len(impulses) - 3} more)" if len(impulses) > 3 else ""
    return (
        f"{who} quick capture retro: you've parked {len(impulses)} impulses that are "
        f"still open — {sample}{more}. Keep the ones that matter, drop the noise. "
        "The capture did its job; clearing them keeps the inbox trustworthy."
    )


#: System prompt for LLM-titling a captured impulse. Kept tight so a local model
#: returns a clean label, never a paragraph; the heuristic covers it if it doesn't.
CAPTURE_TITLE_SYSTEM_PROMPT = (
    "Rewrite the user's raw, half-formed note as a short todo title — an "
    "imperative phrase of at most 8 words, no quotes, no trailing punctuation. "
    "Reply with the title only."
)




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
    client: Generator | None = None,
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
        SWITCH_DRIFT_THRESHOLD_KEY: str(DEFAULT_DRIFT_THRESHOLD),
    }

    def interventions(self) -> list[Intervention]:
        """Declare friction/capture interventions."""
        return [
            Intervention(
                name="reflective_pause",
                description="Insert a short, dismissible pause before an impulsive switch.",
                trigger="a context switch away from an active intended task",
                status="active",
            ),
            Intervention(
                name="capture_and_defer",
                description="Log the new impulse to the inbox so it's safe to return to later.",
                trigger="a captured idea/task during a focus block",
                status="active",
            ),
            Intervention(
                name="drift_check",
                description=(
                    "Proactively notice a run of un-parked switch-impulses in a block "
                    "and suggest capturing rather than chasing — fires once per block."
                ),
                trigger="un-parked switch-impulses in an active block pass the threshold",
                status="active",
            ),
            Intervention(
                name="switch_rate_feedback",
                description="Surface how often switches happened today vs the baseline.",
                trigger="the end-of-day check-in",
                status="active",
            ),
            Intervention(
                name="captured_impulse_retro",
                description=(
                    "A weekly ambient review of still-open parked impulses — keep the "
                    "real ones, drop the noise — closing the capture-and-defer loop so "
                    "parking stays trustworthy."
                ),
                trigger="enough captured impulses have sat open for a few days",
                status="active",
            ),
        ]

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Proactively catch a drifting focus block — the ``drift_check`` cue.

        The reflective-pause loop is reactive (it waits for the user to tap
        "switch"); this closes the loop the module's docstring names — a *rising*
        switch rate — by watching the active block itself. When un-parked
        switch-impulses cross the threshold (:func:`switch_drift`), emit one gentle
        ``nudge`` pointing at capture-and-defer.

        Pure read, per the base contract. Fired once per block: it self-gates on the
        engine's ``coach_fired:`` stamp (like ``delegation_checkin`` / ``projects``)
        so a churny block gets a single heads-up, not one every tick. Quiet hours and
        protected-hyperfocus holding are applied downstream by the engine — an
        aligned, shielded block is deep work, not drift, so it's held there.
        """
        # This nudge reinforces capture-and-defer; honor that opt-out. The retro
        # below shares the switch — both are capture-and-defer follow-throughs.
        if not store.get_bool("capture_then_defer", True):
            return []

        cues: list[Cue] = []
        cues.extend(self._drift_cues(store, ctx))
        cues.extend(self._capture_retro_cues(store, ctx))
        return cues

    def _drift_cues(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """The reactive drift nudge for a churny active block (fires once per block)."""
        session = store.most_recent_active_focus_session()
        if not session:
            return []
        threshold = _drift_threshold(store)
        if not switch_drift(session, threshold):
            return []
        dedup_key = f"switch_drift:{session['id']}"
        if last_fired(store, dedup_key) is not None:
            return []  # already nudged about this block
        impulses = int(session.get("switch_impulses") or 0)
        deferred = int(session.get("switches_deferred") or 0)
        text = build_drift_message(
            session.get("intended_task") or "this", impulses - deferred, name=ctx.display_name
        )
        return [
            Cue(
                module=self.key,
                intervention="drift_check",
                urgency="nudge",
                text=text,
                context_key="focus",
                dedup_key=dedup_key,
                ref={
                    "session_id": session["id"],
                    "switch_impulses": impulses,
                    "switches_deferred": deferred,
                },
            )
        ]

    def _capture_retro_cues(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """At most one weekly, ambient captured-impulse retro.

        Inert unless enough parked impulses have sat open for a few days
        (:func:`captured_impulse_retro_text`). Ambient (rides the digest, never
        interrupts — a triage of finished-with captures is never urgent) and deduped
        to the ISO week so it can't fire more than once a week however often the tick
        runs.
        """
        impulses = store.parked_impulses(
            min_age_days=CAPTURE_RETRO_MIN_AGE_DAYS, limit=CAPTURE_RETRO_LIMIT
        )
        text = captured_impulse_retro_text(impulses, name=ctx.display_name)
        if text is None:
            return []
        iso = ctx.now.isocalendar()
        return [
            Cue(
                module=self.key,
                intervention="captured_impulse_retro",
                urgency="ambient",
                text=text,
                context_key="capture_retro",
                dedup_key=f"capture_retro:{iso[0]}-W{iso[1]:02d}",
                ref={"parked": len(impulses), "todo_ids": [t["id"] for t in impulses[:10]]},
            )
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
        if store.get_bool("capture_then_defer", True):
            lines.append(
                "Capture new impulses to the inbox and defer them rather than acting "
                "immediately — the capture itself relieves the urgency."
            )
            parked = store.parked_impulses(limit=CAPTURE_RETRO_LIMIT)
            if len(parked) >= CAPTURE_RETRO_MIN:
                lines.append(
                    f"{len(parked)} parked impulse(s) still open — worth a quick retro "
                    "to keep the real ones and drop the noise."
                )
        for p in store.get_patterns("context_switch"):
            observed = p.get("observed_value")
            conf = p.get("confidence") or 0.0
            # Don't assert a low-confidence tendency (the repo's learning habit).
            if observed is None or conf < _SWITCH_CONFIDENCE_FLOOR:
                continue
            deferred = p.get("predicted_value")
            defer_clause = ""
            if deferred is not None and observed > 0:
                defer_clause = f"; you defer ~{deferred / observed:.0%}"
            lines.append(
                f"~{observed:g} switch-impulses per focus block{defer_clause} "
                f"(confidence {conf:.0%})."
            )
        return "\n".join(f"- {line}" for line in lines) if lines else None


register(ImpulsivityModule())
