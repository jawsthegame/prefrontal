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

from prefrontal.config import (
    Settings,
    _parse_account_domains,
    _parse_account_labels,
    _parse_calendar_labels,
    _parse_mail_accounts,
)
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.mail import ingest_messages, normalize_message, retriage_messages
from prefrontal.mail.feedback import learned_denylist
from prefrontal.mail.imap import (
    ImapAccount,
    _important_filter,
    _unseen_criteria,
    gmail_account_names,
)
from prefrontal.mail.models import MailItem, normalize_date, parse_sender
from prefrontal.mail.triage import (
    MailTriage,
    _heuristic_triage,
    priority_for_urgency,
    suppress_todo_reason,
    triage_message,
)
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


def test_settings_is_gmail_account():
    s = Settings(gmail_accounts=frozenset({"personal"}))
    assert s.is_gmail_account("personal") is True
    assert s.is_gmail_account("work") is False
    assert s.is_gmail_account(None) is False


def test_load_settings_resolves_gmail_accounts(monkeypatch):
    """load_settings classifies the configured account universe by IMAP host."""
    from prefrontal.config import load_settings

    monkeypatch.setenv("PREFRONTAL_MAIL_ACCOUNTS", "personal=full,work=signals")
    monkeypatch.setenv("MAIL_IMAP_HOST_WORK", "imap.corp.com")  # opt work out of Gmail
    monkeypatch.delenv("MAIL_IMAP_HOST_PERSONAL", raising=False)
    s = load_settings()
    # personal defaults to the Gmail host; work is pointed elsewhere.
    assert "personal" in s.gmail_accounts
    assert "work" not in s.gmail_accounts


def test_parse_account_labels():
    parsed = _parse_account_labels(
        "work=Vistar:orange, outlook=t-mobile:magenta"
    )
    assert parsed == (
        ("work", "Vistar", "orange"),
        ("outlook", "t-mobile", "magenta"),
    )


def test_parse_account_labels_optional_color_and_skips():
    # No color -> empty color; missing '=' or empty label/account -> skipped;
    # a label may itself contain ':' (color is split from the last colon).
    parsed = _parse_account_labels("plain=Personal, junk, =x, a:b=Foo:red")
    assert parsed == (("plain", "Personal", ""), ("a:b", "Foo", "red"))


def test_settings_account_label_map():
    s = Settings(
        account_labels=(("work", "Vistar", "orange"), ("outlook", "t-mobile", "magenta"))
    )
    assert s.account_label_map == {
        "work": {"label": "Vistar", "color": "orange"},
        "outlook": {"label": "t-mobile", "color": "magenta"},
    }
    assert Settings().account_label_map == {}


def test_parse_account_domains_and_map():
    parsed = _parse_account_domains("work@co=Work, me@gmail=home, junk, =x, a=")
    assert parsed == (("work@co", "work"), ("me@gmail", "home"))  # domain lowercased; junk skipped
    s = Settings(account_domains=parsed)
    assert s.account_domain_map == {"work@co": "work", "me@gmail": "home"}
    assert Settings().account_domain_map == {}


def test_parse_calendar_labels():
    parsed = _parse_calendar_labels(
        "personal=Personal:blue, work=Vistar:orange, outlook=T-Mobile:magenta"
    )
    assert parsed == (
        ("personal", "Personal", "blue"),
        ("work", "Vistar", "orange"),
        ("outlook", "T-Mobile", "magenta"),
    )


def test_settings_calendar_label_map():
    s = Settings(
        calendar_labels=(("personal", "Personal", "blue"), ("work", "Vistar", "orange"))
    )
    assert s.calendar_label_map == {
        "personal": {"label": "Personal", "color": "blue"},
        "work": {"label": "Vistar", "color": "orange"},
    }
    assert Settings().calendar_label_map == {}


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


