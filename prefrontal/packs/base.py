"""Base types for Context Packs — composition over primitives, not new modules.

Challenge-area :mod:`~prefrontal.modules` answer *how* your ADHD shows up (time
blindness, hyperfocus, impulsivity). A **Pack** answers *what life you're
managing* — Parent, Caregiver, Grad student — which is an orthogonal axis (a
parent still has time blindness; "parent" is not a peer of "hyperfocus").

A Pack is therefore deliberately **not** a :class:`~prefrontal.modules.base.Module`.
It is a thin, mostly-declarative composition layer that:

1. **switches on** a relevant subset of challenge modules (:attr:`modules`),
2. **seeds domain vocabulary** — todo :attr:`categories` and commitment
   :attr:`commitment_kinds` — plus coaching-state defaults (:attr:`coaching_defaults`,
   e.g. per-category time windows), and
3. is cheap to add and shareable ("install the Parent pack") — the same opt-in
   modular ethos as challenge modules, on the life-context axis.

Situation tools and surface tailoring (the roadmap's points 3–4) are the shipped
Parent household features today; this abstraction is the registry/vocabulary
backbone they slot into. Packs register themselves with
:mod:`prefrontal.packs.registry` on import and are enabled via ``PREFRONTAL_PACKS``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prefrontal.memory.store import MemoryStore


@dataclass(frozen=True)
class SituationTool:
    """One read-only "situation" a pack answers for the life-context it manages.

    A pack switches on modules and seeds vocabulary; a **situation tool** is the
    active counterpart — a named, on-demand question the pack can answer from the
    user's live data ("when do I leave for the school run?"). It composes an
    existing primitive (e.g. :func:`prefrontal.departure.plan_upcoming_departures`)
    into a small computed result, exactly like ``POST /assistant/find-time`` reads
    the calendar without writing anything. Tools are surfaced and run through the
    ``/packs/situations`` router, gated on their owning pack being enabled.

    Attributes:
        key: Stable machine identifier, unique across enabled packs, used in the
            ``/packs/situations/{tool}`` path (e.g. ``school_run``).
        title: Human-readable name (e.g. "School run").
        description: One sentence on what the tool answers.
        handler: The read-only computation. Takes the caller's scoped
            :class:`~prefrontal.memory.store.MemoryStore` and returns a
            JSON-serializable dict — it must never write.
    """

    key: str
    title: str
    description: str
    handler: Callable[[MemoryStore], dict[str, Any]]


@dataclass(frozen=True)
class Pack:
    """One life-context pack: a declarative composition over existing primitives.

    Attributes:
        key: Stable machine identifier used in ``PREFRONTAL_PACKS`` and the
            registry (e.g. ``parent``).
        title: Human-readable name (e.g. "Parent").
        description: One or two sentences on the life context this pack supports.
        modules: Challenge-module keys this pack turns *on* when enabled (on top
            of ``PREFRONTAL_MODULES``). A parent still has time blindness, so the
            pack lights up the modules that matter for that context.
        categories: Todo ``category`` labels this context uses (declared
            vocabulary — surfaced to the user and usable in window overrides).
        commitment_kinds: Commitment ``kind`` values this context adds (e.g.
            ``child``). Declared here for documentation and vocabulary; they must
            already be valid in :data:`prefrontal.commitments.KINDS`.
        coaching_defaults: ``coaching_state`` key/value defaults seeded when the
            pack is enabled (via :meth:`seed`), never clobbering existing values —
            e.g. ``{"todo_window:school": "08:00-15:00"}`` to shape scheduling for
            a pack category. Read-only so the shared base default is never mutated.
        situations: Read-only :class:`SituationTool` s this pack answers on demand
            (e.g. the Parent pack's school-run leave-by). Surfaced and run through
            the ``/packs/situations`` router only when the pack is enabled.
    """

    key: str
    title: str
    description: str = ""
    modules: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    commitment_kinds: tuple[str, ...] = ()
    coaching_defaults: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    situations: tuple[SituationTool, ...] = ()

    def seed(self, store: MemoryStore) -> None:
        """Seed this pack's :attr:`coaching_defaults` into coaching state.

        Absent-only (mirrors :meth:`prefrontal.modules.base.Module.seed`): a value
        the user or an earlier-declared pack already set is preserved, so enabling
        a pack never overwrites a preference and the precedence in
        :func:`prefrontal.packs.registry.resolve_pack_vocabulary` holds.

        Args:
            store: A :class:`~prefrontal.memory.store.MemoryStore` scoped to the
                user being seeded.
        """
        existing = store.all_state()
        for key, value in self.coaching_defaults.items():
            if key not in existing:
                store.set_state(key, value, source="inferred")


@dataclass(frozen=True)
class PackVocabulary:
    """The merged vocabulary contributed by all enabled packs.

    Built by :func:`prefrontal.packs.registry.resolve_pack_vocabulary`. Categories
    and commitment kinds are unions (first-seen order preserved); coaching
    defaults are merged earlier-pack-wins, matching ``PREFRONTAL_PACKS`` order.
    """

    categories: tuple[str, ...] = ()
    commitment_kinds: tuple[str, ...] = ()
    coaching_defaults: Mapping[str, str] = field(default_factory=dict)
