"""Delegate a todo to an assistant — hand off the prep / follow-up work.

Some open loops are less "do a tiny first step" and more "someone should go dig
up the options, draft the email, and hand it back ready to send." This module is
that handoff. A todo is *delegated* to a pluggable **handler**, which does the
prep and writes the result onto the todo's ``todo_delegations`` row (see the
schema and :class:`~prefrontal.memory.repos.todos.TodosRepo`):

- ``agent`` — the in-app AI assistant. The local model writes a **research brief**
  (what to know / decide, options, open questions) plus **draft communications**
  (the email/message the todo probably needs), straight back onto the row. The
  work happens on the box; nothing leaves it. Ends at ``prepped`` — ready for you
  to review and act on.
- ``email`` — a human virtual assistant. The same brief + drafts are composed into
  an email and sent to the VA at ``destination`` over the user's own SMTP source
  (:func:`prefrontal.sources.resolve_smtp`). Ends at ``forwarded`` (sent, VA is on
  it) — you mark it ``returned`` when their work comes back. If SMTP isn't
  configured or the relay errors, the brief is still stored so you can send it by
  hand, and the row ends ``failed`` with the reason.

The prep generation mirrors the rest of the codebase's LLM usage
(:func:`prefrontal.todos.augment_todo` / :func:`~prefrontal.todos.decompose_task`):
one JSON call to the injected local model, a plain-language heuristic when it's
slow/down, and pure/testable throughout. New handlers register in ``_HANDLERS``;
``HANDLERS`` (the accepted set) is *derived* from it so the API boundary can't
drift out of sync with what's actually dispatchable — the same discipline as the
assistant's ``ALLOWED_OPS``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from prefrontal.clock import parse_ts as _parse_ts
from prefrontal.integrations import Generator
from prefrontal.integrations.ollama import OllamaError
from prefrontal.integrations.smtp import SmtpClient
from prefrontal.llm_json import extract_json_object
from prefrontal.log import get_logger
from prefrontal.sources import SmtpSource

if TYPE_CHECKING:
    from prefrontal.memory.store import MemoryStore

logger = get_logger(__name__)

#: Handler names (also the ``handler`` column values).
HANDLER_AGENT = "agent"
HANDLER_EMAIL = "email"

#: Lifecycle statuses (the ``status`` column values).
STATUS_FORWARDED = "forwarded"
STATUS_IN_PREP = "in_prep"
STATUS_PREPPED = "prepped"
STATUS_RETURNED = "returned"
STATUS_FAILED = "failed"

#: Statuses that mean the todo is actively "off your plate" — with a human VA
#: (``forwarded``), the agent still working (``in_prep``), or an agent brief
#: waiting for you (``prepped``). A todo in one of these is *parked*: pulled out of
#: the active do-it-now surfaces (avoidance, one-thing-now, panic, briefing) and
#: re-surfaced only by the slower :func:`checkin_interval_hours` cadence. A
#: ``returned`` or ``failed`` delegation is NOT parked — the work is back with you.
PARKED_STATUSES = frozenset({STATUS_FORWARDED, STATUS_IN_PREP, STATUS_PREPPED})


def checkin_interval_hours(
    todo: dict[str, Any], delegation: dict[str, Any], now: datetime
) -> float:
    """How long to wait before re-surfacing a parked delegation as a check-in.

    Item-dependent (the whole point of delegation is to get it off your plate, so
    it should only resurface on a *slower* cadence that reflects how time-sensitive
    it is):

    - ``prepped`` (an agent brief is sitting ready for you) → soon (12h).
    - ``forwarded`` (with a human VA): near a deadline → daily; high priority →
      ~2 days; otherwise with a deadline → ~3 days; no deadline / low priority →
      ~weekly.
    - ``in_prep`` (still being prepped) → only a long safety fallback.
    """
    status = (delegation or {}).get("status")
    if status == STATUS_PREPPED:
        return 12.0
    if status == STATUS_IN_PREP:
        return 24.0 * 7  # transient; a long fallback only
    # forwarded — scale by urgency.
    from prefrontal.todos import _parse_deadline  # lazy: todos imports the reverse way

    deadline = _parse_deadline(todo.get("deadline"))
    if deadline is not None and (deadline - now).total_seconds() / 86400.0 <= 2:
        return 24.0
    priority = todo.get("priority")
    priority = 1 if priority is None else int(priority)
    if priority >= 2:
        return 48.0
    if deadline is not None:
        return 72.0
    return 24.0 * 7


def checkin_message(
    todo: dict[str, Any], delegation: dict[str, Any], now: datetime
) -> str | None:
    """The gentle "still handled?" check-in text for a parked delegation, or ``None``.

    ``forwarded`` → "heard back from your assistant?"; ``prepped`` → "your prep is
    ready to review"; ``in_prep`` → nothing (it's mid-flight, not worth a nudge).
    """
    status = (delegation or {}).get("status")
    title = todo.get("title", "this")
    if status == STATUS_PREPPED:
        actions = [a for a in (delegation.get("actions") or []) if a.get("mine")]
        extra = (
            f" ({len(actions)} action item{'s' if len(actions) != 1 else ''} for you)"
            if actions
            else ""
        )
        return f'Your assistant prep for “{title}” is ready to review{extra}.'
    if status == STATUS_FORWARDED:
        stamp = _parse_ts(delegation.get("prepped_at") or delegation.get("updated_at"))
        ago = ""
        if stamp is not None:
            days = int((now - stamp).total_seconds() // 86400)
            ago = f" {days}d ago" if days >= 1 else " today"
        dest = delegation.get("destination")
        who = f" to {dest}" if dest else ""
        return (
            f'You handed “{title}” off{who}{ago}. Heard back? Mark it returned once '
            f"it's done, or nudge them."
        )
    return None


#: Missed (ignored) check-ins on a *forwarded* hand-off before the gentle "heard
#: back?" escalates to a "take it back / re-delegate / drop it?" decision prompt.
#: Tunable via the ``delegation_stall_misses`` coaching key.
DEFAULT_STALLED_CHECKIN_MISSES = 3


def stalled_handoff_message(
    todo: dict[str, Any], delegation: dict[str, Any], misses: int, now: datetime
) -> str:
    """The escalated decision prompt for a hand-off that keeps going nowhere.

    Fired once a ``forwarded`` delegation has drawn ``misses`` ignored check-ins
    with no movement: rather than nudge "heard back?" forever on the same slow
    cadence, name the stall and ask for a decision — take it back, re-delegate, or
    drop it — so a dead hand-off gets resolved instead of quietly rotting.
    """
    title = todo.get("title", "this")
    stamp = _parse_ts(delegation.get("prepped_at") or delegation.get("updated_at"))
    ago = ""
    if stamp is not None:
        days = int((now - stamp).total_seconds() // 86400)
        ago = f" {days}d ago" if days >= 1 else " recently"
    dest = delegation.get("destination")
    who = f" to {dest}" if dest else ""
    return (
        f'“{title}” has been parked{who}{ago} and still hasn’t moved after {misses} '
        "check-ins. Time to decide: take it back, re-delegate, or drop it?"
    )


#: System prompt for the inbound loop-closer: is this email the VA handing work back?
_MATCH_SYSTEM = (
    "You decide whether an incoming email is a virtual assistant returning the "
    "COMPLETED work on a task that was handed to them. You are given the email "
    "(already known to come from an assistant's address) and a short list of the "
    "tasks currently delegated to that assistant. Pick the ONE task the email is "
    "the finished return of — the assistant did the work and is handing it back "
    "(a reply with the answer, the draft, the booking, the result) — or none if "
    "the email is merely an acknowledgement (\"on it\", \"will do\"), a question "
    "back, an out-of-office, or unrelated. When unsure, choose none.\n"
    'Reply with ONLY a JSON object: {"todo_id": <an id from the list, or null>, '
    '"reason": "<a few words>"}.'
)


def match_delegated_reply(
    *,
    sender_email: str | None,
    subject: str | None,
    body: str | None,
    candidates: list[dict[str, Any]],
    client: Generator | None = None,
) -> dict[str, Any] | None:
    """Infer whether an incoming mail item is a human VA returning a delegated todo.

    Closes the delegation loop from the *inbound* side (issue #448): when a message
    arrives from the address a todo was handed to, and the model confirms it's the
    finished work coming back, the caller advances that delegation to
    :data:`STATUS_RETURNED` and links the mail to the existing todo — instead of
    spawning an unrelated new todo that buries the returned work.

    Two gates, cheapest first, so this is a no-op on ordinary mail:

    1. **Sender gate (deterministic).** The message must come from the
       ``destination`` of an open ``email``-handler delegation. No match → ``None``
       with no model call. This bounds the model to the rare VA-reply case, and to
       the (usually one) task handed to *that* assistant.
    2. **Content confirmation (model).** Among that sender's delegated todos, the
       model picks the one this message is the completed return of, or none — so a
       VA's "on it!" acknowledgement or an unrelated note doesn't wrongly close a
       loop. Never raises: with no ``client``, or on any model failure, the loop is
       simply not closed automatically (the caller falls back to normal handling).
       Safe by construction — no delegation state is ever mutated on a guess.

    Args:
        sender_email: The incoming message's sender address.
        subject: The incoming subject line.
        body: The incoming body or snippet (``None`` under the ``signals`` policy —
            the sender gate + subject still work).
        candidates: ``store.actively_delegated_todos()`` — todo dicts each carrying
            a decoded ``delegation`` dict.
        client: A model client; ``None`` disables content confirmation (and so
            never auto-advances a delegation).

    Returns:
        The matched todo dict (an element of ``candidates``), or ``None``.
    """
    sender = (sender_email or "").strip().lower()
    if not sender:
        return None
    matches = [
        c
        for c in candidates
        if (c.get("delegation") or {}).get("handler") == HANDLER_EMAIL
        and (c["delegation"].get("destination") or "").strip().lower() == sender
    ]
    if not matches or client is None:
        return None
    chosen_id = _llm_pick_delegation(subject, body, matches, client)
    if chosen_id is None:
        return None
    return next((c for c in matches if c["id"] == chosen_id), None)


def _llm_pick_delegation(
    subject: str | None,
    body: str | None,
    candidates: list[dict[str, Any]],
    client: Generator,
) -> int | None:
    """Ask the model which delegated todo an incoming message returns, or ``None``.

    House style: one JSON call, tolerant extraction, restricted to the ids in
    ``candidates``; any failure or an out-of-set id → ``None`` (no auto-advance).
    """
    from prefrontal.integrations.base import ProviderError

    lines = [f"Email subject: {subject or '(no subject)'}"]
    text = (body or "").strip()
    if text:
        lines += ["Email body:", text[:2000]]
    lines += ["", "Tasks delegated to this assistant:"]
    lines += [f"- id={c['id']}: {(c.get('title') or '').strip()}" for c in candidates]
    try:
        reply = client.generate("\n".join(lines), system=_MATCH_SYSTEM)
    except ProviderError:
        return None
    chosen = extract_json_object(reply).get("todo_id")
    if not isinstance(chosen, int) or isinstance(chosen, bool):
        return None
    return chosen if chosen in {c["id"] for c in candidates} else None

#: Draft channels a prep brief can produce.
_DRAFT_CHANNELS = ("email", "message", "call")

#: Cap on the context window (tokens) we ask Ollama for on a prep call. Ollama's
#: default (~2048) silently truncates a long pasted transcript from the front, so
#: we size ``num_ctx`` to fit the prompt — but bound it here, since a bigger window
#: means much slower prompt evaluation on a local model. ~16k comfortably holds a
#: long meeting transcript; beyond that we truncate the context instead.
_PREP_MAX_NUM_CTX = 16384

#: Per-call timeout (seconds) for the prep generation. A full-transcript prompt at
#: a large ``num_ctx`` can take a couple of minutes to evaluate on an 8B model, far
#: past the client default — so prep runs in the background (see the router) and we
#: give the call room to finish rather than time out into the heuristic.
_PREP_TIMEOUT = 240.0

#: Rough chars-per-token used only to size ``num_ctx`` (order-of-magnitude is fine).
_CHARS_PER_TOKEN = 4

#: Most of the context we ever echo in the *heuristic* fallback — so a model-down
#: fallback surfaces a short excerpt of what you pasted, never the whole thing.
_HEURISTIC_CONTEXT_EXCERPT = 500

_PREP_SYSTEM = (
    "You are an executive assistant preparing to take a task off someone's plate. "
    "Given a task (and often pasted context such as a meeting transcript, thread, or "
    "notes), produce the prep work that makes it ready to act on. Reply with ONLY a "
    "JSON object of the form "
    '{"brief": "<2-5 sentence write-up: what needs deciding, the realistic '
    'options, and any open questions or info to gather first>", '
    '"drafts": [{"channel": "email|message|call", "to": "<who, or empty>", '
    '"subject": "<for email; else empty>", "body": "<the drafted message or, for '
    'a call, a short call script>"}], '
    '"actions": [{"text": "<a concrete action item, imperative>", "mine": true}]}. '
    "For actions: when the context contains a transcript/notes, pull out the concrete "
    "action items or commitments. Set \"mine\": true for an item assigned to or owned "
    "by the user (you'll be told their name), false for anyone else; if unclear, false. "
    'Return "actions": [] when there are no clear action items (a simple chore has '
    "none). "
    "Include a draft only when the task plainly needs one outbound message; an "
    "internal chore (tidy the garage) needs a brief but no drafts, so return "
    '"drafts": []. Never invent facts you were not given — where a real detail '
    "(a date, an account number, a name) is needed, leave a clearly-marked "
    "[bracketed placeholder] for the user to fill. Keep it concise and practical."
)


@dataclass(frozen=True)
class DelegationResult:
    """What a handler produced — persisted onto the ``todo_delegations`` row.

    Attributes:
        handler: The handler that ran (``agent`` / ``email``).
        status: The lifecycle status the delegation ended at.
        brief: The prep write-up (may be a heuristic stub when the model is down).
        drafts: Drafted communications, each ``{channel, to, subject, body}``.
        actions: Extracted action items, each ``{text, mine}`` (``mine`` flags the
            ones the model attributes to the user — the dashboard offers to turn
            those into todos).
        detail: A human-readable note (transport response, failure reason, …).
    """

    handler: str
    status: str
    brief: str
    drafts: list[dict[str, str]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""


def _coerce_drafts(raw: Any) -> list[dict[str, str]]:
    """Keep only well-formed draft dicts from a model reply (defensive)."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        channel = str(item.get("channel", "")).strip().lower()
        if channel not in _DRAFT_CHANNELS:
            channel = "message"
        body = str(item.get("body", "")).strip()
        if not body:
            continue
        out.append(
            {
                "channel": channel,
                "to": str(item.get("to", "")).strip(),
                "subject": str(item.get("subject", "")).strip(),
                "body": body,
            }
        )
    return out


def _coerce_actions(raw: Any) -> list[dict[str, Any]]:
    """Keep only well-formed action items from a model reply (defensive).

    Each survivor is ``{"text": <non-empty str>, "mine": <bool>}``. Anything without
    text is dropped; ``mine`` defaults to ``False`` (only surface a "make this a
    todo" prompt for items the model clearly attributes to the user).
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        out.append({"text": text, "mine": bool(item.get("mine", False))})
    return out


def _heuristic_brief(
    title: str,
    notes: str | None,
    decomposition: dict | None,
    context: str | None = None,
) -> str:
    """A plain, honest brief when the local model is unavailable.

    No research is possible offline, so this says so plainly and leans on whatever
    structure already exists (the notes, any user-supplied context, and any
    decomposed steps) rather than inventing content.
    """
    lines = [
        f"Prep for: {title}.",
        "(Generated offline — the local model was unavailable, so this is a "
        "starting outline, not researched.)",
    ]
    if notes:
        lines.append(f"Context on file: {notes}")
    if context:
        # Only ever a short excerpt here — the heuristic is the model-down path, so
        # echoing the whole pasted blob back would just be parroting it verbatim.
        excerpt = context.strip()
        if len(excerpt) > _HEURISTIC_CONTEXT_EXCERPT:
            trimmed = len(excerpt) - _HEURISTIC_CONTEXT_EXCERPT
            excerpt = (
                excerpt[:_HEURISTIC_CONTEXT_EXCERPT].rstrip()
                + f"… [+{trimmed} more characters — not yet processed]"
            )
        lines.append(f"Context you provided (awaiting the model):\n{excerpt}")
    steps = []
    if decomposition:
        first = decomposition.get("first_step")
        if first:
            steps.append(str(first))
        steps.extend(str(s) for s in (decomposition.get("steps") or []))
    if steps:
        lines.append("Suggested approach:")
        lines.extend(f"  {i + 1}. {s}" for i, s in enumerate(steps))
    return "\n".join(lines)


def _fit_num_ctx(prompt_chars: int) -> int | None:
    """Pick a ``num_ctx`` that holds ``prompt_chars`` (plus room to answer), or None.

    Returns ``None`` when the prompt is small enough for Ollama's default window
    (no need to pay for a bigger, slower context). Otherwise sizes up to
    :data:`_PREP_MAX_NUM_CTX`; a prompt past that gets a truncated *context* upstream
    rather than a window we won't grant.
    """
    est_tokens = prompt_chars // _CHARS_PER_TOKEN + 512  # headroom for the reply
    if est_tokens <= 2048:
        return None
    # Round up to the next power-ish step Ollama likes, capped.
    return min(_PREP_MAX_NUM_CTX, 1 << (est_tokens - 1).bit_length())


def generate_prep(
    title: str,
    notes: str | None = None,
    decomposition: dict | None = None,
    *,
    context: str | None = None,
    owner_name: str | None = None,
    client: Generator | None = None,
) -> tuple[str, list[dict[str, str]], list[dict[str, Any]]]:
    """Produce a ``(brief, drafts, actions)`` prep package for a task.

    One JSON call to the injected local model (house style: catch
    :class:`OllamaError`, tolerant JSON extraction, coerce, fall back). Two things
    make this robust to a big pasted transcript:

    - **The whole context reaches the model.** Ollama's default context window
      (~2048 tokens) silently truncates a long prompt from the front, so we size
      ``num_ctx`` to fit (capped at :data:`_PREP_MAX_NUM_CTX`) and give the call a
      long timeout — a large window evaluates slowly on a local model.
    - **A prose reply is never thrown away.** If the model answers with a useful
      summary that isn't the requested JSON (small local models often do on long,
      messy input), that prose *is* the brief — far better than discarding it and
      echoing the pasted context back. The heuristic (which only excerpts the
      context) fires solely when the model returns nothing at all.

    Args:
        title: The task text.
        notes: Any free-text notes already on the todo (context for the model).
        decomposition: The todo's decomposition dict, if any (reused as scaffolding).
        context: Optional free-text context supplied at delegation time (e.g. output
            pasted from another AI agent with access to work email) — real facts the
            model may rely on, so it's given more weight than the [placeholder] rule.
        owner_name: The user's display name, so the model can flag which action items
            are theirs (``mine``) versus someone else's.
        client: An Ollama-like client; ``None`` uses the heuristic.
    """
    if client is not None:
        prompt = f"Task: {title}"
        if owner_name:
            prompt += f"\nThe user (whose action items to flag as \"mine\") is: {owner_name}"
        if notes:
            prompt += f"\nNotes: {notes}"
        if decomposition and decomposition.get("first_step"):
            steps = [decomposition["first_step"], *(decomposition.get("steps") or [])]
            prompt += "\nKnown steps: " + "; ".join(str(s) for s in steps)
        if context:
            # Real, user-supplied facts — the model may use these directly rather
            # than leaving [placeholders] for them.
            prompt += f"\nAdditional context provided by the user:\n{context}"
        num_ctx = _fit_num_ctx(len(prompt) + len(_PREP_SYSTEM))
        # Only the (slow) large-context calls need the extended timeout.
        timeout = _PREP_TIMEOUT if num_ctx else None
        try:
            reply = client.generate(
                prompt, system=_PREP_SYSTEM, num_ctx=num_ctx, timeout=timeout
            )
        except OllamaError:
            reply = ""
        raw = extract_json_object(reply)
        brief = raw.get("brief")
        if isinstance(brief, str) and brief.strip():
            return (
                brief.strip(),
                _coerce_drafts(raw.get("drafts")),
                _coerce_actions(raw.get("actions")),
            )
        # Salvage: the model said something usable, just not as JSON. Use it as the
        # brief rather than falling through to the parrot-the-context heuristic.
        if reply and reply.strip():
            return reply.strip(), [], []
    return _heuristic_brief(title, notes, decomposition, context), [], []


def compose_va_email(
    title: str,
    brief: str,
    drafts: list[dict[str, str]],
    note: str | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Compose the ``(subject, body)`` of the email sent to a human VA.

    Pure and unit-testable: leads with the task, includes the brief, lists any
    extracted action items, and appends any drafted communications verbatim so the
    VA can send them with minimal edits. ``note`` is an optional personal message
    from the user, shown *first* (before the standard preamble) so the assistant
    reads it as the opening line.
    """
    subject = f"[Prefrontal] Please help with: {title}"
    parts = []
    if note and note.strip():
        parts.append(note.strip() + "\n")
    parts += [
        f"Hi — could you take this off my plate?\n\nTask: {title}\n",
        f"Prep notes:\n{brief}\n",
    ]
    if actions:
        parts.append("Action items:")
        parts.extend(f"  - {a['text']}" for a in actions)
        parts.append("")
    for i, d in enumerate(drafts, 1):
        header = f"Draft {i} ({d['channel']})"
        if d.get("to"):
            header += f" — to {d['to']}"
        parts.append(header + ":")
        if d.get("subject"):
            parts.append(f"Subject: {d['subject']}")
        parts.append(d["body"] + "\n")
    parts.append("Thanks!\n(Sent by Prefrontal on my behalf.)")
    return subject, "\n".join(parts)


