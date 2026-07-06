"""Phase 1: per-user mail — registry resolution, fan-out, and management CLI.

The guarantees here: mail fetch resolves *each user's own* IMAP credentials from
the registry (falling back to the global env), fans out over users, and the
management CLI seals passwords at rest.
"""

from __future__ import annotations

import pytest

from prefrontal.cli import main
from prefrontal.config import Settings, get_settings, load_settings
from prefrontal.crypto import generate_key
from prefrontal.memory.store import MemoryStore
from prefrontal.sources import (
    mail_fetch_accounts,
    put_imap_source,
    resolve_mail_fetch,
)
from tests.conftest import scoped_default


@pytest.fixture()
def store():
    """A user-scoped store on a fresh in-memory, schema-initialized DB."""
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


@pytest.fixture()
def secret_env(monkeypatch):
    """Set a real Fernet key in the env and reset the settings cache."""
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# -- resolution helpers ------------------------------------------------------


def test_resolve_prefers_db_source_over_env(store, secret_env, monkeypatch):
    """A registry source wins over env creds for the same account."""
    monkeypatch.setenv("MAIL_IMAP_USER_PERSONAL", "env@gmail.com")
    monkeypatch.setenv("MAIL_IMAP_PASSWORD_PERSONAL", "env-pw")
    put_imap_source(
        store,
        account="personal",
        host="imap.gmail.com",
        username="db@gmail.com",
        password="db-pw",
        retention="full",
    )
    resolved = resolve_mail_fetch(store, "personal", settings=load_settings())
    assert resolved is not None
    assert resolved.imap.user == "db@gmail.com"
    assert resolved.imap.password == "db-pw"
    assert resolved.policy == "full"  # from the source config, not settings


def test_resolve_falls_back_to_env(store, monkeypatch):
    """With no registry source, env creds + settings policy are used."""
    monkeypatch.setenv("MAIL_IMAP_USER_ENVACCT", "env@x.com")
    monkeypatch.setenv("MAIL_IMAP_PASSWORD_ENVACCT", "env-pw")
    resolved = resolve_mail_fetch(store, "envacct", settings=load_settings())
    assert resolved is not None
    assert resolved.imap.user == "env@x.com"


def test_resolve_none_when_nothing_configured(store):
    """No DB source and no env creds resolves to None (caller reports it)."""
    settings = Settings()  # no mail env
    assert resolve_mail_fetch(store, "ghost", settings=settings) is None


def test_disabled_db_source_falls_through_to_env(store, secret_env, monkeypatch):
    """A disabled registry source is not used; env fills in instead."""
    monkeypatch.setenv("MAIL_IMAP_USER_PERSONAL", "env@gmail.com")
    monkeypatch.setenv("MAIL_IMAP_PASSWORD_PERSONAL", "env-pw")
    put_imap_source(
        store,
        account="personal",
        host="imap.gmail.com",
        username="db@gmail.com",
        password="db-pw",
        enabled=False,
    )
    resolved = resolve_mail_fetch(store, "personal", settings=load_settings())
    assert resolved is not None
    assert resolved.imap.user == "env@gmail.com"  # fell through to env


def test_fetch_accounts_prefers_db_then_env(store, secret_env):
    """Account list is the user's enabled sources, else the global env accounts."""
    settings = Settings(mail_accounts=(("legacy", "signals"),))
    assert mail_fetch_accounts(store, settings=settings) == ["legacy"]
    put_imap_source(
        store, account="personal", host="imap.gmail.com",
        username="u", password="p",
    )
    assert mail_fetch_accounts(store, settings=settings) == ["personal"]


# -- management CLI ----------------------------------------------------------


def test_add_source_requires_secret_key(tmp_path, monkeypatch, capsys):
    """add-source without a configured key fails with clear guidance."""
    monkeypatch.delenv("PREFRONTAL_SECRET_KEY", raising=False)
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY_FILE", "")
    get_settings.cache_clear()
    db = str(tmp_path / "p.db")
    assert main(["init-db", "--db-path", db]) == 0
    assert main(["user", "--db-path", db, "add", "tester", "--operator"]) == 0
    rc = main([
        "mail", "--db-path", db, "add-source",
        "--account", "personal", "--username", "me@x.com", "--password", "pw",
    ])
    get_settings.cache_clear()
    assert rc == 1
    assert "secrets init" in capsys.readouterr().err


