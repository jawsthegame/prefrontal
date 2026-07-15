"""Tests for photo → structured items (roadmap: vision capture).

A photo is read to text by the multimodal model and then fanned out through the
*same* brain-dump paths, so these tests assert two things: the composition
(image → transcript → the assistant + sensor fan-out) and the safety model carried
through it (actions are a preview; behavioral candidates land pending — a misread
photo can never silently mutate the store). No real model or network: fake clients
return canned text.

Vision is Anthropic-only (no local fallback), so the endpoint returns 503 when the
Anthropic backend is unavailable — also covered here.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.integrations.anthropic import AnthropicError
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.vision import plan_vision, transcribe_image
from prefrontal.webhooks.app import create_app

from .conftest import scoped_default

SECRET = "vision-secret"

# The transcript the vision model "reads" off the image, and the brain-dump
# replies its actionable/behavioral halves produce from that transcript.
_TRANSCRIPT = "call the dentist\nkeep my briefings short"
_ASSISTANT_REPLY = json.dumps(
    {
        "reply": "I'll add a todo to call the dentist.",
        "actions": [{"op": "add_todo", "title": "Call the dentist", "priority": 2}],
    }
)
_SENSOR_REPLY = json.dumps(
    [
        {
            "kind": "state",
            "key": "preferred_briefing_format",
            "value": "short",
            "rationale": "keep the briefings short",
        }
    ]
)


class _FakeGen:
    """A text model stand-in that routes by system prompt (assistant vs sensor)."""

    def __init__(self) -> None:
        self.available_result = True

    def available(self) -> bool:
        return self.available_result

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        if system and "note-reader" in system:
            return _SENSOR_REPLY
        return _ASSISTANT_REPLY


class _FakeVision:
    """A vision backend stand-in: available + describe_image returning a transcript."""

    def __init__(
        self, transcript: str = _TRANSCRIPT, *, available: bool = True, error: bool = False
    ) -> None:
        self._transcript = transcript
        self._available = available
        self._error = error
        self.calls: list[dict[str, object]] = []

    def available(self) -> bool:
        return self._available

    def describe_image(
        self,
        image_base64: str,
        *,
        prompt: str,
        media_type: str = "image/jpeg",
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        self.calls.append(
            {"image": image_base64, "prompt": prompt, "media_type": media_type}
        )
        if self._error:
            raise AnthropicError("vision down")
        return self._transcript


class _CombinedFake(_FakeGen, _FakeVision):
    """One client that is both the vision backend and the text model.

    The app injects a single client as both ``ollama`` and ``anthropic``; it must
    answer ``describe_image`` (vision), ``generate`` (assistant/sensor), and
    ``available`` for all three.
    """

    def __init__(self, transcript: str = _TRANSCRIPT, *, available: bool = True) -> None:
        _FakeGen.__init__(self)
        _FakeVision.__init__(self, transcript, available=available)
        # One availability flag drives all three roles (vision + text selection).
        self.available_result = available

    def available(self) -> bool:
        return self._available


# --- module: transcribe_image ----------------------------------------------


def test_transcribe_reads_image_to_text():
    v = _FakeVision("milk\neggs")
    assert transcribe_image("aGk=", "image/png", vision_client=v) == "milk\neggs"
    assert v.calls[0]["media_type"] == "image/png"


def test_transcribe_empty_image_skips_model():
    v = _FakeVision()
    assert transcribe_image("", "image/png", vision_client=v) == ""
    assert v.calls == []


def test_transcribe_unavailable_backend_returns_empty():
    v = _FakeVision(available=False)
    assert transcribe_image("aGk=", "image/png", vision_client=v) == ""
    assert v.calls == []  # gated before the call


def test_transcribe_error_degrades_to_empty():
    """No local fallback ⇒ an erroring backend reads nothing rather than raising."""
    v = _FakeVision(error=True)
    assert transcribe_image("aGk=", "image/png", vision_client=v) == ""


# --- module: plan_vision ----------------------------------------------------


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


def test_plan_vision_fans_transcript_out_to_both_paths(store):
    """Image → transcript → both a validated action and a behavioral candidate."""
    plan = plan_vision(
        "aGk=",
        "image/jpeg",
        store,
        vision_client=_FakeVision(_TRANSCRIPT),
        assistant_client=_FakeGen(),
        sensor_client=_FakeGen(),
    )
    assert plan.transcript == _TRANSCRIPT
    assert [a.op for a in plan.actions] == ["add_todo"]
    assert [c.payload["key"] for c in plan.candidates] == ["preferred_briefing_format"]
    # Planning writes nothing on either half.
    assert store.open_todos() == []
    assert store.list_proposals("pending") == []


def test_plan_vision_unreadable_image_short_circuits(store):
    """A blank transcript ⇒ empty plan, and the brain-dump pipeline isn't touched."""
    plan = plan_vision(
        "aGk=",
        "image/jpeg",
        store,
        vision_client=_FakeVision(""),  # reads nothing
        assistant_client=_FakeGen(),
        sensor_client=_FakeGen(),
    )
    assert plan.transcript == ""
    assert plan.actions == [] and plan.candidates == []


