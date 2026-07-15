"""Unit tests for the optional Claude client at the SDK boundary.

The rest of the suite exercises :class:`AnthropicClient` only through domain-level
boundary fakes (a stand-in with ``generate``/``describe_image``/``available``).
That never checks the one thing this client alone is responsible for: the *wire
shape* it hands the ``anthropic`` SDK — in particular the multimodal image block
:meth:`~AnthropicClient.describe_image` builds, which a text fake can't cover.

So these tests install a fake ``anthropic`` module in ``sys.modules`` (the client
imports it lazily) and assert on the exact ``messages.create`` payload, plus the
shared text-extraction / refusal / error behavior. No network, no real SDK.
"""

from __future__ import annotations

import sys
import types

import pytest

from prefrontal.integrations.anthropic import (
    AnthropicClient,
    AnthropicError,
)


class _FakeAnthropicError(Exception):
    """Stand-in for ``anthropic.AnthropicError`` (the SDK's base error)."""


class _Block:
    """A content block with a ``type`` and (for text blocks) ``text``."""

    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class _Response:
    """A minimal ``messages.create`` response: ``content`` + ``stop_reason``."""

    def __init__(self, content: list[_Block], stop_reason: str | None = "end_turn") -> None:
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, parent: _FakeClientSDK) -> None:
        self._parent = parent

    def create(self, **kwargs: object) -> _Response:
        self._parent.calls.append(kwargs)
        if self._parent.raise_error:
            raise _FakeAnthropicError("boom")
        return self._parent.response


class _FakeClientSDK:
    """Fake ``anthropic.Anthropic()`` capturing the payload it's called with."""

    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs
        self.messages = _FakeMessages(self)

    # Class-level knobs, set by the fixture per test.
    calls: list[dict[str, object]] = []
    response: _Response = _Response([_Block("text", "ok")])
    raise_error: bool = False


@pytest.fixture()
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a fake ``anthropic`` module the lazily-imported client will pick up."""
    module = types.ModuleType("anthropic")

    # Each test gets a fresh capture list / default response on the SDK class.
    _FakeClientSDK.calls = []
    _FakeClientSDK.response = _Response([_Block("text", "ok")])
    _FakeClientSDK.raise_error = False

    module.Anthropic = _FakeClientSDK  # type: ignore[attr-defined]
    module.AnthropicError = _FakeAnthropicError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return module


def _client() -> AnthropicClient:
    return AnthropicClient(api_key="k", model="claude-test", max_tokens=512)


# --- describe_image: the multimodal wire shape -----------------------------


def test_describe_image_builds_image_then_text_content(fake_sdk):
    """The request carries one image block (base64 source) then the text prompt."""
    _FakeClientSDK.response = _Response([_Block("text", "  milk\neggs  ")])
    out = _client().describe_image(
        "aGVsbG8=", prompt="Transcribe this", media_type="image/png"
    )
    assert out == "milk\neggs"  # concatenated text blocks, stripped

    (kwargs,) = _FakeClientSDK.calls
    assert kwargs["model"] == "claude-test"
    content = kwargs["messages"][0]["content"]
    assert content[0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
    }
    assert content[1] == {"type": "text", "text": "Transcribe this"}


def test_describe_image_max_tokens_override(fake_sdk):
    """An explicit max_tokens overrides the client default for that call."""
    _client().describe_image("aGVsbG8=", prompt="p", max_tokens=4096)
    assert _FakeClientSDK.calls[0]["max_tokens"] == 4096


def test_describe_image_defaults_max_tokens_to_client(fake_sdk):
    """Without an override the client's configured max_tokens is used."""
    _client().describe_image("aGVsbG8=", prompt="p")
    assert _FakeClientSDK.calls[0]["max_tokens"] == 512


def test_describe_image_passes_system_when_given(fake_sdk):
    _client().describe_image("aGVsbG8=", prompt="p", system="be terse")
    assert _FakeClientSDK.calls[0]["system"] == "be terse"


def test_describe_image_refusal_returns_empty(fake_sdk):
    """A safety refusal yields '' so callers fall back rather than parse it."""
    _FakeClientSDK.response = _Response([], stop_reason="refusal")
    assert _client().describe_image("aGVsbG8=", prompt="p") == ""


def test_describe_image_wraps_sdk_error(fake_sdk):
    """An SDK error becomes AnthropicError so callers can degrade."""
    _FakeClientSDK.raise_error = True
    with pytest.raises(AnthropicError):
        _client().describe_image("aGVsbG8=", prompt="p")


def test_describe_image_rejects_unsupported_media_type(fake_sdk):
    """An unsupported type fails up-front — no request is made."""
    with pytest.raises(AnthropicError):
        _client().describe_image("aGVsbG8=", prompt="p", media_type="image/tiff")
    assert _FakeClientSDK.calls == []


def test_describe_image_rejects_empty_image(fake_sdk):
    with pytest.raises(AnthropicError):
        _client().describe_image("", prompt="p")
    assert _FakeClientSDK.calls == []


def test_describe_image_without_key_raises_before_import():
    """No key ⇒ AnthropicError without needing the SDK at all."""
    with pytest.raises(AnthropicError):
        AnthropicClient(api_key="").describe_image("aGVsbG8=", prompt="p")


# --- generate: shared text extraction, locked at the wire too --------------


def test_generate_concatenates_text_blocks(fake_sdk):
    _FakeClientSDK.response = _Response([_Block("text", "a"), _Block("text", "b")])
    assert _client().generate("hi") == "ab"
    content = _FakeClientSDK.calls[0]["messages"][0]["content"]
    assert content == "hi"  # text-only request keeps the simple string content


def test_generate_refusal_returns_empty(fake_sdk):
    _FakeClientSDK.response = _Response([], stop_reason="refusal")
    assert _client().generate("hi") == ""


# --- available(): no network, gated on key + SDK import --------------------


def test_available_false_without_key():
    assert AnthropicClient(api_key="").available() is False


def test_available_true_with_key_and_sdk(fake_sdk):
    assert AnthropicClient(api_key="k").available() is True
