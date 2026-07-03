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

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from prefrontal.clock import parse_ts as _parse_ts
from prefrontal.integrations import Generator
from prefrontal.integrations.ollama import OllamaError
from prefrontal.llm_json import extract_json_object

if TYPE_CHECKING:
    from prefrontal.memory.store import MemoryStore

#: Estimate used when neither the model nor the heuristics can offer one.
DEFAULT_ESTIMATE_MINUTES = 30.0
MIN_ESTIMATE_MINUTES = 1.0
MAX_ESTIMATE_MINUTES = 480.0

ENERGY_LEVELS = ("low", "medium", "high")

#: Ceiling on the number of distinct categories in use. The set is derived from
#: ``todos.category`` (no registry table); inference reuses existing categories
#: and only coins a new one while under this cap — so the vocabulary stays small
#: enough to be a useful grouping (and legible in the dashboard) rather than
#: sprawling into a near-unique tag per todo.
MAX_CATEGORIES = 20

#: The bucket used when nothing else fits (and the fallback target at the cap).
DEFAULT_CATEGORY = "other"

#: Keyword → category for the offline/heuristic fallback (first match wins). The
#: labels are intentionally few and broad so the heuristic alone stays well under
#: :data:`MAX_CATEGORIES`; the model may still coin finer ones when available.
_CATEGORY_HEURISTICS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("call", "phone", "email", "text", "reply", "respond", "message", "ping"),
     "communication"),
    (("pay", "bill", "invoice", "venmo", "transfer", "bank", "tax", "budget", "refund"),
     "finance"),
    (("doctor", "dentist", "gym", "workout", "medication", "health", "therapy"),
     "health"),
    (("buy", "order", "pick up", "grab", "return", "drop off", "shop", "groceries", "errand"),
     "errands"),
    (("clean", "tidy", "organize", "laundry", "dishes", "repair", "fix", "yard", "home"),
     "home"),
    (("read", "study", "learn", "research", "course"), "learning"),
    (("write", "draft", "report", "design", "code", "review", "deploy", "ticket", "work"),
     "work"),
    (("schedule", "book", "rsvp", "confirm", "sign up", "file", "submit", "renew", "form"),
     "admin"),
)

#: The canonical vocabulary of built-in categories — the single source of truth,
#: derived from the heuristic labels above (insertion-ordered, de-duplicated).
#: :data:`prefrontal.scheduling.DEFAULT_CATEGORY_WINDOWS` keys against this, so a
#: renamed/typo'd category can't silently lose its scheduling window. The model
#: may still coin finer categories at runtime (bounded by :data:`MAX_CATEGORIES`).
KNOWN_CATEGORIES: tuple[str, ...] = tuple(
    dict.fromkeys(label for _keywords, label in _CATEGORY_HEURISTICS)
)

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




@dataclass(frozen=True)
class AugmentedTodo:
    """Resolved todo fields plus where each came from."""

    estimate_minutes: float
    priority: int
    energy: str
    deadline: str | None  # ISO date (YYYY-MM-DD) or None
    category: str  # normalized, capped at MAX_CATEGORIES distinct
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


def normalize_energy(value: Any) -> str | None:
    """Return a valid energy level (one of :data:`ENERGY_LEVELS`) or ``None``.

    The single validation point for the field — mirrors :func:`normalize_category`.
    Lowercases/strips a value and accepts it only if it's a known level; anything
    else (a model or API caller sending an out-of-vocabulary string) returns
    ``None`` so the caller infers a level rather than storing garbage.
    """
    if not isinstance(value, str):
        return None
    norm = value.strip().lower()
    return norm if norm in ENERGY_LEVELS else None


def normalize_category(value: str | None) -> str:
    """Canonicalize a category label: lowercase, single-spaced, length-capped.

    Returns :data:`DEFAULT_CATEGORY` for empty/blank input so a category column
    is never an empty string. Keeps labels short (≤ 30 chars) so the derived set
    stays legible.
    """
    norm = re.sub(r"\s+", " ", (value or "").strip().lower())
    return norm[:30] if norm else DEFAULT_CATEGORY


def heuristic_category(title: str) -> str:
    """Guess a broad category from task keywords, else :data:`DEFAULT_CATEGORY`."""
    t = _norm(title)
    for keywords, category in _CATEGORY_HEURISTICS:
        if _matches(t, keywords):
            return category
    return DEFAULT_CATEGORY


