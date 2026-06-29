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
- declares the :class:`Intervention`\\ s it provides (mostly planned stubs today).

Concrete modules live alongside this file (``time_blindness.py`` etc.) and
register themselves with :mod:`prefrontal.modules.registry` on import.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

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
    default_state: dict[str, str] = {}

    def interventions(self) -> list[Intervention]:
        """Return the interventions this module provides.

        Returns:
            A list of :class:`Intervention` declarations. Defaults to empty.
        """
        return []

    def seed(self, store: MemoryStore) -> None:
        """Seed this module's ``default_state`` into coaching state.

        Existing values are preserved (``set_state`` upserts, and we only write
        keys that are currently absent), so enabling a module never overwrites
        preferences the user or another module has already set.

        Args:
            store: An open :class:`~prefrontal.memory.store.MemoryStore`.
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
            store: An open :class:`~prefrontal.memory.store.MemoryStore`.

        Returns:
            A Markdown fragment (without a top-level heading; the summarizer adds
            one from :attr:`title`), or ``None`` to contribute nothing.
        """
        raise NotImplementedError
