"""The shared household sheet — a single glanceable "everything about the kids".

Two co-parents share one household (:mod:`prefrontal.memory.repos.household`);
this module turns that household's data into one visible sheet, the way
:mod:`prefrontal.briefing` and :mod:`prefrontal.panic` turn a user's data into a
digest. Same two layers as those: a deterministic, model-free
:func:`build_sheet` / :func:`render_sheet` (fully testable), with no LLM pass —
assembly is cheap, so there is no cache (unlike ``profile_cache``); the sheet is
built on request.

Its reason to exist is **balancing invisible load**: every fact is stamped with
who set it and when, and the very first section is *what changed recently* —
the thing the oblivious parent sees first. See ``docs/household-sheet.md``.

Sections, in order (§6 of the spec):

1. **Recently changed** — the last few facts/agreements by ``updated_at``, each
   showing *what · who · when* ("Sam shoe size → 13 · Dana · 2d ago").
2. **Per-child facts** — grouped by category, one block per child (plus a
   household-wide block for ``child_id = 0`` facts).
3. **Standing agreements** — behaviour plans; star charts rendered from the
   optional ``structured`` JSON (thresholds → rewards).
4. **Upcoming appointments** — the viewer's ``kind='child'`` commitments in the
   near window.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from prefrontal.commitments import KIND_CHILD
from prefrontal.impact import utcnow
from prefrontal.memory.repos.household import (
    FACT_CATEGORIES,
    FACT_CATEGORY_LABELS,
    HOUSEHOLD_WIDE,
)
from prefrontal.memory.store import MemoryStore

#: How many recent changes to surface — kept short so the load-balancing signal
#: (what the other parent hasn't seen) stays legible, not a full changelog.
MAX_RECENTLY_CHANGED = 6

#: How far ahead to surface child appointments.
UPCOMING_APPT_DAYS = 14


@dataclass(frozen=True)
class Change:
    """One recent write to the sheet, normalized across facts and agreements.

    Attributes:
        what: Human label ("Sam · shoe size → 13", "Star chart plan").
        who: Display name of whoever last set it (or ``None`` if unknown).
        when: Compact "how long ago" phrasing ("2d ago", "just now").
        at: The raw ``updated_at`` timestamp (for sorting/serialization).
    """

    what: str
    who: str | None
    when: str
    at: str


@dataclass(frozen=True)
class Appointment:
    """One upcoming child appointment (a ``kind='child'`` commitment)."""

    title: str
    when: str
    start_at: str
    location: str | None = None


@dataclass(frozen=True)
class HouseholdSheet:
    """The assembled shared sheet (deterministic; model-free).

    Attributes:
        household_name: The household's name, or ``None`` if unnamed.
        children: Roster dicts (id/name/birthday).
        recently_changed: Most-recent writes, newest first (the load surface).
        per_child: Ordered blocks ``{child_id, child_name, categories}`` where
            ``categories`` is ``[{category, label, items}]`` in
            :data:`FACT_CATEGORIES` order; the household-wide block (child_id 0)
            comes first when present.
        agreements: Standing-plan dicts (title/kind/body/child_name/structured).
        upcoming: Upcoming child appointments, soonest first.
        counts: ``{children, facts, agreements, upcoming}`` for a summary line.
    """

    household_name: str | None
    children: list[dict[str, Any]] = field(default_factory=list)
    recently_changed: list[Change] = field(default_factory=list)
    per_child: list[dict[str, Any]] = field(default_factory=list)
    agreements: list[dict[str, Any]] = field(default_factory=list)
    upcoming: list[Appointment] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


# --- timestamp helpers (mirrors panic.py) ------------------------------------


def _parse_dt(ts: Any) -> datetime | None:
    """Parse a stored ``YYYY-MM-DD HH:MM:SS`` (naive UTC) timestamp, or ``None``."""
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _ago_phrase(ts: Any, now: datetime) -> str:
    """Compact "how long ago" phrasing for a past timestamp."""
    dt = _parse_dt(ts)
    if dt is None:
        return ""
    minutes = max(0.0, (now - dt).total_seconds() / 60.0)
    if minutes < 2:
        return "just now"
    if minutes < 90:
        return f"{round(minutes)}m ago"
    hours = minutes / 60.0
    if hours < 36:
        return f"{round(hours)}h ago"
    return f"{round(hours / 24.0)}d ago"


# --- build -------------------------------------------------------------------


def _fact_what(fact: dict[str, Any]) -> str:
    """Human label for a fact change: "Sam · shoe size → 13" (or household-wide)."""
    who_for = fact.get("child_name") or "Household"
    value = fact.get("value")
    tail = f" → {value}" if value else " cleared"
    return f"{who_for} · {fact['item']}{tail}"


def _appt_when(start: datetime | None, now: datetime) -> str:
    """Friendly "when" for an appointment ("today 3:00pm", "Thu 3:00pm", "in 5d")."""
    if start is None:
        return ""
    time_str = start.strftime("%-I:%M%p").lower()
    days = (start.date() - now.date()).days
    if days == 0:
        return f"today {time_str}"
    if days == 1:
        return f"tomorrow {time_str}"
    if 0 < days < 7:
        return f"{start.strftime('%a')} {time_str}"
    return f"{start.strftime('%b %-d')} {time_str}"


def build_sheet(store: MemoryStore, *, now: datetime | None = None) -> HouseholdSheet:
    """Assemble the structured household sheet from memory (deterministic).

    Args:
        store: A store **scoped to a user who is in a household** (its household
            methods resolve the shared scope). A user with no household will
            raise from the store's ``_household_id`` guard.
        now: Optional naive-UTC "now" (defaults to :func:`prefrontal.impact.utcnow`).

    Returns:
        A :class:`HouseholdSheet`.
    """
    now = now or utcnow()

    household = store.household()
    children = store.children()
    facts = store.facts()
    agreements = store.agreements()

    # 1. Recently changed — facts + agreements, newest first.
    changes: list[Change] = []
    for f in facts:
        changes.append(
            Change(
                what=_fact_what(f),
                who=f.get("updated_by_name"),
                when=_ago_phrase(f.get("updated_at"), now),
                at=str(f.get("updated_at") or ""),
            )
        )
    for a in agreements:
        changes.append(
            Change(
                what=f"{a['title']} (plan)",
                who=a.get("updated_by_name"),
                when=_ago_phrase(a.get("updated_at"), now),
                at=str(a.get("updated_at") or ""),
            )
        )
    changes.sort(key=lambda c: c.at, reverse=True)
    recently_changed = changes[:MAX_RECENTLY_CHANGED]

    # 2. Per-child facts, grouped by category. Household-wide (child_id 0) first.
    per_child = _group_facts_by_child(facts, children)

    # 3. Agreements — parse structured JSON once for the render.
    agreements_out = [_prepare_agreement(a) for a in agreements]

    # 4. Upcoming child appointments — the viewer's kind='child' commitments.
    upcoming = _upcoming_child_appts(store, now)

    counts = {
        "children": len(children),
        "facts": len(facts),
        "agreements": len(agreements),
        "upcoming": len(upcoming),
    }
    return HouseholdSheet(
        household_name=(household or {}).get("name"),
        children=children,
        recently_changed=recently_changed,
        per_child=per_child,
        agreements=agreements_out,
        upcoming=upcoming,
        counts=counts,
    )


def _group_facts_by_child(
    facts: list[dict[str, Any]], children: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Group facts into per-child blocks, each block's items ordered by category."""
    names = {c["id"]: c["name"] for c in children}
    # Deterministic block order: household-wide (0) first, then children by name.
    order = [HOUSEHOLD_WIDE] + [c["id"] for c in children]
    blocks: list[dict[str, Any]] = []
    for child_id in order:
        owned = [f for f in facts if f["child_id"] == child_id]
        if not owned:
            continue
        categories = []
        for cat in FACT_CATEGORIES:
            items = [
                {
                    "item": f["item"],
                    "value": f.get("value"),
                    "updated_by_name": f.get("updated_by_name"),
                    "updated_at": f.get("updated_at"),
                }
                for f in owned
                if f["category"] == cat
            ]
            if items:
                categories.append(
                    {
                        "category": cat,
                        "label": FACT_CATEGORY_LABELS.get(cat, cat),
                        "items": items,
                    }
                )
        blocks.append(
            {
                "child_id": child_id,
                "child_name": names.get(child_id) or "Household",
                "categories": categories,
            }
        )
    return blocks


