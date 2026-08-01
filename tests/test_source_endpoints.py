"""HTTP surface for configuring inbound sources from the Settings page.

Covers the web-UI CRUD for IMAP mailboxes (``/mail-sources``) and private ICS
calendar feeds (``/calendar-sources``): reading them back, creating/updating with
a partial-update secret, and deleting. The two invariants that matter most are
that the **secret never round-trips** to the browser (IMAP password, ICS feed
URL) and that a blank secret on an edit keeps the stored one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings, get_settings
from prefrontal.crypto import generate_key
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.sources import ics_sources, resolve_imap
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

_SECRET = "sources-http-secret"


def _auth() -> dict[str, str]:
    return {"X-Prefrontal-Token": _SECRET}


@pytest.fixture()
def secret_env(monkeypatch):
    """A real Fernet key in the env so the seal path works; reset the cache."""
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def client_store(secret_env):
    store = scoped_default(MemoryStore(init_db(":memory:")))
    app = create_app(store=store, settings=Settings(webhook_secret=_SECRET))
    with TestClient(app) as client:
        yield client, store


# -- IMAP (mail accounts) ----------------------------------------------------


def test_mail_sources_start_empty_and_report_key_ready(client_store):
    client, _ = client_store
    data = client.get("/mail-sources", headers=_auth()).json()
    assert data["accounts"] == []
    assert data["secret_key_ready"] is True
    assert data["default_host"] == "imap.gmail.com"


def test_create_mail_source_seals_password_and_never_returns_it(client_store):
    client, store = client_store
    body = {
        "account": "personal",
        "host": "imap.gmail.com",
        "username": "me@gmail.com",
        "password": "app-secret",
        "retention": "full",
        "important_only": True,
    }
    out = client.post("/mail-sources", json=body, headers=_auth()).json()
    # The response describes the account but never echoes the secret.
    assert out["account"] == "personal"
    assert out["password_set"] is True
    assert "password" not in out
    assert out["retention"] == "full"
    # Stored and decryptable server-side.
    src = resolve_imap(store, "personal")
    assert src is not None and src.password == "app-secret"
    assert src.important_only is True


def test_blank_password_on_edit_keeps_the_stored_one(client_store):
    client, store = client_store
    client.post(
        "/mail-sources",
        json={"account": "work", "username": "a@co", "password": "orig-pw"},
        headers=_auth(),
    )
    # Edit the host only, no password field — the sealed secret must survive.
    client.post(
        "/mail-sources",
        json={"account": "work", "host": "imap.fastmail.com", "username": "a@co"},
        headers=_auth(),
    )
    src = resolve_imap(store, "work")
    assert src is not None
    assert src.host == "imap.fastmail.com"
    assert src.password == "orig-pw"


def test_new_mail_source_requires_a_password(client_store):
    client, _ = client_store
    r = client.post(
        "/mail-sources", json={"account": "nopw", "username": "x@y"}, headers=_auth()
    )
    assert r.status_code == 422


def test_bad_retention_falls_back_to_signals(client_store):
    client, store = client_store
    client.post(
        "/mail-sources",
        json={"account": "p", "password": "pw", "retention": "bogus"},
        headers=_auth(),
    )
    assert resolve_imap(store, "p").retention == "signals"


def test_delete_mail_source(client_store):
    client, store = client_store
    client.post(
        "/mail-sources", json={"account": "gone", "password": "pw"}, headers=_auth()
    )
    r = client.delete("/mail-sources/gone", headers=_auth())
    assert r.json() == {"account": "gone", "deleted": True}
    assert resolve_imap(store, "gone") is None
    # Deleting a missing account 404s.
    assert client.delete("/mail-sources/nope", headers=_auth()).status_code == 404


# -- ICS (calendar feeds) ----------------------------------------------------


def test_calendar_source_seals_url_and_never_returns_it(client_store):
    client, store = client_store
    body = {
        "account": "personal",
        "url": "https://calendar.example/secret/basic.ics",
        "label": "Home",
        "me_emails": ["me@gmail.com", "me@work.com"],
    }
    out = client.post("/calendar-sources", json=body, headers=_auth()).json()
    assert out["account"] == "personal"
    assert out["url_set"] is True
    assert out["label"] == "Home"
    assert out["me_emails"] == ["me@gmail.com", "me@work.com"]
    # The bearer URL is never in the payload.
    assert "url" not in out
    # Sealed and decryptable server-side.
    feeds = ics_sources(store, include_disabled=True)
    assert feeds[0].url == "https://calendar.example/secret/basic.ics"


def test_blank_url_on_edit_keeps_the_stored_feed(client_store):
    client, store = client_store
    client.post(
        "/calendar-sources",
        json={"account": "work", "url": "https://cal/orig.ics"},
        headers=_auth(),
    )
    # Relabel only — no url field — the sealed URL must survive.
    client.post(
        "/calendar-sources",
        json={"account": "work", "label": "Office", "enabled": False},
        headers=_auth(),
    )
    feed = ics_sources(store, include_disabled=True)[0]
    assert feed.url == "https://cal/orig.ics"
    assert feed.label == "Office"
    assert feed.enabled is False


def test_new_calendar_source_requires_a_url(client_store):
    client, _ = client_store
    r = client.post("/calendar-sources", json={"account": "nourl"}, headers=_auth())
    assert r.status_code == 422


def test_delete_calendar_source(client_store):
    client, store = client_store
    client.post(
        "/calendar-sources",
        json={"account": "gone", "url": "https://cal/x.ics"},
        headers=_auth(),
    )
    r = client.delete("/calendar-sources/gone", headers=_auth())
    assert r.json() == {"account": "gone", "deleted": True}
    assert ics_sources(store, include_disabled=True) == []
    assert client.delete("/calendar-sources/nope", headers=_auth()).status_code == 404


def test_listing_survives_a_missing_key_after_sources_exist(monkeypatch):
    """A key that vanishes/rotates after sources are saved must not 500 the page.

    The Settings read + config-only-edit paths never decrypt (they build from
    metadata-only summaries), so GET still lists the sources and a config-only POST
    still succeeds — the secret just stays sealed and unreadable until the key is
    restored.
    """
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", generate_key())
    get_settings.cache_clear()
    store = scoped_default(MemoryStore(init_db(":memory:")))
    app = create_app(store=store, settings=Settings(webhook_secret=_SECRET))
    with TestClient(app) as client:
        client.post(
            "/mail-sources", json={"account": "m", "password": "pw"}, headers=_auth()
        )
        client.post(
            "/calendar-sources",
            json={"account": "c", "url": "https://cal/x.ics"},
            headers=_auth(),
        )
        # The key disappears (rotated out / keyfile lost) — rows keep their sealed
        # secrets, but nothing can decrypt them now.
        monkeypatch.delenv("PREFRONTAL_SECRET_KEY", raising=False)
        get_settings.cache_clear()
        try:
            mail = client.get("/mail-sources", headers=_auth())
            assert mail.status_code == 200
            assert mail.json()["accounts"][0]["password_set"] is True
            assert mail.json()["secret_key_ready"] is False

            cal = client.get("/calendar-sources", headers=_auth())
            assert cal.status_code == 200
            assert cal.json()["feeds"][0]["url_set"] is True

            # A config-only edit (no new secret) still succeeds — it never decrypts.
            r = client.post(
                "/mail-sources",
                json={"account": "m", "host": "imap.fastmail.com"},
                headers=_auth(),
            )
            assert r.status_code == 200
            assert r.json()["password_set"] is True
        finally:
            get_settings.cache_clear()


def test_secret_key_missing_blocks_new_source(monkeypatch):
    """With no encryption key, GET flags it and POST of a new secret 400s."""
    monkeypatch.delenv("PREFRONTAL_SECRET_KEY", raising=False)
    monkeypatch.delenv("PREFRONTAL_SECRET_KEY_FILE", raising=False)
    get_settings.cache_clear()
    try:
        store = scoped_default(MemoryStore(init_db(":memory:")))
        app = create_app(store=store, settings=Settings(webhook_secret=_SECRET))
        with TestClient(app) as client:
            assert client.get("/mail-sources", headers=_auth()).json()["secret_key_ready"] is False
            r = client.post(
                "/mail-sources", json={"account": "p", "password": "pw"}, headers=_auth()
            )
            assert r.status_code == 400
    finally:
        get_settings.cache_clear()
