"""Contract guard for the ``/schedule/location-settings`` shape.

The ``LocationSettings`` contract is **hand-mirrored** in three independent
places — the Pydantic schema (:mod:`prefrontal.webhooks.schemas.schedule`), the
web dashboard's ``settings.html`` JavaScript, and the iOS ``Models.swift``
``Codable`` struct. Nothing regenerates those mirrors from a single source, so
this test is the drift alarm: it pins the server's **structural** schema (field
names, types, bounds, defaults) to a committed snapshot. Any change to the shape
fails here until the snapshot is regenerated — a deliberate, reviewable signal
that the web + iOS mirrors have to move in lockstep. See
``tests/test_contract_available_hours.py`` for the template this follows.

Update the snapshot **on purpose** (after moving the web + iOS mirrors) with::

    UPDATE_CONTRACT=1 uv run pytest tests/test_contract_location_settings.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.app import create_app
from prefrontal.webhooks.schemas import LocationSettings
from tests.conftest import scoped_default

CONTRACTS = Path(__file__).parent / "contracts"
SCHEMA_SNAPSHOT = CONTRACTS / "location_settings.schema.json"
EXAMPLE = CONTRACTS / "location_settings.example.json"

#: The model both clients mirror.
CONTRACT_MODELS = ("LocationSettings",)


def _strip_prose(node: Any) -> Any:
    """Recursively drop non-structural keys (``description``/``title``)."""
    if isinstance(node, dict):
        return {
            key: _strip_prose(value)
            for key, value in node.items()
            if key not in ("description", "title")
        }
    if isinstance(node, list):
        return [_strip_prose(item) for item in node]
    return node


def _live_schema() -> dict[str, Any]:
    """The normalized OpenAPI component schema for the contract models."""
    store = scoped_default(MemoryStore(init_db(":memory:")))
    app = create_app(store=store, settings=Settings(webhook_secret="contract"))
    with TestClient(app):
        components = app.openapi()["components"]["schemas"]
    return {name: _strip_prose(components[name]) for name in CONTRACT_MODELS}


def test_location_settings_schema_matches_snapshot() -> None:
    live = _live_schema()
    if os.environ.get("UPDATE_CONTRACT"):
        SCHEMA_SNAPSHOT.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n")
        pytest.skip("contract snapshot regenerated (UPDATE_CONTRACT set)")
    assert SCHEMA_SNAPSHOT.exists(), (
        f"missing contract snapshot {SCHEMA_SNAPSHOT}; regenerate with "
        "UPDATE_CONTRACT=1 uv run pytest tests/test_contract_location_settings.py"
    )
    committed = json.loads(SCHEMA_SNAPSHOT.read_text())
    assert live == committed, (
        "The /schedule/location-settings schema drifted from the committed contract.\n"
        "If this change is intentional, update BOTH client mirrors to match — the "
        "web dashboard (prefrontal/webhooks/settings.html) and the iOS app "
        "(ios/Prefrontal/Models/Models.swift) — then regenerate the snapshot with:\n"
        "    UPDATE_CONTRACT=1 uv run pytest tests/test_contract_location_settings.py"
    )


def test_example_fixture_is_valid_and_canonical() -> None:
    """The shared example both mirrors are documented against stays valid."""
    payload = json.loads(EXAMPLE.read_text())
    parsed = LocationSettings(**payload)  # raises if the example no longer validates
    # The example is the canonical full reference: every tunable present.
    assert parsed.home_radius_m == 150
    assert parsed.geofence_radius_m == 120
    assert parsed.post_interval_s == 300
    assert parsed.visits_enabled is True
