"""Symmetric encryption for secrets stored at rest.

Prefrontal keeps per-user *source* credentials — IMAP passwords, Google refresh
tokens — in the ``sources`` table rather than in one global ``.env`` (see
``docs/design/per-user-sources.md``). Those secrets are sealed with Fernet
(AES-128-CBC + HMAC-SHA256) before they touch the database, so a leaked DB file
does not on its own hand over live mailbox / calendar access.

The key is a urlsafe-base64 32-byte Fernet key, supplied either inline via
``PREFRONTAL_SECRET_KEY`` or from a keyfile named by ``PREFRONTAL_SECRET_KEY_FILE``
(the inline value wins). Mint one with ``prefrontal secrets init``.

**Losing the key is unrecoverable**: sealed secrets can no longer be opened and
must be re-entered (mail) or re-authorized (Google). Back it up.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from prefrontal.config import Settings, get_settings


class SecretKeyError(RuntimeError):
    """A secret must be sealed/opened but no usable key is configured (or it's wrong)."""


def _resolve_settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else get_settings()


def _load_key(settings: Settings | None = None) -> bytes:
    """Return the configured Fernet key as bytes, or raise :class:`SecretKeyError`.

    Prefers the inline ``PREFRONTAL_SECRET_KEY``; falls back to reading the file
    named by ``PREFRONTAL_SECRET_KEY_FILE``.
    """
    cfg = _resolve_settings(settings)
    key = (cfg.secret_key or "").strip()
    if not key and cfg.secret_key_file:
        path = Path(cfg.secret_key_file).expanduser()
        try:
            key = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SecretKeyError(
                f"could not read PREFRONTAL_SECRET_KEY_FILE ({path}): {exc}"
            ) from exc
    if not key:
        raise SecretKeyError(
            "no secret key configured — set PREFRONTAL_SECRET_KEY (or "
            "PREFRONTAL_SECRET_KEY_FILE) or run `prefrontal secrets init`"
        )
    return key.encode("ascii")


def _fernet(settings: Settings | None = None) -> Fernet:
    try:
        return Fernet(_load_key(settings))
    except (ValueError, TypeError) as exc:
        # Fernet raises on a malformed (wrong length / not base64) key.
        raise SecretKeyError(
            "PREFRONTAL_SECRET_KEY is not a valid Fernet key — regenerate it "
            "with `prefrontal secrets init`"
        ) from exc


def seal(plaintext: str, *, settings: Settings | None = None) -> bytes:
    """Encrypt ``plaintext`` for storage; returns the sealed token bytes."""
    return _fernet(settings).encrypt(plaintext.encode("utf-8"))


def unseal(token: bytes, *, settings: Settings | None = None) -> str:
    """Decrypt a sealed token back to plaintext.

    Raises :class:`SecretKeyError` if the token can't be opened with the current
    key (wrong/rotated key, or corrupt bytes).
    """
    try:
        return _fernet(settings).decrypt(bytes(token)).decode("utf-8")
    except InvalidToken as exc:
        raise SecretKeyError(
            "could not decrypt secret with the configured key — the key may have "
            "changed; the secret must be re-entered / re-authorized"
        ) from exc


def generate_key() -> str:
    """Return a fresh urlsafe-base64 Fernet key (ASCII string)."""
    return Fernet.generate_key().decode("ascii")


def secret_key_configured(settings: Settings | None = None) -> bool:
    """Whether a usable secret key is available (without revealing it)."""
    try:
        _load_key(settings)
        return True
    except SecretKeyError:
        return False