def test_ingest_mirrors_actionable_mail_into_triage_log(store):
    """A needs-action message is mirrored into the unified triage feed (→ its todo)."""
    ingest_messages(
        store, [_msg()], account="personal", policy="full", client=_ollama_down()
    )
    (todo,) = store.open_todos()
    (row,) = store.recent_triage()
    assert row["source"] == "mail"
    assert (row["kind"], row["route"]) == ("action", "todo")
    assert row["routed_ref"] == f"todo:{todo['id']}"
    assert row["decided_by"] == "heuristic" and row["reason"].startswith("mail:")


def test_triage_log_count_matches_needs_action(store):
    """Only needs-action mail is mirrored — informational mail stays out of the feed."""
    msgs = [
        _msg(),  # a question → needs action
        _msg(**{
            "message_id": "<n-2@example.com>",
            "from": "News <noreply@list.example.com>",
            "subject": "Our weekly newsletter",
            "body": "This week in tech. Unsubscribe anytime.",
        }),
    ]
    summary = ingest_messages(
        store, msgs, account="personal", policy="full", client=_ollama_down()
    )
    assert len(store.recent_triage()) == summary.needs_action


def test_reingest_does_not_duplicate_the_triage_log_row(store):
    """Re-posting the same message is skipped (dedup) — no duplicate feed row / error."""
    for _ in range(2):
        ingest_messages(
            store, [_msg()], account="personal", policy="full", client=_ollama_down()
        )
    assert len(store.recent_triage()) == 1


def test_ingest_stamps_domain_on_mail_todo(store):
    """A work-mailbox todo inherits the account's domain (work/life guardrail)."""
    ingest_messages(
        store, [_msg()], account="work", policy="full", client=_ollama_down(), domain="work"
    )
    (todo,) = store.open_todos()
    assert todo["domain"] == "work"
    # No domain configured for the account → nothing stamped (unchanged behavior).
    ingest_messages(
        store,
        [_msg(message_id="<m2@example.com>")],
        account="personal",
        client=_ollama_down(),
    )
    other = next(t for t in store.open_todos() if t["id"] != todo["id"])
    assert other["domain"] is None


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


def test_mail_accounts_for_todos_maps_todo_to_inbox(store):
    ingest_messages(store, [_msg()], account="work", client=_ollama_down())
    manual = store.add_todo("buy milk")  # no originating mail
    todos = store.open_todos()
    mail_todo = next(t["id"] for t in todos if t["id"] != manual)

    accounts = store.mail_accounts_for_todos([t["id"] for t in todos])
    assert accounts == {mail_todo: "work"}
    # Manual todo has no account entry; empty input is a no-op.
    assert manual not in accounts
    assert store.mail_accounts_for_todos([]) == {}


def test_mail_sources_for_todos_returns_account_and_ids(store):
    ingest_messages(
        store,
        [_msg(message_id="<m-42@example.com>", threadId="thread-abc")],
        account="work",
        client=_ollama_down(),
    )
    manual = store.add_todo("buy milk")  # no originating mail
    todos = store.open_todos()
    mail_todo = next(t["id"] for t in todos if t["id"] != manual)

    sources = store.mail_sources_for_todos([t["id"] for t in todos])
    assert sources[mail_todo] == {
        "account": "work",
        "message_id": "<m-42@example.com>",
        "thread_id": "thread-abc",
    }
    # Manual todo has no mail source; empty input is a no-op.
    assert manual not in sources
    assert store.mail_sources_for_todos([]) == {}


def test_gmail_account_names_classifies_by_host():
    """An account is Gmail when its resolved IMAP host is Gmail (default = Gmail)."""
    env = {"MAIL_IMAP_HOST_WORK": "imap.corp.com"}  # personal has no host -> default
    gmail = gmail_account_names(("personal", "work"), env=env)
    assert gmail == frozenset({"personal"})
    assert gmail_account_names((), env=env) == frozenset()


def test_gmail_message_url_builds_rfc822msgid_search():
    from prefrontal.memory.store import gmail_message_url

    url = gmail_message_url("<CAF+abc@mail.gmail.com>")
    # Angle brackets are stripped; the id is URL-encoded into an rfc822msgid search.
    assert url == (
        "https://mail.google.com/mail/u/0/#search/"
        "rfc822msgid:CAF%2Babc%40mail.gmail.com"
    )
    assert gmail_message_url(None) is None
    assert gmail_message_url("   ") is None
    assert gmail_message_url("<>") is None


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


