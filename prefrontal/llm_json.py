"""Tolerant extraction of JSON from a model reply.

Local models don't reliably honor "reply with only JSON": they add prose, wrap
the object in ``` fences, or trail a stray sentence. Several call sites (the
editing assistant, the todo augmenter, the decomposer) need the same forgiving
"pull the JSON out of whatever the model said" behavior, so it lives here once
rather than as a copy-pasted ``re.search(r"\\{.*\\}")`` at each site.

:func:`extract_json` returns the first parseable object *or array*; the thin
:func:`extract_json_object` wrapper returns a dict (``{}`` when the reply held no
JSON object), which is what the field-extraction call sites want.

The tolerant extractor is the *safety net*, not the first line of defence:
:func:`generate_text` (and the :func:`generate_json` facade over it) asks the
backend for its native JSON mode via the ``format`` argument, so a capable local
model (Ollama's structured output) emits a single valid JSON value and the
extractor parses it on the first, whole-string candidate. It's requested through
a signature-safe shim so a backend — or an injected test double — that doesn't
accept ``format`` is simply called without it and the extractor still recovers
the object, exactly as before.
"""

from __future__ import annotations

import functools
import inspect
import json
import re
from typing import Any

#: Ollama's JSON-mode sentinel: force the reply to a single valid JSON value.
#: Passed as the ``format`` argument to a :class:`~prefrontal.integrations.Generator`
#: that supports it (the local Ollama client); ignored by ones that don't.
JSON_FORMAT = "json"


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


@functools.cache
def _accepted_kwargs(generate: Any) -> frozenset[str] | None:
    """Keyword params ``generate`` accepts, or ``None`` if it takes ``**kwargs``.

    Cached per ``generate`` function (stable per client class) so the reflection
    happens once. Used by :func:`generate_text` to pass only the optional
    arguments a given backend actually declares — the real clients take
    ``num_ctx``/``timeout``/``format``, but a minimal injected test double may
    accept nothing beyond ``system``, and calling it with an unknown keyword
    would raise ``TypeError`` rather than degrade.
    """
    try:
        params = inspect.signature(generate).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return frozenset()
    names: set[str] = set()
    for name, param in params.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return None  # **kwargs — accepts anything
        if param.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            names.add(name)
    return frozenset(names)


def generate_text(
    client: Any,
    prompt: str,
    *,
    system: str | None = None,
    num_ctx: int | None = None,
    timeout: float | None = None,
    want_json: bool = False,
) -> str:
    """Call ``client.generate`` passing only the optional args it declares.

    A signature-safe wrapper: the real backends accept ``num_ctx``/``timeout`` and
    (for the local Ollama client) ``format``, but many injected test doubles — and
    any future minimal :class:`~prefrontal.integrations.Generator` — declare only
    ``system``. This funnels the JSON call sites through one place that requests
    Ollama's native JSON mode (``want_json``) where supported and silently omits
    any argument a given backend doesn't take, so the tolerant extractor keeps
    covering the rest. Returns the raw reply text; provider errors propagate to the
    caller (each site owns its own fallback).

    Args:
        client: The :class:`~prefrontal.integrations.Generator` to call.
        prompt: The user prompt.
        system: Optional system prompt.
        num_ctx: Optional Ollama context-window hint (see
            :meth:`~prefrontal.integrations.ollama.OllamaClient.generate`).
        timeout: Optional per-call timeout override (seconds).
        want_json: Request the backend's native JSON mode when it supports a
            ``format`` argument.
    """
    accepted = _accepted_kwargs(type(client).generate)

    def takes(name: str) -> bool:
        return accepted is None or name in accepted

    kwargs: dict[str, Any] = {}
    if system is not None and takes("system"):
        kwargs["system"] = system
    if num_ctx is not None and takes("num_ctx"):
        kwargs["num_ctx"] = num_ctx
    if timeout is not None and takes("timeout"):
        kwargs["timeout"] = timeout
    if want_json and takes("format"):
        kwargs["format"] = JSON_FORMAT
    return client.generate(prompt, **kwargs)


def generate_json(
    prompt: str,
    *,
    system: str | None = None,
    client: Any = None,
    num_ctx: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any] | list[Any] | None:
    """Ask an LLM for JSON and return the parsed object/array, or ``None``.

    Wraps the "call the model, tolerate a provider failure, pull the JSON out of
    the reply" idiom that otherwise repeats at each call site. Requests the
    backend's native JSON mode (via :func:`generate_text`), so a capable model
    returns a single valid JSON value the extractor parses directly; the tolerant
    :func:`extract_json` still covers a backend that ignores the hint. Returns
    ``None`` on a provider transport error *or* an unparseable reply, so a caller
    falls back the same way for both.

    Args:
        prompt: The user prompt.
        system: Optional system prompt.
        client: An Ollama- or Anthropic-like
            :class:`~prefrontal.integrations.Generator`. ``None`` (the default)
            uses the local Ollama client built from settings.
        num_ctx: Optional Ollama context-window hint for a large prompt.
        timeout: Optional per-call timeout override (seconds).
    """
    # Imported lazily so this small text utility keeps a light import graph and
    # cannot cycle with the integrations package.
    from prefrontal.integrations import AnthropicError, OllamaError

    if client is None:
        from prefrontal.integrations import OllamaClient

        client = OllamaClient.from_settings()
    try:
        reply = generate_text(
            client,
            prompt,
            system=system,
            num_ctx=num_ctx,
            timeout=timeout,
            want_json=True,
        )
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
