"""Natural-language editing of todos, commitments, and conflicts.

The dashboard chat box lets you say "bump the dentist call to urgent and drop the
dry-cleaning todo" and have it happen. This module is the brain behind that: it
turns a free-text message into a small, **validated** list of actions against the
scoped memory store, and executes them.

Design, in three deliberate layers:

1. **Snapshot.** :func:`build_snapshot` gives the model a compact view of the
   user's *current* open todos / upcoming commitments / dismissable conflicts —
   including their ids — so it can resolve "the dentist todo" to a real id
   instead of inventing one.
2. **Interpret + validate.** :func:`interpret` asks the model for a JSON action
   list; :func:`validate_actions` keeps only whitelisted ops with well-typed
   fields whose ids exist in the snapshot. Anything else is dropped with a
   human-readable reason. The model can neither run arbitrary operations nor act
   on hallucinated items.
3. **Execute.** :func:`execute_actions` calls the matching scoped store method.
   Because the store is already scoped to one user and re-checks ownership, an
   action can only ever touch the caller's own data — and a stale id that slipped
   through simply reports "nothing changed" rather than corrupting anything.

The model client is any object with ``generate(prompt, *, system=None) -> str``
(see :class:`Generator`), so the local Ollama client and the optional Claude
client are interchangeable — the endpoint prefers Claude when configured and
falls back to Ollama.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from prefrontal.clock import local_datetime, utcnow
from prefrontal.commitments import to_utc
from prefrontal.delegation import (
    HANDLER_AGENT,
    HANDLER_EMAIL,
    HANDLERS,
    STATUS_FAILED,
    run_delegation,
)
from prefrontal.household import normalize_chore, normalize_routine
from prefrontal.integrations import Generator
from prefrontal.llm_json import generate_json
from prefrontal.log import get_logger
from prefrontal.memory.repos.household import (
    AGREEMENT_KINDS,
    FACT_CATEGORIES,
    HOUSEHOLD_WIDE,
    normalize_fact_category,
    normalize_fact_item,
)
from prefrontal.sources import resolve_smtp_for
from prefrontal.todos import (
    ENERGY_LEVELS,
    MAX_ESTIMATE_MINUTES,
    MIN_ESTIMATE_MINUTES,
)

_log = get_logger(__name__)

#: Ops the assistant is allowed to emit. The whitelist *is* the security boundary
#: (with per-user store scoping): anything not here is refused before execution.
_PRIORITY_NAMES = {0: "low", 1: "normal", 2: "high", 3: "urgent"}

#: Bounds on an outing's "back in N minutes" window, in minutes — 1 minute to a
#: full day, wide enough for any real errand while rejecting a fat-fingered value.
MIN_OUTING_WINDOW_MINUTES = 1.0
MAX_OUTING_WINDOW_MINUTES = 24 * 60.0



class _ActionError(ValueError):
    """A single action failed validation; its message is shown to the user."""


@dataclass(frozen=True)
class ValidatedAction:
    """A whitelisted, type-checked action ready to execute.

    Attributes:
        op: One of :data:`ALLOWED_OPS`.
        params: Cleaned, in-range field values (no ``op`` key).
        summary: A one-line human description shown in the "Apply?" preview.
    """

    op: str
    params: dict[str, Any]
    summary: str

    def to_wire(self) -> dict[str, Any]:
        """Flat dict for the API/UI: ``{op, summary, **params}``."""
        return {"op": self.op, "summary": self.summary, **self.params}


@dataclass
class AssistantPlan:
    """The outcome of interpreting a message: a reply plus proposed actions.

    Attributes:
        reply: A short natural-language acknowledgement from the model.
        actions: Validated actions to preview and (on Apply) execute.
        errors: Human-readable reasons individual actions were dropped.
    """

    reply: str
    actions: list[ValidatedAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


ASSISTANT_SYSTEM = (
    "You are the editing assistant inside a personal ADHD executive-function "
    "dashboard. Turn the user's message into concrete edits to THEIR data. You "
    "are given a snapshot of their open todos, upcoming commitments, current and "
    "recent outings, and dismissable schedule conflicts, each with a numeric id "
    "(todos/commitments/outings) or key (conflicts). Resolve references like 'the "
    "dentist todo' or 'my current outing' to the matching id from the snapshot — "
    "NEVER invent an id that is not listed.\n\n"
    "Reply with ONLY a JSON object, no prose, no markdown fences:\n"
    '{"reply": "<one short sentence to the user>", "actions": [ ...actions ]}\n\n'
    "Your actions are only a PROPOSAL — nothing is saved until the user reviews "
    "them and taps Apply. So the reply must describe what you WILL do once applied "
    "(e.g. \"I'll bump the dentist call to urgent\"), and must NEVER claim it is "
    "already done (no \"Done\", no \"I've bumped …\").\n\n"
    "Each action is an object with an \"op\" and its fields. Allowed ops:\n"
    '- {"op":"add_todo","title":str,"estimate_minutes":int?,"priority":0-3?,'
    '"energy":"low"|"medium"|"high"?,"deadline":"YYYY-MM-DD"?}\n'
    '- {"op":"complete_todo","todo_id":int}\n'
    '- {"op":"drop_todo","todo_id":int}\n'
    '- {"op":"set_priority","todo_id":int,"priority":0-3}\n'
    '- {"op":"set_estimate","todo_id":int,"estimate_minutes":int}\n'
    '- {"op":"rename_todo","todo_id":int,"title":str}\n'
    '- {"op":"set_deadline","todo_id":int,"deadline":"YYYY-MM-DD" or null}\n'
    '- {"op":"set_todo_notes","todo_id":int,"notes":str or null}\n'
    '- {"op":"delegate_todo","todo_id":int,"handler":"agent"|"email",'
    '"destination":str?,"context":str?,"note":str?}\n'
    '- {"op":"add_commitment","title":str,"start_at":"YYYY-MM-DD HH:MM",'
    '"end_at":"YYYY-MM-DD HH:MM"?,"location":str?,"notes":str?}\n'
    '- {"op":"cancel_commitment","commitment_id":int}\n'
    '- {"op":"set_commitment_notes","commitment_id":int,"notes":str or null}\n'
    '- {"op":"set_commitment_hardness","commitment_id":int,"hardness":"hard"|"soft"}\n'
    '- {"op":"dismiss_conflict","key":str}\n'
    "A note is free-text detail attached to a todo or commitment (\"bring the "
    "insurance card\", \"needs the account number\"). It rides along with any "
    "nudge or reminder about that item, so use set_todo_notes / "
    "set_commitment_notes when the user wants to remember something *about* a "
    'task or event (notes:null clears it).\n'
    "Delegating hands a todo to an assistant to do the PREP/follow-up (it does NOT "
    "mark it done): use delegate_todo when the user says \"have the assistant "
    "handle/prep X\", \"get my VA on X\", \"draft the email for X\". "
    "handler:\"agent\" (the default) is the in-app AI — it writes a research brief "
    "and draft messages back onto the todo; handler:\"email\" sends that brief to a "
    "human assistant and so REQUIRES a destination email (use \"agent\" when the "
    "user names no address).\n"
    "A commitment's hardness is how firm it is: \"hard\" = a must-happen "
    "obligation (a slip is a real problem), \"soft\" = an elastic/optional block "
    "you could move. Use set_commitment_hardness for \"this one's non-negotiable\" "
    "or \"that's just a soft hold\".\n"
    "An outing is a trip out with a stated 'back in N minutes' window "
    "(time_window_minutes) that started at start_at. To START a new outing — the "
    "user is heading out (\"running to the store, back in 20\", \"walking the dog\") "
    "— use start_outing with the intention and time_window_minutes (your best "
    "estimate of how long they'll be out, in minutes; e.g. a coffee run ~15, "
    "groceries ~45). Include time_window_minutes whenever you can reasonably guess; "
    "omit it only if the errand gives no clue at all. It starts now unless the user "
    "says otherwise (then pass start_at):\n"
    '- {"op":"start_outing","intention":str,"time_window_minutes":number?,'
    '"start_at":"YYYY-MM-DD HH:MM"?}\n'
    "The rest work on an EXISTING outing (current or past) — resolve outing_id from "
    "the snapshot. You can fix what it was for, adjust the window (e.g. 'give me 15 "
    "more minutes' — add to the outing's current time_window_minutes from the "
    "snapshot), or correct when it started:\n"
    '- {"op":"rename_outing","outing_id":int,"intention":str}\n'
    '- {"op":"set_outing_window","outing_id":int,"time_window_minutes":number}\n'
    '- {"op":"set_outing_start","outing_id":int,"start_at":"YYYY-MM-DD HH:MM"}\n\n'
    "An if-then plan (implementation intention) pairs a CUE the user will run into "
    "with a tiny PRE-DECIDED action, and Prefrontal re-shows the action the moment "
    "the cue is detected — the most effective way to actually start a stalled task. "
    "Use add_if_then when the user says \"when/if X, (then) I'll Y\" (\"when I sit "
    "down at my desk after lunch, I'll open the tax form\", \"if I get to the gym, "
    "start with 10 pushups\"). cue_text is their trigger in their own words; "
    "action_text is the small action, kept AS THEY SAID IT (don't expand it — the "
    "technique needs it pre-committed and tiny). Give a cue the tick can detect: a "
    "\"place\" (a short location name like \"desk\", \"gym\", \"kitchen\"), a "
    "\"time_window\" local band \"HH:MM-HH:MM\" (\"after lunch\"≈\"12:30-14:00\", "
    "\"in the morning\"≈\"08:00-11:00\"), and/or an \"event\" — use "
    "\"arrive_home\" when the trigger is coming home (\"when I get home\", \"once "
    "I'm back\") and \"leave_home\" when it's heading out (\"when I leave the "
    "house\", \"on my way out\"). At least one of place/time_window/event is "
    "required; combine them when the cue is compound (\"at my desk after lunch\" = "
    "place+time_window; \"when I get home in the evening\" = event+time_window):\n"
    '- {"op":"add_if_then","cue_text":str,"action_text":str,"place":str?,'
    '"time_window":"HH:MM-HH:MM"?,"event":"arrive_home"|"leave_home"?}\n\n'
    "priority: 0 low, 1 normal, 2 high, 3 urgent.\n\n"
    "If (and ONLY if) the snapshot has a \"household\" object, these shared "
    "co-parent sheet ops are also available. Resolve a kid's name to an id from "
    "\"household.children\", or a pet's name (e.g. the dog's meds) to an id from "
    "\"household.pets\"; omit \"child\" (or use 0) for a household-wide fact. "
    "\"category\" MUST be one of \"household.fact_categories\":\n"
    '- {"op":"set_fact","category":str,"item":str,"value":str,"child":int?}\n'
    '- {"op":"clear_fact","category":str,"item":str,"child":int?}\n'
    '- {"op":"set_agreement","title":str,"body":str,'
    '"kind":"reward"|"consistency"|"routine"?,"child":int?,"structured":object?}\n'
    '- {"op":"remove_agreement","agreement_id":int}\n'
    "To assign a chore's owner or a routine's accountable person, resolve the "
    "adult's name to a user id from \"household.members\" (the co-parents — NOT "
    "\"household.children\"). Use accountable_id/owner_id:null to leave it "
    "unassigned (\"either parent\").\n"
    "A routine groups chores under an accountable owner and carries a schedule "
    "(days: 0=Mon…6=Sun, empty = every day; month_days: 1–31 for a day-of-month "
    "schedule that wins over days when set; due_time \"HH:MM\", blank = not "
    "time-tied). Create with set_routine; set accountable_id to name who holds it. "
    "To rename or change an existing one, use edit_routine with its id from "
    "\"household.routines\" — set_routine can't rename. Pass enabled:false to "
    "pause, true to resume:\n"
    '- {"op":"set_routine","title":str,"due_time":"HH:MM"?,"days":[0-6]?,'
    '"month_days":[1-31]?,"impact":str?,"enabled":bool?,"accountable_id":int?}\n'
    '- {"op":"edit_routine","routine_id":int,"title":str?,"due_time":"HH:MM"?,'
    '"days":[0-6]?,"month_days":[1-31]?,"impact":str?,"enabled":bool?,'
    '"accountable_id":int?}\n'
    '- {"op":"remove_routine","routine_id":int}\n'
    "A chore is one recurring shared task (whose miss lands on the other parent). "
    "It has an owner (the doer — owner_id, or null for either parent), its own "
    "schedule (days/month_days/due_time — blank due_time inherits its routine's), a "
    "reminder lead (remind_before, minutes before due), and may link under a routine "
    "(routine_id from \"household.routines\", or null to stand alone). Create with "
    "set_chore; to rename or change an existing one use edit_chore with its id from "
    "\"household.chores\" — set_chore can't rename. Pass enabled:false to pause. "
    "away_behavior controls what happens while the household is away (see below): "
    "\"keep\" (default — bills, meds still fire) or \"suppress\" (a location-bound "
    "chore like trash/mail/plants that can't be done from afar, so it's skipped). "
    "service links a chore to a municipal service (e.g. \"trash\", \"recycling\") "
    "whose pickup day can shift on a holiday week — set it for the trash/recycling "
    "reminder so a scraped shift moves that week's reminder (null = ordinary chore):\n"
    '- {"op":"set_chore","title":str,"due_time":"HH:MM"?,"days":[0-6]?,'
    '"month_days":[1-31]?,"remind_before":int?,"impact":str?,"enabled":bool?,'
    '"owner_id":int?,"routine_id":int?,"away_behavior":"keep"|"suppress"?,"service":str?}\n'
    '- {"op":"edit_chore","chore_id":int,"title":str?,"due_time":"HH:MM"?,'
    '"days":[0-6]?,"month_days":[1-31]?,"remind_before":int?,"impact":str?,'
    '"enabled":bool?,"owner_id":int?,"routine_id":int?,'
    '"away_behavior":"keep"|"suppress"?,"service":str?}\n'
    '- {"op":"remove_chore","chore_id":int}\n'
    "An \"away window\" marks the whole household as away (vacation / travel) over "
    "an inclusive local date range — while it's active, chores with "
    "away_behavior:\"suppress\" are skipped. Set it when the user says they'll be "
    "away; clear it when they're back or the plan changes. Dates are \"YYYY-MM-DD\"; "
    "note is a short optional reason (\"beach trip\"):\n"
    '- {"op":"set_away","starts_on":"YYYY-MM-DD","ends_on":"YYYY-MM-DD","note":str?}\n'
    '- {"op":"clear_away"}\n'
    "Distinct from that: a single member can mark *themselves* away (a work trip, a "
    "hospital stay) while the household keeps running — their chores then fall to "
    "the present co-parent, and they aren't nudged. Use set_member_away when the "
    "user says \"I'm away/travelling\" (just them, not the whole family); clear_member"
    "_away when they're back. This always applies to the current user:\n"
    '- {"op":"set_member_away","starts_on":"YYYY-MM-DD","ends_on":"YYYY-MM-DD","note":str?}\n'
    '- {"op":"clear_member_away"}\n'
    "Shared shopping list — add things to buy, check them off, or remove them. "
    "Put size/brand/quantity in \"spec\". For check_shopping/remove_shopping, "
    "resolve the item to an id from \"household.shopping\"; \"got\" defaults to "
    "true (a false un-checks it). Emit ONE add_shopping per distinct item:\n"
    '- {"op":"add_shopping","item":str,"spec":str?,"where_to_buy":str?,"child":int?}\n'
    '- {"op":"check_shopping","shopping_id":int,"got":bool?}\n'
    '- {"op":"remove_shopping","shopping_id":int}\n\n'
    "If the user asks for something "
    "you cannot express with these ops, leave actions empty and say so in reply. "
    "If nothing needs doing, return an empty actions list."
)


def build_snapshot(memory: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Build the compact current-state view handed to the model.

    Only the fields needed for reference-resolution are included, to keep the
    prompt small: open todos (id/title/priority/estimate/deadline/notes), upcoming
    commitments (id/title/start/location/notes/hardness), and dismissable
    possible-conflict keys. ``notes`` is carried so the model can reference or clear
    an existing note ("drop the note on the dentist"), and ``hardness`` so it can
    honor "leave that one soft". Ids come straight from the store, so the model can
    only target things that actually exist.

    Args:
        memory: A **scoped** store (one user).
        now: Injectable clock for tests; unused today but reserved for windowing.

    Returns:
        A JSON-serializable dict with ``todos``, ``commitments``, ``conflicts``.
    """
    todos = [
        {
            "id": t["id"],
            "title": t.get("title"),
            "priority": t.get("priority"),
            "estimate_minutes": t.get("estimate_minutes"),
            "deadline": t.get("deadline"),
            "notes": t.get("notes"),
        }
        for t in memory.open_todos()
    ]
    commitments = [
        {
            "id": c["id"],
            "title": c.get("title"),
            "start_at": c.get("start_at"),
            "location": c.get("location"),
            "notes": c.get("notes"),
            "hardness": c.get("hardness"),
        }
        for c in memory.upcoming_commitments(limit=25)
    ]
    # Current + recent outings (any status), so "my outing" resolves to the active
    # one and "yesterday's coffee run" to a closed one. status/start_at let the
    # model tell them apart; time_window_minutes anchors an "N more minutes" edit.
    outings = [
        {
            "id": o["id"],
            "intention": o.get("intention"),
            "time_window_minutes": o.get("time_window_minutes"),
            "status": o.get("status"),
            # Exposed as ``start_at`` (the row's ``departure_at``) to match the
            # set_outing_start op's field name and the commitment snapshot.
            "start_at": o.get("departure_at"),
        }
        for o in memory.recent_outings(limit=10)
    ]
    conflicts = _possible_conflicts(memory)
    snapshot = {
        "todos": todos,
        "commitments": commitments,
        "outings": outings,
        "conflicts": conflicts,
    }
    household = _household_snapshot(memory)
    if household is not None:
        snapshot["household"] = household
    return snapshot


