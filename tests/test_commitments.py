"""Tests for the commitments ingestion layer (calendar sync).

Covers UTC normalization, event validation, store upsert/prune semantics
(including feed-aware namespacing), the sync orchestration, and the endpoints.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from prefrontal.commitments import (
    find_conflicts,
    is_placeholder_title,
    normalize_event,
    sync_calendar,
    to_utc,
)
from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app

SECRET = "cal-secret"


def _iso(delta_minutes: float) -> str:
    """An offset-aware ISO timestamp `delta_minutes` from now (UTC)."""
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).isoformat()


# -- normalization -----------------------------------------------------------


def test_to_utc_handles_offset_z_naive_and_date():
    """Offsets are converted to UTC; Z, naive, and date-only all parse."""
    assert to_utc("2026-06-28T10:30:00-07:00") == "2026-06-28 17:30:00"
    assert to_utc("2026-06-28T17:30:00Z") == "2026-06-28 17:30:00"
    assert to_utc("2026-06-28T17:30:00") == "2026-06-28 17:30:00"
    assert to_utc("2026-06-28") == "2026-06-28 00:00:00"


def test_normalize_event_requires_title_and_start():
    """Missing required fields raise ValueError."""
    with pytest.raises(ValueError):
        normalize_event({"start_at": _iso(60)})
    with pytest.raises(ValueError):
        normalize_event({"title": "x"})


def test_normalize_event_defaults():
    """Defaults: lead 10 min, soft, calendar source."""
    out = normalize_event({"title": "Dentist", "start_at": "2026-06-28T10:00:00Z"})
    assert out["lead_minutes"] == 10.0
    assert out["hardness"] == "soft"
    assert out["source"] == "calendar"


# -- store -------------------------------------------------------------------


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield s


def test_upsert_inserts_then_updates_by_external_id(store):
    """A repeated external_id updates in place and reports created=False."""
    cid1, created1 = store.upsert_commitment(
        title="Standup", start_at=to_utc(_iso(60)), external_id="personal:abc"
    )
    cid2, created2 = store.upsert_commitment(
        title="Standup (moved)", start_at=to_utc(_iso(90)), external_id="personal:abc"
    )
    assert created1 is True and created2 is False
    assert cid1 == cid2
    assert store.get_commitment(cid1)["title"] == "Standup (moved)"


def test_upcoming_excludes_past(store):
    """Past commitments don't show in the upcoming list."""
    store.upsert_commitment(title="Past", start_at=to_utc(_iso(-60)))
    store.upsert_commitment(title="Soon", start_at=to_utc(_iso(30)))
    titles = [c["title"] for c in store.upcoming_commitments()]
    assert titles == ["Soon"]


# -- sync --------------------------------------------------------------------


def test_sync_counts_and_prunes(store):
    """Re-syncing upserts, and a vanished event is cancelled."""
    first = sync_calendar(
        store,
        [
            {"title": "A", "start_at": _iso(60), "external_id": "personal:a"},
            {"title": "B", "start_at": _iso(120), "external_id": "personal:b"},
        ],
    )
    assert (first.added, first.updated, first.cancelled) == (2, 0, 0)

    # Second sync drops B, updates A.
    second = sync_calendar(
        store, [{"title": "A2", "start_at": _iso(60), "external_id": "personal:a"}]
    )
    assert second.updated == 1
    assert second.cancelled == 1  # B pruned
    assert [c["title"] for c in store.upcoming_commitments()] == ["A2"]


def test_sync_is_feed_aware(store):
    """Syncing one feed must not cancel another feed's commitments."""
    sync_calendar(store, [{"title": "Work mtg", "start_at": _iso(60), "external_id": "work:1"}])
    # Now sync only the personal feed — work:1 must survive.
    sync_calendar(
        store, [{"title": "Coffee", "start_at": _iso(30), "external_id": "personal:1"}]
    )
    titles = {c["title"] for c in store.upcoming_commitments()}
    assert titles == {"Work mtg", "Coffee"}


