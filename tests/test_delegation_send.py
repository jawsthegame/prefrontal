"""Tests for sending a prepared delegation draft (roadmap M4: "drafts → does").

Two layers: the pure core (`preview_send` / `send_prepared_draft` in
`prefrontal/delegation.py`) with a fake SMTP transport, and the HTTP surface
(`POST /todos/{id}/delegate/send/preview` and `/delegate/send`). The safety
properties under test are the two-phase gate's teeth: preview refuses up front on
any blocker, and the confirm refuses a *stale* send (the draft changed since
preview) and never sends caller-supplied body content.
"""

from __future__ import annotations

import json
import smtplib

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal import sources
from prefrontal.config import Settings, get_settings
from prefrontal.crypto import generate_key
from prefrontal.delegation import (
    STATUS_FAILED,
    STATUS_FORWARDED,
    _valid_email,
    preview_send,
    send_prepared_draft,
)
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.integrations.smtp import SmtpClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from tests.conftest import scoped_default

SECRET = "delegate-send-secret"


class _FakeConn:
    """Stand-in for ``smtplib.SMTP`` recording a send (mirrors test_delegation)."""

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
        self.record["to"] = message["To"]
        self.record["subject"] = message["Subject"]
        self.record["body"] = message.get_content()


def _smtp_source() -> sources.SmtpSource:
    return sources.SmtpSource(
        account="default", host="smtp.test", port=587,
        username="me@x.com", password="pw", sender="me@x.com",
    )


def _email_draft(to="dentist@example.com", subject="Booking", body="Hi, I'd like to book."):
    return {"channel": "email", "to": to, "subject": subject, "body": body}


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


def _delegated(store, *, drafts, status="prepped"):
    tid = store.add_todo("Book dentist")
    store.set_delegation(tid, handler="agent", status=status, drafts=drafts)
    return tid


# --- recipient validation (linear, ReDoS-safe) ------------------------------


@pytest.mark.parametrize(
    "addr,ok",
    [
        ("dentist@example.com", True),
        ("a.b+c@sub.example.co.uk", True),
        ("the dentist office", False),  # a name, not an address
        ("no-at-sign.com", False),
        ("two@@ats.com", False),
        ("nodot@domain", False),
        ("trailing@dot.", False),
        ("has space@x.com", False),
        ("", False),
        # A string shaped like the old regex's catastrophic-backtracking input:
        # must return quickly (linear), not hang.
        ("a@" + "!." * 5000, False),
    ],
)
def test_valid_email(addr, ok):
    assert _valid_email(addr) is ok


# --- preview (no side effects) -----------------------------------------------


def test_preview_happy_path(store):
    tid = _delegated(store, drafts=[_email_draft()])
    p = preview_send(store, tid, smtp=_smtp_source())
    assert p.can_send is True
    assert p.blockers == []
    assert p.to == "dentist@example.com"
    assert p.subject == "Booking" and p.body == "Hi, I'd like to book."
    assert p.account == "default" and p.sender == "me@x.com"
    assert p.digest  # a content hash was produced


def test_preview_recipient_override_fills_missing_address(store):
    tid = _delegated(store, drafts=[_email_draft(to="")])  # prep left the address blank
    p = preview_send(store, tid, smtp=_smtp_source(), recipient="office@clinic.com")
    assert p.can_send is True
    assert p.to == "office@clinic.com"


def test_preview_blocks_when_no_email_draft(store):
    call_draft = {"channel": "call", "to": "", "subject": "", "body": "ring them"}
    tid = _delegated(store, drafts=[call_draft])
    p = preview_send(store, tid, smtp=_smtp_source())
    assert p.can_send is False
    assert any("no email draft" in b for b in p.blockers)


def test_preview_blocks_invalid_recipient(store):
    tid = _delegated(store, drafts=[_email_draft(to="the dentist office")])
    p = preview_send(store, tid, smtp=_smtp_source())
    assert p.can_send is False
    assert any("valid email" in b for b in p.blockers)


def test_preview_blocks_unfilled_placeholder(store):
    tid = _delegated(store, drafts=[_email_draft(body="Hi, booking for [date] please.")])
    p = preview_send(store, tid, smtp=_smtp_source())
    assert p.can_send is False
    assert any("placeholder" in b for b in p.blockers)


def test_preview_blocks_when_smtp_unconfigured(store):
    tid = _delegated(store, drafts=[_email_draft()])
    p = preview_send(store, tid, smtp=None)
    assert p.can_send is False
    assert any("isn't configured" in b for b in p.blockers)


def test_preview_blocks_when_not_delegated(store):
    tid = store.add_todo("Book dentist")  # never delegated
    p = preview_send(store, tid, smtp=_smtp_source())
    assert p.can_send is False
    assert any("hasn't been delegated" in b for b in p.blockers)


# --- send (the actual side effect, behind the gate) --------------------------


