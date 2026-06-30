"""Normalized mail representation and inbound normalization.

A :class:`MailItem` is the single shape the rest of the pipeline reasons about,
regardless of where a message came from (n8n's Gmail node, the stdlib IMAP
fetcher, or a JSON file). :func:`normalize_message` maps a loosely-shaped raw
dict onto it and — critically — applies the account's **retention policy**:

- ``full`` keeps ``snippet`` and ``body``.
- ``signals`` drops both at the door, so a locked-down account's content is
  never stored *and* is never handed to the triage model. Only subject + sender
  + the derived verdict survive.

The normalizer is permissive about input keys (``from``/``sender``,
``body``/``text``/``snippet``, ``id``/``message_id``, ...) because providers and
n8n nodes disagree on names; it is strict about the one thing that matters —
there must be a stable ``message_id`` to dedup on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from prefrontal.commitments import to_utc

#: Valid retention policies (see :class:`prefrontal.config.Settings`).
RETENTION_POLICIES = ("full", "signals")


@dataclass(frozen=True)
class MailItem:
    """A normalized, policy-applied email ready for triage and storage.

    Attributes:
        account: The logical account name this message belongs to.
        message_id: Stable provider id, used for account-scoped dedup.
        policy: The retention policy applied (``full`` or ``signals``).
        thread_id: Provider thread id, if known.
        sender_name: Display name parsed from the ``From`` header.
        sender_email: Email address parsed from the ``From`` header.
        subject: Subject line.
        received_at: UTC ``YYYY-MM-DD HH:MM:SS`` timestamp, or ``None`` if the
            date was missing or unparseable (a bad date never drops a message).
        snippet: Short preview. ``None`` under the ``signals`` policy.
        body: Full text body. ``None`` under the ``signals`` policy.
        unread: Whether the message is unread, if known.
    """

    account: str
    message_id: str
    policy: str = "full"
    thread_id: str | None = None
    sender_name: str | None = None
    sender_email: str | None = None
    subject: str | None = None
    received_at: str | None = None
    snippet: str | None = None
    body: str | None = None
    unread: bool | None = None

    @property
    def sender(self) -> str:
        """A human-friendly sender string (``Name <email>`` / name / email / '')."""
        if self.sender_name and self.sender_email:
            return f"{self.sender_name} <{self.sender_email}>"
        return self.sender_name or self.sender_email or ""


def _first(raw: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-empty value among ``keys`` (else ``None``)."""
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_date(value: Any) -> str | None:
    """Normalize an email date to a UTC ``YYYY-MM-DD HH:MM:SS`` string.

    Accepts ISO-8601 (what n8n emits) and RFC 2822 (what raw email headers use,
    e.g. ``Mon, 29 Jun 2026 10:30:00 -0700``). Returns ``None`` for missing or
    unparseable input — a message with a bad date is still worth ingesting, so
    this never raises.

    Args:
        value: A date string (ISO-8601 or RFC 2822), or anything falsy.

    Returns:
        The normalized UTC timestamp, or ``None``.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    try:
        return to_utc(text)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_sender(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract ``(display_name, email)`` from a raw message's sender fields.

    Prefers explicit ``sender_name``/``sender_email`` (or ``from_name``/
    ``from_email``) when present, else parses a combined ``from``/``sender``
    header like ``"Sarah Lee" <sarah@example.com>`` with the stdlib parser.

    Args:
        raw: The raw message dict.

    Returns:
        ``(display_name or None, email or None)``.
    """
    name = _first(raw, "sender_name", "from_name", "fromName")
    email = _first(raw, "sender_email", "from_email", "fromEmail", "email")
    if name or email:
        return (name or None, (email or "").lower() or None)
    combined = _first(raw, "from", "sender", "From")
    if not combined:
        return (None, None)
    parsed_name, parsed_email = parseaddr(str(combined))
    return (parsed_name or None, parsed_email.lower() or None)


def normalize_message(
    raw: dict[str, Any], *, account: str, policy: str = "full"
) -> MailItem:
    """Normalize a raw message dict into a policy-applied :class:`MailItem`.

    Args:
        raw: A loosely-shaped message dict. Recognized keys (first present wins):
            id/message_id/messageId/uid; threadId/thread_id; from/sender (or
            split sender_name/sender_email); subject; date/received_at/internalDate;
            body/text/bodyText/plain; snippet/preview; unread/is_unread/seen.
        account: The logical account name the message belongs to.
        policy: ``full`` (keep snippet/body) or ``signals`` (drop both). An
            unrecognized value is treated as ``signals`` — the safe default.

    Returns:
        A :class:`MailItem`.

    Raises:
        ValueError: If no stable ``message_id`` can be found (dedup needs it).
    """
    if policy not in RETENTION_POLICIES:
        policy = "signals"

    message_id = _first(raw, "message_id", "messageId", "id", "uid")
    if message_id in (None, ""):
        raise ValueError("message is missing a stable id (message_id/id/uid)")

    name, email = parse_sender(raw)
    body = _first(raw, "body", "text", "bodyText", "plain")
    snippet = _first(raw, "snippet", "preview")
    # Derive a snippet from the body when the provider didn't supply one, so a
    # full-policy account still has a short preview for the dashboard.
    if snippet is None and body:
        snippet = " ".join(str(body).split())[:200]

    # `unread` can arrive as unread/is_unread (true=unread) or seen (true=read).
    unread = _first(raw, "unread", "is_unread", "isUnread")
    if unread is None and "seen" in raw:
        unread = not raw.get("seen")

    keep_content = policy == "full"
    return MailItem(
        account=account,
        message_id=str(message_id),
        policy=policy,
        thread_id=_str_or_none(_first(raw, "thread_id", "threadId")),
        sender_name=name,
        sender_email=email,
        subject=_str_or_none(_first(raw, "subject", "title")),
        received_at=normalize_date(
            _first(raw, "received_at", "date", "internalDate", "Date")
        ),
        snippet=str(snippet) if (keep_content and snippet is not None) else None,
        body=str(body) if (keep_content and body is not None) else None,
        unread=bool(unread) if unread is not None else None,
    )


def _str_or_none(value: Any) -> str | None:
    """Coerce a value to ``str``, mapping falsy/None to ``None``."""
    return str(value) if value not in (None, "") else None
