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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from prefrontal.commitments import to_utc
from prefrontal.integrations import Generator
from prefrontal.llm_json import generate_json
from prefrontal.memory.repos.household import (
    AGREEMENT_KINDS,
    FACT_CATEGORIES,
    HOUSEHOLD_WIDE,
    normalize_fact_category,
    normalize_fact_item,
)
from prefrontal.todos import (
    ENERGY_LEVELS,
    MAX_ESTIMATE_MINUTES,
    MIN_ESTIMATE_MINUTES,
)

#: Ops the assistant is allowed to emit. The whitelist *is* the security boundary
#: (with per-user store scoping): anything not here is refused before execution.
ALLOWED_OPS = frozenset(
    {
        "add_todo",
        "complete_todo",
        "drop_todo",
        "set_priority",
        "set_estimate",
        "rename_todo",
        "set_deadline",
        "add_commitment",
        "cancel_commitment",
        "dismiss_conflict",
        # Shared household sheet (docs/household-sheet.md §5). Only usable when the
        # caller is in a household — the snapshot omits the household context
        # otherwise, and these validators drop with a reason.
        "set_fact",
        "clear_fact",
        "set_agreement",
        "remove_agreement",
        "add_shopping",
        "check_shopping",
        "remove_shopping",
    }
)

_PRIORITY_NAMES = {0: "low", 1: "normal", 2: "high", 3: "urgent"}



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
    "are given a snapshot of their open todos, upcoming commitments, and "
    "dismissable schedule conflicts, each with a numeric id (todos/commitments) "
    "or key (conflicts). Resolve references like 'the dentist todo' to the "
    "matching id from the snapshot — NEVER invent an id that is not listed.\n\n"
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
    '- {"op":"add_commitment","title":str,"start_at":"YYYY-MM-DD HH:MM",'
    '"end_at":"YYYY-MM-DD HH:MM"?,"location":str?}\n'
    '- {"op":"cancel_commitment","commitment_id":int}\n'
    '- {"op":"dismiss_conflict","key":str}\n\n'
    "priority: 0 low, 1 normal, 2 high, 3 urgent.\n\n"
    "If (and ONLY if) the snapshot has a \"household\" object, these shared "
    "co-parent sheet ops are also available. Resolve a kid's name to an id from "
    "\"household.children\"; omit \"child\" (or use 0) for a household-wide fact. "
    "\"category\" MUST be one of \"household.fact_categories\":\n"
    '- {"op":"set_fact","category":str,"item":str,"value":str,"child":int?}\n'
    '- {"op":"clear_fact","category":str,"item":str,"child":int?}\n'
    '- {"op":"set_agreement","title":str,"body":str,'
    '"kind":"reward"|"consistency"|"routine"?,"child":int?,"structured":object?}\n'
    '- {"op":"remove_agreement","agreement_id":int}\n'
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
    prompt small: open todos (id/title/priority/estimate/deadline), upcoming
    commitments (id/title/start/location), and dismissable possible-conflict
    keys. Ids come straight from the store, so the model can only target things
    that actually exist.

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
        }
        for t in memory.open_todos()
    ]
    commitments = [
        {
            "id": c["id"],
            "title": c.get("title"),
            "start_at": c.get("start_at"),
            "location": c.get("location"),
        }
        for c in memory.upcoming_commitments(limit=25)
    ]
    conflicts = _possible_conflicts(memory)
    snapshot = {"todos": todos, "commitments": commitments, "conflicts": conflicts}
    household = _household_snapshot(memory)
    if household is not None:
        snapshot["household"] = household
    return snapshot


