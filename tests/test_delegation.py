"""Tests for delegating a todo to an assistant (prep hand-off).

Three layers: the pure prep/handler logic (``generate_prep``, ``AgentHandler`` /
``EmailHandler``, ``run_delegation``), the ``SmtpClient`` transport, and the HTTP
surface (``POST /todos/{id}/delegate``, ``/delegate/return``, and the ``/smtp``
config pair). The SMTP secret round-trip and "never echo the password" guarantee
get their own checks, mirroring ``test_sources.py``.
"""

from __future__ import annotations

import json
import smtplib

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal import delegation, sources
from prefrontal.assistant import (
    ALLOWED_OPS,
    build_snapshot,
    execute_actions,
    validate_actions,
)
from prefrontal.config import Settings, get_settings
from prefrontal.crypto import generate_key
from prefrontal.delegation import (
    STATUS_FAILED,
    STATUS_FORWARDED,
    STATUS_PREPPED,
    STATUS_RETURNED,
    compose_va_email,
    delegation_notice,
    generate_prep,
    run_delegation,
)
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.integrations.smtp import SmtpClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from tests.conftest import scoped_default

SECRET = "delegate-secret"


def _ollama_json(payload: dict) -> OllamaClient:
    """An OllamaClient whose generate() returns ``payload`` as the model's JSON reply."""
    text = json.dumps(payload)
    return OllamaClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"response": text})
    ))


class _FakeConn:
    """A stand-in for ``smtplib.SMTP`` recording what a send did."""

    def __init__(self, record: dict, *, fail_on: str | None = None):
        self.record = record
        self.fail_on = fail_on

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        if self.fail_on == "starttls":
            raise smtplib.SMTPException("tls boom")
        self.record["tls"] = True

    def login(self, user, password):
        self.record["login"] = (user, password)

    def send_message(self, message):
        self.record["subject"] = message["Subject"]
        self.record["to"] = message["To"]
        self.record["from"] = message["From"]
        self.record["body"] = message.get_content()


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


@pytest.fixture()
def secret_env(monkeypatch):
    """A real Fernet key via the environment, so the SMTP source can seal/open."""
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# -- prep generation ---------------------------------------------------------


def test_generate_prep_offline_is_honest_and_uses_structure(store):
    """No model → a heuristic brief that says so and leans on notes/steps, no drafts."""
    brief, drafts = generate_prep(
        "Plan Mia's birthday",
        notes="15 kids, backyard",
        decomposition={"first_step": "Pick a date", "steps": ["Book venue"]},
        client=None,
    )
    assert "Generated offline" in brief
    assert "15 kids" in brief
    assert "Pick a date" in brief and "Book venue" in brief
    assert drafts == []


def test_generate_prep_with_model_returns_brief_and_drafts():
    """A usable model reply yields the brief and well-formed drafts."""
    client = _ollama_json(
        {
            "brief": "Confirm coverage, then book the earliest slot.",
            "drafts": [
                {"channel": "call", "to": "office", "subject": "", "body": "Hi, booking..."},
                {"channel": "bogus", "subject": "", "body": ""},  # dropped: no body
            ],
        }
    )
    brief, drafts = generate_prep("Book dentist", client=client)
    assert brief == "Confirm coverage, then book the earliest slot."
    assert len(drafts) == 1
    assert drafts[0]["channel"] == "call"


def test_generate_prep_model_down_falls_back(store):
    """A model that errors falls back to the heuristic brief (never raises)."""
    def refuse(request):
        raise httpx.ConnectError("down")

    brief, drafts = generate_prep(
        "Renew passport", client=OllamaClient(transport=httpx.MockTransport(refuse))
    )
    assert "Generated offline" in brief
    assert drafts == []


# -- handlers + orchestration ------------------------------------------------


def test_agent_handler_prepped(store):
    tid = store.add_todo("Book dentist")
    todo = store.get_todo(tid)
    client = _ollama_json({"brief": "Call and book.", "drafts": []})
    result = run_delegation(store, todo, handler="agent", client=client)
    assert result.status == STATUS_PREPPED
    assert result.handler == "agent"
    stored = store.get_delegation(tid)
    assert stored["status"] == STATUS_PREPPED
    assert stored["brief"] == "Call and book."
    assert stored["prepped_at"] is not None


