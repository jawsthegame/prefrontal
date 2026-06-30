"""Todo augmentation — enrich a new todo with the fields you didn't supply.

A bare "call the dentist" isn't schedulable: :func:`prefrontal.scheduling.fit_todos`
skips any todo without an ``estimate_minutes``, so an un-estimated todo never gets
surfaced into free time. This module fills the gaps — estimate, priority, energy,
and a deadline parsed from the title — so every todo lands honestly sortable and
schedulable.

It mirrors the outing-window inference (:func:`prefrontal.modules.location_anchor.
infer_time_window`): the local model first (one JSON call), a keyword heuristic
when it's slow/down, then sane defaults. Pure and testable; the Ollama client is
injected, and explicitly-supplied fields are always kept as-is.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from prefrontal.integrations.ollama import OllamaError

if TYPE_CHECKING:
    from prefrontal.memory.store import MemoryStore

#: Estimate used when neither the model nor the heuristics can offer one.
DEFAULT_ESTIMATE_MINUTES = 30.0
MIN_ESTIMATE_MINUTES = 1.0
MAX_ESTIMATE_MINUTES = 480.0

ENERGY_LEVELS = ("low", "medium", "high")

#: Keyword → typical minutes (first match wins; specific before generic).
_ESTIMATE_HEURISTICS: tuple[tuple[tuple[str, ...], float], ...] = (
    (("call", "phone", "text", "email", "reply", "respond", "message", "ping"), 10.0),
    (("pay", "bill", "invoice", "venmo", "transfer"), 10.0),
    (("schedule", "book", "appointment", "rsvp", "confirm", "sign up"), 10.0),
    (("buy", "order", "pick up", "grab", "return", "drop off"), 20.0),
    (("read", "review", "look over", "skim", "check"), 30.0),
    (("clean", "tidy", "organize", "sort", "declutter"), 30.0),
    (("research", "find", "look into", "compare"), 30.0),
    (("write", "draft", "plan", "prepare", "design", "outline", "build", "create"), 45.0),
)

_URGENT = ("urgent", "asap", "immediately", "right away", "critical", "emergency")
_HIGH = ("important", "high priority", "today", "tonight", "by eod", "eod")
_LOW = ("someday", "eventually", "whenever", "no rush", "low priority", "sometime")

_HIGH_ENERGY = (
    "write", "draft", "plan", "prepare", "design", "outline", "build", "create",
    "research", "strategy", "think", "review", "study", "learn", "code",
)
_LOW_ENERGY = (
    "call", "email", "text", "pay", "bill", "schedule", "book", "file", "submit",
    "order", "buy", "pick up", "rsvp", "confirm", "send", "reply",
)

_WEEKDAYS = {
    d: i for i, d in enumerate(
        ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    )
}


class _Generator(Protocol):
    """The slice of :class:`~prefrontal.integrations.ollama.OllamaClient` used here."""

    def generate(self, prompt: str, *, system: str | None = None) -> str: ...


@dataclass(frozen=True)
class AugmentedTodo:
    """Resolved todo fields plus where each came from."""

    estimate_minutes: float
    priority: int
    energy: str
    deadline: str | None  # ISO date (YYYY-MM-DD) or None
    sources: dict[str, str]  # field -> "stated" | "llm" | "heuristic"


def _norm(title: str | None) -> str:
    return re.sub(r"\s+", " ", (title or "").lower()).strip()


def _matches(text: str, keywords: tuple[str, ...]) -> bool:
    """Whole-word/phrase membership (avoids 'call' matching 'recall')."""
    return any(re.search(rf"\b{re.escape(k)}\b", text) for k in keywords)


def heuristic_estimate(title: str) -> float:
    """Guess minutes from task keywords, falling back to the default."""
    t = _norm(title)
    for keywords, minutes in _ESTIMATE_HEURISTICS:
        if _matches(t, keywords):
            return minutes
    return DEFAULT_ESTIMATE_MINUTES


def heuristic_priority(title: str) -> int:
    """0 low · 1 normal · 2 high · 3 urgent, from wording cues."""
    t = _norm(title)
    if _matches(t, _URGENT):
        return 3
    if _matches(t, _HIGH):
        return 2
    if _matches(t, _LOW):
        return 0
    return 1


def heuristic_energy(title: str) -> str:
    """Mental effort: deep-work verbs → high, admin verbs → low, else medium."""
    t = _norm(title)
    if _matches(t, _HIGH_ENERGY):
        return "high"
    if _matches(t, _LOW_ENERGY):
        return "low"
    return "medium"


def heuristic_deadline(title: str, today: date) -> str | None:
    """Parse a relative deadline ('tomorrow', 'by Friday') → ISO date, or None."""
    t = _norm(title)
    if "tomorrow" in t:
        return (today + timedelta(days=1)).isoformat()
    if "tonight" in t or "today" in t or "eod" in t:
        return today.isoformat()
    if "next week" in t:
        return (today + timedelta(days=7)).isoformat()
    match = re.search(
        r"\b(?:by|on|before|due)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        t,
    ) or re.search(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t
    )
    if match:
        delta = (_WEEKDAYS[match.group(1)] - today.weekday()) % 7 or 7
        return (today + timedelta(days=delta)).isoformat()
    return None


_SYSTEM = (
    "You enrich a personal to-do. Given its title and today's date, reply with "
    "ONLY a JSON object, no prose: {\"estimate_minutes\": <integer 1-480, the "
    "realistic minutes to do it>, \"priority\": <0 low, 1 normal, 2 high, 3 "
    "urgent>, \"energy\": \"low\"|\"medium\"|\"high\" (mental effort), "
    "\"deadline\": \"YYYY-MM-DD\" or null (only if the title clearly implies a "
    "due date)}."
)


def _coerce_llm(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only well-typed, in-range fields from a model's JSON reply."""
    out: dict[str, Any] = {}
    est = raw.get("estimate_minutes")
    if isinstance(est, (int, float)) and MIN_ESTIMATE_MINUTES <= est <= MAX_ESTIMATE_MINUTES:
        out["estimate_minutes"] = float(est)
    pri = raw.get("priority")
    if isinstance(pri, int) and not isinstance(pri, bool) and 0 <= pri <= 3:
        out["priority"] = pri
    energy = raw.get("energy")
    if isinstance(energy, str) and energy.lower() in ENERGY_LEVELS:
        out["energy"] = energy.lower()
    deadline = raw.get("deadline")
    if isinstance(deadline, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline.strip()):
        out["deadline"] = deadline.strip()
    return out


