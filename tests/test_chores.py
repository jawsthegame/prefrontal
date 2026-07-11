"""Tests for recurring shared chores — the "load-balancing" notification flow.

Covers the four layers, mirroring test_household.py: the pure timing/message
logic (no store, no clock), the household-scoped storage (upsert, completion
log, dedup cursors), the deterministic sheet render, the reminder/miss-handoff
sweep (:func:`run_chores_check`, with a mock transport asserting who gets what),
and the HTTP + one-tap endpoints.
"""

from __future__ import annotations

import datetime
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.clock import local_datetime
from prefrontal.config import Settings
from prefrontal.household import (
    apply_service_shift,
    away_covers,
    build_sheet,
    capped_away_window,
    chore_missed_cover_message,
    chore_missed_owner_message,
    chore_missed_partner_message,
    chore_reminder_cover_message,
    chore_reminder_message,
    chore_reminder_shift_message,
    describe_chore_days,
    describe_month_days,
    describe_schedule,
    effective_chore_schedule,
    fmt_time_12h,
    format_chore_days,
    format_month_days,
    log_chore_done_and_celebrate,
    miss_due,
    normalize_chore,
    normalize_routine,
    parse_chore_days,
    parse_month_days,
    reminder_due,
    render_sheet,
    resolve_chore_context,
    routine_chore_status,
    routine_complete_message,
    routine_is_complete,
    run_chores_check,
    scheduled_on,
    service_week,
    with_effective_schedule,
)
from prefrontal.impact import utcnow
from prefrontal.integrations.delivery import DeliveryClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.webhooks.app import create_app
from prefrontal.webhooks.oauth import sign_action

BASE = "https://mac-mini.tailnet.ts.net"
SIGNING = "chore-signing-key"
UTC = Settings(timezone="UTC")

# A Wednesday, 9:40pm UTC — inside the reminder window for a 22:00/-30m chore.
REMIND_NOW = datetime.datetime(2026, 7, 1, 21, 40, 0)
# Same Wednesday, 10:30pm — past a 22:00 due time (the miss window).
MISS_NOW = datetime.datetime(2026, 7, 1, 22, 30, 0)


# --- fixtures ----------------------------------------------------------------


@pytest.fixture()
def store():
    """In-memory store: two co-parents (Dana, Alex) sharing one household."""
    conn = init_db(":memory:")
    s = MemoryStore(conn)
    provision_user(s, "dana", display_name="Dana", token="dana-tok")
    provision_user(s, "alex", display_name="Alex", token="alex-tok")
    hid = s.create_household("The Kims")
    s.set_user_household("dana", hid)
    s.set_user_household("alex", hid)
    try:
        yield s
    finally:
        conn.close()


@pytest.fixture()
def dana(store):
    return store.scoped(store.get_user("dana")["id"])


@pytest.fixture()
def alex(store):
    return store.scoped(store.get_user("alex")["id"])


@pytest.fixture()
def client(store):
    app = create_app(store=store, settings=Settings(session_secret=SIGNING, oauth_base_url=BASE))
    with TestClient(app) as c:
        yield c


def _h(token):
    return {"X-Prefrontal-Token": token}


def _capture_client():
    """A DeliveryClient whose mock transport records every (topic, message) sent."""
    sent: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        sent.append({"topic": body.get("topic", ""), "message": body.get("message", "")})
        return httpx.Response(200, json={"id": "x"})

    client = DeliveryClient.from_settings(Settings(), transport=httpx.MockTransport(handler))
    return client, sent


# --- pure: parsing + formatting ----------------------------------------------


def test_parse_and_format_days_round_trip():
    assert parse_chore_days("0,2,4") == [0, 2, 4]
    assert parse_chore_days("") == []  # every-day sentinel
    assert parse_chore_days("7, x, 3, 3") == [3]  # junk + out-of-range dropped, de-duped
    assert parse_chore_days([1, 0]) == [0, 1]
    assert format_chore_days([2, 0, 0]) == "0,2"


def test_describe_days_and_time():
    assert describe_chore_days("") == "every day"
    assert describe_chore_days("0,1,2,3,4") == "weekdays"
    assert describe_chore_days("5,6") == "weekends"
    assert describe_chore_days("0,2") == "Mon, Wed"
    assert fmt_time_12h("22:00") == "10:00pm"
    assert fmt_time_12h("09:05") == "9:05am"


def test_parse_and_format_month_days():
    assert parse_month_days("1,15") == [1, 15]
    assert parse_month_days("") == []  # not month-scheduled
    assert parse_month_days("0, 32, 15, x, 15") == [15]  # out-of-range + junk + dupes dropped
    assert parse_month_days([31, 1]) == [1, 31]
    assert format_month_days([15, 1, 1]) == "1,15"


def test_describe_month_days_and_schedule():
    assert describe_month_days("") == ""
    assert describe_month_days("1") == "the 1st of the month"
    assert describe_month_days("1,2,15,22") == "the 1st, 2nd, 15th & 22nd of the month"
    # describe_schedule: month days win when set, else the weekday phrase.
    assert describe_schedule("0,1,2,3,4", "") == "weekdays"
    assert describe_schedule("0,2", "1,15") == "the 1st & 15th of the month"


def test_scheduled_on_month_days_and_precedence():
    wed = REMIND_NOW  # 2026-07-01 is a Wednesday, the 1st
    # Month day matches the calendar day, ignoring the weekday.
    assert scheduled_on("", "1", wed)
    assert not scheduled_on("", "2", wed)
    # Month days take precedence over weekdays: a Mon-only weekday set is ignored.
    assert scheduled_on("0", "1", wed)
    # Empty month days → fall back to weekdays (Wed = 2).
    assert scheduled_on("2", "", wed)
    assert not scheduled_on("0", "", wed)


def test_scheduled_on_clamps_past_short_month():
    # 2026-02-28 is the last day of February; a "31st" schedule fires on it.
    feb_last = datetime.datetime(2026, 2, 28, 9, 0, 0)
    assert scheduled_on("", "31", feb_last)
    assert scheduled_on("", "15", feb_last) is False
    # In a 31-day month the 31st is literal (no clamp collision with the 28th).
    jan_28 = datetime.datetime(2026, 1, 28, 9, 0, 0)
    assert scheduled_on("", "31", jan_28) is False


# --- pure: normalize ---------------------------------------------------------


def test_normalize_chore_clean():
    clean, err = normalize_chore(
        {"title": "  run the dishwasher ", "due_time": "22:00", "days": [2, 0],
         "remind_before": 45, "impact": "  it makes the morning harder "}
    )
    assert err is None
    assert clean == {
        "title": "run the dishwasher",
        "owner_id": None,
        "days": "0,2",
        "month_days": "",
        "due_time": "22:00",
        "remind_before": 45,
        "impact": "it makes the morning harder",
        "enabled": True,
        "away_behavior": "keep",
        "service": None,
    }


def test_normalize_chore_away_behavior():
    # Explicit suppress is accepted and normalized (case-insensitive).
    clean, err = normalize_chore(
        {"title": "take out trash", "due_time": "20:00", "away_behavior": "SUPPRESS"}
    )
    assert err is None and clean["away_behavior"] == "suppress"
    # Blank / missing falls back to the safe default.
    clean, err = normalize_chore({"title": "pay bill", "due_time": "20:00", "away_behavior": ""})
    assert err is None and clean["away_behavior"] == "keep"
    # An unknown value is rejected, not silently coerced.
    bad, err = normalize_chore(
        {"title": "x", "due_time": "20:00", "away_behavior": "reassign"}
    )
    assert bad is None and "away_behavior" in err