def test_find_conflicts_detects_overlap():
    """Overlapping intervals are flagged; back-to-back ones are not."""
    base = "2026-06-28 10:00:00"
    overlapping = [
        {"id": 1, "title": "Personal call", "start_at": base,
         "end_at": "2026-06-28 10:45:00"},
        {"id": 2, "title": "Work mtg", "start_at": "2026-06-28 10:30:00",
         "end_at": "2026-06-28 11:00:00"},
    ]
    conflicts = find_conflicts(overlapping)
    assert len(conflicts) == 1
    assert conflicts[0].overlap_minutes == 15.0

    back_to_back = [
        {"id": 1, "title": "A", "start_at": base, "end_at": "2026-06-28 10:30:00"},
        {"id": 2, "title": "B", "start_at": "2026-06-28 10:30:00", "end_at": "2026-06-28 11:00:00"},
    ]
    assert find_conflicts(back_to_back) == []


def test_find_conflicts_assumes_default_duration():
    """Commitments without end_at get the default duration for overlap checks."""
    items = [
        {"id": 1, "title": "A", "start_at": "2026-06-28 10:00:00", "end_at": None},
        {"id": 2, "title": "B", "start_at": "2026-06-28 10:10:00", "end_at": None},
    ]
    # 30-min default → 10:00–10:30 overlaps 10:10–10:40 by 20 min.
    conflicts = find_conflicts(items)
    assert len(conflicts) == 1
    assert conflicts[0].overlap_minutes == 20.0


def test_sync_reports_conflicts(store):
    """The sync summary counts double-bookings in the resulting schedule."""
    summary = sync_calendar(
        store,
        [
            {"title": "Personal", "start_at": _iso(60), "end_at": _iso(90),
             "external_id": "personal:x"},
            {"title": "Work", "start_at": _iso(75), "end_at": _iso(105),
             "external_id": "work:y"},
        ],
    )
    assert summary.conflicts == 1


def test_sync_new_conflict_only_fires_on_change(store):
    """new_conflict flags a *changed* conflict set, not every poll with a conflict."""
    clash = [
        {"title": "Personal", "start_at": _iso(60), "end_at": _iso(90),
         "external_id": "personal:x"},
        {"title": "Work", "start_at": _iso(75), "end_at": _iso(105),
         "external_id": "work:y"},
    ]
    first = sync_calendar(store, clash)
    assert (first.conflicts, first.new_conflict) == (1, True)

    # Same conflict next poll → no re-alert.
    second = sync_calendar(store, clash)
    assert (second.conflicts, second.new_conflict) == (1, False)

    # Resolve it (move Work clear of Personal) → no conflict, no alert.
    resolved = sync_calendar(store, [
        {"title": "Personal", "start_at": _iso(60), "end_at": _iso(90),
         "external_id": "personal:x"},
        {"title": "Work", "start_at": _iso(200), "end_at": _iso(230),
         "external_id": "work:y"},
    ])
    assert (resolved.conflicts, resolved.new_conflict) == (0, False)

    # Reintroduce the same clash → alert again.
    again = sync_calendar(store, clash)
    assert (again.conflicts, again.new_conflict) == (1, True)


def test_sync_rejects_bad_batch_atomically(store):
    """A bad timestamp rejects the whole batch before any write."""
    with pytest.raises(ValueError):
        sync_calendar(
            store,
            [
                {"title": "Good", "start_at": _iso(60), "external_id": "personal:g"},
                {"title": "Bad", "start_at": "not-a-date", "external_id": "personal:x"},
            ],
        )
    assert store.upcoming_commitments() == []  # nothing partially applied


# -- endpoints ---------------------------------------------------------------


@pytest.fixture()
def store_open():
    conn = init_db(":memory:")
    try:
        yield MemoryStore(conn)
    finally:
        conn.close()


