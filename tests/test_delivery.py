"""Tests for the native delivery client (integrations/delivery.py).

The HTTP transports are exercised with an ``httpx.MockTransport`` so no real
server is contacted, matching ``test_summarizer``'s Ollama tests. Native APNs is
the product push path (covered in ``test_apns.py``); the ntfy transport here is
the dev-only shim, so its routing tests construct the client with
``ntfy_dev=True``. Covers: payload shape, the local-first no-op when nothing is
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
    Route,
    TwilioVoiceClient,
    resolve_route,
    voice_twiml,
)
from prefrontal.memory.store import MemoryStore
from prefrontal.webhooks.oauth import verify_action
from tests.conftest import scoped_default

BASE = "https://mac-mini.tailnet.ts.net"
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


def test_ntfy_publish_includes_icon_and_click_when_set():
    """A branded push carries the app icon and a default tap target."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.read())
        return httpx.Response(200)

    client = NtfyClient(transport=httpx.MockTransport(handler))
    client.publish(
        "https://ntfy.sh", "prefrontal-me",
        title="Prefrontal", message="hi",
        icon="https://example.com/icon.png", click=f"{BASE}/dashboard",
    )
    assert captured["body"]["icon"] == "https://example.com/icon.png"
    assert captured["body"]["click"] == f"{BASE}/dashboard"


def test_ntfy_publish_omits_icon_and_click_when_empty():
    """Empty icon/click stay out of the payload (unbranded/plain push)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.read())
        return httpx.Response(200)

    client = NtfyClient(transport=httpx.MockTransport(handler))
    client.publish("https://ntfy.sh", "prefrontal-me", title="Prefrontal", message="hi")
    assert "icon" not in captured["body"]
    assert "click" not in captured["body"]


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


# -- resolve_route ------------------------------------------------------------


def test_resolve_route_uses_operator_defaults(store):
    settings = Settings(ntfy_topic="op-topic", ntfy_server="https://ntfy.example")
    route = resolve_route(store, settings)
    assert route.ntfy_topic == "op-topic"
    assert route.ntfy_server == "https://ntfy.example"
    assert route.tts_enabled is False


def test_resolve_route_per_user_overrides_operator(store):
    store.set_state("ntfy_topic", "tom-private", source="explicit")
    store.set_state("apns_token", "tom-device", source="explicit")
    store.set_state("tts_enabled", "on", source="explicit")
    settings = Settings(ntfy_topic="op-topic")
    route = resolve_route(store, settings)
    assert route.ntfy_topic == "tom-private"       # per-user wins
    assert route.apns_token == "tom-device"        # native-push target, per-user
    assert route.tts_enabled is True               # coaching-state bool honored


def test_resolve_route_withholds_operator_target_in_multi_user(store):
    # A second active user makes this a multi-user box: the operator's default
    # topic is one person's device, so an unprovisioned user must NOT inherit it
    # (that would send their private nudges to someone else). Server/icon — not a
    # device target — still default.
    store.create_user("second", display_name="Second")
    settings = Settings(
        ntfy_topic="op-topic",
        ntfy_server="https://ntfy.example",
        ntfy_icon="https://op.example/icon.png",
    )
    route = resolve_route(store, settings)
    assert route.ntfy_topic == ""            # withheld — no cross-account fallback
    assert route.ntfy_token == ""
    assert route.ntfy_server == "https://ntfy.example"      # not a target — kept
    assert route.ntfy_icon == "https://op.example/icon.png"  # not a target — kept


def test_resolve_route_multi_user_still_honors_per_user_target(store):
    # Multi-user, but this user HAS their own routing → it is used unchanged.
    store.create_user("second", display_name="Second")
    store.set_state("ntfy_topic", "tom-private", source="explicit")
    settings = Settings(ntfy_topic="op-topic")
    assert resolve_route(store, settings).ntfy_topic == "tom-private"


def test_resolve_route_icon_defaults_to_settings_and_overrides_per_user(store):
    settings = Settings(ntfy_icon="https://op.example/icon.png")
    assert resolve_route(store, settings).ntfy_icon == "https://op.example/icon.png"
    store.set_state("ntfy_icon", "https://tom.example/icon.png", source="explicit")
    assert resolve_route(store, settings).ntfy_icon == "https://tom.example/icon.png"


def _deliver_capture(route, **kw):
    captured: dict = {}

    def handler(request):
        import json

        captured["body"] = json.loads(request.read())
        return httpx.Response(200)

    _mock_client(handler).deliver(_decision("push"), route, **kw)
    return captured["body"]


def test_deliver_defaults_icon_and_click_to_the_box_origin():
    """With no explicit icon, the box serves its own from base_url (works private)."""
    body = _deliver_capture(Route(ntfy_topic="me"), base_url=BASE)
    assert body["icon"] == f"{BASE}/brand/app-icon.png"
    assert body["click"] == f"{BASE}/dashboard"


def test_deliver_explicit_route_icon_overrides_box_default():
    """A per-user/operator hosted icon wins over the box-served default."""
    route = Route(ntfy_topic="me", ntfy_icon="https://brand.example/icon.png")
    body = _deliver_capture(route, base_url=BASE)
    assert body["icon"] == "https://brand.example/icon.png"


def test_deliver_omits_icon_and_click_without_base_url():
    """No public origin and no explicit icon → a plain push (nothing to point at)."""
    body = _deliver_capture(Route(ntfy_topic="me"))
    assert "icon" not in body
    assert "click" not in body


def test_deliver_title_carries_brand_emoji():
    """The title leads with 🧠 — the brand cue that renders even on iOS, where
    ntfy ignores the icon."""
    body = _deliver_capture(Route(ntfy_topic="me"))
    assert body["title"] == "🧠 Prefrontal"


# -- DeliveryClient routing ---------------------------------------------------


def _mock_client(handler) -> DeliveryClient:
    # ntfy_dev=True so these tests exercise the ntfy dev shim (the only path that
    # renders the captured JSON body / action buttons); native APNs is covered in
    # test_apns.py.
    transport = httpx.MockTransport(handler)
    return DeliveryClient(
        ntfy=NtfyClient(transport=transport),
        voice=TwilioVoiceClient(transport=transport),
        ntfy_dev=True,
    )


def test_digest_channel_is_held_not_sent():
    client = _mock_client(lambda r: httpx.Response(200))
    result = client.deliver(_decision("digest"), Route(ntfy_topic="t"))
    assert result.delivered is False
    assert result.transport == "none"
    assert result.detail == "held for digest"


def test_dev_shim_push_uses_ntfy_and_stamps_channel_and_priority():
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


def test_deliver_drops_click_when_the_nudge_has_action_buttons():
    """A nudge with buttons omits ``click`` so a body tap can't preempt the buttons."""
    captured: dict = {}

    def handler(request):
        import json

        captured["body"] = json.loads(request.read())
        return httpx.Response(200)

    client = _mock_client(handler)
    decision = _decision("push", context_key="outing", ref={"outing_id": 7})
    client.deliver(decision, Route(ntfy_topic="me"), base_url=BASE, secret=SIGNING, handle="tom")

    assert captured["body"]["actions"]        # buttons are present
    assert "click" not in captured["body"]    # …so the body tap does nothing


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


