"""Tests for resolving a double-booking with a reschedule request.

Three layers, mirroring ``test_delegation.py``: the pure side-picking + draft
logic (:func:`pick_move_side`, :func:`build_reschedule_draft`), the send path
(:func:`send_reschedule_draft` over a fake SMTP transport), and the HTTP surface
(``POST /commitments/conflicts/reschedule`` — preview, send-without-SMTP, and the
not-found case).
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
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.integrations.smtp import SmtpClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.reschedule import (
    STATUS_DRAFTED,
    STATUS_FAILED,
    STATUS_FORWARDED,
    RescheduleDraft,
    build_reschedule_draft,
    pick_move_side,
    send_reschedule_draft,
)

SECRET = "reschedule-secret"


def _ollama_json(payload: dict) -> OllamaClient:
    """An OllamaClient whose generate() returns ``payload`` as the model's JSON reply."""
    text = json.dumps(payload)
    return OllamaClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"response": text})
    ))


def _ollama_text(text: str) -> OllamaClient:
    """An OllamaClient whose generate() returns ``text`` verbatim (non-JSON prose)."""
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


# -- side picking ------------------------------------------------------------


def _commit(id_, title, start, *, hardness="soft"):
    return {"id": id_, "title": title, "start_at": start, "hardness": hardness}


def test_pick_move_side_moves_the_soft_over_the_hard():
    """A hard, must-happen commitment is kept; the soft one moves — regardless of order."""
    hard = _commit(1, "Board meeting", "2026-07-14 10:00:00", hardness="hard")
    soft = _commit(2, "Coffee catch-up", "2026-07-14 10:15:00", hardness="soft")
    keep, move = pick_move_side(hard, soft)
    assert keep is hard and move is soft
    # Symmetric: order of arguments doesn't change the verdict.
    keep2, move2 = pick_move_side(soft, hard)
    assert keep2 is hard and move2 is soft


def test_pick_move_side_moves_the_later_when_equally_firm():
    """Two soft blocks → move the later-starting one (smaller disruption)."""
    early = _commit(1, "Standup", "2026-07-14 09:00:00")
    late = _commit(2, "Sync", "2026-07-14 09:20:00")
    keep, move = pick_move_side(early, late)
    assert keep is early and move is late


def test_pick_move_side_tiebreaks_on_id_when_same_start():
    """Same firmness and start → the later-added row (higher id) moves."""
    a = _commit(5, "A", "2026-07-14 09:00:00")
    b = _commit(9, "B", "2026-07-14 09:00:00")
    keep, move = pick_move_side(a, b)
    assert keep is a and move is b


# -- draft generation --------------------------------------------------------


def test_build_draft_offline_is_honest_and_offers_slots():
    """No model → a heuristic draft naming the appointment and offering slots."""
    draft = build_reschedule_draft(
        move_title="Dentist",
        move_when="Tue Jul 14, 10:00 AM",
        recipient_name="Dr. Lee",
        owner_name="Tom",
        slots=["Wed Jul 15, 2:00 PM–2:30 PM"],
        client=None,
    )
    assert draft.offline is True
    assert "Dentist" in draft.body
    assert "Dr. Lee" in draft.body
    assert "Wed Jul 15" in draft.body
    assert draft.body.rstrip().endswith("Tom")  # signed off
    assert "Dentist" in draft.subject


def test_build_draft_offline_uses_placeholder_without_owner_name():
    draft = build_reschedule_draft(move_title="Call", client=None)
    assert "[your name]" in draft.body


def test_build_draft_with_model_returns_subject_and_body():
    client = _ollama_json(
        {"subject": "Can we move our sync?", "body": "Hi — a conflict came up..."}
    )
    draft = build_reschedule_draft(move_title="Sync", client=client)
    assert draft.offline is False
    assert draft.subject == "Can we move our sync?"
    assert draft.body == "Hi — a conflict came up..."


def test_build_draft_salvages_prose_reply():
    """A prose (non-JSON) reply becomes the body rather than falling to the heuristic."""
    client = _ollama_text("Hey, something came up and I need to move our meeting — free Thursday?")
    draft = build_reschedule_draft(move_title="1:1", client=client)
    assert draft.offline is False
    assert draft.body.startswith("Hey, something came up")
    assert draft.subject == "Reschedule request: 1:1"


def test_build_draft_model_down_falls_back_to_heuristic():
    def refuse(request):
        raise httpx.ConnectError("down")

    draft = build_reschedule_draft(
        move_title="Review", client=OllamaClient(transport=httpx.MockTransport(refuse))
    )
    assert draft.offline is True
    assert "Review" in draft.body


def test_build_draft_feeds_context_to_the_model():
    """Recipient, owner, note and slots all reach the prompt."""
    seen = {}

    def capture(request):
        seen["prompt"] = json.loads(request.content)["prompt"]
        return httpx.Response(200, json={"response": json.dumps({"subject": "s", "body": "b"})})

    client = OllamaClient(transport=httpx.MockTransport(capture))
    build_reschedule_draft(
        move_title="Lunch",
        recipient_name="Sam",
        owner_name="Tom",
        note="sorry for the short notice",
        slots=["Fri, 12:00 PM–1:00 PM"],
        client=client,
    )
    assert "Sam" in seen["prompt"]
    assert "Tom" in seen["prompt"]
    assert "short notice" in seen["prompt"]
    assert "Fri, 12:00 PM" in seen["prompt"]


