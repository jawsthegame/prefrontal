"""Optional dependency-free IMAP fetcher (a no-n8n ingestion path).

Most deployments let n8n's Gmail node fetch mail and POST it to
``/webhooks/mail/sync``, so OAuth lives in n8n. This module is the alternative
for when you'd rather Prefrontal pull directly: it uses only the standard
library (:mod:`imaplib` + :mod:`email`), so it adds no dependency and no OAuth
dance. Gmail works with an **app password** (with 2FA enabled) against
``imap.gmail.com``.

It returns raw message dicts in the shape
:func:`prefrontal.mail.models.normalize_message` expects, so the output flows
straight into :func:`prefrontal.mail.ingest.ingest_messages`. Credentials are
read per-account from the environment (``MAIL_IMAP_*_<ACCOUNT>``) so they never
live in the repo.
"""

from __future__ import annotations

import email
import imaplib
import os
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from typing import Any

#: Default IMAP host when an account doesn't specify one.
DEFAULT_IMAP_HOST = "imap.gmail.com"


@dataclass(frozen=True)
class ImapAccount:
    """IMAP connection settings for one account.

    Attributes:
        name: The logical account name (matches the retention-policy config).
        host: IMAP server host.
        user: Login username (usually the full email address).
        password: Login password or app-password.
        mailbox: Mailbox to read (default ``INBOX``).
    """

    name: str
    host: str
    user: str
    password: str
    mailbox: str = "INBOX"

    @classmethod
    def from_env(cls, name: str, env: dict[str, str] | None = None) -> ImapAccount | None:
        """Build an account from ``MAIL_IMAP_*_<ACCOUNT>`` env vars.

        Reads ``MAIL_IMAP_HOST_<NAME>`` (optional, defaults to
        :data:`DEFAULT_IMAP_HOST`), ``MAIL_IMAP_USER_<NAME>`` and
        ``MAIL_IMAP_PASSWORD_<NAME>`` (both required), and optional
        ``MAIL_IMAP_MAILBOX_<NAME>``. ``<NAME>`` is the account name uppercased.

        Args:
            name: The logical account name.
            env: Environment mapping (defaults to ``os.environ``).

        Returns:
            An :class:`ImapAccount`, or ``None`` if user/password are not set.
        """
        env = env if env is not None else dict(os.environ)
        key = name.upper()
        user = env.get(f"MAIL_IMAP_USER_{key}")
        password = env.get(f"MAIL_IMAP_PASSWORD_{key}")
        if not user or not password:
            return None
        return cls(
            name=name,
            host=env.get(f"MAIL_IMAP_HOST_{key}", DEFAULT_IMAP_HOST),
            user=user,
            password=password,
            mailbox=env.get(f"MAIL_IMAP_MAILBOX_{key}", "INBOX"),
        )


def fetch_unread(account: ImapAccount, *, limit: int = 50) -> list[dict[str, Any]]:
    """Fetch unread messages from an account as raw dicts.

    Connects over IMAPS, selects the mailbox read-only (so fetching does not mark
    messages read — Prefrontal observes, it doesn't touch your unread state),
    searches ``UNSEEN``, and returns the most recent ``limit`` of them.

    Args:
        account: The IMAP account to read.
        limit: Maximum number of (most recent) unread messages to return.

    Returns:
        A list of raw message dicts ready for
        :func:`prefrontal.mail.models.normalize_message`.

    Raises:
        imaplib.IMAP4.error: On login or protocol failure.
    """
    conn = imaplib.IMAP4_SSL(account.host)
    try:
        conn.login(account.user, account.password)
        # readonly=True: never change \Seen flags just by ingesting.
        conn.select(account.mailbox, readonly=True)
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()
        if limit and len(ids) > limit:
            ids = ids[-limit:]  # most recent
        messages: list[dict[str, Any]] = []
        for num in ids:
            typ, msg_data = conn.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            messages.append(_parse_rfc822(msg_data[0][1], account.name))
        return messages
    finally:
        try:
            conn.close()
        except imaplib.IMAP4.error:
            pass
        conn.logout()


def _parse_rfc822(raw_bytes: bytes, account: str) -> dict[str, Any]:
    """Parse raw RFC822 bytes into a normalize_message-shaped dict."""
    msg = email.message_from_bytes(raw_bytes)
    return {
        "message_id": (msg.get("Message-ID") or "").strip() or None,
        "from": _decode(msg.get("From")),
        "subject": _decode(msg.get("Subject")),
        "date": msg.get("Date"),
        "body": _extract_body(msg),
        "unread": True,  # we searched UNSEEN
    }


def _decode(value: str | None) -> str | None:
    """Decode an RFC 2047 encoded-word header to a plain string."""
    if not value:
        return None
    try:
        return str(make_header(decode_header(value)))
    except (ValueError, LookupError):
        return value


def _extract_body(msg: Message) -> str | None:
    """Return the message's plain-text body (first ``text/plain`` part)."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                part.get("Content-Disposition", "")
            ):
                return _decode_payload(part)
        return None
    if msg.get_content_type() == "text/plain":
        return _decode_payload(msg)
    return None


def _decode_payload(part: Message) -> str | None:
    """Decode a part's payload bytes to text using its declared charset."""
    payload = part.get_payload(decode=True)
    if payload is None:
        return None
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, ValueError):
        return payload.decode("utf-8", errors="replace")