def _llm_fields(title: str, today: date, client: _Generator) -> dict[str, Any]:
    """Ask the model for all fields at once; return the usable subset (or {})."""
    try:
        reply = client.generate(
            f"Title: {title}\nToday: {today.isoformat()}", system=_SYSTEM
        )
    except OllamaError:
        return {}
    match = re.search(r"\{.*\}", reply or "", re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group())
    except (ValueError, TypeError):
        return {}
    return _coerce_llm(parsed) if isinstance(parsed, dict) else {}


def augment_todo(
    title: str,
    *,
    estimate_minutes: float | None = None,
    priority: int | None = None,
    energy: str | None = None,
    deadline: str | None = None,
    client: _Generator | None = None,
    today: date | None = None,
) -> AugmentedTodo:
    """Fill in whichever todo fields weren't supplied.

    For estimate/priority/energy: the model's value (one JSON call covering all
    of them) if usable, else a keyword heuristic / default. **Deadline is the
    exception** — the exact heuristic is tried first (its weekday math is more
    reliable than the model's date arithmetic) and the model is only a fallback
    for phrasing the heuristic can't parse. Supplied values are kept verbatim and
    marked ``stated``; ``deadline`` counts as supplied only when non-empty.

    Args:
        title: The todo text.
        estimate_minutes/priority/energy/deadline: User-supplied values, or
            ``None`` to infer.
        client: An Ollama-like client; ``None`` skips the model (heuristics only).
        today: Reference date for deadline math (defaults to today).

    Returns:
        An :class:`AugmentedTodo` with resolved fields and a ``sources`` map.
    """
    today = today or date.today()
    sources: dict[str, str] = {}
    needs_model = None in (estimate_minutes, priority, energy) or not deadline
    llm = _llm_fields(title, today, client) if (client is not None and needs_model) else {}

    def resolve(field: str, supplied: Any, heuristic: Any) -> Any:
        if supplied is not None:
            sources[field] = "stated"
            return supplied
        if field in llm:
            sources[field] = "llm"
            return llm[field]
        sources[field] = "heuristic"
        return heuristic()

    est = resolve("estimate_minutes", estimate_minutes, lambda: heuristic_estimate(title))
    pri = resolve("priority", priority, lambda: heuristic_priority(title))
    energy_val = resolve("energy", energy, lambda: heuristic_energy(title))

    # Deadline ordering differs from the other fields: the heuristic's weekday
    # math is exact ("by Friday" → the right date), whereas the model sometimes
    # miscomputes dates — so prefer the heuristic when it matches a relative term,
    # and only fall back to the model for fuzzier phrasing it can't parse.
    if deadline:
        dl, sources["deadline"] = deadline, "stated"
    else:
        guess = heuristic_deadline(title, today)
        if guess is not None:
            dl, sources["deadline"] = guess, "heuristic"
        elif "deadline" in llm:
            dl, sources["deadline"] = llm["deadline"], "llm"
        else:
            dl, sources["deadline"] = None, "heuristic"

    return AugmentedTodo(est, pri, energy_val, dl, sources)


