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


def _fact_what(fact: dict[str, Any]) -> str:
    """Human label for a fact change: "Sam · shoe size → 13" (or household-wide)."""
    who_for = fact.get("child_name") or "Household"
    value = fact.get("value")
    tail = f" → {value}" if value else " cleared"
    return f"{who_for} · {fact['item']}{tail}"


def _star_grant_what(grant: dict[str, Any]) -> str:
    """Human label for a star grant: "Sam · +2⭐ (Star chart)"."""
    who_for = grant.get("child_name") or "Household"
    delta = int(grant.get("delta") or 0)
    sign = "+" if delta >= 0 else ""
    title = grant.get("agreement_title") or "chart"
    return f"{who_for} · {sign}{delta}⭐ ({title})"