def test_add_list_remove_source_roundtrip(tmp_path, monkeypatch, capsys):
    """add-source seals a password; list shows it (no secret); remove deletes it."""
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", generate_key())
    get_settings.cache_clear()
    db = str(tmp_path / "p.db")
    main(["init-db", "--db-path", db])
    main(["user", "--db-path", db, "add", "tester", "--operator"])

    assert main([
        "mail", "--db-path", db, "add-source",
        "--account", "personal", "--host", "imap.gmail.com",
        "--username", "me@gmail.com", "--password", "secret-pw",
        "--retention", "full",
    ]) == 0

    capsys.readouterr()
    assert main(["mail", "--db-path", db, "list-sources"]) == 0
    out = capsys.readouterr().out
    assert "personal" in out and "me@gmail.com" in out and "full" in out
    assert "secret-pw" not in out  # never reveal the password

    # The password is sealed at rest, not stored plaintext.
    with MemoryStore.open(db, initialize=False) as unscoped:
        s = unscoped.scoped(unscoped.get_user("tester")["id"])
        raw = s.get_source("imap", "personal")["secret_enc"]
        assert raw is not None and b"secret-pw" not in bytes(raw)

    assert main(["mail", "--db-path", db, "remove-source", "--account", "personal"]) == 0
    capsys.readouterr()
    assert main(["mail", "--db-path", db, "list-sources"]) == 0
    assert "No IMAP sources" in capsys.readouterr().out
    get_settings.cache_clear()


def test_import_env_sources(tmp_path, monkeypatch, capsys):
    """import-env-sources seals the configured MAIL_IMAP_* accounts into the DB."""
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", generate_key())
    monkeypatch.setenv("PREFRONTAL_MAIL_ACCOUNTS", "testacct")
    monkeypatch.setenv("MAIL_IMAP_USER_TESTACCT", "me@corp.com")
    monkeypatch.setenv("MAIL_IMAP_PASSWORD_TESTACCT", "corp-pw")
    monkeypatch.setenv("MAIL_IMAP_HOST_TESTACCT", "imap.corp.com")
    get_settings.cache_clear()
    db = str(tmp_path / "p.db")
    main(["init-db", "--db-path", db])
    main(["user", "--db-path", db, "add", "tester", "--operator"])

    assert main(["mail", "--db-path", db, "import-env-sources"]) == 0
    assert "testacct" in capsys.readouterr().out

    with MemoryStore.open(db, initialize=False) as unscoped:
        s = unscoped.scoped(unscoped.get_user("tester")["id"])
        src = s.get_source("imap", "testacct")
        assert src is not None
        assert b"corp-pw" not in bytes(src["secret_enc"])  # sealed
    get_settings.cache_clear()


def test_fetch_all_users_uses_each_users_sources(tmp_path, monkeypatch, capsys):
    """`fetch --all-users` pulls each user's own accounts into their own scope."""
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", generate_key())
    get_settings.cache_clear()
    db = str(tmp_path / "p.db")
    main(["init-db", "--db-path", db])
    main(["user", "--db-path", db, "add", "alice", "--operator"])
    main(["user", "--db-path", db, "add", "bob"])
    main([
        "mail", "--db-path", db, "--user", "alice", "add-source",
        "--account", "personal", "--host", "imap.gmail.com",
        "--username", "alice@gmail.com", "--password", "pw", "--no-important-only",
    ])
    main([
        "mail", "--db-path", db, "--user", "bob", "add-source",
        "--account", "work", "--host", "imap.example.com",
        "--username", "bob@example.com", "--password", "pw",
    ])

    def fake_fetch(imap, **kwargs):
        return [{
            "message_id": f"<{imap.user}>",
            "from": "Sender <s@x.com>",
            "subject": f"hi {imap.user}",
            "date": "Mon, 06 Jul 2026 10:00:00 +0000",
            "body": "body",
            "unread": True,
        }]

    monkeypatch.setattr("prefrontal.mail.imap.fetch_unread", fake_fetch)
    capsys.readouterr()
    assert main(["mail", "--db-path", db, "fetch", "--all-users", "--heuristic"]) == 0

    with MemoryStore.open(db, initialize=False) as unscoped:
        alice = unscoped.scoped(unscoped.get_user("alice")["id"])
        bob = unscoped.scoped(unscoped.get_user("bob")["id"])
        assert any(m["subject"] == "hi alice@gmail.com" for m in alice.recent_mail())
        assert any(m["subject"] == "hi bob@example.com" for m in bob.recent_mail())
        # Isolation: alice never sees bob's mail.
        assert not any(m["subject"] == "hi bob@example.com" for m in alice.recent_mail())
    get_settings.cache_clear()


def test_fetch_reports_missing_credentials(tmp_path, monkeypatch, capsys):
    """Fetching an account with no DB source and no env creds exits non-zero."""
    monkeypatch.delenv("MAIL_IMAP_USER_GHOST", raising=False)
    get_settings.cache_clear()
    db = str(tmp_path / "p.db")
    main(["init-db", "--db-path", db])
    main(["user", "--db-path", db, "add", "tester", "--operator"])
    rc = main(["mail", "--db-path", db, "fetch", "--account", "ghost", "--heuristic"])
    get_settings.cache_clear()
    assert rc == 1
    assert "no IMAP credentials" in capsys.readouterr().err