#: Title cues that imply physically *leaving home* — matched whole-word like the
#: other heuristics. Deliberately high-precision (going somewhere), so an online
#: "order printer ink" isn't travel while "pick up printer ink" is. Consumed by
#: :func:`requires_travel` / :mod:`prefrontal.scheduling` to keep travel errands
#: out of late-evening suggestions.
_TRAVEL_KEYWORDS: tuple[str, ...] = (
    "drive", "commute",
    "go to", "get to", "head to", "drop by", "stop by",
    "pick up", "pickup", "drop off", "dropoff", "drop-off",
    "in person", "in-person", "onsite", "on-site",
    "errand", "errands", "store", "mall", "groceries", "grocery", "dmv",
    "post office",
)


def requires_travel(todo: dict[str, Any]) -> bool:
    """Whether a todo likely means leaving home (so it shouldn't be slotted late).

    A title-based heuristic over :data:`_TRAVEL_KEYWORDS` ("drive", "pick up",
    "go to", "groceries", …). Title-only on purpose: an online "order groceries"
    reads the same as a shop run at this altitude, so we err toward the physical-
    presence verbs and let a bare "buy milk" through. Used by
    :func:`prefrontal.scheduling.todo_allowed_at` to stop suggesting travel
    errands into the evening.
    """
    return _matches(_norm(todo.get("title")), _TRAVEL_KEYWORDS)


def at_category_cap(
    candidate: str, existing: list[str], cap: int = MAX_CATEGORIES
) -> bool:
    """Whether adding ``candidate`` as a *new* category would exceed ``cap``.

    ``True`` only when the (normalized) candidate is not already among the
    (normalized) ``existing`` categories *and* the distinct count is already at
    the ceiling. This is the one place the cap rule lives; both the create-time
    remap (:func:`resolve_category`) and the update endpoint's reject decide
    against it, so the two can't drift.
    """
    norm = normalize_category(candidate)
    existing_norm = {normalize_category(c) for c in existing}
    return norm not in existing_norm and len(existing_norm) >= cap


def resolve_category(
    candidate: str, existing: list[str], cap: int = MAX_CATEGORIES
) -> str:
    """Clamp a candidate category to the ``cap`` on the distinct set.

    Under the cap, a novel category is allowed (it grows the set). At the cap
    (see :func:`at_category_cap`), the candidate is remapped to
    :data:`DEFAULT_CATEGORY` if that's in use, else the first (most-common)
    existing category, so we never exceed the ceiling. ``existing`` should be
    ordered most-common-first for the fallback to prefer a well-populated bucket.
    """
    norm = normalize_category(candidate)
    if not at_category_cap(candidate, existing, cap):
        return norm
    existing_norm = [normalize_category(c) for c in existing]
    if DEFAULT_CATEGORY in existing_norm:
        return DEFAULT_CATEGORY
    return existing_norm[0] if existing_norm else norm


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
    "due date), \"category\": \"<short 1-2 word lowercase topic, e.g. finance, "
    "errands, health, work>\"}."
)


