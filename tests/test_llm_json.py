"""Tests for tolerant LLM-reply JSON extraction (prefrontal.llm_json)."""

from __future__ import annotations

from prefrontal.llm_json import extract_json, extract_json_object


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
