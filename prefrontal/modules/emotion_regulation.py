"""Emotion Regulation module — the in-the-moment feeling side of a hard moment.

Emotional dysregulation is a core, large-effect ADHD feature that the module
taxonomy previously addressed only *indirectly* (via panic triage and the
rough-day encouragement layer). This module makes it first-class: it declares the
interventions, contributes an honest profile section, and owns the coaching-state
opt-in — while the actual skills, crisis boundary, and rendering live in the pure
core :mod:`prefrontal.emotion_regulation`, delivered on demand through
``POST /emotion/support``.

Deliberately **on-demand** (no proactive emotion-sensing in v1): the user reaches
for support, one tap or a few words, and gets one evidence-matched micro-skill —
or, if the words suggest crisis, resources instead of a skill. The only ambient
touch is an opt-in acceptance line folded into the rough-day recovery message
(:data:`~prefrontal.emotion_regulation.RECOVERY_ACCEPTANCE_KEY`).
"""

from __future__ import annotations

from collections import Counter

from prefrontal.emotion_regulation import RECOVERY_ACCEPTANCE_KEY
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import register

_SUPPORT_CONTEXT_PREFIX = "emotion support: "


class EmotionRegulationModule(Module):
    """First-class support for regulating the feeling in a hard emotional moment."""

    key = "emotion_regulation"
    title = "Emotion Regulation"
    challenge = (
        "Emotions that hit fast and hard — overwhelm, frustration, the sharp sting "
        "of perceived rejection — and are difficult to ride out without them "
        "hijacking the moment. A core part of ADHD, not a character flaw."
    )
    default_state = {
        # Opt-in: fold a gentle acceptance line into the rough-day recovery message.
        # Off by default — unsolicited emotional content should be a choice.
        RECOVERY_ACCEPTANCE_KEY: "off",
    }

    def interventions(self) -> list[Intervention]:
        """Declare the support behaviors (all on-demand / opt-in in v1)."""
        return [
            Intervention(
                name="distress_tolerance_skill",
                description=(
                    "On demand ('I'm having a hard moment'), offer one brief, "
                    "evidence-matched micro-skill — ACT acceptance, a DBT "
                    "distress-tolerance move (paced breathing, grounding, a cold "
                    "reset), or self-compassion framing — fitted to the feeling. "
                    "POST /emotion/support."
                ),
                trigger="the user reaches for support in a hard moment",
                status="active",
            ),
            Intervention(
                name="crisis_safety",
                description=(
                    "Screen the request for self-harm / crisis language first and, "
                    "if present, respond with resources and an urge to reach a "
                    "person — never a coping skill. General-wellness support, not "
                    "crisis intervention."
                ),
                trigger="a support request whose words suggest crisis",
                status="active",
            ),
            Intervention(
                name="recovery_acceptance",
                description=(
                    "When opted in, fold a single self-compassion line into the "
                    "rough-day recovery message so the feeling is met alongside the "
                    "practical re-fit plan (emotion_recovery_acceptance)."
                ),
                trigger="a rough day, with the acceptance fold-in enabled",
                status="active",
            ),
        ]

    def profile_section(self, store: MemoryStore) -> str | None:
        """Honestly surface recent reaches for in-the-moment support (no judgment)."""
        checkins = store.episodes_by_type("checkin", limit=100)
        moments = [
            c for c in checkins
            if str(c.get("context") or "").startswith(_SUPPORT_CONTEXT_PREFIX)
        ]
        if not moments:
            return None
        states = Counter(
            (c.get("context") or "")[len(_SUPPORT_CONTEXT_PREFIX):].strip()
            for c in moments
        )
        top = ", ".join(f"{state} (×{n})" for state, n in states.most_common(3) if state)
        lines = [
            f"Reached for in-the-moment emotional support {len(moments)} time(s) "
            "recently — reaching for a skill is the regulation working, not a problem."
        ]
        if top:
            lines.append(f"Most common: {top}.")
        return "\n".join(f"- {line}" for line in lines)


register(EmotionRegulationModule())
