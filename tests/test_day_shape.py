"""Tests for the visual day-shape — today laid out as a timeline of blocks.

Covers the structured build (commitments as fixed blocks, todos fitted into the
forward gaps, free time in between), the forward-only suggestion rule, the
past/now/upcoming state + "now" marker, the CHI-2024-driven redundant non-colour
encoding (glyph + pattern + kind on every block), local-zone rendering, the
conflict / FYI / avoided flags, the monochrome text render, and the ``GET /day``
endpoint + ``/day/board`` page.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.day_shape import build_day_shape, day_shape_payload, render_day_shape
from prefrontal.impact import utcnow
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "day-secret"
UTC = Settings(webhook_secret=SECRET, timezone="UTC")


def _at(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


@pytest.fixture()
def noon():
    """A fixed midday 'now' so the band and windowing never depend on the clock."""
    return utcnow().replace(hour=12, minute=0, second=0, microsecond=0)


# --- The shape ---------------------------------------------------------------


def test_empty_day_has_a_single_free_band(store, noon):
    """No commitments, no todos → the waking band is one quiet, open stretch."""
    shape = build_day_shape(store, now=noon, settings=UTC)
    assert shape.date == noon.strftime("%Y-%m-%d")
    # The whole band is free time; nothing is committed and nothing is fitted.
    assert shape.committed_minutes == 0.0
    assert shape.fitted_count == 0
    assert [s.kind for s in shape.segments] == ["free", "free"] or all(
        s.kind == "free" for s in shape.segments
    )
    assert all(s.pattern == "dotted" and s.glyph for s in shape.segments)


def test_commitment_becomes_a_fixed_block(store, noon):
    """A commitment lands as a solid, anchored block carrying its own fields."""
    store.upsert_commitment(
        title="Dentist", start_at=_at(noon + timedelta(hours=1)),
        end_at=_at(noon + timedelta(hours=1, minutes=45)),
        external_id="p:1", hardness="hard", location="Downtown",
    )
    shape = build_day_shape(store, now=noon, settings=UTC)
    block = next(s for s in shape.segments if s.kind == "commitment")
    assert block.title == "Dentist"
    assert block.pattern == "solid" and block.glyph == "📌"
    assert block.hard is True
    assert block.minutes == 45.0
    assert block.state == "upcoming"
    assert "Downtown" in (block.detail or "")
    # The 45-minute meeting is counted as committed time.
    assert shape.committed_minutes == 45.0


def test_todo_is_fitted_into_a_forward_gap(store, noon):
    """An open todo with an estimate is slotted into a gap ahead of now."""
    store.add_todo("Call the plumber", estimate_minutes=20, priority=2)
    shape = build_day_shape(store, now=noon, settings=UTC)
    todo = next(s for s in shape.segments if s.kind == "todo")
    assert todo.title == "Call the plumber"
    assert todo.pattern == "dashed"  # a movable suggestion, not a fixed block
    assert todo.todo_id is not None
    assert todo.estimate_minutes == 20
    assert todo.state in {"now", "upcoming"}
    assert shape.fitted_count == 1


def test_suggestions_are_forward_only_the_past_is_quiet(store, noon):
    """A todo is never back-dated into a gap that already elapsed this morning."""
    store.add_todo("Draft the report", estimate_minutes=30, priority=1)
    shape = build_day_shape(store, now=noon, settings=UTC)
    # Every fitted todo starts at or after now — the morning stays plain free time.
    for s in shape.segments:
        if s.kind == "todo":
            assert s.start_at >= shape.now
    # The pre-noon stretch exists and is free (drawn, not suggested into).
    past_free = [s for s in shape.segments if s.state == "past"]
    assert past_free and all(s.kind == "free" for s in past_free)


def test_small_remainder_after_a_todo_still_tiles(store, noon):
    """A sliver of free time left after a fitted todo is drawn, not dropped.

    Segments tile the day: an 18-min gap that a 15-min block only partly fills
    must still render the ~3-min tail (regression guard — it used to require the
    remainder clear the 10-min window floor, silently dropping real time).
    """
    # A commitment starting exactly at now leaves no pre-now gap; the only forward
    # gap is the 18 minutes between it and the next block.
    store.upsert_commitment(
        title="Morning block", start_at=_at(noon),
        end_at=_at(noon + timedelta(hours=1)), external_id="w:1",
    )
    store.upsert_commitment(
        title="Next block", start_at=_at(noon + timedelta(hours=1, minutes=18)),
        end_at=_at(noon + timedelta(hours=1, minutes=48)), external_id="w:2",
    )
    store.add_todo("Quick note", estimate_minutes=5, priority=2)
    shape = build_day_shape(store, now=noon, settings=UTC)
    todo = next(s for s in shape.segments if s.kind == "todo")
    # The tail free segment starts exactly where the todo ends (no hole) and is
    # shorter than the 10-min window floor — proof it's no longer dropped.
    tail = next(
        s for s in shape.segments
        if s.kind == "free" and s.start_at == todo.end_at
    )
    assert 0 < tail.minutes < 10


def test_no_now_marker_at_the_end_of_the_band(store):
    """At exactly the band's end the day is done — no marker fraction is reported."""
    # 22:00 UTC is the default waking-band end; with nothing on the calendar the
    # band isn't widened, so now sits exactly on band_end.
    now = utcnow().replace(hour=22, minute=0, second=0, microsecond=0)
    shape = build_day_shape(store, now=now, settings=UTC)
    assert shape.band_end_local == "22:00"
    assert shape.now_fraction is None


