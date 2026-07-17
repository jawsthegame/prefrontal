"""APNs provider-token + payload builders, and the delivery integration.

The network send needs HTTP/2 (the ``apns`` extra) and real Apple creds, so it
isn't exercised here; instead we test the pure builders, the transport's guards,
per-user token routing, and that the router prefers APNs when a token is set.
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from prefrontal.coaching import Cue, Decision
from prefrontal.config import Settings
from prefrontal.delivery import (
    ApnsClient,
    DeliveryClient,
    DeliveryResult,
    Route,
    resolve_route,
)
from prefrontal.integrations.apns import build_apns_jwt, build_apns_payload
from prefrontal.memory.store import MemoryStore, provision_user
from tests.conftest import scoped_default


@pytest.fixture
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


def _b64d(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _p256_pem() -> tuple[str, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key


def test_build_apns_jwt_header_claims_and_signature():
    pem, key = _p256_pem()
    token = build_apns_jwt("KEYID123", "TEAMID456", pem, issued_at=1_700_000_000)

    header_b64, claims_b64, sig_b64 = token.split(".")
    assert json.loads(_b64d(header_b64)) == {"alg": "ES256", "kid": "KEYID123"}
    assert json.loads(_b64d(claims_b64)) == {"iss": "TEAMID456", "iat": 1_700_000_000}

    # The signature is a raw r‖s pair (64 bytes) that verifies against the key.
    raw = _b64d(sig_b64)
    assert len(raw) == 64
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    key.public_key().verify(
        encode_dss_signature(r, s),
        f"{header_b64}.{claims_b64}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )


def test_build_apns_payload_shapes():
    minimal = build_apns_payload("T", "B")
    assert minimal == {"aps": {"alert": {"title": "T", "body": "B"}, "sound": "default"}}

    full = build_apns_payload(
        "T", "B", sound=False, category="outing", interruption_level="time-sensitive",
        actions=[{"label": "I'm back", "url": "https://x/act?t=1"}],
    )
    assert full["aps"]["category"] == "outing"
    assert full["aps"]["interruption-level"] == "time-sensitive"
    assert "sound" not in full["aps"]
    assert full["actions"] == [{"label": "I'm back", "url": "https://x/act?t=1"}]


def test_apns_client_guards_without_config_or_token():
    unconfigured = ApnsClient(Settings())
    assert unconfigured.configured is False
    r = unconfigured.publish("devtok", title="t", message="m", channel="push")
    assert r.delivered is False and "not configured" in r.detail

    configured = ApnsClient(
        Settings(apns_key_id="K", apns_team_id="T", apns_auth_key=_p256_pem()[0])
    )
    assert configured.configured is True
    r2 = configured.publish("", title="t", message="m", channel="push")
    assert r2.delivered is False and "no device token" in r2.detail


def test_resolve_route_reads_per_user_apns_token(store):
    store.set_state("apns_token", "abc123devicetoken", source="explicit")
    assert resolve_route(store, Settings()).apns_token == "abc123devicetoken"


def test_resolve_route_withholds_apns_token_in_multi_user(store):
    # apns_token is a per-user target — no operator default, and on a multi-user
    # box an unset one stays empty (never another user's device).
    store.create_user("second", display_name="Second")
    assert resolve_route(store, Settings()).apns_token == ""


def _decision(channel: str = "push", context_key: str = "outing") -> Decision:
    cue = Cue(module="location_anchor", intervention="x", urgency="nudge",
              text="Still out — heading back?", context_key=context_key, dedup_key="d1")
    return Decision(cue=cue, channel=channel, text=cue.text)


class _RecordingNtfy:
    def __init__(self) -> None:
        self.calls = 0

    def publish(self, *args, **kwargs) -> DeliveryResult:
        self.calls += 1
        return DeliveryResult(transport="ntfy", delivered=True, status_code=200)


class _FakeApns:
    configured = True

    def __init__(self, delivered: bool) -> None:
        self._delivered = delivered
        self.calls: list[tuple[str, dict]] = []

    def publish(self, device_token: str, **kwargs) -> DeliveryResult:
        self.calls.append((device_token, kwargs))
        return DeliveryResult(transport="apns", delivered=self._delivered,
                              status_code=200 if self._delivered else 410)


def test_deliver_prefers_apns_when_token_and_configured():
    apns = _FakeApns(delivered=True)
    ntfy = _RecordingNtfy()
    client = DeliveryClient(ntfy=ntfy, apns=apns)  # type: ignore[arg-type]
    result = client.deliver(_decision(), Route(apns_token="tok", ntfy_topic="me"))
    assert result.transport == "apns" and result.channel == "push"
    assert apns.calls and apns.calls[0][0] == "tok"
    assert apns.calls[0][1]["category"] == "outing"   # cue context → APNs category
    assert ntfy.calls == 0                              # APNs landed → ntfy untouched


def test_deliver_falls_back_to_ntfy_shim_when_apns_fails():
    # Only on a dev box (the ntfy shim enabled) — a stale APNs token there falls
    # through to ntfy rather than black-holing the nudge.
    apns = _FakeApns(delivered=False)
    ntfy = _RecordingNtfy()
    client = DeliveryClient(ntfy=ntfy, apns=apns, ntfy_dev=True)  # type: ignore[arg-type]
    result = client.deliver(_decision(), Route(apns_token="stale", ntfy_topic="me"))
    assert apns.calls and ntfy.calls == 1
    assert result.transport == "ntfy"                   # fell through


def test_deliver_no_ntfy_fallback_on_product_build_when_apns_fails():
    # Product build (shim off): a stale APNs token yields a clean no-op, never a
    # cross-transport fallback.
    apns = _FakeApns(delivered=False)
    ntfy = _RecordingNtfy()
    client = DeliveryClient(ntfy=ntfy, apns=apns)  # type: ignore[arg-type]
    result = client.deliver(_decision(), Route(apns_token="stale", ntfy_topic="me"))
    assert apns.calls and ntfy.calls == 0
    assert result.transport == "none"


def test_register_apns_token_endpoint_stores_and_clears():
    """POST /route/apns-token stores the token as the user's route, and ''
    clears it — so the app can register on launch and unregister on sign-out."""
    from fastapi.testclient import TestClient

    from prefrontal.memory.db import init_db
    from prefrontal.webhooks.app import create_app

    conn = init_db(":memory:")
    s = MemoryStore(conn)
    _, token = provision_user(s, "sam", display_name="Sam", is_operator=False)
    try:
        app = create_app(store=s, settings=Settings())
        with TestClient(app) as c:
            hdr = {"X-Prefrontal-Token": token}
            r = c.post("/route/apns-token", json={"token": "deadbeefdevicetoken"}, headers=hdr)
            assert r.status_code == 200 and r.json() == {"registered": True}
            assert resolve_route(_scoped(s, "sam"), Settings()).apns_token == "deadbeefdevicetoken"

            r2 = c.post("/route/apns-token", json={"token": ""}, headers=hdr)
            assert r2.status_code == 200 and r2.json() == {"registered": False}
            assert resolve_route(_scoped(s, "sam"), Settings()).apns_token == ""
    finally:
        conn.close()


def _scoped(store: MemoryStore, handle: str) -> MemoryStore:
    uid = next(u["id"] for u in store.list_users() if u["handle"] == handle)
    return store.scoped(uid)
