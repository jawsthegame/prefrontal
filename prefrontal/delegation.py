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
from typing import TYPE_CHECKING, Any, Protocol

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

#: Draft channels a prep brief can produce.
_DRAFT_CHANNELS = ("email", "message", "call")

_PREP_SYSTEM = (
    "You are an executive assistant preparing to take a task off someone's plate. "
    "Given a task, produce the prep work that makes it ready to act on. Reply with "
    "ONLY a JSON object of the form "
    '{"brief": "<2-5 sentence write-up: what needs deciding, the realistic '
    'options, and any open questions or info to gather first>", '
    '"drafts": [{"channel": "email|message|call", "to": "<who, or empty>", '
    '"subject": "<for email; else empty>", "body": "<the drafted message or, for '
    'a call, a short call script>"}]}. '
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
        detail: A human-readable note (transport response, failure reason, …).
    """

    handler: str
    status: str
    brief: str
    drafts: list[dict[str, str]] = field(default_factory=list)
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


def _heuristic_brief(title: str, notes: str | None, decomposition: dict | None) -> str:
    """A plain, honest brief when the local model is unavailable.

    No research is possible offline, so this says so plainly and leans on whatever
    structure already exists (the notes and any decomposed steps) rather than
    inventing content.
    """
    lines = [
        f"Prep for: {title}.",
        "(Generated offline — the local model was unavailable, so this is a "
        "starting outline, not researched.)",
    ]
    if notes:
        lines.append(f"Context on file: {notes}")
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


def generate_prep(
    title: str,
    notes: str | None = None,
    decomposition: dict | None = None,
    *,
    client: Generator | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Produce a ``(brief, drafts)`` prep package for a task.

    One JSON call to the injected local model (house style: catch
    :class:`OllamaError`, tolerant JSON extraction, coerce, fall back). When the
    model is absent or unusable, returns a heuristic brief and no drafts — the
    delegation still records *something* actionable rather than nothing.

    Args:
        title: The task text.
        notes: Any free-text notes already on the todo (context for the model).
        decomposition: The todo's decomposition dict, if any (reused as scaffolding).
        client: An Ollama-like client; ``None`` uses the heuristic.
    """
    if client is not None:
        prompt = f"Task: {title}"
        if notes:
            prompt += f"\nNotes: {notes}"
        if decomposition and decomposition.get("first_step"):
            steps = [decomposition["first_step"], *(decomposition.get("steps") or [])]
            prompt += "\nKnown steps: " + "; ".join(str(s) for s in steps)
        try:
            reply = client.generate(prompt, system=_PREP_SYSTEM)
        except OllamaError:
            reply = ""
        raw = extract_json_object(reply)
        brief = raw.get("brief")
        if isinstance(brief, str) and brief.strip():
            return brief.strip(), _coerce_drafts(raw.get("drafts"))
    return _heuristic_brief(title, notes, decomposition), []


def compose_va_email(title: str, brief: str, drafts: list[dict[str, str]]) -> tuple[str, str]:
    """Compose the ``(subject, body)`` of the email sent to a human VA.

    Pure and unit-testable: leads with the task, includes the brief, and appends
    any drafted communications verbatim so the VA can send them with minimal edits.
    """
    subject = f"[Prefrontal] Please help with: {title}"
    parts = [
        f"Hi — could you take this off my plate?\n\nTask: {title}\n",
        f"Prep notes:\n{brief}\n",
    ]
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
        brief, drafts = generate_prep(
            req.title, req.notes, req.decomposition, client=req.client
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
            detail=detail,
        )


class EmailHandler:
    """Human VA: prep the todo, then email the brief + drafts to ``destination``."""

    kind = HANDLER_EMAIL

    def run(self, req: DelegationRequest) -> DelegationResult:
        brief, drafts = generate_prep(
            req.title, req.notes, req.decomposition, client=req.client
        )
        to = (req.destination or "").strip()
        # No SMTP configured (or no recipient): keep the brief so it can be sent by
        # hand, and land in `failed` with a clear, non-alarming reason. Nothing is lost.
        if not to:
            return DelegationResult(
                self.kind, STATUS_FAILED, brief, drafts,
                "no assistant email address given — brief stored for manual sending",
            )
        if req.smtp is None or not req.smtp.configured:
            return DelegationResult(
                self.kind, STATUS_FAILED, brief, drafts,
                "SMTP not configured — brief stored; configure email in Settings to send",
            )
        subject, body = compose_va_email(req.title, brief, drafts)
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
                self.kind, STATUS_FAILED, brief, drafts,
                f"send failed ({result.detail}) — brief stored for manual sending",
            )
        return DelegationResult(
            self.kind, STATUS_FORWARDED, brief, drafts, f"emailed {to} ({result.detail})"
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
    client: Generator | None = None,
    smtp: SmtpSource | None = None,
    smtp_client: SmtpClient | None = None,
) -> DelegationResult:
    """Delegate ``todo`` to ``handler``, run the prep, and persist the result.

    Writes a ``todo_delegations`` row on the (scoped) ``store`` and returns the
    :class:`DelegationResult`. Raises :class:`ValueError` for an unknown handler
    (the caller — router/CLI — turns that into a 4xx). The prep is synchronous:
    the local model call is sub-second-ish and the whole point is to hand back a
    ready result, so there's no background queue.

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
        detail=result.detail,
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
        n = len(result.drafts)
        drafts = f" ({n} draft{'s' if n != 1 else ''})" if n else ""
        return f'Prep ready for "{todo_title}"{drafts} — review it when you have a sec.'
    if result.status == STATUS_FORWARDED:
        return f'Sent "{todo_title}" to your assistant — {result.detail}.'
    if result.status == STATUS_FAILED:
        return f'Couldn\'t hand off "{todo_title}": {result.detail}.'
    return None
