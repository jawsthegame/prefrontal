"""Triage a message into an actionable verdict.

Given a :class:`~prefrontal.mail.models.MailItem`, produce a :class:`MailTriage`:
does it need action, how urgent, what kind of message, who (if anyone) is
waiting on the user, and a one-line summary. This is the layer that turns a
stream of mail into the few things worth surfacing.

Two layers, mirroring the profile summarizer and the briefing:

- :func:`triage_message` asks a local Ollama model for a compact JSON verdict,
  grounded in the message's subject/sender (and body, only for ``full``-policy
  accounts where it was retained).
- :func:`_heuristic_triage` is the deterministic fallback used whenever the
  model is unavailable, errors, or returns unparseable output — so ingestion
  never hard-fails on a down model. It is also what runs in tests by default.

Nothing here makes a network call except the optional Ollama generate, which is
local. Message content never leaves the host.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prefrontal.mail.models import MailItem

if TYPE_CHECKING:
    from prefrontal.integrations.ollama import OllamaClient

#: Valid urgency levels, lowest to highest.
URGENCY_LEVELS = ("low", "normal", "high", "urgent")
#: Valid categories the triage may assign.
CATEGORIES = ("reply", "meeting", "fyi", "newsletter", "notification", "other")

#: Maps an urgency level to a ``todos.priority`` (0 low … 3 urgent).
_URGENCY_PRIORITY = {"low": 0, "normal": 1, "high": 2, "urgent": 3}

#: System prompt steering the model to a strict JSON verdict.
TRIAGE_SYSTEM_PROMPT = (
    "You are Prefrontal's mail triage. You are given one email's metadata (and "
    "body, if available). Decide whether it needs the user to act, how urgent it "
    "is, and what kind of message it is. Respond with ONLY a JSON object, no "
    "prose, with these keys: "
    '"needs_action" (boolean — does the user need to reply or do something?), '
    '"urgency" (one of "low","normal","high","urgent"), '
    '"category" (one of "reply","meeting","fyi","newsletter","notification","other"), '
    '"waiting_on" (string — who is waiting on the user, or "" if no one), '
    '"summary" (one short sentence, no more than 15 words). '
    "Newsletters, receipts, and automated notifications almost never need action. "
    "A direct question or request addressed to the user does."
)


@dataclass(frozen=True)
class MailTriage:
    """A triage verdict for one message.

    Attributes:
        needs_action: Whether the user must reply or do something.
        urgency: One of :data:`URGENCY_LEVELS`.
        category: One of :data:`CATEGORIES`.
        waiting_on: Free-text "who is waiting on the user", or ``None``.
        summary: A one-line summary suitable for a briefing/todo title.
        source: ``"llm"`` if the model produced it, ``"heuristic"`` otherwise.
    """

    needs_action: bool
    urgency: str = "normal"
    category: str = "other"
    waiting_on: str | None = None
    summary: str = ""
    source: str = "heuristic"

    @property
    def priority(self) -> int:
        """The ``todos.priority`` this verdict maps to (0 low … 3 urgent)."""
        return priority_for_urgency(self.urgency)


def priority_for_urgency(urgency: str | None) -> int:
    """Map an urgency level to a ``todos.priority`` (0–3), defaulting to normal."""
    return _URGENCY_PRIORITY.get((urgency or "").lower(), 1)


def build_corrections_block(
    senders: list[dict[str, Any]], examples: list[dict[str, Any]]
) -> str:
    """Render learned drop-corrections into a prompt addendum (``""`` if none).

    The output is appended to :data:`TRIAGE_SYSTEM_PROMPT` so triage's behavior
    evolves toward the user's own Drop feedback — without touching the base
    instructions or the message under review. Two kinds of signal:

    - ``senders``: from :meth:`MemoryStore.triage_dropped_senders` — repeat
      offenders, given as a "treat as no-action" hint list.
    - ``examples``: from :meth:`MemoryStore.triage_recent_quick_drops`, each a
      ``{"sender", "subject", "summary"}`` dict — recent quick drops, as
      negative few-shot examples.

    Kept compact and bounded by the callers' limits so it can't crowd out the
    message or push the model toward suppressing everything.
    """
    if not senders and not examples:
        return ""
    lines = [
        "",
        "The user has corrected past triage by dropping these flagged emails "
        "without acting. Use this to avoid repeating the same false positives — "
        "but still flag a clear, direct, personal request even from these senders.",
    ]
    if senders:
        lines.append(
            "Senders whose mail the user has repeatedly dropped (treat similar "
            "mail as NOT needing action):"
        )
        for s in senders:
            who = s.get("sender_email") or s.get("sender_name") or "unknown"
            drops = s.get("drops")
            lines.append(f"- {who}" + (f" ({drops} dropped)" if drops else ""))
    if examples:
        lines.append(
            "Recent emails the user dropped without acting (similar mail does "
            "not need action):"
        )
        for e in examples:
            subject = e.get("subject") or e.get("summary") or "(no subject)"
            who = e.get("sender") or "unknown"
            lines.append(f'- From {who} | Subject: "{subject}"')
    return "\n".join(lines)


def triage_message(
    item: MailItem,
    *,
    client: OllamaClient | None = None,
    fallback: bool = True,
    use_model: bool = True,
    corrections: str = "",
) -> MailTriage:
    """Triage one message, preferring the local model with a heuristic fallback.

    Args:
        item: The normalized message. For a ``signals``-policy account its body
            is already ``None``, so triage runs on subject + sender only.
        client: An Ollama client. Defaults to one built from settings.
        fallback: If ``True`` (default), fall back to :func:`_heuristic_triage`
            on any model failure or unparseable output; if ``False``, re-raise
            the underlying :class:`~prefrontal.integrations.ollama.OllamaError`.
        use_model: If ``False``, skip the model entirely and triage with the
            keyword heuristic. Useful for clearing a large backlog of existing
            unread fast, without spinning the model up per message.
        corrections: A learned-corrections addendum (see
            :func:`build_corrections_block`) appended to the system prompt so the
            model adapts to the user's Drop feedback. Empty string = base prompt.
            Only affects the model path; the heuristic fallback ignores it.

    Returns:
        A :class:`MailTriage`.

    Raises:
        prefrontal.integrations.ollama.OllamaError: If the model fails and
            ``fallback`` is ``False``.
    """
    from prefrontal.integrations.ollama import OllamaClient, OllamaError

    if not use_model:
        return _heuristic_triage(item)

    client = client or OllamaClient.from_settings()
    prompt = _build_prompt(item)
    try:
        raw = client.generate(prompt, system=TRIAGE_SYSTEM_PROMPT + corrections)
    except OllamaError:
        if not fallback:
            raise
        return _heuristic_triage(item)

    parsed = _parse_verdict(raw)
    if parsed is None:
        if not fallback:
            raise OllamaError("Mail triage model returned unparseable output.")
        return _heuristic_triage(item)
    return parsed


def _build_prompt(item: MailItem) -> str:
    """Render the message into the user-prompt text fed to the model."""
    lines = [
        f"From: {item.sender or '(unknown)'}",
        f"Subject: {item.subject or '(no subject)'}",
    ]
    if item.body:
        # Cap the body so a long thread can't blow the context window; the head
        # carries the ask in almost all real mail.
        lines.append("")
        lines.append(item.body.strip()[:4000])
    elif item.snippet:
        lines.append("")
        lines.append(item.snippet.strip())
    return "\n".join(lines)


def _parse_verdict(raw: str) -> MailTriage | None:
    """Parse a model reply into a :class:`MailTriage`, or ``None`` if unusable.

    Tolerant of the model wrapping JSON in prose or code fences: it extracts the
    first ``{...}`` block. Coerces/validates each field to a safe enum value.
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    urgency = str(data.get("urgency", "normal")).lower().strip()
    if urgency not in URGENCY_LEVELS:
        urgency = "normal"
    category = str(data.get("category", "other")).lower().strip()
    if category not in CATEGORIES:
        category = "other"
    waiting = str(data.get("waiting_on", "") or "").strip() or None
    summary = " ".join(str(data.get("summary", "") or "").split())[:200]

    return MailTriage(
        needs_action=bool(data.get("needs_action", False)),
        urgency=urgency,
        category=category,
        waiting_on=waiting,
        summary=summary,
        source="llm",
    )


