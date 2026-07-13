"""Resolve a double-booking by asking one side to move.

Prefrontal *detects* double-bookings (:func:`prefrontal.commitments.find_conflicts`)
but until now could only *dismiss* them. This module adds the delegation-style
resolution: pick which of two overlapping appointments to move, draft a short,
polite reschedule request to that appointment's other party with the local model,
and send it over the user's own SMTP source.

It deliberately rides the delegation rails rather than forking them:

- **Drafting** mirrors :func:`prefrontal.delegation.generate_prep` — one JSON call
  to the injected local model, tolerant extraction, an honest heuristic fallback,
  and *never raises*. A prose (non-JSON) reply is salvaged as the body rather than
  thrown away.
- **Sending** reuses :class:`prefrontal.integrations.smtp.SmtpClient` over the
  user's resolved :class:`prefrontal.sources.SmtpSource`, and the delegation
  ``email`` handler's ``forwarded``/``failed`` semantics: no SMTP or a send error
  keeps the draft (nothing is lost) and lands in ``failed`` with a clear reason.

Everything here is pure and unit-testable; the router/CLI wires in the store, the
model client, the SMTP source, and any candidate open slots (pre-formatted in the
user's local zone, so this module stays timezone-agnostic). Picking *which* side
to move is a suggestion (:func:`pick_move_side`) — the human confirms before
anything is sent (the endpoint only sends on an explicit ``send`` flag).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prefrontal.delegation import STATUS_FAILED, STATUS_FORWARDED
from prefrontal.integrations import Generator
from prefrontal.integrations.ollama import OllamaError
from prefrontal.integrations.smtp import SmtpClient
from prefrontal.llm_json import extract_json_object
from prefrontal.log import get_logger

if TYPE_CHECKING:
    from prefrontal.sources import SmtpSource

logger = get_logger(__name__)

#: Lifecycle statuses. ``forwarded``/``failed`` are shared with delegation (the
#: same "sent over SMTP" vs "kept for manual sending" meaning); ``drafted`` is the
#: preview state — a draft was produced but nothing was sent (the confirm-first
#: default, so a reschedule notice never leaves the box without an explicit send).
STATUS_DRAFTED = "drafted"

#: Hardness values (kept local so this module doesn't import the commitments layer
#: just for two constants; they match :data:`prefrontal.commitments.HARDNESS`).
_HARDNESS_HARD = "hard"


_DRAFT_SYSTEM = (
    "You are drafting a brief, courteous email on the user's behalf asking to "
    "reschedule one of their appointments because it double-books with another "
    "commitment. You are told which appointment to MOVE, what it clashes with, and "
    "sometimes a few alternative times the user is free. Reply with ONLY a JSON "
    'object of the form {"subject": "<a short subject line>", "body": "<the email '
    'body>"}. Keep it warm, short, and apologetic-but-not-grovelling; explain there '
    "is a scheduling conflict (do NOT disclose private details of the other "
    "commitment — \"a conflict has come up\" is enough), ask to find a new time, "
    "and if alternative times are given, offer them. Sign off as the user (you are "
    "told their name). Never invent facts you were not given — where a real detail "
    "is needed and missing, leave a clearly-marked [bracketed placeholder]."
)


@dataclass(frozen=True)
class RescheduleDraft:
    """A drafted reschedule request — the ``(subject, body)`` of the email."""

    subject: str
    body: str
    #: ``True`` when the offline heuristic produced this (the model was unavailable).
    offline: bool = False


@dataclass(frozen=True)
class RescheduleResult:
    """What a reschedule attempt produced (persisted-agnostic; never an exception).

    Attributes:
        status: ``drafted`` (previewed, nothing sent), ``forwarded`` (emailed), or
            ``failed`` (kept for manual sending — no SMTP or a send error).
        subject: The drafted subject line.
        body: The drafted body.
        recipient: The address the notice was (or would be) sent to.
        detail: A human-readable note (transport response, failure reason, …).
        offline: Whether the draft came from the offline heuristic.
    """

    status: str
    subject: str
    body: str
    recipient: str | None = None
    detail: str = ""
    offline: bool = False


def _movability_key(commitment: dict[str, Any]) -> tuple[int, str, int]:
    """Sort key ranking how *movable* a commitment is (larger = move this one).

    A double-booking is resolved by moving the side you'd more readily give up:

    1. a **soft** block is more movable than a **hard**, must-happen one (the whole
       point of ``hardness``);
    2. failing that, the **later-starting** of the two — nudging the second half of
       an overlap is the smaller disruption;
    3. failing that, the **later-added** row (higher id) — the newer entry is the
       likelier intruder on an established commitment.
    """
    hardness = (commitment.get("hardness") or "").strip().lower()
    hard = 1 if hardness == _HARDNESS_HARD else 0
    return (
        0 if hard else 1,  # soft (1) is more movable than hard (0)
        commitment.get("start_at") or "",  # later start sorts larger → more movable
        int(commitment.get("id") or 0),  # later-added larger → more movable
    )


def pick_move_side(
    a: dict[str, Any], b: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Suggest which appointment to move: ``(keep, move)``.

    Deterministic and symmetric — the side with the larger :func:`_movability_key`
    is the one to move; the other is kept. This is only a suggestion: the caller
    surfaces it and lets the user confirm or flip which one moves before anything
    is sent (deciding *which meeting matters more* is a human call).
    """
    if _movability_key(a) >= _movability_key(b):
        return b, a  # a is more (or equally) movable → move a, keep b
    return a, b


