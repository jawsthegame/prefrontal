"""The SQLite behavioral memory layer — the core of Prefrontal.

Everything the system learns about the user lives here. The layer is split into:

- :mod:`prefrontal.memory.db` — connection management and schema initialization.
- :mod:`prefrontal.memory.store` — :class:`~prefrontal.memory.store.MemoryStore`,
  the high-level read/write API over episodes, patterns, and coaching state.
- :mod:`prefrontal.memory.patterns` — the learning pass that derives ``patterns``
  from ``episodes`` (:func:`~prefrontal.memory.patterns.recompute_patterns`).
- :mod:`prefrontal.memory.summarizer` — compresses the tables into a profile
  document for injection into agent system prompts.

The schema itself is defined in ``schema.sql`` alongside this package and
documented in ``docs/schema.md``.
"""

from prefrontal.memory.db import connect, init_db
from prefrontal.memory.patterns import compute_patterns, recompute_patterns
from prefrontal.memory.store import MemoryStore

__all__ = ["connect", "init_db", "MemoryStore", "compute_patterns", "recompute_patterns"]
