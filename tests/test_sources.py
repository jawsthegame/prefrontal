"""Tests for the per-user source registry: repo CRUD + the service layer.

Two guarantees matter most here: secrets are **encrypted at rest** (the raw
``secret_enc`` bytes never contain the plaintext) and a source round-trips
through the service layer (config JSON + decrypted secret) unchanged.
"""

from __future__ import annotations

import pytest

from prefrontal.config import get_settings
from prefrontal.crypto import generate_key
from prefrontal.memory.store import MemoryStore
from prefrontal.sources import (
    IMAP,
    imap_accounts,
    put_imap_source,
    resolve_imap,
)
from tests.conftest import scoped_default


@pytest.fixture()
def store():
    """A user-scoped store on a fresh in-memory, schema-initialized DB."""
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


@pytest.fixture()
def secret_env(monkeypatch):
    """Configure a real Fernet key via the environment for the service layer.

    The service functions seal/open through ``get_settings()``; set a key and
    clear the settings cache so the whole test uses one consistent key.
    """
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# -- repo layer (no encryption; raw bytes) -----------------------------------


def test_repo_upsert_is_idempotent_per_kind_account(store):
    """Re-adding the same (kind, account) updates in place — one row, same id."""
    id1 = store.upsert_source(kind=IMAP, account="personal", config='{"host": "a"}')
    id2 = store.upsert_source(kind=IMAP, account="personal", config='{"host": "b"}')
    assert id1 == id2
    row = store.get_source(IMAP, "personal")
    assert row["config"] == '{"host": "b"}'
    assert len(store.list_sources(kind=IMAP)) == 1


def test_repo_upsert_none_secret_keeps_existing(store):
    """A config-only update (secret_enc=None) must not wipe the stored secret."""
    store.upsert_source(
        kind=IMAP, account="work", config="{}", secret_enc=b"sealed-bytes"
    )
    store.upsert_source(kind=IMAP, account="work", config='{"host": "x"}')
    assert store.get_source(IMAP, "work")["secret_enc"] == b"sealed-bytes"


def test_repo_list_filters_by_kind_and_enabled(store):
    """list_sources filters by kind and (optionally) excludes disabled rows."""
    store.upsert_source(kind=IMAP, account="personal", config="{}")
    store.upsert_source(kind=IMAP, account="work", config="{}", enabled=False)
    store.upsert_source(kind="ics", account="family", config="{}")
    assert len(store.list_sources()) == 3
    assert len(store.list_sources(kind=IMAP)) == 2
    assert len(store.list_sources(kind=IMAP, include_disabled=False)) == 1


def test_repo_delete_removes_the_row(store):
    """delete removes the source row; a second delete is a no-op."""
    store.upsert_source(kind=IMAP, account="personal", config="{}")
    assert store.delete_source(IMAP, "personal") is True
    assert store.get_source(IMAP, "personal") is None
    assert store.delete_source(IMAP, "personal") is False


# -- service layer (encryption boundary) -------------------------------------


def test_imap_source_round_trips(store, secret_env):
    """put/resolve preserves config and decrypts the password."""
    put_imap_source(
        store,
        account="personal",
        host="imap.gmail.com",
        username="me@gmail.com",
        password="app-password",
        mailbox="INBOX",
        important_only=True,
        retention="full",
    )
    src = resolve_imap(store, "personal")
    assert src is not None
    assert src.host == "imap.gmail.com"
    assert src.username == "me@gmail.com"
    assert src.password == "app-password"
    assert src.mailbox == "INBOX"
    assert src.important_only is True
    assert src.retention == "full"
    assert imap_accounts(store) == ["personal"]


def test_imap_password_is_encrypted_at_rest(store, secret_env):
    """The stored secret bytes must not contain the plaintext password."""
    put_imap_source(
        store,
        account="personal",
        host="imap.gmail.com",
        username="me@gmail.com",
        password="plaintext-leak-canary",
    )
    raw = store.get_source(IMAP, "personal")["secret_enc"]
    assert raw is not None
    assert b"plaintext-leak-canary" not in bytes(raw)


def test_imap_config_only_edit_preserves_password(store, secret_env):
    """Updating config with password=None keeps the previously sealed password."""
    put_imap_source(
        store,
        account="personal",
        host="old",
        username="me@gmail.com",
        password="keep-me",
    )
    put_imap_source(
        store,
        account="personal",
        host="new",
        username="me@gmail.com",
        password=None,
    )
    src = resolve_imap(store, "personal")
    assert src.host == "new"
    assert src.password == "keep-me"


def test_resolve_missing_source_returns_none(store, secret_env):
    """Resolving an unconfigured source is None, not an error."""
    assert resolve_imap(store, "nope") is None
