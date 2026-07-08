"""Tests for the at-rest secret sealing (prefrontal/crypto.py).

The contract these lock: a sealed secret is not its plaintext, round-trips under
the same key, and fails *loudly* (never silently returns garbage) when the key is
missing, malformed, or different from the one that sealed it.
"""

from __future__ import annotations

import pytest

from prefrontal.config import Settings
from prefrontal.crypto import (
    SecretKeyError,
    generate_key,
    seal,
    secret_key_configured,
    unseal,
)


def _settings(**kw) -> Settings:
    return Settings(**kw)


def test_seal_unseal_round_trip():
    """A secret sealed with a key opens back to the same plaintext with that key."""
    settings = _settings(secret_key=generate_key())
    token = seal("hunter2", settings=settings)
    assert unseal(token, settings=settings) == "hunter2"


def test_sealed_bytes_are_not_plaintext():
    """The stored form must not contain the plaintext (it's encrypted at rest)."""
    settings = _settings(secret_key=generate_key())
    token = seal("super-secret-password", settings=settings)
    assert isinstance(token, bytes)
    assert b"super-secret-password" not in token


def test_missing_key_raises():
    """With no key configured, sealing/opening raises rather than proceeding."""
    settings = _settings(secret_key="")
    assert not secret_key_configured(settings)
    with pytest.raises(SecretKeyError):
        seal("x", settings=settings)


def test_malformed_key_raises():
    """A non-Fernet key value is rejected with a clear error, not a crash."""
    settings = _settings(secret_key="not-a-valid-fernet-key")
    with pytest.raises(SecretKeyError):
        seal("x", settings=settings)


def test_wrong_key_cannot_open():
    """A token sealed with one key can't be opened with another."""
    sealed_with = _settings(secret_key=generate_key())
    other = _settings(secret_key=generate_key())
    token = seal("data", settings=sealed_with)
    with pytest.raises(SecretKeyError):
        unseal(token, settings=other)


def test_key_from_file(tmp_path):
    """The key may live in a file named by secret_key_file (inline value wins)."""
    key = generate_key()
    keyfile = tmp_path / "secret.key"
    keyfile.write_text(key + "\n", encoding="utf-8")
    settings = _settings(secret_key_file=str(keyfile))
    assert secret_key_configured(settings)
    token = seal("from-file", settings=settings)
    assert unseal(token, settings=settings) == "from-file"


def test_inline_key_wins_over_keyfile(tmp_path):
    """When both secret_key and secret_key_file are set, the inline value wins."""
    inline, filed = generate_key(), generate_key()
    keyfile = tmp_path / "secret.key"
    keyfile.write_text(filed + "\n", encoding="utf-8")
    both = _settings(secret_key=inline, secret_key_file=str(keyfile))
    # A token sealed with the inline key alone opens under the both-set settings...
    assert unseal(seal("s", settings=_settings(secret_key=inline)), settings=both) == "s"
    # ...while one sealed with the (ignored) file's key does not.
    filed_token = seal("s", settings=_settings(secret_key=filed))
    with pytest.raises(SecretKeyError):
        unseal(filed_token, settings=both)


def test_generate_key_is_usable():
    """A freshly generated key is a valid Fernet key."""
    settings = _settings(secret_key=generate_key())
    assert unseal(seal("ok", settings=settings), settings=settings) == "ok"