def test_normalize_chore_and_routine_carry_month_days():
    clean, err = normalize_chore({"title": "pay allowance", "due_time": "18:00",
                                  "month_days": [15, 1, 40]})
    assert err is None and clean["month_days"] == "1,15"  # 40 dropped, sorted
    clean_r, err_r = normalize_routine({"title": "Bills", "month_days": [1]})
    assert err_r is None and clean_r["month_days"] == "1"


@pytest.mark.parametrize(
    "raw",
    [
        {"title": "", "due_time": "22:00"},
        {"title": "x", "due_time": "nope"},
        {"title": "x", "due_time": "22:00", "remind_before": 0},
        {"title": "x", "due_time": "22:00", "remind_before": 99999},
        {"title": "x", "due_time": "22:00", "owner_id": "not-an-int"},
    ],
)
def test_normalize_chore_rejects_bad_input(raw):
    clean, err = normalize_chore(raw)
    assert clean is None and err


# --- context gate: away window (pure) ----------------------------------------

_WED = REMIND_NOW  # 2026-07-01, a Wednesday
_AWAY = {"starts_on": "2026-06-30", "ends_on": "2026-07-05", "note": "beach trip"}


@pytest.mark.parametrize(
    "window, covered",
    [
        (_AWAY, True),                                                    # inside
        ({"starts_on": "2026-07-01", "ends_on": "2026-07-01"}, True),     # single-day, inclusive
        ({"starts_on": "2026-07-02", "ends_on": "2026-07-05"}, False),    # starts tomorrow
        ({"starts_on": "2026-06-20", "ends_on": "2026-06-30"}, False),    # ended yesterday
        (None, False),                                                    # not away
        ({"starts_on": "2026-06-30", "ends_on": ""}, False),              # half-set → fail-open
    ],
)
def test_away_covers(window, covered):
    assert away_covers(window, _WED) is covered


def test_resolve_chore_context_suppresses_only_location_bound_when_away():
    trash = {"id": 1, "title": "trash", "enabled": True, "days": "", "month_days": "",
             "away_behavior": "suppress"}
    bill = {"id": 2, "title": "pay bill", "enabled": True, "days": "", "month_days": "",
            "away_behavior": "keep"}
    # Away: the location-bound chore is suppressed (with a reason), the bill proceeds.
    d = resolve_chore_context(trash, now_local=_WED, away_window=_AWAY)
    assert d.action == "suppress" and "beach trip" in d.reason and "2026-07-05" in d.reason
    assert resolve_chore_context(bill, now_local=_WED, away_window=_AWAY).action == "proceed"
    # Not away: even a suppress-tagged chore proceeds normally.
    assert resolve_chore_context(trash, now_local=_WED, away_window=None).action == "proceed"


def test_resolve_chore_context_ignores_unscheduled_and_disabled():
    # Only tomorrow (Thursday=3), so not scheduled on _WED → no suppression to report.
    not_today = {"id": 1, "title": "trash", "enabled": True, "days": "3", "month_days": "",
                 "away_behavior": "suppress"}
    assert resolve_chore_context(not_today, now_local=_WED, away_window=_AWAY).action == "proceed"
    paused = {"id": 2, "title": "trash", "enabled": False, "days": "", "month_days": "",
              "away_behavior": "suppress"}
    assert resolve_chore_context(paused, now_local=_WED, away_window=_AWAY).action == "proceed"


def test_resolve_chore_context_all_members_away_suppresses_like_household():
    trash = {"id": 1, "title": "trash", "enabled": True, "days": "", "month_days": "",
             "away_behavior": "suppress"}
    bill = {"id": 2, "title": "pay bill", "enabled": True, "days": "", "month_days": "",
            "away_behavior": "keep"}
    # No household window, but everyone's individually away → same effect: nobody home.
    d = resolve_chore_context(trash, now_local=_WED, away_window=None, all_members_away=True)
    assert d.action == "suppress" and "everyone is away" in d.reason
    # A keep chore still proceeds (someone can pay a bill remotely).
    assert resolve_chore_context(
        bill, now_local=_WED, away_window=None, all_members_away=True
    ).action == "proceed"
    # Someone still home → no all-away suppression.
    assert resolve_chore_context(
        trash, now_local=_WED, away_window=None, all_members_away=False
    ).action == "proceed"


def test_capped_away_window_starts_at_departure_ends_capped():
    # Departed the 1st, confirming on the 3rd → window covers from the 1st (so an
    # already-slipped chore is covered) and ends cap_days after today.
    w = capped_away_window("2026-07-01", "2026-07-03", cap_days=14)
    assert w == {"starts_on": "2026-07-01", "ends_on": "2026-07-17",
                 "note": "auto-detected trip"}


def test_cover_message_builders_name_the_away_owner():
    chore = {"title": "take out trash", "due_time": "20:00", "impact": "bins overflow"}
    assert "Dana's away" in chore_reminder_cover_message(chore, "Dana")
    assert "take out trash" in chore_reminder_cover_message(chore, "Dana")
    miss = chore_missed_cover_message(chore, "Dana")
    assert "Dana" in miss and "take out trash" in miss


# --- pure: timing predicates -------------------------------------------------


def _chore(**over):
    base = {"enabled": True, "days": "", "due_time": "22:00", "remind_before": 30,
            "impact": None, "owner_id": None}
    base.update(over)
    return base


def test_reminder_due_only_inside_the_lead_window():
    c = _chore()
    assert reminder_due(c, now_local=REMIND_NOW, done_today=False)          # 21:40 in [21:30,22:00)
    early = REMIND_NOW.replace(hour=21, minute=0)
    assert not reminder_due(c, now_local=early, done_today=False)           # before the window
    assert not reminder_due(c, now_local=MISS_NOW, done_today=False)        # past due → miss's job


def test_reminder_and_miss_respect_done_and_dedup():
    c = _chore()
    assert not reminder_due(c, now_local=REMIND_NOW, done_today=True)       # already done
    today = REMIND_NOW.strftime("%Y-%m-%d")
    assert not reminder_due(c, now_local=REMIND_NOW, done_today=False, last_reminded_on=today)
    assert miss_due(c, now_local=MISS_NOW, done_today=False)
    assert not miss_due(c, now_local=MISS_NOW, done_today=True)
    assert not miss_due(c, now_local=MISS_NOW, done_today=False, last_missed_on=today)


def test_scheduling_gates_on_weekday():
    # REMIND_NOW is a Wednesday (weekday 2); a Mon/Fri-only chore shouldn't fire.
    c = _chore(days="0,4")
    assert not reminder_due(c, now_local=REMIND_NOW, done_today=False)
    assert not miss_due(c, now_local=MISS_NOW, done_today=False)
    assert reminder_due(_chore(days="2"), now_local=REMIND_NOW, done_today=False)


def test_scheduling_gates_on_month_day():
    # REMIND_NOW is the 1st of the month; a chore due on the 1st fires, the 2nd doesn't.
    assert reminder_due(_chore(month_days="1"), now_local=REMIND_NOW, done_today=False)
    assert miss_due(_chore(month_days="1"), now_local=MISS_NOW, done_today=False)
    assert not reminder_due(_chore(month_days="2"), now_local=REMIND_NOW, done_today=False)
    # Month days win over weekdays: a Mon-only weekday set doesn't stop the 1st firing.
    assert reminder_due(_chore(days="0", month_days="1"), now_local=REMIND_NOW, done_today=False)


