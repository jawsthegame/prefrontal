"""Tests for the FastAPI webhook listener.

Uses FastAPI's TestClient with an injected in-memory MemoryStore. The store is
multi-tenant: a single user is provisioned and its per-user token is sent in the
``X-Prefrontal-Token`` header, so we exercise token->user resolution and the
shortcut -> episode mapping end to end without touching disk or the network.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.webhooks.app import create_app

#: The per-user token the fixtures authenticate with. A fixed value is fine in
#: tests (provision_user accepts an explicit token); the auth layer stores only
#: its sha256 and resolves the request to the provisioned user.
SECRET = "test-secret"


@pytest.fixture()
def store():
    """An in-memory store with one provisioned user, kept open for the test.

    The unscoped store is handed to ``create_app``; the app's auth layer scopes
    each request to the user resolved from the ``X-Prefrontal-Token`` header.
    """
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(
        unscoped, "tester", display_name="Tester", token=SECRET, is_operator=True
    )
    try:
        yield unscoped
    finally:
        conn.close()


@pytest.fixture()
def user_store(store):
    """The same store scoped to the provisioned user, for direct per-user calls.

    The app scopes requests itself; this is for assertions/seeding in the test
    body that touch per-user tables directly (which require a bound user).
    """
    return store.scoped(store.get_user("tester")["id"])


@pytest.fixture()
def client(store):
    """A TestClient wired to the injected store; requests carry the user token."""
    settings = Settings()
    app = create_app(store=store, settings=settings)
    with TestClient(app) as c:
        yield c


def test_health_needs_no_auth(client):
    """The health probe responds 200 without a token."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_profile_requires_auth(client):
    """The profile endpoint is auth-guarded."""
    assert client.get("/profile").status_code == 401