@pytest.fixture()
def client(store_open):
    app = create_app(store=store_open, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_calendar_sync_endpoint(client, store_open):
    """The sync endpoint upserts events and reports a summary."""
    resp = client.post(
        "/webhooks/calendar/sync",
        json={"events": [
            {"title": "Dentist", "start_at": _iso(120), "external_id": "personal:d", "hard": True},
        ]},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["added"] == 1
    assert store_open.upcoming_commitments()[0]["hardness"] == "hard"


def test_calendar_sync_bad_timestamp_is_422(client):
    """An unparseable timestamp returns 422."""
    resp = client.post(
        "/webhooks/calendar/sync",
        json={"events": [{"title": "X", "start_at": "nope"}]},
        headers=_auth(),
    )
    assert resp.status_code == 422


def test_manual_commitment_and_list(client, store_open):
    """A manual commitment is created and appears in the list."""
    created = client.post(
        "/commitments",
        json={"title": "Call mom", "start_at": _iso(45)},
        headers=_auth(),
    )
    assert created.status_code == 201
    listed = client.get("/commitments", headers=_auth()).json()["commitments"]
    assert listed[0]["title"] == "Call mom"
    assert listed[0]["source"] == "manual"


def test_conflicts_endpoint(client, store_open):
    """The conflicts endpoint returns overlapping pairs across feeds."""
    client.post(
        "/webhooks/calendar/sync",
        json={"events": [
            {"title": "Personal", "start_at": _iso(60), "end_at": _iso(90),
             "external_id": "personal:x"},
            {"title": "Work", "start_at": _iso(75), "end_at": _iso(105),
             "external_id": "work:y"},
        ]},
        headers=_auth(),
    )
    conflicts = client.get("/commitments/conflicts", headers=_auth()).json()["conflicts"]
    assert len(conflicts) == 1
    titles = {conflicts[0]["a"]["title"], conflicts[0]["b"]["title"]}
    assert titles == {"Personal", "Work"}
    assert conflicts[0]["overlap_minutes"] > 0


def test_commitment_endpoints_require_auth(client):
    """All schedule endpoints are token-guarded."""
    assert client.post("/webhooks/calendar/sync", json={"events": []}).status_code == 401
    assert client.get("/commitments").status_code == 401
    manual = client.post("/commitments", json={"title": "x", "start_at": "2026-01-01"})
    assert manual.status_code == 401


# -- possible conflicts (placeholder overlaps) -------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Busy", True), ("(busy)", True), ("Block", True), ("BLOCK", True),
        ("Hold", True), ("Focus time", True), ("OOO", True), ("Private", True),
        ("Dentist", False), ("Block party planning", False), ("1:1 with Casey", False),
    ],
)
def test_is_placeholder_title(title, expected):
    """Generic holds are placeholders; specific titles (even containing a word) aren't."""
    assert is_placeholder_title(title) is expected


def _clash():
    """A real event overlapping a placeholder, on different feeds."""
    return [
        {"title": "Dentist", "start_at": _iso(60), "end_at": _iso(120),
         "external_id": "personal:d"},
        {"title": "Busy", "start_at": _iso(75), "end_at": _iso(135),
         "external_id": "work:b"},
    ]


def test_sync_splits_hard_and_possible(store):
    """A placeholder overlapping a real event is a *possible* conflict, not hard."""
    first = sync_calendar(store, _clash())
    assert first.conflicts == 0
    assert (first.possible_conflicts, first.new_possible_conflict) == (1, True)
    # Unchanged on the next poll → not "new" (no re-alert).
    second = sync_calendar(store, _clash())
    assert (second.possible_conflicts, second.new_possible_conflict) == (1, False)


def test_possible_conflict_dismiss_endpoint(client):
    """The possible-conflict carries a key; dismissing it removes it and sticks."""
    client.post("/webhooks/calendar/sync", headers=_auth(), json={"events": _clash()})
    conf = client.get("/commitments/conflicts", headers=_auth()).json()
    assert conf["conflicts"] == []
    assert len(conf["possible_conflicts"]) == 1
    key = conf["possible_conflicts"][0]["key"]

    client.post("/commitments/conflicts/dismiss", headers=_auth(), json={"key": key})
    after = client.get("/commitments/conflicts", headers=_auth()).json()
    assert after["possible_conflicts"] == []
    # A re-sync of the same clash stays dismissed (and doesn't re-alert).
    resync = client.post(
        "/webhooks/calendar/sync", headers=_auth(), json={"events": _clash()}
    ).json()
    assert resync["new_possible_conflict"] is False
