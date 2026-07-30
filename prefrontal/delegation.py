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
- ``auto`` — the same in-app assistant, but it does the **legwork** first: a bounded
  loop of allowlisted, unattended-declared tool calls (:mod:`prefrontal.autorun`)
  whose findings feed the same prep. It may also come back with **questions** only
  the user can answer ("what's your current mortgage rate?"), parking at
  ``needs_input`` with the partial write-up already on the todo; answering re-runs it
  with the answers as context. Ends at ``prepped`` or ``needs_input``.
- ``email`` — a human virtual assistant. The same brief + drafts are composed into
  an email and sent to the VA at ``destination`` over the user's own SMTP source
  (:func:`prefrontal.sources.resolve_smtp`). Ends at ``forwarded`` (sent, VA is on
  it) — you mark it ``returned`` when their work comes back. If SMTP isn't
  configured or the relay errors, the brief is still stored so you can send it by
  hand, and the row ends ``failed`` with the reason.
- ``sms`` — a person in your life. The "just ask someone" case: a todo needs a
  quick answer before it can move ("Dad wants help on the 19th — are we free?"), so
  instead of a research brief the local model drafts *one* short, natural question
  (:func:`generate_question`) and it's texted to ``destination`` (a phone number)
  over the operator's Twilio account (:class:`~prefrontal.integrations.sms.TwilioConfig`).
  Same contract as ``email``: ends ``forwarded`` on send, and a bad number /
  unconfigured Twilio / transport error stores the drafted text and ends ``failed``
  so nothing is lost. One-way for now — the reply lands on the Twilio number, so you
  ``mark returned`` by hand (an inbound loop-closer is future work).

The prep generation mirrors the rest of the codebase's LLM usage
(:func:`prefrontal.todos.augment_todo` / :func:`~prefrontal.todos.decompose_task`):
one JSON call to the injected local model, a plain-language heuristic when it's
slow/down, and pure/testable throughout. New handlers register in ``_HANDLERS``;
``HANDLERS`` (the accepted set) is *derived* from it so the API boundary can't
drift out of sync with what's actually dispatchable — the same discipline as the
assistant's ``ALLOWED_OPS``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from prefrontal.autorun import Toolbox, answered_context, merge_questions, run_research
from prefrontal.clock import parse_ts as _parse_ts
from prefrontal.integrations import Generator
from prefrontal.integrations.base import ProviderError
from prefrontal.integrations.sms import TwilioConfig, TwilioSmsClient, normalize_phone
from prefrontal.integrations.smtp import SmtpClient
from prefrontal.llm_json import extract_json_object, fit_num_ctx
from prefrontal.log import get_logger
from prefrontal.sources import SmtpSource

if TYPE_CHECKING:
    from prefrontal.memory.store import MemoryStore

logger = get_logger(__name__)

#: Handler names (also the ``handler`` column values).
HANDLER_AGENT = "agent"
HANDLER_AUTO = "auto"
HANDLER_EMAIL = "email"
HANDLER_SMS = "sms"

#: Handlers that hand a todo to a *human* (someone you're waiting to hear back
#: from), as opposed to the on-box ``agent``/``auto``. These end at ``forwarded``
#: and drive the "heard back?" check-in and stalled-handoff escalation. Naming them
#: here keeps that shared behaviour from having to enumerate handlers by hand.
HUMAN_HANDLERS = frozenset({HANDLER_EMAIL, HANDLER_SMS})

#: Lifecycle statuses (the ``status`` column values).
STATUS_FORWARDED = "forwarded"
STATUS_IN_PREP = "in_prep"
STATUS_PREPPED = "prepped"
STATUS_RETURNED = "returned"
STATUS_FAILED = "failed"

