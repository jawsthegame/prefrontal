"""APNs (Apple Push Notification service) helpers — JWT auth + payload shape.

Pure, dependency-light builders so they're unit-testable without touching the
network: :func:`build_apns_jwt` (the ES256 provider token) and
:func:`build_apns_payload` (the ``aps`` dictionary). The HTTP sender that uses
them lives in :mod:`prefrontal.integrations.delivery` (``ApnsClient``), next to
the other transports.

Token-based auth (a ``.p8`` key) rather than certificates: the provider JWT is
signed ES256 with ``kid`` = key id and ``iss`` = team id, and is reusable for up
to an hour. See Apple's "Establishing a token-based connection to APNs".
"""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import load_pem_private_key


def _b64url(data: bytes) -> bytes:
    """Base64url without padding (JWT segment encoding)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def build_apns_jwt(key_id: str, team_id: str, auth_key_pem: str, *, issued_at: int) -> str:
    """Build a signed APNs provider token (ES256 JWT).

    Args:
        key_id: The ``.p8`` key's Key ID (the ``kid`` header).
        team_id: Your Apple Developer Team ID (the ``iss`` claim).
        auth_key_pem: The ``.p8`` private key's PEM contents.
        issued_at: Unix seconds for the ``iat`` claim (APNs rejects tokens
            older than 1 hour, so callers refresh well within that).

    Returns:
        The compact JWT string ``header.claims.signature``.
    """
    key = load_pem_private_key(auth_key_pem.encode("utf-8"), password=None)
    header = {"alg": "ES256", "kid": key_id}
    claims = {"iss": team_id, "iat": issued_at}
    segments = [
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8")),
    ]
    signing_input = b".".join(segments)
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    # JWS wants the raw r‖s pair (two 32-byte big-endian ints), not cryptography's
    # DER-encoded ECDSA signature.
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return b".".join([signing_input, _b64url(raw)]).decode("ascii")


def build_apns_payload(
    title: str,
    body: str,
    *,
    sound: bool = True,
    category: str | None = None,
    interruption_level: str | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the APNs JSON payload.

    ``category`` maps to a ``UNNotificationCategory`` the app registers (its
    action buttons). ``actions`` (the same ``{label,url}`` specs ntfy uses) ride
    as a custom top-level key so the app's action handler has the signed
    ``/nudge/act`` URLs to call. ``interruption_level`` is the iOS 15+ level
    (``passive``/``active``/``time-sensitive``/``critical``).
    """
    aps: dict[str, Any] = {"alert": {"title": title, "body": body}}
    if sound:
        aps["sound"] = "default"
    if category:
        aps["category"] = category
    if interruption_level:
        aps["interruption-level"] = interruption_level
    payload: dict[str, Any] = {"aps": aps}
    if actions:
        payload["actions"] = actions
    return payload
