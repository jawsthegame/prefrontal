"""HTTP surface for ambiguity clarification.

Covers the detection sweep (creates one pending question per vague item, never
re-asks), the inline resolve (records the reading, honing a todo's notes, and
returns the guided playbook for a recognized task), dismiss, the playbook fetch,
and auth.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from tests.conftest import scoped_default

_SECRET = "clarify-http-secret"


def _auth() -> dict[str, str]:
    return {"X-Prefrontal-Token": _SECRET}


def _app():
    """A TestClient over an in-memory store; the default Ollama is offline (heuristic)."""
    conn = init_db(":memory:")
    store = scoped_default(MemoryStore(conn))
    from prefrontal.webhooks.app import create_app

    app = create_app(store=store, settings=Settings(webhook_secret=_SECRET))
    return TestClient(app), store


def test_check_creates_one_question_per_vague_item_and_never_reasks():
    client, store = _app()
    tid = store.add_todo("Tax", priority=2)
    store.add_todo("Call the dentist to reschedule at 3pm")  # clear → skipped
    with client:
        r = client.post("/clarifications/check", headers=_auth())
        assert r.status_code == 200
        assert r.json()["created"] == 1  # only the ambiguous one
        pending = client.get("/clarifications", headers=_auth()).json()["clarifications"]
        assert [c["title"] for c in pending] == ["Tax"]
        assert pending[0]["target_id"] == tid
        # An option that maps to a known task advertises its guide.
        assert any(o["has_playbook"] for o in pending[0]["options"])
        # Re-sweeping asks nothing new (the item already has history).
        assert client.post("/clarifications/check", headers=_auth()).json()["created"] == 0


def test_resolve_by_option_returns_playbook_and_hones_todo():
    client, store = _app()
    tid = store.add_todo("Tax", priority=2)
    with client:
        client.post("/clarifications/check", headers=_auth())
        cid = client.get("/clarifications", headers=_auth()).json()["clarifications"][0]["id"]
        r = client.post(f"/clarifications/{cid}/resolve", json={"option_index": 0}, headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["task_type"] == "tax_filing"
        assert body["playbook"]["title"] and body["playbook"]["steps"]
        # The todo itself is honed (non-destructively) via its notes.
        assert "Clarified:" in (store.get_todo(tid)["notes"] or "")
        # The question leaves the pending queue and appears under guided.
        after = client.get("/clarifications", headers=_auth()).json()
        assert after["clarifications"] == []
        assert after["guided"] and after["guided"][0]["task_type"] == "tax_filing"


def test_resolve_free_text_without_known_type_has_no_playbook():
    client, store = _app()
    store.add_todo("Project")
    with client:
        client.post("/clarifications/check", headers=_auth())
        cid = client.get("/clarifications", headers=_auth()).json()["clarifications"][0]["id"]
        r = client.post(
            f"/clarifications/{cid}/resolve",
            json={"answer": "the garage shelving build"},
            headers=_auth(),
        )
        assert r.status_code == 200
        assert r.json()["task_type"] is None and r.json()["playbook"] is None


def test_resolve_requires_option_or_answer():
    client, store = _app()
    store.add_todo("Tax")
    with client:
        client.post("/clarifications/check", headers=_auth())
        cid = client.get("/clarifications", headers=_auth()).json()["clarifications"][0]["id"]
        url = f"/clarifications/{cid}/resolve"
        assert client.post(url, json={}, headers=_auth()).status_code == 422
        # Out-of-range option index is rejected, not silently ignored.
        assert client.post(url, json={"option_index": 99}, headers=_auth()).status_code == 422


def test_resolve_unknown_or_resolved_is_404():
    client, store = _app()
    store.add_todo("Tax")
    with client:
        client.post("/clarifications/check", headers=_auth())
        cid = client.get("/clarifications", headers=_auth()).json()["clarifications"][0]["id"]
        def resolve(i):
            return client.post(
                f"/clarifications/{i}/resolve", json={"option_index": 0}, headers=_auth()
            )

        assert resolve(cid).status_code == 200
        # Double-resolve → 404 (no longer pending), so it can't re-apply.
        assert resolve(cid).status_code == 404
        assert resolve(999999).status_code == 404


def test_dismiss_removes_from_queue_and_stops_reasking():
    client, store = _app()
    store.add_todo("Mom")
    with client:
        client.post("/clarifications/check", headers=_auth())
        cid = client.get("/clarifications", headers=_auth()).json()["clarifications"][0]["id"]
        assert client.post(f"/clarifications/{cid}/dismiss", headers=_auth()).status_code == 200
        assert client.get("/clarifications", headers=_auth()).json()["clarifications"] == []
        # Dismissed items are remembered → the sweep won't re-ask.
        assert client.post("/clarifications/check", headers=_auth()).json()["created"] == 0
        assert client.post(f"/clarifications/{cid}/dismiss", headers=_auth()).status_code == 404


def test_playbook_fetch():
    client, _ = _app()
    with client:
        def get(t):
            return client.get(f"/clarifications/playbooks/{t}", headers=_auth())

        assert get("tax_filing").status_code == 200
        assert get("nope").status_code == 404


def test_playbook_localization_is_opt_in():
    """A guide localizes to the home ZIP only when the user opted in."""
    client, store = _app()
    with client:
        def steps_blob():
            pb = client.get("/clarifications/playbooks/license_renewal", headers=_auth()).json()
            return " ".join(s["detail"] for s in pb["steps"])

        # Seeded off by default → generic phrasing, even though home_zip is seeded.
        assert store.get_state("home_zip") == "19027"
        assert "your area" in steps_blob() and "19027" not in steps_blob()
        # Opt in → the guide weaves in the ZIP.
        store.set_state("playbook_localization", "1", source="explicit")
        assert "19027" in steps_blob()


def test_coaching_tick_fills_the_queue_passively():
    """POST /webhooks/coach/check runs the same detection sweep, so the queue
    fills without pressing the dashboard's manual check."""
    client, store = _app()
    store.add_todo("Tax", priority=2)
    with client:
        # No manual /clarifications/check — a coaching tick alone should file it.
        r = client.post("/webhooks/coach/check", json={}, headers=_auth())
        assert r.status_code == 200
        pending = client.get("/clarifications", headers=_auth()).json()["clarifications"]
        assert [c["title"] for c in pending] == ["Tax"]


def test_clarifications_require_auth():
    client, _ = _app()
    with client:
        assert client.get("/clarifications").status_code == 401
        assert client.post("/clarifications/check").status_code == 401