def _heuristic_draft(
    *,
    move_title: str,
    move_when: str | None,
    recipient_name: str | None,
    owner_name: str | None,
    slots: list[str] | None,
) -> RescheduleDraft:
    """A plain, honest draft when the local model is unavailable.

    Says nothing it wasn't given: names the appointment, that a conflict came up,
    and offers any supplied alternative times — leaving ``[your name]`` as a
    placeholder when the owner's name is unknown.
    """
    greeting = f"Hi {recipient_name}," if recipient_name else "Hi,"
    when = f" ({move_when})" if move_when else ""
    lines = [
        greeting,
        "",
        f"A scheduling conflict has come up and I'm no longer able to keep our "
        f"“{move_title}”{when}. I'm sorry for the inconvenience.",
    ]
    if slots:
        lines += [
            "",
            "Could we find another time? A few that work for me:",
            *(f"  - {s}" for s in slots),
        ]
    else:
        lines += ["", "Could we find another time that works for you?"]
    lines += ["", "Thank you!", owner_name or "[your name]"]
    return RescheduleDraft(
        subject=f"Reschedule request: {move_title}",
        body="\n".join(lines),
        offline=True,
    )


def build_reschedule_draft(
    *,
    move_title: str,
    move_when: str | None = None,
    recipient_name: str | None = None,
    owner_name: str | None = None,
    note: str | None = None,
    slots: list[str] | None = None,
    client: Generator | None = None,
) -> RescheduleDraft:
    """Draft a ``(subject, body)`` reschedule request for the appointment to move.

    House style (mirrors :func:`prefrontal.delegation.generate_prep`): one JSON
    call to the injected model, tolerant extraction, coerce, fall back — and never
    raise. A model that returns usable prose instead of JSON has that prose used as
    the body rather than discarded. With no ``client`` (or on any model failure)
    the :func:`_heuristic_draft` is returned.

    Args:
        move_title: Title of the appointment being moved.
        move_when: A pre-formatted local time for that appointment (the caller owns
            timezone rendering), or ``None``.
        recipient_name: The other party's display name, if known.
        owner_name: The user's display name (used to sign off).
        note: An optional cover note from the user to fold into the request.
        slots: Pre-formatted alternative-time labels to offer (local zone).
        client: A model client; ``None`` uses the heuristic.
    """
    if client is not None:
        prompt_lines = [f"Appointment to move: {move_title}"]
        if move_when:
            prompt_lines.append(f"Its current time: {move_when}")
        if recipient_name:
            prompt_lines.append(f"The other party is: {recipient_name}")
        if owner_name:
            prompt_lines.append(f"Sign the email as: {owner_name}")
        if note:
            prompt_lines.append(f"Personal note from the user to include: {note}")
        if slots:
            prompt_lines.append("Alternative times the user is free:")
            prompt_lines += [f"  - {s}" for s in slots]
        try:
            reply = client.generate("\n".join(prompt_lines), system=_DRAFT_SYSTEM)
        except OllamaError:
            reply = ""
        raw = extract_json_object(reply)
        subject = raw.get("subject")
        body = raw.get("body")
        if isinstance(body, str) and body.strip():
            subj = (
                subject.strip()
                if isinstance(subject, str) and subject.strip()
                else f"Reschedule request: {move_title}"
            )
            return RescheduleDraft(subject=subj, body=body.strip())
        # Salvage a prose reply as the body rather than parroting the heuristic.
        if reply and reply.strip():
            return RescheduleDraft(
                subject=f"Reschedule request: {move_title}", body=reply.strip()
            )
    return _heuristic_draft(
        move_title=move_title,
        move_when=move_when,
        recipient_name=recipient_name,
        owner_name=owner_name,
        slots=slots,
    )


def send_reschedule_draft(
    draft: RescheduleDraft,
    *,
    recipient: str | None,
    smtp: SmtpSource | None,
    smtp_client: SmtpClient | None = None,
) -> RescheduleResult:
    """Send a drafted reschedule request over the user's SMTP, or keep it.

    Mirrors :class:`prefrontal.delegation.EmailHandler`: with no recipient or no
    configured SMTP source the draft is kept and the result lands in ``failed``
    with a clear, non-alarming reason (nothing is lost — the user can send it by
    hand). A transport error is caught and returned, never raised. On success the
    result is ``forwarded``.
    """
    to = (recipient or "").strip()
    if not to:
        return RescheduleResult(
            STATUS_FAILED, draft.subject, draft.body, recipient=None,
            detail="no recipient address given — draft kept for manual sending",
            offline=draft.offline,
        )
    if smtp is None or not smtp.configured:
        return RescheduleResult(
            STATUS_FAILED, draft.subject, draft.body, recipient=to,
            detail="SMTP not configured — draft kept; configure email in Settings to send",
            offline=draft.offline,
        )
    client = smtp_client or SmtpClient()
    result = client.send(
        smtp.host,
        smtp.port,
        smtp.username,
        smtp.password,
        sender=smtp.sender,
        to=to,
        subject=draft.subject,
        body=draft.body,
        use_tls=smtp.use_tls,
    )
    if not result.delivered:
        return RescheduleResult(
            STATUS_FAILED, draft.subject, draft.body, recipient=to,
            detail=f"send failed ({result.detail}) — draft kept for manual sending",
            offline=draft.offline,
        )
    return RescheduleResult(
        STATUS_FORWARDED, draft.subject, draft.body, recipient=to,
        detail=f"emailed {to} ({result.detail})", offline=draft.offline,
    )
