"""Tests for the native delivery client (integrations/delivery.py).

The HTTP transports are exercised with an ``httpx.MockTransport`` so no real
ntfy/Pushover server is contacted, matching ``test_summarizer``'s Ollama tests.
Covers: payload shape per transport, the local-first no-op when nothing is
configured, per-user routing over operator defaults, and the channel→transport
routing (including held digests, action-button attachment, and voice→TTS).
"""

from __future__ import annotations

import httpx
import pytest

from prefrontal.coaching import Cue, Decision
from prefrontal.config import Settings
from prefrontal.integrations.delivery import (
    DeliveryClient,
    DeliveryResult,
    NtfyClient,
    PushoverClient,
    Route,
    resolve_route,
)
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.oauth import verify_action
from tests.conftest import scoped_default

BASE = "https://agent-1.tail8b0a.ts.net"
SIGNING = "delivery-signing-key"


@pytest.fixture
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


def _decision(channel: str, *, context_key: str = "todo", ref: dict | None = None) -> Decision:
    cue = Cue(
        module="location_anchor",
        intervention="tiny_first_step",
        urgency="nudge",
        text="Still out — heading back?",
        context_key=context_key,
        dedup_key="d1",
        ref=ref or {},
    )
    return Decision(cue=cue, channel=channel, text=cue.text)


# -- NtfyClient ---------------------------------------------------------------


def test_ntfy_publish_builds_json_and_reports_success():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"id": "abc"})

    client = NtfyClient(transport=httpx.MockTransport(handler))
    actions = [{"action": "http", "label": "I'm back", "url": f"{BASE}/nudge/act?t=x"}]
    result = client.publish(
        "https://ntfy.sh", "prefrontal-me", "tok",
        title="Prefrontal", message="hi", priority=4, actions=actions,
    )

    assert result.delivered is True
    assert result.transport == "ntfy"
    assert captured["url"] == "https://ntfy.sh/"
    assert captured["auth"] == "Bearer tok"
    assert captured["body"] == {
        "topic": "prefrontal-me",
        "title": "Prefrontal",
        "message": "hi",
        "priority": 4,
        "actions": actions,
    }


def test_ntfy_no_op_without_topic():
    called = False

    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(200)

    client = NtfyClient(transport=httpx.MockTransport(handler))
    result = client.publish("https://ntfy.sh", "", title="t", message="m")
    assert result.delivered is False
    assert called is False  # nothing left the host
    assert "no server/topic" in result.detail


def test_ntfy_transport_error_is_reported_not_raised():
    def boom(request):
        raise httpx.ConnectError("refused")

    client = NtfyClient(transport=httpx.MockTransport(boom))
    result = client.publish("https://ntfy.sh", "topic", title="t", message="m")
    assert result.delivered is False
    assert "failed" in result.detail


# -- PushoverClient -----------------------------------------------------------


def test_pushover_publish_posts_form_and_supplementary_url():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"status": 1})

    client = PushoverClient(transport=httpx.MockTransport(handler))
    result = client.publish(
        "app-tok", "user-key",
        title="Prefrontal", message="hi", priority=1,
        url=f"{BASE}/nudge/act?t=x", url_title="I'm back",
    )

    assert result.delivered is True
    assert result.transport == "pushover"
    assert captured["url"] == PushoverClient.API_URL
    assert "token=app-tok" in captured["body"]
    assert "user=user-key" in captured["body"]
    assert "priority=1" in captured["body"]
    assert "url_title=I%27m+back" in captured["body"]


def test_pushover_no_op_without_credentials():
    called = False

    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(200)

    client = PushoverClient(transport=httpx.MockTransport(handler))
    assert client.publish("", "user", title="t", message="m").delivered is False
    assert client.publish("tok", "", title="t", message="m").delivered is False
    assert called is False


# -- resolve_route ------------------------------------------------------------


def test_resolve_route_uses_operator_defaults(store):
    settings = Settings(
        ntfy_topic="op-topic", ntfy_server="https://ntfy.example", pushover_token="op-tok"
    )
    route = resolve_route(store, settings)
    assert route.ntfy_topic == "op-topic"
    assert route.ntfy_server == "https://ntfy.example"
    assert route.pushover_token == "op-tok"
    assert route.tts_enabled is False


def test_resolve_route_per_user_overrides_operator(store):
    store.set_state("ntfy_topic", "tom-private", source="explicit")
    store.set_state("pushover_user_key", "tom-key", source="explicit")
    store.set_state("tts_enabled", "on", source="explicit")
    settings = Settings(ntfy_topic="op-topic", pushover_user_key="op-key")
    route = resolve_route(store, settings)
    assert route.ntfy_topic == "tom-private"       # per-user wins
    assert route.pushover_user_key == "tom-key"
    assert route.tts_enabled is True               # coaching-state bool honored