#: Subjects/senders that signal a no-action bulk message.
_BULK_MARKERS = (
    "unsubscribe",
    "newsletter",
    "no-reply",
    "noreply",
    "donotreply",
    "do-not-reply",
    "notification",
    "receipt",
    "order confirmation",
    "digest",
    "weekly update",
)
#: Words that, in a subject, bump urgency.
_URGENT_MARKERS = (
    "urgent", "asap", "immediately", "eod", "by end of day", "deadline", "overdue",
)
_HIGH_MARKERS = (
    "today", "tomorrow", "reminder", "action required", "response needed", "please review",
)
#: Phrasing that signals the sender wants something back from the user.
_ACTION_MARKERS = (
    "?",
    "please",
    "can you",
    "could you",
    "let me know",
    "thoughts",
    "approve",
    "review",
    "sign",
    "confirm",
    "reply",
    "respond",
    "follow up",
    "follow-up",
    "waiting on",
    "waiting for",
)


def _heuristic_triage(item: MailItem) -> MailTriage:
    """Keyword-based triage used when the model is unavailable.

    Deliberately conservative: bulk/automated mail is marked no-action, and
    anything with a question or request to the user is flagged for a reply with
    urgency lifted by explicit time pressure in the subject.
    """
    subject = (item.subject or "").lower()
    sender = (item.sender_email or item.sender or "").lower()
    haystack = f"{subject}\n{(item.body or item.snippet or '').lower()}"

    is_bulk = any(m in subject or m in sender for m in _BULK_MARKERS)
    if is_bulk:
        is_newsletter = "newsletter" in haystack or "unsubscribe" in haystack
        category = "newsletter" if is_newsletter else "notification"
        return MailTriage(
            needs_action=False,
            urgency="low",
            category=category,
            summary=_clip(item.subject) or "Bulk/automated message",
            source="heuristic",
        )

    is_meeting = any(m in subject for m in ("invitation:", "meeting", "calendar", "invite", "rsvp"))
    wants_action = any(m in haystack for m in _ACTION_MARKERS)

    urgency = "normal"
    if any(m in subject for m in _URGENT_MARKERS):
        urgency = "urgent"
    elif any(m in subject for m in _HIGH_MARKERS):
        urgency = "high"

    if is_meeting:
        category = "meeting"
        needs_action = True
    elif wants_action:
        category = "reply"
        needs_action = True
    else:
        category = "fyi"
        needs_action = False
        urgency = "low" if urgency == "normal" else urgency

    waiting_on = item.sender_name or item.sender_email if needs_action else None
    return MailTriage(
        needs_action=needs_action,
        urgency=urgency,
        category=category,
        waiting_on=waiting_on,
        summary=_clip(item.subject) or "(no subject)",
        source="heuristic",
    )


def _clip(text: str | None, limit: int = 120) -> str:
    """Collapse whitespace and clip ``text`` to ``limit`` chars (``""`` if None)."""
    if not text:
        return ""
    collapsed = " ".join(text.split())
    return collapsed[:limit]