def test_send_happy_path_sends_and_forwards(store):
    tid = _delegated(store, drafts=[_email_draft()])
    p = preview_send(store, tid, smtp=_smtp_source())
    record: dict = {}
    outcome = send_prepared_draft(
        store, tid, smtp=_smtp_source(), expected_digest=p.digest,
        smtp_client=SmtpClient(connect=lambda h, port, t: _FakeConn(record)),
    )
    assert outcome.sent is True and outcome.code == "sent"
    assert record["to"] == "dentist@example.com"
    assert record["body"].strip() == "Hi, I'd like to book."  # transport appends a trailing newline
    row = store.get_delegation(tid)
    assert row["status"] == STATUS_FORWARDED
    assert "sent draft to dentist@example.com" in row["detail"]


def test_send_refuses_stale_digest(store):
    tid = _delegated(store, drafts=[_email_draft()])
    record: dict = {}
    outcome = send_prepared_draft(
        store, tid, smtp=_smtp_source(), expected_digest="not-the-real-digest",
        smtp_client=SmtpClient(connect=lambda h, port, t: _FakeConn(record)),
    )
    assert outcome.sent is False and outcome.code == "stale"
    assert record == {}  # nothing was sent
    assert store.get_delegation(tid)["status"] == "prepped"  # unchanged


def test_send_refuses_empty_digest(store):
    """An empty digest is a *provided* value that can't match — it must not slip
    past the gate the way `None` (an internal opt-out) does."""
    tid = _delegated(store, drafts=[_email_draft()])
    record: dict = {}
    outcome = send_prepared_draft(
        store, tid, smtp=_smtp_source(), expected_digest="",
        smtp_client=SmtpClient(connect=lambda h, port, t: _FakeConn(record)),
    )
    assert outcome.sent is False and outcome.code == "stale"
    assert record == {}


def test_send_refuses_when_blocked(store):
    tid = _delegated(store, drafts=[_email_draft(body="Confirm for [date].")])
    p = preview_send(store, tid, smtp=_smtp_source())
    assert p.can_send is False
    record: dict = {}
    outcome = send_prepared_draft(
        store, tid, smtp=_smtp_source(), expected_digest=p.digest,
        smtp_client=SmtpClient(connect=lambda h, port, t: _FakeConn(record)),
    )
    assert outcome.sent is False and outcome.code == "blocked"
    assert record == {}


def test_send_transport_failure_marks_failed_and_keeps_draft(store):
    tid = _delegated(store, drafts=[_email_draft()])
    p = preview_send(store, tid, smtp=_smtp_source())
    outcome = send_prepared_draft(
        store, tid, smtp=_smtp_source(), expected_digest=p.digest,
        smtp_client=SmtpClient(connect=lambda h, port, t: _FakeConn({}, fail_on="starttls")),
    )
    assert outcome.sent is False and outcome.code == "send_failed"
    assert store.get_delegation(tid)["status"] == STATUS_FAILED
    # the draft is still there to try again
    assert store.get_delegation(tid)["drafts"]


def test_preview_warns_when_already_sent(store):
    tid = _delegated(store, drafts=[_email_draft()])
    p = preview_send(store, tid, smtp=_smtp_source())
    send_prepared_draft(
        store, tid, smtp=_smtp_source(), expected_digest=p.digest,
        smtp_client=SmtpClient(connect=lambda h, port, t: _FakeConn({})),
    )
    again = preview_send(store, tid, smtp=_smtp_source())
    assert any("already sent" in w for w in again.warnings)


def test_send_refuses_duplicate_after_success(store):
    """A retry of an already-sent draft is refused, not re-delivered.

    Regression: an already-sent draft was only a *warning*, and the send gate
    consulted only blockers + the digest. A client that retried after a send that
    succeeded but timed out client-side (same, unchanged draft → same digest) would
    send a second copy. The guard must be a hard refusal.
    """
    tid = _delegated(store, drafts=[_email_draft()])
    p = preview_send(store, tid, smtp=_smtp_source())
    first = send_prepared_draft(
        store, tid, smtp=_smtp_source(), expected_digest=p.digest,
        smtp_client=SmtpClient(connect=lambda h, port, t: _FakeConn({})),
    )
    assert first.sent is True
    # Same draft, same digest (nothing changed) — the retry after a timed-out send.
    record: dict = {}
    again = send_prepared_draft(
        store, tid, smtp=_smtp_source(), expected_digest=p.digest,
        smtp_client=SmtpClient(connect=lambda h, port, t: _FakeConn(record)),
    )
    assert again.sent is False and again.code == "already_sent"
    assert record == {}  # the transport was never touched a second time


def test_send_allow_resend_overrides_the_duplicate_guard(store):
    """``allow_resend=True`` deliberately re-sends the same stored draft."""
    tid = _delegated(store, drafts=[_email_draft()])
    p = preview_send(store, tid, smtp=_smtp_source())
    send_prepared_draft(
        store, tid, smtp=_smtp_source(), expected_digest=p.digest,
        smtp_client=SmtpClient(connect=lambda h, port, t: _FakeConn({})),
    )
    record: dict = {}
    again = send_prepared_draft(
        store, tid, smtp=_smtp_source(), expected_digest=p.digest, allow_resend=True,
        smtp_client=SmtpClient(connect=lambda h, port, t: _FakeConn(record)),
    )
    assert again.sent is True and again.code == "sent"
    assert record["to"] == "dentist@example.com"