def _category_guidance(existing: list[str], cap: int) -> str:
    """Steer the model to reuse the user's categories and respect the cap."""
    if not existing:
        return ""
    listed = ", ".join(existing)
    if len(existing) >= cap:
        return (
            f" For \"category\" you MUST pick the best fit from this existing list "
            f"(the limit of {cap} is reached, do not invent a new one): {listed}."
        )
    return (
        f" For \"category\", strongly prefer reusing one of the user's existing "
        f"categories: {listed}. Only coin a new one if none genuinely fit."
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
    energy = normalize_energy(raw.get("energy"))
    if energy:
        out["energy"] = energy
    deadline = raw.get("deadline")
    if isinstance(deadline, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline.strip()):
        out["deadline"] = deadline.strip()
    category = raw.get("category")
    if isinstance(category, str) and category.strip():
        out["category"] = normalize_category(category)
    return out


def _llm_fields(
    title: str,
    today: date,
    client: Generator,
    *,
    existing_categories: list[str] | None = None,
    category_cap: int = MAX_CATEGORIES,
) -> dict[str, Any]:
    """Ask the model for all fields at once; return the usable subset (or {})."""
    system = _SYSTEM + _category_guidance(existing_categories or [], category_cap)
    try:
        reply = client.generate(
            f"Title: {title}\nToday: {today.isoformat()}", system=system
        )
    except OllamaError:
        return {}
    return _coerce_llm(extract_json_object(reply))


def augment_todo(
    title: str,
    *,
    estimate_minutes: float | None = None,
    priority: int | None = None,
    energy: str | None = None,
    deadline: str | None = None,
    category: str | None = None,
    existing_categories: list[str] | None = None,
    category_cap: int = MAX_CATEGORIES,
    client: Generator | None = None,
    today: date | None = None,
) -> AugmentedTodo:
    """Fill in whichever todo fields weren't supplied.

    For estimate/priority/energy/category: the model's value (one JSON call
    covering all of them) if usable, else a keyword heuristic / default.
    **Deadline is the exception** — the exact heuristic is tried first (its
    weekday math is more reliable than the model's date arithmetic) and the model
    is only a fallback for phrasing the heuristic can't parse. Supplied values are
    kept verbatim and marked ``stated``; ``deadline`` counts as supplied only when
    non-empty.

    Category is always clamped to ``category_cap`` distinct values via
    :func:`_resolve_category`: a novel category is allowed only while under the
    cap, so the derived set (from ``existing_categories``) can't sprawl. A
    supplied category is normalized and still clamped — the ceiling holds even
    for explicit values.

    Args:
        title: The todo text.
        estimate_minutes/priority/energy/deadline/category: User-supplied values,
            or ``None`` to infer.
        existing_categories: The user's current categories, ideally ordered
            most-common-first (drives both the model hint and the cap fallback).
        category_cap: Max distinct categories (defaults to :data:`MAX_CATEGORIES`).
        client: An Ollama-like client; ``None`` skips the model (heuristics only).
        today: Reference date for deadline math (defaults to today).

    Returns:
        An :class:`AugmentedTodo` with resolved fields and a ``sources`` map.
    """
    today = today or date.today()
    existing_categories = existing_categories or []
    sources: dict[str, str] = {}
    # A supplied energy is only trusted if it's a real level; an out-of-vocabulary
    # value is dropped to None here so it's inferred rather than stored verbatim
    # (and so it correctly counts as a field the model should fill).
    energy = normalize_energy(energy) if energy is not None else None
    needs_model = None in (estimate_minutes, priority, energy, category) or not deadline
    llm = (
        _llm_fields(
            title,
            today,
            client,
            existing_categories=existing_categories,
            category_cap=category_cap,
        )
        if (client is not None and needs_model)
        else {}
    )

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
    # Category resolves like the others, then is clamped to the cap regardless of
    # source (a stated value can't smuggle in a 21st category).
    raw_category = resolve("category", category, lambda: heuristic_category(title))
    category_val = resolve_category(raw_category, existing_categories, category_cap)

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

    return AugmentedTodo(est, pri, energy_val, dl, category_val, sources)


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
    client: Generator | None = None,
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
        raw = extract_json_object(reply)
        first = raw.get("first_step")
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


# --- Category rollup (grouping → trends) -------------------------------------
#
# The derived-set view of categories: no registry table, so "the categories in
# use" and their trends are computed from the todos themselves. Feeds the
# dashboard's Categories panel — how many open per topic, the typical length
# (mean estimate) for planning, how often a topic is finished vs dropped, and how
# avoided it looks right now. These are the "common execution length" and
# "avoidance trend" signals that later phases fold into the coaching strategy.


def category_stats(
    todos: list[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    """Per-category rollup over a user's todos (open + closed), busiest first.

    Args:
        todos: All of the user's todos (any status), each a store dict.
        now: Reference time for avoidance scoring.

    Returns:
        One dict per category with ``category``, ``open``/``done``/``dropped``/
        ``total`` counts, ``avg_estimate_minutes`` (mean estimate — the "typical
        length", or ``None`` if none carry one), ``completion_rate`` (done ÷
        closed, or ``None`` if nothing is closed yet), and ``avoidance`` (summed
        :func:`avoidance_score` over the open todos). Sorted by open count, then
        total, descending.
    """
    groups: dict[str, dict[str, Any]] = {}
    for todo in todos:
        cat = normalize_category(todo.get("category"))
        g = groups.setdefault(
            cat,
            {
                "category": cat,
                "open": 0,
                "done": 0,
                "dropped": 0,
                "total": 0,
                "_est_sum": 0.0,
                "_est_n": 0,
                "avoidance": 0.0,
            },
        )
        g["total"] += 1
        status = (todo.get("status") or "open").lower()
        if status == "done":
            g["done"] += 1
        elif status == "dropped":
            g["dropped"] += 1
        else:
            g["open"] += 1
            g["avoidance"] += avoidance_score(todo, now)
        estimate = todo.get("estimate_minutes")
        if isinstance(estimate, (int, float)) and not isinstance(estimate, bool):
            g["_est_sum"] += float(estimate)
            g["_est_n"] += 1

    out: list[dict[str, Any]] = []
    for g in groups.values():
        closed = g["done"] + g["dropped"]
        out.append(
            {
                "category": g["category"],
                "open": g["open"],
                "done": g["done"],
                "dropped": g["dropped"],
                "total": g["total"],
                "avg_estimate_minutes": (
                    round(g["_est_sum"] / g["_est_n"], 1) if g["_est_n"] else None
                ),
                "completion_rate": (
                    round(g["done"] / closed, 2) if closed else None
                ),
                "avoidance": round(g["avoidance"], 1),
            }
        )
    out.sort(key=lambda x: (-x["open"], -x["total"]))
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
