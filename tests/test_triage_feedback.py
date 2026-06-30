"""Tests for the triage feedback loop: dropped intake todos evolve the prompt.

Covers the pure prompt-addendum builder, the store queries that separate signal
from avoidance noise (quick drops + repeat senders), the capture bridge
(:func:`record_drop_feedback`), the addendum's injection into the triage system
prompt, and the ``/mail/triage/learned`` endpoints. No network, no disk.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.mail.feedback import learned_corrections, record_drop_feedback
from prefrontal.mail.models import normalize_message
from prefrontal.mail.triage import (
    TRIAGE_SYSTEM_PROMPT,
    build_corrections_block,
    triage_message,
)
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "test-secret"


@pytest.fixture()
def store():
    """An in-memory, schema-initialized store kept open for the whole test."""
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def _mail_todo(store, *, sender_email="sarah@example.com", sender_name="Sarah",
               subject="Quick question", summary="asks for the report",
               category="reply", urgency="high", message_id="<m-1@e>") -> int:
    """Create a needs-action todo linked to a mail row, as ingest would. Returns todo id."""
    todo_id = store.add_todo(f"Reply to {sender_name}: {subject}")
    store.record_mail(
        account="personal",
        message_id=message_id,
        sender_email=sender_email,
        sender_name=sender_name,
        subject=subject,
        summary=summary,
        category=category,
        urgency=urgency,
        needs_action=True,
        todo_id=todo_id,
    )
    return todo_id


# -- pure builder ------------------------------------------------------------


def test_build_corrections_block_empty_is_blank():
    assert build_corrections_block([], []) == ""


def test_build_corrections_block_includes_senders_and_examples():
    block = build_corrections_block(
        [{"sender_email": "alerts@foo.com", "drops": 3}],
        [{"sender": "Bob", "subject": "Weekly metrics", "summary": "fyi"}],
    )
    assert "alerts@foo.com" in block
    assert "3 dropped" in block
    assert "Weekly metrics" in block
    assert block.startswith("\n")  # appends cleanly onto the base system prompt


# -- capture bridge ----------------------------------------------------------


def test_record_drop_feedback_skips_manual_todos(store):
    """A todo with no originating email is not a triage correction."""
    todo_id = store.add_todo("call the dentist")
    todo = store.get_todo(todo_id)
    assert record_drop_feedback(store, todo_id, todo, now=datetime(2026, 6, 30)) is None
    assert store.triage_feedback_list() == []


def test_record_drop_feedback_records_mail_todo_with_age(store):
    """Dropping a mail-sourced todo records the email's context and its age."""
    todo_id = _mail_todo(store)
    todo = store.get_todo(todo_id)
    created = datetime.strptime(todo["created_at"][:19], "%Y-%m-%d %H:%M:%S")
    fid = record_drop_feedback(store, todo_id, todo, now=created + timedelta(days=1))
    assert fid is not None
    rows = store.triage_feedback_list()
    assert len(rows) == 1
    assert rows[0]["sender_email"] == "sarah@example.com"
    assert rows[0]["subject"] == "Quick question"
    assert 0.9 < rows[0]["days_open"] < 1.1


# -- signal vs noise (store queries) -----------------------------------------


def test_quick_drops_exclude_slow_one_offs(store):
    """A single slow drop is treated as avoidance and never surfaced as signal."""
    store.record_triage_drop(
        todo_id=None, message_id="<a>", sender_email="late@x.com",
        sender_name="Late", subject="Old thing", summary="", category="reply",
        urgency="normal", days_open=9.0,
    )
    assert store.triage_recent_quick_drops(max_days=2.0) == []
    # ...and a one-off doesn't reach the repeat-sender threshold either.
    assert store.triage_dropped_senders(min_count=2) == []
    assert learned_corrections(store, quick_drop_days=2.0, repeat_threshold=2) == ""


def test_quick_drop_is_signal_even_once(store):
    """A drop that happened fast (before it could be avoided) is a clean signal."""
    store.record_triage_drop(
        todo_id=None, message_id="<a>", sender_email="news@x.com",
        sender_name="News", subject="Daily digest", summary="digest",
        category="newsletter", urgency="low", days_open=0.1,
    )
    quick = store.triage_recent_quick_drops(max_days=2.0)
    assert len(quick) == 1
    block = learned_corrections(store, quick_drop_days=2.0, repeat_threshold=2)
    assert "Daily digest" in block


def test_repeat_sender_is_signal_even_when_slow(store):
    """Repetition overrides slowness: a sender dropped repeatedly becomes a hint."""
    for i in range(2):
        store.record_triage_drop(
            todo_id=None, message_id=f"<{i}>", sender_email="repeat@x.com",
            sender_name="Repeat", subject=f"Thing {i}", summary="", category="reply",
            urgency="normal", days_open=8.0,  # slow each time
        )
    senders = store.triage_dropped_senders(min_count=2)
    assert len(senders) == 1
    assert senders[0]["sender_email"] == "repeat@x.com"
    assert senders[0]["drops"] == 2
    # Slow, so it's not in the quick-drop examples, but the sender hint stands.
    assert store.triage_recent_quick_drops(max_days=2.0) == []
    assert "repeat@x.com" in learned_corrections(store, repeat_threshold=2)


