"""Tests for municipal service-shift extraction (the scrape's LLM + storage seam).

Covers the deterministic normalization of a model's raw array, the extract path
with a mock model (offline / empty / valid), and the refresh orchestrator that
upserts extracted shifts into the store. The live HTTP fetch of the page is the
one piece not covered here — it's a thin, deployment-specific step over this.
"""
from __future__ import annotations

import httpx

from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.service_shifts import (
    extract_service_shifts,
    normalize_extracted_shifts,
    refresh_service_shifts,
)

# July 2 2026 is a Thursday (weekday 3); its week starts Mon June 29.
_PAGE = "Trash & recycling collection will be delayed one day the week of July 4th."


def _ollama_replying(text: str) -> OllamaClient:
    return OllamaClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"response": text}))
    )


def _offline_ollama() -> OllamaClient:
    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no server in tests")

    return OllamaClient(transport=httpx.MockTransport(refuse))


def test_normalize_derives_week_and_weekday_from_date():
    raw = [{"service": "Trash", "new_date": "2026-07-02", "reason": "Independence Day"}]
    assert normalize_extracted_shifts(raw, services=["trash"]) == [
        {"service": "trash", "week": "2026-06-29", "shifted_weekday": 3,
         "reason": "Independence Day"}
    ]


def test_normalize_drops_bad_and_out_of_scope_rows():
    raw = [
        {"service": "trash", "new_date": "not-a-date"},        # unparseable date
        {"service": "", "new_date": "2026-07-02"},             # no service
        {"service": "snow", "new_date": "2026-07-02"},         # not in requested set
        {"new_date": "2026-07-02"},                            # missing service
        "junk",                                                # not a dict
    ]
    assert normalize_extracted_shifts(raw, services=["trash", "recycling"]) == []


def test_extract_with_model_returns_normalized_shifts():
    reply = '[{"service": "trash", "new_date": "2026-07-02", "reason": "July 4th"}]'
    shifts = extract_service_shifts(_PAGE, services=["trash"], client=_ollama_replying(reply))
    assert shifts == [
        {"service": "trash", "week": "2026-06-29", "shifted_weekday": 3, "reason": "July 4th"}
    ]


def test_extract_is_empty_when_model_unavailable_or_says_none():
    # Transport failure → safe empty (no guessed shift).
    assert extract_service_shifts(_PAGE, services=["trash"], client=_offline_ollama()) == []
    # Model explicitly reports no shifts.
    assert extract_service_shifts(_PAGE, services=["trash"], client=_ollama_replying("[]")) == []
    # Blank page → no model call needed.
    assert extract_service_shifts("   ", services=["trash"], client=_ollama_replying("[]")) == []


def test_refresh_upserts_extracted_shifts_and_is_idempotent():
    conn = init_db(":memory:")
    try:
        root = MemoryStore(conn)
        provision_user(root, "dana", display_name="Dana", token="d")
        hid = root.create_household("Home")
        root.set_user_household("dana", hid)
        store = root.scoped(root.get_user("dana")["id"])

        reply = '[{"service": "trash", "new_date": "2026-07-02", "reason": "July 4th"}]'
        stored = refresh_service_shifts(
            store, page_text=_PAGE, services=["trash"],
            source_url="https://example.gov/schedule", client=_ollama_replying(reply),
        )
        assert len(stored) == 1
        row = store.service_shift("trash", "2026-06-29")
        assert row["shifted_weekday"] == 3 and row["source_url"].endswith("/schedule")

        # Re-running the same scrape overwrites in place (no duplicate row).
        refresh_service_shifts(
            store, page_text=_PAGE, services=["trash"], client=_ollama_replying(reply)
        )
        assert len(store.service_shifts()) == 1
    finally:
        conn.close()
