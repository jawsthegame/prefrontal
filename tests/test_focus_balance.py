"""Tests for the focus-balance lens over closed-loop trips.

Covers the pure domain vocabulary/normalizer and per-trip domain resolution, the
rollup (:func:`prefrontal.focus_balance.build_focus_balance`) including target
scaling / underserved detection / the time window, the phrasings, the trip-store
domain methods, the trip-tracking module's weekly nudge, the Parent-pack seeding,
and the HTTP surface (``/webhooks/trip/{label,domain}``, ``/balance``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.clock import TS_FMT
from prefrontal.config import Settings
from prefrontal.focus_balance import (
    FOCUS_DOMAINS,
    build_focus_balance,
    domain_of_trip,
    format_minutes,
    normalize_focus_domain,
    nudge_enabled,
    read_targets,
    underserved_nudge_text,
)
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.registry import get as get_module
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

HOME = (40.0000, -73.0000)
SECRET = "balance-secret"


def _minutes_ago(minutes: float) -> str:
    return (datetime.utcnow() - timedelta(minutes=minutes)).strftime(TS_FMT)


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


def _trip(store, minutes, *, domain=None, category=None, label="outing"):
    """Create a completed trip that lasted ~``minutes`` and returned just now."""
    tid = store.open_trip(departed_at=_minutes_ago(minutes))
    store.close_trip(tid)
    store.label_trip(tid, label=label, category=category, domain=domain)
    return tid


# -- normalization -----------------------------------------------------------


def test_normalize_canonical_and_blank():
    assert normalize_focus_domain("shop") == "shop"
    assert normalize_focus_domain("  WORK ") == "work"
    assert normalize_focus_domain("") is None
    assert normalize_focus_domain(None) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("kids", "kids"),
        ("kid", "kids"),
        ("childcare", "kids"),
        ("family", "kids"),
        ("house", "home"),
        ("household", "home"),
        ("chores", "home"),
        ("business", "shop"),
        ("store", "shop"),
        ("office", "work"),
        ("health", "personal"),
        ("leisure", "personal"),
    ],
)
def test_normalize_synonyms(raw, expected):
    assert normalize_focus_domain(raw) == expected


def test_normalize_free_text_passthrough():
    """A genuinely novel domain is kept (lower-cased), not rejected."""
    assert normalize_focus_domain("Volunteering") == "volunteering"


# -- per-trip domain resolution ----------------------------------------------


def test_domain_of_trip_explicit_wins_over_category():
    assert domain_of_trip({"domain": "shop", "category": "work"}) == "shop"


def test_domain_of_trip_category_fallback():
    assert domain_of_trip({"category": "work"}) == "work"
    assert domain_of_trip({"category": "family"}) == "kids"
    assert domain_of_trip({"category": "health"}) == "personal"


def test_domain_of_trip_ambiguous_is_none():
    """errand/other are genuinely ambiguous, so they're not guessed into a sphere."""
    assert domain_of_trip({"category": "errand"}) is None
    assert domain_of_trip({}) is None


# -- rollup ------------------------------------------------------------------


def test_build_focus_balance_buckets_minutes(store):
    _trip(store, 60, domain="shop")
    _trip(store, 30, domain="shop")
    _trip(store, 45, domain="home")
    _trip(store, 20, category="work")  # inferred → work

    balance = build_focus_balance(store)
    by = {d.domain: d for d in balance.domains}
    assert round(by["shop"].minutes) == 90
    assert by["shop"].trips == 2
    assert round(by["home"].minutes) == 45
    assert round(by["work"].minutes) == 20
    assert round(balance.total_minutes) == 155
    # Sorted biggest first.
    assert balance.domains[0].domain == "shop"


def test_unassigned_bucket_for_uncategorized(store):
    _trip(store, 40)  # no domain, no category
    balance = build_focus_balance(store)
    assert any(d.domain == "unassigned" and round(d.minutes) == 40 for d in balance.domains)


