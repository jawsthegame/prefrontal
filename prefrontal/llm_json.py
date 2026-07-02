"""Tolerant extraction of JSON from a model reply.

Local models don't reliably honor "reply with only JSON": they add prose, wrap
the object in ``` fences, or trail a stray sentence. Several call sites (the
editing assistant, the todo augmenter, the decomposer) need the same forgiving
"pull the JSON out of whatever the model said" behavior, so it lives here once
rather than as a copy-pasted ``re.search(r"\\{.*\\}")`` at each site.

:func:`extract_json` returns the first parseable object *or array*; the thin
:func:`extract_json_object` wrapper returns a dict (``{}`` when the reply held no
JSON object), which is what the field-extraction call sites want.
"""

from __future__ import annotations

import json
import re
from typing import Any


def extract_json(text: str) -> dict[str, Any] | list[Any] | None:
    """Pull the first JSON object/array out of a model reply (tolerant of fences).

    Tries the whole string first, then a ```json fenced block, then a
    brace/bracket-matched span. Returns ``None`` if nothing parses.
    """
    text = text.strip()
    if not text:
        return None
    for candidate in _json_candidates(text):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def extract_json_object(text: str) -> dict[str, Any]:
    """Return the first JSON *object* from ``text``, or ``{}`` if there is none."""
    parsed = extract_json(text)
    return parsed if isinstance(parsed, dict) else {}


def _json_candidates(text: str) -> list[str]:
    """Yield progressively looser JSON substrings to attempt parsing."""
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    # First balanced {...} or [...] span, whichever appears first.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break
    return candidates