def test_app_icon_served_public_as_png(client):
    """The ntfy notification icon is served unauthenticated as a PNG."""
    resp = client.get("/brand/app-icon.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic — a real image


def test_todos_expose_account_and_label_map(store, user_store):
    """/todos tags each todo with its mail account and echoes the label map."""
    from prefrontal.mail.ingest import ingest_messages

    # A mail-created todo (linked to the "work" inbox) and a plain manual one.
    ingest_messages(
        user_store,
        [{
            "message_id": "<m-1@corp>",
            "from": '"Sarah Lee" <sarah@corp.com>',
            "subject": "Quick question about the report",
            "date": "2026-06-28T10:30:00-07:00",
            "body": "Can you send me the Q2 report?",
        }],
        account="work",
        use_model=False,
    )
    manual_id = user_store.add_todo("buy milk")

    settings = Settings(account_labels=(("work", "Acme", "orange"),))
    app = create_app(store=store, settings=settings)
    with TestClient(app) as c:
        resp = c.get("/todos", headers={"X-Prefrontal-Token": SECRET})
    assert resp.status_code == 200
    data = resp.json()
    assert data["accounts"] == {"work": {"label": "Acme", "color": "orange"}}
    by_id = {t["id"]: t for t in data["todos"]}
    assert by_id[manual_id]["account"] is None
    mail_todo = next(t for t in data["todos"] if t["id"] != manual_id)
    assert mail_todo["account"] == "work"


def test_todos_stuck_surfaces_body_double(client, user_store):
    """GET /todos/stuck lists a repeat-bailed task with a first step + suggestion."""
    for _ in range(2):
        user_store.log_episode(
            "task", outcome="miss", context="todo dropped: Call the dentist"
        )
    resp = client.get("/todos/stuck", headers={"X-Prefrontal-Token": SECRET})
    assert resp.status_code == 200
    stuck = resp.json()["stuck"]
    assert len(stuck) == 1
    item = stuck[0]
    assert item["title"] == "Call the dentist" and item["misses"] == 2
    assert item["first_step"]  # a concrete tiny first action
    assert "start-together" in item["suggestion"]


def test_todos_stuck_requires_auth(client):
    """The stuck-tasks endpoint is auth-guarded."""
    assert client.get("/todos/stuck").status_code == 401


def test_todos_deep_link_to_gmail_source(store, user_store):
    """A todo from a Gmail inbox carries a source_url deep link; others don't."""
    from prefrontal.mail.ingest import ingest_messages

    ingest_messages(
        user_store,
        [{
            "message_id": "<m-9@corp>",
            "from": '"Sarah Lee" <sarah@corp.com>',
            "subject": "Quick question about the report",
            "date": "2026-06-28T10:30:00-07:00",
            "body": "Can you send me the Q2 report?",
        }],
        account="work",
        use_model=False,
    )
    manual_id = user_store.add_todo("buy milk")

    # "work" configured as a Gmail inbox -> its todo deep-links to the message.
    settings = Settings(gmail_accounts=frozenset({"work"}))
    app = create_app(store=store, settings=settings)
    with TestClient(app) as c:
        data = c.get("/todos", headers={"X-Prefrontal-Token": SECRET}).json()
    by_id = {t["id"]: t for t in data["todos"]}
    mail_todo = next(t for t in data["todos"] if t["id"] != manual_id)
    assert mail_todo["source_url"] == (
        "https://mail.google.com/mail/u/0/#search/rfc822msgid:m-9%40corp"
    )
    # Manual todos never have a mail source.
    assert by_id[manual_id]["source_url"] is None

    # With no Gmail account configured, the same mail todo gets no deep link.
    settings = Settings()
    app = create_app(store=store, settings=settings)
    with TestClient(app) as c:
        data = c.get("/todos", headers={"X-Prefrontal-Token": SECRET}).json()
    mail_todo = next(t for t in data["todos"] if t["id"] != manual_id)
    assert mail_todo["source_url"] is None


def test_profile_returns_markdown(client):
    """With no narrative cached, /profile falls back to the structured profile."""
    resp = client.get("/profile", headers={"X-Prefrontal-Token": SECRET})
    assert resp.status_code == 200
    assert "Behavioral profile" in resp.text
    assert resp.headers["X-Profile-Source"] == "uncached"


def test_profile_serves_cached_narrative(client, user_store):
    """Once a narrative is cached, /profile serves the prose, not the structure."""
    from prefrontal.memory.summarizer import build_profile

    user_store.set_profile_cache(
        "Lead with departures: pad estimates 1.4x.",
        source="llm",
        model="qwen2.5:14b",
        structured=build_profile(user_store),  # matches current facts → not stale
    )
    resp = client.get("/profile", headers={"X-Prefrontal-Token": SECRET})
    assert resp.status_code == 200
    assert resp.text == "Lead with departures: pad estimates 1.4x."
    assert resp.headers["X-Profile-Source"] == "llm"
    assert resp.headers["X-Profile-Model"] == "qwen2.5:14b"
    assert resp.headers["X-Profile-Generated-At"]
    assert resp.headers["X-Profile-Stale"] == "false"


def test_profile_format_structured_bypasses_cache(client, user_store):
    """?format=structured returns the raw profile even when a narrative is cached."""
    user_store.set_profile_cache(
        "cached prose", source="llm", model="m", structured="# Behavioral profile\n"
    )
    resp = client.get(
        "/profile?format=structured", headers={"X-Prefrontal-Token": SECRET}
    )
    assert resp.status_code == 200
    assert "Behavioral profile" in resp.text
    assert "cached prose" not in resp.text
    assert resp.headers["X-Profile-Source"] == "structured"


def test_profile_stale_header_tracks_facts(client, user_store):
    """Changing an underlying fact flips X-Profile-Stale to true."""
    user_store.set_profile_cache(
        "prose", source="llm", model="m", structured="# Behavioral profile\nstale"
    )
    resp = client.get("/profile", headers={"X-Prefrontal-Token": SECRET})
    assert resp.headers["X-Profile-Stale"] == "true"  # structured no longer matches


def _mock_ollama(reply: str) -> OllamaClient:
    """An OllamaClient whose /api/generate always returns `reply`."""
    return OllamaClient(
        model="mock-model",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"response": reply})
        ),
    )