def _prepare_agreement(a: dict[str, Any]) -> dict[str, Any]:
    """Copy an agreement row with its ``structured`` JSON parsed (or ``None``)."""
    parsed: Any = None
    raw = a.get("structured")
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
    return {
        "id": a.get("id"),
        "title": a.get("title"),
        "kind": a.get("kind"),
        "body": a.get("body"),
        "child_name": a.get("child_name"),
        "structured": parsed,
        "updated_by_name": a.get("updated_by_name"),
    }


def _upcoming_child_appts(store: MemoryStore, now: datetime) -> list[Appointment]:
    """The viewer's near-window child appointments (``kind='child'`` commitments)."""
    horizon = now + timedelta(days=UPCOMING_APPT_DAYS)
    out: list[Appointment] = []
    for c in store.upcoming_commitments(limit=100):
        if c.get("kind") != KIND_CHILD:
            continue
        start = _parse_dt(c.get("start_at"))
        if start is None or start > horizon:
            continue
        out.append(
            Appointment(
                title=c.get("title") or "appointment",
                when=_appt_when(start, now),
                start_at=str(c.get("start_at")),
                location=c.get("location"),
            )
        )
    return out


# --- render ------------------------------------------------------------------


def _render_star_chart(structured: dict[str, Any]) -> list[str]:
    """Render a star/points chart's thresholds as list lines, if well-formed."""
    thresholds = structured.get("thresholds")
    if not isinstance(thresholds, list) or not thresholds:
        return []
    unit = structured.get("unit", "star")
    lines = []
    for t in thresholds:
        if not isinstance(t, dict):
            continue
        n = t.get(unit) if unit in t else t.get("stars") or t.get("points")
        reward = t.get("reward")
        if n is not None and reward:
            lines.append(f"  - {n} {unit}s → {reward}")
    if lines and structured.get("earn_only"):
        lines.append("  - _(earn-only — stars are never taken away)_")
    return lines