@dataclass
class DelegationRequest:
    """Everything a handler needs to do its prep (assembled by :func:`run_delegation`)."""

    title: str
    notes: str | None = None
    decomposition: dict | None = None
    context: str | None = None  # optional free-text context pasted at delegation time
    va_note: str | None = None  # optional cover note shown atop the email to a human VA
    owner_name: str | None = None  # user's display name (to flag their action items)
    destination: str | None = None
    client: Generator | None = None  # local LLM for prep
    smtp: SmtpSource | None = None  # resolved SMTP source (email handler)
    smtp_client: SmtpClient | None = None


class DelegationHandler(Protocol):
    """A pluggable destination for a delegated todo."""

    kind: str

    def run(self, req: DelegationRequest) -> DelegationResult:  # pragma: no cover - protocol
        ...


class AgentHandler:
    """In-app AI assistant: the local model preps the todo, results stay on-box."""

    kind = HANDLER_AGENT

    def run(self, req: DelegationRequest) -> DelegationResult:
        brief, drafts, actions = generate_prep(
            req.title, req.notes, req.decomposition,
            context=req.context, owner_name=req.owner_name, client=req.client,
        )
        # generate_prep's offline fallback stamps this marker into the brief.
        offline = "Generated offline" in brief
        detail = (
            "prep drafted offline (heuristic)" if offline else "prep drafted by the agent"
        )
        return DelegationResult(
            handler=self.kind,
            status=STATUS_PREPPED,
            brief=brief,
            drafts=drafts,
            actions=actions,
            detail=detail,
        )