def test_window_excludes_out_of_range_trips(store):
    _trip(store, 30, domain="home")
    # A "now" a fortnight in the future puts the just-returned trip outside a 7d look-back.
    future = datetime.utcnow() + timedelta(days=14)
    balance = build_focus_balance(store, now=future)
    assert not balance.has_data


def test_targets_scaled_to_window_and_underserved(store):
    # 60 min of home time; weekly aim 300 → over a 7d window, 60 < 150 (half) ⇒ underserved.
    _trip(store, 60, domain="home")
    store.set_state("focus_target:home", "300")
    balance = build_focus_balance(store)
    home = next(d for d in balance.domains if d.domain == "home")
    assert home.target_minutes == 300  # 7d window == full weekly aim
    assert home.underserved is True
    assert [d.domain for d in balance.underserved()] == ["home"]


def test_target_domain_with_no_trips_still_appears(store):
    """A wholly-neglected targeted sphere shows up at zero, not silently gone."""
    _trip(store, 30, domain="shop")
    store.set_state("focus_target:personal", "120")
    balance = build_focus_balance(store)
    personal = next((d for d in balance.domains if d.domain == "personal"), None)
    assert personal is not None
    assert personal.minutes == 0
    assert personal.underserved is True


def test_half_days_scales_target(store):
    _trip(store, 100, domain="home")
    store.set_state("focus_target:home", "140")  # weekly
    balance = build_focus_balance(store, days=7)  # full week: aim 140
    home7 = next(d for d in balance.domains if d.domain == "home")
    assert home7.target_minutes == 140
    balance_short = build_focus_balance(store, days=1)  # 1/7 of the week: aim 20
    home1 = next(d for d in balance_short.domains if d.domain == "home")
    assert home1.target_minutes == pytest.approx(20.0, abs=0.1)


# -- phrasing ----------------------------------------------------------------


@pytest.mark.parametrize(
    "minutes,expected",
    [(0, "0m"), (40, "40m"), (60, "1h"), (90, "1h30"), (370, "6h10")],
)
def test_format_minutes(minutes, expected):
    assert format_minutes(minutes) == expected


def test_summary_and_nudge_text(store):
    _trip(store, 30, domain="home")
    store.set_state("focus_target:home", "300")
    balance = build_focus_balance(store)
    text = underserved_nudge_text(balance)
    assert text is not None
    assert "home" in text and "light" in text.lower()


def test_nudge_text_none_when_balanced(store):
    _trip(store, 400, domain="home")
    store.set_state("focus_target:home", "300")
    assert underserved_nudge_text(build_focus_balance(store)) is None


# -- state helpers -----------------------------------------------------------


def test_read_targets_skips_garbage_and_nonpositive(store):
    store.set_state("focus_target:home", "300")
    store.set_state("focus_target:work", "0")       # "don't track"
    store.set_state("focus_target:shop", "notanum")
    assert read_targets(store) == {"home": 300.0}


def test_nudge_enabled_flag(store):
    assert nudge_enabled(store) is False
    store.set_state("focus_balance_nudge", "1")
    assert nudge_enabled(store) is True


# -- store methods -----------------------------------------------------------


def test_label_trip_sets_domain_and_set_trip_domain(store):
    tid = _trip(store, 20, category="errand")
    assert store.get_trip(tid)["domain"] is None
    store.set_trip_domain(tid, "shop")
    assert store.get_trip(tid)["domain"] == "shop"
    # Clearing works.
    store.set_trip_domain(tid, None)
    assert store.get_trip(tid)["domain"] is None


def test_completed_trips_since_filters(store):
    _trip(store, 30, domain="home")
    since = (datetime.utcnow() - timedelta(days=1)).strftime(TS_FMT)
    rows = store.completed_trips_since(since)
    assert len(rows) == 1
    # A since in the future returns nothing.
    future = (datetime.utcnow() + timedelta(days=1)).strftime(TS_FMT)
    assert store.completed_trips_since(future) == []


# -- module nudge ------------------------------------------------------------


