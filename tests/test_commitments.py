"""Tests for the commitments ingestion layer (calendar sync).

Covers UTC normalization, event validation, store upsert/prune semantics
(including feed-aware namespacing), the sync orchestration, and the endpoints.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from prefrontal.commitments import (
    RECUR_OCCURRENCE_SEP,
    expand_recurrences,
    find_conflicts,
    is_placeholder_title,
    normalize_event,
    sync_calendar,
    to_utc,
)
from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import (
    MemoryStore,
    commitment_url,
    feed_label,
    feed_slug,
)
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "cal-secret"


def _iso(delta_minutes: float) -> str:
    """An offset-aware ISO timestamp `delta_minutes` from now (UTC)."""
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).isoformat()


# -- normalization -----------------------------------------------------------


def test_to_utc_handles_offset_z_naive_and_date():
    """Offsets are converted to UTC; Z, naive, and date-only all parse."""
    assert to_utc("2026-06-28T10:30:00-07:00") == "2026-06-28 17:30:00"
    assert to_utc("2026-06-28T17:30:00Z") == "2026-06-28 17:30:00"
    assert to_utc("2026-06-28T17:30:00") == "2026-06-28 17:30:00"  # naive → UTC default
    assert to_utc("2026-06-28") == "2026-06-28 00:00:00"


def test_to_utc_naive_uses_tzid_iana():
    """A naive time with an IANA TZID is interpreted in that zone (the ICS case)."""
    # 10:30 in New York on Jun 28 is EDT (UTC-4) → 14:30 UTC.
    assert to_utc("2026-06-28T10:30:00", tzid="America/New_York") == "2026-06-28 14:30:00"


def test_to_utc_naive_uses_windows_tzid():
    """A Windows zone name (Outlook feeds) resolves via the Windows→IANA map."""
    # 09:00 'Pacific Standard Time' on Jun 28 is PDT (UTC-7) → 16:00 UTC.
    assert to_utc("2026-06-28T09:00:00", tzid="Pacific Standard Time") == "2026-06-28 16:00:00"


def test_to_utc_respects_dst():
    """The same wall-clock time maps to different UTC across DST boundaries."""
    # Eastern: summer is UTC-4, winter UTC-5.
    assert to_utc("2026-07-01T12:00:00", tzid="America/New_York") == "2026-07-01 16:00:00"
    assert to_utc("2026-01-01T12:00:00", tzid="America/New_York") == "2026-01-01 17:00:00"


def test_to_utc_offset_aware_ignores_tzid():
    """An explicit offset wins; a (contradictory) tzid is ignored."""
    assert (
        to_utc("2026-06-28T10:30:00-07:00", tzid="America/New_York")
        == "2026-06-28 17:30:00"
    )


def test_to_utc_naive_falls_back_to_default_tz():
    """A naive time with no (or unresolvable) tzid uses the home timezone."""
    assert to_utc("2026-06-28T08:00:00", default_tz="America/Los_Angeles") == "2026-06-28 15:00:00"
    # An unknown tzid degrades to default_tz rather than silently assuming UTC.
    assert (
        to_utc("2026-06-28T08:00:00", tzid="Narnia/Cair_Paravel",
               default_tz="America/Los_Angeles")
        == "2026-06-28 15:00:00"
    )


def test_to_utc_date_only_not_shifted_by_default_tz():
    """All-day (date-only) events stay floating midnight, never shifted a day."""
    assert to_utc("2026-06-28", default_tz="America/New_York") == "2026-06-28 00:00:00"


def test_normalize_event_threads_tzid_and_default_tz():
    """normalize_event applies tzid/end_tzid, and default_tz for unzoned times."""
    out = normalize_event(
        {
            "title": "Standup",
            "start_at": "2026-06-28T09:30:00",
            "end_at": "2026-06-28T10:00:00",
            "tzid": "America/New_York",
        },
    )
    assert out["start_at"] == "2026-06-28 13:30:00"
    assert out["end_at"] == "2026-06-28 14:00:00"  # end_tzid defaults to tzid
    # No tzid → default_tz governs.
    out2 = normalize_event(
        {"title": "Call", "start_at": "2026-06-28T09:00:00"},
        default_tz="America/Chicago",
    )
    assert out2["start_at"] == "2026-06-28 14:00:00"  # CDT (UTC-5)


def test_sync_converts_tzid_to_utc(store):
    """A full sync stores TZID events in UTC, so the schedule reads correctly."""
    sync_calendar(
        store,
        [{"title": "1:1", "start_at": "2027-06-28T15:00:00",
          "tzid": "America/New_York", "external_id": "work:z"}],
        default_tz="America/New_York",
    )
    (got,) = store.upcoming_commitments()
    assert got["start_at"] == "2027-06-28 19:00:00"  # 15:00 EDT → 19:00 UTC


# -- recurrence expansion ----------------------------------------------------

# A weekly Wednesday 07:30 America/New_York master, dated long before the window
# — exactly the shape (Tom Workout) that never surfaced before expansion.
_WEEKLY_WED = {
    "title": "Tom Workout",
    "start_at": "2025-09-10T07:30:00",
    "end_at": "2025-09-10T08:30:00",
    "tzid": "America/New_York",
    "external_id": "personal:wk@google.com",
    "rrule": "FREQ=WEEKLY;BYDAY=WE",
}


def _wed_noon(month: int) -> datetime:
    """A Wednesday-noon UTC reference instant in the given 2026 month."""
    return datetime(2026, month, {7: 1, 1: 7}[month], 12, tzinfo=timezone.utc)


def test_expand_weekly_master_yields_todays_occurrence():
    """A long-past weekly master produces the current week's occurrence, with a
    stable per-occurrence external_id and the master's duration preserved."""
    out = expand_recurrences([_WEEKLY_WED], now=_wed_noon(7), default_tz="America/New_York")
    assert len(out) == 1  # only this Wednesday falls in [now-1h, now+36h]
    occ = normalize_event(out[0], default_tz="America/New_York")
    assert occ["start_at"] == "2026-07-01 11:30:00"  # 07:30 EDT → 11:30 UTC
    assert occ["end_at"] == "2026-07-01 12:30:00"  # 1h duration carried over
    assert out[0]["external_id"] == (
        "personal:wk@google.com" + RECUR_OCCURRENCE_SEP + "20260701T073000"
    )