def _household_snapshot(memory: Any) -> dict[str, Any] | None:
    """Household context for the shared sheet, or ``None`` if the user is in none.

    Gives the model the roster (so "Sam" resolves to a real ``child`` id), the
    controlled fact-category vocabulary, open agreements (so "remove the sticker
    plan" resolves to a real ``agreement_id``), and the shopping list (so "check
    off the milk" resolves to a real shopping id) — the same id-discipline as the
    todo/commitment snapshot. Omitted entirely for a user with no household, which
    is the signal the household-op validators key on.
    """
    if memory.household_id_or_none() is None:
        return None
    return {
        "children": [
            {"id": c["id"], "name": c.get("name")} for c in memory.children()
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
) -> tuple[str, list[dict[str, Any]]]:
    """Ask the model to turn ``message`` into a reply + raw action list.

    Args:
        message: The user's chat message.
        snapshot: Output of :func:`build_snapshot`.
        client: A model client (Ollama or Anthropic).

    Returns:
        ``(reply, raw_actions)`` — ``raw_actions`` is a list of unvalidated
        dicts. Returns ``("", [])`` if the model is unreachable or its reply
        can't be parsed as JSON, so the caller degrades gracefully.
    """
    prompt = (
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


def _require_household(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the snapshot's household context, or reject (caller isn't a co-parent)."""
    household = snapshot.get("household")
    if not isinstance(household, dict):
        raise _ActionError("you're not set up in a household")
    return household


def _resolve_child(action: dict[str, Any], household: dict[str, Any]) -> tuple[int, str]:
    """Resolve an optional ``child`` to ``(child_id, label)``.

    A missing/zero ``child`` means household-wide (:data:`HOUSEHOLD_WIDE`). A given
    id must match a child in the snapshot, so the model can't attach a fact to a
    kid who doesn't exist.

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
    for c in household.get("children", []):
        if c.get("id") == cid:
            return cid, (c.get("name") or f"child #{cid}")
    raise _ActionError(f"no child with id {cid}")


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


def _validate_one(action: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedAction:
    """Validate a single raw action, raising :class:`_ActionError` on any problem."""
    op = action.get("op")
    if op not in ALLOWED_OPS:
        raise _ActionError(f"unsupported action '{op}'")

    if op == "add_todo":
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

    if op in ("complete_todo", "drop_todo"):
        tid, title = _require_todo(action, snapshot)
        verb = "Complete" if op == "complete_todo" else "Drop"
        return ValidatedAction(op, {"todo_id": tid}, f"{verb} todo: “{title}”")

    if op == "set_priority":
        tid, title = _require_todo(action, snapshot)
        pri = _as_priority(action.get("priority"))
        return ValidatedAction(
            op, {"todo_id": tid, "priority": pri},
            f"Set “{title}” priority to {_PRIORITY_NAMES[pri]}",
        )

    if op == "set_estimate":
        tid, title = _require_todo(action, snapshot)
        est = _as_estimate(action.get("estimate_minutes"))
        return ValidatedAction(
            op, {"todo_id": tid, "estimate_minutes": est},
            f"Set “{title}” estimate to {est:g}m",
        )

    if op == "rename_todo":
        tid, title = _require_todo(action, snapshot)
        new_title = _as_title(action.get("title"))
        return ValidatedAction(
            op, {"todo_id": tid, "title": new_title},
            f"Rename “{title}” → “{new_title}”",
        )

    if op == "set_deadline":
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

    if op == "add_commitment":
        title = _as_title(action.get("title"))
        start_at = _nonblank(action.get("start_at"), "start_at")  # parsed at execute
        params = {"title": title, "start_at": start_at}
        if action.get("end_at") is not None:
            params["end_at"] = _nonblank(action.get("end_at"), "end_at")
        if action.get("location") is not None and str(action.get("location")).strip():
            params["location"] = str(action["location"]).strip()
        return ValidatedAction(op, params, f"Add commitment: “{title}” at {start_at}")

    if op == "cancel_commitment":
        cid, title = _require_commitment(action, snapshot)
        return ValidatedAction(op, {"commitment_id": cid}, f"Cancel commitment: “{title}”")

    if op == "dismiss_conflict":
        key = action.get("key")
        keys = {c.get("key"): c.get("label") for c in snapshot.get("conflicts", [])}
        if not isinstance(key, str) or key not in keys:
            raise _ActionError("no dismissable conflict with that key")
        label = keys[key] or key
        return ValidatedAction(op, {"key": key}, f"Dismiss conflict: {label}")

    if op in ("set_fact", "clear_fact"):
        household = _require_household(snapshot)
        child_id, who = _resolve_child(action, household)
        category = _as_fact_category(action.get("category"))
        item = normalize_fact_item(action.get("item"))
        if not item:
            raise _ActionError("item must be a non-empty string")
        params = {"child_id": child_id, "category": category, "item": item}
        if op == "clear_fact":
            return ValidatedAction(op, params, f"Clear {who}'s {item}")
        value = action.get("value")
        if value is not None and not isinstance(value, (str, int, float)):
            raise _ActionError("value must be text")
        params["value"] = str(value).strip() if value is not None else None
        shown = params["value"] or "—"
        return ValidatedAction(op, params, f"Set {who}'s {item} → {shown}")

    if op == "set_agreement":
        household = _require_household(snapshot)
        child_id, who = _resolve_child(action, household)
        title = _as_title(action.get("title"))
        params = {"child_id": child_id, "title": title}
        kind = action.get("kind")
        if kind is not None:
            if str(kind).lower() not in AGREEMENT_KINDS:
                raise _ActionError("kind must be reward|consistency|routine")
            params["kind"] = str(kind).lower()
        if action.get("body") is not None:
            params["body"] = _nonblank(action.get("body"), "body")
        params["structured"] = _as_structured(action.get("structured"))
        return ValidatedAction(op, params, f"Set plan “{title}” for {who}")

    if op == "remove_agreement":
        household = _require_household(snapshot)
        aid, title = _require_agreement(action, household)
        return ValidatedAction(op, {"agreement_id": aid}, f"Remove plan: “{title}”")

    if op == "add_shopping":
        household = _require_household(snapshot)
        child_id, _who = _resolve_child(action, household)
        item = _as_title(action.get("item"))
        params = {"item": item, "child_id": child_id}
        extras = []
        if action.get("spec") is not None and str(action.get("spec")).strip():
            params["spec"] = str(action["spec"]).strip()
            extras.append(params["spec"])
        if action.get("where_to_buy") is not None and str(action.get("where_to_buy")).strip():
            params["where_to_buy"] = str(action["where_to_buy"]).strip()
            extras.append(f"@ {params['where_to_buy']}")
        detail = f" ({', '.join(extras)})" if extras else ""
        return ValidatedAction(op, params, f"Add to shopping: “{item}”{detail}")

    if op == "check_shopping":
        household = _require_household(snapshot)
        sid, item = _require_shopping(action, household)
        got = action.get("got")
        got = True if got is None else bool(got)
        verb = "Check off" if got else "Un-check"
        return ValidatedAction(op, {"shopping_id": sid, "got": got}, f"{verb}: “{item}”")

    if op == "remove_shopping":
        household = _require_household(snapshot)
        sid, item = _require_shopping(action, household)
        return ValidatedAction(op, {"shopping_id": sid}, f"Remove from shopping: “{item}”")

    raise _ActionError(f"unsupported action '{op}'")  # pragma: no cover


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


def plan(message: str, memory: Any, *, client: Generator) -> AssistantPlan:
    """Interpret a message and return a validated, previewable plan (no writes).

    Args:
        message: The user's chat message.
        memory: A **scoped** store.
        client: A model client (Ollama or Anthropic).
    """
    snapshot = build_snapshot(memory)
    reply, raw_actions = interpret(message, snapshot, client=client)
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
    memory: Any, actions: list[ValidatedAction], *, timezone: str = "UTC"
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

    Returns:
        One ``{op, summary, ok, detail}`` result per action.
    """
    return [_execute_one(memory, a, timezone) for a in actions]


def _execute_one(memory: Any, action: ValidatedAction, tz: str) -> dict[str, Any]:
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
        elif op == "add_commitment":
            start_at = to_utc(p["start_at"], default_tz=tz)
            end_at = _to_utc_or_none(p.get("end_at"), tz)
            _cid, created = memory.upsert_commitment(
                title=p["title"],
                start_at=start_at,
                end_at=end_at,
                location=p.get("location"),
                source="manual",
            )
            result.update(ok=True, detail="added" if created else "updated")
        elif op == "cancel_commitment":
            result["ok"] = memory.cancel_commitment(p["commitment_id"])
        elif op == "dismiss_conflict":
            memory.dismiss_conflict(p["key"])
            result["ok"] = True
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
        if not result["ok"] and not result["detail"]:
            result["detail"] = "nothing changed (item may have already moved)"
    except ValueError as exc:  # e.g. to_utc on an unparseable date
        result["detail"] = f"couldn't apply: {exc}"
    return result


def _to_utc_or_none(value: str | None, tz: str) -> str | None:
    """Convert a deadline/end string to UTC, or ``None`` if empty/None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return to_utc(value, default_tz=tz)