def _household_snapshot(memory: Any) -> dict[str, Any] | None:
    """Household context for the shared sheet, or ``None`` if the user is in none.

    Gives the model the roster (so "Sam" resolves to a real ``child`` id), the
    adult ``members`` (so "make it Alex's job" resolves to a real owner/accountable
    user id), the controlled fact-category vocabulary, open agreements (so "remove
    the sticker plan" resolves to a real ``agreement_id``), the shopping list (so
    "check off the milk" resolves to a real shopping id), and the routines and
    chores (so "move the dishes to 8pm" resolves to a real ``chore_id``) — the same
    id-discipline as the todo/commitment snapshot. Omitted entirely for a user with
    no household, which is the signal the household-op validators key on.
    """
    hid = memory.household_id_or_none()
    if hid is None:
        return None
    return {
        "children": [
            {"id": c["id"], "name": c.get("name")} for c in memory.children()
        ],
        # The adult roster (co-parents) — the owner/accountable candidates. Kept
        # distinct from ``children``: a chore's owner or a routine's accountable
        # holder is a member, never a kid.
        "members": [
            {"id": m["id"], "name": m.get("display_name") or m.get("handle")}
            for m in memory.household_members(hid)
            if m.get("status") == "active"
        ],
        "pets": [
            {"id": p["id"], "name": p.get("name"), "species": p.get("species")}
            for p in memory.pets()
        ],
        "fact_categories": list(FACT_CATEGORIES),
        "agreements": [
            {"id": a["id"], "title": a.get("title"), "child_id": a.get("child_id")}
            for a in memory.agreements()
        ],
        "shopping": [
            {"id": s["id"], "item": s.get("item"), "got": bool(s.get("got"))}
            for s in memory.shopping_items()
        ],
        "routines": [
            {"id": r["id"], "title": r.get("title"), "enabled": bool(r.get("enabled"))}
            for r in memory.routines()
        ],
        "chores": [
            {
                "id": ch["id"],
                "title": ch.get("title"),
                "enabled": bool(ch.get("enabled")),
                "owner_id": ch.get("owner_id"),
                "routine_id": ch.get("routine_id"),
                "away_behavior": ch.get("away_behavior") or "keep",
                "service": ch.get("service"),
            }
            for ch in memory.chores()
        ],
        # The current "we're away" window (vacation / travel), or null if not away —
        # so the assistant can report it, avoid re-setting, or clear it on return.
        "away_window": memory.away_window(),
        # The *current user's* own away status (just them, not the household) — so
        # the assistant can report/clear it and tell the two windows apart.
        "my_away_window": memory.member_away_window(),
    }