# --- Decomposition (the initiation lever) ------------------------------------
#
# Big tasks stall on *starting*. Decomposition turns "write the report" into one
# tiny, obvious first action (≤ max_first_step_minutes) that breaks inertia, plus
# the remaining steps kept collapsed so the list itself doesn't re-trigger
# paralysis. Same LLM-with-heuristic-fallback shape as the rest of this module.

DEFAULT_MAX_FIRST_STEP_MINUTES = 5.0
_MAX_STEPS = 5

#: Verb → a concrete tiny first action, for the offline/heuristic fallback.
_FIRST_STEP_HEURISTICS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("write", "draft", "outline", "blog", "post", "essay", "report", "doc"),
     "Open a blank doc and write one rough heading or ugly sentence — that's it."),
    (("plan", "design", "strategy", "brainstorm"),
     "Open a note and jot three bullet points, however messy."),
    (("call", "phone", "ring"),
     "Find the number, put it on screen, and dial — don't rehearse."),
    (("email", "reply", "respond", "message", "write back"),
     "Open a reply and type just the first line."),
    (("research", "find", "look into", "compare", "read"),
     "Open one tab and search the very first question."),
    (("clean", "tidy", "organize", "sort", "declutter"),
     "Set a 5-minute timer and clear one small surface."),
    (("buy", "order", "pick up", "shop"),
     "Open the site/app and put one item in the cart."),
    (("schedule", "book", "rsvp", "confirm"),
     "Open the calendar and pick one candidate time."),
)


@dataclass(frozen=True)
class Decomposition:
    """A todo broken into a tiny first step plus the remaining ordered steps."""

    first_step: str
    first_step_minutes: float
    steps: list[str]  # remaining steps after the first (may be empty)
    source: str       # "llm" | "heuristic"


_DECOMP_SYSTEM = (
    "You help someone with ADHD *start* a task — initiation is the hard part. "
    "Given a task and a max number of minutes for the first step, reply with "
    "ONLY JSON, no prose: {\"first_step\": \"<one concrete, physical, obvious "
    "action doable in <=N minutes that breaks inertia>\", \"first_step_minutes\": "
    "<integer <=N>, \"steps\": [\"<next step>\", ...]}. The first step must be "
    "tiny and unintimidating (open the file, find the number, write one line). "
    "steps = the remaining 2-5 short steps to finish."
)