def test_plan_vision_empty_image_short_circuits(store):
    plan = plan_vision(
        "",
        "image/jpeg",
        store,
        vision_client=_FakeVision(),
        assistant_client=_FakeGen(),
        sensor_client=_FakeGen(),
    )
    assert plan.transcript == "" and plan.actions == []


# --- endpoint: POST /vision -------------------------------------------------


@pytest.fixture()
def app_store():
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "tester", display_name="Tester", token=SECRET, is_operator=True)
    try:
        yield unscoped
    finally:
        conn.close()


@pytest.fixture()
def user_store(app_store):
    return app_store.scoped(app_store.get_user("tester")["id"])


def _client(app_store, client):
    app = create_app(store=app_store, settings=Settings(), ollama=client, anthropic=client)
    return TestClient(app)


def test_vision_requires_auth(app_store):
    with _client(app_store, _CombinedFake()) as c:
        assert c.post("/vision", json={"image_base64": "aGk="}).status_code == 401


def test_vision_blank_image_422(app_store):
    with _client(app_store, _CombinedFake()) as c:
        resp = c.post(
            "/vision", json={"image_base64": "   "}, headers={"X-Prefrontal-Token": SECRET}
        )
    assert resp.status_code == 422


def test_vision_unsupported_media_type_422(app_store):
    """An unsupported media type is rejected up-front, not swallowed into an empty
    200 that looks like a blank image."""
    fake = _CombinedFake()
    with _client(app_store, fake) as c:
        resp = c.post(
            "/vision",
            json={"image_base64": "aGk=", "media_type": "image/tiff"},
            headers={"X-Prefrontal-Token": SECRET},
        )
    assert resp.status_code == 422
    assert fake.calls == []  # describe_image never reached


def test_vision_normalizes_media_type_case_and_whitespace(app_store):
    """A padded/upper-case but valid media type is normalized, not rejected."""
    fake = _CombinedFake()
    with _client(app_store, fake) as c:
        resp = c.post(
            "/vision",
            json={"image_base64": "aGk=", "media_type": "  IMAGE/PNG  "},
            headers={"X-Prefrontal-Token": SECRET},
        )
    assert resp.status_code == 200
    assert fake.calls[0]["media_type"] == "image/png"


def test_vision_503_when_anthropic_unavailable(app_store):
    """No vision backend ⇒ 503, not a misleading empty plan."""
    with _client(app_store, _CombinedFake(available=False)) as c:
        resp = c.post(
            "/vision",
            json={"image_base64": "aGk="},
            headers={"X-Prefrontal-Token": SECRET},
        )
    assert resp.status_code == 503


def test_vision_previews_actions_and_records_proposals(app_store, user_store):
    """Image → transcript → actions (preview) + candidates (pending) in one call."""
    with _client(app_store, _CombinedFake()) as c:
        resp = c.post(
            "/vision",
            json={"image_base64": "aGk=", "media_type": "image/png"},
            headers={"X-Prefrontal-Token": SECRET},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["transcript"] == _TRANSCRIPT
        assert len(body["actions"]) == 1 and body["actions"][0]["op"] == "add_todo"
        assert len(body["proposals"]) == 1
        assert body["proposals"][0]["status"] == "pending"
        assert body["provider"]["vision"] == "anthropic"

        # The actionable half wrote nothing yet…
        assert user_store.open_todos() == []
        # …but the candidate is pending in the shared review queue.
        pending = c.get("/proposals", headers={"X-Prefrontal-Token": SECRET}).json()
        assert len(pending["proposals"]) == 1

        # Applying the echoed actions writes the todo (existing confirm path).
        applied = c.post(
            "/assistant/apply",
            json={"actions": body["actions"]},
            headers={"X-Prefrontal-Token": SECRET},
        ).json()
    assert applied["applied"] == 1
    todos = user_store.open_todos()
    assert len(todos) == 1 and todos[0]["title"] == "Call the dentist"


def test_vision_strips_data_uri_prefix(app_store):
    """A ``data:`` URI body is normalized to the bare base64 payload for the SDK."""
    fake = _CombinedFake()
    with _client(app_store, fake) as c:
        c.post(
            "/vision",
            json={"image_base64": "data:image/png;base64,aGk=", "media_type": "image/png"},
            headers={"X-Prefrontal-Token": SECRET},
        )
    assert fake.calls[0]["image"] == "aGk="