def _possible_conflicts(memory: Any) -> list[dict[str, Any]]:
    """Return dismissable possible-conflict ``{key, label}`` pairs.

    Mirrors the ``GET /commitments/conflicts`` endpoint: find overlaps, keep the
    *possible* (soft, placeholder-vs-real) ones the user hasn't dismissed, and
    label each with a stable dismissal key. So "dismiss the 2pm overlap" resolves
    to a real key the model can emit.
    """
    from prefrontal.commitments import (
        conflict_dismissal_key,
        find_conflicts,
        partition_conflicts,
    )

    _hard, possible = partition_conflicts(
        find_conflicts(memory.upcoming_commitments()), memory.dismissed_conflicts()
    )
    return [
        {
            "key": conflict_dismissal_key(c),
            "label": f"{c.a.get('title', '?')} vs {c.b.get('title', '?')}",
        }
        for c in possible
    ]


def interpret(
    message: str,
    snapshot: dict[str, Any],
    *,
    client: Generator,
    now: datetime | None = None,
    tz: str = "UTC",
) -> tuple[str, list[dict[str, Any]]]:
    """Ask the model to turn ``message`` into a reply + raw action list.

    Args:
        message: The user's chat message.
        snapshot: Output of :func:`build_snapshot`.
        client: A model client (Ollama or Anthropic).
        now: Current instant as naive UTC (defaults to :func:`utcnow`). Anchors
            the model's resolution of relative dates/times ("tomorrow", "in an
            hour", "next Tuesday").
        tz: The user's IANA timezone. The model is told the current *local* time
            and instructed to emit local wall-clock times — without it the model
            has no "now" and no zone, so it guesses a date and often emits a
            UTC-assumed time, which is how a "3pm" edit lands hours off.

    Returns:
        ``(reply, raw_actions)`` — ``raw_actions`` is a list of unvalidated
        dicts. Returns ``("", [])`` if the model is unreachable or its reply
        can't be parsed as JSON, so the caller degrades gracefully.
    """
    now_local = local_datetime(now or utcnow(), tz)
    when = (
        f"Right now it is {now_local:%A %Y-%m-%d %H:%M} local time ({tz}). "
        "Resolve every relative date/time the user gives (\"today\", \"tomorrow\", "
        "\"tonight\", \"next Tuesday\", \"in 2 hours\") against this. Emit all "
        "start_at/end_at/deadline values as the user's LOCAL wall-clock time in "
        "the required format — never append a 'Z' or a UTC offset; the server "
        "converts to UTC."
    )
    prompt = (
        f"{when}\n\n"
        f"Current state (JSON):\n{json.dumps(snapshot, default=str)}\n\n"
        f"User message: {message}"
    )
    parsed = generate_json(prompt, system=ASSISTANT_SYSTEM, client=client)
    if isinstance(parsed, list):
        return "", [a for a in parsed if isinstance(a, dict)]
    if isinstance(parsed, dict):
        reply = parsed.get("reply")
        actions = parsed.get("actions")
        reply = reply if isinstance(reply, str) else ""
        actions = [a for a in actions if isinstance(a, dict)] if isinstance(actions, list) else []
        return reply, actions
    return "", []


# --- validation -----------------------------------------------------------


def _as_int(value: Any) -> int | None:
    """Coerce to int, rejecting bools and non-integers. ``None`` on failure."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_priority(value: Any) -> int:
    """Validate a 0–3 priority, raising :class:`_ActionError` otherwise."""
    pri = _as_int(value)
    if pri is None or not 0 <= pri <= 3:
        raise _ActionError("priority must be an integer 0–3")
    return pri


def _as_estimate(value: Any) -> float:
    """Validate an in-range minute estimate."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _ActionError("estimate_minutes must be a number")
    try:
        est = float(value)
    except (TypeError, ValueError):
        raise _ActionError("estimate_minutes must be a number") from None
    if not MIN_ESTIMATE_MINUTES <= est <= MAX_ESTIMATE_MINUTES:
        raise _ActionError(
            f"estimate_minutes must be {MIN_ESTIMATE_MINUTES:g}–{MAX_ESTIMATE_MINUTES:g}"
        )
    return est


def _nonblank(value: Any, field: str) -> str:
    """Validate a non-blank string field, naming it in the error message."""
    if not isinstance(value, str) or not value.strip():
        raise _ActionError(f"{field} must be a non-empty string")
    return value.strip()


def _as_title(value: Any) -> str:
    """Validate a non-blank title string."""
    return _nonblank(value, "title")


def _require_todo(action: dict[str, Any], snapshot: dict[str, Any]) -> tuple[int, str]:
    """Resolve ``todo_id`` against the snapshot, returning ``(id, title)``."""
    tid = _as_int(action.get("todo_id"))
    if tid is None:
        raise _ActionError("todo_id must be an integer")
    for t in snapshot.get("todos", []):
        if t.get("id") == tid:
            return tid, (t.get("title") or f"todo #{tid}")
    raise _ActionError(f"no open todo with id {tid}")


def _require_commitment(action: dict[str, Any], snapshot: dict[str, Any]) -> tuple[int, str]:
    """Resolve ``commitment_id`` against the snapshot, returning ``(id, title)``."""
    cid = _as_int(action.get("commitment_id"))
    if cid is None:
        raise _ActionError("commitment_id must be an integer")
    for c in snapshot.get("commitments", []):
        if c.get("id") == cid:
            return cid, (c.get("title") or f"commitment #{cid}")
    raise _ActionError(f"no upcoming commitment with id {cid}")


def _require_outing(action: dict[str, Any], snapshot: dict[str, Any]) -> tuple[int, str]:
    """Resolve ``outing_id`` against the snapshot, returning ``(id, intention)``."""
    oid = _as_int(action.get("outing_id"))
    if oid is None:
        raise _ActionError("outing_id must be an integer")
    for o in snapshot.get("outings", []):
        if o.get("id") == oid:
            return oid, (o.get("intention") or f"outing #{oid}")
    raise _ActionError(f"no outing with id {oid}")