def test_forget_and_clear(store):
    fid = store.record_triage_drop(
        todo_id=None, message_id="<a>", sender_email="x@x.com", sender_name="X",
        subject="s", summary="", category="reply", urgency="low", days_open=0.1,
    )
    assert store.forget_triage_feedback(fid) is True
    assert store.forget_triage_feedback(fid) is False  # already gone
    store.record_triage_drop(
        todo_id=None, message_id="<b>", sender_email="y@y.com", sender_name="Y",
        subject="s", summary="", category="reply", urgency="low", days_open=0.1,
    )
    assert store.clear_triage_feedback() == 1
    assert store.triage_feedback_list() == []


# -- injection into the triage prompt ----------------------------------------


def _ollama_capturing(captured: list[str]) -> OllamaClient:
    """An OllamaClient that records each request's system prompt, returns no-action."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content).get("system", ""))
        return httpx.Response(
            200,
            json={"response": json.dumps(
                {"needs_action": True, "urgency": "high", "category": "reply",
                 "waiting_on": "", "summary": "x"}
            )},
        )

    return OllamaClient(transport=httpx.MockTransport(handler))


def test_corrections_appended_to_system_prompt():
    captured: list[str] = []
    client = _ollama_capturing(captured)
    item = normalize_message(
        {"message_id": "<m>", "from": "a@b.com", "subject": "hi", "body": "hi"},
        account="personal", policy="full",
    )
    triage_message(item, client=client, corrections="\nLEARNED-HINT-XYZ")
    assert len(captured) == 1
    assert captured[0].startswith(TRIAGE_SYSTEM_PROMPT)
    assert "LEARNED-HINT-XYZ" in captured[0]


# -- end to end through the app ----------------------------------------------


@pytest.fixture()
def app_bits(store):
    """A TestClient + the capture list, sharing the test's store."""
    captured: list[str] = []
    settings = Settings(webhook_secret=SECRET, mail_default_policy="full")
    app = create_app(store=store, settings=settings, ollama=_ollama_capturing(captured))
    with TestClient(app) as c:
        yield c, captured


def _h() -> dict:
    return {"X-Prefrontal-Token": SECRET}


def _sync(client, *msgs):
    body = {"account": "personal", "messages": list(msgs)}
    return client.post("/webhooks/mail/sync", json=body, headers=_h())


def _first_todo_id(client):
    return client.get("/mail", headers=_h()).json()["needs_action"][0]["todo_id"]


def _forget(client, fid):
    return client.post("/mail/triage/learned/forget", json={"id": fid}, headers=_h())


def test_drop_feeds_learned_then_next_sync_evolves_prompt(app_bits):
    client, captured = app_bits
    msg = {
        "message_id": "<m-1@e>",
        "from": '"Sarah Lee" <sarah@example.com>',
        "subject": "Daily standup notes",
        "date": "2026-06-28T10:30:00-07:00",
        "body": "FYI notes attached.",
    }
    # 1) Sync creates a needs-action todo for the message.
    _sync(client, msg)
    todo_id = _first_todo_id(client)
    assert todo_id is not None

    # 2) Drop it — a quick drop, so it's a clean false-positive signal.
    assert client.post(f"/todos/{todo_id}/drop", headers=_h()).status_code == 200

    # 3) It now shows up as something triage has learned.
    learned = client.get("/mail/triage/learned", headers=_h()).json()
    assert len(learned["recent"]) == 1
    assert learned["recent"][0]["sender_email"] == "sarah@example.com"
    assert "Daily standup notes" in learned["prompt_addendum"]

    # 4) The next sync's triage prompt carries that correction.
    captured.clear()
    _sync(client, dict(msg, message_id="<m-2@e>"))
    assert captured and "Daily standup notes" in captured[-1]


def test_learned_forget_and_clear_endpoints(app_bits):
    client, _ = app_bits
    msg = {"message_id": "<m@e>", "from": "n@x.com", "subject": "Digest", "body": "x",
           "date": "2026-06-28T10:30:00-07:00"}
    _sync(client, msg)
    client.post(f"/todos/{_first_todo_id(client)}/drop", headers=_h())

    fid = client.get("/mail/triage/learned", headers=_h()).json()["recent"][0]["id"]
    assert _forget(client, fid).status_code == 200
    assert _forget(client, fid).status_code == 404  # already gone
    assert client.get("/mail/triage/learned", headers=_h()).json()["recent"] == []

    # clear is a no-op-safe reset
    cleared = client.post("/mail/triage/learned/clear", headers=_h()).json()
    assert cleared == {"ok": True, "cleared": 0}
