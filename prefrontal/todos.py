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
from datetime import date, timedelta
from typing import Any, Protocol

from prefrontal.integrations.ollama import OllamaError

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

    For each missing field: the model's value (one JSON call covering all of
    them) if usable, else a keyword heuristic / sane default. Supplied values are
    kept verbatim and marked ``stated``. ``deadline`` is treated as supplied only
    when non-empty; otherwise it's inferred (and may stay ``None``).

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

    if deadline:
        dl, sources["deadline"] = deadline, "stated"
    elif "deadline" in llm:
        dl, sources["deadline"] = llm["deadline"], "llm"
    else:
        dl, sources["deadline"] = heuristic_deadline(title, today), "heuristic"

    return AugmentedTodo(est, pri, energy_val, dl, sources)
