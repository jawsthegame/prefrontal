"""Tests for the Impulsivity module — capture-and-defer.

Covers the pure title helpers (heuristic + LLM-with-fallback) and the
``POST /webhooks/impulse/capture`` endpoint that parks an impulse as a
``source='impulse'`` todo and reads back a speakable confirmation.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.impulsivity import (
    DEFAULT_PAUSE_SECONDS,
    build_pause_message,
    heuristic_capture_title,
    infer_capture_title,
    pause_seconds,
    switch_rate_feedback,
    switch_response,
)
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "impulse-secret"


def _utc_minutes_ago(minutes: float) -> str:
    """A SQLite-style UTC timestamp `minutes` in the past (for started_at)."""
    return (datetime.utcnow() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _offline_ollama() -> OllamaClient:
    """An OllamaClient whose every call fails — forces the heuristic path."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no server in tests")

    return OllamaClient(transport=httpx.MockTransport(refuse))


def _ollama_replying(text: str) -> OllamaClient:
    """An OllamaClient whose generate() returns `text` (for the LLM path)."""
    return OllamaClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"response": text})
    ))


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    try:
        yield scoped_default(MemoryStore(conn))
    finally:
        conn.close()


@pytest.fixture()
def client(store):
    """A TestClient with auth on and Ollama offline (deterministic heuristic titling)."""
    app = create_app(
        store=store,
        settings=Settings(webhook_secret=SECRET),
        ollama=_offline_ollama(),
    )
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Prefrontal-Token": SECRET}


# -- pure title helpers ------------------------------------------------------


def test_heuristic_title_truncates_and_capitalizes():
    long = "reorganize the entire font folder and rename every single weight"
    title = heuristic_capture_title(long)
    assert title.startswith("Reorganize the entire font folder")
    assert title.endswith("…")  # truncated past the word cap
    assert len(title.split()) <= 9  # 8 words + the ellipsis token


def test_heuristic_title_blank_is_safe():
    assert heuristic_capture_title("   ") == "Captured impulse"


def test_infer_title_uses_llm_when_available():
    title = infer_capture_title(
        "ooh the thing with the fonts", client=_ollama_replying("Reorganize font folder")
    )
    assert title == "Reorganize font folder"


def test_infer_title_strips_quotes_and_overlong_llm_reply():
    """A chatty model reply is cleaned: quotes stripped, word count capped."""
    title = infer_capture_title(
        "x", client=_ollama_replying('"call the dentist and also book the car and the vet today"')
    )
    assert '"' not in title
    assert len(title.split()) <= 8


def test_infer_title_falls_back_when_model_down():
    title = infer_capture_title("call the plumber", client=_offline_ollama())
    assert title == "Call the plumber"  # heuristic fallback


def test_infer_title_blank_is_none():
    assert infer_capture_title("   ", client=None) is None


# -- HTTP: capture-and-defer -------------------------------------------------