def test_email_handler_sends_and_forwards(store):
    tid = store.add_todo("Book dentist")
    todo = store.get_todo(tid)
    record: dict = {}
    smtp = sources.SmtpSource(
        account="default", host="smtp.test", port=587,
        username="me@x.com", password="pw", sender="me@x.com",
    )
    smtp_client = SmtpClient(connect=lambda h, p, t: _FakeConn(record))
    result = run_delegation(
        store, todo, handler="email", destination="va@x.com",
        client=_ollama_json({"brief": "Prep.", "drafts": []}),
        smtp=smtp, smtp_client=smtp_client,
    )
    assert result.status == STATUS_FORWARDED
    assert record["to"] == "va@x.com"
    assert record["tls"] is True
    assert record["login"] == ("me@x.com", "pw")
    assert store.get_delegation(tid)["status"] == STATUS_FORWARDED


def test_email_handler_no_smtp_stores_brief_and_fails(store):
    """Without a configured SMTP source the brief is still stored; status is failed."""
    tid = store.add_todo("Book dentist")
    todo = store.get_todo(tid)
    result = run_delegation(
        store, todo, handler="email", destination="va@x.com",
        client=_ollama_json({"brief": "Prep.", "drafts": []}), smtp=None,
    )
    assert result.status == STATUS_FAILED
    assert "SMTP not configured" in result.detail
    assert store.get_delegation(tid)["brief"] == "Prep."  # not lost


def test_email_handler_send_error_is_caught(store):
    tid = store.add_todo("Book dentist")
    todo = store.get_todo(tid)
    smtp = sources.SmtpSource(
        account="default", host="smtp.test", port=587, username="u", password="p", sender="u@x"
    )
    smtp_client = SmtpClient(connect=lambda h, p, t: _FakeConn({}, fail_on="starttls"))
    result = run_delegation(
        store, todo, handler="email", destination="va@x.com",
        client=None, smtp=smtp, smtp_client=smtp_client,
    )
    assert result.status == STATUS_FAILED
    assert "send failed" in result.detail


def test_run_delegation_rejects_unknown_handler(store):
    tid = store.add_todo("x")
    with pytest.raises(ValueError):
        run_delegation(store, store.get_todo(tid), handler="carrier-pigeon")


def test_delegation_notice_by_status():
    R = delegation.DelegationResult
    draft = {"channel": "call", "to": "", "subject": "", "body": "hi"}
    assert delegation_notice("X", R("agent", STATUS_PREPPED, "b", [draft])).startswith("Prep ready")
    assert "assistant" in delegation_notice("X", R("email", STATUS_FORWARDED, "b", [], "emailed"))
    assert delegation_notice("X", R("agent", STATUS_RETURNED, "b")) is None


def test_compose_va_email_includes_brief_and_drafts():
    subject, body = compose_va_email(
        "Book dentist", "Confirm coverage.",
        [{"channel": "call", "to": "office", "subject": "", "body": "Hi, booking..."}],
    )
    assert "Book dentist" in subject
    assert "Confirm coverage." in body
    assert "Hi, booking..." in body


# -- SmtpClient transport ----------------------------------------------------


def test_smtp_client_noop_when_unconfigured():
    """No host/sender/recipient → a no-op result, nothing sent, no error."""
    r = SmtpClient().send("", 587, "", "", sender="", to="", subject="s", body="b")
    assert r.delivered is False
    assert "not configured" in r.detail


def test_smtp_client_skips_login_without_credentials():
    record: dict = {}
    r = SmtpClient(connect=lambda h, p, t: _FakeConn(record)).send(
        "smtp.test", 25, "", "", sender="me@x", to="you@x", subject="s", body="b", use_tls=False
    )
    assert r.delivered is True
    assert "login" not in record  # no creds → no login attempt
    assert "tls" not in record  # use_tls False → no starttls


# -- SMTP source encryption --------------------------------------------------


def test_smtp_source_round_trip_and_encryption(store, secret_env):
    sources.put_smtp_source(
        store, host="smtp.gmail.com", port=587, username="me@x.com",
        password="app-secret", sender="me@x.com",
    )
    row = store.get_source(sources.SMTP, sources.SMTP_ACCOUNT)
    assert b"app-secret" not in bytes(row["secret_enc"])  # sealed at rest
    resolved = sources.resolve_smtp(store)
    assert resolved.host == "smtp.gmail.com"
    assert resolved.password == "app-secret"
    assert resolved.configured is True


def test_smtp_source_none_password_preserves_secret(store, secret_env):
    """A config-only re-save (password=None) keeps the stored secret."""
    sources.put_smtp_source(store, host="a", password="keep-me", sender="me@x")
    sources.put_smtp_source(store, host="b", password=None, sender="me@x")  # edit host only
    resolved = sources.resolve_smtp(store)
    assert resolved.host == "b"
    assert resolved.password == "keep-me"


