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