def _heuristic_decomposition(title: str, max_first_minutes: float) -> Decomposition:
    """A generic-but-actionable first step when the model is unavailable."""
    t = _norm(title)
    step = (
        f"Set a {int(max_first_minutes)}-minute timer and do the smallest visible "
        "piece — just begin."
    )
    for keywords, action in _FIRST_STEP_HEURISTICS:
        if _matches(t, keywords):
            step = action
            break
    return Decomposition(step, min(max_first_minutes, 5.0), [], "heuristic")


def decompose_task(
    title: str,
    *,
    max_first_minutes: float = DEFAULT_MAX_FIRST_STEP_MINUTES,
    client: _Generator | None = None,
) -> Decomposition:
    """Break a task into a tiny first step (+ remaining steps).

    Tries the model first (one JSON call); falls back to a verb-keyed heuristic
    first step (with no further steps) when the model is unavailable or unusable.

    Args:
        title: The task text.
        max_first_minutes: Ceiling for the first step's length.
        client: An Ollama-like client; ``None`` uses the heuristic.

    Returns:
        A :class:`Decomposition`.
    """
    if client is not None:
        try:
            reply = client.generate(
                f"Task: {title}\nFirst step max minutes: {int(max_first_minutes)}",
                system=_DECOMP_SYSTEM,
            )
        except OllamaError:
            reply = ""
        match = re.search(r"\{.*\}", reply or "", re.DOTALL)
        if match:
            try:
                raw = json.loads(match.group())
            except (ValueError, TypeError):
                raw = {}
            first = raw.get("first_step") if isinstance(raw, dict) else None
            if isinstance(first, str) and first.strip():
                mins = raw.get("first_step_minutes")
                mins = (
                    float(mins)
                    if isinstance(mins, (int, float)) and not isinstance(mins, bool)
                    else max_first_minutes
                )
                mins = max(MIN_ESTIMATE_MINUTES, min(mins, max_first_minutes))
                steps = raw.get("steps")
                steps = (
                    [str(s).strip() for s in steps if str(s).strip()][:_MAX_STEPS]
                    if isinstance(steps, list)
                    else []
                )
                return Decomposition(first.strip(), mins, steps, "llm")
    return _heuristic_decomposition(title, max_first_minutes)


# --- Avoidance detection (honest prioritization) -----------------------------
#
# The anti-shiny mechanism: surface the important thing you keep *not* doing,
# rather than trusting self-assigned priority (which is gameable). A pure
# heuristic over data we already have — no new event tracking. An open todo
# looks avoided when it has sat a while AND isn't a "no time" excuse: older +
# higher-priority + quicker + nearer-deadline scores higher. Low-priority
# ("someday") items are exempt so this never nags about genuine maybes.

DEFAULT_AVOIDANCE_MIN_DAYS = 3.0


def _parse_ts(ts: Any) -> datetime | None:
    """Parse a stored ``YYYY-MM-DD HH:MM:SS`` UTC timestamp, or ``None``."""
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _days_open(todo: dict[str, Any], now: datetime) -> float | None:
    created = _parse_ts(todo.get("created_at"))
    if created is None:
        return None
    return max(0.0, (now - created).total_seconds() / 86400.0)


def avoidance_score(todo: dict[str, Any], now: datetime) -> float:
    """How strongly an open todo looks avoided (0.0 = not at all).

    Combines days open, priority, smallness (a quick task you keep skipping has
    no "no time" excuse), and deadline pressure. Low-priority items score 0.
    """
    if (todo.get("status") or "open") != "open":
        return 0.0
    priority = todo.get("priority")
    priority = 1 if priority is None else int(priority)
    if priority < 1:  # low / "someday" — not avoidance
        return 0.0
    days = _days_open(todo, now)
    if days is None:
        return 0.0
    score = days * (1 + priority)  # older and higher-priority ⇒ more avoided
    estimate = todo.get("estimate_minutes")
    if estimate is not None and estimate <= 30:
        score *= 1.5  # quick task left undone is a stronger avoidance signal
    deadline = _parse_ts(todo.get("deadline"))
    if deadline is not None:
        days_to = (deadline - now).total_seconds() / 86400.0
        if days_to < 0:
            score *= 3.0  # overdue
        elif days_to <= 2:
            score *= 2.0  # imminent
    return round(score, 1)


