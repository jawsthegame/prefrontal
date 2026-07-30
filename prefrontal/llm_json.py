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


#: Rough characters-per-token for sizing a context window (English prose ≈ 4).
CHARS_PER_TOKEN = 4


def fit_num_ctx(prompt_chars: int, *, cap: int, reply_tokens: int = 512) -> int | None:
    """Pick a ``num_ctx`` that holds ``prompt_chars`` (plus room to answer), or None.

    Ollama's default context window (~2048 tokens) silently truncates a longer
    prompt *from the front*, which reads as the model ignoring its instructions —
    so any call site that can be handed a large prompt has to size the window
    itself. Returns ``None`` when the prompt fits the default (no need to pay for a
    bigger, slower window), otherwise the next power-of-two step, capped at ``cap``.
    A prompt past ``cap`` needs truncating upstream rather than a window we won't
    grant.

    Args:
        prompt_chars: Total characters going to the model (prompt + system).
        cap: The largest window to ask for.
        reply_tokens: Headroom reserved for the model's own answer.
    """
    est_tokens = prompt_chars // CHARS_PER_TOKEN + reply_tokens
    if est_tokens <= 2048:
        return None
    return min(cap, 1 << (est_tokens - 1).bit_length())


def generate_json(
    prompt: str,
    *,
    system: str | None = None,
    client: Any = None,
) -> dict[str, Any] | list[Any] | None:
    """Ask an LLM for JSON and return the parsed object/array, or ``None``.

    Wraps the "call the model, tolerate a provider failure, pull the JSON out of
    the reply" idiom that otherwise repeats at each call site. Returns ``None`` on
    a provider transport error *or* an unparseable reply, so a caller falls back
    the same way for both.

    Args:
        prompt: The user prompt.
        system: Optional system prompt.
        client: An Ollama- or Anthropic-like
            :class:`~prefrontal.integrations.Generator`. ``None`` (the default)
            uses the local Ollama client built from settings.
    """
    # Imported lazily so this small text utility keeps a light import graph and
    # cannot cycle with the integrations package.
    from prefrontal.integrations import AnthropicError, OllamaError

    if client is None:
        from prefrontal.integrations import OllamaClient

        client = OllamaClient.from_settings()
    try:
        reply = client.generate(prompt, system=system)
    except (OllamaError, AnthropicError):
        return None
    return extract_json(reply)


def _json_candidates(text: str) -> list[str]:
    """Yield progressively looser JSON substrings to attempt parsing."""
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    # First balanced {...} or [...] span, whichever *opener* appears first. Order
    # by opener position, not a fixed {}-before-[] preference: a prose-wrapped bare
    # array ("here are the actions: [{...}, {...}]") would otherwise match the '{'
    # of its first element first and collapse to that single object, dropping the
    # rest of the array.
    spans: list[tuple[int, str]] = []
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
                    spans.append((start, text[start : i + 1]))
                    break
    candidates.extend(span for _, span in sorted(spans))
    # Last resort: the same candidates with un-escaped newlines inside string values
    # repaired. A model writing a multi-paragraph body ("body": "Line one\nLine two"
    # with a *real* newline) emits JSON that is invalid by one character class, and
    # discarding the whole object over it loses a perfectly good answer.
    repaired = [fixed for c in list(candidates) if (fixed := _escape_raw_newlines(c)) != c]
    candidates.extend(repaired)
    return candidates


def _escape_raw_newlines(text: str) -> str:
    """Escape literal newlines/tabs that appear *inside* JSON string literals.

    A tiny scanner rather than a regex, because "inside a string literal" needs the
    quote/backslash state a regex can't track. Text outside strings is untouched, so
    formatting whitespace between fields still parses as before.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string and not escaped and ch in "\n\r\t":
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
            continue
        out.append(ch)
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            in_string = not in_string
    return "".join(out)
