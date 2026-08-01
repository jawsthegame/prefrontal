"""Tests for tolerant LLM-reply JSON extraction (prefrontal.llm_json)."""

from __future__ import annotations

import pytest

from prefrontal.integrations import OllamaError
from prefrontal.llm_json import (
    extract_json,
    extract_json_object,
    generate_json,
    generate_text,
)


def test_extract_json_plain_object():
    assert extract_json('{"reply":"ok","actions":[]}') == {"reply": "ok", "actions": []}


def test_extract_json_fenced_and_trailing_prose():
    text = 'Sure!\n```json\n{"reply":"hi","actions":[]}\n```\nAnything else?'
    assert extract_json(text) == {"reply": "hi", "actions": []}


def test_extract_json_bare_array():
    assert extract_json('[{"op":"drop_todo","todo_id":1}]') == [
        {"op": "drop_todo", "todo_id": 1}
    ]


def test_extract_json_prose_wrapped_array_keeps_all_elements():
    """A prose-wrapped array must not collapse to its first object.

    Regression: the balanced-span matcher tried the '{' span before the '[' span
    regardless of position, so an array wrapped in prose matched the '{' of its
    first element and returned only that object — silently dropping the rest (in
    the assistant, every requested edit after the first). Candidates are now
    ordered by opener position, so the earlier '[' wins.
    """
    text = (
        'Sure, here are the actions: '
        '[{"op":"add_todo","title":"milk"}, {"op":"drop_todo","todo_id":5}]'
    )
    assert extract_json(text) == [
        {"op": "add_todo", "title": "milk"},
        {"op": "drop_todo", "todo_id": 5},
    ]


def test_extract_json_prose_wrapped_object_with_inner_array():
    """An object that merely contains an array still parses as the object."""
    assert extract_json('result: {"reply":"hi","actions":[1,2]} thanks') == {
        "reply": "hi",
        "actions": [1, 2],
    }


def test_extract_json_repairs_raw_newlines_inside_strings():
    """A model writing a multi-paragraph body emits JSON that's invalid by one
    character class. Observed for real (a delegation prep whose whole fenced blob
    ended up stored *as* the brief); one bad newline mustn't cost the answer."""
    text = '```json\n{"brief": "Decide.", "body": "Line one\n\nLine two\n"}\n```'
    got = extract_json_object(text)
    assert got["brief"] == "Decide."
    assert got["body"] == "Line one\n\nLine two\n"


def test_extract_json_repair_leaves_valid_escapes_alone():
    text = '{"a": "already\\nescaped", "b": "tab\\there"}'
    assert extract_json_object(text) == {"a": "already\nescaped", "b": "tab\there"}


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


@pytest.mark.parametrize(
    "reply,expected",
    [('[{"op":"x"}]', [{"op": "x"}]), ('{"a":1}', {"a": 1})],
)
def test_generate_json_handles_object_and_array(reply, expected):
    assert generate_json("p", client=_FakeClient(reply=reply)) == expected


class _RecordingClient:
    """A Generator that declares the full real signature and records its kwargs."""

    def __init__(self, *, reply: str = "{}") -> None:
        self._reply = reply
        self.calls: list[dict[str, object]] = []

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        num_ctx: int | None = None,
        timeout: float | None = None,
        format: object = None,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "num_ctx": num_ctx,
                "timeout": timeout,
                "format": format,
            }
        )
        return self._reply


def test_generate_text_requests_json_mode_when_supported():
    """want_json sets the backend's native JSON format when it accepts ``format``."""
    client = _RecordingClient()
    generate_text(client, "p", system="s", want_json=True)
    assert client.calls[0]["format"] == "json"
    assert client.calls[0]["system"] == "s"


def test_generate_text_passes_num_ctx_and_timeout_when_supported():
    client = _RecordingClient()
    generate_text(client, "p", num_ctx=8192, timeout=30.0)
    assert client.calls[0]["num_ctx"] == 8192
    assert client.calls[0]["timeout"] == 30.0
    assert client.calls[0]["format"] is None  # want_json defaults False


def test_generate_text_omits_unsupported_kwargs_for_minimal_client():
    """A double declaring only ``(prompt, *, system)`` must not get format/num_ctx/
    timeout — passing an unknown keyword would raise TypeError, not degrade."""
    client = _FakeClient(reply="ok")
    got = generate_text(
        client, "p", system="s", num_ctx=9, timeout=1.0, want_json=True
    )
    assert got == "ok"  # no TypeError; the extra kwargs were dropped


def test_generate_json_requests_json_mode():
    """The facade asks for JSON mode, so a capable backend returns valid JSON."""
    client = _RecordingClient(reply='{"a": 1}')
    assert generate_json("p", client=client) == {"a": 1}
    assert client.calls[0]["format"] == "json"
