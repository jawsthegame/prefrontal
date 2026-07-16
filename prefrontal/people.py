"""People — names mentioned in ingested items, queued for identify + categorize.

Ingested items (mail, calendar, an n8n event, a manual capture) constantly *name*
people — "call with Sam", "budget from Dana Ruiz", "Mrs. Alvarez emailed about the
field trip". Left unstructured those names are just text; captured and categorized
they become two things the rest of Prefrontal already trades on:

- **learning** — who recurs in your life, and how you relate to them (family /
  coworker / professional / …), enriches the behavioral profile the summarizer
  injects into every agent's prompt.
- **prioritization** — an action that names someone who matters to you should
  outrank one that names a stranger. A todo triage creates from a signal that
  mentions a high-importance person gets a small priority bump.

This module is the **pure, model-optional core** (mirroring
:mod:`prefrontal.triage` and :mod:`prefrontal.blockers`): deterministic name
extraction (:func:`extract_names`) that always runs, an optional one-call model
refinement (:func:`extract_names_llm`) that only ever *adds* to it and falls back
silently, and the small helpers that shape a roster/queue for a surface. The
storage side is :class:`prefrontal.memory.repos.people.PeopleRepo`; the
:func:`enqueue_mentions` orchestrator is the one place that both reads (skip a
known person) and writes (queue an unknown one, touch a known one).

The extractor leans toward *surfacing* a candidate rather than dropping it — a
missed real person is a learning signal lost, whereas a stray false positive is
one dismiss in the review queue. But "lean toward surfacing" is not "surface every
capitalized phrase": real mail and calendar text is dominated by Title-Case
noun-phrases that are not people ("Weekly Report", "Order Confirmation", "United
Airlines"), so the extractor drops runs that are purely generic words or that name
an organization (:data:`_COMMON_WORDS`, :data:`_ORG_MARKERS`). That filter is what
keeps the queue reviewable instead of almost-all false positives. Nothing here is
authoritative: an extracted name is a **pending** :data:`STATUS_PENDING` mention
until a human identifies or dismisses it, the same propose-don't-apply stance the
LLM sensor and clarifications take.
"""
from __future__ import annotations

import re
from typing import Any

from prefrontal.integrations import Generator, ProviderError
from prefrontal.llm_json import extract_json

#: How the user relates to a person. ``unknown`` is the pre-identification default;
#: everything else is a category the user assigns when they identify a mention.
RELATIONSHIPS: tuple[str, ...] = (
    "family",
    "coworker",
    "friend",
    "professional",  # doctor, teacher, lawyer, contractor acting in a role
    "service",       # a business/vendor contact
    "acquaintance",
    "other",
    "unknown",
)

#: How much a person matters, mirroring the todo/blocker ``priority`` scale so the
#: two compose: 0 low · 1 normal · 2 high · 3 top. Drives :func:`priority_boost`.
IMPORTANCE_MIN = 0
IMPORTANCE_MAX = 3

#: Mention lifecycle. A mention is `pending` until a human resolves it to
#: `identified` (linked to a :class:`people` row) or `dismissed` (not a person, or
#: not worth tracking).
STATUS_PENDING = "pending"
STATUS_IDENTIFIED = "identified"
STATUS_DISMISSED = "dismissed"

#: Person lifecycle — an identified person is `active` until `archived` (no longer
#: relevant, but kept so their history/mentions stay legible).
PERSON_ACTIVE = "active"
PERSON_ARCHIVED = "archived"

#: How much text to keep as the mention's context snippet (enough to recognize the
#: item, not the whole body).
_CONTEXT_CHARS = 200

# --- name extraction --------------------------------------------------------

