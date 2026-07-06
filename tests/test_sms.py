"""Twilio SMS transport + the invite-SMS handoff.

The HTTP transport is exercised with an ``httpx.MockTransport`` so no real
Twilio request is made, matching ``test_delivery.py``.
"""
from __future__ import annotations

import httpx
import pytest

from prefrontal.config import Settings
from prefrontal.integrations.sms import (
    SmsResult,
    TwilioSmsClient,
    invite_sms_text,
    normalize_phone,
    send_invite_sms,
)

SID = "AC_test_sid"
TOKEN = "test_token"
FROM = "+14155550100"
TO = "+14155551234"


# -- normalize_phone ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+14155551234", "+14155551234"),
        ("+1 (415) 555-1234", "+14155551234"),
        ("415-555-1234", "4155551234"),
        ("  +14155551234  ", "+14155551234"),
        ("", None),
        (None, None),
        ("not a phone", None),
        ("+12", None),          # too short
        ("12345678901234567", None),  # too long
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


# -- invite_sms_text ----------------------------------------------------------


def test_invite_sms_text_with_link_and_household():
    body = invite_sms_text("PLUM-7F2Q", "https://box.example/kids?invite=PLUM-7F2Q",
                           household_name="The Kims")
    assert "The Kims" in body
    assert "https://box.example/kids?invite=PLUM-7F2Q" in body
    assert "PLUM-7F2Q" in body  # code is always included as the fallback


def test_invite_sms_text_without_link_falls_back_to_code():
    body = invite_sms_text("PLUM-7F2Q", "")
    assert "PLUM-7F2Q" in body
    assert "http" not in body  # no origin configured → no link, just the code


# -- TwilioSmsClient.send -----------------------------------------------------


def test_send_posts_to_twilio_with_auth_and_form():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.read().decode()
        return httpx.Response(201)

    client = TwilioSmsClient(transport=httpx.MockTransport(handler))
    result = client.send(SID, TOKEN, sender=FROM, to=TO, body="hi")

    assert result.delivered is True
    assert result.status_code == 201
    assert f"/Accounts/{SID}/Messages.json" in captured["url"]
    assert captured["auth"] is not None  # HTTP Basic (sid, token)
    assert "From=%2B14155550100" in captured["body"]  # url-encoded +14155550100
    assert "To=%2B14155551234" in captured["body"]
    assert "Body=hi" in captured["body"]


def test_send_no_ops_when_unconfigured():
    # No transport is even needed — a missing credential short-circuits the send.
    client = TwilioSmsClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    for args in (
        ("", TOKEN, FROM, TO),
        (SID, "", FROM, TO),
        (SID, TOKEN, "", TO),
        (SID, TOKEN, FROM, ""),
    ):
        result = client.send(args[0], args[1], sender=args[2], to=args[3], body="hi")
        assert result == SmsResult(detail="twilio: not configured")


def test_send_swallows_transport_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    client = TwilioSmsClient(transport=httpx.MockTransport(boom))
    result = client.send(SID, TOKEN, sender=FROM, to=TO, body="hi")
    assert result.delivered is False
    assert "failed" in result.detail


def test_send_reports_non_2xx():
    client = TwilioSmsClient(transport=httpx.MockTransport(lambda r: httpx.Response(400)))
    result = client.send(SID, TOKEN, sender=FROM, to=TO, body="hi")
    assert result.delivered is False
    assert result.status_code == 400


# -- send_invite_sms (orchestration) -----------------------------------------


def _settings(**kw) -> Settings:
    base = {"twilio_account_sid": SID, "twilio_auth_token": TOKEN, "twilio_from": FROM}
    base.update(kw)
    return Settings(**base)


def test_send_invite_sms_sends_the_join_link():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(201)

    result = send_invite_sms(
        _settings(),
        code="PLUM-7F2Q",
        join_url="https://box.example/kids?invite=PLUM-7F2Q",
        to=TO,
        household_name="The Kims",
        client=TwilioSmsClient(transport=httpx.MockTransport(handler)),
    )
    assert result.delivered is True
    # The composed invite text (url-encoded) rode in the Body field.
    assert "PLUM-7F2Q" in captured["body"]


def test_send_invite_sms_no_ops_when_twilio_unconfigured():
    # No transport needed: unconfigured settings short-circuit before any request.
    result = send_invite_sms(
        Settings(),  # no twilio_* set
        code="PLUM-7F2Q",
        join_url="https://box.example/kids?invite=PLUM-7F2Q",
        to=TO,
    )
    assert result.delivered is False
    assert "not configured" in result.detail


def test_send_invite_sms_rejects_bad_number_without_calling_twilio():
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach Twilio for a bad number")

    result = send_invite_sms(
        _settings(),
        code="PLUM-7F2Q",
        join_url="",
        to="not a phone",
        client=TwilioSmsClient(transport=httpx.MockTransport(boom)),
    )
    assert result.delivered is False
    assert "recipient" in result.detail
