"""Context Packs — life-context composition over Prefrontal's primitives.

Where challenge-area :mod:`prefrontal.modules` capture *how* ADHD shows up, a
**Pack** captures *what life you're managing* (Parent, Caregiver, …) — an
orthogonal axis. A pack is a thin declarative layer, not a module: it switches on
a relevant subset of modules, seeds domain vocabulary and coaching defaults, and
is cheap to add and shareable. See :mod:`prefrontal.packs.base` for the shape.

Importing this package registers all built-in packs with
:mod:`prefrontal.packs.registry`. Enable a subset via ``PREFRONTAL_PACKS`` (see
``.env.example``); unset enables **none** (a pack is an explicit opt-in).

To add a pack: build a :class:`~prefrontal.packs.base.Pack`, call
``register(YourPack)`` at the bottom of its file, and import it here so it loads.
"""

# Import built-in packs for their registration side effects.
from prefrontal.packs import parent  # noqa: F401  (side-effect import)
from prefrontal.packs.base import Pack, PackVocabulary
from prefrontal.packs.registry import (
    available,
    enabled_packs,
    get,
    is_enabled,
    pack_module_keys,
    register,
    resolve_pack_vocabulary,
)

__all__ = [
    "Pack",
    "PackVocabulary",
    "available",
    "enabled_packs",
    "get",
    "is_enabled",
    "pack_module_keys",
    "register",
    "resolve_pack_vocabulary",
]
