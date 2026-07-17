"""Tests for Google sign-in — the browser session-cookie auth (webhooks/oauth.py).

Machine clients keep their tokens; these cover the human login path: the signed
session cookie, the login redirect, and the callback's allowlist + cookie issue.
The Google code-for-email exchange is monkeypatched (no live network).
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.webhooks import oauth
from prefrontal.webhooks.app import create_app

SECRET = "sess-secret"


def _settings(**kw):
    base = dict(
        session_secret=SECRET,
        google_oauth_client_id="cid",
        google_oauth_client_secret="csecret",
        # http so the issued session cookie isn't Secure (the test client is http)
        oauth_base_url="http://testserver",
        google_oauth_allowed="tom@example.com=tom",
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    s = MemoryStore(conn)
    provision_user(s, "tom", display_name="Tom", token="tok-tom", is_operator=True)
    try:
        yield s
    finally:
        conn.close()


@pytest.fixture()
def client(store):
    with TestClient(create_app(store=store, settings=_settings())) as c:
        yield c


def _login_state(client, next_path: str = "") -> str:
    """Hit /login (sets the state cookie) and return the matching state value."""
    url = "/auth/google/login" + (f"?next={next_path}" if next_path else "")
    r = client.get(url, follow_redirects=False)
    assert r.status_code in (302, 307)
    loc = r.headers["location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert "scope=openid+email" in loc or "scope=openid%20email" in loc
    return parse_qs(urlparse(loc).query)["state"][0]


# -- session token -----------------------------------------------------------


def test_session_sign_verify_roundtrip():
    t = oauth.sign_session("tom", SECRET)
    assert oauth.verify_session(t, SECRET) == "tom"
    assert oauth.verify_session("not-a-token", SECRET) is None
    assert oauth.verify_session(t, "wrong-secret") is None
    assert oauth.verify_session(oauth.sign_session("tom", SECRET, now=0), SECRET) is None  # expired


# -- routes ------------------------------------------------------------------


def test_login_404_when_oauth_unconfigured(store):
    with TestClient(create_app(store=store, settings=Settings())) as c:
        assert c.get("/auth/google/login", follow_redirects=False).status_code == 404


def test_callback_allowed_email_signs_in(client, monkeypatch):
    monkeypatch.setattr(oauth, "_exchange_code_for_email",
                        lambda code, settings, client=None: "tom@example.com")
    state = _login_state(client)
    cb = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert cb.status_code == 303 and cb.headers["location"] == "/dashboard"
    assert "prefrontal_session" in cb.headers.get("set-cookie", "")

    # The session cookie now authenticates a tokenless request.
    assert client.get("/todos").status_code == 200
    # ...and logging out drops it again.
    client.post("/auth/logout")
    assert client.get("/todos").status_code == 401


def test_callback_returns_to_next_page(client, monkeypatch):
    """Signing in from /admin lands back on /admin, not the default dashboard."""
    monkeypatch.setattr(oauth, "_exchange_code_for_email",
                        lambda code, settings, client=None: "tom@example.com")
    state = _login_state(client, next_path="/admin")
    cb = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert cb.status_code == 303 and cb.headers["location"] == "/admin"
    assert "prefrontal_session" in cb.headers.get("set-cookie", "")


def test_callback_ignores_offsite_next(client, monkeypatch):
    """A crafted external ?next= can't turn sign-in into an open redirect."""
    monkeypatch.setattr(oauth, "_exchange_code_for_email",
                        lambda code, settings, client=None: "tom@example.com")
    state = _login_state(client, next_path="https://evil.example")
    cb = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert cb.status_code == 303 and cb.headers["location"] == "/dashboard"