#: Lowercased tokens that look like a Title-Case name but never are one on their
#: own — weekday/month names, mail-header words, and common capitalized fillers.
#: A run made entirely of stopwords is dropped; a run that merely *begins* with
#: one has the leading stopword(s) peeled (so "New York" → "York", then dropped as
#: a lone token with no cue), while "Best Regards" (all stopwords) is dropped
#: outright.
_STOPWORDS: frozenset[str] = frozenset(
    {
        # weekdays / months (+ common abbreviations)
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        "january", "february", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
        "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
        # greeting / sign-off / mail-header noise
        "hi", "hello", "hey", "dear", "re", "fwd", "fw", "subject", "sent", "date",
        "from", "sender", "cc", "bcc", "regards", "best", "sincerely", "cheers",
        "thanks", "thank", "regarding", "please", "note", "reminder", "fyi", "ps",
        # generic capitalized fillers that begin sentences
        "the", "a", "an", "i", "we", "you", "it", "this", "that", "these", "those",
        "today", "tomorrow", "tonight", "yesterday", "next", "last", "new",
        "your", "our", "my", "his", "her", "their", "and", "or", "but",
    }
)

#: Lowercased words that are common Title-Cased tokens in mail/calendar/task text
#: but are essentially never a person's given name or surname. A candidate whose
#: *every* token is one of these (or a :data:`_STOPWORDS` entry) is dropped, so a
#: capitalized noun-phrase like "Field Trip", "Weekly Report" or "Order
#: Confirmation" never reaches the review queue — while a real name survives
#: because at least one of its tokens ("Dana", "Ruiz") is not a common word.
#:
#: Deliberately conservative: any word that is *also* a plausible name (Bill,
#: Mark, Grace, Will, May, June, Rose, …) is left OUT, since dropping a real name
#: costs more than surfacing one extra generic phrase.
_COMMON_WORDS: frozenset[str] = frozenset(
    {
        # scheduling / time
        "meeting", "meetings", "call", "calls", "appointment", "appointments",
        "appt", "reminder", "reminders", "event", "events", "invite", "invitation",
        "schedule", "agenda", "deadline", "followup", "review", "reviews", "sync",
        "standup", "checkin", "session", "conference", "webinar", "zoom", "hangout",
        "recap", "kickoff", "onboarding", "interview", "demo", "training",
        # business / correspondence nouns
        "report", "reports", "budget", "invoice", "invoices", "receipt", "receipts",
        "order", "orders", "payment", "payments", "statement", "statements",
        "account", "accounts", "update", "updates", "request", "requests", "notice",
        "notification", "notifications", "alert", "alerts", "confirmation", "summary",
        "proposal", "contract", "agreement", "project", "projects", "task", "tasks",
        "todo", "ticket", "tickets", "issue", "issues", "form", "forms", "document",
        "documents", "file", "files", "folder", "message", "inbox", "newsletter",
        "subscription", "offer", "offers", "deal", "deals", "sale", "sales",
        "support", "billing", "shipping", "delivery", "tracking", "refund", "return",
        "quote", "estimate", "reservation", "booking", "itinerary", "checkout",
        "status", "info", "information", "details", "detail", "list", "plan",
        "plans", "results", "result", "version", "menu", "photo", "photos",
        # generic / place / filler nouns
        "trip", "trips", "vacation", "holiday", "flight", "flights", "hotel", "room",
        "office", "building", "floor", "street", "road", "avenue", "city", "town",
        "north", "south", "east", "west", "field", "park", "center", "centre",
        "quick", "weekly", "monthly", "daily", "annual", "urgent", "important",
        "final", "draft", "copy", "action", "items", "item", "welcome", "congrats",
        "congratulations", "happy", "birthday", "anniversary", "team", "group",
    }
)

#: Lowercased tokens that mark an *organization* rather than a person. A candidate
#: run containing ANY of these names a company/institution ("United Airlines",
#: "Chase Bank", "State University"), so the whole run is dropped even though it is
#: otherwise a clean Title-Case run.
_ORG_MARKERS: frozenset[str] = frozenset(
    {
        "inc", "corp", "corporation", "co", "company", "llc", "ltd", "plc", "gmbh",
        "bank", "airlines", "airways", "airline", "university", "college", "school",
        "hospital", "clinic", "institute", "foundation", "association", "society",
        "department", "bureau", "agency", "authority", "committee", "council",
        "store", "shop", "market", "supermarket", "restaurant", "cafe", "hotel",
        "services", "solutions", "systems", "technologies", "industries", "partners",
        "group", "holdings", "enterprises", "media", "network", "labs", "studio",
    }
)