def test_disabled_chore_never_fires():
    c = _chore(enabled=False)
    assert not reminder_due(c, now_local=REMIND_NOW, done_today=False)
    assert not miss_due(c, now_local=MISS_NOW, done_today=False)


def test_messages_carry_impact_and_title():
    c = _chore(title="run the dishwasher", impact="it makes the morning harder")
    assert "run the dishwasher" in chore_reminder_message(c)
    assert "makes the morning harder" in chore_reminder_message(c)
    assert "10:00pm" in chore_missed_owner_message(c)
    assert "Heads up" in chore_missed_partner_message(c)


# --- storage -----------------------------------------------------------------


def test_set_chore_upsert_and_listing(store, dana):
    dana_id = store.get_user("dana")["id"]
    cid = dana.set_chore(title="run the dishwasher", due_time="22:00", days="0,2",
                         owner_id=dana_id, impact="mornings", updated_by=dana_id)
    # Upsert on title is a full-definition replace (like set_agreement): re-submit
    # every field an edit form carries.
    again = dana.set_chore(title="run the dishwasher", due_time="21:30",
                           owner_id=dana_id, updated_by=dana_id)
    assert cid == again
    chores = dana.chores()
    assert len(chores) == 1
    assert chores[0]["due_time"] == "21:30"  # last write wins
    assert chores[0]["owner_name"] == "Dana"


def test_month_days_round_trip_through_store(store, dana):
    dana_id = store.get_user("dana")["id"]
    cid = dana.set_chore(title="pay allowance", due_time="18:00", month_days="1,15",
                         updated_by=dana_id)
    chore = next(c for c in dana.chores() if c["id"] == cid)
    assert chore["month_days"] == "1,15"
    assert dana.chore(cid)["month_days"] == "1,15"
    rid = dana.set_routine(title="Bills", month_days="1", due_time="09:00", updated_by=dana_id)
    assert next(r for r in dana.routines() if r["id"] == rid)["month_days"] == "1"


