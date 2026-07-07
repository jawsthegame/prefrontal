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

#: Appended when the caller lets the model *decline* (avoided-task sweep). The
#: model, not a fixed rule, judges whether a task is even worth breaking down.
_DECOMP_DECLINE_INSTRUCTION = (
    " If the task is already a single, simple, obvious action that can't be "
    "usefully broken down (nothing would be gained by splitting it), reply with "
    'exactly {"decompose": false} and nothing else — never invent busywork steps.'
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
    guidance: str | None = None,
    allow_decline: bool = False,
) -> Decomposition | None:
    """Break a task into a tiny first step (+ remaining steps).

    Tries the model first (one JSON call); falls back to a verb-keyed heuristic
    first step (with no further steps) when the model is unavailable or unusable.

    Args:
        title: The task text.
        max_first_minutes: Ceiling for the first step's length.
        client: An Ollama-like client; ``None`` uses the heuristic.
        guidance: Optional learned addendum appended to the system prompt — the
            negative examples from breakdowns the user dismissed as unhelpful (see
            :func:`learned_decomposition_guidance`). Ignored on the heuristic path.
        allow_decline: When ``True``, let the *model* judge whether the task is
            even worth breaking down; if it declines, return ``None`` instead of a
            breakdown. Used by the avoided-task sweep. A missing/unusable model
            can't judge, so it still falls back to a heuristic first step (an
            avoided task benefits from one). ``False`` (default) always returns a
            :class:`Decomposition` — for the on-demand / first-step callers.

    Returns:
        A :class:`Decomposition`, or ``None`` only when ``allow_decline`` and the
        model actively judged the task not worth decomposing.
    """
    if client is not None:
        system = _DECOMP_SYSTEM + (guidance or "")
        if allow_decline:
            system += _DECOMP_DECLINE_INSTRUCTION
        try:
            reply = client.generate(
                f"Task: {title}\nFirst step max minutes: {int(max_first_minutes)}",
                system=system,
            )
        except OllamaError:
            reply = ""
        raw = extract_json_object(reply)
        # The model's explicit "not worth breaking down" verdict (only honored
        # when the caller opted in). Checked before first_step so a contradictory
        # reply still respects the decline.
        if allow_decline and raw.get("decompose") is False:
            return None
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


# --- Learning from dismissed breakdowns --------------------------------------
#
# When the user dismisses a breakdown (dashboard "not needed" / "didn't help"),
# it's captured in `decomposition_feedback` (see the todos repo). Two readers fold
# that history back in — the mirror of how dropped triage todos evolve the triage
# prompt (prefrontal/mail/feedback.py):
#   - not_useful → negative examples appended to the decomposer prompt.
#   - not_needed → once repeated, suppress the auto-decompose on new todos.

#: How many "not needed" dismissals before auto-decompose is switched off. Small,
#: since a few explicit "I didn't need this" is a clear signal; an operator can
#: override via the `decomposition_suppress_threshold` state key (0 = never suppress).
DEFAULT_DECOMP_SUPPRESS_THRESHOLD = 3

#: Cap on negative examples injected into the decomposer prompt — bounded so the
#: addendum stays small and can't drown the base instructions.
_DECOMP_GUIDANCE_LIMIT = 6


def learned_decomposition_guidance(
    store: MemoryStore, *, limit: int = _DECOMP_GUIDANCE_LIMIT
) -> str:
    """Prompt addendum from breakdowns the user dismissed as unhelpful.

    Folds recent ``not_useful`` dismissals into the decomposer's system prompt as
    negative few-shot examples, so the model steers away from first steps the user
    already rejected. Empty string when there's nothing to learn from (the base
    prompt is used unchanged).
    """
    rows = store.decomposition_feedback_list(reason="not_useful", limit=limit)
    lines = [
        f'- "{(r.get("title") or "a task").strip()}" → avoid a first step like: {fs}'
        for r in rows
        if (fs := (r.get("first_step") or "").strip())
    ]
    if not lines:
        return ""
    return (
        "\n\nThe user found these past breakdowns unhelpful. Do NOT produce first "
        "steps in this vein — make the first step smaller and more concrete, and "
        "the remaining steps genuinely useful:\n" + "\n".join(lines)
    )


