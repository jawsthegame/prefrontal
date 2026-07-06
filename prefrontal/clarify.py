"""Ambiguity clarification — hone a vague todo/commitment, then guide it.

Task paralysis has a quieter cause than size: you can't start what you can't
*name*. A calendar event called "Tax" — or a todo that just says "Mom" — stalls
not because it's big but because it's ambiguous. Is "Tax" the filing deadline, a
property-tax bill, or a meeting with the accountant? Each reading is a different
task, so the mind can't pick a first move and the item quietly rots into another
avoided loop. This module gives the system a way to *notice* that ambiguity and
hone it in before that happens — and, once the item resolves to a recognized
**task type**, to offer a guided walkthrough (a "playbook") so the honed task has
an obvious way in.

Two halves, both mirroring shapes already in the codebase:

- **Detection + clarifying question.** A pure heuristic ambiguity score (short /
  single-word / known-ambiguous titles, discounted when a clear action verb or
  concrete detail is present) gates an optional local-model pass that proposes
  ONE clarifying question with a few candidate interpretations
  (:func:`detect_clarification`). Same LLM-first-with-heuristic-fallback,
  graceful-degradation shape as :mod:`prefrontal.todos` / :mod:`prefrontal.classify`:
  no model → a hand-authored question rather than an invented one, and nothing is
  written until the human answers (the pending-until-confirmed safety model of
  :mod:`prefrontal.sensor`).
- **Playbooks.** A small registry of ``task_type -> Playbook`` (ordered steps)
  for recognized tasks. When a clarification resolves to a known type, the
  dashboard opens that playbook as a dim-everything guide overlay — the same
  overlay pattern panic mode uses. An unrecognized reading simply gets no
  playbook (the Task Paralysis "Break it down" lever still applies to it).

Nothing here touches the store: detection and playbooks are pure and testable,
the Ollama client is injected, and persistence lives in the ``clarifications``
repo (:mod:`prefrontal.memory.repos.clarifications`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prefrontal.integrations.ollama import OllamaError
from prefrontal.llm_json import extract_json_object
from prefrontal.memory.repos.clarifications import TARGET_COMMITMENT, TARGET_TODO

if TYPE_CHECKING:
    from prefrontal.integrations import Generator
    from prefrontal.memory.store import MemoryStore

#: Cap on how many ambiguous items one detection sweep looks at, so a coaching
#: tick fires a bounded number of model calls (todos first, priority-ordered, then
#: upcoming commitments). Mirrors the decomposition sweep's ``max_attempts``.
MAX_SWEEP_ITEMS = 8

#: An item at/above this ambiguity score is worth a clarifying question. Tuned so
#: a bare noun ("Tax", "Mom") clears it while a spelled-out action ("Call the
#: dentist to reschedule") stays well below.
AMBIGUITY_THRESHOLD = 0.5

#: The most candidate interpretations to offer for one item — enough to cover the
#: common readings without turning the inline question into a survey.
MAX_OPTIONS = 4

#: Action verbs whose presence means the title already says *what to do* — a
#: strong signal the item is not ambiguous ("call the dentist" needs no honing).
_ACTION_VERBS = frozenset(
    {
        "call", "email", "text", "message", "reply", "respond", "send", "ask",
        "pay", "buy", "order", "book", "schedule", "rsvp", "confirm", "cancel",
        "renew", "file", "submit", "sign", "register", "return", "pick", "drop",
        "write", "draft", "read", "review", "plan", "prepare", "finish", "fix",
        "clean", "organize", "print", "download", "upload", "check", "update",
        "wash", "pack", "mail", "post", "deposit", "transfer", "refund", "reschedule",
    }
)

#: Bare nouns that are notoriously under-specified on a calendar or todo list —
#: each stands for several genuinely different tasks. Matched whole-word against a
#: normalized title. These both raise the ambiguity score and seed the offline
#: candidate interpretations below.
_AMBIGUOUS_TOKENS = frozenset(
    {
        "tax", "taxes", "mom", "dad", "car", "house", "insurance", "doctor",
        "dentist", "meeting", "appt", "appointment", "review", "project", "bank",
        "school", "vet", "birthday", "gift", "trip", "renewal", "bill", "form",
        "kids", "work", "passport", "dmv", "mortgage", "rent", "visa", "license",
        "physical", "checkup", "quarterly", "benefits", "will", "estate",
    }
)

#: Hand-authored candidate readings for the most common ambiguous single tokens,
#: so the "Tax" case works fully offline (no model needed). Each entry is
#: ``(label, task_type)`` where ``task_type`` is a :data:`PLAYBOOKS` key when the
#: reading is recognized (it becomes a guided walkthrough), else ``None``. The
#: last option is always a plain "Something else" escape hatch, added at build time.
_TOKEN_INTERPRETATIONS: dict[str, list[tuple[str, str | None]]] = {
    "tax": [
        ("Filing my tax return", "tax_filing"),
        ("A property / vehicle tax bill to pay", None),
        ("A meeting with my accountant", None),
    ],
    "taxes": [
        ("Filing my tax return", "tax_filing"),
        ("A property / vehicle tax bill to pay", None),
        ("A meeting with my accountant", None),
    ],
    "passport": [
        ("Renewing / applying for a passport", "passport_renewal"),
        ("A passport photo to get taken", None),
        ("Finding / filing the passport somewhere", None),
    ],
    "doctor": [
        ("A doctor's appointment to attend", "medical_appointment"),
        ("Booking a doctor's appointment", "medical_appointment"),
        ("Following up on results / a prescription", None),
    ],
    "dentist": [
        ("A dentist appointment to attend", "medical_appointment"),
        ("Booking a dentist appointment", "medical_appointment"),
        ("A follow-up (billing, results)", None),
    ],
    "car": [
        ("A service / repair appointment", None),
        ("Registration / insurance renewal", None),
        ("Something to buy or sell", None),
    ],
    "dmv": [
        ("Renewing a license / registration", None),
        ("An in-person DMV appointment", None),
        ("Gathering the paperwork first", None),
    ],
    "insurance": [
        ("A renewal / payment", None),
        ("Filing a claim", None),
        ("Comparing / switching policies", None),
    ],
}


def _norm(title: str | None) -> str:
    """Lowercase, collapse whitespace — the shared normalization for matching."""
    return re.sub(r"\s+", " ", (title or "").lower()).strip()


def _tokens(title: str) -> list[str]:
    """Word tokens of a normalized title (letters/digits only)."""
    return re.findall(r"[a-z0-9]+", _norm(title))


def _has_action_verb(tokens: list[str]) -> bool:
    """Whether any token is a known action verb (the title says what to do)."""
    return any(t in _ACTION_VERBS for t in tokens)


def ambiguous_token(title: str) -> str | None:
    """Return the first known-ambiguous bare noun in ``title``, or ``None``."""
    for t in _tokens(title):
        if t in _AMBIGUOUS_TOKENS:
            return t
    return None


def ambiguity_score(title: str) -> float:
    """How ambiguous a todo/commitment title looks, in ``[0.0, 1.0]`` (pure).

    A deliberately conservative heuristic — the cost of a false positive is an
    unwanted inline question, so it errs toward *not* asking. The signals:

    - **Length.** One or two bare words score high (nothing pins the meaning
      down); four or more words score low (the phrase carries its own context).
    - **A known-ambiguous noun** ("tax", "car", "review") raises the score.
    - **An action verb** ("call", "pay", "renew") lowers it sharply — the title
      already names the move.
    - **Concrete detail** (a number, a time, a proper multi-word name) lowers it.

    Returns ``0.0`` for an empty title.
    """
    tokens = _tokens(title)
    n = len(tokens)
    if n == 0:
        return 0.0

    if n == 1:
        score = 0.6
    elif n == 2:
        score = 0.45
    elif n == 3:
        score = 0.3
    else:
        score = 0.1

    token = ambiguous_token(title)
    if token is not None:
        score += 0.3
        # A lone known-ambiguous noun ("Tax") is the canonical case — pin it high.
        if n == 1:
            score += 0.1

    if _has_action_verb(tokens):
        score -= 0.4  # names the action → far less ambiguous

    # Concrete anchors that make even a short title actionable: a digit (a time,
    # amount, or date) or a longer phrase reads as specific, not vague.
    if any(c.isdigit() for c in title):
        score -= 0.25
    if n >= 5:
        score -= 0.15

    return max(0.0, min(1.0, round(score, 3)))


def is_ambiguous(title: str, *, threshold: float = AMBIGUITY_THRESHOLD) -> bool:
    """Whether ``title`` is ambiguous enough to be worth a clarifying question."""
    return ambiguity_score(title) >= threshold


# -- Clarification candidate ---------------------------------------------------


@dataclass(frozen=True)
class ClarificationOption:
    """One candidate reading of an ambiguous item the user can pick.

    ``task_type`` names a :data:`PLAYBOOKS` entry when this reading is a
    recognized task (choosing it opens a guided walkthrough); ``None`` when it's
    just a disambiguation with no built-in guide.
    """

    label: str
    task_type: str | None = None


@dataclass(frozen=True)
class ClarificationCandidate:
    """A proposed clarifying question for one ambiguous todo/commitment.

    Never written on its own — it becomes a pending ``clarifications`` row that
    the human answers (mirroring the sensor's propose-then-confirm safety model).
    ``source`` is ``"llm"`` when the model phrased it, else ``"heuristic"``.
    """

    title: str
    question: str
    options: list[ClarificationOption] = field(default_factory=list)
    source: str = "heuristic"


_SYSTEM = (
    "You help someone with ADHD who lists tasks and calendar events with vague, "
    "one-word titles they later can't act on. Given ONE such title, produce a "
    "single short clarifying question and 2-4 candidate interpretations — the "
    "distinct things the title might actually mean. Reply with ONLY JSON, no "
    'prose: {"question": "<one short question>", "options": ["<reading 1>", '
    '"<reading 2>", ...]}. Each reading is a short phrase (a few words). Do not '
    "invent specifics the title can't support; keep the readings plausible and "
    "general. Return an empty options list only if the title is already clear."
)


def _known_task_type(label: str) -> str | None:
    """Best-effort map of a free-text reading to a recognized playbook key.

    Keyword-matched against the label so an LLM-phrased reading like "file my tax
    return" still lands on ``tax_filing``. Conservative — an unmatched reading is
    simply a plain disambiguation (no guide), never a wrong one.
    """
    text = _norm(label)
    for task_type, keywords in _TASK_TYPE_KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(k)}\b", text) for k in keywords):
            return task_type
    return None


def _heuristic_candidate(title: str) -> ClarificationCandidate:
    """A hand-authored clarifying question when the model is unavailable.

    For a recognized ambiguous token ("tax") the readings come from
    :data:`_TOKEN_INTERPRETATIONS`; otherwise a generic is-it-a-task-or-an-event
    question that still lets the user hone the item in one tap.
    """
    token = ambiguous_token(title)
    readings = _TOKEN_INTERPRETATIONS.get(token or "")
    if readings:
        options = [ClarificationOption(label, tt) for label, tt in readings[: MAX_OPTIONS - 1]]
        question = f"“{title.strip()}” could mean a few things — which is it?"
    else:
        options = [
            ClarificationOption("A task I need to do"),
            ClarificationOption("An event / appointment to attend"),
            ClarificationOption("A reminder about someone or something"),
        ]
        question = f"What does “{title.strip()}” refer to?"
    options.append(ClarificationOption("Something else"))
    return ClarificationCandidate(
        title=title.strip(), question=question, options=options, source="heuristic"
    )


def _coerce_llm_candidate(title: str, raw: dict[str, Any]) -> ClarificationCandidate | None:
    """Validate a model reply into a candidate, or ``None`` if it's unusable.

    Keeps only well-typed short readings, caps them at :data:`MAX_OPTIONS`, maps
    each to a recognized ``task_type`` where possible, and always appends a
    "Something else" escape hatch. An empty/too-short question falls back to a
    generic one so a usable options list is never dropped on phrasing alone.
    """
    raw_options = raw.get("options")
    if not isinstance(raw_options, list):
        return None
    seen: set[str] = set()
    options: list[ClarificationOption] = []
    for item in raw_options:
        raw_label = item.get("label") if isinstance(item, dict) else item
        label = str(raw_label or "").strip()
        if not label or len(label) > 80:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        options.append(ClarificationOption(label[:80], _known_task_type(label)))
        if len(options) >= MAX_OPTIONS - 1:
            break
    if not options:
        return None
    options.append(ClarificationOption("Something else"))
    question = str(raw.get("question") or "").strip()
    if len(question) < 3:
        question = f"What does “{title.strip()}” refer to?"
    return ClarificationCandidate(
        title=title.strip(), question=question[:200], options=options, source="llm"
    )


def detect_clarification(
    title: str,
    *,
    client: Generator | None = None,
    threshold: float = AMBIGUITY_THRESHOLD,
) -> ClarificationCandidate | None:
    """Propose a clarifying question for an ambiguous title, or ``None``.

    Returns ``None`` immediately when the title isn't ambiguous enough
    (:func:`is_ambiguous`) — the gate that keeps the system from questioning
    clear items. When it is ambiguous, the local model (if reachable) phrases the
    question and readings; on any model failure — or an unusable reply — it falls
    back to a hand-authored question (:func:`_heuristic_candidate`), so an
    ambiguous item always yields *something* to ask rather than a guess or a gap.

    Args:
        title: The todo/commitment title.
        client: An Ollama-like :class:`~prefrontal.integrations.Generator`, or
            ``None`` to use the heuristic only.
        threshold: Ambiguity gate (see :func:`ambiguity_score`).

    Returns:
        A :class:`ClarificationCandidate`, or ``None`` if the title is clear.
    """
    if not is_ambiguous(title, threshold=threshold):
        return None
    if client is not None:
        try:
            reply = client.generate(f"Title: {title.strip()}", system=_SYSTEM)
        except OllamaError:
            reply = ""
        candidate = _coerce_llm_candidate(title, extract_json_object(reply))
        if candidate is not None:
            return candidate
    return _heuristic_candidate(title)


# -- Playbooks (the guided walkthrough) ----------------------------------------
#
# A playbook is an ordered, hand-authored set of steps for a recognized task
# type. It's the "pop up / overlay that helps guide me through the task" — shown
# in the dashboard's dim-everything guide overlay once a clarification resolves to
# a known type. Static data on purpose: a walkthrough for filing taxes shouldn't
# depend on a model being up, and the steps are the same for everyone.


@dataclass(frozen=True)
class PlaybookStep:
    """One step in a guided walkthrough: a short imperative plus optional detail."""

    title: str
    detail: str = ""


@dataclass(frozen=True)
class Playbook:
    """An ordered walkthrough for a recognized task type."""

    task_type: str
    title: str
    steps: list[PlaybookStep]
    intro: str = ""


PLAYBOOKS: dict[str, Playbook] = {
    "tax_filing": Playbook(
        task_type="tax_filing",
        title="Filing your tax return",
        intro="One step at a time — you don't have to hold the whole thing at once.",
        steps=[
            PlaybookStep(
                "Find the deadline and write it down",
                "Confirm the filing date so the rest has a real clock. Note it on "
                "the calendar now.",
            ),
            PlaybookStep(
                "Gather your income documents",
                "W-2s, 1099s, interest/dividend statements. Make one folder (physical "
                "or a single email label) and drop them in as they arrive.",
            ),
            PlaybookStep(
                "Pull together deductions and receipts",
                "Charitable gifts, medical, mortgage interest, business expenses — "
                "whatever applied this year. Rough is fine; you're collecting, not "
                "calculating.",
            ),
            PlaybookStep(
                "Pick how you'll file",
                "Tax software, an accountant, or free file. If you used someone last "
                "year, start by re-opening that.",
            ),
            PlaybookStep(
                "Enter everything and review",
                "Work through the software/return section by section. Save as you go "
                "so you can stop and come back.",
            ),
            PlaybookStep(
                "Submit and save proof",
                "File it, then save the confirmation and a copy of the return "
                "somewhere you'll find it next year.",
            ),
        ],
    ),
    "passport_renewal": Playbook(
        task_type="passport_renewal",
        title="Renewing your passport",
        intro="A paperwork task with a clear order — just take the next step.",
        steps=[
            PlaybookStep(
                "Check your expiry date and travel dates",
                "Many countries require 6 months' validity. That tells you how urgent this is.",
            ),
            PlaybookStep(
                "Confirm you can renew (vs. apply new)",
                "Renewal by mail is usually possible if your current passport is "
                "undamaged and recent. Otherwise it's an in-person appointment.",
            ),
            PlaybookStep(
                "Get a compliant passport photo",
                "Pharmacy, post office, or a phone app that meets the spec.",
            ),
            PlaybookStep(
                "Fill out the renewal form",
                "Download and complete it — don't sign until instructed if mailing.",
            ),
            PlaybookStep(
                "Assemble the packet and pay",
                "Form, photo, current passport, and the fee. Double-check the current fee amount.",
            ),
            PlaybookStep(
                "Mail (tracked) or attend your appointment",
                "Use tracked mail, or add the appointment to your calendar with travel time.",
            ),
        ],
    ),
    "medical_appointment": Playbook(
        task_type="medical_appointment",
        title="Sorting out the appointment",
        intro="Small, concrete moves — start with whichever is true for you.",
        steps=[
            PlaybookStep(
                "Decide: book one, or attend one already booked?",
                "If it's already on the calendar, jump to the last two steps.",
            ),
            PlaybookStep(
                "Find the number / booking link",
                "Put it on screen so calling is a tap, not a search.",
            ),
            PlaybookStep(
                "Book the slot",
                "Pick a time and write down the date, place, and any reference number.",
            ),
            PlaybookStep(
                "Note what to bring",
                "Insurance card, ID, referral, a list of questions or symptoms.",
            ),
            PlaybookStep(
                "Add it with travel time",
                "Block the appointment plus the drive/prep so a departure reminder can fire.",
            ),
        ],
    ),
}


def resolve_playbook(task_type: str | None) -> Playbook | None:
    """Return the built-in playbook for ``task_type``, or ``None`` if unrecognized."""
    if not task_type:
        return None
    return PLAYBOOKS.get(task_type)


def known_task_types() -> frozenset[str]:
    """The recognized task types that have a guided playbook."""
    return frozenset(PLAYBOOKS)


#: Keyword cues that map a free-text reading (or an LLM-phrased option) onto a
#: recognized playbook key. Kept beside the playbooks so adding one is one edit.
_TASK_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "tax_filing": ("tax return", "file", "filing", "irs", "return"),
    "passport_renewal": ("passport", "renew passport", "visa"),
    "medical_appointment": ("doctor", "dentist", "appointment", "clinic", "checkup", "physical"),
}


def playbook_view(playbook: Playbook) -> dict[str, Any]:
    """A JSON-ready view of a playbook, for the guide overlay / API responses."""
    return {
        "task_type": playbook.task_type,
        "title": playbook.title,
        "intro": playbook.intro,
        "steps": [{"title": s.title, "detail": s.detail} for s in playbook.steps],
    }


def candidate_view(candidate: ClarificationCandidate) -> dict[str, Any]:
    """A JSON-ready view of a clarification candidate (question + options)."""
    return {
        "title": candidate.title,
        "question": candidate.question,
        "source": candidate.source,
        "options": [
            {"label": o.label, "task_type": o.task_type} for o in candidate.options
        ],
    }


# -- Detection sweep (the coaching-tick lever) --------------------------------
#
# The tick-driven counterpart of the decomposition sweep
# (prefrontal.todos.sweep_avoided_decompositions): each coaching tick notices
# newly-ambiguous items and files a pending clarifying question, so the "Needs
# clarification" queue fills passively rather than only when the dashboard's
# manual check is pressed. Bounded model calls per tick, and it never re-asks an
# item it has history for.


def sweep_ambiguous_items(
    store: MemoryStore,
    client: Generator | None,
    *,
    limit: int = MAX_SWEEP_ITEMS,
) -> list[int]:
    """File clarifying questions for ambiguous todos/commitments (tick sweep).

    Sweeps the user's open todos (priority-ordered) then upcoming commitments,
    skipping any item that already has clarification history
    (:meth:`~prefrontal.memory.repos.clarifications.ClarificationsRepo.clarified_target_ids`
    — pending, answered, *or* dismissed, so nothing is re-asked) and any ``fyi``
    commitment (someone else's event, never the user's task). For each remaining
    item it scores ambiguity and, for the vague ones, files ONE pending question
    via :func:`detect_clarification` (local model, heuristic fallback). Bounded to
    ``limit`` detections per run — the model-call budget for one tick.

    Args:
        store: A user-scoped :class:`~prefrontal.memory.store.MemoryStore`.
        client: An Ollama-like generator (``None`` uses the heuristic detector).
        limit: Max items inspected/model-called this run.

    Returns:
        The ids of the clarifications created (possibly empty).
    """
    seen_todos = store.clarified_target_ids(TARGET_TODO)
    seen_commits = store.clarified_target_ids(TARGET_COMMITMENT)
    candidates: list[tuple[str, dict[str, Any]]] = [
        (TARGET_TODO, t) for t in store.open_todos() if t["id"] not in seen_todos
    ] + [
        (TARGET_COMMITMENT, c)
        for c in store.upcoming_commitments(limit=50)
        if c["id"] not in seen_commits and c.get("kind") != "fyi"
    ]
    created: list[int] = []
    checked = 0
    for target_type, item in candidates:
        if checked >= limit:
            break
        checked += 1
        candidate = detect_clarification(item.get("title") or "", client=client)
        if candidate is None:
            continue
        created.append(
            store.add_clarification(
                target_type=target_type,
                target_id=item["id"],
                title=candidate.title,
                question=candidate.question,
                options=candidate_view(candidate)["options"],
                source=candidate.source,
            )
        )
    return created
