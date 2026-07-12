"""Base types for Prefrontal's challenge-area modules.

ADHD is not one thing. Time blindness, task paralysis, hyperfocus, and
impulsivity are distinct executive-function challenges that need different
support — and any given person experiences some subset of them. Prefrontal
models each challenge as a discrete **module** that can be enabled or disabled
independently, so the system can be tuned to an individual profile rather than
assuming everyone needs the same nudges.

A module is deliberately small. It:

- declares metadata (``key``, ``title``, ``challenge``);
- owns a set of ``coaching_state`` defaults that are seeded when it is enabled;
- contributes a section to the behavioral profile via :meth:`Module.profile_section`;
- declares the :class:`Intervention`\\ s it provides (each with a trigger and a
  wired/planned status; see ``prefrontal modules -v``).

Concrete modules live alongside this file (``time_blindness.py`` etc.) and
register themselves with :mod:`prefrontal.modules.registry` on import.

Modules take the concrete :class:`~prefrontal.memory.store.MemoryStore` directly.
An earlier ``ModuleStore`` Protocol tried to fence a module to a narrow read/seed
slice of the store, but Prefrontal is single-tenant, self-hosted, and every
module is first-party code — there is no untrusted plugin to contain — so the
fence bought nothing and, as modules grew to touch outings, todos, trips, and
commitments, it drifted into a contract that no longer described the real
surface. Depending on ``MemoryStore`` outright is the honest shape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prefrontal.coaching import CoachContext, Cue, Decision
    from prefrontal.memory.store import MemoryStore


@dataclass(frozen=True)
class TutorialStep:
    """One card in a module's new-user walkthrough.

    A tutorial is a short, ordered list of these — plain prose a first-time user
    can read in the in-app Guide (``GET /guide``) to learn what a module does and
    what to expect. Deliberately data-only (no HTML): the web page and the CLI
    each render it their own way, and it stays easy to test.

    Attributes:
        title: The step's heading (e.g. "What Prefrontal will do").
        body: The step's prose. Lines beginning with ``• `` render as bullets.
    """

    title: str
    body: str


@dataclass(frozen=True)
class Intervention:
    """A single support behavior a module can perform.

    Interventions are described declaratively so they can be listed, documented,
    and toggled before any of them is fully implemented. Most ship as ``planned``
    today.

    Attributes:
        name: Stable identifier, unique within a module (e.g. ``departure_buffer``).
        description: One-line explanation of what the intervention does.
        trigger: What causes it to fire (e.g. "an upcoming calendar event").
        status: ``planned`` (designed, not wired) or ``active`` (implemented).
    """

    name: str
    description: str
    trigger: str
    status: str = "planned"


class Module(ABC):
    """Base class for a challenge-area module.

    Subclasses set the three class attributes and implement
    :meth:`profile_section`. Everything else has a sensible default so a minimal
    module is only a few lines.

    Attributes:
        key: Stable machine identifier used in config and the registry
            (e.g. ``time_blindness``).
        title: Human-readable name (e.g. "Time Blindness").
        challenge: One or two sentences describing the executive-function
            challenge this module addresses.
        default_state: ``coaching_state`` key/value defaults seeded when the
            module is enabled (see :meth:`seed`). These never clobber existing
            values. Subclasses override this with their own dict (it is only
            ever read, never mutated).
    """

    key: str = ""
    title: str = ""
    challenge: str = ""
    #: Read-only so the shared base default can never be mutated in place; a
    #: subclass overrides it with its own plain-dict literal (any ``Mapping``
    #: satisfies the annotation and :meth:`seed` only ever reads it).
    default_state: Mapping[str, str] = MappingProxyType({})
    #: Whether this module's cues may pierce protected hyperfocus. Almost every
    #: module stays silent while an aligned deep-work block shields the user; the
    #: sanctioned interrupts set this ``True`` — self-care basic-needs checks
    #: (meant to break flow) and hyperfocus's own alignment check (the one
    #: interrupt allowed *during* an aligned overrun, so the protecting module
    #: must not be gated by its own protection). The coaching engine reads this
    #: into its central suppression gate, so no module re-checks protection and
    #: the engine names none of them.
    pierces_protection: bool = False

    def interventions(self) -> list[Intervention]:
        """Return the interventions this module provides.

        Returns:
            A list of :class:`Intervention` declarations. Defaults to empty.
        """
        return []

    def tutorial(self) -> list[TutorialStep]:
        """Return a new-user walkthrough for this module.

        Powers the in-app Guide (``GET /guide``) and ``prefrontal modules
        --tutorial``: a first-time user reads it to understand what the module
        is for, how it will show up, and that they don't have to do anything to
        start. The guide is always available and can be re-read at any time.

        The default builds the walkthrough straight from the module's own
        metadata — :attr:`challenge` and its :meth:`interventions` — so every
        module (including any future one) has a coherent guide for free, and the
        guide can never drift out of sync with what the module actually does. A
        module with unusual onboarding may override this, but most should not
        need to.

        Returns:
            An ordered list of :class:`TutorialStep`\\s.
        """
        steps: list[TutorialStep] = []
        if self.challenge:
            steps.append(
                TutorialStep(title=f"What {self.title} helps with", body=self.challenge)
            )
        active = [i for i in self.interventions() if i.status == "active"]
        if active:
            body = "\n".join(f"• {i.description}" for i in active)
            steps.append(TutorialStep(title="What Prefrontal will do", body=body))
        planned = [i for i in self.interventions() if i.status != "active"]
        if planned:
            body = "\n".join(f"• {i.description}" for i in planned)
            steps.append(TutorialStep(title="Coming soon", body=body))
        steps.append(
            TutorialStep(
                title="You're set",
                body=(
                    f"There's nothing to switch on — {self.title} runs quietly in the "
                    "background and gets better the more you use Prefrontal. Come back to "
                    "this guide whenever you want a refresher."
                ),
            )
        )
        return steps

    def provides_protection(self, store: MemoryStore) -> bool:
        """Whether this module is currently shielding the user from other cues.

        The coaching engine OR-s this across every enabled module once per tick
        into :attr:`~prefrontal.coaching.CoachContext.focus_protected`, so the
        central suppression gate can hold other modules' non-critical cues without
        the engine naming a specific module. Today only Hyperfocus returns ``True``
        (during an aligned deep-work block); every other module inherits this
        ``False`` default.
        """
        return False

    def channel_targets(self) -> Mapping[str, str]:
        """Declare this module's channel-outcome-trackable contexts.

        Maps a ``context_key`` this module's cues carry → the
        :attr:`~prefrontal.coaching.Cue.ref` key holding the acknowledgement
        target id (e.g. ``{"outing": "outing_id"}``). A cue whose ``context_key``
        is in the combined map — and that goes out on an interrupting channel — is
        tracked for channel learning: a one-tap ack logs a *success*, silence past
        the window a *miss* (feeding ``channel_response``). Default ``{}`` — a
        module with no tappable ack is never tracked, since counting it would bias
        its channel toward "always ignored".
        """
        return {}

    def evaluate(self, store: MemoryStore, ctx: CoachContext) -> list[Cue]:
        """Return the coaching cues due right now, given memory + context.

        The coaching agent (:mod:`prefrontal.coaching`) calls this on each tick to
        ask "anything to say?" A module reads the store and may advance its own
        one-fire state, but must **not** deliver — it only returns
        :class:`~prefrontal.coaching.Cue`\\s; whether/how to send them (channel,
        debounce, quiet hours) is the agent's job. Defaults to none, so a module
        lights up its coaching by filling this in — nothing else to wire.
        """
        return []

    def before_collect(self, store: MemoryStore, ctx: CoachContext) -> None:  # noqa: B027
        """Run once per tick, *before* any cue is collected. Default no-op.

        The place for a module's pre-collection housekeeping — e.g. sweeping its
        own prior unanswered nudges into outcome episodes so the tick's learning
        signals are current. The coaching engine calls this on every enabled
        module so no module needs bespoke wiring in the tick loop (contrast the
        old hard-coded self-care sweep). Runs after :func:`build_context`, so
        ``ctx`` (including ``ctx.now``) is available.
        """

    def after_fire(  # noqa: B027
        self, store: MemoryStore, decisions: list[Decision], ctx: CoachContext
    ) -> None:
        """Run once per tick, *after* the fired decisions are recorded. Default no-op.

        The place for a module's post-fire housekeeping — e.g. stamping a delivery
        time so a later one-tap response can be timed, or applying the store writes
        a side-effect-free :meth:`evaluate` deliberately held back (so ``evaluate``
        stays a pure read; see ``location_anchor``). ``decisions`` is the full fired
        list for the tick; a module filters to its own cues
        (``d.cue.module == self.key``). ``ctx`` is the same per-tick context passed
        to :meth:`evaluate` / :meth:`before_collect` (carrying ``ctx.now`` and the
        current location), so a module can recompute exactly what it decided. Like
        :meth:`before_collect`, the engine calls this on every enabled module.
        """

    def seed(self, store: MemoryStore) -> None:
        """Seed this module's ``default_state`` into coaching state.

        Existing values are preserved (``set_state`` upserts, and we only write
        keys that are currently absent), so enabling a module never overwrites
        preferences the user or another module has already set.

        Args:
            store: The :class:`~prefrontal.memory.store.MemoryStore`, scoped to
                the user being seeded.
        """
        existing = store.all_state()
        for key, value in self.default_state.items():
            if key not in existing:
                store.set_state(key, value, source="inferred")

    @abstractmethod
    def profile_section(self, store: MemoryStore) -> str | None:
        """Return this module's contribution to the behavioral profile.

        The summarizer concatenates each enabled module's section into
        ``profile.md``. Return ``None`` (or an empty string) to contribute
        nothing — e.g. when there is not yet enough data.

        Args:
            store: The :class:`~prefrontal.memory.store.MemoryStore`, scoped to
                the user whose profile is being assembled.

        Returns:
            A Markdown fragment (without a top-level heading; the summarizer adds
            one from :attr:`title`), or ``None`` to contribute nothing.
        """
        raise NotImplementedError