def test_expand_keeps_wall_clock_across_dst():
    """The same 07:30 local event lands at a different UTC in winter (EST)."""
    out = expand_recurrences([_WEEKLY_WED], now=_wed_noon(1), default_tz="America/New_York")
    occ = normalize_event(out[0], default_tz="America/New_York")
    assert occ["start_at"] == "2026-01-07 12:30:00"  # 07:30 EST → 12:30 UTC


# A future Wednesday, so occurrences land ahead of the real clock and survive
# upcoming_commitments()'s `start_at >= now` filter regardless of test run date.
_FUTURE_WED = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)


def test_expand_stable_id_upserts_across_polls(store):
    """Re-syncing regenerates the identical occurrence id, so it updates in place
    rather than piling up duplicate rows."""
    for _ in range(2):
        sync_calendar(store, [dict(_WEEKLY_WED)], default_tz="America/New_York", now=_FUTURE_WED)
    rows = [c for c in store.upcoming_commitments() if c["title"] == "Tom Workout"]
    assert len(rows) == 1


def test_expand_honors_exdate():
    """An EXDATE for the current occurrence cancels it (skipped weeks stay skipped)."""
    master = dict(_WEEKLY_WED, exdate=["20260701T073000"])
    out = expand_recurrences([master], now=_wed_noon(7), default_tz="America/New_York")
    assert out == []


