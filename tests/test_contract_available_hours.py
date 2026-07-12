"""Contract guard for the ``/schedule/available-hours`` shape.

The ``AvailableHours`` / ``DayAvailability`` contract is **hand-mirrored** in three
independent places — the Pydantic schema
(:mod:`prefrontal.webhooks.schemas.schedule`), the web dashboard's ``settings.html``
JavaScript, and the iOS ``Models.swift`` ``Codable`` structs. Nothing regenerates
those mirrors from a single source, so this test is the drift alarm: it pins the
server's **structural** schema (field names, types, patterns, defaults, weekday
vocabulary) to a committed snapshot. Any change to the shape fails here until the
snapshot is regenerated — a deliberate, reviewable signal that the web + iOS
mirrors have to move in lockstep.

Docstrings/titles are intentionally stripped before comparison: prose is not part
of the contract, so editing a description never trips this guard.

Update the snapshot **on purpose** (after moving the web + iOS mirrors) with::

    UPDATE_CONTRACT=1 uv run pytest tests/test_contract_available_hours.py
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
from prefrontal.scheduling import WEEKDAYS
from prefrontal.webhooks.app import create_app
from prefrontal.webhooks.schemas import AvailableHours
from tests.conftest import scoped_default

CONTRACTS = Path(__file__).parent / "contracts"
SCHEMA_SNAPSHOT = CONTRACTS / "available_hours.schema.json"
EXAMPLE = CONTRACTS / "available_hours.example.json"

#: The models whose structural schema both clients mirror.
CONTRACT_MODELS = ("AvailableHours", "DayAvailability")


def _strip_prose(node: Any) -> Any:
    """Recursively drop non-structural keys (``description``/``title``).

    Leaves everything that a client mirror actually depends on — property names,
    ``type``, ``pattern``, ``default``, ``required``, ``$ref``,
    ``additionalProperties`` — so the snapshot fails on real shape drift but not
    on a reworded docstring.
    """
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


def test_available_hours_schema_matches_snapshot() -> None:
    live = _live_schema()
    if os.environ.get("UPDATE_CONTRACT"):
        SCHEMA_SNAPSHOT.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n")
        pytest.skip("contract snapshot regenerated (UPDATE_CONTRACT set)")
    assert SCHEMA_SNAPSHOT.exists(), (
        f"missing contract snapshot {SCHEMA_SNAPSHOT}; regenerate with "
        "UPDATE_CONTRACT=1 uv run pytest tests/test_contract_available_hours.py"
    )
    committed = json.loads(SCHEMA_SNAPSHOT.read_text())
    assert live == committed, (
        "The /schedule/available-hours schema drifted from the committed contract.\n"
        "If this change is intentional, update BOTH client mirrors to match — the "
        "web dashboard (prefrontal/webhooks/settings.html) and the iOS app "
        "(ios/Prefrontal/Models/Models.swift) — then regenerate the snapshot with:\n"
        "    UPDATE_CONTRACT=1 uv run pytest tests/test_contract_available_hours.py"
    )


def test_example_fixture_is_valid_and_canonical() -> None:
    """The shared example both mirrors are documented against stays valid."""
    payload = json.loads(EXAMPLE.read_text())
    parsed = AvailableHours(**payload)  # raises if the example no longer validates
    # The example is meant to be the canonical full-week reference: all seven days,
    # a mix of available bands and an off day, so every field/branch is exercised.
    assert set(parsed.days) == set(WEEKDAYS)
    assert any(not d.available for d in parsed.days.values()), "cover an off day"
    assert any(d.available for d in parsed.days.values()), "cover an available day"
