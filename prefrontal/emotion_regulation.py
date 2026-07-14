"""Emotion regulation — in-the-moment support for a hard emotional moment.

Emotional dysregulation is a *core* feature of ADHD, not a side-effect: the
adult-ADHD effect size for it is among the largest of any symptom cluster
(Hedges' *g* ≈ 1.17), and — unlike time-blindness or task-initiation — no
validated self-help tool addresses it for this population. The system already
carries the *task* and *day* sides of a hard moment (panic-mode triage; the
encouragement/recovery layer's rough-day plan); this module carries the third,
missing side: the **feeling itself**, in the moment.

It is deliberately narrow and evidence-matched. On demand — the user says "I'm
having a hard moment" (one tap, or a few words) — it offers **one** brief,
concrete micro-skill drawn from two traditions with the best evidence for acute
distress:

- **ACT** (acceptance & commitment): name the feeling, allow it without a fight,
  take one small values-aligned action — the opposite of "calm down".
- **DBT distress tolerance**: paced breathing (long exhale), 5-4-3-2-1 grounding,
  a cold-water temperature reset (TIPP), radical acceptance — body-first skills
  for riding out a spike without making it worse.
- plus **self-compassion** framing for the rejection-sensitive moments ADHD makes
  so sharp ("RSD" as lived experience, *not* a diagnosis this tool asserts).

Two hard boundaries shape everything here:

1. **This is general-wellness support, not therapy or crisis intervention.** It
   offers coping micro-skills for everyday overwhelm/frustration/rejection — it
   does not diagnose, treat, or counsel.
2. **Crisis is never met with a breathing exercise.** :func:`looks_like_crisis`
   screens the free-text capture first; if it trips, the response is *only*
   resources and an urge to reach a person — never a coping skill, never a
   dismissal. See :data:`CRISIS_MESSAGE`.

Like :mod:`prefrontal.panic` and :mod:`prefrontal.encouragement`, the core is
deterministic and model-free (the skill text is delivered *as written* — a
safety-sensitive instruction is not something to let a model paraphrase). It
composes into the coaching surfaces rather than adding a new delivery path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prefrontal.memory.store import MemoryStore


# --- Crisis boundary (screened before anything else) -------------------------
#
# The single most important behaviour in this module: language that suggests
# self-harm or suicidal ideation must NOT be answered with a coping skill. These
# patterns are intentionally broad (false positives here cost a resource message
# the user can ignore; a false negative costs far more), matched on word
# boundaries against the lowered text.
_CRISIS_PATTERNS = tuple(
    re.compile(p) for p in (
        r"\bkill(ing)?\s+my\s?self\b",
        r"\bkill\s+me\b",
        r"\bsuicid",
        # Explicit self-harm phrasings only — a standalone "end my …" would trip on
        # "end my meeting/day", so match "end it all" / "end it" / "end my life".
        r"\bend(ing)?\s+(it\s+all|it|my\s+life)\b",
        r"\bwant\s+to\s+die\b",
        r"\bwant\s+to\s+be\s+dead\b",
        r"\bbetter\s+off\s+dead\b",
        r"\bdon'?t\s+want\s+to\s+(be\s+here|live|wake\s+up)\b",
        r"\bno\s+(reason|point)\s+(to|in)\s+(live|living|going\s+on)\b",
        r"\bhurt(ing)?\s+my\s?self\b",
        r"\bharm(ing)?\s+my\s?self\b",
        r"\bself[-\s]?harm\b",
        r"\bcut(ting)?\s+my\s?self\b",
    )
)

#: The response when :func:`looks_like_crisis` trips — resources only, no skill,
#: no analysis. Kept general (a local emergency number always applies) with the US
#: 988 line named because the deployment localizes to a US home ZIP. Warm, brief,
#: and pointed at reaching a *person*.
CRISIS_MESSAGE = (
    "It sounds like you're in real pain right now, and I'm not the right thing to "
    "lean on for this — a person is. Please reach out now: in the US, call or text "
    "**988** (Suicide & Crisis Lifeline), or call your local emergency number. If "
    "you can, tell someone you trust what you just told me. You don't have to carry "
    "this alone, and reaching out is not a failure."
)


def looks_like_crisis(text: str | None) -> bool:
    """Whether free text suggests self-harm / suicidal ideation (screen first).

    Broad on purpose: the cost of a false positive is a resource message the user
    can dismiss; the cost of a false negative is answering a crisis with a
    breathing exercise. Empty/``None`` text is not a crisis (a bare one-tap request
    with no words routes to an ordinary skill).
    """
    if not text:
        return False
    lowered = text.lower()
    return any(p.search(lowered) for p in _CRISIS_PATTERNS)


# --- The skills library ------------------------------------------------------

#: The emotional states a moment maps to; drives which skills fit. ``generic`` is
#: the fallback for a wordless one-tap request or unrecognized text.
STATES = ("overwhelm", "anxiety", "anger", "rejection", "sadness", "generic")


@dataclass(frozen=True)
class Skill:
    """One brief, in-the-moment regulation micro-skill.

    ``prompt`` is delivered verbatim — it is the intervention, so it is written to
    be complete and correct on its own (never paraphrased by a model). ``family``
    names its tradition (``act`` / ``dbt`` / ``self_compassion``) for the profile
    and learning; ``fits`` is the set of :data:`STATES` it suits.
    """

    key: str
    family: str
    prompt: str
    fits: tuple[str, ...]


#: The curated library. Small on purpose — a handful of well-chosen skills the
#: user can actually reach for, not a catalogue. Ordered so the per-state picker
#: prefers body-first skills for acute spikes and framing skills for rejection.
SKILLS: tuple[Skill, ...] = (
    Skill(
        "paced_breathing", "dbt",
        "Slow the exhale: breathe in for 4, out for 6, for about a minute. The long "
        "out-breath is what tells your body the emergency is over — you don't have "
        "to feel calm first, just breathe the ratio.",
        ("overwhelm", "anxiety", "anger", "generic"),
    ),
    Skill(
        "grounding_54321", "dbt",
        "Come back into the room: name 5 things you can see, 4 you can hear, 3 you "
        "can touch, 2 you can smell, 1 you can taste. It interrupts the spin and "
        "puts you back in the present.",
        ("overwhelm", "anxiety", "generic"),
    ),
    Skill(
        "temperature_reset", "dbt",
        "Cool your system down, literally: splash cold water on your face, or hold "
        "something cold for 30 seconds. Cold resets a spike faster than reasoning "
        "with it can — then decide anything.",
        ("anger", "anxiety", "overwhelm"),
    ),
    Skill(
        "radical_acceptance", "dbt",
        "This moment is already here — arguing that it *shouldn't* be adds a second "
        "layer of pain on top of the first. Try saying: “this is hard, and it's "
        "what's true right now.” Acceptance isn't approval; it's setting down "
        "the extra fight.",
        ("anxiety", "sadness", "rejection", "generic"),
    ),
    Skill(
        "name_and_allow", "act",
        "Name it plainly, even silently: “I'm noticing overwhelm.” You "
        "don't have to fix or banish it — just let it be here for a moment. Naming a "
        "feeling loosens its grip more than fighting it does.",
        ("overwhelm", "anger", "sadness", "generic"),
    ),
    Skill(
        "values_step", "act",
        "Ask: who do I want to be for the next ten minutes? Then take one small "
        "action in that direction — not the whole mountain, just the next honest "
        "step. Feelings can ride along; they don't have to drive.",
        ("overwhelm", "sadness", "generic"),
    ),
    Skill(
        "friend_voice", "self_compassion",
        "Notice the harsh inner voice, then ask: what would I say to a friend who "
        "felt this? Say that to yourself, in those words. The cruelty isn't truth — "
        "it's the overwhelm talking.",
        ("rejection", "sadness", "anger"),
    ),
    Skill(
        "rejection_reframe", "self_compassion",
        "That sting of rejection can hit fast and huge — for a lot of people with "
        "ADHD it does, and it's a real response, not a character flaw. The feeling "
        "is loud, but it isn't a verdict. Give it a few minutes before you act on "
        "it or believe what it says about you.",
        ("rejection",),
    ),
)

_SKILLS_BY_KEY = {s.key: s for s in SKILLS}

#: Keyword → state cues for the light free-text classifier. First matching state
#: (in :data:`STATES` order) wins; nothing matching ⇒ ``generic``.
_STATE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "overwhelm": ("overwhelm", "too much", "buried", "swamped", "can't cope",
                  "cant cope", "drowning", "so much", "spiral"),
    "anxiety": ("anxious", "anxiety", "panic", "panicking", "worried", "worry",
                "nervous", "dread", "on edge", "scared", "freaking"),
    "anger": ("angry", "anger", "furious", "frustrat", "irritat", "pissed",
              "rage", "annoyed", "resent"),
    "rejection": ("reject", "criticiz", "criticis", "left out", "not good enough",
                  "hate me", "embarrass", "ashamed", "shame", "humiliat",
                  "rsd", "not wanted", "unwanted"),
    "sadness": ("sad", "down", "empty", "crying", "cry", "low", "lonely", "numb",
                "defeated", "discouraged"),
}

#: Coaching-state key holding the last skill key surfaced, so back-to-back
#: requests rotate rather than repeat the same skill.
LAST_SKILL_STATE_KEY = "er_last_skill"

#: Prefix stamped on the ``checkin`` episode :func:`record_support` logs for every
#: in-the-moment support request (``context="emotion support: <state|crisis>"``).
#: Exported as the single source of truth so the coaching engine's *vulnerability*
#: gate — which reads these episodes to hold nudges during a hard moment — filters
#: on the exact wire format the writer uses, and the two can't drift (mirroring how
#: :data:`~prefrontal.receptivity.COACH_NUDGE_CONTEXT_PREFIX` binds the nudge writer
#: and the receptivity reads).
SUPPORT_CONTEXT_PREFIX = "emotion support: "

#: The ``<state>`` slot value for a crisis screen (:func:`looks_like_crisis`), where
#: a :class:`SupportResponse` carries no emotional ``state`` — so a crisis check-in's
#: context is ``SUPPORT_CONTEXT_PREFIX + SUPPORT_CRISIS_KEY``. The vulnerability gate
#: reads this as its more serious tier (a longer hold).
SUPPORT_CRISIS_KEY = "crisis"

#: Coaching-state key (opt-in, default off) gating whether a gentle acceptance
#: line is folded into the rough-day encouragement/recovery message.
RECOVERY_ACCEPTANCE_KEY = "emotion_recovery_acceptance"


def infer_state(text: str | None) -> str:
    """Map free text to one of :data:`STATES` (``generic`` when nothing matches).

    A deliberately light keyword classifier — this only picks *which* well-formed
    skill to offer, so a rough guess is fine and a wordless request is fully valid
    (it yields ``generic``). Not called until after :func:`looks_like_crisis`.
    """
    if not text:
        return "generic"
    lowered = text.lower()
    for state in STATES:
        for kw in _STATE_KEYWORDS.get(state, ()):
            if kw in lowered:
                return state
    return "generic"


def pick_skill(state: str, *, last_key: str | None = None) -> Skill:
    """Choose a fitting skill for ``state``, avoiding an immediate repeat.

    Deterministic: among the library's skills that fit ``state`` (in library
    order), returns the first that isn't ``last_key``; if that would leave nothing
    (only one fits, and it was just used) it returns it anyway — a repeat beats no
    skill. Falls back to the generic-fitting set for an unknown state.
    """
    fitting = [s for s in SKILLS if state in s.fits]
    if not fitting:
        fitting = [s for s in SKILLS if "generic" in s.fits]
    for skill in fitting:
        if skill.key != last_key:
            return skill
    return fitting[0]


@dataclass(frozen=True)
class SupportResponse:
    """What one on-demand support request warrants (pure; no side effects).

    ``kind`` is ``"crisis"`` (resources only — ``skill_key``/``state`` empty) or
    ``"skill"`` (a micro-skill was chosen). ``message`` is the ready-to-deliver
    text either way.
    """

    kind: str
    message: str
    skill_key: str = ""
    family: str = ""
    state: str = ""


def _acknowledge(state: str) -> str:
    """A short, non-patronizing opener that meets the named feeling."""
    return {
        "overwhelm": "That sounds like a lot to be holding at once.",
        "anxiety": "Sounds like your system's running hot right now.",
        "anger": "That sounds genuinely frustrating.",
        "rejection": "That one stings — and the sting is real.",
        "sadness": "Sounds like a heavy moment.",
        "generic": "Rough moment. Let's take one small step through it.",
    }.get(state, "Rough moment. Let's take one small step through it.")


def render_support(skill: Skill, *, state: str) -> str:
    """The delivered message for a chosen skill: acknowledge → skill → soft close.

    Leads with a brief acknowledgment tuned to ``state`` (never "calm down"), hands
    the skill verbatim, and closes with permission to stop there — the goal is to
    take the edge off, not to fix the day. Forgiving by construction: no streak, no
    "you should have," nothing owed.
    """
    return (
        f"{_acknowledge(state)}\n\n"
        f"Try this: {skill.prompt}\n\n"
        "That's enough for right now — you don't have to sort the whole thing out "
        "this minute."
    )


def build_support(store: MemoryStore, text: str | None = None) -> SupportResponse:
    """Decide the response to an on-demand support request (pure read).

    Crisis is screened **first** (:func:`looks_like_crisis`) and short-circuits to
    :data:`CRISIS_MESSAGE` — no skill, no state inference. Otherwise the (optional)
    text is mapped to a :data:`STATES` value and a fitting skill is chosen, rotating
    off the last one surfaced (:data:`LAST_SKILL_STATE_KEY`). Pure — the caller
    persists the outcome via :func:`record_support`, so a dry read never mutates
    state (mirroring the engine's evaluate/apply split).
    """
    if looks_like_crisis(text):
        return SupportResponse(kind="crisis", message=CRISIS_MESSAGE)
    state = infer_state(text)
    last_key = store.get_state(LAST_SKILL_STATE_KEY)
    skill = pick_skill(state, last_key=last_key)
    return SupportResponse(
        kind="skill",
        message=render_support(skill, state=state),
        skill_key=skill.key,
        family=skill.family,
        state=state,
    )


def record_support(store: MemoryStore, response: SupportResponse) -> None:
    """Persist the writes a :class:`SupportResponse` implies (log + rotation).

    Logs a ``checkin`` episode so recent hard moments show honestly in the profile
    (``context="emotion support: <state|crisis>"``, acknowledged — the user reached
    for support, which is the healthy move), and advances the last-skill cursor so
    the next request rotates. Best-effort: a telemetry failure must never break the
    support response itself.
    """
    try:
        context = f"{SUPPORT_CONTEXT_PREFIX}{response.state or SUPPORT_CRISIS_KEY}"
        store.log_episode("checkin", acknowledged=True, context=context, outcome="success")
        if response.kind == "skill" and response.skill_key:
            store.set_state(LAST_SKILL_STATE_KEY, response.skill_key, source="inferred")
    except Exception:  # noqa: BLE001 — logging/rotation is best-effort, never fatal
        pass


def recovery_acceptance_line(store: MemoryStore) -> str | None:
    """A gentle acceptance line to fold into the rough-day recovery, or ``None``.

    Opt-in via :data:`RECOVERY_ACCEPTANCE_KEY` (default off). When on, returns one
    calm self-compassion sentence to sit above the day's re-fit plan — pairing the
    *emotional* side of a rough day with the practical one the recovery plan
    already handles. ``None`` when the key is off, so the encouragement layer is
    unchanged unless the user asks for this.
    """
    if (store.get_state(RECOVERY_ACCEPTANCE_KEY, "off") or "off").strip().lower() != "on":
        return None
    return (
        "First, the feeling: a rough day is a hard *day* — not evidence about you. "
        "Set the self-criticism down; it isn't telling you the truth."
    )