def render_sheet(sheet: HouseholdSheet) -> str:
    """Render a :class:`HouseholdSheet` as a single Markdown section.

    Deterministic and server-side — meant to drop into the existing ``/family``
    view rather than a new UI. Leads with what changed recently (the load
    surface), then the per-child grid, standing agreements, and upcoming appts.

    Args:
        sheet: The structured sheet from :func:`build_sheet`.

    Returns:
        A Markdown string.
    """
    title = sheet.household_name or "Household"
    lines = [f"# {title} — shared sheet", ""]

    if not any(
        (sheet.recently_changed, sheet.per_child, sheet.agreements, sheet.upcoming)
    ):
        lines.append(
            "_Nothing here yet. Add the kids' sizes, routines, allergies, and "
            "plans in plain English — “Sam's shoe size is 13”, “add a dentist "
            "appt for Sam next Tuesday 3pm”._"
        )
        return "\n".join(lines).rstrip() + "\n"

    # 1. Recently changed — the load-balancing surface, first.
    if sheet.recently_changed:
        lines.append("## Recently changed")
        for c in sheet.recently_changed:
            who = f" · {c.who}" if c.who else ""
            when = f" · {c.when}" if c.when else ""
            lines.append(f"- {c.what}{who}{when}")
        lines.append("")

    # 2. Per-child facts.
    for block in sheet.per_child:
        lines.append(f"## {block['child_name']}")
        for cat in block["categories"]:
            lines.append(f"**{cat['label']}**")
            for it in cat["items"]:
                value = it.get("value") or "—"
                lines.append(f"- {it['item']}: {value}")
            lines.append("")

    # 3. Standing agreements.
    if sheet.agreements:
        lines.append("## Standing agreements")
        for a in sheet.agreements:
            who = f" — {a['child_name']}" if a.get("child_name") else ""
            lines.append(f"### {a['title']}{who} _({a['kind']})_")
            if a.get("body"):
                lines.append(a["body"])
            if isinstance(a.get("structured"), dict):
                lines.extend(_render_star_chart(a["structured"]))
            lines.append("")

    # 4. Upcoming appointments.
    if sheet.upcoming:
        lines.append("## Upcoming appointments")
        for appt in sheet.upcoming:
            where = f" · {appt.location}" if appt.location else ""
            lines.append(f"- **{appt.when}** — {appt.title}{where}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
