"""Service-shift week helper.

Municipal collection shifts ("holiday moved trash to Wednesday") are entered
manually — the operator records them with ``prefrontal service-shift set`` (stored
in ``service_shifts`` via the store, applied by
:func:`prefrontal.household.run_chores_check`). This module holds the small week-key
helper that CLI shares for that flow.

An automatic scrape of a municipality's schedule page used to live here, but it
was never wired to a fetch/cron and has been removed; add it back alongside a page
fetch if automatic holiday-week detection is wanted.
"""
from __future__ import annotations

from datetime import datetime, timedelta


def monday_of(date_str: str) -> str:
    """The local Monday ``"YYYY-MM-DD"`` of the week containing ``date_str`` (a date)."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")
