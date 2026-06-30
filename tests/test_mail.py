"""Tests for the mail ingestion pipeline.

Covers normalization (including per-account retention policy), the deterministic
heuristic triage, config policy parsing, the ingest orchestration (dedup, todo
creation, episode logging), and the ``/webhooks/mail/sync`` + ``/mail`` routes.

The Ollama client is exercised with an ``httpx.MockTransport`` so nothing touches
a real server: one handler returns a valid JSON verdict (the model path), and a
500-handler forces the heuristic fallback. No network, no disk.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings, _parse_mail_accounts
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.mail import ingest_messages, normalize_message
from prefrontal.mail.imap import ImapAccount, _important_filter, _unseen_criteria
from prefrontal.mail.models import normalize_date, parse_sender
from prefrontal.mail.triage import _heuristic_triage, priority_for_urgency, triage_message
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "test-secret"


# -- fixtures / helpers ------------------------------------------------------


@pytest.fixture()
def store():
    """An in-memory, schema-initialized store kept open for the whole test."""
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def _ollama_returning(verdict: dict) -> OllamaClient:
    """An OllamaClient whose /api/generate returns ``verdict`` as JSON text."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": json.dumps(verdict)})

    return OllamaClient(transport=httpx.MockTransport(handler))


def _ollama_down() -> OllamaClient:
    """An OllamaClient whose calls fail, forcing the heuristic fallback."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    return OllamaClient(transport=httpx.MockTransport(handler))


def _msg(**overrides) -> dict:
    """A raw message dict with sensible defaults, overridable per test."""
    base = {
        "message_id": "<m-1@example.com>",
        "from": '"Sarah Lee" <sarah@example.com>',
        "subject": "Quick question about the report",
        "date": "2026-06-28T10:30:00-07:00",
        "body": "Hi, can you send me the Q2 report? Thanks!",
    }
    base.update(overrides)
    return base


# -- IMAP: Gmail "Important only" --------------------------------------------


def test_imap_account_gmail_defaults_to_important_only():
    """A Gmail account fetches only Important mail unless told otherwise."""
    env = {"MAIL_IMAP_USER_PERSONAL": "me@gmail.com", "MAIL_IMAP_PASSWORD_PERSONAL": "pw"}
    acct = ImapAccount.from_env("personal", env=env)
    assert acct.is_gmail is True
    assert acct.important_only is True


def test_imap_account_nongmail_not_important_only():
    """A non-Gmail host can't use the Gmail extension, so it stays off."""
    env = {
        "MAIL_IMAP_USER_WORK": "me@corp.com",
        "MAIL_IMAP_PASSWORD_WORK": "pw",
        "MAIL_IMAP_HOST_WORK": "imap.corp.com",
    }
    acct = ImapAccount.from_env("work", env=env)
    assert acct.is_gmail is False
    assert acct.important_only is False


def test_imap_account_important_only_env_override():
    """MAIL_IMAP_IMPORTANT_ONLY_<NAME> overrides the Gmail default."""
    env = {
        "MAIL_IMAP_USER_PERSONAL": "me@gmail.com",
        "MAIL_IMAP_PASSWORD_PERSONAL": "pw",
        "MAIL_IMAP_IMPORTANT_ONLY_PERSONAL": "false",
    }
    assert ImapAccount.from_env("personal", env=env).important_only is False


def test_important_filter_only_for_gmail_important():
    """The X-GM-RAW filter is added only for a Gmail + important_only account."""
    gmail = ImapAccount("personal", "imap.gmail.com", "u", "p", important_only=True)
    assert _important_filter(gmail) == ("X-GM-RAW", '"is:important"')
    # Non-Gmail (even if flagged) and plain Gmail without the flag: no filter.
    assert _important_filter(ImapAccount("w", "imap.corp.com", "u", "p", important_only=True)) == ()
    assert _important_filter(ImapAccount("p", "imap.gmail.com", "u", "p")) == ()


# -- normalization -----------------------------------------------------------


def test_normalize_full_keeps_body_and_parses_sender():
    item = normalize_message(_msg(), account="personal", policy="full")
    assert item.message_id == "<m-1@example.com>"
    assert item.sender_name == "Sarah Lee"
    assert item.sender_email == "sarah@example.com"
    assert item.subject == "Quick question about the report"
    assert item.body and "Q2 report" in item.body
    assert item.snippet  # derived from the body
    assert item.received_at == "2026-06-28 17:30:00"  # normalized to UTC


