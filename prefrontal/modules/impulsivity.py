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

from prefrontal.integrations import Generator
from prefrontal.integrations.ollama import OllamaError
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register

#: Default reflective pause, in seconds (also the ``pause_seconds`` state default).
DEFAULT_PAUSE_SECONDS = 20.0

#: Valid resolutions of a switch-impulse, in the order the Shortcut menu shows them.
SWITCH_ACTIONS = ("return", "defer", "switch")

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
                name="switch_rate_feedback",
                description="Surface how often switches happened today vs the baseline.",
                trigger="the end-of-day check-in",
                status="active",
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
        if store.get_bool("capture_then_defer", True):
            lines.append(
                "Capture new impulses to the inbox and defer them rather than acting "
                "immediately — the capture itself relieves the urgency."
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
