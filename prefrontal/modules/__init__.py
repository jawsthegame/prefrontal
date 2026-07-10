"""Challenge-area modules — one per executive-function difficulty.

ADHD presents differently for everyone, so Prefrontal's support behaviors are
organized into independently enableable modules rather than a single fixed
assistant. Each module targets one challenge:

- :mod:`prefrontal.modules.time_blindness` — duration estimation, departure timing.
- :mod:`prefrontal.modules.task_paralysis` — task initiation / activation energy.
- :mod:`prefrontal.modules.hyperfocus` — protect good focus, interrupt bad focus.
- :mod:`prefrontal.modules.impulsivity` — friction before impulsive switches.
- :mod:`prefrontal.modules.location_anchor` — escalating nudges back to a stated
  intention as its time window elapses (the "Coffee Shop Nudge").
- :mod:`prefrontal.modules.trip_tracking` — passively tracks undeclared round
  trips (leave home → return), then asks for a label, category, and honest note.
- :mod:`prefrontal.modules.self_care` — basic-needs check (v1: "have you eaten?")
  that deliberately pierces a focus block; opt-in.

Importing this package registers all built-in modules with
:mod:`prefrontal.modules.registry`. Enable a subset via ``PREFRONTAL_MODULES``
(see ``.env.example``); an empty value enables them all.

To add a module: subclass :class:`prefrontal.modules.base.Module`, implement
``profile_section``, call ``register(YourModule())`` at the bottom of the file,
and import it here so it loads.
"""

# Import built-in modules for their registration side effects.
from prefrontal.modules import (  # noqa: E402,F401  (side-effect imports, after registry)
    delegation_checkin,
    hyperfocus,
    impulsivity,
    location_anchor,
    projects,
    self_care,
    task_paralysis,
    time_blindness,
    trip_tracking,
)
from prefrontal.modules.base import Intervention, Module
from prefrontal.modules.registry import available, enabled_modules, get, register

__all__ = [
    "Intervention",
    "Module",
    "available",
    "enabled_modules",
    "get",
    "register",
]
