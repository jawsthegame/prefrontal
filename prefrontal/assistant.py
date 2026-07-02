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
(see :class:`_Generator`), so the local Ollama client and the optional Claude
client are interchangeable — the endpoint prefers Claude when configured and
falls back to Ollama.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from prefrontal.commitments import to_utc
from prefrontal.integrations.anthropic import AnthropicError
from prefrontal.integrations.ollama import OllamaError
from prefrontal.todos import (
    ENERGY_LEVELS,
    MAX_ESTIMATE_MINUTES,
    MIN_ESTIMATE_MINUTES,
)

#: Expected failures from either model backend — caught by :func:`interpret` so a
#: down/misconfigured model degrades to "assistant unavailable" rather than a 500.
_CLIENT_ERRORS = (OllamaError, AnthropicError)

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
    }
)

_PRIORITY_NAMES = {0: "low", 1: "normal", 2: "high", 3: "urgent"}


class _Generator(Protocol):
    """The slice of the model clients (Ollama/Anthropic) used here."""

    def generate(self, prompt: str, *, system: str | None = None) -> str: ...


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
    "priority: 0 low, 1 normal, 2 high, 3 urgent. If the user asks for something "
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
    return {"todos": todos, "commitments": commitments, "conflicts": conflicts}


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


def _extract_json(text: str) -> dict[str, Any] | list[Any] | None:
    """Pull the first JSON object/array out of a model reply (tolerant of fences).

    Tries the whole string first, then a ```json fenced block, then a
    brace/bracket-matched span. Returns ``None`` if nothing parses.
    """
    text = text.strip()
    if not text:
        return None
    for candidate in _json_candidates(text):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _json_candidates(text: str) -> list[str]:
    """Yield progressively looser JSON substrings to attempt parsing."""
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    # First balanced {...} or [...] span, whichever appears first.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break
    return candidates


def interpret(
    message: str,
    snapshot: dict[str, Any],
    *,
    client: _Generator,
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
    try:
        raw = client.generate(prompt, system=ASSISTANT_SYSTEM)
    except _CLIENT_ERRORS:
        return "", []
    parsed = _extract_json(raw)
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


def plan(message: str, memory: Any, *, client: _Generator) -> AssistantPlan:
    """Interpret a message and return a validated, previewable plan (no writes).

    Args:
        message: The user's chat message.
        memory: A **scoped** store.
        client: A model client (Ollama or Anthropic).
    """
    snapshot = build_snapshot(memory)
    reply, raw_actions = interpret(message, snapshot, client=client)
    actions, errors = validate_actions(raw_actions, snapshot)
    if not reply:
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
