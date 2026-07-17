"""Twilio SMS — text a household invite link to a co-parent.

A tiny, credential-free-at-construction transport in the same shape as the
delivery-layer clients (:class:`~prefrontal.delivery.TwilioVoiceClient`
/ :class:`~prefrontal.delivery.NtfyClient`): an ``httpx``-based POST
to Twilio's REST API, no SDK
dependency, tests inject a transport. Local-first, like every other integration
client — with nothing configured it runs in **no-op/log mode** and nothing
leaves the host, and transport errors are caught and returned as an
:class:`SmsResult`, never raised (a down transport must not sink the caller).

Its one job today is the invite handoff: when a parent creates a household
invite, :func:`send_invite_sms` texts the join link straight to the person being
invited, so onboarding a co-parent doesn't depend on copy-pasting a code out of
band. The Twilio *account* credentials are the operator's (the same account the
n8n voice-call escalation uses, see ``docs/deployment.md``); the *recipient*
number is supplied per invite and never stored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from prefrontal.config import Settings, get_settings
from prefrontal.log import get_logger

logger = get_logger(__name__)

#: Twilio REST API root (per-account message resource is ``/Accounts/{sid}/Messages.json``).
_API_ROOT = "https://api.twilio.com/2010-04-01"

#: A lenient E.164-ish check: an optional ``+`` then 7–15 digits, after stripping
#: the spaces/dashes/parens humans type. Deep validation is Twilio's job — this
#: only rejects input that plainly isn't a phone number so the caller can 422
#: rather than fire a doomed request.
_PHONE_RE = re.compile(r"^\+?[0-9]{7,15}$")


def normalize_phone(raw: str | None) -> str | None:
    """Return an E.164-ish phone number (spaces/dashes/parens stripped), or ``None``.

    ``None`` when the input is blank or plainly not a phone number, so the caller
    can reject it up front instead of handing Twilio a value it will only bounce.
    """
    if not raw:
        return None
    cleaned = re.sub(r"[\s()\-.]", "", raw.strip())
    return cleaned if _PHONE_RE.match(cleaned) else None


@dataclass(frozen=True)
class SmsResult:
    """Outcome of one SMS send attempt (never an exception — see module docs).

    Attributes:
        delivered: ``True`` only if Twilio accepted the message (2xx).
        status_code: HTTP status from Twilio, or ``None`` when nothing was sent.
        detail: Human-readable note for logs and the CLI.
    """

    delivered: bool = False
    status_code: int | None = None
    detail: str = ""


class TwilioSmsClient:
    """Send an SMS via Twilio's REST API.

    Credential-free at construction — the account SID / auth token / from-number
    ride in per :meth:`send`, so one client can serve any configuration. Tests
    inject an ``httpx`` transport, matching the delivery-layer clients.
    """

    def __init__(self, timeout: float = 10.0, transport: httpx.BaseTransport | None = None) -> None:
        self.timeout = timeout
        self._transport = transport

    def send(
        self,
        account_sid: str,
        auth_token: str,
        *,
        sender: str,
        to: str,
        body: str,
    ) -> SmsResult:
        """POST one message to Twilio.

        No-ops (nothing sent) unless the account SID, auth token, from-number, and
        recipient are all present — so a box with Twilio unconfigured simply
        reports "not configured" instead of erroring.
        """
        if not (account_sid and auth_token and sender and to):
            return SmsResult(detail="twilio: not configured")
        url = f"{_API_ROOT}/Accounts/{account_sid}/Messages.json"
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                resp = client.post(
                    url,
                    data={"From": sender, "To": to, "Body": body},
                    auth=(account_sid, auth_token),
                )
        except httpx.HTTPError as exc:  # network down, DNS, timeout, …
            logger.warning("twilio SMS delivery failed: %s", exc)
            return SmsResult(detail=f"twilio request failed: {exc}")
        return SmsResult(
            delivered=resp.is_success,
            status_code=resp.status_code,
            detail=f"twilio responded {resp.status_code}",
        )


def invite_sms_text(code: str, join_url: str, *, household_name: str | None = None) -> str:
    """Compose the invite SMS body (pure — the whole thing is unit-testable).

    Leads with who/what, carries the tap-to-join link when a public origin is
    configured, and always includes the raw code as the fallback for a deployment
    with no ``oauth_base_url`` (the link would be empty there).
    """
    where = f" to {household_name}" if household_name else ""
    lead = f"You've been invited{where} on Prefrontal."
    if join_url:
        return f"{lead} Tap to join: {join_url} (or enter code {code})"
    return f"{lead} Open the app and enter invite code {code} to join."


def send_invite_sms(
    settings: Settings | None,
    *,
    code: str,
    join_url: str,
    to: str,
    household_name: str | None = None,
    client: TwilioSmsClient | None = None,
) -> SmsResult:
    """Text an invite's join link to ``to`` on the operator's Twilio account.

    Orchestration shared by the ``POST /household/invites`` endpoint and the
    ``prefrontal household invite --sms`` CLI, so the "compose + route + send"
    path lives in one place (mirroring :func:`delivery.deliver_to_household`).
    Never raises — returns a no-op :class:`SmsResult` when Twilio isn't configured
    or the number is unusable, so a failed text never sinks invite creation.
    """
    resolved = settings or get_settings()
    number = normalize_phone(to)
    if number is None:
        return SmsResult(detail="sms: no valid recipient number")
    client = client or TwilioSmsClient()
    body = invite_sms_text(code, join_url, household_name=household_name)
    return client.send(
        resolved.twilio_account_sid,
        resolved.twilio_auth_token,
        sender=resolved.twilio_from,
        to=number,
        body=body,
    )