# -- re-triage ---------------------------------------------------------------


def _flag_verdict() -> dict:
    return {
        "needs_action": True,
        "urgency": "high",
        "category": "reply",
        "waiting_on": "Sarah",
        "summary": "Sarah asks for the Q2 report",
    }


def _noaction_verdict() -> dict:
    return {
        "needs_action": False,
        "urgency": "low",
        "category": "newsletter",
        "waiting_on": "",
        "summary": "Marketing update, no action",
    }


def test_retriage_clears_overflagged_mail_and_drops_todo(store):
    """A message the new prompt no longer flags is cleared and its todo dropped."""
    ingest_messages(store, [_msg()], account="personal", client=_ollama_returning(_flag_verdict()))
    todo_id = store.open_todos()[0]["id"]
    assert len(store.mail_needing_action()) == 1

    summary = retriage_messages(
        store, account="personal", client=_ollama_returning(_noaction_verdict())
    )

    assert summary.scanned == 1
    assert summary.cleared == 1
    assert summary.todos_dropped == 1
    assert store.mail_needing_action() == []
    assert store.get_todo(todo_id)["status"] == "dropped"
    row = store.recent_mail()[0]
    assert row["needs_action"] == 0
    assert row["category"] == "newsletter"


def test_retriage_does_not_record_drop_feedback(store):
    """Auto-drops from re-triage must NOT pollute learned corrections."""
    ingest_messages(store, [_msg()], account="personal", client=_ollama_returning(_flag_verdict()))
    retriage_messages(
        store, account="personal", client=_ollama_returning(_noaction_verdict())
    )
    # The prompt's own judgment isn't user feedback — no correction recorded.
    assert store.triage_feedback_list() == []


def test_retriage_dry_run_writes_nothing(store):
    """--dry-run reports the change but leaves the row and todo untouched."""
    ingest_messages(store, [_msg()], account="personal", client=_ollama_returning(_flag_verdict()))
    todo_id = store.open_todos()[0]["id"]

    summary = retriage_messages(
        store,
        account="personal",
        client=_ollama_returning(_noaction_verdict()),
        dry_run=True,
    )

    assert summary.dry_run is True
    assert summary.cleared == 1
    assert summary.todos_dropped == 1  # counted, but not performed
    assert store.get_todo(todo_id)["status"] == "open"
    assert len(store.mail_needing_action()) == 1


def test_retriage_only_needs_action_leaves_cleared_mail_alone(store):
    """The default scope re-triages only flagged mail, not already-cleared rows."""
    # One flagged, one not (heuristic newsletter → no action).
    ingest_messages(store, [_msg()], account="personal", client=_ollama_returning(_flag_verdict()))
    ingest_messages(
        store,
        [_msg(message_id="<m-2@x>", subject="Newsletter", body="unsubscribe here")],
        account="personal",
        client=_ollama_returning(_noaction_verdict()),
    )
    summary = retriage_messages(
        store, account="personal", client=_ollama_returning(_noaction_verdict())
    )
    assert summary.scanned == 1  # only the flagged one


def test_retriage_all_can_newly_flag_and_create_todo(store):
    """--all re-triages everything and can flag a previously-cleared message."""
    ingest_messages(
        store,
        [_msg(subject="FYI", body="just so you know")],
        account="personal",
        client=_ollama_returning(_noaction_verdict()),
    )
    assert store.open_todos() == []

    summary = retriage_messages(
        store,
        account="personal",
        only_needs_action=False,
        client=_ollama_returning(_flag_verdict()),
    )

    assert summary.scanned == 1
    assert summary.newly_flagged == 1
    assert summary.todos_created == 1
    assert len(store.mail_needing_action()) == 1
    assert len(store.open_todos()) == 1


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


# -- todo-creation suppression (mail-triage false-positive gate) --------------


def _item(sender_email="sarah@example.com", sender_name="Sarah", subject="Q?"):
    return MailItem(
        account="personal",
        message_id="<x@example.com>",
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject,
    )