# --- HTTP surface ------------------------------------------------------------


@pytest.fixture()
def secret_env(monkeypatch):
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ollama_email_draft() -> OllamaClient:
    """A model reply that preps an email draft with a real recipient."""
    payload = {"brief": "Book it.", "drafts": [_email_draft()]}
    return OllamaClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"response": json.dumps(payload)})
    ))


def _headers():
    return {"X-Prefrontal-Token": SECRET}


@pytest.fixture()
def http(secret_env):
    from prefrontal.webhooks.app import create_app

    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "me", display_name="Me", token=SECRET, is_operator=True)
    app = create_app(store=unscoped, settings=Settings(), ollama=_ollama_email_draft())
    with TestClient(app) as c:
        yield c
    conn.close()


def _delegate_over_http(http) -> int:
    tid = http.post("/todos", json={"title": "Book dentist"}, headers=_headers()).json()["todo_id"]
    http.post(f"/todos/{tid}/delegate", json={"handler": "agent"}, headers=_headers())
    return tid


def test_http_send_requires_auth(http):
    assert http.post("/todos/1/delegate/send", json={"preview_digest": "x"}).status_code == 401
    assert http.post("/todos/1/delegate/send/preview", json={}).status_code == 401


def test_http_preview_404_for_unknown_todo(http):
    r = http.post("/todos/999/delegate/send/preview", json={}, headers=_headers())
    assert r.status_code == 404


def test_http_preview_then_send_flow(http):
    tid = _delegate_over_http(http)
    # Configure an outbox.
    http.post(
        "/smtp",
        json={"host": "smtp.test", "username": "me@x.com", "password": "pw", "sender": "me@x.com"},
        headers=_headers(),
    )
    preview = http.post(
        f"/todos/{tid}/delegate/send/preview", json={}, headers=_headers()
    ).json()
    assert preview["can_send"] is True
    assert preview["to"] == "dentist@example.com"

    # Inject a fake transport so the send doesn't touch a real server.
    record: dict = {}
    http.app.state.smtp_client = SmtpClient(connect=lambda h, port, t: _FakeConn(record))
    r = http.post(
        f"/todos/{tid}/delegate/send",
        json={"preview_digest": preview["digest"]},
        headers=_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is True and body["status"] == STATUS_FORWARDED
    assert record["to"] == "dentist@example.com"


def test_http_send_stale_digest_is_409(http):
    tid = _delegate_over_http(http)
    http.post(
        "/smtp",
        json={"host": "smtp.test", "username": "me@x.com", "password": "pw", "sender": "me@x.com"},
        headers=_headers(),
    )
    r = http.post(
        f"/todos/{tid}/delegate/send",
        json={"preview_digest": "stale"},
        headers=_headers(),
    )
    assert r.status_code == 409


def test_http_send_empty_digest_rejected(http):
    """The API rejects an empty preview_digest (schema min_length) — the gate can't
    be bypassed with "preview_digest": ""."""
    tid = _delegate_over_http(http)
    r = http.post(
        f"/todos/{tid}/delegate/send",
        json={"preview_digest": ""},
        headers=_headers(),
    )
    assert r.status_code == 422


def test_http_send_blocked_is_422(http):
    # Delegated, but no outbox configured → a precondition blocker.
    tid = _delegate_over_http(http)
    preview = http.post(
        f"/todos/{tid}/delegate/send/preview", json={}, headers=_headers()
    ).json()
    assert preview["can_send"] is False  # smtp unconfigured
    r = http.post(
        f"/todos/{tid}/delegate/send",
        json={"preview_digest": preview["digest"]},
        headers=_headers(),
    )
    assert r.status_code == 422


def test_http_send_duplicate_is_409(http):
    """A retry of an already-sent draft is refused with 409, not re-delivered."""
    tid = _delegate_over_http(http)
    http.post(
        "/smtp",
        json={"host": "smtp.test", "username": "me@x.com", "password": "pw", "sender": "me@x.com"},
        headers=_headers(),
    )
    preview = http.post(
        f"/todos/{tid}/delegate/send/preview", json={}, headers=_headers()
    ).json()
    http.app.state.smtp_client = SmtpClient(connect=lambda h, port, t: _FakeConn({}))
    first = http.post(
        f"/todos/{tid}/delegate/send",
        json={"preview_digest": preview["digest"]},
        headers=_headers(),
    )
    assert first.status_code == 200 and first.json()["sent"] is True
    # Retry with the same digest — must be refused (409) and never reach transport.
    record: dict = {}
    http.app.state.smtp_client = SmtpClient(connect=lambda h, port, t: _FakeConn(record))
    again = http.post(
        f"/todos/{tid}/delegate/send",
        json={"preview_digest": preview["digest"]},
        headers=_headers(),
    )
    assert again.status_code == 409
    assert record == {}