def test_self_care_cue_attaches_meds_buttons():
    """A meds cue drives signed Took / Snooze buttons and drops the dashboard click.

    Regression: ``meds`` was absent from the delivery context→kind maps (unlike
    ``meal``/``water``), so a meds nudge got no buttons and fell back to opening
    the dashboard — the reported "it still navigates to the dashboard" symptom.
    """
    captured: dict = {}

    def handler(request):
        import json

        captured["body"] = json.loads(request.read())
        return httpx.Response(200)

    client = _mock_client(handler)
    decision = _decision("push", context_key="meds", ref={"target": 20260703})
    client.deliver(decision, Route(ntfy_topic="me"), base_url=BASE, secret=SIGNING, handle="tom")

    actions = captured["body"]["actions"]
    assert [a["label"] for a in actions] == ["✓ Took", "Snooze"]
    token = actions[0]["url"].split("t=", 1)[1]
    assert verify_action(token, SIGNING) == ("tom", "meds_took", 20260703)
    # buttons present → the notification body no longer opens the dashboard
    assert "click" not in captured["body"]


def test_morning_prep_cue_attaches_set_alarm_button():
    """The evening morning-prep nudge carries a client-side Set-alarm view button
    built from its ref (no signing needed), deep-linking to the iOS Shortcut."""
    captured: dict = {}

    def handler(request):
        import json

        captured["body"] = json.loads(request.read())
        return httpx.Response(200)

    client = _mock_client(handler)
    decision = _decision(
        "push",
        context_key="morning_prep",
        ref={"alarm_at": "06:15", "alarm_shortcut": "Set Alarm"},
    )
    # No base_url / secret needed — the button is a client-side view action.
    client.deliver(decision, Route(ntfy_topic="me"))

    actions = captured["body"]["actions"]
    assert len(actions) == 1
    assert actions[0]["action"] == "view" and actions[0]["label"] == "⏰ Set alarm"
    assert actions[0]["url"] == (
        "shortcuts://run-shortcut?name=Set%20Alarm&input=text&text=06%3A15"
    )


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


# -- voice → Twilio call (the native 150% escalation) ------------------------


def _twilio_route(**kw) -> Route:
    base = dict(
        twilio_account_sid="AC123", twilio_auth_token="tok",
        twilio_from="+15550001111", twilio_to="+15559998888",
    )
    base.update(kw)
    return Route(**base)


