"""Phase 2: per-user calendar — ICS parsing, source registry, and sync CLI.

Guarantees: the in-app ICS parser matches the old n8n behavior (TZID, all-day,
declined/cancelled drops, namespaced ids), feed URLs are sealed at rest, and
`calendar sync --all-users` lands each user's own events only in their scope.
"""

from __future__ import annotations

import pytest

from prefrontal.cli import main
from prefrontal.config import get_settings
from prefrontal.crypto import generate_key
from prefrontal.ics import parse_ics
from prefrontal.memory.store import MemoryStore
from prefrontal.sources import ICS, ics_sources, put_ics_source
from tests.conftest import scoped_default

SAMPLE = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:evt-1
SUMMARY:Team sync
DTSTART;TZID=America/New_York:20990707T090000
DTEND;TZID=America/New_York:20990707T093000
LOCATION:Room 4
RRULE:FREQ=WEEKLY;BYDAY=TU
EXDATE;TZID=America/New_York:20990714T090000
END:VEVENT
BEGIN:VEVENT
UID:evt-utc
SUMMARY:Standup
DTSTART:20990708T140000Z
END:VEVENT
BEGIN:VEVENT
UID:evt-allday
SUMMARY:Holiday
DTSTART;VALUE=DATE:20990709
END:VEVENT
BEGIN:VEVENT
UID:evt-declined
SUMMARY:Declined
DTSTART:20990710T140000Z
ATTENDEE;PARTSTAT=DECLINED:mailto:Me@Example.com
END:VEVENT
BEGIN:VEVENT
UID:evt-cancelled
SUMMARY:Cancelled
STATUS:CANCELLED
DTSTART:20990711T140000Z
END:VEVENT
END:VCALENDAR"""


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


@pytest.fixture()
def secret_env(monkeypatch):
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# -- parser ------------------------------------------------------------------


def test_parse_ics_fields_and_namespace():
    events = parse_ics(SAMPLE, namespace="work", me_emails=("me@example.com",))
    by_id = {e["external_id"]: e for e in events}
    # declined + cancelled are dropped; 3 remain.
    assert set(by_id) == {"work:evt-1", "work:evt-utc", "work:evt-allday"}
    tz = by_id["work:evt-1"]
    assert tz["title"] == "Team sync"
    assert tz["start_at"] == "2099-07-07T09:00:00"
    assert tz["tzid"] == "America/New_York"
    assert tz["end_at"] == "2099-07-07T09:30:00"
    assert tz["rrule"] == "FREQ=WEEKLY;BYDAY=TU"
    assert tz["exdate"] == ["2099-07-14T09:00:00"]
    assert tz["location"] == "Room 4"
    # UTC 'Z' start carries no tzid; all-day is date-only.
    assert by_id["work:evt-utc"]["start_at"] == "2099-07-08T14:00:00Z"
    assert by_id["work:evt-allday"]["start_at"] == "2099-07-09"


def test_parse_ics_declined_filter_off_when_no_me():
    """With no me_emails, a declined event is NOT dropped (nothing to match)."""
    events = parse_ics(SAMPLE, namespace="work", me_emails=())
    assert "work:evt-declined" in {e["external_id"] for e in events}


def test_parse_ics_unfolds_continuation_lines():
    ics = (
        "BEGIN:VEVENT\r\nUID:u1\r\nSUMMARY:A very long titl\r\n e that folds\r\n"
        "DTSTART:20990101T090000Z\r\nEND:VEVENT\r\n"
    )
    events = parse_ics(ics, namespace="p")
    assert events[0]["title"] == "A very long title that folds"


def test_parse_ics_reads_recurrence_id_and_url():
    """RECURRENCE-ID (a moved instance) and URL lines are parsed onto the event."""
    ics = (
        "BEGIN:VEVENT\r\nUID:evt-moved\r\nSUMMARY:Moved instance\r\n"
        "DTSTART;TZID=America/New_York:20990707T100000\r\n"
        "RECURRENCE-ID;TZID=America/New_York:20990707T090000\r\n"
        "URL:https://example.com/event/evt-moved\r\nEND:VEVENT\r\n"
    )
    (e,) = parse_ics(ics, namespace="work")
    assert e["recurrence_id"] == "2099-07-07T09:00:00"
    assert e["url"] == "https://example.com/event/evt-moved"


# -- registry (URL sealed) ---------------------------------------------------


def test_ics_source_round_trip_and_sealed(store, secret_env):
    put_ics_source(
        store,
        account="personal",
        url="https://calendar.google.com/secret/abc/basic.ics",
        me_emails=("me@example.com",),
        label="Personal",
    )
    srcs = ics_sources(store)
    assert len(srcs) == 1
    assert srcs[0].url == "https://calendar.google.com/secret/abc/basic.ics"
    assert srcs[0].namespace == "personal"
    assert srcs[0].me_emails == ("me@example.com",)
    # The URL is sealed at rest, not stored plaintext.
    raw = store.get_source(ICS, "personal")["secret_enc"]
    assert raw is not None and b"basic.ics" not in bytes(raw)


# -- CLI ---------------------------------------------------------------------


def test_calendar_add_list_remove(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", generate_key())
    get_settings.cache_clear()
    db = str(tmp_path / "p.db")
    main(["init-db", "--db-path", db])
    main(["user", "--db-path", db, "add", "tester", "--operator"])

    assert main([
        "calendar", "--db-path", db, "add-source",
        "--account", "personal", "--url", "https://host/secret.ics",
        "--me", "me@example.com", "--label", "Personal",
    ]) == 0
    capsys.readouterr()
    assert main(["calendar", "--db-path", db, "list-sources"]) == 0
    out = capsys.readouterr().out
    assert "personal" in out and "me@example.com" in out
    assert "secret.ics" not in out  # URL is a bearer secret, never printed

    assert main(["calendar", "--db-path", db, "remove-source", "--account", "personal"]) == 0
    get_settings.cache_clear()


def test_calendar_add_requires_secret_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PREFRONTAL_SECRET_KEY", raising=False)
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY_FILE", "")
    get_settings.cache_clear()
    db = str(tmp_path / "p.db")
    main(["init-db", "--db-path", db])
    main(["user", "--db-path", db, "add", "tester", "--operator"])
    rc = main([
        "calendar", "--db-path", db, "add-source",
        "--account", "personal", "--url", "https://host/secret.ics",
    ])
    get_settings.cache_clear()
    assert rc == 1
    assert "secrets init" in capsys.readouterr().err


def test_calendar_sync_all_users_isolated(tmp_path, monkeypatch, capsys):
    """`calendar sync --all-users` lands each user's own feed in their own scope."""
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", generate_key())
    get_settings.cache_clear()
    # Keep the sync deterministic + offline regardless of a live local Ollama.
    monkeypatch.setattr(
        "prefrontal.integrations.ollama.OllamaClient.available", lambda self: False
    )
    db = str(tmp_path / "p.db")
    main(["init-db", "--db-path", db])
    main(["user", "--db-path", db, "add", "alice", "--operator"])
    main(["user", "--db-path", db, "add", "bob"])
    main([
        "calendar", "--db-path", db, "--user", "alice", "add-source",
        "--account", "personal", "--url", "https://host/alice.ics",
    ])
    main([
        "calendar", "--db-path", db, "--user", "bob", "add-source",
        "--account", "work", "--url", "https://host/bob.ics",
    ])

    def fake_fetch(url, **kwargs):
        who = "alice" if "alice" in url else "bob"
        return (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            f"UID:{who}-1\nSUMMARY:{who} event\n"
            "DTSTART:20990301T100000Z\nEND:VEVENT\nEND:VCALENDAR"
        )

    monkeypatch.setattr("prefrontal.ics.fetch_ics", fake_fetch)
    capsys.readouterr()
    assert main(["calendar", "--db-path", db, "sync", "--all-users"]) == 0

    with MemoryStore.open(db, initialize=False) as unscoped:
        alice = unscoped.scoped(unscoped.get_user("alice")["id"])
        bob = unscoped.scoped(unscoped.get_user("bob")["id"])
        a_titles = [c["title"] for c in alice.upcoming_commitments()]
        b_titles = [c["title"] for c in bob.upcoming_commitments()]
        assert "alice event" in a_titles
        assert "bob event" in b_titles
        assert "bob event" not in a_titles  # isolation
    get_settings.cache_clear()


def test_calendar_sync_reports_no_feeds(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "prefrontal.integrations.ollama.OllamaClient.available", lambda self: False
    )
    get_settings.cache_clear()
    db = str(tmp_path / "p.db")
    main(["init-db", "--db-path", db])
    main(["user", "--db-path", db, "add", "tester", "--operator"])
    assert main(["calendar", "--db-path", db, "sync"]) == 0
    assert "no calendar sources" in capsys.readouterr().err
    get_settings.cache_clear()