#: Lowercased words that, immediately before a lone Title-Case token, are strong
#: evidence the token is a person's name ("call **Sam**", "from **Dana**").
_NAME_CUES: frozenset[str] = frozenset(
    {
        "with", "w/", "from", "to", "for", "call", "email", "emailing", "emailed",
        "meeting", "met", "meet", "per", "ask", "asked", "tell", "told", "remind",
        "reminded", "dear", "hi", "hello", "hey", "contact", "see", "text", "texted",
        "cc", "thanks", "thank", "re", "regarding", "about", "reply", "ping",
        # directed-action verbs — the following word is usually the recipient
        "send", "sent", "give", "notify", "forward", "invite", "pay",
        # honorifics — the following word is the actual name ("Dr Lee" without a dot)
        "mr", "mrs", "ms", "dr", "prof", "sir", "aunt", "uncle", "grandma", "grandpa",
    }
)

#: A Title-Case run of 1–3 words, with an optional trailing possessive ("Sam's").
#: Group 1 is the run; group 2 is the ``'s`` marker when present.
_TITLE_RUN = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})('s)?\b")
#: Honorific + name with a literal dot ("Dr. Lee", "Mrs. Alvarez") — the dot
#: breaks the Title-run, so this pass captures the trailing name directly. Names
#: after a dot-less honorific ("Dr Lee") are handled by the cue path instead.
_HONORIFIC_NAME = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sir|Aunt|Uncle|Grandma|Grandpa)\.\s+([A-Z][a-z]+)\b"
)


def _preceding_cue(text: str, pos: int) -> bool:
    """Whether the word immediately before ``pos`` is a name cue.

    Looks only at the last whitespace-delimited token before ``pos`` and does
    *not* reach across a period, so a sentence-ending word ("…emailed. Later")
    never cues the next sentence's first capitalized word (dotted honorifics like
    "Dr." are handled by :data:`_HONORIFIC_NAME` instead). Implemented with plain
    string ops rather than a trailing-anchored regex to avoid any super-linear
    backtracking on adversarial input (CodeQL ReDoS).
    """
    before = text[:pos].rstrip()
    if not before or before.endswith("."):
        return False
    return before.split()[-1].lower() in _NAME_CUES


def normalize_name(name: str) -> str:
    """Trim and collapse internal whitespace, preserving case (for display)."""
    return re.sub(r"\s+", " ", name or "").strip()


def name_key(name: str) -> str:
    """Case-insensitive matching key for a name (normalized + lowercased).

    Used for de-duplication and to match a fresh mention against the roster, so
    "sam" and "Sam" are one person and one pending queue entry.
    """
    return normalize_name(name).lower()