def test_profile_refresh_generates_and_caches(store, user_store):
    """?refresh=1 regenerates the narrative via Ollama and persists it."""
    settings = Settings()
    app = create_app(store=store, settings=settings, ollama=_mock_ollama("fresh prose"))
    with TestClient(app) as c:
        resp = c.get("/profile?refresh=1", headers={"X-Prefrontal-Token": SECRET})
        assert resp.status_code == 200
        assert resp.text == "fresh prose"
        assert resp.headers["X-Profile-Source"] == "llm"
    # The regenerated narrative is now cached for subsequent (non-refresh) reads.
    assert user_store.get_profile_cache()["text"] == "fresh prose"


def test_shortcut_rejects_missing_token(client):
    """A shortcut POST without the shared secret is rejected with 401."""
    resp = client.post("/webhooks/shortcut", json={"action": "made_it"})
    assert resp.status_code == 401


def test_shortcut_rejects_wrong_token(client):
    """A wrong shared secret is rejected with 401."""
    resp = client.post(
        "/webhooks/shortcut",
        json={"action": "made_it"},
        headers={"X-Prefrontal-Token": "nope"},
    )
    assert resp.status_code == 401


def test_made_it_creates_success_episode(client, user_store):
    """'made_it' maps to a success episode and persists to the store."""
    resp = client.post(
        "/webhooks/shortcut",
        json={"action": "made_it", "episode_type": "departure", "channel": "notification"},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["outcome"] == "success"
    assert body["n8n_delivered"] is False  # n8n disabled by default

    ep = user_store.get_episode(body["episode_id"])
    assert ep["episode_type"] == "departure"
    assert ep["outcome"] == "success"
    assert ep["acknowledged"] == 1  # one-tap implies acknowledgement


def test_missed_it_creates_miss_episode(client, user_store):
    """'missed_it' maps to a miss episode and, like made_it, implies an ack.

    A one-tap 'Missed it' is still the user engaging — the ntfy button path
    records acknowledged=True for it, so the Shortcut default must match rather
    than leave it NULL (which would undercount acks in channel-response learning).
    """
    resp = client.post(
        "/webhooks/shortcut",
        json={"action": "missed_it", "episode_type": "reminder"},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["outcome"] == "miss"
    assert user_store.get_episode(body["episode_id"])["acknowledged"] == 1


def test_log_action_requires_outcome(client):
    """action='log' without an explicit outcome is a 422."""
    resp = client.post(
        "/webhooks/shortcut",
        json={"action": "log", "episode_type": "task"},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 422


def test_invalid_episode_type_is_rejected(client):
    """Pydantic rejects an episode_type outside the allowed set."""
    resp = client.post(
        "/webhooks/shortcut",
        json={"action": "made_it", "episode_type": "bogus"},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 422


def _feature_source_for(store, episode_id):
    """The `source` recorded on the feature event for a given episode's log."""
    row = store.conn.execute(
        "SELECT source FROM feature_events WHERE ref = ? ORDER BY id DESC LIMIT 1",
        (str(episode_id),),
    ).fetchone()
    return row["source"] if row is not None else None


def test_shortcut_source_defaults_to_shortcut(client, user_store):
    """Omitting `source` records the usage event as the free-signing fallback."""
    resp = client.post(
        "/webhooks/shortcut",
        json={"action": "made_it"},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 201
    assert _feature_source_for(user_store, resp.json()["episode_id"]) == "shortcut"


def test_shortcut_records_native_provenance(client, user_store):
    """A native App Intent / geofence write tags its own source, not 'shortcut'."""
    for source in ("app_intent", "geofence"):
        resp = client.post(
            "/webhooks/shortcut",
            json={"action": "made_it", "source": source},
            headers={"X-Prefrontal-Token": SECRET},
        )
        assert resp.status_code == 201
        assert _feature_source_for(user_store, resp.json()["episode_id"]) == source


def test_shortcut_rejects_unknown_source(client):
    """An out-of-vocab source is a 422 (the provenance set is closed)."""
    resp = client.post(
        "/webhooks/shortcut",
        json={"action": "made_it", "source": "carrier_pigeon"},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 422


def test_n8n_inbound_triages_and_routes(client):
    """The inbound n8n route now classifies + routes the signal (handled=True)."""
    resp = client.post(
        "/webhooks/n8n",
        json={"subject": "Please pay the overdue invoice today", "id": "evt-7"},
        headers={"X-Prefrontal-Token": SECRET},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["handled"] is True
    assert body["decision"]["kind"] == "action"
    assert body["routed_ref"].startswith("todo:")  # actionable → an augmented todo


def test_default_user_allows_no_token(store):
    """With PREFRONTAL_DEFAULT_USER set, a tokenless request resolves to that user."""
    app = create_app(store=store, settings=Settings(default_user="tester"))
    with TestClient(app) as c:
        resp = c.post("/webhooks/shortcut", json={"action": "made_it"})
    assert resp.status_code == 201


def test_disabled_user_token_is_rejected(store):
    """A disabled user's token no longer resolves (401)."""
    store.set_user_status("tester", "disabled")
    app = create_app(store=store, settings=Settings())
    with TestClient(app) as c:
        resp = c.post(
            "/webhooks/shortcut",
            json={"action": "made_it"},
            headers={"X-Prefrontal-Token": SECRET},
        )
    assert resp.status_code == 401


def test_calendar_slots_finds_free_windows(client, user_store):
    """GET /calendar/slots returns open windows of at least the requested length."""
    # No commitments over a fortnight: some future waking day always has room, so
    # the response is non-empty regardless of the wall-clock time the suite runs.
    resp = client.get(
        "/calendar/slots?minutes=30&days=14", headers={"X-Prefrontal-Token": SECRET}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["minutes"] == 30 and body["days"] == 14
    assert len(body["slots"]) >= 1
    slot = body["slots"][0]
    # Each slot carries the UTC bounds plus pre-formatted local labels for display.
    assert set(slot) >= {"start_at", "end_at", "minutes", "day", "start", "end"}
    assert slot["minutes"] >= 30


def test_calendar_slots_respects_custom_hour_guard(client, user_store):
    """Slots honor the user's own hour guard (off-zone), not just the 06:00–22:00 default.

    The endpoint derives its waking band from ``window_config_for(...).awake_band()``,
    so a per-user off-zone override must narrow the results. This guards the wiring:
    ``find_slots``'s default band equals the *default* off-zone complement, so a test
    on the default config would still pass even if the guard were dropped — only a
    customized band proves the endpoint reads the user's config.
    """
    from datetime import datetime

    # Awake only 09:00–16:00 local (tz defaults to UTC, so labels are UTC too).
    user_store.set_state("todo_offzone", "16:00-09:00", source="test")
    resp = client.get(
        "/calendar/slots?minutes=30&days=14", headers={"X-Prefrontal-Token": SECRET}
    )
    assert resp.status_code == 200
    slots = resp.json()["slots"]
    assert slots, "a fortnight of empty waking days should yield some slot"

    def hour(label: str) -> int:
        return datetime.strptime(label, "%I:%M %p").hour

    # Nothing falls outside the custom band — no early-morning or evening slots.
    for s in slots:
        assert hour(s["start"]) >= 9, f"slot starts before the guard: {s['start']}"
        assert hour(s["end"]) <= 16, f"slot ends after the guard: {s['end']}"
    # And a full future day opens exactly at the custom band start (9am, not the
    # default 6am) — proof the guard is the user's, not find_slots' fallback.
    assert any(s["start"] == "9:00 AM" for s in slots)


def test_calendar_slots_validates_query(client):
    """Out-of-range minutes / days are rejected at the edge (422)."""
    hdr = {"X-Prefrontal-Token": SECRET}
    assert client.get("/calendar/slots?minutes=0", headers=hdr).status_code == 422
    assert client.get("/calendar/slots?minutes=1441", headers=hdr).status_code == 422
    assert client.get("/calendar/slots?days=0", headers=hdr).status_code == 422
    assert client.get("/calendar/slots?days=15", headers=hdr).status_code == 422


def test_calendar_slots_requires_auth(client):
    """The slot finder is auth-guarded like the other read endpoints."""
    assert client.get("/calendar/slots").status_code == 401


def test_calendar_page_served_unauthenticated(client):
    """The read-only calendar shell is a data-less page (no token needed)."""
    resp = client.get("/calendar")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Household calendar" in resp.text
