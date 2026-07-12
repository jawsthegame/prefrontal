"""Symmetric encryption for secrets stored at rest.

Prefrontal keeps per-user *source* credentials — IMAP passwords, Google refresh
tokens — in the ``sources`` table rather than in one global ``.env`` (see
``docs/design/per-user-sources.md``). Those secrets are sealed with Fernet
(AES-128-CBC + HMAC-SHA256) before they touch the database, so a leaked DB file
does not on its own hand over live mailbox / calendar access.

The key is a urlsafe-base64 32-byte Fernet key, supplied either inline via
``PREFRONTAL_SECRET_KEY`` or from a keyfile named by ``PREFRONTAL_SECRET_KEY_FILE``
(the inline value wins). Mint one with ``prefrontal secrets init``.

**Key rotation.** New secrets are always sealed with the *primary* key
(``PREFRONTAL_SECRET_KEY`` / keyfile). Retired keys listed in
``PREFRONTAL_SECRET_KEYS_OLD`` (comma-separated) are accepted for *decrypt only*
via :class:`~cryptography.fernet.MultiFernet`, so an operator can roll the primary
without every sealed secret breaking at once: to rotate, mint a new key, set it as
``PREFRONTAL_SECRET_KEY`` and move the old value into ``PREFRONTAL_SECRET_KEYS_OLD``.
Old secrets keep opening under the retired key until they're re-sealed, after which
the retired key can be dropped.

**Losing the (primary and all retired) keys is unrecoverable**: sealed secrets can
no longer be opened and must be re-entered (mail) or re-authorized (Google). Back
them up.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from prefrontal.config import Settings, get_settings
from prefrontal.log import get_logger

logger = get_logger(__name__)


class SecretKeyError(RuntimeError):
    """A secret must be sealed/opened but no usable key is configured (or it's wrong)."""


def _resolve_settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else get_settings()


def _load_primary(settings: Settings | None = None) -> bytes:
    """Return the *primary* Fernet key as bytes, or raise :class:`SecretKeyError`.

    Prefers the inline ``PREFRONTAL_SECRET_KEY``; falls back to reading the file
    named by ``PREFRONTAL_SECRET_KEY_FILE``. This is the key new secrets are
    sealed with; retired keys (:func:`_load_keys`) are decrypt-only.
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


def _load_keys(settings: Settings | None = None) -> list[bytes]:
    """Return all configured keys, primary first, then retired decrypt-only keys.

    The primary key (:func:`_load_primary`) leads; the retired keys named by
    ``PREFRONTAL_SECRET_KEYS_OLD`` follow, in the order given. Only the primary
    is used to seal; the rest exist so already-sealed secrets keep opening across
    a key roll.
    """
    cfg = _resolve_settings(settings)
    keys = [_load_primary(settings)]
    keys.extend(k.strip().encode("ascii") for k in cfg.secret_keys_old if k.strip())
    return keys


def _multifernet(settings: Settings | None = None) -> MultiFernet:
    """Build a :class:`MultiFernet` from the primary + retired keys.

    Seals use the first key (the primary); decrypts try each key in turn. A
    malformed *primary* is fatal (:class:`SecretKeyError`); a malformed *retired*
    key is skipped with a warning, since it must not block sealing under a good
    primary.
    """
    keys = _load_keys(settings)
    fernets: list[Fernet] = []
    for index, raw in enumerate(keys):
        try:
            fernets.append(Fernet(raw))
        except (ValueError, TypeError) as exc:
            if index == 0:
                # Fernet raises on a malformed (wrong length / not base64) key.
                raise SecretKeyError(
                    "PREFRONTAL_SECRET_KEY is not a valid Fernet key — regenerate "
                    "it with `prefrontal secrets init`"
                ) from exc
            logger.warning(
                "ignoring malformed key in PREFRONTAL_SECRET_KEYS_OLD (position %d): %s",
                index,
                exc,
            )
    return MultiFernet(fernets)


def seal(plaintext: str, *, settings: Settings | None = None) -> bytes:
    """Encrypt ``plaintext`` for storage; returns the sealed token bytes.

    Always seals with the primary key; retired keys are decrypt-only.
    """
    return _multifernet(settings).encrypt(plaintext.encode("utf-8"))


def unseal(token: bytes, *, settings: Settings | None = None) -> str:
    """Decrypt a sealed token back to plaintext.

    Tries the primary key then any retired keys (``PREFRONTAL_SECRET_KEYS_OLD``),
    so secrets sealed before a key roll keep opening. Raises :class:`SecretKeyError`
    if no configured key can open the token (wrong/rotated-out key, or corrupt
    bytes).
    """
    try:
        return _multifernet(settings).decrypt(bytes(token)).decode("utf-8")
    except InvalidToken as exc:
        raise SecretKeyError(
            "could not decrypt secret with any configured key — the key may have "
            "been rotated out; the secret must be re-entered / re-authorized"
        ) from exc


def generate_key() -> str:
    """Return a fresh urlsafe-base64 Fernet key (ASCII string)."""
    return Fernet.generate_key().decode("ascii")


def secret_key_configured(settings: Settings | None = None) -> bool:
    """Whether a usable secret key is available (without revealing it)."""
    try:
        _load_primary(settings)
        return True
    except SecretKeyError:
        return False