class EmailHandler:
    """Human VA: prep the todo, then email the brief + drafts to ``destination``."""

    kind = HANDLER_EMAIL

    def run(self, req: DelegationRequest) -> DelegationResult:
        brief, drafts, actions = generate_prep(
            req.title, req.notes, req.decomposition,
            context=req.context, owner_name=req.owner_name, client=req.client,
        )
        to = (req.destination or "").strip()
        # No SMTP configured (or no recipient): keep the brief so it can be sent by
        # hand, and land in `failed` with a clear, non-alarming reason. Nothing is lost.
        if not to:
            return DelegationResult(
                self.kind, STATUS_FAILED, brief, drafts, actions,
                "no assistant email address given — brief stored for manual sending",
            )
        if req.smtp is None or not req.smtp.configured:
            return DelegationResult(
                self.kind, STATUS_FAILED, brief, drafts, actions,
                "SMTP not configured — brief stored; configure email in Settings to send",
            )
        subject, body = compose_va_email(
            req.title, brief, drafts, note=req.va_note, actions=actions
        )
        client = req.smtp_client or SmtpClient()
        result = client.send(
            req.smtp.host,
            req.smtp.port,
            req.smtp.username,
            req.smtp.password,
            sender=req.smtp.sender,
            to=to,
            subject=subject,
            body=body,
            use_tls=req.smtp.use_tls,
        )
        if not result.delivered:
            return DelegationResult(
                self.kind, STATUS_FAILED, brief, drafts, actions,
                f"send failed ({result.detail}) — brief stored for manual sending",
            )
        return DelegationResult(
            self.kind, STATUS_FORWARDED, brief, drafts, actions,
            f"emailed {to} ({result.detail})",
        )