# -- HTTP surface ------------------------------------------------------------


@pytest.fixture()
def http(secret_env):
    """A TestClient whose agent prep uses a deterministic (mock) model reply."""
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "me", display_name="Me", token=SECRET, is_operator=True)
    from prefrontal.webhooks.app import create_app

    ollama = _ollama_json({"brief": "Call and book.", "drafts": []})
    app = create_app(store=unscoped, settings=Settings(), ollama=ollama)
    with TestClient(app) as c:
        yield c
    conn.close()


def _headers():
    return {"X-Prefrontal-Token": SECRET}


def test_http_delegate_agent(http):
    tid = http.post("/todos", json={"title": "Book dentist"}, headers=_headers()).json()["todo_id"]
    r = http.post(f"/todos/{tid}/delegate", json={"handler": "agent"}, headers=_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == STATUS_PREPPED
    assert body["brief"] == "Call and book."
    # GET /todos now carries the delegation
    listed = http.get("/todos", headers=_headers()).json()["todos"]
    assert listed[0]["delegation"]["status"] == STATUS_PREPPED


def test_http_delegate_email_needs_destination(http):
    tid = http.post("/todos", json={"title": "x"}, headers=_headers()).json()["todo_id"]
    r = http.post(f"/todos/{tid}/delegate", json={"handler": "email"}, headers=_headers())
    assert r.status_code == 422


def test_http_delegate_email_without_smtp_fails_gracefully(http):
    tid = http.post("/todos", json={"title": "x"}, headers=_headers()).json()["todo_id"]
    r = http.post(
        f"/todos/{tid}/delegate",
        json={"handler": "email", "destination": "va@x.com"}, headers=_headers(),
    )
    assert r.status_code == 200
    assert r.json()["status"] == STATUS_FAILED  # brief stored, nothing sent


def test_http_delegate_return_and_action_route_intact(http):
    tid = http.post("/todos", json={"title": "x"}, headers=_headers()).json()["todo_id"]
    http.post(f"/todos/{tid}/delegate", json={"handler": "agent"}, headers=_headers())
    ret = http.post(f"/todos/{tid}/delegate/return", json={}, headers=_headers())
    assert ret.json()["status"] == STATUS_RETURNED
    # the {action} catch-all still resolves done/drop (not shadowed by /delegate)
    assert http.post(f"/todos/{tid}/done", headers=_headers()).json()["status"] == "done"


def test_http_delegate_return_404_without_delegation(http):
    tid = http.post("/todos", json={"title": "x"}, headers=_headers()).json()["todo_id"]
    r = http.post(f"/todos/{tid}/delegate/return", json={}, headers=_headers())
    assert r.status_code == 404


def test_http_smtp_get_post_never_echoes_password(http):
    assert http.get("/smtp", headers=_headers()).json()["accounts"] == []
    r = http.post(
        "/smtp",
        json={
            "host": "smtp.gmail.com", "username": "me@x.com",
            "password": "app-pw", "sender": "me@x.com",
        },
        headers=_headers(),
    )
    body = r.json()
    assert body["account"] == "default"  # defaulted
    assert body["configured"] is True
    assert body["password_set"] is True
    assert "password" not in body  # never returned
    accounts = http.get("/smtp", headers=_headers()).json()["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["host"] == "smtp.gmail.com"
    assert accounts[0]["password_set"] is True
    assert "password" not in accounts[0]


def test_http_smtp_multiple_named_accounts_and_delete(http):
    """Several named outboxes coexist; one can be removed without touching the rest."""
    for acct in ("default", "work"):
        http.post(
            "/smtp",
            json={"account": acct, "host": f"smtp.{acct}", "password": "p", "sender": f"{acct}@x"},
            headers=_headers(),
        )
    accounts = {a["account"] for a in http.get("/smtp", headers=_headers()).json()["accounts"]}
    assert accounts == {"default", "work"}
    assert http.delete("/smtp/work", headers=_headers()).status_code == 200
    assert http.delete("/smtp/work", headers=_headers()).status_code == 404  # gone
    left = {a["account"] for a in http.get("/smtp", headers=_headers()).json()["accounts"]}
    assert left == {"default"}


def test_http_smtp_partial_update_keeps_password(http):
    http.post(
        "/smtp",
        json={"host": "a", "username": "me@x", "password": "keep", "sender": "me@x"},
        headers=_headers(),
    )
    # Re-save without a password (edit host only) — the stored secret survives.
    r = http.post(
        "/smtp", json={"host": "b", "username": "me@x", "sender": "me@x"}, headers=_headers()
    )
    assert r.json()["password_set"] is True
    assert r.json()["host"] == "b"
    assert r.json()["host"] == "b"


# -- NL assistant op ---------------------------------------------------------


def test_delegate_todo_is_in_allowed_ops():
    """The op is dispatchable — registered, so the whitelist accepts it."""
    assert "delegate_todo" in ALLOWED_OPS


def test_assistant_op_delegates_to_agent(store):
    tid = store.add_todo("Book dentist")
    snap = build_snapshot(store)
    actions, errors = validate_actions(
        [{"op": "delegate_todo", "todo_id": tid, "handler": "agent"}], snap
    )
    assert errors == []
    results = execute_actions(
        store, actions, client=_ollama_json({"brief": "Call and book.", "drafts": []})
    )
    assert results[0]["ok"] is True
    assert store.get_delegation(tid)["status"] == STATUS_PREPPED


def test_assistant_op_defaults_handler_to_agent(store):
    """A bare delegate (no handler) defaults to the in-app agent."""
    tid = store.add_todo("Book dentist")
    actions, errors = validate_actions(
        [{"op": "delegate_todo", "todo_id": tid}], build_snapshot(store)
    )
    assert errors == []
    assert actions[0].params["handler"] == "agent"


def test_assistant_op_email_requires_destination(store):
    """An email hand-off with no address is rejected at validation, not executed."""
    tid = store.add_todo("Book dentist")
    actions, errors = validate_actions(
        [{"op": "delegate_todo", "todo_id": tid, "handler": "email"}], build_snapshot(store)
    )
    assert actions == []
    assert any("destination" in e for e in errors)


def test_assistant_op_rejects_unknown_todo(store):
    """delegate_todo on a non-existent id drops at validation (id not in snapshot)."""
    actions, errors = validate_actions(
        [{"op": "delegate_todo", "todo_id": 9999, "handler": "agent"}], build_snapshot(store)
    )
    assert actions == []
    assert errors


# -- per-account SMTP selection ----------------------------------------------


def test_resolve_smtp_for_account_then_domain_then_default(store, secret_env):
    """A todo's mail account wins, then its domain, then the 'default' outbox."""
    for acct, host in [("default", "d"), ("work", "w"), ("home", "h")]:
        sources.put_smtp_source(store, account=acct, host=host, password="p", sender=f"{acct}@x")
    # account match beats domain
    assert sources.resolve_smtp_for(store, account="work", domain="home").account == "work"
    # no account match → domain match
    assert sources.resolve_smtp_for(store, account="personal", domain="home").account == "home"
    # neither matches → default
    assert sources.resolve_smtp_for(store, account="personal", domain="school").account == "default"


def test_resolve_smtp_for_single_source_convenience(store, secret_env):
    """With exactly one configured outbox, an unmatched todo still uses it."""
    sources.put_smtp_source(store, account="personal", host="p", password="p", sender="me@x")
    assert sources.resolve_smtp_for(store, account="work", domain="work").account == "personal"


def test_resolve_smtp_for_none_when_ambiguous(store, secret_env):
    """Two outboxes and no match/default → None (don't guess which identity)."""
    sources.put_smtp_source(store, account="work", host="w", password="p", sender="me@w")
    sources.put_smtp_source(store, account="personal", host="p", password="p", sender="me@p")
    assert sources.resolve_smtp_for(store, account="school", domain="school") is None


def test_http_delegate_email_auto_matches_domain(http):
    """A work-domain todo with only a 'work' outbox routes to it (tries to send)."""
    http.post(
        "/smtp",
        json={"account": "work", "host": "smtp.invalid.test", "password": "p", "sender": "me@w"},
        headers=_headers(),
    )
    # a second outbox so the single-source convenience can't mask the domain match
    http.post(
        "/smtp",
        json={"account": "other", "host": "smtp.other.test", "password": "p", "sender": "me@o"},
        headers=_headers(),
    )
    tid = http.post("/todos", json={"title": "Q3 report"}, headers=_headers()).json()["todo_id"]
    http.post(f"/todos/{tid}/domain", json={"domain": "work"}, headers=_headers())
    r = http.post(
        f"/todos/{tid}/delegate",
        json={"handler": "email", "destination": "va@x.com"}, headers=_headers(),
    ).json()
    # Selection found the 'work' outbox and attempted a send (fake host → send failed),
    # rather than reporting "SMTP not configured" (which is the no-match outcome).
    assert r["status"] == STATUS_FAILED
    assert "send failed" in r["detail"]