def extract_names(text: str) -> list[str]:
    """Extract candidate people-names from free text (deterministic, always runs).

    Three signals, unioned in first-seen order:

    1. **An honorific + name** ("Dr. Lee", "Mrs. Alvarez") — the dot would
       otherwise split the run, so it is matched directly.
    2. **A Title-Case run of two or three words** ("Dana Ruiz") — a strong person
       signal on its own. Leading cue/filler tokens the greedy run swept in are
       peeled first ("Ping Sam" → "Sam", "New York" → "York").
    3. **A lone Title-Case word** ("Sam") only when it carries corroborating
       evidence: a trailing possessive (``Sam's``), a peeled leading *cue*, or a
       preceding :data:`_NAME_CUES` word (``call Sam``, ``from Dana``). A bare
       capitalized word with none of these is dropped — it is far more likely a
       sentence start, a place, or a product ("New York" → "York" → dropped).

    Candidates that are purely generic are then discarded: a run whose every token
    is a :data:`_STOPWORDS` or :data:`_COMMON_WORDS` entry ("Weekly Report", "Order
    Confirmation"), or that contains an :data:`_ORG_MARKERS` token naming an
    organization ("United Airlines", "State University"). This is what keeps the
    review queue from filling with the capitalized noun-phrases that dominate real
    mail and calendar text — a real name survives because at least one of its
    tokens is neither generic nor an org marker. Beyond that the extractor still
    errs toward surfacing (a stray false positive is one dismiss in the queue), so
    the result is *candidates for review*, not confirmed people.

    Returns:
        Normalized names, de-duplicated case-insensitively, in order of first
        appearance.
    """
    text = text or ""
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        name = normalize_name(raw)
        if not name:
            return
        tokens = name_key(name).split()
        if not tokens:
            return
        # Drop a run that is purely generic (every token a stopword or common
        # non-name word — "Weekly Report", "Order Confirmation") or that names an
        # organization rather than a person ("United Airlines", "State University").
        # A real name keeps at least one token that is neither.
        if all(t in _STOPWORDS or t in _COMMON_WORDS for t in tokens):
            return
        if any(t in _ORG_MARKERS for t in tokens):
            return
        key = name_key(name)
        if key in seen:
            return
        seen.add(key)
        found.append(name)

    # Honorific + dotted name first ("Dr. Lee") — the dot would otherwise split
    # the Title-run into two lone words the cue path drops.
    for m in _HONORIFIC_NAME.finditer(text):
        _add(m.group(1))

    for m in _TITLE_RUN.finditer(text):
        run, possessive = m.group(1), m.group(2)
        tokens = run.split()
        # Peel off leading cue/filler tokens the greedy run swept in — a
        # sentence-initial verb ("Ping Sam", "Email Dana") or a filler ("New
        # York" → drop "New"). A stripped *cue* is itself evidence the remainder
        # is a name; a stripped stopword is not.
        stripped_cue = False
        while tokens and tokens[0].lower() in (_NAME_CUES | _STOPWORDS):
            if tokens[0].lower() in _NAME_CUES:
                stripped_cue = True
            tokens.pop(0)
        if not tokens:
            continue
        candidate = " ".join(tokens)
        if len(tokens) >= 2 or possessive or stripped_cue:
            _add(candidate)
            continue
        # A lone Title-Case word survives only with a preceding cue word ("call
        # **Sam**", "from **Dana**").
        if _preceding_cue(text, m.start()):
            _add(candidate)
    return found


_LLM_SYSTEM = (
    "You extract the personal names of PEOPLE mentioned in a short piece of text "
    "for a personal assistant. Return ONLY a JSON array of strings, no prose — each "
    "string one person's name as written. Include only real people (first name, "
    "full name, or an honorific + name like 'Dr. Lee'). EXCLUDE companies, products, "
    "places, days, months, and the reader themselves. Return [] if no person is named."
)


def extract_names_llm(text: str, client: Generator) -> list[str] | None:
    """One JSON call listing the people named in ``text``; ``None`` on any failure.

    Coerced/validated like the other model layers in the codebase: a
    :class:`ProviderError`, malformed JSON, or a non-list reply all yield ``None``
    so the caller keeps the heuristic result (local-first, never a dropped item).
    Only used to *augment* :func:`extract_names` on the on-demand path — the
    ingestion hot path stays heuristic-only to keep capture snappy.
    """
    if not (text or "").strip():
        return []
    try:
        reply = client.generate(f"Text:\n{text[:1500]}", system=_LLM_SYSTEM)
    except ProviderError:
        return None
    raw = extract_json(reply)
    if not isinstance(raw, list):
        return None
    names: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        name = normalize_name(item)
        key = name_key(name)
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def extract_candidate_names(text: str, *, client: Generator | None = None) -> list[str]:
    """Heuristic names, optionally augmented by the model, in first-seen order.

    The heuristic result always stands; when a ``client`` is supplied its findings
    are *added* (never subtracted), so the model can catch a name the rules missed
    without ever silencing one they found.
    """
    names = extract_names(text)
    if client is None:
        return names
    extra = extract_names_llm(text, client)
    if not extra:
        return names
    seen = {name_key(n) for n in names}
    for name in extra:
        if name_key(name) not in seen:
            seen.add(name_key(name))
            names.append(name)
    return names


def context_snippet(text: str) -> str:
    """A short, single-line context snippet for a mention (first ~200 chars)."""
    flat = re.sub(r"\s+", " ", text or "").strip()
    return flat[:_CONTEXT_CHARS]


# --- the read+write orchestrator --------------------------------------------