def test_now_marker_and_states(store, noon):
    """Blocks are classified past/now/upcoming and now_fraction locates the marker."""
    # A commitment in progress right now.
    store.upsert_commitment(
        title="Standup", start_at=_at(noon - timedelta(minutes=10)),
        end_at=_at(noon + timedelta(minutes=20)), external_id="w:1",
    )
    shape = build_day_shape(store, now=noon, settings=UTC)
    standup = next(s for s in shape.segments if s.title == "Standup")
    assert standup.state == "now"
    # now sits inside the waking band, so the marker has a real fraction.
    assert shape.now_fraction is not None
    assert 0.0 <= shape.now_fraction <= 1.0


def test_every_segment_carries_a_noncolour_kind_signal(store, noon):
    """CHI-2024: meaning never rides on colour alone — glyph + pattern + kind word."""
    store.upsert_commitment(
        title="Meeting", start_at=_at(noon + timedelta(hours=2)),
        end_at=_at(noon + timedelta(hours=3)), external_id="w:9",
    )
    store.add_todo("Email back", estimate_minutes=15, priority=1)
    shape = build_day_shape(store, now=noon, settings=UTC)
    patterns = {"commitment": "solid", "todo": "dashed", "free": "dotted"}
    for s in shape.segments:
        assert s.glyph, "every block needs a glyph"
        assert s.pattern == patterns[s.kind], "kind must map to a distinct border pattern"
        assert s.kind in patterns


def test_double_booking_is_flagged_in_words(store, noon):
    """Overlapping commitments set the conflict flag (surfaced as text, not hue)."""
    store.upsert_commitment(
        title="Call A", start_at=_at(noon + timedelta(hours=1)),
        end_at=_at(noon + timedelta(hours=2)), external_id="w:a",
    )
    store.upsert_commitment(
        title="Call B", start_at=_at(noon + timedelta(hours=1, minutes=30)),
        end_at=_at(noon + timedelta(hours=2, minutes=30)), external_id="w:b",
    )
    shape = build_day_shape(store, now=noon, settings=UTC)
    call_b = next(s for s in shape.segments if s.title == "Call B")
    assert call_b.conflict is True


def test_fyi_event_is_shown_but_marked(store, noon):
    """An FYI event (someone else's) appears, flagged, and isn't counted as your time."""
    store.upsert_commitment(
        title="Kid's recital", start_at=_at(noon + timedelta(hours=2)),
        end_at=_at(noon + timedelta(hours=3)), external_id="fyi:1", kind="fyi",
    )
    shape = build_day_shape(store, now=noon, settings=UTC)
    recital = next(s for s in shape.segments if s.title == "Kid's recital")
    assert recital.fyi is True
    assert recital.kind == "commitment"
    # FYI time isn't yours — it doesn't add to committed minutes.
    assert shape.committed_minutes == 0.0