def test_expand_suppresses_modified_occurrence():
    """A RECURRENCE-ID instance replaces the generated occurrence for that slot,
    so a rescheduled week isn't double-booked."""
    moved = {
        "title": "Tom Workout",
        "start_at": "2026-07-01T09:00:00",  # bumped from 07:30
        "tzid": "America/New_York",
        "external_id": "personal:wk@google.com",  # same series UID
        "recurrence_id": "2026-07-01T07:30:00",  # original slot it overrides
    }
    out = expand_recurrences([_WEEKLY_WED, moved], now=_wed_noon(7), default_tz="America/New_York")
    starts = sorted(normalize_event(e, default_tz="America/New_York")["start_at"] for e in out)
    assert starts == ["2026-07-01 13:00:00"]  # only the moved 09:00 EDT instance


def test_expand_passes_through_non_recurring():
    """Events without an RRULE are returned untouched (no id rewrite)."""
    plain = {"title": "One-off", "start_at": "2026-07-01T15:00:00Z", "external_id": "work:x"}
    assert expand_recurrences([plain], now=_wed_noon(7)) == [plain]


def test_expand_skips_malformed_rrule_without_failing_batch():
    """A garbage RRULE drops just that event, never rejecting the whole sync."""
    bad = dict(_WEEKLY_WED, rrule="FREQ=NONSENSE;BYDAY=??")
    assert expand_recurrences([bad], now=_wed_noon(7), default_tz="America/New_York") == []


def test_sync_expands_recurring_end_to_end(store):
    """A recurring master synced through the full path lands as a commitment."""
    summary = sync_calendar(
        store, [dict(_WEEKLY_WED)], default_tz="America/New_York", now=_FUTURE_WED
    )
    assert summary.added == 1
    (got,) = [c for c in store.upcoming_commitments() if c["title"] == "Tom Workout"]
    assert got["start_at"] == "2026-08-05 11:30:00"  # 07:30 EDT → 11:30 UTC


def test_normalize_event_requires_title_and_start():
    """Missing required fields raise ValueError."""
    with pytest.raises(ValueError):
        normalize_event({"start_at": _iso(60)})
    with pytest.raises(ValueError):
        normalize_event({"title": "x"})


def test_normalize_event_defaults():
    """Defaults: lead 10 min, soft, calendar source, no source_url."""
    out = normalize_event({"title": "Dentist", "start_at": "2026-06-28T10:00:00Z"})
    assert out["lead_minutes"] == 10.0
    assert out["hardness"] == "soft"
    assert out["source"] == "calendar"
    assert out["source_url"] is None


def test_normalize_event_accepts_url_aliases():
    """A source link arrives as ``url``, ``source_url``, or ``html_link``."""
    for key in ("url", "source_url", "html_link"):
        out = normalize_event(
            {"title": "x", "start_at": "2026-06-28T10:00:00Z", key: "https://e/1"}
        )
        assert out["source_url"] == "https://e/1"


# -- deeplinks ---------------------------------------------------------------


def test_commitment_url_prefers_explicit_source_url():
    """An explicit http(s) source_url is used verbatim, for any provider."""
    c = {"external_id": "outlook:ABC", "title": "Block", "source_url": "https://x/e"}
    assert commitment_url(c) == "https://x/e"


def test_commitment_url_rejects_non_http_source_url():
    """A non-http(s) source_url is dropped (no javascript: into an href)."""
    c = {"title": "x", "source_url": "javascript:alert(1)"}
    assert commitment_url(c) is None


def test_commitment_url_derives_google_search_link():
    """A Google event (UID ends @google.com) gets a title-search deeplink."""
    c = {"external_id": "work:abc_R20260629T190000@google.com", "title": "Casey 1:1"}
    url = commitment_url(c)
    assert url == "https://calendar.google.com/calendar/u/0/r/search?q=Casey+1%3A1"


def test_commitment_url_none_for_unlinkable_providers():
    """Outlook/iCloud UIDs aren't linkable without an explicit source_url."""
    assert commitment_url({"external_id": "outlook:DEADBEEF", "title": "Block"}) is None
    assert commitment_url({"external_id": "personal:UUID-1", "title": "Brow"}) is None
    assert commitment_url({"external_id": None, "title": "Manual"}) is None


