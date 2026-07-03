"""Tests for tolerant LLM-reply JSON extraction (prefrontal.llm_json)."""

from __future__ import annotations

import pytest

from prefrontal.integrations import OllamaError
from prefrontal.llm_json import extract_json, extract_json_object, generate_json


def test_extract_json_plain_object():
    assert extract_json('{"reply":"ok","actions":[]}') == {"reply": "ok", "actions": []}


def test_extract_json_fenced_and_trailing_prose():
    text = 'Sure!\n```json\n{"reply":"hi","actions":[]}\n```\nAnything else?'
    assert extract_json(text) == {"reply": "hi", "actions": []}


def test_extract_json_bare_array():
    assert extract_json('[{"op":"drop_todo","todo_id":1}]') == [
        {"op": "drop_todo", "todo_id": 1}
    ]


def test_extract_json_garbage_returns_none():
    assert extract_json("I can't help with that.") is None


def test_extract_json_object_wrapper():
    """extract_json_object returns a dict, or {} for a non-object reply."""
    assert extract_json_object('prose {"a": 1} more') == {"a": 1}
    assert extract_json_object("[1, 2, 3]") == {}   # array is not an object
    assert extract_json_object("nope") == {}


class _FakeClient:
    """A Generator stub: replays a canned reply, or raises to simulate a failure."""

    def __init__(self, *, reply: str = "", error: Exception | None = None) -> None:
        self._reply = reply
        self._error = error

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        if self._error is not None:
            raise self._error
        return self._reply


def test_generate_json_parses_a_reply():
    client = _FakeClient(reply='Here you go:\n```json\n{"reply":"hi","actions":[]}\n```')
    assert generate_json("hi", client=client) == {"reply": "hi", "actions": []}


def test_generate_json_returns_none_on_provider_error():
    client = _FakeClient(error=OllamaError("model down"))
    assert generate_json("hi", client=client) is None


def test_generate_json_returns_none_on_unparseable_reply():
    client = _FakeClient(reply="sorry, I can't do that")
    assert generate_json("hi", client=client) is None


@pytest.mark.parametrize("reply", ['[{"op":"x"}]', '{"a":1}'])
def test_generate_json_handles_object_and_array(reply):
    assert generate_json("p", client=_FakeClient(reply=reply)) is not None