#: An ``auto`` run stopped because it needs facts only the user has (their own
#: numbers, preferences, constraints) — the questions are on the row, waiting to be
#: answered inline or whenever. Deliberately **not** parked: the ball is with the
#: user, exactly like ``returned``/``failed``, so the check-in cadence surfaces it.
#: The partial write-up ships alongside, so this is never a bare "waiting".
STATUS_NEEDS_INPUT = "needs_input"

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

    ``forwarded`` → "heard back?" (worded for the handler — a texted question reads
    differently from a VA hand-off); ``prepped`` → "your prep is ready to review";
    ``in_prep`` → nothing (it's mid-flight, not worth a nudge).
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
        if delegation.get("handler") == HANDLER_SMS:
            who = f" {dest}" if dest else " them"
            return (
                f'You texted{who}{ago} to check on “{title}”. Heard back? Mark it '
                "returned once you know, or nudge them."
            )
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
        handler: The handler that ran (``agent`` / ``email`` / ``auto``).
        status: The lifecycle status the delegation ended at.
        brief: The prep write-up (may be a heuristic stub when the model is down).
        drafts: Drafted communications, each ``{channel, to, subject, body}``.
        actions: Extracted action items, each ``{text, mine}`` (``mine`` flags the
            ones the model attributes to the user — the dashboard offers to turn
            those into todos).
        detail: A human-readable note (transport response, failure reason, …).
        steps: For the ``auto`` handler, the executed tool calls (see
            :class:`prefrontal.autorun.RunStep`) — the inspectable trail of a run.
            Empty for the single-shot handlers.
        questions: For the ``auto`` handler, what the run needs from the user, each
            ``{text, why, answer}`` (``answer`` ``None`` until they reply). Non-empty
            exactly when the status is :data:`STATUS_NEEDS_INPUT`.
    """

    handler: str
    status: str
    brief: str
    drafts: list[dict[str, str]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)


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
    """Size the prep call's context window; see :func:`llm_json.fit_num_ctx`."""
    return fit_num_ctx(prompt_chars, cap=_PREP_MAX_NUM_CTX)


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

    One JSON call to the injected model (house style: catch
    :class:`~prefrontal.integrations.base.ProviderError`, tolerant JSON extraction,
    coerce, fall back — the client may be local or cloud, whichever the ``summarizer``
    agent resolved to). Two things
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
        except ProviderError:
            # ProviderError, not OllamaError: the injected client is whatever the
            # `summarizer` agent resolved to, so a cloud failure has to degrade to the
            # heuristic exactly like a local one (it used to escape and surface as
            # "prep failed unexpectedly").
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
            return _salvage_brief(reply), [], []
    return _heuristic_brief(title, notes, decomposition, context), [], []


#: Cap on an outbound SMS body (chars). A delegated question should be *one* short
#: text, not a wall — Twilio splits a long body into billed segments — so we trim
#: to a couple of segments' worth. Sizing, not truncation-as-feature: the composer
#: is told to be brief; this only guards a runaway model reply.
_SMS_MAX_CHARS = 320

_QUESTION_SYSTEM = (
    "You help someone send ONE short, friendly text message to a person in their "
    "life to get a quick answer they need before they can move a to-do forward "
    "(for example, checking a date with their partner). You are given the to-do and "
    "any note or context they added. Write the text the way they would — warm, "
    "natural, and to the point, ending in the actual question. No email-style "
    "greeting or sign-off, no subject line; a first name up front is fine if you're "
    'given one. Reply with ONLY a JSON object: {"message": "<the text to send>"}. '
    "Never invent specifics (a date, a place, a name) you weren't given — leave a "
    "clearly-marked [bracketed placeholder] where a real detail is missing so the "
    "user can fill it in before it sends."
)


def _heuristic_question(title: str, note: str | None = None) -> str:
    """A plain question text for the ``sms`` handler when the local model is down.

    No drafting is possible offline, so this leans on the user's own ``note`` (their
    framing of what to ask) and otherwise falls back to a neutral ask about the
    todo — never inventing wording or details it wasn't given.
    """
    if note and note.strip():
        return note.strip()[:_SMS_MAX_CHARS]
    return f"Quick question about “{title}” — can you let me know?"[:_SMS_MAX_CHARS]


def generate_question(
    title: str,
    *,
    note: str | None = None,
    context: str | None = None,
    client: Generator | None = None,
) -> str:
    """Draft the short SMS question to send for an ``sms`` delegation.

    The text-message analogue of :func:`generate_prep`, in the same house style —
    one JSON call to the injected local model, tolerant extraction, a plain-language
    heuristic when the model is slow/down — but it produces a single concise text
    rather than a research brief + drafts. The user's ``note`` is their own framing
    of the question and carries the most weight; ``context`` gives the model more to
    work with. Never raises: any model failure degrades to :func:`_heuristic_question`.

    Args:
        title: The todo text (what the question is in service of).
        note: The user's own phrasing of what to ask (given the most weight).
        context: Optional extra free-text context to inform the wording.
        client: An Ollama-like client; ``None`` uses the heuristic.
    """
    if client is not None:
        lines = [f"To-do: {title}"]
        if note:
            lines.append(f"What they want to ask (their words): {note}")
        if context:
            lines.append(f"Extra context: {context}")
        try:
            reply = client.generate("\n".join(lines), system=_QUESTION_SYSTEM)
        except ProviderError:
            reply = ""
        message = extract_json_object(reply).get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:_SMS_MAX_CHARS]
        # Salvage a prose reply (small local models often skip the JSON envelope)
        # rather than falling through to the terser heuristic.
        if reply and reply.strip():
            return reply.strip()[:_SMS_MAX_CHARS]
    return _heuristic_question(title, note)


