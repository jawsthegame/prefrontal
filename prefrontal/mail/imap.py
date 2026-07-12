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
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from typing import Any

#: Default IMAP host when an account doesn't specify one.
DEFAULT_IMAP_HOST = "imap.gmail.com"

#: Default recency window (days) for the UNSEEN search. A large neglected inbox
#: can hold tens of thousands of unread; an unbounded ``SEARCH UNSEEN`` then
#: returns an id list that overruns imaplib's ~1 MB line limit (and triaging
#: years of dead mail into todos is pointless anyway). Bounding to recent unread
#: keeps the response small and the results actionable. ``None`` means no bound.
DEFAULT_UNSEEN_WINDOW_DAYS = 30

#: imaplib caps a single response line at 1 MB by default; a backlog of unread
#: ids can exceed that even within the window. Lift it defensively (belt-and-
#: suspenders alongside the recency bound) so a big-but-bounded list still reads.
_MAXLINE_FLOOR = 10_000_000


@dataclass(frozen=True)
class ImapAccount:
    """IMAP connection settings for one account.

    Attributes:
        name: The logical account name (matches the retention-policy config).
        host: IMAP server host.
        user: Login username (usually the full email address).
        password: Login password or app-password.
        mailbox: Mailbox to read (default ``INBOX``).
        important_only: When ``True`` and the account is Gmail, only messages
            Gmail flagged **Important** are fetched (via the ``X-GM-RAW`` search
            extension). Defaults to ``True`` for Gmail accounts; a no-op for
            non-Gmail hosts (which don't support the extension).
    """

    name: str
    host: str
    user: str
    password: str
    mailbox: str = "INBOX"
    important_only: bool = False

    @property
    def is_gmail(self) -> bool:
        """Whether this account talks to Gmail (so Gmail-only search applies)."""
        return "gmail" in self.host.lower()

    @classmethod
    def from_env(cls, name: str, env: dict[str, str] | None = None) -> ImapAccount | None:
        """Build an account from ``MAIL_IMAP_*_<ACCOUNT>`` env vars.

        Reads ``MAIL_IMAP_HOST_<NAME>`` (optional, defaults to
        :data:`DEFAULT_IMAP_HOST`), ``MAIL_IMAP_USER_<NAME>`` and
        ``MAIL_IMAP_PASSWORD_<NAME>`` (both required), optional
        ``MAIL_IMAP_MAILBOX_<NAME>``, and optional
        ``MAIL_IMAP_IMPORTANT_ONLY_<NAME>`` (defaults to on for Gmail accounts).
        ``<NAME>`` is the account name uppercased.

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
        host = env.get(f"MAIL_IMAP_HOST_{key}", DEFAULT_IMAP_HOST)
        override = _env_bool(env.get(f"MAIL_IMAP_IMPORTANT_ONLY_{key}"))
        # Default: Gmail accounts consider only Important mail; explicit env wins.
        important_only = ("gmail" in host.lower()) if override is None else override
        return cls(
            name=name,
            host=host,
            user=user,
            password=password,
            mailbox=env.get(f"MAIL_IMAP_MAILBOX_{key}", "INBOX"),
            important_only=important_only,
        )


def gmail_account_names(
    names: tuple[str, ...] | list[str], env: dict[str, str] | None = None
) -> frozenset[str]:
    """Return which of ``names`` are Gmail inboxes, by the same rule as an account.

    An account is Gmail when its resolved IMAP host contains ``gmail`` — the
    exact test :attr:`ImapAccount.is_gmail` applies. The host comes from
    ``MAIL_IMAP_HOST_<NAME>`` (name uppercased), defaulting to
    :data:`DEFAULT_IMAP_HOST` (which *is* Gmail). So an account with no explicit
    host — including one fed purely by n8n's Gmail node, which sets no IMAP env —
    counts as Gmail; point ``MAIL_IMAP_HOST_<NAME>`` at a non-Gmail server to opt
    a non-Gmail inbox out. This is the single source of truth for Gmail-ness,
    shared by the IMAP fetcher and the deep-link surfaces.

    Args:
        names: The logical account names to classify (e.g. from configured
            retention policies / label pills).
        env: Environment mapping (defaults to ``os.environ``).

    Returns:
        The subset of ``names`` that resolve to a Gmail host.
    """
    env = env if env is not None else dict(os.environ)
    return frozenset(
        name
        for name in names
        if "gmail" in env.get(f"MAIL_IMAP_HOST_{name.upper()}", DEFAULT_IMAP_HOST).lower()
    )


def fetch_unread(
    account: ImapAccount,
    *,
    limit: int = 50,
    since_days: int | None = DEFAULT_UNSEEN_WINDOW_DAYS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch unread messages from an account as raw dicts.

    Connects over IMAPS, selects the mailbox read-only (so fetching does not mark
    messages read — Prefrontal observes, it doesn't touch your unread state),
    searches for unread mail within the recency window, and returns the most
    recent ``limit`` of them.

    Args:
        account: The IMAP account to read.
        limit: Maximum number of (most recent) unread messages to return.
        since_days: Only consider unread mail newer than this many days
            (``SEARCH UNSEEN SINCE <date>``). Bounds the response size on a large
            neglected inbox and keeps results actionable. ``None`` searches all
            unread (use with care on big mailboxes).
        now: Reference "now" for the window (defaults to the current UTC time);
            injectable for tests.

    Returns:
        A list of raw message dicts ready for
        :func:`prefrontal.mail.models.normalize_message`.

    Raises:
        imaplib.IMAP4.error: On login or protocol failure.
    """
    # A bounded-but-still-large id list can exceed imaplib's default 1 MB line
    # cap; lift the floor before connecting.
    if imaplib._MAXLINE < _MAXLINE_FLOOR:
        imaplib._MAXLINE = _MAXLINE_FLOOR

    criteria = _unseen_criteria(since_days, now) + _important_filter(account)
    conn = imaplib.IMAP4_SSL(account.host)
    try:
        conn.login(account.user, account.password)
        # readonly=True: never change \Seen flags just by ingesting.
        conn.select(account.mailbox, readonly=True)
        typ, data = conn.search(None, *criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()
        if limit and len(ids) > limit:
            ids = ids[-limit:]  # most recent
        messages: list[dict[str, Any]] = []
        for num in ids:
            # Fetch the stable IMAP UID alongside the body: some list/automation
            # senders omit the Message-ID header, and without a fallback id
            # normalize_message would drop those messages as "invalid". The UID is
            # stable per mailbox, so it dedups a re-delivered header-less message.
            typ, msg_data = conn.fetch(num, "(UID RFC822)")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            meta, raw_bytes = msg_data[0]
            uid_match = re.search(rb"UID (\d+)", meta or b"")
            uid = uid_match.group(1).decode() if uid_match else None
            messages.append(_parse_rfc822(raw_bytes, account.name, uid=uid))
        return messages
    finally:
        try:
            conn.close()
        except imaplib.IMAP4.error:
            pass
        conn.logout()


def _unseen_criteria(since_days: int | None, now: datetime | None) -> tuple[str, ...]:
    """Build the IMAP SEARCH criteria for unread mail within the window.

    Returns ``("UNSEEN",)`` when unbounded, else ``("UNSEEN", "SINCE", <date>)``
    where date is IMAP's ``DD-Mon-YYYY`` form (e.g. ``31-May-2026``). The SINCE
    date is internal-date based and day-granular, which is exactly what bounding
    a backlog needs.
    """
    if not since_days or since_days <= 0:
        return ("UNSEEN",)
    ref = now or datetime.now(timezone.utc)
    since = (ref - timedelta(days=since_days)).strftime("%d-%b-%Y")
    return ("UNSEEN", "SINCE", since)


def _important_filter(account: ImapAccount) -> tuple[str, ...]:
    """Gmail Important-only SEARCH terms for an account, or empty.

    Returns ``("X-GM-RAW", '"is:important"')`` for a Gmail account with
    :attr:`ImapAccount.important_only` (ANDed onto the unseen criteria so only
    mail Gmail flagged Important comes back), else ``()``. The extension is
    Gmail-only, so it's never sent to other hosts.
    """
    if account.important_only and account.is_gmail:
        return ("X-GM-RAW", '"is:important"')
    return ()


def _env_bool(value: str | None) -> bool | None:
    """Parse a truthy/falsey env string, or ``None`` when unset/blank."""
    if value is None or not value.strip():
        return None
    return value.strip().lower() in ("1", "true", "yes", "on")


def _parse_rfc822(raw_bytes: bytes, account: str, *, uid: str | None = None) -> dict[str, Any]:
    """Parse raw RFC822 bytes into a normalize_message-shaped dict.

    ``uid`` is the message's IMAP UID, carried as a ``uid`` fallback so a message
    that omits the ``Message-ID`` header still gets a stable dedup id
    (``normalize_message`` uses ``uid`` only when ``message_id`` is absent).
    """
    msg = email.message_from_bytes(raw_bytes)
    return {
        "message_id": (msg.get("Message-ID") or "").strip() or None,
        "uid": f"imap-uid:{uid}" if uid else None,
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