def test_normalize_signals_drops_content():
    """Under the signals policy, body and snippet are dropped at the door."""
    item = normalize_message(_msg(), account="corp", policy="signals")
    assert item.subject == "Quick question about the report"
    assert item.sender_email == "sarah@example.com"
    assert item.body is None
    assert item.snippet is None


def test_normalize_unknown_policy_is_treated_as_signals():
    item = normalize_message(_msg(), account="x", policy="bogus")
    assert item.policy == "signals"
    assert item.body is None


def test_normalize_requires_a_message_id():
    with pytest.raises(ValueError):
        normalize_message({"subject": "no id here"}, account="personal")


def test_normalize_bad_date_is_none_not_error():
    item = normalize_message(_msg(date="not a date"), account="personal", policy="full")
    assert item.received_at is None


def test_normalize_date_accepts_rfc2822():
    assert normalize_date("Mon, 29 Jun 2026 10:30:00 -0700") == "2026-06-29 17:30:00"


def test_parse_sender_split_fields_win():
    name, email = parse_sender({"sender_name": "Bob", "sender_email": "BOB@X.COM"})
    assert name == "Bob"
    assert email == "bob@x.com"  # lower-cased


# -- heuristic triage --------------------------------------------------------


def test_heuristic_flags_question_as_reply():
    v = _heuristic_triage(normalize_message(_msg(), account="p", policy="full"))
    assert v.needs_action is True
    assert v.category == "reply"
    assert v.source == "heuristic"


def test_heuristic_newsletter_is_no_action():
    raw = _msg(
        message_id="<n-1>",
        **{"from": "news@brand.com"},
        subject="Your weekly newsletter — unsubscribe anytime",
        body="Lots of news. Click to unsubscribe.",
    )
    v = _heuristic_triage(normalize_message(raw, account="p", policy="full"))
    assert v.needs_action is False
    assert v.urgency == "low"
    assert v.category == "newsletter"


def test_heuristic_urgent_subject_bumps_urgency():
    raw = _msg(message_id="<u-1>", subject="URGENT: can you approve this today?")
    v = _heuristic_triage(normalize_message(raw, account="p", policy="full"))
    assert v.needs_action is True
    assert v.urgency == "urgent"


def test_priority_mapping():
    assert priority_for_urgency("urgent") == 3
    assert priority_for_urgency("low") == 0
    assert priority_for_urgency(None) == 1  # default normal


# -- triage with the model (mock transport) ---------------------------------


def test_triage_uses_model_verdict_when_available():
    client = _ollama_returning(
        {
            "needs_action": True,
            "urgency": "high",
            "category": "reply",
            "waiting_on": "Sarah",
            "summary": "Sarah asks for the Q2 report",
        }
    )
    item = normalize_message(_msg(), account="p", policy="full")
    v = triage_message(item, client=client)
    assert v.source == "llm"
    assert v.urgency == "high"
    assert v.waiting_on == "Sarah"


def test_triage_falls_back_to_heuristic_when_model_down():
    item = normalize_message(_msg(), account="p", policy="full")
    v = triage_message(item, client=_ollama_down())
    assert v.source == "heuristic"


def test_use_model_false_skips_the_model():
    """A working model is ignored when use_model=False (backlog-clear path)."""
    boom = _ollama_returning({"needs_action": True, "urgency": "urgent"})
    item = normalize_message(_msg(), account="p", policy="full")
    v = triage_message(item, client=boom, use_model=False)
    assert v.source == "heuristic"


# -- config policy parsing ---------------------------------------------------


def test_unseen_criteria_bounded():
    """A positive window yields UNSEEN SINCE <DD-Mon-YYYY>."""
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    assert _unseen_criteria(30, now) == ("UNSEEN", "SINCE", "31-May-2026")


def test_unseen_criteria_unbounded():
    """No/zero window searches all unread."""
    assert _unseen_criteria(None, None) == ("UNSEEN",)
    assert _unseen_criteria(0, None) == ("UNSEEN",)


def test_parse_mail_accounts():
    parsed = _parse_mail_accounts("personal=full, work=full ,corp=signals")
    assert dict(parsed) == {"personal": "full", "work": "full", "corp": "signals"}