def test_co_parents_share_chores(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    dana.set_chore(title="lunches", due_time="07:30", updated_by=dana_id)
    assert [c["title"] for c in alex.chores()] == ["lunches"]  # household-scoped


def test_completion_log_is_idempotent_and_tracked(store, dana):
    dana_id = store.get_user("dana")["id"]
    cid = dana.set_chore(title="dishes", due_time="22:00", updated_by=dana_id)
    first = dana.log_chore_done(chore_id=cid, done_on="2026-07-01", done_by=dana_id)
    assert first["created"] is True
    second = dana.log_chore_done(chore_id=cid, done_on="2026-07-01", done_by=dana_id)
    assert second["created"] is False  # same day → in-place, not a double-log
    assert dana.chore_ids_done_on("2026-07-01") == {cid}
    assert dana.chore_ids_done_on("2026-07-02") == set()
    assert dana.log_chore_done(chore_id=999, done_on="2026-07-01", done_by=dana_id) is None


def test_remove_and_enable(store, dana):
    dana_id = store.get_user("dana")["id"]
    cid = dana.set_chore(title="dishes", due_time="22:00", updated_by=dana_id)
    assert dana.set_chore_enabled(cid, False) is True
    assert dana.chores()[0]["enabled"] == 0
    assert dana.remove_chore(cid) is True
    assert dana.chores() == []
    assert dana.remove_chore(cid) is False


def test_away_behavior_round_trips_through_store(store, dana):
    dana_id = store.get_user("dana")["id"]
    cid = dana.set_chore(title="trash", due_time="20:00", away_behavior="suppress",
                         updated_by=dana_id)
    assert dana.chores()[0]["away_behavior"] == "suppress"
    assert dana.chore(cid)["away_behavior"] == "suppress"  # single-row read carries it too
    # update_chore preserves it when unspecified? No — update passes the full shape;
    # here we flip it back to the default explicitly.
    assert dana.update_chore(cid, title="trash", due_time="20:00", away_behavior="keep",
                             updated_by=dana_id) == "ok"
    assert dana.chores()[0]["away_behavior"] == "keep"


def test_away_window_set_get_clear(store, dana, alex):
    assert dana.away_window() is None  # not away by default
    dana.set_away_window(starts_on="2026-07-10", ends_on="2026-07-17", note="beach")
    got = alex.away_window()  # household-scoped: the co-parent sees the same window
    assert got == {"starts_on": "2026-07-10", "ends_on": "2026-07-17", "note": "beach"}
    dana.clear_away_window()
    assert dana.away_window() is None
    dana.clear_away_window()  # idempotent


def test_member_away_window_is_per_user(store, dana, alex):
    assert dana.member_away_window() is None
    dana.set_member_away(starts_on="2026-07-10", ends_on="2026-07-17", note="work trip")
    # Per-user, NOT household-scoped: Dana away doesn't mark Alex away.
    assert dana.member_away_window() == {
        "starts_on": "2026-07-10", "ends_on": "2026-07-17", "note": "work trip"
    }
    assert alex.member_away_window() is None
    dana.clear_member_away()
    assert dana.member_away_window() is None
    dana.clear_member_away()  # idempotent


def test_update_chore_edits_renames_and_guards(store, dana, alex):
    """update_chore edits any attribute by id (incl. rename); ok/missing/duplicate."""
    dana_id = store.get_user("dana")["id"]
    alex_id = store.get_user("alex")["id"]
    rid = dana.set_routine(title="Evening", due_time="20:00", updated_by=dana_id)
    cid = dana.set_chore(title="dishes", due_time="22:00", owner_id=dana_id,
                         updated_by=dana_id)

    # Rename + reassign owner + retime + link under a routine, all at once.
    assert dana.update_chore(
        cid, title="run the dishwasher", due_time="21:30", owner_id=alex_id,
        routine_id=rid, updated_by=dana_id,
    ) == "ok"
    got = next(c for c in dana.chores() if c["id"] == cid)
    assert got["title"] == "run the dishwasher"
    assert got["owner_id"] == alex_id
    assert got["due_time"] == "21:30"
    assert got["routine_id"] == rid

    # A missing id → "missing"; renaming onto another chore's name → "duplicate".
    assert dana.update_chore(9999, title="x", updated_by=dana_id) == "missing"
    other = dana.set_chore(title="trash", due_time="19:00", updated_by=dana_id)
    assert dana.update_chore(other, title="run the dishwasher",
                             updated_by=dana_id) == "duplicate"


# --- sheet render ------------------------------------------------------------


def test_sheet_shows_chores_with_today_status(store, dana):
    dana_id = store.get_user("dana")["id"]
    cid = dana.set_chore(title="run the dishwasher", due_time="22:00", owner_id=dana_id,
                         impact="mornings", updated_by=dana_id)
    dana.log_chore_done(chore_id=cid, done_on=REMIND_NOW.strftime("%Y-%m-%d"), done_by=dana_id)
    sheet = build_sheet(dana, now=REMIND_NOW, timezone="UTC")
    assert sheet.counts["chores"] == 1
    assert sheet.chores[0]["done_today"] is True
    md = render_sheet(sheet)
    assert "## Shared chores" in md
    assert "[x] run the dishwasher" in md
    assert "Dana" in md and "mornings" in md


def test_sheet_flags_scheduled_today_per_chore(store, dana):
    """Each chore carries ``scheduled_today`` for its *effective* schedule on today."""
    did = store.get_user("dana")["id"]
    # REMIND_NOW is a Wednesday (weekday 2).
    every_day = dana.set_chore(title="dishes", due_time="22:00", owner_id=did, updated_by=did)
    today_only = dana.set_chore(title="wed thing", due_time="", days="2",
                                owner_id=did, updated_by=did)
    other_day = dana.set_chore(title="mon thing", due_time="", days="0",
                               owner_id=did, updated_by=did)
    # A chore that inherits a Wednesday routine's schedule is scheduled today too.
    rid = dana.set_routine(title="wed routine", days="2", updated_by=did)
    inherits = dana.set_chore(title="via routine", due_time="", owner_id=did,
                              routine_id=rid, updated_by=did)
    sheet = build_sheet(dana, now=REMIND_NOW, timezone="UTC")
    flags = {c["title"]: c["scheduled_today"] for c in sheet.chores}
    assert flags == {
        "dishes": True, "wed thing": True, "via routine": True, "mon thing": False
    }
    assert every_day and today_only and other_day and inherits  # ids returned


# --- the sweep: run_chores_check ---------------------------------------------


def test_sweep_reminds_only_the_owner(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    alex.set_state("ntfy_topic", "alex-topic")
    dana.set_chore(title="run the dishwasher", due_time="22:00", owner_id=dana_id,
                   updated_by=dana_id)
    client, sent = _capture_client()
    result = run_chores_check(dana, settings=UTC, now=REMIND_NOW, client=client)
    assert [s["topic"] for s in sent] == ["dana-topic"]  # only the owner
    assert "Time to run the dishwasher" in sent[0]["message"]
    assert result[0]["stage"] == "reminder"
    # Dedup: a second sweep the same window is a no-op.
    assert run_chores_check(dana, settings=UTC, now=REMIND_NOW, client=client) == []


def test_sweep_miss_handoff_splits_owner_and_partner(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    alex.set_state("ntfy_topic", "alex-topic")
    dana.set_chore(title="run the dishwasher", due_time="22:00", owner_id=dana_id,
                   impact="it makes Alex's morning harder", updated_by=dana_id)
    client, sent = _capture_client()
    result = run_chores_check(dana, settings=UTC, now=MISS_NOW, client=client)
    assert result[0]["stage"] == "missed"
    by_topic = {s["topic"]: s["message"] for s in sent}
    assert set(by_topic) == {"dana-topic", "alex-topic"}
    assert "still isn't done" in by_topic["dana-topic"]       # owner nudge
    assert "Heads up" in by_topic["alex-topic"]               # partner heads-up
    assert "morning harder" in by_topic["alex-topic"]


def test_sweep_skips_a_chore_done_today(store, dana):
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    cid = dana.set_chore(title="dishes", due_time="22:00", owner_id=dana_id, updated_by=dana_id)
    dana.log_chore_done(chore_id=cid, done_on=MISS_NOW.strftime("%Y-%m-%d"), done_by=dana_id)
    client, sent = _capture_client()
    assert run_chores_check(dana, settings=UTC, now=MISS_NOW, client=client) == []
    assert sent == []


def test_sweep_unassigned_chore_reminds_everyone(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    alex.set_state("ntfy_topic", "alex-topic")
    dana.set_chore(title="empty the bins", due_time="22:00", updated_by=dana_id)  # no owner
    client, sent = _capture_client()
    run_chores_check(dana, settings=UTC, now=REMIND_NOW, client=client)
    assert {s["topic"] for s in sent} == {"dana-topic", "alex-topic"}


def test_sweep_suppresses_location_bound_chore_while_away(store, dana):
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    trash = dana.set_chore(title="take out trash", due_time="22:00", owner_id=dana_id,
                           away_behavior="suppress", updated_by=dana_id)
    dana.set_chore(title="pay the water bill", due_time="22:00", owner_id=dana_id,
                   away_behavior="keep", updated_by=dana_id)
    # REMIND_NOW is 2026-07-01 — inside this window.
    dana.set_away_window(starts_on="2026-06-30", ends_on="2026-07-05", note="beach trip")

    client, sent = _capture_client()
    result = run_chores_check(dana, settings=UTC, now=REMIND_NOW, client=client)

    stages = {r["title"]: r["stage"] for r in result}
    assert stages == {"take out trash": "suppressed", "pay the water bill": "reminder"}
    # Only the bill actually notified; trash is silent while away.
    assert [s["topic"] for s in sent] == ["dana-topic"]
    assert "pay the water bill" in sent[0]["message"]
    # The suppressed chore left NO cursor, so it resumes cleanly once we're back:
    # clearing the window and sweeping again fires its reminder.
    assert next(c for c in dana.chores() if c["id"] == trash)["last_reminded_on"] is None
    dana.clear_away_window()
    client2, sent2 = _capture_client()
    result2 = run_chores_check(dana, settings=UTC, now=REMIND_NOW, client=client2)
    assert any(r["title"] == "take out trash" and r["stage"] == "reminder" for r in result2)
    assert "take out trash" in sent2[0]["message"]


# --- member away: reassignment ----------------------------------------------


def _away(scoped, start="2026-06-30", end="2026-07-05"):
    scoped.set_member_away(starts_on=start, ends_on=end, note="work trip")


def test_sweep_reassigns_away_owners_reminder_to_present_partner(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    alex.set_state("ntfy_topic", "alex-topic")
    dana.set_chore(title="take out trash", due_time="22:00", owner_id=dana_id,
                   updated_by=dana_id)
    _away(dana)  # Dana is away; Alex is home.

    client, sent = _capture_client()
    result = run_chores_check(dana, settings=UTC, now=REMIND_NOW, client=client)

    assert result[0]["stage"] == "reminder"
    # Only Alex is nudged, framed as covering for Dana. Dana (away) stays silent.
    assert [s["topic"] for s in sent] == ["alex-topic"]
    assert "Dana's away" in sent[0]["message"] and "take out trash" in sent[0]["message"]


def test_sweep_reassigns_away_owners_miss_to_present_partner(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    alex.set_state("ntfy_topic", "alex-topic")
    dana.set_chore(title="take out trash", due_time="22:00", owner_id=dana_id,
                   impact="bins overflow", updated_by=dana_id)
    _away(dana)

    client, sent = _capture_client()
    result = run_chores_check(dana, settings=UTC, now=MISS_NOW, client=client)

    assert result[0]["stage"] == "missed"
    # The present partner picks it up; the away owner isn't nagged on their trip.
    assert [s["topic"] for s in sent] == ["alex-topic"]
    assert "Dana" in sent[0]["message"] and "take out trash" in sent[0]["message"]


def test_sweep_away_partner_gets_no_heads_up(store, dana, alex):
    """Owner present, partner away: the away partner gets neither nudge nor heads-up."""
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    alex.set_state("ntfy_topic", "alex-topic")
    dana.set_chore(title="run the dishwasher", due_time="22:00", owner_id=dana_id,
                   updated_by=dana_id)
    _away(alex)  # the non-owner is away

    client, sent = _capture_client()
    run_chores_check(dana, settings=UTC, now=MISS_NOW, client=client)
    # Owner (present) gets the miss nudge; the away partner is silent.
    assert [s["topic"] for s in sent] == ["dana-topic"]
    assert "still isn't done" in sent[0]["message"]


def test_sweep_all_members_away_suppresses_location_bound(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    alex.set_state("ntfy_topic", "alex-topic")
    dana.set_chore(title="take out trash", due_time="22:00", owner_id=dana_id,
                   away_behavior="suppress", updated_by=dana_id)
    dana.set_chore(title="pay the water bill", due_time="22:00", owner_id=dana_id,
                   away_behavior="keep", updated_by=dana_id)
    _away(dana)
    _away(alex)  # nobody home

    client, sent = _capture_client()
    result = run_chores_check(dana, settings=UTC, now=REMIND_NOW, client=client)

    stages = {r["title"]: r["stage"] for r in result}
    # Location-bound chore suppressed (nobody to do it); the keep bill still fires
    # to its owner — even though Dana's away, someone should know a bill is due.
    assert stages["take out trash"] == "suppressed"
    assert stages["pay the water bill"] == "reminder"
    assert [s["topic"] for s in sent] == ["dana-topic"]
    assert "pay the water bill" in sent[0]["message"]


def test_sweep_unassigned_chore_skips_away_members(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    alex.set_state("ntfy_topic", "alex-topic")
    dana.set_chore(title="empty the bins", due_time="22:00", updated_by=dana_id)  # no owner
    _away(alex)

    client, sent = _capture_client()
    run_chores_check(dana, settings=UTC, now=REMIND_NOW, client=client)
    # Unassigned → only the present member is reminded (Alex is away).
    assert [s["topic"] for s in sent] == ["dana-topic"]


# --- service shifts (holiday pickup-day changes) -----------------------------

# REMIND_NOW is Wed 2026-07-01; its week starts Mon 2026-06-29.
_WEEK = "2026-06-29"
_TUE = datetime.datetime(2026, 6, 30, 21, 40, 0)  # Tue in the same week, reminder window


def test_service_week_is_the_local_monday():
    assert service_week(REMIND_NOW) == _WEEK          # Wed → that Monday
    assert service_week(_TUE) == _WEEK                # Tue → same Monday
    assert service_week(datetime.datetime(2026, 6, 29, 8, 0)) == _WEEK  # Monday itself


def test_apply_service_shift_moves_days_and_message():
    chore = {"id": 1, "title": "take out trash", "due_time": "22:00", "days": "1",
             "month_days": "5"}
    shifted = apply_service_shift(chore, {"shifted_weekday": 2, "reason": "July 4th"})
    assert shifted["days"] == "2" and shifted["month_days"] == ""   # moved to Wed, month cleared
    msg = chore_reminder_shift_message(shifted)
    assert "Wed" in msg and "July 4th" in msg and "take out trash" in msg


def test_service_and_shift_round_trip_through_store(store, dana):
    dana_id = store.get_user("dana")["id"]
    cid = dana.set_chore(title="take out trash", due_time="22:00", days="1",
                         service="trash", updated_by=dana_id)
    assert dana.chores()[0]["service"] == "trash"
    assert dana.chore(cid)["service"] == "trash"
    dana.set_service_shift(service="trash", week=_WEEK, shifted_weekday=2, reason="July 4th")
    got = dana.service_shift("trash", _WEEK)
    assert got["shifted_weekday"] == 2 and got["reason"] == "July 4th"
    assert dana.service_shift("trash", "2026-07-06") is None  # a different week
    assert dana.clear_service_shift(service="trash", week=_WEEK) is True
    assert dana.service_shift("trash", _WEEK) is None


def test_sweep_moves_service_chore_to_shifted_day(store, dana):
    """A holiday shift fires the reminder on the new day, with a 'moved' note."""
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    # Normally Tuesday (day 1) trash; this week a holiday moved it to Wednesday (2).
    dana.set_chore(title="take out trash", due_time="22:00", days="1", owner_id=dana_id,
                   service="trash", updated_by=dana_id)
    dana.set_service_shift(service="trash", week=_WEEK, shifted_weekday=2, reason="July 4th")

    # On the shifted day (Wed = REMIND_NOW) it fires, rephrased.
    client, sent = _capture_client()
    res = run_chores_check(dana, settings=UTC, now=REMIND_NOW, client=client)
    assert res and res[0]["stage"] == "reminder"
    assert [s["topic"] for s in sent] == ["dana-topic"]
    assert "moved to Wed this week" in sent[0]["message"] and "July 4th" in sent[0]["message"]


def test_sweep_service_chore_silent_on_normal_day_when_shifted(store, dana):
    """With the pickup moved to Wed, the normal Tue reminder does NOT fire."""
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    dana.set_chore(title="take out trash", due_time="22:00", days="1", owner_id=dana_id,
                   service="trash", updated_by=dana_id)
    dana.set_service_shift(service="trash", week=_WEEK, shifted_weekday=2, reason="July 4th")

    client, sent = _capture_client()
    res = run_chores_check(dana, settings=UTC, now=_TUE, client=client)  # Tuesday
    assert res == [] and sent == []  # moved off Tuesday → nothing on the old day


def test_sweep_service_chore_unaffected_without_a_shift(store, dana):
    """No shift stored → the service chore behaves exactly like a normal one."""
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    dana.set_chore(title="take out trash", due_time="22:00", days="2", owner_id=dana_id,
                   service="trash", updated_by=dana_id)  # normally Wed
    client, sent = _capture_client()
    res = run_chores_check(dana, settings=UTC, now=REMIND_NOW, client=client)  # Wed
    assert res and res[0]["stage"] == "reminder"
    assert "moved to" not in sent[0]["message"]  # plain reminder, no shift note


# --- HTTP + one-tap ----------------------------------------------------------


def test_chore_endpoints_crud_and_validation(client):
    r = client.post(
        "/household/chores",
        json={"title": "run the dishwasher", "due_time": "22:00", "days": [0, 2],
              "impact": "mornings"},
        headers=_h("dana-tok"),
    )
    assert r.status_code == 200
    cid = r.json()["id"]
    # Bad due time → 422.
    assert client.post(
        "/household/chores", json={"title": "x", "due_time": "nope"}, headers=_h("dana-tok")
    ).status_code == 422
    # Non-member owner → 422.
    assert client.post(
        "/household/chores",
        json={"title": "y", "due_time": "22:00", "owner_id": 9999},
        headers=_h("dana-tok"),
    ).status_code == 422
    # It shows on the shared sheet for the co-parent.
    sheet = client.get("/household/sheet", headers=_h("alex-tok")).json()
    assert any(c["title"] == "run the dishwasher" for c in sheet["sheet"]["chores"])
    # Mark done, then pause, then remove.
    assert client.post(f"/household/chores/{cid}/done", headers=_h("alex-tok")).json()["created"]
    assert client.post(
        f"/household/chores/{cid}/enabled", json={"enabled": False}, headers=_h("dana-tok")
    ).json()["enabled"] is False
    assert client.post(f"/household/chores/{cid}/remove", headers=_h("dana-tok")).json()["removed"]
    assert client.post(f"/household/chores/{cid}/done", headers=_h("dana-tok")).status_code == 404


def test_chore_endpoint_accepts_month_days(client, store):
    r = client.post(
        "/household/chores",
        json={"title": "pay allowance", "due_time": "18:00", "month_days": [1, 15]},
        headers=_h("dana-tok"),
    )
    assert r.status_code == 200
    sheet = client.get("/household/sheet", headers=_h("alex-tok")).json()
    chore = next(c for c in sheet["sheet"]["chores"] if c["title"] == "pay allowance")
    assert chore["month_days"] == "1,15"
    assert chore["effective_month_days"] == "1,15"


def test_one_tap_done_marks_chore_and_is_idempotent(client, store, dana):
    dana_id = store.get_user("dana")["id"]
    cid = dana.set_chore(title="dishes", due_time="22:00", owner_id=dana_id, updated_by=dana_id)
    token = sign_action("alex", "chore_done", cid, SIGNING)  # the partner taps Done
    r = client.get(f"/nudge/act?t={token}")
    assert r.status_code == 200 and "sorted for today" in r.text
    # Attributed to whoever tapped (Alex), and idempotent.
    today = local_datetime(utcnow(), Settings().timezone).strftime("%Y-%m-%d")
    assert cid in dana.chore_ids_done_on(today)
    assert client.get(f"/nudge/act?t={token}").status_code == 200  # re-tap is fine


def test_one_tap_done_on_missing_chore_is_graceful(client):
    token = sign_action("dana", "chore_done", 4242, SIGNING)
    r = client.get(f"/nudge/act?t={token}")
    assert r.status_code == 200 and "no longer on the list" in r.text


def test_chore_done_selector_marks_and_reads_a_past_day(client, store, dana):
    """The day selector can back-fill yesterday and read a past day's tick state."""
    did = store.get_user("dana")["id"]
    cid = dana.set_chore(title="dishes", due_time="22:00", owner_id=did, updated_by=did)
    tz = Settings().timezone
    today = local_datetime(utcnow(), tz).strftime("%Y-%m-%d")
    yday = (local_datetime(utcnow(), tz) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    # Default body (no days_ago) still logs today — back-compat with the ntfy tap.
    r = client.post(f"/household/chores/{cid}/done", headers=_h("dana-tok"))
    assert r.status_code == 200 and r.json()["done_on"] == today
    # Explicit days_ago=1 back-fills yesterday, attributed to the caller.
    r = client.post(f"/household/chores/{cid}/done", json={"days_ago": 1}, headers=_h("alex-tok"))
    assert r.status_code == 200 and r.json()["done_on"] == yday
    assert cid in dana.chore_ids_done_on(yday)

    # The read endpoint resolves the offset server-side (timezone-owned). "dishes"
    # runs every day, so it's in the scheduled set for both days too.
    assert client.get("/household/chores/done?days_ago=0", headers=_h("dana-tok")).json() == {
        "days_ago": 0, "done_on": today, "ids": [cid], "scheduled": [cid]}
    assert client.get("/household/chores/done?days_ago=1", headers=_h("alex-tok")).json() == {
        "days_ago": 1, "done_on": yday, "ids": [cid], "scheduled": [cid]}


def test_chore_done_endpoint_reports_the_days_scheduled_set(client, store, dana):
    """The read endpoint reports which chores run on the requested day (server-owned).

    Backs the card's "today's chores only" default for a past day: the selected
    day's scheduled set comes from the server, not a browser-timezone guess.
    """
    did = store.get_user("dana")["id"]
    tz = Settings().timezone
    now_local = local_datetime(utcnow(), tz)
    today_wd = str(now_local.weekday())
    yday_wd = str((now_local - datetime.timedelta(days=1)).weekday())
    every = dana.set_chore(title="dishes", due_time="", owner_id=did, updated_by=did)
    today_c = dana.set_chore(title="today only", due_time="", days=today_wd,
                             owner_id=did, updated_by=did)
    yday_c = dana.set_chore(title="yday only", due_time="", days=yday_wd,
                            owner_id=did, updated_by=did)

    d0 = client.get("/household/chores/done?days_ago=0", headers=_h("dana-tok")).json()
    assert set(d0["scheduled"]) == {every, today_c}
    d1 = client.get("/household/chores/done?days_ago=1", headers=_h("dana-tok")).json()
    assert set(d1["scheduled"]) == {every, yday_c}


def test_chore_undone_clears_a_day_and_is_idempotent(client, store, dana):
    """Un-ticking removes that day's completion; re-undoing is a harmless no-op."""
    did = store.get_user("dana")["id"]
    cid = dana.set_chore(title="dishes", due_time="22:00", owner_id=did, updated_by=did)
    client.post(f"/household/chores/{cid}/done", json={"days_ago": 1}, headers=_h("dana-tok"))
    tz = Settings().timezone
    yday = (local_datetime(utcnow(), tz) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    assert cid in dana.chore_ids_done_on(yday)

    r = client.post(f"/household/chores/{cid}/undone", json={"days_ago": 1}, headers=_h("dana-tok"))
    assert r.status_code == 200 and r.json()["removed"] is True
    assert cid not in dana.chore_ids_done_on(yday)
    # Idempotent: nothing left to remove.
    r = client.post(f"/household/chores/{cid}/undone", json={"days_ago": 1}, headers=_h("dana-tok"))
    assert r.status_code == 200 and r.json()["removed"] is False
    # A missing chore still 404s.
    assert client.post("/household/chores/9999/undone", headers=_h("dana-tok")).status_code == 404


def test_chore_day_selector_rejects_out_of_range(client, store, dana):
    """Only today and yesterday are addressable — further back is a 422, not a clamp."""
    did = store.get_user("dana")["id"]
    cid = dana.set_chore(title="dishes", due_time="22:00", owner_id=did, updated_by=did)
    assert client.post(
        f"/household/chores/{cid}/done", json={"days_ago": 2}, headers=_h("dana-tok")
    ).status_code == 422
    assert client.post(
        f"/household/chores/{cid}/undone", json={"days_ago": -1}, headers=_h("dana-tok")
    ).status_code == 422
    assert client.get(
        "/household/chores/done?days_ago=2", headers=_h("dana-tok")
    ).status_code == 422


def test_chores_check_endpoint(client, store, dana, monkeypatch):
    dana_id = store.get_user("dana")["id"]
    store.scoped(dana_id).set_state("ntfy_topic", "dana-topic")
    dana.set_chore(title="dishes", due_time="00:01", owner_id=dana_id, updated_by=dana_id)
    # Freeze the sweep's clock at a fixed mid-day so a 00:01-due chore is
    # unambiguously past due (a real `utcnow()` in the first minute of the local
    # day would leave it *not* yet due, silently emptying the sweep and passing
    # any weaker assertion for the wrong reason).
    frozen = datetime.datetime(2026, 7, 8, 12, 0, 0)
    monkeypatch.setattr(
        "prefrontal.webhooks.routers.household.utcnow", lambda: frozen
    )
    out = client.post("/webhooks/household/chores/check", headers=_h("dana-tok")).json()
    # The undone, past-due chore actually fired a miss (not merely "returned a list").
    missed = [s for s in out["sent"] if s.get("stage") == "missed"]
    assert [s["title"] for s in missed] == ["dishes"]


# --- routines: grouping + accountability + schedule inheritance --------------


def test_normalize_routine_clean_and_optional_time():
    clean, err = normalize_routine(
        {"title": " Monday pickup ", "accountable_id": "3", "days": [0], "due_time": "7:30"}
    )
    assert err is None
    assert clean == {"title": "Monday pickup", "accountable_id": 3, "days": "0",
                     "month_days": "", "due_time": "07:30", "impact": None, "enabled": True}
    # Blank time is allowed — a routine that just groups chores, no clock.
    untimed, err2 = normalize_routine({"title": "Tidy up", "due_time": ""})
    assert err2 is None and untimed["due_time"] == ""
    # A non-blank but malformed time is rejected.
    bad, err3 = normalize_routine({"title": "x", "due_time": "nope"})
    assert bad is None and "HH:MM" in err3


def test_effective_schedule_inherit_override_untimed():
    routine = {"days": "0,1", "due_time": "08:00"}
    # No own time → inherit the routine's schedule (days, month_days, due_time).
    assert effective_chore_schedule({"due_time": "", "days": ""}, routine) == ("0,1", "", "08:00")
    # Own time → full override (own days + time win).
    assert effective_chore_schedule(
        {"due_time": "09:15", "days": "2"}, routine
    ) == ("2", "", "09:15")
    # Standalone with no time → untimed.
    assert effective_chore_schedule({"due_time": "", "days": "3"}, None) == ("3", "", "")
    # with_effective_schedule copies the row with the resolved fields.
    merged = with_effective_schedule({"id": 1, "title": "t", "due_time": "", "days": ""}, routine)
    assert merged["due_time"] == "08:00" and merged["title"] == "t"


def test_effective_schedule_inherits_and_overrides_month_days():
    routine = {"days": "", "month_days": "1,15", "due_time": "08:00"}
    # No own time → inherit the routine's month schedule too.
    assert effective_chore_schedule({"due_time": "", "days": "", "month_days": ""}, routine) == (
        "", "1,15", "08:00",
    )
    # Own time → the chore's own month days win.
    assert effective_chore_schedule(
        {"due_time": "09:00", "days": "", "month_days": "5"}, routine
    ) == ("", "5", "09:00")
    merged = with_effective_schedule(
        {"id": 1, "due_time": "", "days": "", "month_days": ""}, routine
    )
    assert merged["month_days"] == "1,15"


def test_set_routine_upsert_and_accountability_counts(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    alex_id = store.get_user("alex")["id"]
    rid = dana.set_routine(title="Bedtime", accountable_id=dana_id, due_time="19:30",
                           updated_by=dana_id)
    dana.set_routine(title="Mornings", accountable_id=dana_id, updated_by=dana_id)
    dana.set_routine(title="Pickup", accountable_id=alex_id, updated_by=dana_id)
    routines = {r["title"]: r for r in dana.routines()}
    assert routines["Bedtime"]["accountable_name"] == "Dana"
    assert routines["Bedtime"]["due_time"] == "19:30"
    # Upsert on title (re-assign accountability), not a duplicate.
    dana.set_routine(title="Bedtime", accountable_id=alex_id, updated_by=dana_id)
    assert dana.routine(rid)["accountable_id"] == alex_id
    # Carrying facet: Dana holds Mornings; Alex holds Bedtime + Pickup.
    counts = {c["name"]: c["count"] for c in dana.accountability_counts()}
    assert counts == {"Dana": 1, "Alex": 2}


def test_remove_routine_unlinks_its_chores(store, dana):
    dana_id = store.get_user("dana")["id"]
    rid = dana.set_routine(title="Evening", due_time="20:00", updated_by=dana_id)
    cid = dana.set_chore(title="dishes", due_time="", routine_id=rid, updated_by=dana_id)
    assert dana.routines()[0]["chore_count"] == 1
    assert dana.remove_routine(rid) is True
    # The chore survives, now standing alone (routine_id cleared).
    survivor = next(c for c in dana.chores() if c["id"] == cid)
    assert survivor["routine_id"] is None and survivor["routine_title"] is None


def test_sweep_uses_routine_inherited_schedule(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    alex_id = store.get_user("alex")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    alex.set_state("ntfy_topic", "alex-topic")
    # Routine on Wednesday 22:00; the chore sets no time of its own → inherits it.
    rid = dana.set_routine(title="Evening", days="2", due_time="22:00", updated_by=dana_id)
    dana.set_chore(title="dishes", due_time="", routine_id=rid, owner_id=alex_id,
                   updated_by=dana_id)
    client, sent = _capture_client()
    result = run_chores_check(dana, settings=UTC, now=REMIND_NOW, client=client)
    # Inherited 22:00 due + default 30m lead → the 21:40 sweep reminds the owner.
    assert [s["topic"] for s in sent] == ["alex-topic"]
    assert result and result[0]["stage"] == "reminder"


def test_sweep_fires_a_month_scheduled_chore_only_on_its_day(store, dana):
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    # Due on the 1st at 22:00; REMIND_NOW (the 1st, 21:40) is inside the lead window.
    dana.set_chore(title="pay allowance", due_time="22:00", month_days="1", owner_id=dana_id,
                   updated_by=dana_id)
    client, sent = _capture_client()
    result = run_chores_check(dana, settings=UTC, now=REMIND_NOW, client=client)
    assert [s["topic"] for s in sent] == ["dana-topic"]
    assert result and result[0]["stage"] == "reminder"
    # On a different calendar day (the 2nd) nothing fires.
    client2, sent2 = _capture_client()
    day2 = REMIND_NOW.replace(day=2)
    assert run_chores_check(dana, settings=UTC, now=day2, client=client2) == []
    assert sent2 == []


def test_sweep_never_fires_an_untimed_chore(store, dana):
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    # No routine, no own time → untimed checklist chore: it has no clock to fire on.
    dana.set_chore(title="tidy entryway", due_time="", owner_id=dana_id, updated_by=dana_id)
    client, sent = _capture_client()
    assert run_chores_check(dana, settings=UTC, now=MISS_NOW, client=client) == []
    assert sent == []


def test_routine_endpoints_crud_and_chore_linkage(client, store):
    dana_id = store.get_user("dana")["id"]
    # Create a routine with Dana accountable.
    r = client.post(
        "/household/routines",
        json={"title": "Monday pickup prep", "accountable_id": dana_id, "due_time": "07:30",
              "days": [0]},
        headers=_h("dana-tok"),
    )
    assert r.status_code == 200
    rid = r.json()["id"]
    # A non-member can't be accountable.
    assert client.post(
        "/household/routines", json={"title": "x", "accountable_id": 9999},
        headers=_h("dana-tok"),
    ).status_code == 422
    # A chore can join the routine; an unknown routine_id is refused.
    ok = client.post(
        "/household/chores",
        json={"title": "pack laundry", "routine_id": rid},  # untimed, inherits routine
        headers=_h("dana-tok"),
    )
    assert ok.status_code == 200
    assert client.post(
        "/household/chores", json={"title": "y", "routine_id": 9999}, headers=_h("dana-tok"),
    ).status_code == 422
    # Pause + remove.
    assert client.post(f"/household/routines/{rid}/enabled", json={"enabled": False},
                       headers=_h("dana-tok")).json()["enabled"] is False
    assert client.post(f"/household/routines/{rid}/remove",
                       headers=_h("dana-tok")).json()["removed"]
    assert client.post(f"/household/routines/{rid}/enabled", json={"enabled": True},
                       headers=_h("dana-tok")).status_code == 404


def test_routine_update_endpoint_edits_and_renames(client, store):
    dana_id = store.get_user("dana")["id"]
    alex_id = store.get_user("alex")["id"]
    rid = client.post(
        "/household/routines",
        json={"title": "Morning prep", "accountable_id": dana_id, "due_time": "07:30",
              "days": [0, 1, 2]},
        headers=_h("dana-tok"),
    ).json()["id"]
    # A chore linked under it should survive the edit (and any rename).
    client.post(
        "/household/chores", json={"title": "pack bags", "routine_id": rid},
        headers=_h("dana-tok"),
    )

    # Edit in place: rename + reassign + retime + pause, all at once.
    r = client.post(
        f"/household/routines/{rid}/update",
        json={"title": "AM launch", "accountable_id": alex_id, "due_time": "08:00",
              "days": [0, 1, 2, 3, 4], "impact": "kids miss the bus", "enabled": False},
        headers=_h("dana-tok"),
    )
    assert r.status_code == 200 and r.json()["id"] == rid
    got = next(x for x in store.scoped(dana_id).routines() if x["id"] == rid)
    assert got["title"] == "AM launch"
    assert got["accountable_id"] == alex_id
    assert got["due_time"] == "08:00"
    assert got["enabled"] == 0
    assert got["chore_count"] == 1  # the linked chore stayed with the routine

    # Editing a missing routine → 404; a non-member accountable → 422.
    assert client.post("/household/routines/9999/update", json={"title": "x"},
                       headers=_h("dana-tok")).status_code == 404
    assert client.post(f"/household/routines/{rid}/update",
                       json={"title": "AM launch", "accountable_id": 9999},
                       headers=_h("dana-tok")).status_code == 422

    # Renaming onto another routine's name collides → 409.
    other = client.post("/household/routines", json={"title": "Bedtime"},
                        headers=_h("dana-tok")).json()["id"]
    assert client.post(f"/household/routines/{other}/update", json={"title": "AM launch"},
                       headers=_h("dana-tok")).status_code == 409


# --- routine completion: track, celebrate, highlight -------------------------


def test_routine_completion_pure():
    chores = [
        {"id": 1, "routine_id": 5, "enabled": True},
        {"id": 2, "routine_id": 5, "enabled": True},
        {"id": 3, "routine_id": 5, "enabled": False},   # disabled — ignored
        {"id": 4, "routine_id": 9, "enabled": True},     # a different routine
    ]
    assert routine_chore_status(5, chores, {1}) == (1, 2)
    assert not routine_is_complete(5, chores, {1})            # one still open
    assert routine_is_complete(5, chores, {1, 2})             # disabled #3 doesn't block
    assert not routine_is_complete(7, chores, {1, 2})         # routine with no chores
    assert routine_is_complete(5, chores, {1, 2, 3})          # extra done is fine


def test_routine_complete_message_names_the_routine_and_holder():
    msg = routine_complete_message({"title": "Bedtime", "accountable_name": "Dana"}, 3)
    assert "Bedtime" in msg and "Dana" in msg and "🎉" in msg
    # No accountable holder → warm teamwork line, still names the routine.
    assert "teamwork" in routine_complete_message({"title": "Mornings"}, 1)


def test_mark_routine_completed_cursor(store, dana):
    dana_id = store.get_user("dana")["id"]
    rid = dana.set_routine(title="Evening", updated_by=dana_id)
    assert dana.routine(rid)["last_completed_on"] is None
    dana.mark_routine_completed(rid, "2026-07-01")
    assert dana.routine(rid)["last_completed_on"] == "2026-07-01"


def test_finishing_last_chore_celebrates_routine_once(store, dana, alex):
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    alex.set_state("ntfy_topic", "alex-topic")
    rid = dana.set_routine(title="Bedtime", due_time="20:00", updated_by=dana_id)
    c1 = dana.set_chore(title="baths", due_time="", routine_id=rid, updated_by=dana_id)
    c2 = dana.set_chore(title="teeth", due_time="", routine_id=rid, updated_by=dana_id)
    client, sent = _capture_client()
    today = "2026-07-01"

    # First of two chores done → routine not complete → no celebration.
    r1 = log_chore_done_and_celebrate(
        dana, chore_id=c1, done_on=today, done_by=dana_id, settings=UTC, client=client)
    assert r1["routine_completed"] is None and sent == []

    # The last chore done → complete → both parents congratulated, once.
    r2 = log_chore_done_and_celebrate(
        dana, chore_id=c2, done_on=today, done_by=dana_id, settings=UTC, client=client)
    assert r2["routine_completed"]["title"] == "Bedtime"
    assert {s["topic"] for s in sent} == {"dana-topic", "alex-topic"}
    assert "Bedtime" in sent[0]["message"]

    # Re-tapping the same chore doesn't re-fire the celebration (per-day cursor).
    before = len(sent)
    r3 = log_chore_done_and_celebrate(
        dana, chore_id=c2, done_on=today, done_by=dana_id, settings=UTC, client=client)
    assert r3["routine_completed"] is None and len(sent) == before


def test_celebrate_ignores_disabled_chores_and_missing_household(store, dana):
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    rid = dana.set_routine(title="Tidy", due_time="18:00", updated_by=dana_id)
    live = dana.set_chore(title="counters", due_time="", routine_id=rid, updated_by=dana_id)
    skip = dana.set_chore(title="garage", due_time="", routine_id=rid, updated_by=dana_id)
    dana.set_chore_enabled(skip, False)  # only the live chore counts
    client, sent = _capture_client()
    r = log_chore_done_and_celebrate(
        dana, chore_id=live, done_on="2026-07-02", done_by=dana_id, settings=UTC, client=client)
    assert r["routine_completed"]["title"] == "Tidy"          # disabled chore didn't block
    # A chore not in this household → None (so the caller 404s), no crash.
    assert log_chore_done_and_celebrate(
        dana, chore_id=9999, done_on="2026-07-02", done_by=dana_id, settings=UTC, client=client
    ) is None


def test_standalone_chore_never_celebrates(store, dana):
    dana_id = store.get_user("dana")["id"]
    dana.set_state("ntfy_topic", "dana-topic")
    cid = dana.set_chore(title="bins", due_time="22:00", updated_by=dana_id)  # no routine
    client, sent = _capture_client()
    r = log_chore_done_and_celebrate(
        dana, chore_id=cid, done_on="2026-07-02", done_by=dana_id, settings=UTC, client=client)
    assert r["routine_completed"] is None and sent == []


def test_sheet_flags_and_renders_completed_routine(store, dana):
    dana_id = store.get_user("dana")["id"]
    rid = dana.set_routine(title="Bedtime", due_time="20:00", updated_by=dana_id)
    cid = dana.set_chore(title="teeth", due_time="", routine_id=rid, updated_by=dana_id)
    today = REMIND_NOW.strftime("%Y-%m-%d")

    sheet = build_sheet(dana, now=REMIND_NOW, timezone="UTC")
    r = next(x for x in sheet.routines if x["id"] == rid)
    assert r["done_today"] is False and r["chores_total"] == 1 and r["chores_done"] == 0

    dana.log_chore_done(chore_id=cid, done_on=today, done_by=dana_id)
    sheet2 = build_sheet(dana, now=REMIND_NOW, timezone="UTC")
    r2 = next(x for x in sheet2.routines if x["id"] == rid)
    assert r2["done_today"] is True and r2["chores_done"] == 1
    assert "all done today" in render_sheet(sheet2)


def test_done_endpoint_reports_routine_completion(client, store, dana):
    dana_id = store.get_user("dana")["id"]
    rid = dana.set_routine(title="Bedtime", due_time="20:00", updated_by=dana_id)
    cid = dana.set_chore(title="teeth", due_time="", routine_id=rid, updated_by=dana_id)
    r = client.post(f"/household/chores/{cid}/done", headers=_h("dana-tok")).json()
    assert r["routine_completed"] and r["routine_completed"]["title"] == "Bedtime"