def test_module_emits_focus_balance_cue_when_enabled(store):
    from prefrontal.coaching import CoachContext

    store.set_home(*HOME)
    _trip(store, 30, domain="home", label="quick errand")
    store.set_state("focus_target:home", "300")
    store.set_state("focus_balance_nudge", "1")

    module = get_module("trip_tracking")
    ctx = CoachContext(now=datetime.utcnow())
    cues = module.evaluate(store, ctx)
    balance_cues = [c for c in cues if c.intervention == "focus_balance"]
    assert len(balance_cues) == 1
    assert balance_cues[0].urgency == "ambient"
    assert balance_cues[0].context_key == "focus_balance"


def test_module_silent_when_nudge_disabled(store):
    from prefrontal.coaching import CoachContext

    store.set_home(*HOME)
    _trip(store, 30, domain="home")
    store.set_state("focus_target:home", "300")
    # No focus_balance_nudge flag.
    cues = get_module("trip_tracking").evaluate(store, CoachContext(now=datetime.utcnow()))
    assert [c for c in cues if c.intervention == "focus_balance"] == []


def test_module_silent_when_balanced(store):
    from prefrontal.coaching import CoachContext

    store.set_home(*HOME)
    _trip(store, 400, domain="home")
    store.set_state("focus_target:home", "300")
    store.set_state("focus_balance_nudge", "1")
    cues = get_module("trip_tracking").evaluate(store, CoachContext(now=datetime.utcnow()))
    assert [c for c in cues if c.intervention == "focus_balance"] == []


def test_focus_balance_cue_dedups_weekly(store):
    from prefrontal.coaching import CoachContext

    store.set_home(*HOME)
    _trip(store, 30, domain="home")
    store.set_state("focus_target:home", "300")
    store.set_state("focus_balance_nudge", "1")
    module = get_module("trip_tracking")
    # Two ticks in the same ISO week share a dedup key.
    now = datetime(2026, 7, 4, 9, 0, 0)
    a = module.evaluate(store, CoachContext(now=now))
    b = module.evaluate(store, CoachContext(now=now + timedelta(hours=3)))
    ka = next(c.dedup_key for c in a if c.intervention == "focus_balance")
    kb = next(c.dedup_key for c in b if c.intervention == "focus_balance")
    assert ka == kb


# -- parent pack -------------------------------------------------------------


def test_parent_pack_seeds_targets_and_flag(store):
    from prefrontal.packs import get as get_pack

    get_pack("parent").seed(store)
    assert nudge_enabled(store) is True
    targets = read_targets(store)
    assert targets.get("kids") == 300.0
    assert targets.get("home") == 120.0
    assert targets.get("personal") == 120.0


# -- endpoints ---------------------------------------------------------------


@pytest.fixture()
def client(store):
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


def test_label_endpoint_accepts_domain(client, store):
    tid = _trip(store, 20)
    resp = client.post(
        "/webhooks/trip/label",
        json={"trip_id": tid, "label": "Costco", "category": "errand", "domain": "kids"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["domain"] == "kids"  # canonical
    assert body["category"] == "errand"


def test_domain_endpoint_sets_and_clears(client, store):
    tid = _trip(store, 20, category="errand")
    assert client.post(
        "/webhooks/trip/domain", json={"trip_id": tid, "domain": "business"}, headers=_auth()
    ).json()["domain"] == "shop"
    assert client.post(
        "/webhooks/trip/domain", json={"trip_id": tid, "domain": None}, headers=_auth()
    ).json()["domain"] is None


def test_domain_endpoint_404(client):
    resp = client.post(
        "/webhooks/trip/domain", json={"trip_id": 9999, "domain": "shop"}, headers=_auth()
    )
    assert resp.status_code == 404


def test_balance_endpoint(client, store):
    _trip(store, 60, domain="shop")
    _trip(store, 30, domain="home")
    resp = client.get("/balance", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == 7
    assert round(body["total_minutes"]) == 90
    domains = {d["domain"]: d for d in body["domains"]}
    assert round(domains["shop"]["minutes"]) == 60
    assert body["summary"] and "shop" in body["summary"]


def test_trips_list_includes_domains_vocab(client):
    resp = client.get("/trips", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["domains"] == list(FOCUS_DOMAINS)