def test_capture_creates_impulse_todo(client, store):
    """A captured impulse becomes an open todo flagged source='impulse'."""
    resp = client.post(
        "/webhooks/impulse/capture",
        json={"impulse_text": "reorganize the reference folder"},
        headers=_auth(),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["raw"] == "reorganize the reference folder"
    # It lands in the normal open-loop list, marked as an impulse.
    todo = store.get_todo(body["todo_id"])
    assert todo["status"] == "open"
    assert todo["source"] == "impulse"
    assert todo["notes"] == "reorganize the reference folder"  # raw text preserved


def test_capture_confirmation_is_speakable(client):
    """The read-back names the parked item so a Shortcut shows it verbatim."""
    body = client.post(
        "/webhooks/impulse/capture",
        json={"impulse_text": "email Dana about the venue"},
        headers=_auth(),
    ).json()
    conf = body["confirmation"]
    assert "Parked" in conf
    assert body["title"] in conf
    assert "safe" in conf


def test_capture_rejects_blank(client):
    resp = client.post(
        "/webhooks/impulse/capture", json={"impulse_text": "   "}, headers=_auth()
    )
    assert resp.status_code == 422


def test_capture_requires_auth(client):
    assert client.post(
        "/webhooks/impulse/capture", json={"impulse_text": "x"}
    ).status_code == 401


# -- pure reflective-pause core ----------------------------------------------


def test_pause_is_longer_early_in_a_block():
    """A switch early in a planned block pauses longer than one near the end."""
    early = pause_seconds(5, 90, base_seconds=20)
    late = pause_seconds(90, 90, base_seconds=20)
    assert early > late
    assert late == pytest.approx(20)  # at plan end → base


def test_pause_bounds_and_no_plan():
    """Bounded to [base, 3*base]; a windowless block is a flat base pause."""
    assert pause_seconds(0, 60, base_seconds=20) == pytest.approx(60)  # 3x at the start
    assert pause_seconds(999, 60, base_seconds=20) == pytest.approx(20)  # clamped past plan
    assert pause_seconds(40, None, base_seconds=20) == 20  # no plan → flat base
    assert pause_seconds(40, 0, base_seconds=20) == 20


def test_switch_response_normalizes_and_rejects():
    assert switch_response(" Defer ") == "defer"
    for bad in ("", "nope", None):
        with pytest.raises(ValueError):
            switch_response(bad)


def test_pause_message_names_task_and_time():
    msg = build_pause_message("writing the Q3 report", 20, name="Tom")
    assert "writing the Q3 report" in msg
    assert "20 min" in msg
    assert "Tom" in msg


# -- pure switch-rate feedback -----------------------------------------------


def test_switch_rate_feedback_none_without_signal():
    assert switch_rate_feedback([]) is None  # no sessions
    # Sessions but zero impulses signalled → nothing to reflect.
    assert switch_rate_feedback([{"switch_impulses": 0, "switches_deferred": 0}]) is None


def test_switch_rate_feedback_sums_across_sessions():
    line = switch_rate_feedback(
        [
            {"switch_impulses": 4, "switches_deferred": 3},
            {"switch_impulses": 2, "switches_deferred": 1},
        ]
    )
    assert line == "6 switch-impulses, 4 deferred"


def test_switch_rate_feedback_singular_and_baseline():
    assert switch_rate_feedback([{"switch_impulses": 1, "switches_deferred": 0}]) == (
        "1 switch-impulse, 0 deferred"
    )
    line = switch_rate_feedback(
        [{"switch_impulses": 6, "switches_deferred": 4}], baseline=1.4
    )
    assert line == "6 switch-impulses, 4 deferred (you usually honor ~1.4/session)"


# -- HTTP: reflective-pause loop (switch / resolve) --------------------------


def test_switch_records_impulse_without_changing_status(client, store):
    """/focus/switch counts the impulse and returns a pause, but doesn't close it."""
    sid = store.start_focus_session(
        "the API refactor", planned_minutes=90, started_at=_utc_minutes_ago(20)
    )
    body = client.post("/webhooks/focus/switch", json={}, headers=_auth()).json()
    assert body["session_id"] == sid
    assert body["intended_task"] == "the API refactor"
    assert body["pause_seconds"] > DEFAULT_PAUSE_SECONDS  # 20/90 in → scaled up
    assert body["options"] == ["return", "defer", "switch"]
    session = store.get_focus_session(sid)
    assert session["status"] == "active"  # no state change beyond the counter
    assert session["switch_impulses"] == 1
    assert session["switches_deferred"] == 0


def test_switch_with_no_active_session_is_409(client):
    resp = client.post("/webhooks/focus/switch", json={}, headers=_auth())
    assert resp.status_code == 409


def test_resolve_return_keeps_block_active(client, store):
    sid = store.start_focus_session("deep work", started_at=_utc_minutes_ago(10))
    client.post("/webhooks/focus/switch", json={}, headers=_auth())
    body = client.post(
        "/webhooks/focus/resolve", json={"action": "return"}, headers=_auth()
    ).json()
    assert body["session_status"] == "active"
    assert body["todo_id"] is None
    assert "back to" in body["confirmation"].lower()
    assert store.get_focus_session(sid)["status"] == "active"


def test_resolve_defer_parks_impulse_and_stays(client, store):
    """defer creates a source='impulse' todo, counts the deferral, keeps the block."""
    sid = store.start_focus_session("deep work", started_at=_utc_minutes_ago(10))
    client.post("/webhooks/focus/switch", json={}, headers=_auth())
    body = client.post(
        "/webhooks/focus/resolve",
        json={"action": "defer", "impulse_text": "reorganize the font folder"},
        headers=_auth(),
    ).json()
    assert body["session_status"] == "active"
    todo = store.get_todo(body["todo_id"])
    assert todo["source"] == "impulse"
    assert todo["notes"] == "reorganize the font folder"
    session = store.get_focus_session(sid)
    assert session["switch_impulses"] == 1  # counted once, by /switch
    assert session["switches_deferred"] == 1


def test_resolve_defer_without_text_is_422(client, store):
    store.start_focus_session("deep work", started_at=_utc_minutes_ago(10))
    client.post("/webhooks/focus/switch", json={}, headers=_auth())
    resp = client.post(
        "/webhooks/focus/resolve", json={"action": "defer"}, headers=_auth()
    )
    assert resp.status_code == 422


def test_resolve_switch_closes_block_and_logs_episode(client, store):
    sid = store.start_focus_session(
        "deep work", planned_minutes=60, started_at=_utc_minutes_ago(30)
    )
    client.post("/webhooks/focus/switch", json={}, headers=_auth())
    body = client.post(
        "/webhooks/focus/resolve", json={"action": "switch"}, headers=_auth()
    ).json()
    assert body["session_status"] == "switched"
    assert store.get_focus_session(sid)["status"] == "switched"
    # Close logs two episodes: the `task` (drift) and the per-session `switch`
    # (context_switch learning). Find each by type rather than by order.
    episodes = store.recent_episodes(5)
    task_ep = next(e for e in episodes if e["episode_type"] == "task")
    assert task_ep["outcome"] == "partial"  # real block, cut short by a deliberate switch
    assert "switched" in task_ep["context"]
    # A deliberate switch stopped by choice isn't an estimation-accuracy signal, so
    # the truncated duration is NOT recorded as actual_value (it can't feed the
    # time-estimation bias); the minutes spent are kept in notes for provenance.
    assert task_ep["actual_value"] is None
    assert task_ep["notes"] and "before switching" in task_ep["notes"]
    switch_ep = next(e for e in episodes if e["episode_type"] == "switch")
    assert switch_ep["predicted_value"] == 1.0  # one switch-impulse signalled
    assert switch_ep["actual_value"] == 0.0  # not deferred (honored)


def test_retract_switched_estimates_only_touches_switched_blocks(store):
    """The backfill nulls actual_value on old switched blocks, leaving completed
    focus blocks (a real estimation signal) alone; dry-run writes nothing."""
    from prefrontal.modules.hyperfocus import retract_switched_estimates

    # Simulate legacy data: a switched block that (wrongly) recorded its duration.
    switched = store.log_episode(
        "task", predicted_value=60.0, actual_value=8.0,
        context="focus switched: deep work", outcome="partial",
    )
    # A completed block — a genuine estimate signal that must be preserved.
    completed = store.log_episode(
        "task", predicted_value=30.0, actual_value=32.0,
        context="focus: emails", outcome="success",
    )

    dry = retract_switched_estimates(store, apply=False)
    assert dry == {"scanned": 1, "cleared": 1, "samples": ["focus switched: deep work"]}
    assert store.get_episode(switched)["actual_value"] == 8.0  # dry run wrote nothing

    applied = retract_switched_estimates(store, apply=True)
    assert applied["scanned"] == 1 and applied["cleared"] == 1
    assert store.get_episode(switched)["actual_value"] is None
    assert store.get_episode(completed)["actual_value"] == 32.0  # untouched

    # Idempotent — the cleared row no longer matches.
    again = retract_switched_estimates(store, apply=True)
    assert again == {"scanned": 1, "cleared": 0, "samples": []}


def test_resolve_bad_action_is_422(client, store):
    store.start_focus_session("deep work", started_at=_utc_minutes_ago(10))
    resp = client.post(
        "/webhooks/focus/resolve", json={"action": "teleport"}, headers=_auth()
    )
    assert resp.status_code == 422
