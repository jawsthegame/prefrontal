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
    from datetime import datetime

    from prefrontal.coaching import CoachContext, Cue, Decision
    from prefrontal.memory.store import MemoryStore


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

    def interventions(self) -> list[Intervention]:
        """Return the interventions this module provides.

        Returns:
            A list of :class:`Intervention` declarations. Defaults to empty.
        """
        return []

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
        self, store: MemoryStore, decisions: list[Decision], now: datetime
    ) -> None:
        """Run once per tick, *after* the fired decisions are recorded. Default no-op.

        The place for a module's post-fire housekeeping — e.g. stamping a
        delivery time so a later one-tap response can be timed. ``decisions`` is
        the full fired list for the tick; a module filters to its own cues
        (``d.cue.module == self.key``). Like :meth:`before_collect`, the engine
        calls this on every enabled module.
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