def test_callback_resolves_email_from_user_record(client, store, monkeypatch):
    """A user's stored email signs them in — no GOOGLE_OAUTH_ALLOWED entry needed."""
    # 'jamie@example.com' is NOT in the env allowlist ("tom@example.com=tom");
    # it resolves purely because it's stored on the jamie user row.
    provision_user(store, "jamie", display_name="Jamie", token="tok-jamie",
                   email="Jamie@Example.com")
    monkeypatch.setattr(oauth, "_exchange_code_for_email",
                        lambda code, settings, client=None: "jamie@example.com")
    state = _login_state(client)
    cb = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert cb.status_code == 303
    assert "prefrontal_session" in cb.headers.get("set-cookie", "")
    # The cookie authenticates as jamie (her todo is isolated to her scope).
    client.post("/todos", json={"title": "jamie todo", "estimate_minutes": 5})
    mine = client.get("/todos").json()["todos"]
    assert [t["title"] for t in mine] == ["jamie todo"]


def test_callback_rejects_disabled_email_user(client, store, monkeypatch):
    """A disabled user with a matching email is refused (403), no cookie set."""
    provision_user(store, "gone", token="tok-gone", email="gone@example.com")
    store.set_user_status("gone", "disabled")
    monkeypatch.setattr(oauth, "_exchange_code_for_email",
                        lambda code, settings, client=None: "gone@example.com")
    state = _login_state(client)
    cb = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert cb.status_code == 403
    assert "prefrontal_session" not in cb.headers.get("set-cookie", "")


def test_callback_rejects_disallowed_email(client, monkeypatch):
    monkeypatch.setattr(oauth, "_exchange_code_for_email",
                        lambda code, settings, client=None: "stranger@example.com")
    state = _login_state(client)
    cb = client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert cb.status_code == 403
    assert "set-cookie" not in cb.headers or "prefrontal_session" not in cb.headers["set-cookie"]


def test_callback_rejects_bad_state(client):
    # verify_state runs before any code exchange, so a forged state 400s without
    # the exchange ever being reached (no _exchange_code_for_email patch needed).
    _login_state(client)  # sets a state cookie
    cb = client.get("/auth/google/callback?code=abc&state=forged", follow_redirects=False)
    assert cb.status_code == 400


def test_token_header_still_works_alongside_cookie(client):
    """Machine clients are unaffected: the per-user token still authenticates."""
    assert client.get("/todos", headers={"X-Prefrontal-Token": "tok-tom"}).status_code == 200
    assert client.get("/todos").status_code == 401  # no token, no cookie


def test_signed_state_no_cookie_needed():
    """CSRF state is a signed token (survives the cross-site redirect; no cookie).

    A valid state yields its carried ``next`` path (``""`` when none); only a
    forged/expired/wrong-secret state returns ``None``.
    """
    st = oauth.sign_state(SECRET)
    assert oauth.verify_state(st, SECRET) == ""
    assert oauth.verify_state("forged", SECRET) is None
    assert oauth.verify_state(oauth.sign_state(SECRET, now=0), SECRET) is None  # expired
    assert oauth.verify_state(st, "other-secret") is None


def test_state_round_trips_next_path():
    """The signed state carries a ``next`` destination back intact."""
    st = oauth.sign_state(SECRET, "/admin")
    assert oauth.verify_state(st, SECRET) == "/admin"
    # Two states are distinct (random nonce) even for the same next path.
    assert oauth.sign_state(SECRET, "/admin") != oauth.sign_state(SECRET, "/admin")


def test_safe_next_clamps_open_redirects():
    """Only same-origin absolute paths survive; everything else falls to default."""
    assert oauth.safe_next("/admin") == "/admin"
    assert oauth.safe_next("/day/board") == "/day/board"
    assert oauth.safe_next("") == "/dashboard"          # default
    assert oauth.safe_next("//evil.com") == "/dashboard"  # protocol-relative
    assert oauth.safe_next("/\\evil.com") == "/dashboard"  # backslash trick
    assert oauth.safe_next("https://evil.com") == "/dashboard"  # absolute URL
    assert oauth.safe_next("relative") == "/dashboard"  # not absolute
    assert oauth.safe_next("/ok\r\nSet-Cookie: x") == "/dashboard"  # header injection
    assert oauth.safe_next("/x", default="/y") == "/x"
    assert oauth.safe_next("bad", default="/y") == "/y"