# -- store -------------------------------------------------------------------


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


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


def test_source_url_round_trips_and_surfaces_as_url(store):
    """An explicit source_url persists across upsert and surfaces as ``url``."""
    cid, _ = store.upsert_commitment(
        title="Demo",
        start_at=to_utc(_iso(60)),
        external_id="work:e1@google.com",
        source_url="https://example.com/event/1",
    )
    got = store.get_commitment(cid)
    assert got["source_url"] == "https://example.com/event/1"
    assert got["url"] == "https://example.com/event/1"  # explicit wins over derived


def test_upcoming_commitment_url_falls_back_to_derived(store):
    """Without a source_url, a Google commitment exposes a derived search link."""
    store.upsert_commitment(
        title="Casey 1:1", start_at=to_utc(_iso(60)), external_id="work:e2@google.com"
    )
    (got,) = store.upcoming_commitments()
    assert got["source_url"] is None
    assert got["url"] == "https://calendar.google.com/calendar/u/0/r/search?q=Casey+1%3A1"


def test_feed_label_maps_prefix_to_display_name():
    """The external_id prefix becomes a human calendar label; manual → None."""
    assert feed_label("personal:abc") == "Personal"
    assert feed_label("work:xyz") == "Work"
    assert feed_label("outlook:1") == "Outlook"
    assert feed_label("family:1") == "Family"
    assert feed_label("icloud:1") == "Icloud"  # unknown feed: title-cased slug
    assert feed_label(None) is None
    assert feed_label("no-prefix-uid") is None


def test_feed_slug_returns_raw_namespace():
    """The raw external_id prefix, unmapped (the pill lookup key); manual → None."""
    assert feed_slug("personal:abc") == "personal"
    assert feed_slug("work:xyz") == "work"
    assert feed_slug("outlook:1") == "outlook"
    assert feed_slug("icloud:1") == "icloud"  # not title-cased, unlike feed_label
    assert feed_slug(None) is None
    assert feed_slug("no-prefix-uid") is None


def test_commitment_reads_expose_calendar_label(store):
    """Store reads annotate each commitment with its source calendar + feed key."""
    cid, _ = store.upsert_commitment(
        title="Swim", start_at=to_utc(_iso(60)), external_id="family:s1"
    )
    manual, _ = store.upsert_commitment(title="Dentist", start_at=to_utc(_iso(120)))
    assert store.get_commitment(cid)["calendar"] == "Family"
    assert store.get_commitment(cid)["calendar_key"] == "family"
    assert store.get_commitment(manual)["calendar"] is None
    assert store.get_commitment(manual)["calendar_key"] is None
    by_title = {c["title"]: c for c in store.upcoming_commitments()}
    assert by_title["Swim"]["calendar"] == "Family"
    assert by_title["Swim"]["calendar_key"] == "family"
    assert by_title["Dentist"]["calendar_key"] is None


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


def test_sync_classifies_new_events_and_keeps_verdict(store):
    """New events are classified once; existing ones aren't re-classified."""
    calls: list[str] = []

    def classify(title: str) -> tuple[str, str]:
        calls.append(title)
        return ("fyi", "llm") if "brow" in title.lower() else ("self", "llm")

    events = [
        {"title": "Harlequin Brow Appt", "start_at": _iso(60), "external_id": "family:1"},
        {"title": "Standup", "start_at": _iso(90), "external_id": "work:1"},
    ]
    sync_calendar(store, events, classify=classify)
    by_title = {c["title"]: c for c in store.upcoming_commitments()}
    assert by_title["Harlequin Brow Appt"]["kind"] == "fyi"
    assert by_title["Standup"]["kind"] == "self"
    assert len(calls) == 2

    # Re-syncing the same events must not consult the classifier again.
    calls.clear()
    sync_calendar(store, events, classify=classify)
    assert calls == []
    assert {c["title"]: c["kind"] for c in store.upcoming_commitments()}[
        "Harlequin Brow Appt"
    ] == "fyi"