def test_parse_mail_accounts_defaults_and_coercion():
    # No '=' -> full; bad policy -> signals.
    parsed = dict(_parse_mail_accounts("a, b=weird, =skip"))
    assert parsed == {"a": "full", "b": "signals"}


def test_settings_policy_for():
    s = Settings(mail_accounts=(("corp", "signals"),), mail_default_policy="full")
    assert s.policy_for("corp") == "signals"
    assert s.policy_for("anything-else") == "full"


# -- ingest orchestration ----------------------------------------------------


def test_ingest_creates_todo_and_episode_for_action(store):
    summary = ingest_messages(
        store, [_msg()], account="personal", policy="full", client=_ollama_down()
    )
    assert summary.ingested == 1
    assert summary.needs_action == 1
    assert summary.todos_created == 1

    todos = store.open_todos()
    assert len(todos) == 1
    assert "Sarah" in todos[0]["title"]

    episodes = store.episodes_by_type("mail")
    assert len(episodes) == 1
    assert episodes[0]["channel"] == "personal"

    action_items = store.mail_needing_action()
    assert len(action_items) == 1
    assert action_items[0]["todo_id"] == todos[0]["id"]


def test_ingest_is_idempotent_on_message_id(store):
    msgs = [_msg()]
    first = ingest_messages(store, msgs, account="personal", client=_ollama_down())
    second = ingest_messages(store, msgs, account="personal", client=_ollama_down())
    assert first.ingested == 1
    assert second.ingested == 0
    assert second.skipped == 1
    # Only one row and one todo despite two ingest passes.
    assert len(store.recent_mail()) == 1
    assert len(store.open_todos()) == 1


def test_ingest_signals_policy_stores_no_body(store):
    ingest_messages(
        store, [_msg()], account="corp", policy="signals", client=_ollama_down()
    )
    row = store.recent_mail()[0]
    assert row["policy"] == "signals"
    assert row["body"] is None
    assert row["snippet"] is None
    assert row["subject"]  # subject + sender retained


def test_ingest_skips_messages_without_id(store):
    summary = ingest_messages(
        store, [{"subject": "no id"}], account="p", client=_ollama_down()
    )
    assert summary.invalid == 1
    assert summary.ingested == 0


def test_closing_todo_clears_mail_action_item(store):
    ingest_messages(store, [_msg()], account="personal", client=_ollama_down())
    todo_id = store.open_todos()[0]["id"]
    store.close_todo(todo_id, status="done")
    assert store.mail_needing_action() == []


# -- webhook routes ----------------------------------------------------------


@pytest.fixture()
def client(store):
    """A TestClient with auth + an injected model that returns a fixed verdict."""
    settings = Settings(
        webhook_secret=SECRET,
        mail_accounts=(("corp", "signals"),),
        mail_default_policy="full",
    )
    ollama = _ollama_returning(
        {
            "needs_action": True,
            "urgency": "high",
            "category": "reply",
            "waiting_on": "Sarah",
            "summary": "Sarah asks for the Q2 report",
        }
    )
    app = create_app(store=store, settings=settings, ollama=ollama)
    with TestClient(app) as c:
        yield c


def test_mail_sync_requires_auth(client):
    assert client.post("/webhooks/mail/sync", json={"account": "p"}).status_code == 401


def test_mail_sync_ingests_and_lists(client):
    resp = client.post(
        "/webhooks/mail/sync",
        json={"account": "personal", "messages": [_msg()]},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["ingested"] == 1
    assert body["needs_action"] == 1
    assert body["policy"] == "full"  # personal unconfigured -> mail_default_policy

    listing = client.get("/mail", headers={"X-Prefrontal-Token": SECRET}).json()
    assert len(listing["needs_action"]) == 1
    assert listing["needs_action"][0]["urgency"] == "high"


def test_mail_sync_honors_configured_signals_policy(client):
    resp = client.post(
        "/webhooks/mail/sync",
        json={"account": "corp", "messages": [_msg()]},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 201
    assert resp.json()["policy"] == "signals"
    listing = client.get("/mail", headers={"X-Prefrontal-Token": SECRET}).json()
    assert listing["recent"][0]["body"] is None


def test_mail_sync_rejects_blank_account(client):
    resp = client.post(
        "/webhooks/mail/sync",
        json={"account": "  ", "messages": []},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 422
