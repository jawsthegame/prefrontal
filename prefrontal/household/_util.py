"""Small shared helpers for the household package.

Pure, dependency-free utilities used by more than one household submodule (so
they live here rather than in one of them, to keep the dependency direction
flat — both ``__init__`` and ``chores`` import from this leaf, never each other).
"""

from __future__ import annotations

from datetime import time
from typing import Any


def _parse_hhmm(value: Any) -> time | None:
    """Parse a ``"HH:MM"`` 24-hour string into a :class:`datetime.time`, or ``None``."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, TypeError):
        return None
