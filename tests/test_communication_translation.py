"""Tests for communication translation (roadmap M4) — the pure core + endpoint.

Covers mode/register normalization, the model-success path (a fake ``Generator``
returning canned JSON), the defensive coercion of a malformed reply, the offline
heuristic fallback for each mode (decode/draft caveat; soften degrades to the
user's own text), empty-input handling, and the ``POST /communicate/translate``
endpoint including auth. The LLM is stubbed — this feature is model-driven, so a
fake generator stands in for Ollama/Anthropic.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from prefrontal.communication_translation import (
    DEFAULT_REGISTER,
    MODES,
    REGISTERS,
    TranslationResult,
    normalize_mode,
    normalize_register,
    translate,
)
from prefrontal.config import Settings
from prefrontal.integrations.ollama import OllamaError
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.webhooks.app import create_app

SECRET = "comm-secret"


class _FakeGenerator:
    """A minimal :class:`~prefrontal.integrations.Generator` for tests.

    Records the last ``(prompt, system)`` it saw and returns a canned reply, or
    raises when constructed with ``raises=`` to exercise the transport-failure path.
    """

    def __init__(self, reply: str = "", *, raises: bool = False):
        self.reply = reply
        self.raises = raises
        self.last_prompt: str | None = None
        self.last_system: str | None = None

    def generate(self, prompt, *, system=None, num_ctx=None, timeout=None) -> str:
        self.last_prompt = prompt
        self.last_system = system
        if self.raises:
            raise OllamaError("model down")
        return self.reply


def _json(output: str) -> str:
    return json.dumps({"output": output})


# --- normalization -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("decode", "decode"), ("DRAFT", "draft"), (" soften ", "soften"), ("", "decode"),
     (None, "decode"), ("nonsense", "decode")],
)
def test_normalize_mode(raw, expected):
    assert normalize_mode(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("warm", "warm"), ("FIRM", "firm"), ("", DEFAULT_REGISTER), (None, DEFAULT_REGISTER),
     ("nonsense", DEFAULT_REGISTER)],
)
def test_normalize_register(raw, expected):
    assert normalize_register(raw) == expected


def test_modes_and_registers_are_stable():
    assert MODES == ("decode", "draft", "soften")
    assert set(REGISTERS) == {"professional", "warm", "firm", "concise", "friendly"}


# --- model-success path ------------------------------------------------------


def test_decode_uses_model_output_and_ignores_register():
    client = _FakeGenerator(_json("They want the draft by Friday."))
    result = translate("circle back re: cadence", "decode", "warm", client=client)
    assert result.output == "They want the draft by Friday."
    assert result.mode == "decode"
    assert result.register == ""  # decode never carries a register
    assert result.offline is False
    # The system prompt and a decode-framed user turn reached the model.
    assert "decode" in (client.last_prompt or "")
    assert client.last_system and "ONLY JSON" in client.last_system


def test_draft_carries_register_and_folds_it_into_the_prompt():
    client = _FakeGenerator(_json("Hi Sam, quick update — [status]."))
    result = translate("tell Sam I'm running late", "draft", "firm", client=client)
    assert result.output.startswith("Hi Sam")
    assert result.register == "firm"
    assert "firm" in (client.last_prompt or "")


def test_soften_rewrites_via_model():
    client = _FakeGenerator(_json("Could we revisit this when you have a moment?"))
    result = translate("fix this now", "soften", "professional", client=client)
    assert result.output == "Could we revisit this when you have a moment?"
    assert result.mode == "soften"
    assert result.offline is False


def test_malformed_reply_reports_unusable_not_unavailable():
    # A non-empty reply that doesn't parse → the model WAS online, so the caveat
    # must say the response was unusable, not that the model was unavailable.
    client = _FakeGenerator("sorry, I can't do that")
    result = translate("what does this mean", "decode", client=client)
    assert result.offline is True
    assert result.output == ""
    assert "couldn't use" in result.note.lower()
    assert "unavailable" not in result.note.lower()


def test_transport_error_reports_unavailable():
    # An empty reply (transport failure) is genuine unavailability — the other side
    # of the distinction from the malformed-reply case above.
    client = _FakeGenerator(raises=True)
    result = translate("draft a reply", "draft", client=client)
    assert result.offline is True
    assert "unavailable" in result.note.lower()
    assert "couldn't use" not in result.note.lower()


# --- offline heuristic (no client) -------------------------------------------


def test_offline_decode_declines_honestly():
    result = translate("some cryptic email", "decode", client=None)
    assert result.offline is True
    assert result.output == ""
    assert "can't decode" in result.note.lower()


def test_offline_draft_declines_honestly():
    result = translate("say I'll be late", "draft", client=None)
    assert result.offline is True
    assert result.output == ""
    assert "can't draft" in result.note.lower()


def test_offline_soften_returns_original_text():
    result = translate("just send it already", "soften", "warm", client=None)
    assert result.offline is True
    assert result.output == "just send it already"  # degrades to the user's own text
    assert result.register == "warm"
    assert "warm" in result.note.lower()


def test_empty_input_returns_a_prompt_not_a_call():
    client = _FakeGenerator(_json("should not be used"))
    result = translate("   ", "decode", client=client)
    assert result.output == ""
    assert client.last_prompt is None  # model never consulted
    assert isinstance(result, TranslationResult)


# --- endpoint ----------------------------------------------------------------


def _app_client():
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "tester", display_name="T", token=SECRET, is_operator=True)
    app = create_app(store=unscoped, settings=Settings(webhook_secret=SECRET))
    return conn, TestClient(app)


def test_endpoint_requires_auth():
    conn, client = _app_client()
    try:
        with client:
            r = client.post("/communicate/translate", json={"text": "hi"})
            assert r.status_code == 401
    finally:
        conn.close()


def test_endpoint_returns_offline_shape_without_a_model():
    # No model configured in the test app → decode reports offline, not a crash.
    conn, client = _app_client()
    try:
        with client:
            r = client.post(
                "/communicate/translate",
                json={"text": "circle back to align", "mode": "decode"},
                headers={"X-Prefrontal-Token": SECRET},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["mode"] == "decode"
            assert body["offline"] is True
            assert body["output"] == ""
            assert "provider" in body
    finally:
        conn.close()


def test_endpoint_soften_degrades_to_original_without_a_model():
    conn, client = _app_client()
    try:
        with client:
            r = client.post(
                "/communicate/translate",
                json={"text": "fix it now", "mode": "soften", "register": "friendly"},
                headers={"X-Prefrontal-Token": SECRET},
            )
            body = r.json()
            assert body["output"] == "fix it now"
            assert body["register"] == "friendly"
            assert body["offline"] is True
    finally:
        conn.close()
