"""Municipal service-shift scraping — the "holiday moved trash to Wednesday" input.

The passive companion to the chore sweep's schedule logic. A weekly job fetches a
municipality's collection-schedule page (the URL lives in a ``services`` household
fact), hands the page text to an LLM to pull out any holiday-shifted pickups, and
upserts them into ``service_shifts``. From there :func:`prefrontal.household.run_chores_check`
reads them to move that week's reminder onto the shifted day.

This module owns only the *extraction + storage* seam (given page text → stored
shifts). The HTTP fetch of the live page is intentionally a thin, separate step:
the extraction is fully testable with synthetic page text + a mock model, and the
fetch is the one piece that needs the real, deployment-specific URL.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from prefrontal.household import service_week
from prefrontal.llm_json import generate_json
from prefrontal.log import get_logger

_log = get_logger(__name__)

#: The extraction system prompt — a tight instruction so a small local model
#: returns just the structured shifts, nothing else.
_EXTRACT_SYSTEM = (
    "You read a municipal trash/recycling collection-schedule web page and extract "
    "ONLY the holiday-shifted pickups it announces (e.g. 'collection delayed one day "
    "the week of July 4'). Respond with a JSON array; each element is "
    '{"service": str, "new_date": "YYYY-MM-DD", "reason": str}. '
    "service is the affected service in lowercase (e.g. 'trash', 'recycling'). "
    "new_date is the actual date pickup happens that week. reason is the holiday. "
    "If the page announces no shifts, respond with []. Output only the JSON array."
)


def _extract_prompt(page_text: str, services: list[str], today: str) -> str:
    """The user prompt: the page text, the services we care about, and today's date."""
    wanted = ", ".join(services) if services else "trash, recycling"
    # Cap the page text so a long page can't blow the context window; the schedule
    # blurb is always near the top of these pages.
    body = page_text.strip()[:6000]
    return (
        f"Today is {today}. Services of interest: {wanted}.\n"
        f"Collection-schedule page text:\n\"\"\"\n{body}\n\"\"\"\n"
        "Extract the holiday-shifted pickups as the JSON array described."
    )


def normalize_extracted_shifts(
    raw: Any, *, services: list[str]
) -> list[dict[str, Any]]:
    """Turn the model's raw array into storage-ready shift dicts (or drop bad rows).

    Each valid row becomes ``{"service", "week", "shifted_weekday", "reason"}`` —
    ``week`` (local Monday) and ``shifted_weekday`` derived deterministically from
    ``new_date`` so the model never has to compute a weekday or week key itself.
    Rows with an unknown service, a missing/unparseable date, or a service outside
    the requested set are dropped rather than stored wrong.
    """
    allowed = {s.strip().lower() for s in services} if services else None
    out: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        service = str(item.get("service") or "").strip().lower()
        if not service or (allowed is not None and service not in allowed):
            continue
        try:
            when = datetime.strptime(str(item.get("new_date")).strip(), "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        reason = item.get("reason")
        reason = str(reason).strip()[:80] if reason else None
        out.append({
            "service": service,
            "week": service_week(when),
            "shifted_weekday": when.weekday(),
            "reason": reason,
        })
    return out


def extract_service_shifts(
    page_text: str,
    *,
    services: list[str],
    today: str | None = None,
    client: Any = None,
) -> list[dict[str, Any]]:
    """Extract holiday-shifted pickups from a collection-schedule page's text.

    Returns storage-ready shift dicts (see :func:`normalize_extracted_shifts`), or
    an empty list when the model fails, returns nothing, or the page announces no
    shifts — the safe default is "no shift" (chores keep their normal day) rather
    than guessing.
    """
    if not (page_text or "").strip():
        return []
    today = today or datetime.utcnow().strftime("%Y-%m-%d")  # tz-ok: a date hint for the model
    raw = generate_json(
        _extract_prompt(page_text, services, today), system=_EXTRACT_SYSTEM, client=client
    )
    if raw is None:
        _log.info("service-shift extraction returned nothing (model unavailable or empty)")
        return []
    return normalize_extracted_shifts(raw, services=services)


def refresh_service_shifts(
    store: Any,
    *,
    page_text: str,
    services: list[str],
    source_url: str | None = None,
    today: str | None = None,
    client: Any = None,
) -> list[dict[str, Any]]:
    """Extract shifts from ``page_text`` and upsert them; return what was stored.

    The testable orchestrator behind the weekly scrape: everything except fetching
    the live page. Idempotent per (service, week) — re-running with the same page
    overwrites in place, so a job that runs daily converges rather than duplicating.
    """
    shifts = extract_service_shifts(
        page_text, services=services, today=today, client=client
    )
    for s in shifts:
        store.set_service_shift(
            service=s["service"], week=s["week"], shifted_weekday=s["shifted_weekday"],
            reason=s.get("reason"), source_url=source_url,
        )
    return shifts


def monday_of(date_str: str) -> str:
    """The local Monday ``"YYYY-MM-DD"`` of the week containing ``date_str`` (a date)."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")
