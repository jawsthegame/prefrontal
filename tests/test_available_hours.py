"""Per-weekday "available hours" — storage, band resolution, and the API.

Covers the pure layer (parse the stored JSON, resolve a band per weekday, and
``find_slots`` honouring it) and the ``GET/POST /schedule/available-hours`` HTTP
surface (defaults, partial writes, off-day preservation, validation), plus the
behavioural payoff: the hours actually gate ``/schedule/slots``.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.scheduling import (
    STATE_AVAILABLE_HOURS_KEY,
    WindowConfig,
    find_slots,
    parse_available_hours,
    window_config_for,
)
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "avail-secret"
# A Monday noon, UTC, so weekday math is unambiguous in the tests below.
MONDAY_NOON = datetime(2026, 6, 29, 12, 0, 0)


# -- pure: parse + band resolution ------------------------------------------


def test_parse_available_hours_absent_is_unconfigured() -> None:
    assert parse_available_hours(None) is None
    assert parse_available_hours("") is None
    assert parse_available_hours("not json") is None
    assert parse_available_hours("[1, 2]") is None  # not an object


def test_parse_available_hours_reads_available_and_off_days() -> None:
    raw = json.dumps({
        "mon": {"available": True, "start": "09:00", "end": "17:00"},
        "sun": {"available": False, "start": "10:00", "end": "14:00"},
    })
    bands = parse_available_hours(raw)
    assert bands == {0: (9 * 60, 17 * 60), 6: None}  # Mon band, Sun off


def test_parse_available_hours_drops_wrapping_or_bad_bands() -> None:
    # A wrapping (overnight) band and a garbage clock are both skipped, so the day
    # falls back to the flat band rather than producing an overnight slot.
    raw = json.dumps({
        "mon": {"available": True, "start": "22:00", "end": "06:00"},  # wraps
        "tue": {"available": True, "start": "nope", "end": "17:00"},   # unparseable
    })
    assert parse_available_hours(raw) is None  # nothing usable → unconfigured


def _cfg(schedule: dict | None) -> WindowConfig:
    raw = json.dumps(schedule) if schedule is not None else None
    return WindowConfig.build(state_available_hours=raw)


def test_band_for_weekday_falls_back_when_unconfigured() -> None:
    cfg = _cfg(None)  # default off-zone 22:00-06:00 → awake 06:00-22:00
    assert cfg.band_for_weekday(0) == ("06:00", "22:00")
    assert cfg.band_for_weekday(6) == ("06:00", "22:00")


def test_band_for_weekday_configured_day_and_off_day() -> None:
    cfg = _cfg({
        "mon": {"available": True, "start": "09:00", "end": "17:00"},
        "sun": {"available": False, "start": "10:00", "end": "14:00"},
    })
    assert cfg.band_for_weekday(0) == ("09:00", "17:00")  # Mon
    assert cfg.band_for_weekday(6) is None                # Sun off
    # A weekday absent from the (partial) schedule still falls back to the flat band.
    assert cfg.band_for_weekday(2) == ("06:00", "22:00")  # Wed


# -- pure: find_slots honours the resolver ----------------------------------


def test_find_slots_skips_an_unavailable_weekday() -> None:
    cfg = _cfg({"mon": {"available": False, "start": "09:00", "end": "17:00"}})
    # Scan just Monday (offset 0). With Monday off, no slots at all.
    slots = find_slots(
        [], MONDAY_NOON, "UTC", minutes=30, days=1,
        band_for_weekday=cfg.band_for_weekday,
    )
    assert slots == []


def test_find_slots_narrows_to_the_days_band() -> None:
    # Monday available 13:00-15:00 only. Scanning Monday from noon, the sole window
    # is 13:00-15:00 (120 min), not the flat 06:00-22:00.
    cfg = _cfg({"mon": {"available": True, "start": "13:00", "end": "15:00"}})
    slots = find_slots(
        [], MONDAY_NOON, "UTC", minutes=30, days=1,
        band_for_weekday=cfg.band_for_weekday,
    )
    assert len(slots) == 1
    assert slots[0].minutes == 120
    assert slots[0].start.endswith("13:00:00")
    assert slots[0].end.endswith("15:00:00")


def test_find_slots_without_resolver_is_unchanged() -> None:
    # Regression: the flat-band path (no resolver) still works as before.
    slots = find_slots([], MONDAY_NOON, "UTC", minutes=30, days=1,
                       awake_band=("06:00", "22:00"))
    assert len(slots) == 1
    assert slots[0].start.endswith("12:00:00")  # clamped to now
    assert slots[0].end.endswith("22:00:00")


# -- HTTP surface -----------------------------------------------------------


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


@pytest.fixture()
def client(store):
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_get_defaults_to_flat_band_and_unconfigured(client) -> None:
    body = client.get("/schedule/available-hours", headers=_auth()).json()
    assert body["configured"] is False
    assert set(body["days"]) == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    for day in body["days"].values():
        assert day == {"available": True, "start": "06:00", "end": "22:00"}


def test_post_round_trips_and_marks_configured(client, store) -> None:
    resp = client.post(
        "/schedule/available-hours",
        json={"days": {"mon": {"available": True, "start": "09:00", "end": "17:00"}}},
        headers=_auth(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["days"]["mon"] == {"available": True, "start": "09:00", "end": "17:00"}
    # Persisted as the coaching key, marked an explicit user choice.
    entry = store.all_state()[STATE_AVAILABLE_HOURS_KEY]
    assert entry["source"] == "explicit"


def test_post_is_a_partial_merge(client) -> None:
    client.post(
        "/schedule/available-hours",
        json={"days": {"mon": {"available": True, "start": "08:00", "end": "16:00"}}},
        headers=_auth(),
    )
    # A second write naming only Tuesday leaves Monday untouched.
    body = client.post(
        "/schedule/available-hours",
        json={"days": {"tue": {"available": True, "start": "10:00", "end": "18:00"}}},
        headers=_auth(),
    ).json()
    assert body["days"]["mon"] == {"available": True, "start": "08:00", "end": "16:00"}
    assert body["days"]["tue"] == {"available": True, "start": "10:00", "end": "18:00"}


def test_off_day_keeps_its_band_for_toggling_back_on(client) -> None:
    body = client.post(
        "/schedule/available-hours",
        json={"days": {"sun": {"available": False, "start": "10:00", "end": "14:00"}}},
        headers=_auth(),
    ).json()
    # Stored (and echoed) with available=false but the band preserved.
    assert body["days"]["sun"] == {"available": False, "start": "10:00", "end": "14:00"}


def test_post_rejects_end_before_start(client) -> None:
    resp = client.post(
        "/schedule/available-hours",
        json={"days": {"mon": {"available": True, "start": "17:00", "end": "09:00"}}},
        headers=_auth(),
    )
    assert resp.status_code == 422


def test_post_rejects_bad_clock_and_unknown_weekday(client) -> None:
    assert client.post(
        "/schedule/available-hours",
        json={"days": {"mon": {"available": True, "start": "9am", "end": "17:00"}}},
        headers=_auth(),
    ).status_code == 422
    assert client.post(
        "/schedule/available-hours",
        json={"days": {"funday": {"available": True, "start": "09:00", "end": "17:00"}}},
        headers=_auth(),
    ).status_code == 422


# -- behavioural: the hours gate slot-finding -------------------------------


def test_available_hours_gate_the_slots_endpoint(client) -> None:
    # Mark every weekday unavailable → the slot finder returns nothing all week.
    off = {day: {"available": False, "start": "09:00", "end": "17:00"}
           for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    client.post("/schedule/available-hours", json={"days": off}, headers=_auth())
    body = client.get(
        "/calendar/slots", params={"minutes": 30, "days": 7}, headers=_auth()
    ).json()
    assert body["slots"] == []


def test_window_config_for_reads_the_state_key(store) -> None:
    store.set_state(
        STATE_AVAILABLE_HOURS_KEY,
        json.dumps({"mon": {"available": False, "start": "09:00", "end": "17:00"}}),
        source="explicit",
    )
    cfg = window_config_for(Settings(webhook_secret=SECRET), store)
    assert cfg.band_for_weekday(0) is None  # Monday off, read straight from state