#: The dispatch registry. ``HANDLERS`` (the accepted set) is derived from it so a
#: new handler is enabled everywhere by adding one entry — the API can't accept a
#: handler name that has no implementation (mirrors the assistant's ALLOWED_OPS).
_HANDLERS: dict[str, DelegationHandler] = {
    HANDLER_AGENT: AgentHandler(),
    HANDLER_EMAIL: EmailHandler(),
}

#: Handler names the API accepts, kept in lockstep with what's dispatchable.
HANDLERS = frozenset(_HANDLERS)


def run_delegation(
    store: MemoryStore,
    todo: dict[str, Any],
    *,
    handler: str,
    destination: str | None = None,
    context: str | None = None,
    va_note: str | None = None,
    owner_name: str | None = None,
    client: Generator | None = None,
    smtp: SmtpSource | None = None,
    smtp_client: SmtpClient | None = None,
) -> DelegationResult:
    """Delegate ``todo`` to ``handler``, run the prep, and persist the result.

    Writes a ``todo_delegations`` row on the (scoped) ``store`` and returns the
    :class:`DelegationResult`. Raises :class:`ValueError` for an unknown handler
    (the caller — router/CLI — turns that into a 4xx). This call is *synchronous and
    can be slow* (a full-transcript prep evaluates a large context window on the
    local model), so the HTTP router runs it on a background thread after writing an
    ``in_prep`` row; the CLI just waits.

    ``smtp`` is only used by the ``email`` handler; resolve it via
    :func:`prefrontal.sources.resolve_smtp` before calling (kept out of here so
    this stays store-agnostic about the encryption boundary).
    """
    impl = _HANDLERS.get(handler)
    if impl is None:
        raise ValueError(f"Unknown delegation handler: {handler!r}")
    decomposition = store.get_decomposition(todo["id"])
    req = DelegationRequest(
        title=todo["title"],
        notes=todo.get("notes"),
        decomposition=decomposition,
        context=context,
        va_note=va_note,
        owner_name=owner_name,
        destination=destination,
        client=client,
        smtp=smtp,
        smtp_client=smtp_client,
    )
    result = impl.run(req)
    store.set_delegation(
        todo["id"],
        handler=result.handler,
        destination=destination,
        status=result.status,
        brief=result.brief,
        drafts=result.drafts,
        actions=result.actions,
        detail=result.detail,
        context=context,
        prepped=result.status in (STATUS_PREPPED, STATUS_FORWARDED),
    )
    return result


def delegation_notice(todo_title: str, result: DelegationResult) -> str | None:
    """The push message when a delegation reaches a terminal state, or ``None``.

    ``prepped`` (agent) → "prep is ready to review"; ``forwarded`` (email) → "sent
    to your assistant"; ``failed`` → a gentle heads-up that it needs a hand.
    ``None`` for non-terminal states (no push worth sending).
    """
    if result.status == STATUS_PREPPED:
        bits = []
        nd = len(result.drafts)
        if nd:
            bits.append(f"{nd} draft{'s' if nd != 1 else ''}")
        na = sum(1 for a in result.actions if a.get("mine"))
        if na:
            bits.append(f"{na} action item{'s' if na != 1 else ''} for you")
        extra = f" ({', '.join(bits)})" if bits else ""
        return f'Prep ready for "{todo_title}"{extra} — review it when you have a sec.'
    if result.status == STATUS_FORWARDED:
        return f'Sent "{todo_title}" to your assistant — {result.detail}.'
    if result.status == STATUS_FAILED:
        return f'Couldn\'t hand off "{todo_title}": {result.detail}.'
    return None
