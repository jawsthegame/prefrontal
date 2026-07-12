"""The in-app new-user Guide — per-module walkthroughs, re-readable.

Covers the content model (each module derives a coherent tutorial from its own
metadata), the HTTP surface (``/guide`` shell, ``/guide/data``, marking a module
read / new, resetting), that the guide only ever shows *enabled* modules, and
auth.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import prefrontal.modules  # noqa: F401  (registers built-in modules on import)
from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.modules import available, get
from prefrontal.modules.base import Module, TutorialStep
from tests.conftest import scoped_default

_SECRET = "guide-http-secret"


def _auth() -> dict[str, str]:
    return {"X-Prefrontal-Token": _SECRET}


def _app(settings: Settings | None = None):
    """A TestClient over an in-memory store (Ollama offline → heuristic paths)."""
    store = scoped_default(MemoryStore(init_db(":memory:")))
    from prefrontal.webhooks.app import create_app

    app = create_app(store=store, settings=settings or Settings(webhook_secret=_SECRET))
    return TestClient(app), store


# -- content model -----------------------------------------------------------


def test_every_module_has_a_nonempty_tutorial():
    """The default walkthrough gives every registered module coherent steps."""
    for module in available():
        steps = module.tutorial()
        assert steps, f"{module.key} has no tutorial steps"
        assert all(isinstance(s, TutorialStep) for s in steps)
        assert all(s.title and s.body for s in steps)


def test_tutorial_is_derived_from_metadata():
    """The steps are built from the module's own challenge + interventions, so the
    guide can never drift from what the module actually does."""
    module = get("time_blindness")
    steps = module.tutorial()
    titles = [s.title for s in steps]
    assert titles[0] == "What Time Blindness helps with"
    assert steps[0].body == module.challenge
    # The active interventions are each rendered as a bullet in one step.
    do_step = next(s for s in steps if s.title == "What Prefrontal will do")
    for iv in module.interventions():
        assert f"• {iv.description}" in do_step.body
    # It closes with the reassuring "nothing to switch on" step.
    assert steps[-1].title == "You're set"


def test_planned_interventions_get_a_coming_soon_step():
    """A module with a not-yet-wired intervention surfaces it under 'Coming soon'."""
    from prefrontal.modules.base import Intervention

    class Sample(Module):
        key = "sample"
        title = "Sample"
        challenge = "A test challenge."

        def interventions(self):
            return [
                Intervention("wired", "Does a thing.", "a trigger", status="active"),
                Intervention("future", "Will do a thing.", "a trigger", status="planned"),
            ]

        def profile_section(self, store):  # pragma: no cover - unused here
            return None

    titles = [s.title for s in Sample().tutorial()]
    assert "Coming soon" in titles


# -- HTTP surface ------------------------------------------------------------


def test_guide_page_serves_html():
    client, _ = _app()
    with client:
        r = client.get("/guide")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Getting started" in r.text


def test_guide_data_lists_enabled_modules_with_steps_and_interventions():
    client, _ = _app()
    with client:
        data = client.get("/guide/data", headers=_auth()).json()
        assert data["total"] == len(available())
        assert data["completed"] == []
        first = data["modules"][0]
        assert first["key"] and first["title"] and first["steps"]
        assert {"name", "description", "trigger", "status"} <= set(first["interventions"][0])


def test_guide_only_shows_enabled_modules():
    """A deployment that enables a subset sees only those modules' guides."""
    client, _ = _app(Settings(webhook_secret=_SECRET, modules=("hyperfocus", "time_blindness")))
    with client:
        data = client.get("/guide/data", headers=_auth()).json()
        keys = {m["key"] for m in data["modules"]}
        assert keys == {"hyperfocus", "time_blindness"}
        assert data["total"] == 2


def test_mark_seen_and_unseen_tracks_progress():
    client, _ = _app()
    with client:
        after = client.post("/guide/seen", json={"key": "hyperfocus"}, headers=_auth()).json()
        assert after["completed"] == ["hyperfocus"]
        assert next(m for m in after["modules"] if m["key"] == "hyperfocus")["completed"] is True
        # Flip it back to new — the guide is never a one-shot.
        back = client.post(
            "/guide/seen", json={"key": "hyperfocus", "seen": False}, headers=_auth()
        ).json()
        assert back["completed"] == []


def test_progress_persists_across_requests_and_reset_clears_it():
    client, store = _app()
    with client:
        client.post("/guide/seen", json={"key": "hyperfocus"}, headers=_auth())
        client.post("/guide/seen", json={"key": "impulsivity"}, headers=_auth())
        assert set(client.get("/guide/data", headers=_auth()).json()["completed"]) == {
            "hyperfocus",
            "impulsivity",
        }
        # Persisted in coaching state, not just in-request.
        assert "hyperfocus" in (store.get_state("guide_seen") or "")
        reset = client.post("/guide/reset", headers=_auth()).json()
        assert reset["completed"] == []
        assert (store.get_state("guide_seen") or "") == ""


def test_completed_ignores_a_now_disabled_module():
    """A key marked read that later isn't enabled doesn't inflate the count."""
    settings = Settings(webhook_secret=_SECRET, modules=("hyperfocus",))
    client, store = _app(settings)
    with client:
        # Simulate an old mark for a module this deployment no longer enables.
        store.set_state("guide_seen", "hyperfocus,trip_tracking", source="explicit")
        data = client.get("/guide/data", headers=_auth()).json()
        assert data["completed"] == ["hyperfocus"]  # trip_tracking not enabled → excluded
        assert data["total"] == 1


def test_guide_endpoints_require_auth():
    client, _ = _app()
    with client:
        assert client.get("/guide/data").status_code == 401
        assert client.post("/guide/seen", json={"key": "hyperfocus"}).status_code == 401
        assert client.post("/guide/reset").status_code == 401