def _as_outing_window(value: Any) -> float:
    """Validate an in-range outing window in minutes."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _ActionError("time_window_minutes must be a number")
    try:
        mins = float(value)
    except (TypeError, ValueError):
        raise _ActionError("time_window_minutes must be a number") from None
    if not MIN_OUTING_WINDOW_MINUTES <= mins <= MAX_OUTING_WINDOW_MINUTES:
        raise _ActionError(
            "time_window_minutes must be "
            f"{MIN_OUTING_WINDOW_MINUTES:g}–{MAX_OUTING_WINDOW_MINUTES:g}"
        )
    return mins


def _require_household(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the snapshot's household context, or reject (caller isn't a co-parent)."""
    household = snapshot.get("household")
    if not isinstance(household, dict):
        raise _ActionError("you're not set up in a household")
    return household


def _resolve_child(action: dict[str, Any], household: dict[str, Any]) -> tuple[int, str]:
    """Resolve an optional ``child`` to ``(child_id, label)``.

    A missing/zero ``child`` means household-wide (:data:`HOUSEHOLD_WIDE`). A given
    id must match a roster member — a kid *or* a pet — in the snapshot, so the
    model can't attach a fact (e.g. the dog's meds) to a member who doesn't exist.

    Accepts the model's ``child`` key *or* the ``child_id`` key that
    :meth:`ValidatedAction.to_wire` emits — the ``/assistant`` → preview →
    ``/assistant/apply`` round-trip echoes the wire action back verbatim, so
    re-validation must read the same key it wrote. (``_require_todo`` /
    ``_require_commitment`` already read their emitted ``*_id`` keys; child
    resolution was the lone asymmetry, which silently reassigned every per-child
    fact/agreement to the household on apply.)
    """
    raw = action.get("child", action.get("child_id"))
    if raw is None:
        return HOUSEHOLD_WIDE, "the household"
    cid = _as_int(raw)
    if cid is None:
        raise _ActionError("child must be a child id from the snapshot")
    if cid == HOUSEHOLD_WIDE:
        return HOUSEHOLD_WIDE, "the household"
    for m in [*household.get("children", []), *household.get("pets", [])]:
        if m.get("id") == cid:
            return cid, (m.get("name") or f"#{cid}")
    raise _ActionError(f"no child or pet with id {cid}")


def _require_agreement(
    action: dict[str, Any], household: dict[str, Any]
) -> tuple[int, str]:
    """Resolve ``agreement_id`` against the snapshot, returning ``(id, title)``."""
    aid = _as_int(action.get("agreement_id"))
    if aid is None:
        raise _ActionError("agreement_id must be an integer")
    for a in household.get("agreements", []):
        if a.get("id") == aid:
            return aid, (a.get("title") or f"agreement #{aid}")
    raise _ActionError(f"no agreement with id {aid}")


def _require_shopping(
    action: dict[str, Any], household: dict[str, Any]
) -> tuple[int, str]:
    """Resolve ``shopping_id`` against the snapshot, returning ``(id, item)``."""
    sid = _as_int(action.get("shopping_id"))
    if sid is None:
        raise _ActionError("shopping_id must be an integer")
    for s in household.get("shopping", []):
        if s.get("id") == sid:
            return sid, (s.get("item") or f"item #{sid}")
    raise _ActionError(f"no shopping item with id {sid}")


def _require_routine(
    action: dict[str, Any], household: dict[str, Any]
) -> tuple[int, str]:
    """Resolve ``routine_id`` against the snapshot, returning ``(id, title)``."""
    rid = _as_int(action.get("routine_id"))
    if rid is None:
        raise _ActionError("routine_id must be an integer")
    for r in household.get("routines", []):
        if r.get("id") == rid:
            return rid, (r.get("title") or f"routine #{rid}")
    raise _ActionError(f"no routine with id {rid}")


def _require_chore(
    action: dict[str, Any], household: dict[str, Any]
) -> tuple[int, str]:
    """Resolve ``chore_id`` against the snapshot, returning ``(id, title)``."""
    cid = _as_int(action.get("chore_id"))
    if cid is None:
        raise _ActionError("chore_id must be an integer")
    for c in household.get("chores", []):
        if c.get("id") == cid:
            return cid, (c.get("title") or f"chore #{cid}")
    raise _ActionError(f"no chore with id {cid}")


def _resolve_link(
    action: dict[str, Any], household: dict[str, Any], key: str, listing: str
) -> int | None:
    """Resolve an optional cross-reference id (``owner_id``/``accountable_id``/
    ``routine_id``) against a snapshot listing, so the assistant can only point at
    real household members/routines — the same id-discipline as everything else.

    Returns the validated int, or ``None`` when the field is absent or explicitly
    null (the "unassign / either parent" signal). Raises :class:`_ActionError` for
    a non-int or an id not present in ``household[listing]``.
    """
    if key not in action or action.get(key) is None:
        return None
    rid = _as_int(action.get(key))
    if rid is None:
        raise _ActionError(f"{key} must be an integer id, or null")
    if any(row.get("id") == rid for row in household.get(listing, [])):
        return rid
    raise _ActionError(f"{key} {rid} is not in this household's {listing}")


def _name_of(household: dict[str, Any], listing: str, uid: int | None) -> str:
    """Display name/title for an id in a snapshot listing, for an action summary."""
    for row in household.get(listing, []):
        if row.get("id") == uid:
            return row.get("name") or row.get("title") or f"#{uid}"
    return f"#{uid}"


def _as_fact_category(value: Any) -> str:
    """Validate a fact category against the controlled vocab."""
    cat = normalize_fact_category(value if isinstance(value, str) else None)
    if cat is None:
        raise _ActionError(
            "category must be one of " + ", ".join(FACT_CATEGORIES)
        )
    return cat


def _as_structured(value: Any) -> str | None:
    """Validate an optional ``structured`` payload, returning a JSON string or None.

    Accepts an object (serialized) or an already-serialized JSON string; anything
    that isn't valid JSON is rejected rather than stored as junk the render can't
    parse.
    """
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, str):
        try:
            json.loads(value)
        except ValueError:
            raise _ActionError("structured must be valid JSON") from None
        return value
    raise _ActionError("structured must be a JSON object")


# --- per-op validators ------------------------------------------------------
#
# Each validator takes (op, action, snapshot) and returns a ValidatedAction or
# raises _ActionError. They're registered in _VALIDATORS below; _validate_one is
# just a lookup + dispatch, and ALLOWED_OPS is derived from the registry keys so
# the whitelist (the security boundary) can never drift from what's implemented.
# A validator handling more than one op branches on the passed-in ``op``.


