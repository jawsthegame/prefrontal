"""Tests for the served usage guide (``GET /manual``) and its renderer.

The guide is `docs/guide.md` rendered on request; these pin that the route serves
formatted HTML (not the raw Markdown or Swagger), needs no auth, and that the
renderer degrades gracefully when the source file is missing.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks import usage_guide
from prefrontal.webhooks.app import create_app

from .conftest import scoped_default

_SECRET = "manual-secret"


def _client():
    store = scoped_default(MemoryStore(init_db(":memory:")))
    return TestClient(create_app(store=store, settings=Settings(webhook_secret=_SECRET)))


def test_manual_serves_rendered_guide_without_auth():
    with _client() as c:
        r = c.get("/manual")  # no token
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        # The guide's own title, and Markdown actually rendered (not raw / not Swagger).
        assert "Prefrontal — Usage Guide" in r.text
        assert "<table" in r.text  # the reference tables became HTML
        assert "swagger" not in r.text.lower()  # /docs (Swagger) is a different route


def test_manual_and_docs_are_distinct_routes():
    # /docs stays FastAPI's API explorer; the human guide lives at /manual.
    with _client() as c:
        assert "Prefrontal — Usage Guide" not in c.get("/docs").text
        assert "Prefrontal — Usage Guide" in c.get("/manual").text


def test_renderer_reads_the_repo_guide():
    md = usage_guide._read_guide()
    assert md is not None
    assert md.startswith("# Prefrontal — Usage Guide")


def test_renderer_missing_source_degrades_gracefully(monkeypatch, tmp_path):
    # No candidate path exists → a plain explanatory page, never an error.
    monkeypatch.setattr(usage_guide, "_GUIDE_CANDIDATES", (tmp_path / "nope.md",))
    page = usage_guide.render_usage_guide_page()
    assert "Usage guide unavailable" in page
    assert "<html" in page.lower()