def test_sync_preserves_user_kind_correction(store):
    """A user's manual kind override is never clobbered by a later sync."""
    events = [
        {"title": "Brow appt", "start_at": _iso(60), "external_id": "family:1"},
    ]
    sync_calendar(store, events, classify=lambda t: ("fyi", "llm"))
    cid = store.upcoming_commitments()[0]["id"]
    store.set_commitment_kind(cid, "self", "user")  # the user disagrees
    # A fresh sync with a classifier that would say 'fyi' must not override.
    sync_calendar(store, events, classify=lambda t: ("fyi", "llm"))
    assert store.get_commitment(cid)["kind"] == "self"


def test_find_conflicts_ignores_fyi(store):
    """An FYI commitment overlapping a real one is not a double-booking."""
    base = "2026-06-28 10:00:00"
    items = [
        {"id": 1, "title": "Work mtg", "start_at": base, "end_at": "2026-06-28 11:00:00",
         "kind": "self"},
        {"id": 2, "title": "Jamie brow", "start_at": "2026-06-28 10:30:00",
         "end_at": "2026-06-28 11:30:00", "kind": "fyi"},
    ]
    assert find_conflicts(items) == []
    # Same pair, both 'self' → it is a conflict.
    items[1]["kind"] = "self"
    assert len(find_conflicts(items)) == 1


def test_kind_feedback_latest_wins(store):
    """record_kind_feedback collapses by normalized title; latest verdict wins."""
    store.record_kind_feedback("Harlequin Brow Appt", "fyi", llm_kind="self")
    store.record_kind_feedback("harlequin brow appt", "self", llm_kind="fyi")
    examples = store.kind_feedback_examples()
    assert len(examples) == 1
    assert examples[0]["kind"] == "self"
    assert examples[0]["display"] == "harlequin brow appt"


def test_set_commitment_kind_endpoint_records_feedback(client):
    """POST /commitments/{id}/kind updates the row and learns from it."""
    client.post(
        "/webhooks/calendar/sync",
        headers=_auth(),
        json={"events": [
            {"title": "Harlequin Brow Appt", "start_at": _iso(60),
             "external_id": "family:9"},
        ]},
    )
    cid = client.get("/commitments", headers=_auth()).json()["commitments"][0]["id"]
    r = client.post(f"/commitments/{cid}/kind", headers=_auth(), json={"kind": "fyi"})
    assert r.status_code == 200
    assert r.json()["commitment"]["kind"] == "fyi"
    assert r.json()["commitment"]["kind_source"] == "user"
    # Bad kind → 422; unknown id → 404.
    assert client.post(f"/commitments/{cid}/kind", headers=_auth(),
                       json={"kind": "nope"}).status_code == 422
    assert client.post("/commitments/99999/kind", headers=_auth(),
                       json={"kind": "fyi"}).status_code == 404


def test_commitments_expose_calendar_key_and_label_map(store_open):
    """/commitments tags each event with its feed key and echoes the label map."""
    settings = Settings(
        webhook_secret=SECRET,
        calendar_labels=(
            ("personal", "Personal", "blue"),
            ("work", "Vistar", "orange"),
            ("outlook", "T-Mobile", "magenta"),
        ),
    )
    app = create_app(store=store_open, settings=settings)
    with TestClient(app) as c:
        c.post(
            "/webhooks/calendar/sync",
            headers=_auth(),
            json={"events": [
                {"title": "Standup", "start_at": _iso(60), "external_id": "work:s1"},
            ]},
        )
        data = c.get("/commitments", headers=_auth()).json()
    assert data["calendars"] == {
        "personal": {"label": "Personal", "color": "blue"},
        "work": {"label": "Vistar", "color": "orange"},
        "outlook": {"label": "T-Mobile", "color": "magenta"},
    }
    ev = data["commitments"][0]
    assert ev["calendar_key"] == "work"
    assert ev["calendar"] == "Work"  # default label; the pill relabels via the map


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
        yield scoped_default(MemoryStore(conn))
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
