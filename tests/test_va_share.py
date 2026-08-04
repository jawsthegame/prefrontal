"""The VA (assistant) share link — a read-only, unauthenticated page over the
owner's open ``work``-domain todos.

Management (``/todos/va-share``) is owner-authenticated like every other
endpoint; the public page and its data endpoint (``/va/{token}[/todos]``) carry
no auth at all — the token itself is the access control, resolved straight to
the owning user (see :mod:`prefrontal.memory.repos.va_share`).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "va-share-secret"


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def _client(store, *, oauth_base_url=""):
    app = create_app(
        store=store, settings=Settings(webhook_secret=SECRET, oauth_base_url=oauth_base_url)
    )
    return TestClient(app)


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_va_share_requires_auth_to_manage(store):
    with _client(store) as c:
        assert c.get("/todos/va-share").status_code == 401
        assert c.post("/todos/va-share").status_code == 401
        assert c.delete("/todos/va-share").status_code == 401


def test_va_share_status_starts_inactive(store):
    with _client(store) as c:
        body = c.get("/todos/va-share", headers=_auth()).json()
    assert body == {"active": False, "created_at": None}


def test_va_share_create_returns_token_once_and_flips_status(store):
    with _client(store) as c:
        created = c.post("/todos/va-share", headers=_auth()).json()
        assert created["token"]
        assert created["created_at"]
        status = c.get("/todos/va-share", headers=_auth()).json()
    assert status["active"] is True
    assert status["created_at"] == created["created_at"]
    assert "token" not in status


def test_va_share_create_builds_url_from_oauth_base_url(store):
    with _client(store, oauth_base_url="https://example.ts.net") as c:
        created = c.post("/todos/va-share", headers=_auth()).json()
    assert created["url"] == f"https://example.ts.net/va/{created['token']}"


def test_va_share_create_url_blank_without_base_url(store):
    with _client(store) as c:  # no oauth_base_url configured
        created = c.post("/todos/va-share", headers=_auth()).json()
    assert created["url"] == ""


def test_va_share_regenerate_revokes_the_old_token(store):
    with _client(store) as c:
        first = c.post("/todos/va-share", headers=_auth()).json()["token"]
        second = c.post("/todos/va-share", headers=_auth()).json()["token"]
        assert first != second
        assert c.get(f"/va/{first}/todos").status_code == 404
        assert c.get(f"/va/{second}/todos").status_code == 200


def test_va_share_create_leaves_exactly_one_active_row(store):
    # The regenerate above proves the old token stops working; this asserts the
    # underlying invariant directly — revoke-then-insert commits as one
    # transaction, so there's never more than one un-revoked row for a user.
    store.create_va_share()
    store.create_va_share()
    store.create_va_share()
    active = store.conn.execute(
        "SELECT COUNT(*) FROM va_shares WHERE user_id = ? AND revoked_at IS NULL",
        (store._uid(),),
    ).fetchone()[0]
    assert active == 1


def test_va_share_revoke(store):
    with _client(store) as c:
        token = c.post("/todos/va-share", headers=_auth()).json()["token"]
        assert c.get(f"/va/{token}/todos").status_code == 200
        revoked = c.delete("/todos/va-share", headers=_auth()).json()
        assert revoked == {"revoked": True}
        assert c.get(f"/va/{token}/todos").status_code == 404
        # Revoking again (nothing active) reports False, not an error.
        assert c.delete("/todos/va-share", headers=_auth()).json() == {"revoked": False}


def test_va_share_data_shows_only_open_work_domain_todos(store):
    store.add_todo("Ship the report", domain="work")
    store.add_todo("Buy groceries", domain="home")
    store.add_todo("No domain set")  # no domain at all — must not show
    done_id = store.add_todo("Old work thing", domain="work")
    store.close_todo(done_id, "done")

    with _client(store) as c:
        token = c.post("/todos/va-share", headers=_auth()).json()["token"]
        body = c.get(f"/va/{token}/todos").json()

    titles = [t["title"] for t in body["todos"]]
    assert titles == ["Ship the report"]


def test_va_share_data_shape_is_minimal_no_notes_or_internal_fields(store):
    # An anonymous visitor gets exactly what the page renders — not the raw
    # todo row. `notes` in particular can carry private detail (account
    # numbers, context meant for the person doing the work).
    store.add_todo(
        "Ship the report", domain="work", notes="account #4471, ask Sam", category="admin"
    )
    with _client(store) as c:
        token = c.post("/todos/va-share", headers=_auth()).json()["token"]
        body = c.get(f"/va/{token}/todos").json()

    todo = body["todos"][0]
    assert set(todo) == {"id", "title", "priority", "deadline", "estimate_minutes"}


def test_va_share_data_404s_for_unknown_token(store):
    with _client(store) as c:
        r = c.get("/va/not-a-real-token/todos")
    assert r.status_code == 404


def test_va_share_page_serves_html_for_any_token(store):
    # The shell is identical regardless of validity — only the data endpoint
    # the page's own JS calls actually checks the token.
    with _client(store) as c:
        r = c.get("/va/whatever-token-abc")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "/todos" in r.text  # the page fetches its data off the URL's own token


def test_va_share_page_requires_no_auth(store):
    with _client(store) as c:
        r = c.get("/va/whatever-token-abc")  # no headers at all
    assert r.status_code == 200