def test_suppress_reason_allows_a_real_reply():
    """A direct reply from a real person still creates a todo (returns None)."""
    verdict = MailTriage(needs_action=True, category="reply")
    assert suppress_todo_reason(_item(), verdict) is None


@pytest.mark.parametrize(
    "sender_email",
    [
        "no-reply@vistarmedia.com",
        "noreply@service.com",
        "notifications@github.com",
        "DoNotReply@bank.com",
    ],
)
def test_suppress_reason_blocks_no_reply_style_senders(sender_email):
    """no-reply / notifications senders never spawn a todo, even if flagged."""
    verdict = MailTriage(needs_action=True, category="reply")
    assert suppress_todo_reason(_item(sender_email=sender_email), verdict) == (
        "no-reply-sender"
    )


@pytest.mark.parametrize("category", ["notification", "newsletter", "fyi"])
def test_suppress_reason_blocks_informational_categories(category):
    """Informational categories are recorded but don't become open loops."""
    verdict = MailTriage(needs_action=True, category=category)
    reason = suppress_todo_reason(_item(), verdict)
    assert reason == f"non-actionable-category:{category}"


def test_suppress_reason_blocks_denylisted_sender():
    """A learned repeat-dropped sender is hard-suppressed by exact address."""
    verdict = MailTriage(needs_action=True, category="reply")
    deny = frozenset({"spam@vendor.com"})
    assert (
        suppress_todo_reason(
            _item(sender_email="spam@vendor.com"), verdict, denylisted_senders=deny
        )
        == "denylisted-sender"
    )
    # A sender not on the list is unaffected.
    assert (
        suppress_todo_reason(
            _item(sender_email="real@vendor.com"), verdict, denylisted_senders=deny
        )
        is None
    )


def test_ingest_suppresses_todo_for_no_reply_sender_but_keeps_record(store):
    """A no-reply sender the model flags is recorded + needs_action, no todo."""
    client = _ollama_returning(
        {"needs_action": True, "urgency": "normal", "category": "reply", "summary": "x"}
    )
    msg = _msg(
        message_id="<vistar-1@x>", **{"from": "Vistar <no-reply@vistarmedia.com>"}
    )
    summary = ingest_messages(store, [msg], account="personal", client=client)

    assert summary.needs_action == 1  # still flagged on the record
    assert summary.todos_created == 0  # but no open loop
    assert summary.todos_suppressed == 1
    assert store.open_todos() == []

    # Mail-record-only: still visible in /mail's needs-action view, todo unset.
    action_items = store.mail_needing_action()
    assert len(action_items) == 1
    assert action_items[0]["todo_id"] is None


def test_ingest_honors_denylisted_senders(store):
    """A denylisted sender is gated out of todo creation at ingest."""
    client = _ollama_returning(
        {"needs_action": True, "urgency": "high", "category": "reply", "summary": "x"}
    )
    msg = _msg(message_id="<d-1@x>", **{"from": "Spam <spam@vendor.com>"})
    summary = ingest_messages(
        store,
        [msg],
        account="personal",
        client=client,
        denylisted_senders=frozenset({"spam@vendor.com"}),
    )
    assert summary.todos_created == 0
    assert summary.todos_suppressed == 1
    assert store.open_todos() == []


def test_learned_denylist_needs_repeat_drops(store):
    """learned_denylist returns only senders dropped >= repeat_threshold times."""
    for i in range(2):
        store.record_triage_drop(
            todo_id=None,
            message_id=f"<r-{i}@x>",
            sender_email="repeat@vendor.com",
            sender_name="Repeat",
            subject="s",
            summary="s",
            category="reply",
            urgency="normal",
            days_open=0.1,
        )
    store.record_triage_drop(
        todo_id=None,
        message_id="<solo@x>",
        sender_email="solo@vendor.com",
        sender_name="Solo",
        subject="s",
        summary="s",
        category="reply",
        urgency="normal",
        days_open=0.1,
    )
    deny = learned_denylist(store, repeat_threshold=2)
    assert "repeat@vendor.com" in deny
    assert "solo@vendor.com" not in deny