def avoided_todos(
    todos: list[dict[str, Any]],
    now: datetime,
    *,
    min_days: float = DEFAULT_AVOIDANCE_MIN_DAYS,
) -> list[dict[str, Any]]:
    """Open todos that look avoided, worst first.

    A todo qualifies when it's been open at least ``min_days`` and isn't
    low-priority. Returns ``{todo, days_open, score}`` dicts sorted by score.
    """
    out: list[dict[str, Any]] = []
    for todo in todos:
        if (todo.get("status") or "open") != "open":
            continue
        days = _days_open(todo, now)
        score = avoidance_score(todo, now)
        if days is None or days < min_days or score <= 0:
            continue
        out.append({"todo": todo, "days_open": round(days, 1), "score": score})
    out.sort(key=lambda x: -x["score"])
    return out


# --- Outcome capture (feed the learning loop) --------------------------------
#
# Closing a todo is a real behavioral outcome, but until now it was thrown away:
# the learning pass only saw outings, focus sessions, and mail. A finished todo
# is a task ``success``; a dropped one is a ``miss`` — exactly the ``drift``
# signal for the ``task`` type, and the moment an avoided todo finally resolves.
# This mirrors ``record_outing_return`` / ``record_outing_abandoned`` so todo
# closes flow into the same episode history every other touchpoint already does.


def todo_episode_fields(
    todo: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Derive :meth:`MemoryStore.log_episode` kwargs from a closed todo (pure).

    A ``done`` todo is a task ``success``; anything else (``dropped``) is a
    ``miss`` — so it folds into the ``drift`` score for ``task``. The estimate is
    recorded as ``predicted_value``, but ``actual_value`` is deliberately
    ``None``: a todo's created→closed span is wall-clock, not time spent on task,
    so treating it as the actual duration would pollute ``time_estimation`` (the
    same reasoning as ``record_outing_abandoned``). The age is kept in ``notes``
    for future analysis instead.

    Args:
        todo: A todo dict (as returned by the store), ideally post-close so its
            ``status`` and ``completed_at`` are current.
        now: Reference time (naive UTC) for the age note when ``completed_at`` is
            absent (e.g. a dropped todo). ``None`` skips the age note.

    Returns:
        A kwargs dict for :meth:`MemoryStore.log_episode`.
    """
    done = (todo.get("status") or "").lower() == "done"
    end = _parse_ts(todo.get("completed_at")) or now
    start = _parse_ts(todo.get("created_at"))
    notes = None
    if start is not None and end is not None:
        days = max(0.0, (end - start).total_seconds() / 86400.0)
        notes = f"{'completed' if done else 'dropped'} after {days:.1f}d open"
    return {
        "episode_type": "task",
        "predicted_value": todo.get("estimate_minutes"),
        "actual_value": None,
        "acknowledged": None,
        "context": f"todo {'done' if done else 'dropped'}: {todo.get('title')}",
        "outcome": "success" if done else "miss",
        "notes": notes,
    }


def record_todo_closed(
    store: MemoryStore, todo: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Log a closed todo as a ``task`` episode for pattern tracking.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        todo: The closed todo dict (post-close, so ``status`` reflects the
            outcome).
        now: Reference time forwarded to :func:`todo_episode_fields`.

    Returns:
        ``{"episode_id": int, "outcome": str}``.
    """
    fields = todo_episode_fields(todo, now=now)
    episode_id = store.log_episode(**fields)
    return {"episode_id": episode_id, "outcome": fields["outcome"]}