def auto_decompose_suppressed(store: MemoryStore, *, threshold: int | None = None) -> bool:
    """Whether repeated "not needed" dismissals mean we should skip auto-decompose.

    Learning *when not to* break down: once the user has dismissed
    ``threshold`` breakdowns as unnecessary, stop auto-decomposing new todos. The
    on-demand "Break it down" button is unaffected — that's an explicit request.
    ``threshold`` defaults to the ``decomposition_suppress_threshold`` state key
    (or :data:`DEFAULT_DECOMP_SUPPRESS_THRESHOLD`); ``0`` disables suppression.
    """
    if threshold is None:
        threshold = int(
            store.get_float(
                "decomposition_suppress_threshold", DEFAULT_DECOMP_SUPPRESS_THRESHOLD
            )
        )
    if threshold <= 0:
        return False
    return store.decomposition_dismissed_count(reason="not_needed") >= threshold


def sweep_avoided_decompositions(
    store: MemoryStore,
    client: Generator | None,
    *,
    now: datetime,
    max_attempts: int = 2,
) -> int:
    """Break down the tasks the user is actually *avoiding* — not fresh ones.

    Decomposition is help for a stall, so it shouldn't clutter a task the moment
    it's added. This runs on the coaching tick: for the worst-avoided open todos
    that don't yet have a breakdown (and haven't already been decided on — a
    stored breakdown, a user dismissal, or a prior model decline), it asks the
    model to break the task down *or decline* if it's not worth it
    (:func:`decompose_task` with ``allow_decline``). A decline is recorded so we
    don't re-ask every tick. Returns how many breakdowns were created.

    Bounded by ``max_attempts`` model calls per tick (worst-avoided first), and a
    no-op when the user has switched auto-decompose off
    (:func:`auto_decompose_suppressed`). On-demand "Break it down" is unaffected.
    """
    if auto_decompose_suppressed(store):
        return 0
    avoided = avoided_todos(store.open_todos(), now)
    if not avoided:
        return 0
    max_first = store.get_float("max_first_step_minutes", DEFAULT_MAX_FIRST_STEP_MINUTES)
    decided = store.decomposition_feedback_todo_ids()
    guidance = learned_decomposition_guidance(store)
    made = attempts = 0
    for a in avoided:
        if attempts >= max_attempts:
            break
        todo = a["todo"]
        tid = todo["id"]
        if tid in decided or store.get_decomposition(tid) is not None:
            continue  # already decided (breakdown, dismissal, or prior decline)
        attempts += 1
        d = decompose_task(
            todo["title"],
            max_first_minutes=max_first,
            client=client,
            guidance=guidance,
            allow_decline=True,
        )
        if d is None:
            # The model judged it not worth breaking down — remember that so the
            # next tick doesn't re-ask (it also captures the model's judgment).
            store.record_decomposition_dismissal(
                todo_id=tid,
                title=todo.get("title"),
                reason="llm_declined",
                source="llm",
                category=todo.get("category"),
                estimate_minutes=todo.get("estimate_minutes"),
            )
            continue
        store.set_decomposition(
            tid,
            first_step=d.first_step,
            first_step_minutes=d.first_step_minutes,
            steps=d.steps,
            source=d.source,
        )
        made += 1
    return made


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