def test_voice_twiml_escapes_and_wraps():
    assert voice_twiml("back now") == "<Response><Say>back now</Say></Response>"
    # A stray & / < can't malform the payload.
    assert voice_twiml("A & B <x>") == "<Response><Say>A &amp; B &lt;x&gt;</Say></Response>"


def test_voice_places_twilio_call_when_configured():
    captured: dict = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode()
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(201)  # Twilio returns 201 Created on a queued call

    client = _mock_client(handler)
    result = client.deliver(_decision("voice", context_key="outing"), _twilio_route())

    assert result.transport == "twilio" and result.delivered is True
    assert result.channel == "voice" and result.status_code == 201
    assert captured["url"] == "https://api.twilio.com/2010-04-01/Accounts/AC123/Calls.json"
    # From/To and the spoken TwiML all ride in the form body; auth is basic.
    assert "From=%2B15550001111" in captured["body"]
    assert "To=%2B15559998888" in captured["body"]
    assert "Say" in captured["body"] and captured["auth"].startswith("Basic ")


def test_voice_prefers_local_tts_over_calling(store):
    """TTS (you're at the machine) wins over a call when both are available."""
    class _FakeTTS:
        def speak(self, message, *, enabled):
            return DeliveryResult(channel="voice", transport="tts", delivered=True)

    called = False

    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(201)

    client = DeliveryClient(
        tts=_FakeTTS(), voice=TwilioVoiceClient(transport=httpx.MockTransport(handler))
    )
    result = client.deliver(_decision("voice"), _twilio_route(tts_enabled=True))
    assert result.transport == "tts" and called is False  # never dialed


def test_voice_falls_back_to_push_when_twilio_unconfigured():
    """No Twilio (and no TTS) → a voice cue still lands as a max-priority ntfy push."""
    captured: dict = {}

    def handler(request):
        import json

        captured["body"] = json.loads(request.read())
        return httpx.Response(200)

    client = _mock_client(handler)
    result = client.deliver(_decision("voice"), Route(ntfy_topic="me"))
    assert result.transport == "ntfy" and result.delivered is True
    assert captured["body"]["priority"] == 5  # voice → ntfy max/urgent


def test_voice_call_no_op_without_credentials():
    called = False

    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(201)

    result = TwilioVoiceClient(transport=httpx.MockTransport(handler)).call(
        "", "", sender="", to="", message="hi"
    )
    assert result.delivered is False and called is False
    assert result.transport == "twilio" and "not configured" in result.detail


def test_voice_call_rejects_invalid_recipient():
    called = False

    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(201)

    result = TwilioVoiceClient(transport=httpx.MockTransport(handler)).call(
        "AC123", "tok", sender="+15550001111", to="not-a-number", message="hi"
    )
    assert result.delivered is False and called is False  # never dialed a bad number


def test_voice_call_transport_error_is_reported_not_raised():
    def handler(request):
        raise httpx.ConnectError("network down")

    result = TwilioVoiceClient(transport=httpx.MockTransport(handler)).call(
        "AC123", "tok", sender="+15550001111", to="+15559998888", message="hi"
    )
    assert result.delivered is False and "failed" in result.detail


def test_resolve_route_twilio_recipient_is_per_user_target(store):
    # Account creds + caller-ID are the operator's (non-targeting → always default);
    # the recipient number is per-user, honored from coaching_state.
    store.set_state("twilio_to", "+15551234567", source="explicit")
    settings = Settings(
        twilio_account_sid="AC", twilio_auth_token="tok",
        twilio_from="+15550001111", twilio_to="+15550000000",
    )
    route = resolve_route(store, settings)
    assert route.twilio_account_sid == "AC" and route.twilio_from == "+15550001111"
    assert route.twilio_to == "+15551234567"  # per-user wins


def test_resolve_route_withholds_twilio_recipient_in_multi_user(store):
    # The recipient number is a device target, so on a multi-user box an
    # unprovisioned user must NOT inherit the operator's — that would ring the
    # wrong phone. The shared account creds still default.
    store.create_user("second", display_name="Second")
    settings = Settings(
        twilio_account_sid="AC", twilio_auth_token="tok",
        twilio_from="+15550001111", twilio_to="+15550000000",
    )
    route = resolve_route(store, settings)
    assert route.twilio_to == ""                     # withheld — no wrong-phone call
    assert route.twilio_account_sid == "AC"          # shared account cred — kept
    assert route.twilio_from == "+15550001111"


def test_deliver_all_returns_one_result_per_decision():
    client = _mock_client(lambda r: httpx.Response(200))
    route = Route(ntfy_topic="me")
    results = client.deliver_all([_decision("push"), _decision("digest")], route)
    assert [r.transport for r in results] == ["ntfy", "none"]