def enqueue_mentions(
    store: Any,
    *,
    text: str,
    source: str = "triage",
    ref: str | None = None,
    external_id: str | None = None,
    context: str | None = None,
    client: Generator | None = None,
) -> dict[str, Any]:
    """Extract names from ``text`` and reconcile them against the roster.

    For each candidate name:

    - **Known** (matches an active :class:`people` row by name or alias) — the
      person is *touched* (mention count + ``last_seen`` bumped): the learning
      signal that this person recurs. Nothing is queued.
    - **Unknown** — a **pending** mention is queued for the user to identify and
      categorize. The pending queue de-dupes on the normalized name, so a name
      that keeps appearing does not pile up.

    Best-effort and side-effect-only from the caller's view: it returns a small
    summary but the ingestion path ignores it. ``client`` (optional) augments the
    heuristic extraction with the model.

    Returns:
        ``{"names", "queued": [mention_id], "known": [person_id]}``.
    """
    names = extract_candidate_names(text, client=client)
    snippet = context if context is not None else context_snippet(text)
    queued: list[int] = []
    known: list[int] = []
    for name in names:
        person = store.find_person(name_key(name))
        if person is not None and person.get("status") == PERSON_ACTIVE:
            store.touch_person(int(person["id"]))
            known.append(int(person["id"]))
            continue
        mention_id = store.add_person_mention(
            name=name, source=source, context=snippet, ref=ref, external_id=external_id
        )
        if mention_id:
            queued.append(mention_id)
    return {"names": names, "queued": queued, "known": known}


def priority_boost(store: Any, text: str) -> int:
    """Extra todo-priority notches (0–2) when ``text`` names a known important person.

    A read-only scan: an action that names a high-importance person (importance 3)
    is bumped two notches, a high one (importance 2) by one, capped so the boost
    can never invent urgency out of thin air — it only nudges an item that already
    concerns someone who matters up the list. Unknown/low-importance names add
    nothing.
    """
    boost = 0
    for name in extract_names(text):
        person = store.find_person(name_key(name))
        if person is None or person.get("status") != PERSON_ACTIVE:
            continue
        importance = int(person.get("importance") or 0)
        if importance >= 3:
            boost = max(boost, 2)
        elif importance >= 2:
            boost = max(boost, 1)
    return boost


# --- surface helpers (pure) --------------------------------------------------


def describe_person(person: dict[str, Any]) -> str:
    """One line for a roster entry: ``"Dana Ruiz — coworker · high · seen 4×"``."""
    name = normalize_name(person.get("name") or "Someone")
    rel = person.get("relationship") or "unknown"
    importance = _importance_word(int(person.get("importance") or 0))
    count = int(person.get("mention_count") or 0)
    seen = f" · seen {count}×" if count else ""
    return f"{name} — {rel} · {importance}{seen}"


def describe_mention(mention: dict[str, Any]) -> str:
    """One line for a queued mention: ``"Sam — from mail: \"call with Sam …\""``."""
    name = normalize_name(mention.get("name") or "Someone")
    source = mention.get("source") or "capture"
    context = (mention.get("context") or "").strip()
    tail = f': "{context}"' if context else ""
    return f"{name} — from {source}{tail}"


def _importance_word(importance: int) -> str:
    """Map an importance integer to a short word for display."""
    return {0: "low", 1: "normal", 2: "high", 3: "top"}.get(importance, "normal")


def roster_profile_lines(people: list[dict[str, Any]]) -> list[str]:
    """Markdown bullet lines for the summarizer's "Key people" section.

    Only people who matter enough to steer prioritization (importance ≥ 2) are
    listed, most important then most-mentioned first, so the profile injected into
    every agent stays focused on the handful of people worth calibrating around
    rather than the whole address book.
    """
    notable = [
        p
        for p in people
        if p.get("status") == PERSON_ACTIVE and int(p.get("importance") or 0) >= 2
    ]
    notable.sort(
        key=lambda p: (int(p.get("importance") or 0), int(p.get("mention_count") or 0)),
        reverse=True,
    )
    lines: list[str] = []
    for person in notable:
        rel = person.get("relationship") or "unknown"
        importance = _importance_word(int(person.get("importance") or 0))
        note = (person.get("notes") or "").strip()
        detail = f" — {note}" if note else ""
        lines.append(
            f"- **{normalize_name(person.get('name') or '')}** "
            f"({rel}, {importance} importance){detail}"
        )
    return lines