#: Pulls the ``brief`` field out of a JSON-shaped reply that wouldn't *parse* — the
#: value runs to the quote that precedes the next top-level key (or the end).
_BRIEF_FIELD = re.compile(
    r'"brief"\s*:\s*"(.*?)"\s*(?:,\s*"(?:drafts|actions)"|,?\s*[}\]]\s*$)', re.DOTALL
)


def _salvage_brief(reply: str) -> str:
    """The best readable brief from a reply that didn't parse as JSON.

    :func:`~prefrontal.llm_json.extract_json` already repairs the common
    malformation (raw newlines inside string values), so reaching here means the reply
    is broken in some *other* way — an unterminated string, a stray quote, a truncated
    object. Storing that verbatim puts a wall of JSON on the todo card (seen live), so
    if the reply is visibly a JSON object with a ``brief`` field, lift the field out;
    otherwise keep the prose as-is, which is the case this salvage was built for.
    """
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)(?:```|\Z)", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if text.startswith("{") and '"brief"' in text:
        match = _BRIEF_FIELD.search(text)
        if match:
            # Unescape the JSON-ish escapes we're likely to see in a prose value; the
            # value never reached a JSON decoder, so nothing else did it for us.
            brief = (
                match.group(1)
                .replace("\\n", "\n").replace('\\"', '"').replace("\\t", "\t")
            ).strip()
            if brief:
                return brief
    return text


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
    va_note: str | None = None  # email: cover note atop the VA email; sms: what to ask
    owner_name: str | None = None  # user's display name (to flag their action items)
    destination: str | None = None  # email: VA address; sms: recipient phone number
    client: Generator | None = None  # local LLM for prep
    smtp: SmtpSource | None = None  # resolved SMTP source (email handler)
    smtp_client: SmtpClient | None = None
    sms: TwilioConfig | None = None  # resolved Twilio config (sms handler)
    sms_client: TwilioSmsClient | None = None
    toolbox: Toolbox | None = None  # unattended tools (auto handler); None = none
    #: Previously-asked questions the user has answered, each ``{text, why, answer}``
    #: — folded in as facts so a re-run doesn't re-ask them (auto handler).
    answered: list[dict[str, Any]] | None = None


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


def _join_context(*parts: str | None) -> str | None:
    """Blank-line-join the non-empty context blocks, or ``None`` if there are none."""
    kept = [p.strip() for p in parts if p and p.strip()]
    return "\n\n".join(kept) or None


class AutoHandler:
    """Auto mode: research the todo with tools first, *then* write the prep.

    A superset of :class:`AgentHandler` — it runs a bounded loop of allowlisted,
    unattended-declared MCP tool calls (:mod:`prefrontal.autorun`), folds what it
    learned into the context, and hands off to the same :func:`generate_prep`. So the
    brief/drafts/actions come back in the shape every surface already renders, and the
    only difference is that the write-up is informed by real lookups.

    It can also **ask**: when the task needs facts only the user has ("should I take a
    HELOC?" needs their equity and rate), the run stops, the questions go on the row,
    and the status becomes :data:`STATUS_NEEDS_INPUT` — with the partial write-up
    alongside, so answering is a choice rather than a toll gate. Answers arrive
    whenever (inline, from iOS, or in prose via the assistant) and a re-delegation
    folds them back in through :func:`~prefrontal.autorun.answered_context`.

    Phase 1 gathers information and delivers nothing: emailing the result or dropping
    a file in Drive is a *pre-authorized delivery contract* (phase 3), not a tool the
    loop may choose. With no tools enabled this degrades, honestly and silently, to
    exactly what the ``agent`` handler does.
    """

    kind = HANDLER_AUTO

    def run(self, req: DelegationRequest) -> DelegationResult:
        # Anything the user already answered is a *fact*, so it joins the context the
        # same way pasted material does — and it goes in before the loop starts, so
        # the run doesn't re-ask what it's already been told.
        answers = answered_context(req.answered)
        context = _join_context(req.context, answers)
        run = run_research(
            req.title,
            req.notes,
            context=context,
            client=req.client,
            toolbox=req.toolbox,
            # It has had its round of questions — press it to conclude rather than
            # asking a fresh one every time (observed against the local model).
            already_asked=bool(answers),
        )
        if run.findings:
            context = _join_context(
                context,
                "Material the assistant gathered for this task (tool results):\n"
                f"{run.findings}",
            )
        # Always write up what we have, even when parking for answers: a partial
        # report the user can read beats a bare "waiting on you".
        brief, drafts, actions = generate_prep(
            req.title, req.notes, req.decomposition,
            context=context, owner_name=req.owner_name, client=req.client,
        )
        offline = "Generated offline" in brief
        if run.questions:
            status = STATUS_NEEDS_INPUT
            detail = f"researched what it could — {run.detail}"
        else:
            status = STATUS_PREPPED
            if offline:
                detail = "prep drafted offline (heuristic)"
            elif run.calls:
                detail = f"researched the task — {run.detail}, then drafted the prep"
            else:
                # No calls: say why, since "auto mode did nothing autonomous" is
                # surprising unless it's visible (no tools enabled, model down, …).
                detail = f"prep drafted by the agent — no tools used ({run.detail})"
        return DelegationResult(
            handler=self.kind,
            status=status,
            brief=brief,
            drafts=drafts,
            actions=actions,
            detail=detail,
            steps=[s.as_dict() for s in run.steps],
            questions=[q.as_dict() for q in run.questions],
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


class SmsHandler:
    """Text a person a question: compose a short SMS and send it via Twilio.

    The follow-up-by-text case: a todo needs a quick answer from someone in your
    life ("Dad wants help on the 19th — are we free?") before you can move it
    forward, so instead of a VA research brief this composes *one* short, natural
    question (:func:`generate_question`) and texts it to ``destination`` — a phone
    number — on the operator's Twilio account (the same one household invites use).

    Mirrors :class:`EmailHandler`'s contract exactly: the composed text is always
    stored (as the ``brief``) so nothing is lost, and a missing/blank number,
    unconfigured Twilio, or a transport error lands ``failed`` with a clear,
    non-alarming reason rather than raising — you can still send the text by hand.
    A send Twilio accepts ends ``forwarded`` (parked, awaiting their reply). This is
    a one-way send: the reply arrives on the operator's Twilio number, so closing
    the loop is a manual "mark returned" for now (an inbound SMS loop-closer, the
    mirror of :func:`match_delegated_reply`, is future work).
    """

    kind = HANDLER_SMS

    def run(self, req: DelegationRequest) -> DelegationResult:
        message = generate_question(
            req.title, note=req.va_note, context=req.context, client=req.client
        )
        number = normalize_phone(req.destination)
        # No usable number: keep the composed text so it can be sent by hand, and
        # land in `failed` with a clear reason. Nothing is lost.
        if number is None:
            return DelegationResult(
                self.kind, STATUS_FAILED, message, [], [],
                "no valid phone number given — message stored for manual sending",
            )
        if req.sms is None or not req.sms.configured:
            return DelegationResult(
                self.kind, STATUS_FAILED, message, [], [],
                "texting isn't set up — message stored; configure Twilio to send texts",
            )
        client = req.sms_client or TwilioSmsClient()
        result = client.send(
            req.sms.account_sid,
            req.sms.auth_token,
            sender=req.sms.sender,
            to=number,
            body=message,
        )
        if not result.delivered:
            return DelegationResult(
                self.kind, STATUS_FAILED, message, [], [],
                f"text send failed ({result.detail}) — message stored for manual sending",
            )
        return DelegationResult(
            self.kind, STATUS_FORWARDED, message, [], [],
            f"texted {number} ({result.detail})",
        )


#: The dispatch registry. ``HANDLERS`` (the accepted set) is derived from it so a
#: new handler is enabled everywhere by adding one entry — the API can't accept a
#: handler name that has no implementation (mirrors the assistant's ALLOWED_OPS).
_HANDLERS: dict[str, DelegationHandler] = {
    HANDLER_AGENT: AgentHandler(),
    HANDLER_AUTO: AutoHandler(),
    HANDLER_EMAIL: EmailHandler(),
    HANDLER_SMS: SmsHandler(),
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
    sms: TwilioConfig | None = None,
    sms_client: TwilioSmsClient | None = None,
    toolbox: Toolbox | None = None,
    answered: list[dict[str, Any]] | None = None,
) -> DelegationResult:
    """Delegate ``todo`` to ``handler``, run the prep, and persist the result.

    Writes a ``todo_delegations`` row on the (scoped) ``store`` and returns the
    :class:`DelegationResult`. Raises :class:`ValueError` for an unknown handler
    (the caller — router/CLI — turns that into a 4xx). This call is *synchronous and
    can be slow* (a full-transcript prep evaluates a large context window on the
    local model), so the HTTP router runs it on a background thread after writing an
    ``in_prep`` row; the CLI just waits.

    ``smtp`` is only used by the ``email`` handler (resolve it via
    :func:`prefrontal.sources.resolve_smtp`); ``sms`` only by the ``sms`` handler
    (build it via :meth:`~prefrontal.integrations.sms.TwilioConfig.from_settings`).
    Both are resolved by the caller so this stays store-/settings-agnostic.
    ``toolbox`` is only used by the ``auto`` handler and is resolved the same way, by
    the caller — via :func:`prefrontal.autorun.build_toolbox` — so this function stays
    independent of the MCP/settings layer. Omitting it leaves auto mode with no tools
    (it then behaves like ``agent``), which is also the correct default for a caller
    that has no settings to hand.
    """
    impl = _HANDLERS.get(handler)
    if impl is None:
        raise ValueError(f"Unknown delegation handler: {handler!r}")
    # Canonicalize an sms recipient to E.164 once, here, so the value we *persist*
    # matches what actually gets texted — and an unusable string is stored as NULL
    # rather than leaking into the recent-numbers pick-list or the check-in copy.
    # (The handler also normalizes defensively; passing the canonical form through
    # makes that a no-op.) Other handlers keep their destination verbatim.
    if handler == HANDLER_SMS:
        destination = normalize_phone(destination)
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
        sms=sms,
        sms_client=sms_client,
        toolbox=toolbox,
        answered=answered,
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
        steps=result.steps,
        # Answered history survives the re-run; this round's asks are appended.
        questions=merge_questions(answered, result.questions),
        prepped=result.status in (STATUS_PREPPED, STATUS_FORWARDED),
    )
    return result


# -- Send a prepared draft (M4: "drafts → does") -------------------------------
#
# Delegation's ``agent`` handler *prepares* draft communications but never sends
# them — they sit on the todo for the user to copy out by hand. This is the first
# "does the thing" action (roadmap M4): promote a prepared **email** draft to an
# actually-sent message, over the same SMTP source the email handler uses, but
# only behind a deliberate two-phase gate that mirrors the NL-assistant's
# propose→apply contract:
#
#   preview_send(...)  → shows EXACTLY what would go out (recipient, subject,
#                        body, outbox) + a content digest, and refuses up front on
#                        any blocker (no email draft, no valid recipient, unfilled
#                        [placeholders], SMTP unconfigured);
#   send_prepared_draft(..., expected_digest=…)  → re-resolves from the *current*
#                        stored draft, refuses if the content changed since the
#                        preview (stale digest), and only then sends.
#
# The body/subject always come from the stored draft, never from the caller, so a
# caller can pin *which* message goes out (via the digest) and *who* it goes to (a
# validated recipient) but can't inject content. A single wrong autonomous send is
# the one thing that would cost the trust the product runs on — hence the belt and
# braces. Never a browser agent; a bounded, verifiable, confirmed action only.

#: The draft ``channel`` that can be sent as an email.
DRAFT_EMAIL_CHANNEL = "email"

#: A ``[bracketed placeholder]`` the prep leaves for a missing real detail. Its
#: presence blocks a send — a template with "[name]" must be filled in first.
_PLACEHOLDER_RE = re.compile(r"\[[^\[\]\n]{1,80}\]")

def _valid_email(addr: str) -> bool:
    """A deliberately-simple, *linear* "looks like an email" guard.

    Just enough to stop an obvious non-address (the prep often writes a name in
    ``to``); SMTP is the real validator. Hand-parsed rather than a regex to avoid
    ReDoS on the user-supplied recipient — one ``@``, a non-empty local part, and a
    dotted domain with non-empty labels, no whitespace.
    """
    addr = addr.strip()
    if not addr or any(ch.isspace() for ch in addr):
        return False
    local, sep, domain = addr.partition("@")
    if not sep or not local or "@" in domain:
        return False
    labels = domain.split(".")
    return len(labels) >= 2 and all(labels)


@dataclass(frozen=True)
class SendPreview:
    """What ``preview_send`` would send — shown to the user before they confirm.

    ``can_send`` is the single go/no-go; ``blockers`` explains any no. ``warnings``
    are non-blocking (e.g. "looks already sent"). ``digest`` pins the exact
    recipient+subject+body so a later ``send_prepared_draft`` can detect drift.
    """

    todo_id: int
    to: str
    subject: str
    body: str
    sender: str
    account: str
    can_send: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    digest: str = ""


@dataclass(frozen=True)
class SendOutcome:
    """The result of attempting a send.

    ``code`` discriminates the outcome for the caller/HTTP layer:
    ``sent`` (done), ``blocked`` (a precondition failed — fix and retry),
    ``already_sent`` (this draft was already delivered — refused to avoid a
    duplicate), ``stale`` (the draft changed since preview — re-preview),
    ``send_failed`` (the transport rejected it — the draft is kept for another try).
    """

    sent: bool
    code: str
    to: str
    status: str
    detail: str


def email_draft(drafts: Any) -> dict[str, str] | None:
    """Return the first sendable ``email`` draft (non-empty body), or ``None``."""
    if not isinstance(drafts, list):
        return None
    for d in drafts:
        if not isinstance(d, dict):
            continue
        if str(d.get("channel", "")).strip().lower() != DRAFT_EMAIL_CHANNEL:
            continue
        if str(d.get("body", "")).strip():
            return d
    return None


def has_placeholder(*texts: str) -> bool:
    """True if any text still carries an unfilled ``[bracketed placeholder]``."""
    return any(_PLACEHOLDER_RE.search(t or "") for t in texts)


def draft_digest(to: str, subject: str, body: str) -> str:
    """A stable content hash pinning exactly what would be sent (change-detection).

    Not a security token — auth is the scoped store; this only lets the confirm
    step refuse when the stored draft drifted between preview and send.
    """
    canon = "\x00".join((to.strip(), subject.strip(), body.strip()))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _already_sent(row: dict[str, Any] | None) -> bool:
    """Whether this delegation row already recorded a successful email send.

    A successful send marks the row :data:`STATUS_FORWARDED` with a
    ``"sent draft to …"`` detail (see :func:`send_prepared_draft`) — the auditable
    trace. That exact pairing is the durable "already sent" signal, and it can't
    collide with the *initial* handoff (which is also ``forwarded`` but carries a
    prep-summary detail, never ``"sent draft to"``). Re-delegating rewrites the
    row's status/detail, so a genuinely new draft reads as not-yet-sent again.
    """
    if row is None:
        return False
    return row.get("status") == STATUS_FORWARDED and "sent draft to" in (
        row.get("detail") or ""
    )


def _resolve_send(
    store: MemoryStore,
    todo_id: int,
    smtp: SmtpSource | None,
    recipient: str | None,
) -> tuple[dict[str, str] | None, str, str, str, str, str, list[str], list[str]]:
    """Shared resolution for preview + send: the draft, recipient, outbox, blockers.

    Returns ``(draft, to, subject, body, sender, account, blockers, warnings)``.
    ``draft`` is ``None`` when there's nothing to send; ``blockers`` is non-empty
    whenever the send can't proceed as-is.
    """
    blockers: list[str] = []
    warnings: list[str] = []
    empty = (None, "", "", "", "", "")
    row = store.get_delegation(todo_id)
    if row is None:
        return (*empty, ["this todo hasn't been delegated — there's no draft to send"], warnings)
    draft = email_draft(row.get("drafts"))
    if draft is None:
        return (*empty, ["the delegation prep has no email draft to send"], warnings)

    subject = str(draft.get("subject", "")).strip()
    body = str(draft.get("body", "")).strip()
    to = (recipient or "").strip() or str(draft.get("to", "")).strip()

    if not to:
        blockers.append("no recipient — the draft names no email address; supply one to send")
    elif not _valid_email(to):
        blockers.append(f'the recipient "{to}" isn\'t a valid email address; supply one')
    if has_placeholder(subject, body):
        blockers.append("the draft still has [bracketed placeholders] to fill in before sending")

    sender = ""
    account = ""
    if smtp is None or not smtp.configured:
        blockers.append("email isn't configured — set up an outbox in Settings to send")
    else:
        sender = (smtp.sender or smtp.username or "").strip()
        account = smtp.account or "default"

    if _already_sent(row):
        warnings.append(
            "this draft looks like it was already sent — sending again will send a duplicate"
        )

    return draft, to, subject, body, sender, account, blockers, warnings


def preview_send(
    store: MemoryStore,
    todo_id: int,
    *,
    smtp: SmtpSource | None,
    recipient: str | None = None,
) -> SendPreview:
    """Show exactly what sending this todo's email draft would do (no side effects).

    ``recipient`` optionally overrides the draft's own ``to`` (the prep often can't
    know the address). The returned ``digest`` should be echoed back to
    :func:`send_prepared_draft` so it can refuse a stale send.
    """
    draft, to, subject, body, sender, account, blockers, warnings = _resolve_send(
        store, todo_id, smtp, recipient
    )
    digest = draft_digest(to, subject, body) if draft is not None else ""
    return SendPreview(
        todo_id=todo_id,
        to=to,
        subject=subject,
        body=body,
        sender=sender,
        account=account,
        can_send=draft is not None and not blockers,
        blockers=blockers,
        warnings=warnings,
        digest=digest,
    )


def send_prepared_draft(
    store: MemoryStore,
    todo_id: int,
    *,
    smtp: SmtpSource | None,
    expected_digest: str | None = None,
    recipient: str | None = None,
    smtp_client: SmtpClient | None = None,
    allow_resend: bool = False,
) -> SendOutcome:
    """Send this todo's prepared email draft, then record the outcome on the row.

    Re-resolves from the *current* stored draft and refuses if a blocker is present
    (``blocked``), the draft was already sent (``already_sent``), or
    ``expected_digest`` no longer matches the content (``stale``) — the
    "re-verify before firing" guard. Subject/body are taken from the store, not
    the caller, so only *which* draft and *who* it goes to are caller-controlled.
    The transport never raises: a rejected send lands ``send_failed`` with the
    delegation marked ``failed`` (the draft is kept), a success marks it
    ``forwarded`` with a "sent draft to …" detail — the auditable trace, mirroring
    the email handler.

    An already-sent draft is refused (``already_sent``) rather than re-delivered:
    a client that retries after a timed-out-but-successful send would otherwise
    send a duplicate. Pass ``allow_resend=True`` to deliberately re-send the same
    stored draft (e.g. the first copy never arrived); regenerating the draft also
    clears the sent state.
    """
    draft, to, subject, body, _sender, _account, blockers, _warnings = _resolve_send(
        store, todo_id, smtp, recipient
    )
    current = store.get_delegation(todo_id) or {}
    current_status = current.get("status", "")
    if draft is None or blockers:
        return SendOutcome(
            sent=False,
            code="blocked",
            to=to,
            status=current_status,
            detail=blockers[0] if blockers else "nothing to send",
        )
    if _already_sent(current) and not allow_resend:
        return SendOutcome(
            sent=False,
            code="already_sent",
            to=to,
            status=current_status,
            detail="this draft was already sent — sending again would deliver a duplicate",
        )
    # ``None`` = an internal caller opting out of the digest check (CLI/tests); an
    # empty string is a *provided* digest that can't match, so it's refused — a
    # caller can't slip past the gate with "preview_digest": "".
    if expected_digest is not None and expected_digest != draft_digest(to, subject, body):
        return SendOutcome(
            sent=False,
            code="stale",
            to=to,
            status=current_status,
            detail="the draft changed since you previewed it — preview again before sending",
        )
    # smtp is non-None here (else it'd be a blocker above).
    assert smtp is not None
    client = smtp_client or SmtpClient()
    result = client.send(
        smtp.host,
        smtp.port,
        smtp.username,
        smtp.password,
        sender=smtp.sender,
        to=to,
        subject=subject,
        body=body,
        use_tls=smtp.use_tls,
    )
    if not result.delivered:
        detail = f"send failed ({result.detail}) — draft kept for another try"
        store.update_delegation_status(todo_id, STATUS_FAILED, detail=detail)
        return SendOutcome(
            sent=False, code="send_failed", to=to, status=STATUS_FAILED, detail=detail
        )
    detail = f"sent draft to {to} ({result.detail})"
    # prepped=False: don't re-stamp prepped_at — the prep completed earlier; a send
    # is a later event and shouldn't overwrite the original prep-completion time.
    store.update_delegation_status(todo_id, STATUS_FORWARDED, detail=detail, prepped=False)
    return SendOutcome(sent=True, code="sent", to=to, status=STATUS_FORWARDED, detail=detail)


def delegation_notice(todo_title: str, result: DelegationResult) -> str | None:
    """The push message when a delegation reaches a terminal state, or ``None``.

    ``prepped`` (agent) → "prep is ready to review"; ``forwarded`` → "sent to your
    assistant" (email) / "texted your question" (sms); ``needs_input`` (auto) → the
    questions it's waiting on; ``failed`` → a gentle heads-up that it needs a hand.
    ``None`` for non-terminal states (no push worth sending).
    """
    if result.status == STATUS_NEEDS_INPUT:
        pending = [q for q in result.questions if not q.get("answer")]
        n = len(pending)
        # Lead with the work already done, so this reads as progress rather than as a
        # request for homework — and name the first question, since one concrete
        # question is answerable from a notification while "3 questions" is a chore.
        first = f' First: {pending[0]["text"]}' if pending else ""
        return (
            f'Got a start on "{todo_title}" — it needs {n} thing{"s" if n != 1 else ""} '
            f"from you to finish.{first}"
        )
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
        if result.handler == HANDLER_SMS:
            return (
                f'Texted your question about "{todo_title}" — {result.detail}. '
                "Mark it returned once you hear back."
            )
        return f'Sent "{todo_title}" to your assistant — {result.detail}.'
    if result.status == STATUS_FAILED:
        return f'Couldn\'t hand off "{todo_title}": {result.detail}.'
    return None