# -- send path ---------------------------------------------------------------


_DRAFT = RescheduleDraft(subject="Reschedule request: X", body="Hi — ...")


def test_send_no_recipient_fails_and_keeps_draft():
    result = send_reschedule_draft(_DRAFT, recipient=None, smtp=None)
    assert result.status == STATUS_FAILED
    assert "no recipient" in result.detail
    assert result.body == _DRAFT.body  # nothing lost


def test_send_no_smtp_fails_and_keeps_draft():
    result = send_reschedule_draft(_DRAFT, recipient="them@x.com", smtp=None)
    assert result.status == STATUS_FAILED
    assert "SMTP not configured" in result.detail


def test_send_over_smtp_forwards():
    record: dict = {}
    smtp = sources.SmtpSource(
        account="default", host="smtp.test", port=587,
        username="me@x.com", password="pw", sender="me@x.com",
    )
    result = send_reschedule_draft(
        _DRAFT, recipient="them@x.com", smtp=smtp,
        smtp_client=SmtpClient(connect=lambda h, p, t: _FakeConn(record)),
    )
    assert result.status == STATUS_FORWARDED
    assert record["to"] == "them@x.com"
    assert record["subject"] == "Reschedule request: X"
    assert result.recipient == "them@x.com"


def test_send_transport_error_is_caught():
    smtp = sources.SmtpSource(
        account="default", host="smtp.test", port=587,
        username="u", password="p", sender="u@x",
    )
    result = send_reschedule_draft(
        _DRAFT, recipient="them@x.com", smtp=smtp,
        smtp_client=SmtpClient(connect=lambda h, p, t: _FakeConn({}, fail_on="starttls")),
    )
    assert result.status == STATUS_FAILED
    assert "send failed" in result.detail


# -- HTTP surface ------------------------------------------------------------


@pytest.fixture()
def secret_env(monkeypatch):
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def http(secret_env):
    """A TestClient with a deterministic (mock) model reply for the draft."""
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "me", display_name="Tom", token=SECRET, is_operator=True)
    from prefrontal.webhooks.app import create_app

    ollama = _ollama_json({"subject": "Move our meeting?", "body": "Hi — a conflict came up."})
    app = create_app(store=unscoped, settings=Settings(), ollama=ollama)
    with TestClient(app) as c:
        yield c
    conn.close()


def _headers():
    return {"X-Prefrontal-Token": SECRET}


def _make_conflict(http) -> str:
    """Create two overlapping commitments and return the conflict's dismissal key."""
    http.post(
        "/commitments",
        json={"title": "Board meeting", "start_at": "2026-07-14T10:00:00Z",
              "end_at": "2026-07-14T11:00:00Z", "hard": True},
        headers=_headers(),
    )
    http.post(
        "/commitments",
        json={"title": "Coffee", "start_at": "2026-07-14T10:30:00Z",
              "end_at": "2026-07-14T11:00:00Z"},
        headers=_headers(),
    )
    conflicts = http.get("/commitments/conflicts", headers=_headers()).json()["conflicts"]
    assert conflicts, "expected a hard double-booking"
    return conflicts[0]["key"]


def test_http_reschedule_preview_does_not_send(http):
    key = _make_conflict(http)
    r = http.post(
        "/commitments/conflicts/reschedule",
        json={"key": key, "recipient_name": "Sam"}, headers=_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == STATUS_DRAFTED
    assert body["subject"] == "Move our meeting?"
    assert body["dismissed"] is None
    # The soft "Coffee" is the one moved; the hard board meeting is kept.
    assert body["moved"]["title"] == "Coffee"
    assert body["kept"]["title"] == "Board meeting"
    # Preview does not touch the conflict — it still shows.
    conflicts = http.get("/commitments/conflicts", headers=_headers()).json()["conflicts"]
    assert any(c["key"] == key for c in conflicts)


def test_http_reschedule_send_without_smtp_fails_gracefully(http):
    key = _make_conflict(http)
    r = http.post(
        "/commitments/conflicts/reschedule",
        json={"key": key, "to": "sam@x.com", "send": True}, headers=_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == STATUS_FAILED
    assert "SMTP not configured" in body["detail"]
    assert body["dismissed"] is None  # not sent → conflict still stands
    assert body["body"]  # draft kept


def test_http_reschedule_move_override_flips_side(http):
    key = _make_conflict(http)
    r = http.post(
        "/commitments/conflicts/reschedule",
        json={"key": key, "move": "a"}, headers=_headers(),
    ).json()
    conflicts = http.get("/commitments/conflicts", headers=_headers()).json()
    pair = next(c for c in conflicts["conflicts"] if c["key"] == key)
    assert r["moved"]["id"] == pair["a"]["id"]


def test_http_reschedule_unknown_key_404s(http):
    r = http.post(
        "/commitments/conflicts/reschedule",
        json={"key": "nope::nope"}, headers=_headers(),
    )
    assert r.status_code == 404


def test_http_reschedule_offers_open_slots(http):
    key = _make_conflict(http)
    r = http.post(
        "/commitments/conflicts/reschedule",
        json={"key": key, "offer_slots": True}, headers=_headers(),
    ).json()
    assert isinstance(r["slots"], list) and r["slots"]  # some open time was found