def test_avoided_todo_is_named_as_such(store, noon):
    """A todo you keep skipping is fitted with the 'avoided' reason, not 'fits'."""
    old = utcnow() - timedelta(days=6)
    tid = store.add_todo("File the taxes", estimate_minutes=30, priority=2)
    # Backdate its creation so it registers as avoided.
    store.conn.execute(
        "UPDATE todos SET created_at = ? WHERE id = ?", (_at(old), tid)
    )
    store.conn.commit()
    shape = build_day_shape(store, now=noon, settings=UTC)
    todo = next((s for s in shape.segments if s.kind == "todo"), None)
    assert todo is not None
    assert todo.reason == "avoided"
    assert todo.glyph == "🐢"
    assert "skipping" in (todo.detail or "")


def test_times_render_in_local_zone(store):
    """Band + block times read in the user's zone, not raw UTC."""
    eastern = Settings(webhook_secret=SECRET, timezone="America/New_York")
    # now = 14:00 UTC = 10:00 EDT — mid-morning, local day intact.
    now = datetime(2026, 7, 6, 14, 0, 0)
    store.upsert_commitment(
        title="Lunch", start_at="2026-07-06 16:00:00",  # 12:00 EDT
        end_at="2026-07-06 17:00:00", external_id="p:1",
    )
    shape = build_day_shape(store, now=now, settings=eastern)
    lunch = next(s for s in shape.segments if s.title == "Lunch")
    assert lunch.start_local == "12:00"  # local, not the 16:00 UTC wall clock
    assert shape.now_local == "10:00"


# --- Render ------------------------------------------------------------------


def test_render_is_monochrome_and_marks_now(store, noon):
    """The CLI render reads as a shape with no colour — glyphs, times, a ▸ for now."""
    store.upsert_commitment(
        title="Standup", start_at=_at(noon - timedelta(minutes=5)),
        end_at=_at(noon + timedelta(minutes=25)), external_id="w:1", hardness="hard",
    )
    store.add_todo("Tidy inbox", estimate_minutes=15, priority=1)
    text = render_day_shape(build_day_shape(store, now=noon, settings=UTC))
    assert text.startswith("# Today —")
    assert "▸" in text  # the in-progress block is marked
    assert "Standup" in text and "(hard)" in text
    assert "suggested" in text  # a fitted todo is labelled a suggestion


def test_render_empty_is_calm(store, noon):
    """An empty waking day renders a single calm line, never a scolding void."""
    # Force an empty band by asking on a day with nothing at all.
    shape = build_day_shape(store, now=noon, settings=UTC)
    text = render_day_shape(shape)
    assert "# Today —" in text


def test_payload_is_json_friendly(store, noon):
    """day_shape_payload flattens to plain dicts/lists (no dataclasses)."""
    store.add_todo("Something", estimate_minutes=20)
    payload = day_shape_payload(build_day_shape(store, now=noon, settings=UTC))
    assert isinstance(payload["segments"], list)
    assert set(payload) >= {
        "date", "tz", "band_start_local", "band_end_local", "now_fraction",
        "committed_minutes", "free_minutes", "fitted_count", "segments",
    }
    for seg in payload["segments"]:
        assert {"kind", "glyph", "pattern", "state", "start_local"} <= set(seg)


# --- Endpoint + page ---------------------------------------------------------


def test_day_endpoint_is_token_guarded(store):
    """GET /day returns the shape payload + text, and 401s without a token."""
    # Anchor the commitment to midday *today* (not now+2h): the endpoint builds the
    # shape from the server's real clock, so a now-relative offset slips into
    # tomorrow when the test runs in the last couple of UTC hours, emptying today's
    # shape. Midday always falls inside today's band regardless of the run time.
    midday = utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    store.upsert_commitment(
        title="Dentist", start_at=_at(midday),
        end_at=_at(midday + timedelta(hours=1)), external_id="p:1",
    )
    app = create_app(store=store, settings=UTC)
    with TestClient(app) as c:
        assert c.get("/day").status_code == 401
        body = c.get("/day", headers={"X-Prefrontal-Token": SECRET}).json()
    assert "segments" in body and "text" in body
    assert "# Today —" in body["text"]
    assert any(s["kind"] == "commitment" for s in body["segments"])


def test_day_board_page_serves(store):
    """GET /day/board serves the self-contained timeline shell (no auth needed)."""
    app = create_app(store=store, settings=UTC)
    with TestClient(app) as c:
        r = c.get("/day/board")
    assert r.status_code == 200
    assert 'id="tl"' in r.text  # the timeline container
    assert 'href="/day/board"' in r.text  # its own nav entry
