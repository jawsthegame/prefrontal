"""Unit tests for the local Ollama client's on-device vision surface.

The vision-capture flow is local-first: it prefers the on-device multimodal model
and only falls back to the cloud. These tests lock the two pieces that routing
decision rests on — the ``/api/generate`` wire shape ``describe_image`` sends (the
image must ride in the ``images`` array under the *vision* model) and
``can_describe_images`` (a vision model configured *and* installed) — using an
httpx ``MockTransport`` so nothing hits a real server.
"""

from __future__ import annotations

import httpx
import pytest

from prefrontal.integrations.ollama import OllamaClient, OllamaError


def _vision_client(
    handler, *, vision_model: str = "llava", model: str = "llama3.1:8b"
) -> OllamaClient:
    return OllamaClient(
        model=model,
        vision_model=vision_model,
        transport=httpx.MockTransport(handler),
    )


# --- describe_image: the on-device multimodal wire shape --------------------


def test_describe_image_sends_image_under_vision_model():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"response": "  milk\neggs  "})

    out = _vision_client(handler).describe_image(
        "aGVsbG8=", prompt="Transcribe this", media_type="image/png"
    )
    assert out == "milk\neggs"  # stripped
    assert captured["path"] == "/api/generate"
    body = captured["body"]
    assert body["model"] == "llava"  # the *vision* model, not the text model
    assert body["prompt"] == "Transcribe this"
    assert body["images"] == ["aGVsbG8="]  # image rides in the images array
    assert body["stream"] is False


def test_describe_image_maps_max_tokens_to_num_predict():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"response": "ok"})

    _vision_client(handler).describe_image("aGk=", prompt="p", max_tokens=256)
    assert captured["body"]["options"] == {"num_predict": 256}


def test_describe_image_without_vision_model_raises():
    """No vision model configured ⇒ error (so the provider falls back to cloud)."""
    client = OllamaClient(vision_model="")
    with pytest.raises(OllamaError):
        client.describe_image("aGk=", prompt="p")


def test_describe_image_empty_image_raises():
    with pytest.raises(OllamaError):
        _vision_client(lambda r: httpx.Response(200)).describe_image("", prompt="p")


def test_describe_image_wraps_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(OllamaError):
        _vision_client(handler).describe_image("aGk=", prompt="p")


# --- can_describe_images: routing gate --------------------------------------


def _tags_client(names: list[str] | None, *, vision_model: str, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        if status != 200:
            return httpx.Response(status)
        models = [{"name": n} for n in (names or [])]
        return httpx.Response(200, json={"models": models})

    return OllamaClient(
        vision_model=vision_model, transport=httpx.MockTransport(handler)
    )


def test_can_describe_images_false_without_vision_model():
    # No server call needed — an unset vision model can't see, full stop.
    assert OllamaClient(vision_model="").can_describe_images() is False


def test_can_describe_images_true_when_model_installed():
    c = _tags_client(["llama3.1:8b", "llava:latest"], vision_model="llava")
    assert c.can_describe_images() is True  # 'llava' matches 'llava:latest'


def test_can_describe_images_false_when_model_absent():
    c = _tags_client(["llama3.1:8b"], vision_model="llava")
    assert c.can_describe_images() is False


def test_can_describe_images_false_when_server_down():
    c = _tags_client(None, vision_model="llava", status=503)
    assert c.can_describe_images() is False


def test_can_describe_images_matches_exact_tag():
    c = _tags_client(["llava:13b"], vision_model="llava:13b")
    assert c.can_describe_images() is True


def test_can_describe_images_tolerates_non_string_names():
    """An odd /api/tags schema (non-string name, missing key) must not crash the
    later name.split(...); such entries are just ignored."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"models": [{"name": 123}, {"nope": "x"}, "junk", {"name": "llava:latest"}]},
        )

    c = OllamaClient(vision_model="llava", transport=httpx.MockTransport(handler))
    assert c.can_describe_images() is True  # the one valid entry still matches


# --- generate: the thinking-mode flag ---------------------------------------
#
# A hybrid-thinking model (Qwen3, DeepSeek-R1) runs a reasoning pass by default,
# which costs multiples of the answer without improving it for any call site here
# (measured: 64.5s vs 25.8s on the same auto-run loop turn with qwen3:14b). The
# snappy inference paths run on a 10s timeout, so leaving it on times every one of
# them out to a heuristic — hence "off" is the default, and it must actually reach
# the wire.


def _capture(handler_response=None, status=200):
    """A client plus the captured request bodies."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.content))
        if callable(handler_response):
            return handler_response(len(bodies))
        return httpx.Response(status, json={"response": "ok"})

    return OllamaClient(transport=httpx.MockTransport(handler)), bodies


def test_generate_sends_think_false_by_default():
    client, bodies = _capture()
    client.generate("hi")
    assert bodies[0]["think"] is False


def test_generate_think_is_configurable_and_overridable_per_call():
    client, bodies = _capture()
    client.think = True
    client.generate("hi")
    assert bodies[0]["think"] is True
    client.generate("hi", think=False)  # per-call override wins
    assert bodies[1]["think"] is False


def test_from_settings_carries_the_think_setting():
    from prefrontal.config import Settings

    assert OllamaClient.from_settings(Settings()).think is False
    assert OllamaClient.from_settings(Settings(ollama_think=True)).think is True


def test_generate_retries_without_think_when_the_server_rejects_it():
    """An older server rejects `think` for a non-thinking model. Sending the field is
    what keeps a hybrid model fast, so a version quirk must not break generation."""
    def responses(call_number: int) -> httpx.Response:
        if call_number == 1:
            return httpx.Response(400, json={"error": '"think" is not supported'})
        return httpx.Response(200, json={"response": "recovered"})

    client, bodies = _capture(responses)
    assert client.generate("hi") == "recovered"
    assert "think" in bodies[0]
    assert "think" not in bodies[1]  # the retry drops it


def test_generate_still_raises_on_an_unrelated_400():
    client, _bodies = _capture(lambda n: httpx.Response(400, json={"error": "no model"}))
    with pytest.raises(OllamaError):
        client.generate("hi")
