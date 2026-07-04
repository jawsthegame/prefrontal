"""Tests for the n8n integration client.

Focuses on the construction seam (`from_settings`) and no-op/local-first mode,
which need no network — a client with no webhook URL never leaves the host.
The workflow-sync tests drive the REST upsert through an httpx MockTransport,
so they exercise the create/update/activate logic without a live n8n.
"""

from __future__ import annotations

import json

import httpx

from prefrontal.config import Settings
from prefrontal.integrations.n8n import N8nClient, N8nWorkflowSyncer


def test_from_settings_reads_url_and_token():
    """from_settings mirrors the other integration clients' construction seam."""
    settings = Settings(
        n8n_webhook_url="https://n8n.example/webhook/abc",
        n8n_webhook_token="secret",
    )
    client = N8nClient.from_settings(settings)
    assert client.webhook_url == "https://n8n.example/webhook/abc"
    assert client.token == "secret"
    assert client.enabled is True


def test_from_settings_no_url_is_disabled_noop():
    """An unconfigured URL yields a disabled client that drops events locally."""
    client = N8nClient.from_settings(Settings())
    assert client.enabled is False
    result = client.trigger("episode.logged", {"id": 1})
    assert result.delivered is False
    assert result.status_code is None


# --- workflow sync --------------------------------------------------------

API = "http://127.0.0.1:5678/api/v1"


def _write_workflow(tmp_path, filename, name, *, active=False):
    """Write a minimal but API-shaped workflow template to tmp_path."""
    doc = {
        "name": name,
        "active": active,
        "nodes": [],
        "connections": {},
        "settings": {},
        "meta": {"note": "should be stripped"},
        "pinData": {},
        "id": "should-be-stripped",
    }
    (tmp_path / filename).write_text(json.dumps(doc))
    return doc


def test_syncer_from_settings_and_enabled():
    """from_settings reads the API url/key; both are required to be enabled."""
    syncer = N8nWorkflowSyncer.from_settings(
        Settings(n8n_api_url="http://127.0.0.1:5678/api/v1", n8n_api_key="k")
    )
    assert syncer.enabled is True
    # trailing slash is normalized off
    assert syncer.api_url == "http://127.0.0.1:5678/api/v1"
    assert N8nWorkflowSyncer(api_url=API, api_key="").enabled is False
    assert N8nWorkflowSyncer(api_url="", api_key="k").enabled is False


def test_push_unconfigured_is_clean_skip(tmp_path):
    """No url/key → a no-op that reports enabled=False but ok=True (a skip succeeds)."""
    _write_workflow(tmp_path, "a.json", "Prefrontal — A")
    report = N8nWorkflowSyncer().push(str(tmp_path))
    assert report == {
        "enabled": False,
        "ok": True,
        "pushed": [],
        "detail": "n8n API not configured; skipped workflow sync",
    }


def test_push_creates_and_updates_by_name(tmp_path):
    """A workflow whose name already exists is PUT (updated); a new one is POSTed."""
    _write_workflow(tmp_path, "existing.json", "Prefrontal — Existing", active=True)
    _write_workflow(tmp_path, "fresh.json", "Prefrontal — Fresh", active=False)
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith("/workflows"):
            return httpx.Response(200, json={
                "data": [{"id": "42", "name": "Prefrontal — Existing"}],
                "nextCursor": None,
            })
        if request.method == "PUT":  # update existing id 42
            body = json.loads(request.content)
            # read-only fields are stripped before sending
            assert set(body) <= {"name", "nodes", "connections", "settings", "staticData"}
            return httpx.Response(200, json={"id": "42", "name": body["name"]})
        if request.method == "POST" and request.url.path.endswith("/workflows"):
            name = json.loads(request.content)["name"]
            return httpx.Response(200, json={"id": "99", "name": name})
        if request.url.path.endswith("/activate"):
            return httpx.Response(200, json={"id": request.url.path.split("/")[-2]})
        if request.url.path.endswith("/deactivate"):
            return httpx.Response(200, json={"id": request.url.path.split("/")[-2]})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = N8nWorkflowSyncer(api_url=API, api_key="k").push(str(tmp_path), client=client)

    assert report["enabled"] is True
    assert report["ok"] is True
    by_name = {w["name"]: w for w in report["pushed"]}
    assert by_name["Prefrontal — Existing"]["action"] == "updated"
    assert by_name["Prefrontal — Existing"]["workflow_id"] == "42"
    assert by_name["Prefrontal — Existing"]["active"] is True  # file said active
    assert by_name["Prefrontal — Fresh"]["action"] == "created"
    assert by_name["Prefrontal — Fresh"]["workflow_id"] == "99"
    assert by_name["Prefrontal — Fresh"]["active"] is False    # file said inactive
    # active:true template hit /activate; active:false hit /deactivate
    assert any(m == "POST" and p.endswith("/42/activate") for m, p in calls)
    assert any(m == "POST" and p.endswith("/99/deactivate") for m, p in calls)


def test_push_no_activate_leaves_state_untouched(tmp_path):
    """--no-activate upserts the definition but never calls activate/deactivate."""
    _write_workflow(tmp_path, "a.json", "Prefrontal — A", active=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [], "nextCursor": None})
        if request.method == "POST" and request.url.path.endswith("/workflows"):
            return httpx.Response(200, json={"id": "1", "name": "Prefrontal — A"})
        raise AssertionError(f"unexpected activate/deactivate: {request.url.path}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = N8nWorkflowSyncer(api_url=API, api_key="k").push(
        str(tmp_path), activate=False, client=client
    )
    assert report["ok"] is True
    assert report["pushed"][0]["active"] is None  # state untouched


def test_push_reports_per_workflow_failure(tmp_path):
    """A single failing upsert marks that workflow failed and the report not-ok."""
    _write_workflow(tmp_path, "a.json", "Prefrontal — A")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [], "nextCursor": None})
        return httpx.Response(400, json={"message": "bad workflow"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = N8nWorkflowSyncer(api_url=API, api_key="k").push(str(tmp_path), client=client)
    assert report["ok"] is False
    assert report["pushed"][0]["action"] == "failed"
    assert "Prefrontal — A" in report["detail"]


def test_push_list_error_is_reported_not_raised(tmp_path):
    """If n8n is unreachable for the initial listing, push reports ok=False cleanly."""
    _write_workflow(tmp_path, "a.json", "Prefrontal — A")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = N8nWorkflowSyncer(api_url=API, api_key="k").push(str(tmp_path), client=client)
    assert report["enabled"] is True
    assert report["ok"] is False
    assert report["pushed"] == []
    assert "could not list workflows" in report["detail"]


def test_push_paginates_existing_workflows(tmp_path):
    """The name→id map follows nextCursor across pages before matching."""
    _write_workflow(tmp_path, "a.json", "Prefrontal — Page2", active=False)
    pages = {
        None: {"data": [{"id": "1", "name": "Other"}], "nextCursor": "c2"},
        "c2": {"data": [{"id": "2", "name": "Prefrontal — Page2"}], "nextCursor": None},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            cursor = request.url.params.get("cursor")
            return httpx.Response(200, json=pages[cursor])
        if request.method == "PUT":
            return httpx.Response(200, json={"id": "2", "name": "Prefrontal — Page2"})
        if request.url.path.endswith("/deactivate"):
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = N8nWorkflowSyncer(api_url=API, api_key="k").push(str(tmp_path), client=client)
    assert report["ok"] is True
    # matched the id from page 2 → update, not create
    assert report["pushed"][0]["action"] == "updated"
    assert report["pushed"][0]["workflow_id"] == "2"