def _parse_deadline(value: object) -> datetime | None:
    """Parse a date-only todo deadline (``YYYY-MM-DD``) to an end-of-day datetime.

    Deadlines are stored date-only (see :class:`AugmentedTodo`), so they can't go
    through :func:`~prefrontal.clock.parse_ts`, which expects a full timestamp and
    returns ``None`` for a bare date. A deadline means "due by the end of that
    day" in the user's *local* zone, so this anchors to local 23:59:59 (converted
    to UTC) — anchoring to 23:59 UTC would flag a "due today" item as overdue
    hours early for a western-hemisphere user.
    """
    if not isinstance(value, str):
        return None
    try:
        day = datetime.strptime(value.strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None
    # Lazy imports: prefrontal.scheduling imports from this module, so importing
    # it (and config) at module top would be circular.
    from prefrontal.config import get_settings
    from prefrontal.scheduling import end_of_local_day_utc

    return end_of_local_day_utc(day, get_settings().timezone)


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
    deadline = _parse_deadline(todo.get("deadline"))
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


def _todo_priority(todo: dict[str, Any]) -> int:
    """A todo's priority as an int, defaulting an absent value to 1 (normal).

    The same normalization :func:`avoidance_score` and :func:`_dropped_is_give_up`
    apply inline; named here so :func:`focus_conflict` compares on the same scale.
    """
    priority = todo.get("priority")
    return 1 if priority is None else int(priority)


def focus_conflict(
    todos: list[dict[str, Any]], now: datetime
) -> dict[str, Any] | None:
    """Whether you're mid-flight on a *less important* task than one you're avoiding.

    The honest-prioritization companion to :func:`avoided_todos`: it's not enough
    to surface the thing you keep skipping — the sharper signal is catching it while
    you're actively working on something *lower* priority instead. "Working on" is a
    todo the user explicitly **started** (``started_at`` set, still open).

    Fires only when *everything* you've started is strictly lower priority than the
    most-important task you're **avoiding and haven't started** (the top of
    :func:`avoided_todos`). If you've also started something at least as important,
    you're already engaged with what matters, so there's no conflict — this stays
    quiet rather than nagging. Low-priority "someday" items can't be the "instead"
    (``avoided_todos`` already exempts them), so it never pushes a genuine maybe.

    Returns ``{working_on, instead, days_open}`` — the low-priority started todo, the
    more-important avoided one, and how long the latter's sat — or ``None`` when
    nothing is started, nothing important is being avoided, or you're already on it.
    """
    started = [
        t
        for t in todos
        if (t.get("status") or "open") == "open" and t.get("started_at")
    ]
    if not started:
        return None
    instead_hit = next(
        (a for a in avoided_todos(todos, now) if not a["todo"].get("started_at")),
        None,
    )
    if instead_hit is None:
        return None
    instead = instead_hit["todo"]
    instead_priority = _todo_priority(instead)
    # Quiet unless the *most* important thing you've started still ranks below the
    # avoided task — otherwise you're engaged with something that matters.
    if max(_todo_priority(t) for t in started) >= instead_priority:
        return None
    working_on = min(started, key=_todo_priority)
    return {
        "working_on": working_on,
        "instead": instead,
        "days_open": instead_hit["days_open"],
    }


def sort_todos_for_display(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return ``todos`` with in-progress (started) ones pinned to the top.

    A *stable* sort on "not started", so the caller's existing order (from
    :meth:`~prefrontal.memory.repos.todos.TodosRepo.open_todos` — priority then
    deadline) is preserved within both the started and not-started groups. This is a
    display concern only: it keeps the task you're mid-flight on visible at the top
    of the list instead of letting it sink under a higher-priority item you haven't
    begun, without touching the store's ordering (which scheduling/briefing/fitting
    all read raw).
    """
    return sorted(todos, key=lambda t: 0 if t.get("started_at") else 1)


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


def follow_through_stats(todos: list[dict[str, Any]]) -> dict[str, Any]:
    """Follow-through rollup over the todos the user explicitly *started*.

    Of the tasks you begin, how many do you finish? Counts only todos with a
    ``started_at`` set, split by what happened next: ``completed`` (done),
    ``abandoned`` (dropped), and ``in_progress`` (started but still open).
    ``rate`` is ``completed ÷ (completed + abandoned)`` — the follow-through rate
    among *resolved* started tasks (in-progress ones aren't counted for or against
    it yet) — or ``None`` until at least one started task has closed.

    Pure: fed the store's todos, no clock needed (status already reflects the
    outcome). The dashboard's insights panel renders this as a headline.
    """
    started = completed = abandoned = in_progress = 0
    for todo in todos:
        if not todo.get("started_at"):
            continue
        started += 1
        status = (todo.get("status") or "open").lower()
        if status == "done":
            completed += 1
        elif status == "dropped":
            abandoned += 1
        else:
            in_progress += 1
    resolved = completed + abandoned
    return {
        "started": started,
        "completed": completed,
        "abandoned": abandoned,
        "in_progress": in_progress,
        "rate": round(completed / resolved, 2) if resolved else None,
    }


# --- Outcome capture (feed the learning loop) --------------------------------
#
# Closing a todo is a real behavioral outcome, but until now it was thrown away:
# the learning pass only saw outings, focus sessions, and mail. A finished todo
# is a task ``success``; a dropped one is a ``miss`` — exactly the ``drift``
# signal for the ``task`` type, and the moment an avoided todo finally resolves.
# This mirrors ``record_outing_return`` / ``record_outing_abandoned`` so todo
# closes flow into the same episode history every other touchpoint already does.


#: Outcome for a "this is wrong" drop — a mis-captured / no-longer-relevant todo
#: cleared as hygiene. Deliberately NOT ``miss``: it's outside ``DRIFT_WEIGHTS`` so
#: it never touches the ``drift`` score, and the briefing's "Slipped" counts only
#: ``miss`` — so hygiene drops don't masquerade as things you let slip. The episode
#: is still logged (with the age note) for provenance / future capture-quality work.
DISCARDED_OUTCOME = "discarded"


def _dropped_is_give_up(todo: dict[str, Any], ref: datetime | None) -> bool:
    """Whether dropping ``todo`` reads as "I give up" (a real ``miss``) vs "this is
    wrong" (hygiene, :data:`DISCARDED_OUTCOME`).

    Give-up = you abandoned a genuine, aging commitment: it was something you'd
    been avoiding (a real priority, open past the avoidance floor — the same
    definition behind "You keep putting off") **or** it was already overdue. A
    quick cleanup drop, a low-priority "someday", or a not-yet-aged item is
    hygiene, not a slip.

    Recomputes the avoidance inputs directly rather than calling
    :func:`avoidance_score`, because that is status-gated and the row is already
    ``dropped`` by the time a close is recorded.

    A todo the user explicitly **started** and then dropped is always a give-up —
    you engaged with it and abandoned it, which is the follow-through failure worth
    counting — regardless of priority or age.
    """
    if todo.get("started_at"):
        return True
    priority = todo.get("priority")
    priority = 1 if priority is None else int(priority)
    if priority < 1:
        return False  # low / "someday" — dropping it is just tidying up
    if ref is None:
        return False  # can't tell how long it sat → treat as hygiene, not a slip
    deadline = _parse_deadline(todo.get("deadline"))
    if deadline is not None and deadline < ref:
        return True  # you had a deadline and let it pass, then bailed → giving up
    days = _days_open(todo, ref)
    return days is not None and days >= DEFAULT_AVOIDANCE_MIN_DAYS


def todo_episode_fields(
    todo: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Derive :meth:`MemoryStore.log_episode` kwargs from a closed todo (pure).

    A ``done`` todo is a task ``success``. A ``dropped`` one is classified: an
    "I give up" drop (:func:`_dropped_is_give_up` — an aging/overdue commitment you
    abandoned) is a ``miss`` that folds into the ``task`` ``drift`` score and the
    briefing's "Slipped" line; a "this is wrong" hygiene drop is
    :data:`DISCARDED_OUTCOME`, logged for provenance but counted by neither. The
    estimate is recorded as ``predicted_value``, but ``actual_value`` is
    deliberately ``None``: a todo's created→closed span is wall-clock, not time
    spent on task, so treating it as the actual duration would pollute
    ``time_estimation`` (the same reasoning as ``record_outing_abandoned``). The
    age is kept in ``notes`` for future analysis instead.

    Args:
        todo: A todo dict (as returned by the store), ideally post-close so its
            ``status`` and ``completed_at`` are current.
        now: Reference time (naive UTC) for the age note and the give-up-vs-hygiene
            call when ``completed_at`` is absent (e.g. a dropped todo). ``None``
            skips the age note and treats a drop as hygiene (we can't date it).

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
    # Follow-through signal: was this a task the user had explicitly *started*?
    # "started → completed" is a follow-through win; "started → dropped" is an
    # abandon-after-starting — the exact pattern worth tracking. Record it in the
    # note (and mark the context) so the learning pass and briefing can see it.
    started_at = _parse_ts(todo.get("started_at"))
    started = started_at is not None
    if started:
        tail = "completed after starting" if done else "abandoned after starting"
        if started_at is not None and end is not None:
            hours = max(0.0, (end - started_at).total_seconds() / 3600.0)
            tail += f" ({hours:.1f}h in)"
        notes = f"{notes}; {tail}" if notes else tail
    if done:
        outcome = "success"
    elif _dropped_is_give_up(todo, end):
        outcome = "miss"
    else:
        outcome = DISCARDED_OUTCOME
    verb = "done" if done else "dropped"
    if started:
        verb += ", started"
    return {
        "episode_type": "task",
        "predicted_value": todo.get("estimate_minutes"),
        "actual_value": None,
        "acknowledged": None,
        "context": f"todo {verb}: {todo.get('title')}",
        "outcome": outcome,
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


# --- one-off cleanup: reclassify historical hygiene todo-drops ----------------
#
# Before the give-up-vs-hygiene split, every dropped todo logged a `miss`, so
# past cleanup (mis-captured / no-longer-relevant todos) inflated the briefing's
# "Slipped" line and the `task` drift score. This backfill downgrades the hygiene
# ones to `discarded`. The old episode carries only the drop age (in `notes`), not
# the priority/deadline, so it's classified on age alone — conservatively: only a
# clearly-quick drop (under the avoidance floor) is downgraded; an aged one is
# left a `miss` (it may have been a genuine give-up, and we can't prove otherwise).

#: The age note `todo_episode_fields` writes on a dropped todo, e.g. "dropped
#: after 0.5d open" — the only per-episode signal the backfill has to work with.
_DROP_AGE_RE = re.compile(r"dropped after ([\d.]+)d open")


def historical_drop_is_hygiene(episode: dict[str, Any]) -> bool:
    """Whether a past ``todo dropped:`` ``miss`` episode was a *hygiene* drop.

    Uses the drop age recorded in ``notes``: under :data:`DEFAULT_AVOIDANCE_MIN_DAYS`
    is hygiene (a quick "this is wrong" clear). An absent/unparseable age, or one
    at/over the floor, returns ``False`` — left a ``miss`` rather than risk
    downgrading a real give-up.
    """
    m = _DROP_AGE_RE.search(episode.get("notes") or "")
    if not m:
        return False
    try:
        return float(m.group(1)) < DEFAULT_AVOIDANCE_MIN_DAYS
    except ValueError:
        return False


def reclassify_hygiene_drops(store: MemoryStore, *, apply: bool) -> dict[str, Any]:
    """Downgrade historical hygiene todo-drop misses to ``discarded`` (idempotent).

    Scans the caller's ``task`` ``miss`` episodes that came from a todo drop
    (``context`` starts with ``"todo dropped:"``) and, for the hygiene ones
    (:func:`historical_drop_is_hygiene`), rewrites the outcome to
    :data:`DISCARDED_OUTCOME` so they stop feeding drift + "Slipped". ``apply``
    ``False`` is a dry run (counts only). Re-running is a no-op (the rewritten
    rows are no longer ``miss``). Returns ``{scanned, reclassified, samples}``.
    """
    scanned = reclassified = 0
    samples: list[str] = []
    for ep in store.episodes_by_type("task", limit=1_000_000):
        if ep.get("outcome") != "miss":
            continue
        context = ep.get("context") or ""
        if not context.startswith("todo dropped:"):
            continue
        scanned += 1
        if not historical_drop_is_hygiene(ep):
            continue
        if apply:
            store.reclassify_episode_outcome(ep["id"], outcome=DISCARDED_OUTCOME)
        reclassified += 1
        if len(samples) < 5:
            samples.append(context)
    return {"scanned": scanned, "reclassified": reclassified, "samples": samples}