# -- DeliveryClient routing ---------------------------------------------------


def _mock_client(handler) -> DeliveryClient:
    transport = httpx.MockTransport(handler)
    return DeliveryClient(
        ntfy=NtfyClient(transport=transport),
        pushover=PushoverClient(transport=transport),
    )


def test_digest_channel_is_held_not_sent():
    client = _mock_client(lambda r: httpx.Response(200))
    result = client.deliver(_decision("digest"), Route(ntfy_topic="t"))
    assert result.delivered is False
    assert result.transport == "none"
    assert result.detail == "held for digest"


def test_push_prefers_ntfy_and_stamps_channel_and_priority():
    captured: dict = {}

    def handler(request):
        import json

        captured["body"] = json.loads(request.read())
        return httpx.Response(200)

    client = _mock_client(handler)
    route = Route(ntfy_topic="prefrontal-me")
    result = client.deliver(_decision("sound"), route)

    assert result.transport == "ntfy"
    assert result.channel == "sound"           # channel stamped by the router
    assert captured["body"]["priority"] == 4   # sound → ntfy high


def test_outing_cue_attaches_signed_action_buttons():
    captured: dict = {}

    def handler(request):
        import json

        captured["body"] = json.loads(request.read())
        return httpx.Response(200)

    client = _mock_client(handler)
    decision = _decision("push", context_key="outing", ref={"outing_id": 7})
    client.deliver(decision, Route(ntfy_topic="me"), base_url=BASE, secret=SIGNING, handle="tom")

    actions = captured["body"]["actions"]
    assert [a["label"] for a in actions] == ["I'm back", "Abandon"]
    # The button URL is a signed one-tap /nudge/act link for this outing.
    token = actions[0]["url"].split("t=", 1)[1]
    assert verify_action(token, SIGNING) == ("tom", "outing_return", 7)


def test_self_care_cue_attaches_meal_buttons():
    """A meal cue's synthetic date target drives signed Ate / Snooze buttons."""
    captured: dict = {}

    def handler(request):
        import json

        captured["body"] = json.loads(request.read())
        return httpx.Response(200)

    client = _mock_client(handler)
    decision = _decision("push", context_key="meal", ref={"target": 20260703})
    client.deliver(decision, Route(ntfy_topic="me"), base_url=BASE, secret=SIGNING, handle="tom")

    actions = captured["body"]["actions"]
    assert [a["label"] for a in actions] == ["✓ Ate", "Snooze"]
    token = actions[0]["url"].split("t=", 1)[1]
    assert verify_action(token, SIGNING) == ("tom", "meal_ate", 20260703)


def test_falls_back_to_pushover_when_no_ntfy_topic():
    captured: dict = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode()
        return httpx.Response(200)

    client = _mock_client(handler)
    route = Route(pushover_token="tok", pushover_user_key="key")
    decision = _decision("push", context_key="outing", ref={"outing_id": 7})
    result = client.deliver(decision, route, base_url=BASE, secret=SIGNING, handle="tom")

    assert result.transport == "pushover"
    assert captured["url"] == PushoverClient.API_URL
    # No inline buttons on Pushover — the first action rides as a supplementary URL.
    assert "url=" in captured["body"] and "nudge%2Fact" in captured["body"]


def test_no_transport_configured_is_a_clean_no_op():
    client = _mock_client(lambda r: httpx.Response(200))
    result = client.deliver(_decision("push"), Route())
    assert result.delivered is False
    assert result.transport == "none"
    assert result.detail == "no transport configured"


def test_voice_speaks_locally_when_tts_enabled():
    class _FakeTTS:
        def __init__(self):
            self.spoken = None

        def speak(self, message, *, enabled):
            self.spoken = (message, enabled)
            return DeliveryResult(
                channel="voice", transport="tts", delivered=True, detail="spoken locally"
            )

    tts = _FakeTTS()
    client = DeliveryClient(tts=tts)
    result = client.deliver(_decision("voice", context_key="todo"), Route(tts_enabled=True))

    assert result.transport == "tts"
    assert result.delivered is True
    assert tts.spoken == ("Still out — heading back?", True)


def test_deliver_all_returns_one_result_per_decision():
    client = _mock_client(lambda r: httpx.Response(200))
    route = Route(ntfy_topic="me")
    results = client.deliver_all([_decision("push"), _decision("digest")], route)
    assert [r.transport for r in results] == ["ntfy", "none"]
