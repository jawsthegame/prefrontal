"""Communication translation — decode, draft, or soften a work message.

The dreaded multi-step admin that ADHD adults avoid is a task-*initiation* wall,
and a large, documented slice of it is **communication**: a work email whose real
ask is buried under corporate hedging, a reply you can't start because you can't
find the right register, a blunt message you're afraid to send as written. This
is a ready-now, high-demand, low-risk LLM use among ADHD adults (roadmap M4, the
"communication translation" tool) — and unlike the rest of M4 it takes **no**
irreversible action: it only ever returns *text* for the user to read, copy, or
edit. Nothing is sent, booked, or written to the store.

Three modes, one entry point (:func:`translate`):

- **decode** — read a loaded/ambiguous message and say plainly what it *means*:
  the actual ask, the subtext, the real urgency, and any implied deadline. Turns
  "circle back to align on the deliverable cadence" into "they want to know when
  you'll send the next draft."
- **draft** — draft a reply in a chosen *register* (professional / warm / firm /
  concise / friendly) from a short description of what the user wants to say.
- **soften** — re-register a message the user already wrote (often too blunt, or
  too anxious/apologetic) into the chosen tone, keeping the intent.

Like :mod:`prefrontal.clarify` and the delegation prep, the model does the work
when reachable and a hand-authored heuristic covers the model-down path — but
*decode* and *draft* genuinely need the model (there is nothing honest to invent
offline), so their fallback says so plainly rather than guessing. *soften* has a
minimal deterministic fallback (it can at least return the user's own text with a
register note). The system prompt forbids inventing facts: any missing real
detail (a date, a name, a number) is left as a clearly-marked
``[bracketed placeholder]``, mirroring the delegation drafting convention.

This is general-purpose writing help, not advice: it never counsels the user on
*whether* to send something, only on *how* it reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from prefrontal.integrations.base import ProviderError
from prefrontal.llm_json import extract_json_object

if TYPE_CHECKING:
    from prefrontal.integrations import Generator


#: The supported translation modes (the ``mode`` field of a request).
MODES = ("decode", "draft", "soften")

#: The registers a ``draft``/``soften`` can target. ``decode`` ignores register
#: (it explains, it doesn't rewrite). Unknown/empty falls back to ``professional``.
REGISTERS = ("professional", "warm", "firm", "concise", "friendly")

DEFAULT_REGISTER = "professional"

#: What each register asks the model for, folded into the prompt so the tone is
#: concrete rather than a bare adjective.
_REGISTER_GUIDANCE = {
    "professional": "polished and businesslike; courteous but not stiff",
    "warm": "warm and personable; friendly without being unprofessional",
    "firm": "clear and firm; direct about the ask or the boundary, still polite",
    "concise": "as short as it can be while staying complete; no filler",
    "friendly": "casual and friendly, the register you'd use with a close colleague",
}


@dataclass(frozen=True)
class TranslationResult:
    """The text a :func:`translate` call produced — no side effects.

    Attributes:
        mode: The mode that ran (``decode`` / ``draft`` / ``soften``).
        register: The target register for ``draft``/``soften`` (``""`` for
            ``decode``, which doesn't rewrite).
        output: The translated text — the decoded meaning, the drafted reply, or
            the softened message. The one thing the caller shows the user.
        note: An optional short framing line for the client (e.g. the fallback
            caveat), never part of the message itself.
        offline: True when the result came from the heuristic fallback rather than
            usable model output — either the model was unreachable *or* it replied
            with something that couldn't be used (see ``note`` for which).
    """

    mode: str
    register: str
    output: str
    note: str = ""
    offline: bool = False


def normalize_mode(mode: str | None) -> str:
    """Coerce a caller-supplied mode to a supported one (defaults to ``decode``)."""
    m = (mode or "").strip().lower()
    return m if m in MODES else "decode"


def normalize_register(register: str | None) -> str:
    """Coerce a caller-supplied register to a supported one (defaults to professional)."""
    r = (register or "").strip().lower()
    return r if r in REGISTERS else DEFAULT_REGISTER


_SYSTEM = (
    "You help someone with ADHD handle work communication they're avoiding — the "
    "task-initiation wall around email and messages. You do ONE of three jobs, "
    "named in the request, and you ONLY produce text (you never send, book, or act "
    "on anything). Reply with ONLY JSON, no prose: "
    '{"output": "<the result text>"}. The three jobs:\n'
    "- decode: the user pastes a message they received. Explain plainly what it "
    "actually means — the real ask, any subtext, the true urgency, and any implied "
    "deadline. A few short sentences or tight bullet lines. Do not draft a reply.\n"
    "- draft: the user describes what they want to say. Write the message for them "
    "in the requested register.\n"
    "- soften: the user pastes a message they wrote. Rewrite it in the requested "
    "register, keeping their meaning and every concrete fact — just change how it "
    "reads.\n"
    "Never invent facts you were not given. Where a real detail (a date, a name, a "
    "number, a link) is needed but absent, leave a clearly-marked [bracketed "
    "placeholder] for the user to fill. Do not advise whether to send anything; "
    "only handle how it reads. Keep it practical and ready to use."
)


def _prompt(mode: str, register: str, text: str) -> str:
    """Assemble the user-turn prompt for the model from the request."""
    if mode == "decode":
        return f"Job: decode.\nMessage received:\n{text}"
    guidance = _REGISTER_GUIDANCE.get(register, _REGISTER_GUIDANCE[DEFAULT_REGISTER])
    label = "draft" if mode == "draft" else "soften"
    lead = (
        "What the user wants to say" if mode == "draft" else "Message the user wrote"
    )
    return (
        f"Job: {label}.\nRegister: {register} ({guidance}).\n{lead}:\n{text}"
    )


def _coerce_output(raw: dict) -> str:
    """Pull a non-empty ``output`` string from a model reply, or ``""``."""
    out = raw.get("output")
    if not isinstance(out, str):
        return ""
    return out.strip()


def _heuristic(
    mode: str, register: str, text: str, *, unusable: bool = False
) -> TranslationResult:
    """A hand-authored result when usable model output isn't available.

    ``decode`` and ``draft`` cannot be done honestly without the model — there is
    nothing to explain from no reading, and nothing to draft from no model — so they
    return an honest caveat rather than a guess. ``soften`` degrades gracefully: it
    hands the user's own text back with a register note, which is at least useful.

    ``unusable`` distinguishes the two ways this path is reached: ``False`` means the
    model was never reachable (no client, or a transport error), ``True`` means the
    model *replied* but its answer couldn't be used (e.g. non-JSON) — the caveat
    names which so the message isn't misleading when the model was in fact online.
    """
    cause = (
        "the model returned a response I couldn't use"
        if unusable
        else "the model was unavailable"
    )
    if mode == "soften":
        return TranslationResult(
            mode=mode,
            register=register,
            output=text.strip(),
            note=(
                f"Because {cause}, this is your original text unchanged — "
                f"rewrite it to read as {register} when a model is reachable, or edit by hand."
            ),
            offline=True,
        )
    what = "decode this message" if mode == "decode" else "draft this reply"
    return TranslationResult(
        mode=mode,
        register="" if mode == "decode" else register,
        output="",
        note=(
            f"I can't {what} right now — {cause}. "
            "Try again when a model is reachable (or opt the assistant into the cloud provider)."
        ),
        offline=True,
    )


def translate(
    text: str,
    mode: str | None = None,
    register: str | None = None,
    *,
    client: Generator | None = None,
) -> TranslationResult:
    """Decode, draft, or soften ``text`` — text only, no side effects.

    The model does the work when ``client`` is reachable; on any model failure — or
    an unusable/empty reply — it falls back to :func:`_heuristic`, so a call always
    returns *something* (an honest offline caveat rather than a fabricated result
    for the modes that can't be faked). ``register`` shapes the tone of a
    ``draft``/``soften`` and is ignored for ``decode``.

    Args:
        text: The message to decode, the ask to draft, or the message to soften.
        mode: One of :data:`MODES` (defaults to ``decode``).
        register: One of :data:`REGISTERS` for ``draft``/``soften`` (defaults to
            ``professional``); ignored for ``decode``.
        client: An Ollama-like :class:`~prefrontal.integrations.Generator`, or
            ``None`` to use the heuristic only.

    Returns:
        A :class:`TranslationResult`. ``output`` is empty only when an offline
        mode couldn't produce anything honest (see ``note``/``offline``).
    """
    resolved_mode = normalize_mode(mode)
    resolved_register = "" if resolved_mode == "decode" else normalize_register(register)
    stripped = (text or "").strip()
    if not stripped:
        return TranslationResult(
            mode=resolved_mode,
            register=resolved_register,
            output="",
            note="Nothing to work with — paste the message or say what you want to write.",
        )
    if client is not None:
        prompt = _prompt(resolved_mode, resolved_register or DEFAULT_REGISTER, stripped)
        try:
            reply = client.generate(prompt, system=_SYSTEM)
        except ProviderError:
            reply = ""
        output = _coerce_output(extract_json_object(reply))
        if output:
            return TranslationResult(
                mode=resolved_mode,
                register=resolved_register,
                output=output,
            )
        # Consulted the model but couldn't use its answer: a non-empty reply that
        # didn't parse is "unusable" (the model was online), an empty one is a
        # transport failure — the heuristic caveat names the right one.
        return _heuristic(
            resolved_mode,
            resolved_register or DEFAULT_REGISTER,
            stripped,
            unusable=bool(reply.strip()),
        )
    return _heuristic(resolved_mode, resolved_register or DEFAULT_REGISTER, stripped)
