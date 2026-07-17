"""Outbound email over SMTP — deliver a delegated todo's prep brief to a human assistant.

A tiny, credential-free-at-construction transport in the same shape as the other
integration clients (:class:`~prefrontal.integrations.sms.TwilioSmsClient`,
:class:`~prefrontal.delivery.NtfyClient`): the SMTP account
credentials ride in per :meth:`send`, so one client serves any user's outbox.
Local-first, like every other integration client — with nothing configured it
runs in **no-op/log mode** and nothing leaves the host, and transport errors are
caught and returned as an :class:`SmtpResult`, never raised (a down relay must
not sink the caller that was only trying to hand off a todo).

Unlike the inbound mail path (stdlib ``imaplib`` in :mod:`prefrontal.mail.imap`),
this is Prefrontal's only *outbound* email surface. It exists for the delegation
``email`` handler: when a user forwards a todo to their virtual assistant, the
prep brief is mailed here over the user's own SMTP source (configured on the
dashboard, sealed at rest — see :func:`prefrontal.sources.put_smtp_source`).

Tests inject a ``connect`` factory returning a fake SMTP context manager, matching
how the delivery-layer clients inject an ``httpx`` transport.
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage

from prefrontal.log import get_logger

logger = get_logger(__name__)

#: An object with the subset of :class:`smtplib.SMTP` this client drives, usable
#: as a context manager. The default factory returns a real ``smtplib.SMTP``;
#: tests pass a fake exposing the same methods.
SmtpConn = smtplib.SMTP

#: Factory signature: ``(host, port, timeout) -> SmtpConn``. Injectable for tests.
ConnectFn = Callable[[str, int, float], SmtpConn]

#: SMTPS / implicit-TLS port: the socket is TLS from the first byte, so it uses
#: ``SMTP_SSL`` and must NOT also issue STARTTLS. Every other port uses plain
#: ``SMTP`` with an optional STARTTLS upgrade. Common for app-password relays
#: (many hosts, and Gmail's SSL endpoint) that offer only 465, not 587.
IMPLICIT_TLS_PORT = 465


def _default_connect(host: str, port: int, timeout: float) -> SmtpConn:
    """Open a real SMTP connection (the production ``connect`` factory).

    Port 465 is implicit-TLS (SMTPS), so it connects with ``SMTP_SSL``; every
    other port uses plain ``SMTP`` (STARTTLS is applied in :meth:`SmtpClient.send`
    when requested).
    """
    if port == IMPLICIT_TLS_PORT:
        return smtplib.SMTP_SSL(host, port, timeout=timeout)
    return smtplib.SMTP(host, port, timeout=timeout)


@dataclass(frozen=True)
class SmtpResult:
    """Outcome of one send attempt (never an exception — see module docs).

    Attributes:
        delivered: ``True`` only if the relay accepted the message.
        detail: Human-readable note for logs, the CLI, and the delegation row.
    """

    delivered: bool = False
    detail: str = ""


class SmtpClient:
    """Send an email via SMTP.

    Credential-free at construction — host / port / username / password / from
    ride in per :meth:`send`, so one client can serve any user's SMTP source.
    Tests inject a ``connect`` factory, matching the delivery-layer clients'
    ``httpx`` transport injection.
    """

    def __init__(
        self, timeout: float = 15.0, connect: ConnectFn | None = None
    ) -> None:
        self.timeout = timeout
        self._connect = connect or _default_connect

    def send(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        sender: str,
        to: str,
        subject: str,
        body: str,
        use_tls: bool = True,
    ) -> SmtpResult:
        """Send one plain-text email.

        No-ops (nothing sent) unless a host, a from-address, and a recipient are
        all present — so a box with SMTP unconfigured simply reports "not
        configured" instead of erroring. Transport: port 465 is implicit TLS
        (``SMTP_SSL``, no STARTTLS); on any other port STARTTLS is attempted when
        ``use_tls`` (the norm for 587). Login is skipped when no username/password
        is given (an open relay / local MTA). Any SMTP or socket error is caught
        and returned, never raised.
        """
        if not (host and sender and to):
            return SmtpResult(detail="smtp: not configured")
        message = EmailMessage()
        message["From"] = sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        try:
            with self._connect(host, port, self.timeout) as conn:
                # Port 465 is already TLS (SMTP_SSL); STARTTLS on it would error.
                if use_tls and port != IMPLICIT_TLS_PORT:
                    conn.starttls()
                if username and password:
                    conn.login(username, password)
                conn.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:  # relay down, auth, TLS, …
            logger.warning("smtp send failed: %s", exc)
            return SmtpResult(detail=f"smtp send failed: {exc}")
        return SmtpResult(delivered=True, detail="smtp accepted the message")