def _v_add_todo(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    title = _as_title(action.get("title"))
    params: dict[str, Any] = {"title": title}
    extras = []
    if action.get("estimate_minutes") is not None:
        params["estimate_minutes"] = _as_estimate(action["estimate_minutes"])
        extras.append(f"{params['estimate_minutes']:g}m")
    if action.get("priority") is not None:
        params["priority"] = _as_priority(action["priority"])
        extras.append(_PRIORITY_NAMES[params["priority"]])
    if action.get("energy") is not None:
        energy = str(action["energy"]).lower()
        if energy not in ENERGY_LEVELS:
            raise _ActionError("energy must be low|medium|high")
        params["energy"] = energy
    if action.get("deadline") is not None:
        params["deadline"] = _nonblank(action.get("deadline"), "deadline")
        extras.append(f"by {params['deadline']}")
    detail = f" ({', '.join(extras)})" if extras else ""
    return ValidatedAction(op, params, f"Add todo: “{title}”{detail}")


def _v_todo_status(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    tid, title = _require_todo(action, snapshot)
    verb = "Complete" if op == "complete_todo" else "Drop"
    return ValidatedAction(op, {"todo_id": tid}, f"{verb} todo: “{title}”")


def _v_delegate_todo(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    tid, title = _require_todo(action, snapshot)
    handler = str(action.get("handler") or HANDLER_AGENT).strip().lower()
    if handler not in HANDLERS:
        raise _ActionError('handler must be "agent" or "email"')
    params: dict[str, Any] = {"todo_id": tid, "handler": handler}
    context = str(action.get("context") or "").strip()
    if context:
        params["context"] = context
    if handler == HANDLER_EMAIL:
        # An email hand-off has to know who to send to — reject it here rather than
        # letting it fall through to a "failed" delegation at execute time.
        dest = _nonblank(action.get("destination"), "destination")
        params["destination"] = dest
        note = str(action.get("note") or "").strip()
        if note:
            params["note"] = note
        summary = f"Delegate “{title}” to your assistant ({dest})"
    else:
        summary = f"Delegate “{title}” to the AI assistant to prep"
    return ValidatedAction(op, params, summary)


def _v_set_priority(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    tid, title = _require_todo(action, snapshot)
    pri = _as_priority(action.get("priority"))
    return ValidatedAction(
        op, {"todo_id": tid, "priority": pri},
        f"Set “{title}” priority to {_PRIORITY_NAMES[pri]}",
    )


def _v_set_estimate(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    tid, title = _require_todo(action, snapshot)
    est = _as_estimate(action.get("estimate_minutes"))
    return ValidatedAction(
        op, {"todo_id": tid, "estimate_minutes": est},
        f"Set “{title}” estimate to {est:g}m",
    )


def _v_rename_todo(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    tid, title = _require_todo(action, snapshot)
    new_title = _as_title(action.get("title"))
    return ValidatedAction(
        op, {"todo_id": tid, "title": new_title},
        f"Rename “{title}” → “{new_title}”",
    )


def _v_set_deadline(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    tid, title = _require_todo(action, snapshot)
    raw_deadline = action.get("deadline")
    if raw_deadline is None:
        return ValidatedAction(
            op, {"todo_id": tid, "deadline": None}, f"Clear deadline on “{title}”"
        )
    deadline = _nonblank(raw_deadline, "deadline")
    return ValidatedAction(
        op, {"todo_id": tid, "deadline": deadline},
        f"Set “{title}” deadline to {deadline}",
    )


def _note_or_none(value: Any) -> str | None:
    """Coerce a model-supplied note to trimmed text, or ``None`` to clear it.

    A blank/whitespace-only value clears the note (same as an explicit ``null``),
    so "remove the note on the dentist" and an empty string both land as ``None``.
    """
    if value is None:
        return None
    if not isinstance(value, (str, int, float)):
        raise _ActionError("notes must be text")
    text = str(value).strip()
    return text or None


def _v_set_todo_notes(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    tid, title = _require_todo(action, snapshot)
    notes = _note_or_none(action.get("notes"))
    if notes is None:
        return ValidatedAction(
            op, {"todo_id": tid, "notes": None}, f"Clear note on “{title}”"
        )
    return ValidatedAction(
        op, {"todo_id": tid, "notes": notes}, f"Note on “{title}”: {notes}"
    )


def _v_set_commitment_notes(
    op: str, action: dict[str, Any], snapshot: dict[str, Any]
) -> ValidatedAction:
    cid, title = _require_commitment(action, snapshot)
    notes = _note_or_none(action.get("notes"))
    if notes is None:
        return ValidatedAction(
            op, {"commitment_id": cid, "notes": None}, f"Clear note on “{title}”"
        )
    return ValidatedAction(
        op, {"commitment_id": cid, "notes": notes}, f"Note on “{title}”: {notes}"
    )


def _v_set_commitment_hardness(
    op: str, action: dict[str, Any], snapshot: dict[str, Any]
) -> ValidatedAction:
    cid, title = _require_commitment(action, snapshot)
    raw = action.get("hardness")
    hardness = str(raw).strip().lower() if raw is not None else ""
    if hardness not in ("hard", "soft"):
        raise _ActionError('hardness must be "hard" or "soft"')
    return ValidatedAction(
        op, {"commitment_id": cid, "hardness": hardness},
        f"Mark “{title}” {hardness}",
    )


def _v_add_commitment(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    title = _as_title(action.get("title"))
    start_at = _nonblank(action.get("start_at"), "start_at")  # parsed at execute
    params: dict[str, Any] = {"title": title, "start_at": start_at}
    if action.get("end_at") is not None:
        params["end_at"] = _nonblank(action.get("end_at"), "end_at")
    if action.get("location") is not None and str(action.get("location")).strip():
        params["location"] = str(action["location"]).strip()
    notes = _note_or_none(action.get("notes"))
    if notes is not None:
        params["notes"] = notes
    return ValidatedAction(op, params, f"Add commitment: “{title}” at {start_at}")


def _v_cancel_commitment(
    op: str, action: dict[str, Any], snapshot: dict[str, Any]
) -> ValidatedAction:
    cid, title = _require_commitment(action, snapshot)
    return ValidatedAction(op, {"commitment_id": cid}, f"Cancel commitment: “{title}”")


def _v_dismiss_conflict(
    op: str, action: dict[str, Any], snapshot: dict[str, Any]
) -> ValidatedAction:
    key = action.get("key")
    keys = {c.get("key"): c.get("label") for c in snapshot.get("conflicts", [])}
    if not isinstance(key, str) or key not in keys:
        raise _ActionError("no dismissable conflict with that key")
    label = keys[key] or key
    return ValidatedAction(op, {"key": key}, f"Dismiss conflict: {label}")


def _v_start_outing(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    intention = _nonblank(action.get("intention"), "intention")
    raw = action.get("time_window_minutes")
    if raw is not None:
        minutes = _as_outing_window(raw)
    else:
        # No explicit window: parse one from the intention ("back in 20"), else
        # fall back to a sane default — same order the /outing/start endpoint uses,
        # minus the LLM inference (this assistant is already the model, and its
        # prompt asks it to supply time_window_minutes when it can).
        from prefrontal.modules.location_anchor import (
            DEFAULT_INFERRED_WINDOW_MINUTES,
            parse_time_window,
        )
        minutes = parse_time_window(intention) or DEFAULT_INFERRED_WINDOW_MINUTES
    params: dict[str, Any] = {"intention": intention, "time_window_minutes": minutes}
    if action.get("start_at") is not None:
        params["start_at"] = _nonblank(action.get("start_at"), "start_at")  # parsed at execute
    return ValidatedAction(
        op, params, f"Start outing: “{intention}” (back in {minutes:g}m)"
    )


def _v_rename_outing(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    oid, intention = _require_outing(action, snapshot)
    new_intention = _nonblank(action.get("intention"), "intention")
    return ValidatedAction(
        op, {"outing_id": oid, "intention": new_intention},
        f"Rename outing “{intention}” → “{new_intention}”",
    )


def _v_set_outing_window(
    op: str, action: dict[str, Any], snapshot: dict[str, Any]
) -> ValidatedAction:
    oid, intention = _require_outing(action, snapshot)
    minutes = _as_outing_window(action.get("time_window_minutes"))
    return ValidatedAction(
        op, {"outing_id": oid, "time_window_minutes": minutes},
        f"Set “{intention}” window to {minutes:g}m",
    )


def _v_set_outing_start(
    op: str, action: dict[str, Any], snapshot: dict[str, Any]
) -> ValidatedAction:
    oid, intention = _require_outing(action, snapshot)
    start_at = _nonblank(action.get("start_at"), "start_at")  # parsed at execute
    return ValidatedAction(
        op, {"outing_id": oid, "start_at": start_at},
        f"Set “{intention}” start to {start_at}",
    )


def _v_add_if_then(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    """Validate an if-then plan capture — cue phrasing + tiny action + a detectable cue.

    ``action_text`` is stored as stated (the implementation-intention technique
    depends on the action being pre-committed and tiny — we don't rewrite it here).
    A plan needs a cue the coaching tick can actually detect, so at least one of
    ``place`` (normalized to a curated-place match key, the same way places are
    stored), ``time_window`` (a valid ``"HH:MM-HH:MM"`` band), or ``event`` (a
    transition in :data:`~prefrontal.modules.implementation_intention.CUE_EVENTS`)
    is required — a plan with none could never fire.
    """
    from prefrontal.geocode import normalize_query
    from prefrontal.modules.implementation_intention import CUE_EVENTS
    from prefrontal.scheduling import parse_window

    cue_text = _nonblank(action.get("cue_text"), "cue_text")
    action_text = _nonblank(action.get("action_text"), "action_text")
    params: dict[str, Any] = {"cue_text": cue_text, "action_text": action_text}
    place = action.get("place")
    if place is not None and str(place).strip():
        normalized = normalize_query(str(place))
        if normalized:
            params["cue_place"] = normalized
    window = action.get("time_window")
    if window is not None and str(window).strip():
        if parse_window(str(window)) is None:
            raise _ActionError('time_window must be a "HH:MM-HH:MM" band')
        params["cue_window"] = str(window).strip()
    event = action.get("event")
    if event is not None and str(event).strip():
        normalized_event = str(event).strip().lower()
        if normalized_event not in CUE_EVENTS:
            raise _ActionError(f"event must be one of {', '.join(CUE_EVENTS)}")
        params["cue_event"] = normalized_event
    if not ({"cue_place", "cue_window", "cue_event"} & params.keys()):
        raise _ActionError("an if-then plan needs a place, time window, or event as its cue")
    return ValidatedAction(
        op, params, f"Add if-then plan: when {cue_text}, then {action_text}"
    )


def _v_fact(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    household = _require_household(snapshot)
    child_id, who = _resolve_child(action, household)
    category = _as_fact_category(action.get("category"))
    item = normalize_fact_item(action.get("item"))
    if not item:
        raise _ActionError("item must be a non-empty string")
    params: dict[str, Any] = {"child_id": child_id, "category": category, "item": item}
    if op == "clear_fact":
        return ValidatedAction(op, params, f"Clear {who}'s {item}")
    value = action.get("value")
    if value is not None and not isinstance(value, (str, int, float)):
        raise _ActionError("value must be text")
    params["value"] = str(value).strip() if value is not None else None
    shown = params["value"] or "—"
    return ValidatedAction(op, params, f"Set {who}'s {item} → {shown}")


def _v_set_agreement(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    household = _require_household(snapshot)
    child_id, who = _resolve_child(action, household)
    title = _as_title(action.get("title"))
    params: dict[str, Any] = {"child_id": child_id, "title": title}
    kind = action.get("kind")
    if kind is not None:
        if str(kind).lower() not in AGREEMENT_KINDS:
            raise _ActionError("kind must be reward|consistency|routine")
        params["kind"] = str(kind).lower()
    if action.get("body") is not None:
        params["body"] = _nonblank(action.get("body"), "body")
    params["structured"] = _as_structured(action.get("structured"))
    return ValidatedAction(op, params, f"Set plan “{title}” for {who}")


def _v_remove_agreement(
    op: str, action: dict[str, Any], snapshot: dict[str, Any]
) -> ValidatedAction:
    household = _require_household(snapshot)
    aid, title = _require_agreement(action, household)
    return ValidatedAction(op, {"agreement_id": aid}, f"Remove plan: “{title}”")


def _v_add_shopping(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    household = _require_household(snapshot)
    child_id, _who = _resolve_child(action, household)
    item = _as_title(action.get("item"))
    params: dict[str, Any] = {"item": item, "child_id": child_id}
    extras = []
    if action.get("spec") is not None and str(action.get("spec")).strip():
        params["spec"] = str(action["spec"]).strip()
        extras.append(params["spec"])
    if action.get("where_to_buy") is not None and str(action.get("where_to_buy")).strip():
        params["where_to_buy"] = str(action["where_to_buy"]).strip()
        extras.append(f"@ {params['where_to_buy']}")
    detail = f" ({', '.join(extras)})" if extras else ""
    return ValidatedAction(op, params, f"Add to shopping: “{item}”{detail}")


def _v_check_shopping(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    household = _require_household(snapshot)
    sid, item = _require_shopping(action, household)
    got = action.get("got")
    got = True if got is None else bool(got)
    verb = "Check off" if got else "Un-check"
    return ValidatedAction(op, {"shopping_id": sid, "got": got}, f"{verb}: “{item}”")


def _v_remove_shopping(
    op: str, action: dict[str, Any], snapshot: dict[str, Any]
) -> ValidatedAction:
    household = _require_household(snapshot)
    sid, item = _require_shopping(action, household)
    return ValidatedAction(op, {"shopping_id": sid}, f"Remove from shopping: “{item}”")


def _v_set_routine(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    household = _require_household(snapshot)
    clean, error = normalize_routine(action)
    if error is not None:
        raise _ActionError(error)
    title = clean["title"]
    # set_routine is title-keyed, so re-using an existing name overwrites that
    # routine (and would reset its accountable owner). Point the model at
    # edit_routine (by id) instead of silently clobbering.
    for r in household.get("routines", []):
        if (r.get("title") or "").strip().lower() == title.lower():
            raise _ActionError(
                f"a routine named “{title}” already exists — "
                "edit it with edit_routine and its routine_id"
            )
    # Who's accountable resolves against the adult roster in the snapshot (a real
    # member id, or unassigned when omitted). `normalize_routine` already accepts
    # accountable_id, but re-resolve it here so a stale/non-member id is refused.
    clean["accountable_id"] = _resolve_link(action, household, "accountable_id", "members")
    detail = f"Add routine “{title}”"
    if clean["accountable_id"] is not None:
        detail += f", accountable: {_name_of(household, 'members', clean['accountable_id'])}"
    return ValidatedAction(op, clean, detail)


def _v_edit_routine(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    household = _require_household(snapshot)
    rid, cur_title = _require_routine(action, household)
    params: dict[str, Any] = {"routine_id": rid}
    changes: list[str] = []
    if action.get("title") is not None:
        params["title"] = _as_title(action.get("title"))
        changes.append(f"rename to “{params['title']}”")
    if "due_time" in action:
        params["due_time"] = action.get("due_time")
        changes.append("change time")
    if "days" in action:
        params["days"] = action.get("days")
        changes.append("change days")
    if "month_days" in action:
        params["month_days"] = action.get("month_days")
        changes.append("change days of month")
    if action.get("impact") is not None:
        params["impact"] = action.get("impact")
        changes.append("update why-it-matters")
    if "enabled" in action:
        params["enabled"] = bool(action.get("enabled"))
        changes.append("resume" if params["enabled"] else "pause")
    if "accountable_id" in action:
        aid = _resolve_link(action, household, "accountable_id", "members")
        params["accountable_id"] = aid
        changes.append(
            f"accountable → {_name_of(household, 'members', aid)}" if aid is not None
            else "clear accountable owner"
        )
    if len(params) == 1:  # only routine_id — nothing to do
        raise _ActionError("edit_routine needs at least one field to change")
    return ValidatedAction(op, params, f"Edit routine “{cur_title}”: {', '.join(changes)}")


def _v_remove_routine(
    op: str, action: dict[str, Any], snapshot: dict[str, Any]
) -> ValidatedAction:
    household = _require_household(snapshot)
    rid, title = _require_routine(action, household)
    return ValidatedAction(op, {"routine_id": rid}, f"Remove routine: “{title}”")


def _v_set_chore(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    household = _require_household(snapshot)
    clean, error = normalize_chore(action)
    if error is not None:
        raise _ActionError(error)
    title = clean["title"]
    # set_chore is title-keyed, so re-using an existing name overwrites that chore.
    # Point the model at edit_chore (by id) instead of silently clobbering.
    for c in household.get("chores", []):
        if (c.get("title") or "").strip().lower() == title.lower():
            raise _ActionError(
                f"a chore named “{title}” already exists — "
                "edit it with edit_chore and its chore_id"
            )
    # owner (the "responsible" doer) and routine link resolve against the snapshot.
    clean["owner_id"] = _resolve_link(action, household, "owner_id", "members")
    clean["routine_id"] = _resolve_link(action, household, "routine_id", "routines")
    detail = f"Add chore “{title}”"
    if clean["owner_id"] is not None:
        detail += f", owner: {_name_of(household, 'members', clean['owner_id'])}"
    if clean["routine_id"] is not None:
        detail += f", under {_name_of(household, 'routines', clean['routine_id'])}"
    return ValidatedAction(op, clean, detail)


def _v_edit_chore(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    household = _require_household(snapshot)
    cid, cur_title = _require_chore(action, household)
    params: dict[str, Any] = {"chore_id": cid}
    changes: list[str] = []
    if action.get("title") is not None:
        params["title"] = _as_title(action.get("title"))
        changes.append(f"rename to “{params['title']}”")
    if "due_time" in action:
        params["due_time"] = action.get("due_time")
        changes.append("change time")
    if "days" in action:
        params["days"] = action.get("days")
        changes.append("change days")
    if "month_days" in action:
        params["month_days"] = action.get("month_days")
        changes.append("change days of month")
    if "remind_before" in action:
        params["remind_before"] = action.get("remind_before")
        changes.append("change reminder lead")
    if action.get("impact") is not None:
        params["impact"] = action.get("impact")
        changes.append("update why-it-matters")
    if "enabled" in action:
        params["enabled"] = bool(action.get("enabled"))
        changes.append("resume" if params["enabled"] else "pause")
    if "away_behavior" in action:
        # Re-normalized against AWAY_BEHAVIORS by the handler's normalize_chore.
        params["away_behavior"] = action.get("away_behavior")
        changes.append(f"while away → {params['away_behavior']}")
    if "service" in action:
        # Normalized by the handler's normalize_chore; None/"" unlinks the service.
        params["service"] = action.get("service")
        changes.append(f"service → {params['service'] or 'none'}")
    if "owner_id" in action:
        oid = _resolve_link(action, household, "owner_id", "members")
        params["owner_id"] = oid
        changes.append(
            f"owner → {_name_of(household, 'members', oid)}" if oid is not None
            else "clear owner (either parent)"
        )
    if "routine_id" in action:
        lid = _resolve_link(action, household, "routine_id", "routines")
        params["routine_id"] = lid
        changes.append(
            f"link to {_name_of(household, 'routines', lid)}" if lid is not None
            else "unlink from routine"
        )
    if len(params) == 1:  # only chore_id — nothing to do
        raise _ActionError("edit_chore needs at least one field to change")
    return ValidatedAction(op, params, f"Edit chore “{cur_title}”: {', '.join(changes)}")


def _v_remove_chore(
    op: str, action: dict[str, Any], snapshot: dict[str, Any]
) -> ValidatedAction:
    household = _require_household(snapshot)
    cid, title = _require_chore(action, household)
    return ValidatedAction(op, {"chore_id": cid}, f"Remove chore: “{title}”")


def _as_iso_date(value: Any, field_name: str) -> str:
    """Validate a ``"YYYY-MM-DD"`` local date string, or raise :class:`_ActionError`."""
    if not isinstance(value, str):
        raise _ActionError(f"{field_name} must be a 'YYYY-MM-DD' date")
    try:
        datetime.strptime(value.strip(), "%Y-%m-%d")  # tz-ok: validates a local date
    except ValueError:
        raise _ActionError(f"{field_name} must be a 'YYYY-MM-DD' date") from None
    return value.strip()


def _v_set_away(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    _require_household(snapshot)
    starts_on = _as_iso_date(action.get("starts_on"), "starts_on")
    ends_on = _as_iso_date(action.get("ends_on"), "ends_on")
    if ends_on < starts_on:
        raise _ActionError("ends_on must be on or after starts_on")
    note = action.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise _ActionError("note must be text")
        note = note.strip()[:120] or None
    params = {"starts_on": starts_on, "ends_on": ends_on, "note": note}
    detail = f"Mark household away {starts_on} → {ends_on}"
    if note:
        detail += f" ({note})"
    return ValidatedAction(op, params, detail)


def _v_clear_away(op: str, action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    _require_household(snapshot)
    return ValidatedAction(op, {}, "Clear the household away window")


def _v_set_member_away(
    op: str, action: dict[str, Any], snapshot: dict[str, Any]
) -> ValidatedAction:
    # Per-user state (like a preference) — usable without a household; it only
    # affects chore routing, which is a no-op for a solo user with no chores.
    starts_on = _as_iso_date(action.get("starts_on"), "starts_on")
    ends_on = _as_iso_date(action.get("ends_on"), "ends_on")
    if ends_on < starts_on:
        raise _ActionError("ends_on must be on or after starts_on")
    note = action.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise _ActionError("note must be text")
        note = note.strip()[:120] or None
    params = {"starts_on": starts_on, "ends_on": ends_on, "note": note}
    detail = f"Mark yourself away {starts_on} → {ends_on}"
    if note:
        detail += f" ({note})"
    return ValidatedAction(op, params, detail)


def _v_clear_member_away(
    op: str, action: dict[str, Any], snapshot: dict[str, Any]
) -> ValidatedAction:
    return ValidatedAction(op, {}, "Clear your away status")


#: op → validator. This registry *is* the whitelist: :data:`ALLOWED_OPS` is
#: derived from its keys, so adding a capability is one entry here and cannot
#: drift from the security boundary. The household ops (set_fact … remove_shopping)
#: are only usable when the caller is in a household — the snapshot omits the
#: household context otherwise, and those validators drop with a reason.
_VALIDATORS: dict[
    str, Callable[[str, dict[str, Any], dict[str, Any]], ValidatedAction]
] = {
    "add_todo": _v_add_todo,
    "complete_todo": _v_todo_status,
    "drop_todo": _v_todo_status,
    "delegate_todo": _v_delegate_todo,
    "set_priority": _v_set_priority,
    "set_estimate": _v_set_estimate,
    "rename_todo": _v_rename_todo,
    "set_deadline": _v_set_deadline,
    "set_todo_notes": _v_set_todo_notes,
    "add_commitment": _v_add_commitment,
    "cancel_commitment": _v_cancel_commitment,
    "set_commitment_notes": _v_set_commitment_notes,
    "set_commitment_hardness": _v_set_commitment_hardness,
    "dismiss_conflict": _v_dismiss_conflict,
    "start_outing": _v_start_outing,
    "rename_outing": _v_rename_outing,
    "set_outing_window": _v_set_outing_window,
    "set_outing_start": _v_set_outing_start,
    "add_if_then": _v_add_if_then,
    "set_fact": _v_fact,
    "clear_fact": _v_fact,
    "set_agreement": _v_set_agreement,
    "remove_agreement": _v_remove_agreement,
    "add_shopping": _v_add_shopping,
    "check_shopping": _v_check_shopping,
    "remove_shopping": _v_remove_shopping,
    "set_routine": _v_set_routine,
    "edit_routine": _v_edit_routine,
    "remove_routine": _v_remove_routine,
    "set_chore": _v_set_chore,
    "edit_chore": _v_edit_chore,
    "remove_chore": _v_remove_chore,
    "set_away": _v_set_away,
    "clear_away": _v_clear_away,
    "set_member_away": _v_set_member_away,
    "clear_member_away": _v_clear_member_away,
}

#: The ops the assistant may emit — the security boundary, derived from the
#: registry so the two can never disagree.
ALLOWED_OPS = frozenset(_VALIDATORS)


def _validate_one(action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    """Validate a single raw action, raising :class:`_ActionError` on any problem."""
    op = action.get("op")
    validator = _VALIDATORS.get(op) if isinstance(op, str) else None
    if validator is None:
        raise _ActionError(f"unsupported action '{op}'")
    return validator(op, action, snapshot)


def validate_actions(
    raw_actions: list[dict[str, Any]], snapshot: dict[str, Any]
) -> tuple[list[ValidatedAction], list[str]]:
    """Validate raw actions against the snapshot.

    Args:
        raw_actions: Unvalidated dicts (from the model, or echoed back by the
            client on Apply).
        snapshot: Current-state view for id resolution.

    Returns:
        ``(actions, errors)`` — valid :class:`ValidatedAction` objects, plus a
        human-readable reason for each dropped action.
    """
    actions: list[ValidatedAction] = []
    errors: list[str] = []
    for raw in raw_actions:
        if not isinstance(raw, dict):
            errors.append("ignored a malformed action")
            continue
        try:
            actions.append(_validate_one(raw, snapshot))
        except _ActionError as exc:
            errors.append(str(exc))
    return actions, errors


def plan(
    message: str,
    memory: Any,
    *,
    client: Generator,
    now: datetime | None = None,
    tz: str = "UTC",
) -> AssistantPlan:
    """Interpret a message and return a validated, previewable plan (no writes).

    Args:
        message: The user's chat message.
        memory: A **scoped** store.
        client: A model client (Ollama or Anthropic).
        now: Current instant as naive UTC (defaults to :func:`utcnow`).
        tz: The user's IANA timezone, so relative dates/times resolve in *their*
            zone and are emitted as local wall-clock (see :func:`interpret`).
    """
    snapshot = build_snapshot(memory)
    reply, raw_actions = interpret(message, snapshot, client=client, now=now, tz=tz)
    actions, errors = validate_actions(raw_actions, snapshot)
    # Fall back to the honest, deterministic acknowledgement when the model gave
    # no reply, OR when it *tried* to edit but every action was dropped in
    # validation: keeping a confident model reply ("I'll bump that to urgent")
    # there would tell the user an edit is coming that will never happen. A reply
    # with no actions and no errors is a legitimate informational answer — leave
    # it be.
    if not reply or (errors and not actions):
        reply = _default_reply(actions, errors)
    return AssistantPlan(reply=reply, actions=actions, errors=errors)


def _default_reply(actions: list[ValidatedAction], errors: list[str]) -> str:
    """A fallback acknowledgement when the model gave no ``reply`` text."""
    if actions:
        n = len(actions)
        return f"I can make {n} change{'s' if n != 1 else ''}. Review and Apply below."
    if errors:
        return "I couldn't turn that into an edit I can make."
    return "I didn't find anything to change."


# --- execution ------------------------------------------------------------


def execute_actions(
    memory: Any,
    actions: list[ValidatedAction],
    *,
    timezone: str = "UTC",
    client: Generator | None = None,
) -> list[dict[str, Any]]:
    """Execute validated actions against the scoped store.

    Each action is applied independently; a failure (e.g. an unparseable date, or
    an id that changed since it was proposed) is reported per-action rather than
    aborting the batch. The store re-checks ownership on every mutation, so this
    can only touch the caller's own rows.

    Args:
        memory: A **scoped** store.
        actions: Validated actions from :func:`validate_actions`.
        timezone: IANA zone used to interpret naive deadline/commitment times.
        client: Optional model client, used only by ``delegate_todo`` to write the
            prep brief (falls back to a heuristic brief when ``None`` / offline).
            Ignored by every other op, so callers with no model can omit it.

    Returns:
        One ``{op, summary, ok, detail}`` result per action.
    """
    return [_execute_one(memory, a, timezone, client) for a in actions]


def _execute_one(
    memory: Any, action: ValidatedAction, tz: str, client: Generator | None = None
) -> dict[str, Any]:
    """Apply one action, returning a result row (never raises)."""
    op, p = action.op, action.params
    result = {"op": op, "summary": action.summary, "ok": False, "detail": ""}
    try:
        if op == "add_todo":
            deadline = _to_utc_or_none(p.get("deadline"), tz)
            tid = memory.add_todo(
                p["title"],
                estimate_minutes=p.get("estimate_minutes"),
                priority=p.get("priority", 1),
                deadline=deadline,
                energy=p.get("energy"),
            )
            result.update(ok=True, detail=f"todo #{tid}")
        elif op == "complete_todo":
            result["ok"] = memory.close_todo(p["todo_id"], "done")
        elif op == "drop_todo":
            result["ok"] = memory.close_todo(p["todo_id"], "dropped")
        elif op == "set_priority":
            result["ok"] = memory.set_todo_priority(p["todo_id"], p["priority"])
        elif op == "set_estimate":
            result["ok"] = memory.set_todo_estimate(p["todo_id"], p["estimate_minutes"])
        elif op == "rename_todo":
            result["ok"] = memory.set_todo_title(p["todo_id"], p["title"])
        elif op == "set_deadline":
            deadline = _to_utc_or_none(p.get("deadline"), tz)
            result["ok"] = memory.update_todo_deadline(p["todo_id"], deadline)
        elif op == "set_todo_notes":
            result["ok"] = memory.set_todo_notes(p["todo_id"], p.get("notes"))
        elif op == "delegate_todo":
            todo = memory.get_todo(p["todo_id"])
            if todo is None or todo.get("status") != "open":
                result["detail"] = "todo is no longer open"
            else:
                handler = p["handler"]
                smtp = None
                if handler == HANDLER_EMAIL:
                    src = memory.mail_sources_for_todos([todo["id"]]).get(todo["id"]) or {}
                    smtp = resolve_smtp_for(
                        memory, account=src.get("account"), domain=todo.get("domain")
                    )
                outcome = run_delegation(
                    memory, todo, handler=handler,
                    destination=p.get("destination"), context=p.get("context"),
                    va_note=p.get("note"),
                    client=client, smtp=smtp,
                )
                # A failed hand-off (e.g. SMTP unconfigured) still stored the brief,
                # but the delegation didn't land — report it as not-ok with the reason.
                result.update(
                    ok=outcome.status != STATUS_FAILED,
                    detail=f"{outcome.status}: {outcome.detail}",
                )
        elif op == "add_commitment":
            start_at = to_utc(p["start_at"], default_tz=tz)
            end_at = _to_utc_or_none(p.get("end_at"), tz)
            _cid, created = memory.upsert_commitment(
                title=p["title"],
                start_at=start_at,
                end_at=end_at,
                location=p.get("location"),
                notes=p.get("notes"),
                source="manual",
            )
            result.update(ok=True, detail="added" if created else "updated")
        elif op == "cancel_commitment":
            result["ok"] = memory.cancel_commitment(p["commitment_id"])
        elif op == "set_commitment_notes":
            result["ok"] = memory.set_commitment_notes(
                p["commitment_id"], p.get("notes")
            ) is not None
        elif op == "set_commitment_hardness":
            result["ok"] = memory.set_commitment_hardness(
                p["commitment_id"], p["hardness"]
            ) is not None
        elif op == "dismiss_conflict":
            memory.dismiss_conflict(p["key"])
            result["ok"] = True
        elif op == "start_outing":
            departure = _to_utc_or_none(p.get("start_at"), tz)
            oid = memory.start_outing(
                p["intention"], p["time_window_minutes"], departure_at=departure
            )
            result.update(ok=True, detail=f"outing #{oid}")
        elif op == "rename_outing":
            result["ok"] = memory.set_outing_intention(p["outing_id"], p["intention"]) is not None
        elif op == "set_outing_window":
            result["ok"] = (
                memory.set_outing_window(p["outing_id"], p["time_window_minutes"]) is not None
            )
        elif op == "set_outing_start":
            start_at = to_utc(p["start_at"], default_tz=tz)
            result["ok"] = memory.set_outing_departure(p["outing_id"], start_at) is not None
        elif op == "add_if_then":
            pid = memory.add_implementation_intention(
                cue_text=p["cue_text"],
                action_text=p["action_text"],
                cue_place=p.get("cue_place"),
                cue_window=p.get("cue_window"),
                cue_event=p.get("cue_event"),
            )
            result.update(ok=True, detail=f"plan #{pid}")
        elif op == "set_fact":
            # updated_by is the acting (scoped) user — never model-supplied — so
            # every LLM-driven edit is attributed (the raw material for the digest).
            memory.set_fact(
                category=p["category"],
                item=p["item"],
                value=p.get("value"),
                updated_by=memory.user_id,
                child_id=p["child_id"],
            )
            result["ok"] = True
        elif op == "clear_fact":
            result["ok"] = memory.clear_fact(
                category=p["category"], item=p["item"], child_id=p["child_id"]
            )
        elif op == "set_agreement":
            aid = memory.set_agreement(
                title=p["title"],
                body=p.get("body"),
                kind=p.get("kind", "consistency"),
                structured=p.get("structured"),
                updated_by=memory.user_id,
                child_id=p["child_id"],
            )
            result.update(ok=True, detail=f"agreement #{aid}")
        elif op == "remove_agreement":
            result["ok"] = memory.remove_agreement(p["agreement_id"])
        elif op == "add_shopping":
            sid = memory.add_shopping_item(
                item=p["item"],
                spec=p.get("spec"),
                where_to_buy=p.get("where_to_buy"),
                child_id=p["child_id"],
                added_by=memory.user_id,
            )
            result.update(ok=True, detail=f"item #{sid}")
        elif op == "check_shopping":
            result["ok"] = memory.set_shopping_got(
                p["shopping_id"], p["got"], user_id=memory.user_id
            )
        elif op == "remove_shopping":
            result["ok"] = memory.remove_shopping_item(p["shopping_id"])
        elif op == "set_routine":
            rid = memory.set_routine(
                title=p["title"],
                days=p["days"],
                month_days=p.get("month_days", ""),
                due_time=p["due_time"],
                accountable_id=p.get("accountable_id"),
                impact=p.get("impact"),
                enabled=p.get("enabled", True),
                updated_by=memory.user_id,
            )
            result.update(ok=True, detail=f"routine #{rid}")
        elif op == "edit_routine":
            cur = memory.routine(p["routine_id"])
            if cur is None:
                result["detail"] = "no such routine"
            else:
                # Merge the requested changes over the current row, then normalize
                # the whole thing — so unspecified fields (incl. the accountable
                # owner) are preserved rather than reset.
                merged = {**cur, **{k: v for k, v in p.items() if k != "routine_id"}}
                clean, err = normalize_routine(merged)
                if err is not None:
                    raise ValueError(err)
                outcome = memory.update_routine(
                    p["routine_id"], updated_by=memory.user_id, **clean
                )
                result["ok"] = outcome == "ok"
                if outcome == "duplicate":
                    result["detail"] = "another routine already has that name"
        elif op == "remove_routine":
            result["ok"] = memory.remove_routine(p["routine_id"])
        elif op == "set_chore":
            cid = memory.set_chore(
                title=p["title"],
                due_time=p["due_time"],
                days=p["days"],
                month_days=p.get("month_days", ""),
                owner_id=p.get("owner_id"),
                routine_id=p.get("routine_id"),
                remind_before=p.get("remind_before", 30),
                impact=p.get("impact"),
                enabled=p.get("enabled", True),
                away_behavior=p.get("away_behavior", "keep"),
                service=p.get("service"),
                updated_by=memory.user_id,
            )
            result.update(ok=True, detail=f"chore #{cid}")
        elif op == "edit_chore":
            cur = memory.chore(p["chore_id"])
            if cur is None:
                result["detail"] = "no such chore"
            else:
                # Merge the requested changes over the current row, then normalize —
                # so unspecified fields (owner, schedule, …) are preserved. The
                # routine link isn't part of normalize_chore's shape, so carry the
                # edited value if one was set, else keep the current link.
                merged = {**cur, **{k: v for k, v in p.items() if k != "chore_id"}}
                clean, err = normalize_chore(merged)
                if err is not None:
                    raise ValueError(err)
                routine_id = p["routine_id"] if "routine_id" in p else cur.get("routine_id")
                outcome = memory.update_chore(
                    p["chore_id"], updated_by=memory.user_id, routine_id=routine_id, **clean
                )
                result["ok"] = outcome == "ok"
                if outcome == "duplicate":
                    result["detail"] = "another chore already has that name"
        elif op == "remove_chore":
            result["ok"] = memory.remove_chore(p["chore_id"])
        elif op == "set_away":
            memory.set_away_window(
                starts_on=p["starts_on"], ends_on=p["ends_on"], note=p.get("note")
            )
            result.update(ok=True, detail=f"away {p['starts_on']} → {p['ends_on']}")
        elif op == "clear_away":
            memory.clear_away_window()
            result.update(ok=True, detail="away window cleared")
        elif op == "set_member_away":
            memory.set_member_away(
                starts_on=p["starts_on"], ends_on=p["ends_on"], note=p.get("note")
            )
            result.update(ok=True, detail=f"you're away {p['starts_on']} → {p['ends_on']}")
        elif op == "clear_member_away":
            memory.clear_member_away()
            result.update(ok=True, detail="your away status cleared")
        if not result["ok"] and not result["detail"]:
            result["detail"] = "nothing changed (item may have already moved)"
    except Exception as exc:  # noqa: BLE001 — one bad action must never abort the batch
        # `execute_actions` promises per-action isolation and this helper promises
        # it never raises. A ValueError (e.g. to_utc on an unparseable date) is the
        # common case, but a store call can also raise sqlite3 errors or an
        # unexpected KeyError/TypeError on a surprising row shape — those must be
        # reported per-action too, not propagated to abort later valid actions.
        _log.warning("action %r failed: %s", op, exc)
        result["detail"] = f"couldn't apply: {exc}"
    return result


def _to_utc_or_none(value: str | None, tz: str) -> str | None:
    """Convert a deadline/end string to UTC, or ``None`` if empty/None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return to_utc(value, default_tz=tz)
