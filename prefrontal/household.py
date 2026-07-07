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

1. **Recently changed** — the last few facts/agreements/star grants by their
   timestamp, each showing *what · who · when* ("Sam shoe size → 13 · Dana ·
   2d ago", "Sam · +2⭐ (Star chart) · Alex · 1h ago").
2. **Per-child facts** — grouped by category, one block per child (plus a
   household-wide block for ``child_id = 0`` facts).
3. **Standing agreements** — behaviour plans; star charts rendered from the
   optional ``structured`` JSON (thresholds → rewards) with live progress from
   the star ledger (total so far · how many to the next reward).
4. **Upcoming appointments** — the viewer's ``kind='child'`` commitments in the
   near window.
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from prefrontal.clock import parse_ts as _parse_dt
from prefrontal.commitments import KIND_CHILD
from prefrontal.impact import utcnow
from prefrontal.memory.repos.household import (
    FACT_CATEGORIES,
    FACT_CATEGORY_LABELS,
    HOUSEHOLD_WIDE,
)
from prefrontal.memory.store import MemoryStore
from prefrontal.scheduling import local_datetime

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
        children: Kid roster dicts (id/name/birthday).
        pets: Pet roster dicts (id/name/species/birthday) — same table as the
            kids, split out so the app surfaces them under their own section.
        recently_changed: Most-recent writes, newest first (the load surface).
        per_child: Ordered blocks ``{child_id, child_name, categories}`` where
            ``categories`` is ``[{category, label, items}]`` in
            :data:`FACT_CATEGORIES` order; the household-wide block (child_id 0)
            comes first when present.
        per_pet: The same per-member fact blocks for the pets (no household-wide
            block — that belongs to the kids/household section).
        agreements: Standing-plan dicts (title/kind/body/child_name/structured),
            each also carrying ``star_total`` (the chart's running ledger total)
            and ``next_goal`` (the nearest unreached reward, or ``None``).
        upcoming: Upcoming child appointments, soonest first.
        shopping: Shopping-list dicts (id/item/spec/where_to_buy/got/child_name/
            added_by_name), still-needed first.
        chores: Recurring-chore dicts (id/title/owner_name/due_time/days/month_days/
            impact/enabled) each carrying ``done_today`` for today's local date,
            enabled first.
        counts: ``{children, facts, agreements, upcoming, shopping, chores}`` for a
            summary line (``shopping`` is the still-needed count; ``chores`` counts
            the enabled ones).
    """

    household_name: str | None
    children: list[dict[str, Any]] = field(default_factory=list)
    pets: list[dict[str, Any]] = field(default_factory=list)
    recently_changed: list[Change] = field(default_factory=list)
    per_child: list[dict[str, Any]] = field(default_factory=list)
    per_pet: list[dict[str, Any]] = field(default_factory=list)
    agreements: list[dict[str, Any]] = field(default_factory=list)
    upcoming: list[Appointment] = field(default_factory=list)
    shopping: list[dict[str, Any]] = field(default_factory=list)
    chores: list[dict[str, Any]] = field(default_factory=list)
    routines: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


# --- timestamp helpers (mirrors panic.py) ------------------------------------


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


def _star_grant_what(grant: dict[str, Any]) -> str:
    """Human label for a star grant: "Sam · +2⭐ (Star chart)"."""
    who_for = grant.get("child_name") or "Household"
    delta = int(grant.get("delta") or 0)
    sign = "+" if delta >= 0 else ""
    title = grant.get("agreement_title") or "chart"
    return f"{who_for} · {sign}{delta}⭐ ({title})"


def _appt_when(start: datetime | None, now: datetime, tz: str) -> str:
    """Friendly "when" for an appointment ("today 3:00pm", "Thu 3:00pm", "in 5d").

    ``start``/``now`` are naive UTC; convert both to the household's local zone
    before formatting the clock time and computing the day delta, so an Eastern
    family's 3pm appointment doesn't read as 7pm (raw UTC) or slip a day.
    """
    if start is None:
        return ""
    local_start = local_datetime(start, tz)
    local_now = local_datetime(now, tz)
    time_str = local_start.strftime("%-I:%M%p").lower()
    days = (local_start.date() - local_now.date()).days
    if days == 0:
        return f"today {time_str}"
    if days == 1:
        return f"tomorrow {time_str}"
    if 0 < days < 7:
        return f"{local_start.strftime('%a')} {time_str}"
    return f"{local_start.strftime('%b %-d')} {time_str}"


def build_sheet(
    store: MemoryStore, *, now: datetime | None = None, timezone: str = "UTC"
) -> HouseholdSheet:
    """Assemble the structured household sheet from memory (deterministic).

    Args:
        store: A store **scoped to a user who is in a household** (its household
            methods resolve the shared scope). A user with no household will
            raise from the store's ``_household_id`` guard.
        now: Optional naive-UTC "now" (defaults to :func:`prefrontal.impact.utcnow`).
        timezone: The household's local timezone, used to resolve "today" for each
            chore's ``done_today`` flag (defaults to UTC).

    Returns:
        A :class:`HouseholdSheet`.
    """
    now = now or utcnow()

    household = store.household()
    children = store.children()
    pets = store.pets()
    facts = store.facts()
    agreements = store.agreements()

    star_totals = store.star_totals()

    # 1. Recently changed — facts + agreements + star grants, newest first.
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
    for grant in store.recent_star_awards(limit=MAX_RECENTLY_CHANGED):
        changes.append(
            Change(
                what=_star_grant_what(grant),
                who=grant.get("awarded_by_name"),
                when=_ago_phrase(grant.get("created_at"), now),
                at=str(grant.get("created_at") or ""),
            )
        )
    changes.sort(key=lambda c: c.at, reverse=True)
    recently_changed = changes[:MAX_RECENTLY_CHANGED]

    # 2. Per-child facts, grouped by category. Household-wide (child_id 0) first.
    per_child = _group_facts_by_child(facts, children)
    # 2b. Per-pet facts (meds, vet/groomer contacts, food) — same grid, no
    # household-wide block (that belongs to the kids/household section).
    per_pet = _group_facts_by_child(facts, pets, include_household_wide=False)

    # 3. Agreements — parse structured JSON + attach the chart's star progress.
    agreements_out = [_prepare_agreement(a, star_totals.get(a["id"], 0)) for a in agreements]

    # 4. Upcoming child appointments — the viewer's kind='child' commitments.
    upcoming = _upcoming_child_appts(store, now, timezone)

    # 5. Shared shopping list — still-needed first.
    shopping = store.shopping_items()

    # 6. Recurring shared chores — with today's local done/pending status, and the
    # routines that group them (each flagged done_today once all its chores are in).
    routines = store.routines()
    chores = _chores_with_status(store, now, timezone, routines)
    today_local = local_datetime(now, timezone).strftime("%Y-%m-%d")
    done_ids = store.chore_ids_done_on(today_local)
    for r in routines:
        done, total = routine_chore_status(r["id"], chores, done_ids)
        r["chores_done"] = done
        r["chores_total"] = total
        r["done_today"] = total > 0 and done == total

    counts = {
        "children": len(children),
        "pets": len(pets),
        "facts": len(facts),
        "agreements": len(agreements),
        "upcoming": len(upcoming),
        "shopping": sum(1 for s in shopping if not s.get("got")),
        "chores": sum(1 for c in chores if c.get("enabled")),
        "routines": sum(1 for r in routines if r.get("enabled")),
    }
    return HouseholdSheet(
        household_name=(household or {}).get("name"),
        children=children,
        pets=pets,
        recently_changed=recently_changed,
        per_child=per_child,
        per_pet=per_pet,
        agreements=agreements_out,
        upcoming=upcoming,
        shopping=shopping,
        chores=chores,
        routines=routines,
        counts=counts,
    )


def _chores_with_status(
    store: MemoryStore,
    now: datetime,
    timezone: str,
    routines: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Chore rows with ``done_today`` + the *effective* schedule (routine-inherited).

    ``effective_days``/``effective_due_time`` resolve a chore's routine-inherited
    schedule (see :func:`with_effective_schedule`), so a surface renders when a
    chore actually runs without re-deriving it. Enabled first, then by due time.
    """
    chores = store.chores()
    if not chores:
        return []
    today = local_datetime(now, timezone).strftime("%Y-%m-%d")
    done_ids = store.chore_ids_done_on(today)
    routines_by_id = {r["id"]: r for r in (routines or [])}
    out = []
    for c in chores:
        eff = with_effective_schedule(c, routines_by_id.get(c.get("routine_id")))
        out.append({
            **c,
            "done_today": c["id"] in done_ids,
            "effective_days": eff["days"],
            "effective_month_days": eff["month_days"],
            "effective_due_time": eff["due_time"],
        })
    out.sort(
        key=lambda c: (0 if c.get("enabled") else 1, c.get("effective_due_time") or "", c["title"])
    )
    return out


def _group_facts_by_child(
    facts: list[dict[str, Any]],
    children: list[dict[str, Any]],
    *,
    include_household_wide: bool = True,
) -> list[dict[str, Any]]:
    """Group facts into per-member blocks, each block's items ordered by category.

    Serves both the kids grid (with the household-wide block first) and the pets
    grid (``include_household_wide=False`` — household-wide facts belong to the
    kids/household section, not repeated under the pets).
    """
    names = {c["id"]: c["name"] for c in children}
    # Deterministic block order: household-wide (0) first when included, then
    # roster members by name.
    order = ([HOUSEHOLD_WIDE] if include_household_wide else []) + [c["id"] for c in children]
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


def _prepare_agreement(a: dict[str, Any], star_total: int = 0) -> dict[str, Any]:
    """Copy an agreement row with its ``structured`` JSON parsed and star progress.

    ``star_total`` is the chart's running ledger total; from it and the parsed
    goals we precompute the next unreached reward so the render (and any machine
    client) shows "7 stars · 3 to go → …" without re-deriving it.
    """
    parsed = parse_structured(a.get("structured"))
    ng = next_goal(parsed, star_total) if isinstance(parsed, dict) else None
    return {
        "id": a.get("id"),
        "title": a.get("title"),
        "kind": a.get("kind"),
        "body": a.get("body"),
        "child_name": a.get("child_name"),
        "structured": parsed,
        "star_total": star_total,
        "next_goal": ng,
        "updated_by_name": a.get("updated_by_name"),
    }


def _upcoming_child_appts(
    store: MemoryStore, now: datetime, tz: str
) -> list[Appointment]:
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
                when=_appt_when(start, now, tz),
                start_at=str(c.get("start_at")),
                location=c.get("location"),
            )
        )
    return out


# --- star chart goals (pure) -------------------------------------------------
#
# An agreement's `structured` JSON declares the reward goals; the running total
# lives in the household_stars ledger. These helpers turn (structured, totals)
# into the reached/next-goal facts the award endpoint and the render both need —
# kept pure so the "did this grant cross a reward line?" decision is unit-tested
# without a store, a transport, or a request.


def parse_structured(raw: Any) -> Any:
    """Parse an agreement's ``structured`` JSON string, or ``None`` if absent/bad."""
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _star_thresholds(structured: Any) -> list[tuple[int, str]]:
    """Sorted ``(count, reward)`` goal pairs from a chart's ``structured`` JSON.

    Tolerates the template's shapes: the count lives under the chart's ``unit``
    key, or ``stars``/``points``. Malformed entries are skipped, not raised on.
    """
    if not isinstance(structured, dict):
        return []
    raw = structured.get("thresholds")
    if not isinstance(raw, list):
        return []
    unit = structured.get("unit", "star")
    out: list[tuple[int, str]] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        if unit in t:
            n = t.get(unit)
        elif "stars" in t:
            n = t.get("stars")
        else:
            n = t.get("points")
        reward = t.get("reward")
        if isinstance(n, (int, float)) and reward:
            out.append((int(n), str(reward)))
    out.sort(key=lambda pair: pair[0])
    return out


_TIER_RE = re.compile(r"^\s*(\d+)\s*=\s*(.+?)\s*$")


def parse_star_tiers(raw: str) -> list[dict[str, Any]] | None:
    """Parse a ``"7=small LEGO, 30=large"`` reward-tier spec into thresholds.

    The server-side twin of the ``/kids`` field's parser: each comma-separated
    ``count=reward`` becomes ``{"stars": count, "reward": reward}``, sorted by
    count. Blanks/garbage are skipped; ``None`` when nothing valid parses (so the
    caller can reject rather than store an empty chart). Multiple tiers are the
    point — e.g. a small reward at 7 and a bigger one at 30.
    """
    tiers: list[dict[str, Any]] = []
    for part in (raw or "").split(","):
        m = _TIER_RE.match(part)
        if m:
            tiers.append({"stars": int(m.group(1)), "reward": m.group(2).strip()})
    tiers.sort(key=lambda t: t["stars"])
    return tiers or None


def _unit_of(structured: Any) -> str:
    """The chart's counting unit (``"star"`` by default)."""
    return structured.get("unit", "star") if isinstance(structured, dict) else "star"


def newly_reached_goals(structured: Any, before: int, after: int) -> list[dict[str, Any]]:
    """Goals whose threshold the total crossed going ``before`` → ``after``.

    A goal ``n`` is newly reached when ``before < n <= after`` — so re-awarding
    at or above a threshold never re-fires it, and a single big grant that leaps
    past several thresholds reports each one (each is its own reward to hand out).
    """
    unit = _unit_of(structured)
    return [
        {"count": n, "unit": unit, "reward": reward}
        for n, reward in _star_thresholds(structured)
        if before < n <= after
    ]


def next_goal(structured: Any, total: int) -> dict[str, Any] | None:
    """The nearest not-yet-reached goal above ``total`` (with ``remaining``), or ``None``."""
    for n, reward in _star_thresholds(structured):
        if n > total:
            return {
                "count": n,
                "unit": _unit_of(structured),
                "reward": reward,
                "remaining": n - total,
            }
    return None


def star_congrats_text(child_name: str | None, goal: dict[str, Any]) -> str:
    """The celebration line sent to both parents when a reward goal is hit."""
    count = goal["count"]
    unit = goal["unit"]
    label = unit if count == 1 else f"{unit}s"
    who = child_name or "The kids"
    return f"🌟 {who} hit {count} {label} — reward unlocked: {goal['reward']}!"


def _resolve_child_name(store: MemoryStore, child_id: int) -> str | None:
    """The roster name for ``child_id`` (``None`` for household-wide id 0)."""
    if not child_id:
        return None
    return next((c["name"] for c in store.children() if c["id"] == child_id), None)


def award_stars_and_notify(
    store: MemoryStore,
    agreement_id: int,
    *,
    delta: int,
    awarded_by: int | None,
    note: str | None = None,
    settings: Any = None,
    client: Any = None,
) -> dict[str, Any] | None:
    """Award stars, detect crossed reward goals, and congratulate + notify both parents.

    The single code path behind every way a star can be given — the HTTP endpoint,
    the CLI, the one-tap notification button, and (via the caller) a scheduled
    prompt — so the "did this cross a goal? then tell both parents" rule lives in
    exactly one place.

    Returns ``None`` if the agreement isn't in the store's household (so a caller
    can 404). Raises :class:`ValueError` when a negative ``delta`` is applied to an
    earn-only chart. On success returns the new ``total``, the ``goals_reached``,
    the ``next_goal``, the resolved ``child_name``, and the per-parent ``notified``
    records (empty unless a goal was crossed).
    """
    agreement = store.agreement(agreement_id)
    if agreement is None:
        return None
    structured = parse_structured(agreement.get("structured"))
    if isinstance(structured, dict) and structured.get("earn_only") and delta < 0:
        raise ValueError("this chart is earn-only — stars can't be taken away")
    result = store.award_stars(
        agreement_id=agreement_id, delta=delta, awarded_by=awarded_by, note=note
    )
    assert result is not None  # agreement existence already confirmed above
    goals = newly_reached_goals(structured, result["before"], result["after"])
    child_name = _resolve_child_name(store, agreement.get("child_id") or 0)
    notified: list[dict[str, Any]] = []
    if goals:
        # Lazy import: delivery pulls in webhooks.notify, which would re-enter the
        # partially-initialized webhooks package if imported at module load.
        from prefrontal.integrations.delivery import (
            deliver_to_household,
            household_notice,
        )

        message = " ".join(star_congrats_text(child_name, g) for g in goals)
        notified = deliver_to_household(
            store,
            store.household_id_or_none(),
            household_notice(message, channel="sound"),
            settings=settings,
            client=client,
        )
    return {
        "agreement": agreement,
        "child_name": child_name,
        "total": result["after"],
        "delta": result["delta"],
        "goals_reached": goals,
        "next_goal": next_goal(structured, result["after"]),
        "notified": notified,
    }


# --- scheduled award prompts (pure) ------------------------------------------
#
# A chart can carry a `prompt` block in its `structured` JSON — a recurring
# "should <kid> get a star for <chart> today?" check-in on chosen weekdays at a
# time of day. The schedule lives on the chart (no new table); the sweep that
# fires it (a periodic check endpoint / CLI) uses `prompt_due`, and the chart's
# `last_prompted_at` column dedups to once per day. Weekdays are Python's
# `date.weekday()` ints: Mon=0 … Sun=6.


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


def normalize_prompt(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a prompt-schedule config, returning ``(clean, None)`` or ``(None, error)``.

    ``days`` are weekday ints (0=Mon … 6=Sun; de-duped and sorted), ``time`` is
    ``"HH:MM"`` 24-hour, ``enabled`` a bool, and ``question`` an optional custom
    line (whitespace-collapsed, length-capped). An enabled schedule with no days
    is rejected — that would silently never fire.
    """
    if not isinstance(raw, dict):
        return None, "prompt must be an object"
    days_raw = raw.get("days", [])
    if not isinstance(days_raw, list):
        return None, "days must be a list of weekday numbers (0=Mon … 6=Sun)"
    days = sorted(
        {int(d) for d in days_raw if isinstance(d, (int, float)) and 0 <= int(d) <= 6}
    )
    parsed_time = _parse_hhmm(raw.get("time"))
    if parsed_time is None:
        return None, "time must be 'HH:MM' (24-hour), e.g. '19:30'"
    enabled = bool(raw.get("enabled", True))
    if enabled and not days:
        return None, "pick at least one day, or disable the reminder"
    question = raw.get("question")
    if question is not None:
        question = re.sub(r"\s+", " ", str(question).strip())[:140] or None
    return {
        "enabled": enabled,
        "days": days,
        "time": parsed_time.strftime("%H:%M"),  # tz-ok: local wall-clock schedule from user input
        "question": question,
    }, None


def prompt_due(
    structured: Any,
    *,
    now_local: datetime,
    last_prompted_local: datetime | None = None,
) -> bool:
    """Whether a chart's award prompt should fire at ``now_local``.

    Fires on the first sweep at or after the configured time on a chosen weekday,
    then holds for the rest of that local day (``last_prompted_local`` on the same
    date suppresses it) — so a check that runs every 15–30 min sends exactly once.
    """
    p = structured.get("prompt") if isinstance(structured, dict) else None
    if not isinstance(p, dict) or not p.get("enabled"):
        return False
    if now_local.weekday() not in (p.get("days") or []):
        return False
    due_t = _parse_hhmm(p.get("time"))
    if due_t is None or now_local.time() < due_t:
        return False
    if last_prompted_local is not None and last_prompted_local.date() == now_local.date():
        return False
    return True


def prompt_question(structured: Any, child_name: str | None, title: str) -> str:
    """The prompt's question line — the chart's custom text, or a sensible default."""
    p = structured.get("prompt") if isinstance(structured, dict) else None
    custom = p.get("question") if isinstance(p, dict) else None
    if custom:
        return custom
    who = child_name or "the kids"
    return f"🌟 Did {who} earn a star for {title} today?"


# --- weekly mental-load check-in (pure) --------------------------------------
#
# Opt-in, household-scoped. Once a week both parents get a warm, non-judgmental
# "how did the invisible load feel for *you*?" self-report (light / balanced /
# heavy — three, since ntfy renders at most three action buttons). Once both have
# replied, a gentle shared note goes back to both — affirming if aligned, a soft
# "might be worth a chat" if one felt heavier, and it never names who. The
# schedule + last-sent live on the household row; the answers in household_checkins.

#: The self-report values, warm labels, and the one-tap actions that set them.
#: Kept in one place so notify.py buttons, the /nudge/act handler, and the store
#: never disagree on the vocabulary.
CHECKIN_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("light", "load_light", "Felt light 🙂"),
    ("balanced", "load_balanced", "Balanced ⚖️"),
    ("heavy", "load_heavy", "Carried a lot 🫠"),
)
#: One-tap action name → stored response value.
CHECKIN_ACTION_RESPONSE: dict[str, str] = {a: v for v, a, _ in CHECKIN_CHOICES}


def week_key(dt: datetime) -> str:
    """A stable ISO week key like ``"2026-W27"`` — the dedup unit for the check-in."""
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def normalize_checkin_config(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the check-in schedule, returning ``(clean, None)`` or ``(None, error)``.

    ``day`` is a weekday int (0=Mon … 6=Sun) and ``time`` is ``"HH:MM"``. Enabling
    the check-in without both is rejected (it would never fire); a disabled config
    may omit them (turning it off).
    """
    if not isinstance(raw, dict):
        return None, "config must be an object"
    enabled = bool(raw.get("enabled", False))
    day = raw.get("day")
    if day is not None:
        try:
            day = int(day)
        except (ValueError, TypeError):
            return None, "day must be 0 (Mon) … 6 (Sun)"
        if not 0 <= day <= 6:
            return None, "day must be 0 (Mon) … 6 (Sun)"
    parsed_time = _parse_hhmm(raw.get("time"))
    if enabled and (day is None or parsed_time is None):
        return None, "pick a day and time, or turn the check-in off"
    return {
        "enabled": enabled,
        "day": day,
        "time": parsed_time.strftime("%H:%M") if parsed_time else None,  # tz-ok: local wall-clock schedule from user input
    }, None


def checkin_due(
    config: Any,
    *,
    now_local: datetime,
    last_sent_local: datetime | None = None,
) -> bool:
    """Whether the weekly check-in should fire now (once per ISO week, at/after its time)."""
    if not isinstance(config, dict) or not config.get("enabled"):
        return False
    day = config.get("day")
    if day is None or now_local.weekday() != day:
        return False
    due_t = _parse_hhmm(config.get("time"))
    if due_t is None or now_local.time() < due_t:
        return False
    if last_sent_local is not None and week_key(last_sent_local) == week_key(now_local):
        return False
    return True


def checkin_question() -> str:
    """The warm, welcoming ask — framed as *your* week, with no wrong answers."""
    return (
        "💛 Weekly check-in — no wrong answers, and it's not about keeping score. "
        "How has the invisible load felt for *you* this week?"
    )


def checkin_summary(responses: list[str]) -> str:
    """The gentle shared note once parents have replied — never names who felt what.

    Aligned/light → affirming; someone heavier → a soft invitation to talk (no
    scorekeeping); both heavy → a be-kind-to-each-other nudge. Kept deliberately
    non-accusatory so people stay honest.
    """
    heavy = sum(1 for r in responses if r == "heavy")
    both = len(responses) >= 2
    lead = "You both checked in 💛" if both else "Thanks for checking in 💛"
    if heavy == 0:
        return (
            f"{lead} Sounds like things feel pretty balanced right now — nice. "
            "Keep looking out for each other."
        )
    if heavy == len(responses):
        tail = " for both of you" if both else ""
        return (
            f"{lead} Sounds like a heavy stretch{tail}. Be extra gentle with each "
            "other this week — what's one thing you could drop or share?"
        )
    return (
        f"{lead} It sounds like it's felt heavier for one of you lately. "
        "No scorekeeping — maybe a good week to trade a task or two, or just talk it through."
    )


# --- daily delta digest (pure) -----------------------------------------------
#
# The active half of load-balancing (spec §7): push each parent the *other*
# parent's changes they haven't seen. "Seen" is when they last opened the sheet
# (household_seen_at); "digested" is when we last told them (household_digested_at).
# The diff below is filtered by both — so a parent is never told about their own
# edits, nor told twice — and stays silent when there's nothing new.

#: The once-a-day cap: a sweep can run more often, but a parent gets at most one
#: digest per this many hours.
MIN_DIGEST_INTERVAL_HOURS = 20


def unseen_changes(
    store: MemoryStore, *, viewer_id: int, since: str = ""
) -> list[dict[str, Any]]:
    """The other parent's sheet changes newer than ``since`` and not made by ``viewer_id``.

    Unions the provenance-carrying, household-shared writes — facts, agreements,
    and star grants — keeping only rows whose stored timestamp string sorts after
    ``since`` (``""`` = everything) and that the viewer did not author. Newest first.
    """
    out: list[dict[str, Any]] = []
    for f in store.facts():
        at = str(f.get("updated_at") or "")
        if f.get("updated_by") != viewer_id and at > since:
            out.append({"what": _fact_what(f), "who": f.get("updated_by_name"), "at": at})
    for a in store.agreements():
        at = str(a.get("updated_at") or "")
        if a.get("updated_by") != viewer_id and at > since:
            out.append(
                {"what": f"{a['title']} (plan)", "who": a.get("updated_by_name"), "at": at}
            )
    for g in store.recent_star_awards(limit=50):
        at = str(g.get("created_at") or "")
        if g.get("awarded_by") != viewer_id and at > since:
            out.append({"what": _star_grant_what(g), "who": g.get("awarded_by_name"), "at": at})
    out.sort(key=lambda c: c["at"], reverse=True)
    return out


def digest_message(changes: list[dict[str, Any]], *, max_items: int = 6) -> str:
    """A warm "here's what you missed" digest from the other parent's changes."""
    lines = ["💛 A few things your co-parent updated since you last looked:"]
    for c in changes[:max_items]:
        who = f" ({c['who']})" if c.get("who") else ""
        lines.append(f"• {c['what']}{who}")
    extra = len(changes) - min(len(changes), max_items)
    if extra > 0:
        lines.append(f"…and {extra} more on the sheet.")
    return "\n".join(lines)


def digest_interval_ok(
    last_digested: Any, now: datetime, *, min_hours: int = MIN_DIGEST_INTERVAL_HOURS
) -> bool:
    """Whether enough time has passed since the last digest (the once-a-day cap)."""
    if not last_digested:
        return True
    last = _parse_dt(last_digested)
    if last is None:
        return True
    return (now - last).total_seconds() >= min_hours * 3600


# --- load balance view (pure) ------------------------------------------------
#
# The *objective* companion to the subjective check-in: who's actually been
# keeping the sheet up, from provenance counts. Tone is the whole game — a raw
# "Dana 14, Alex 1" reads as a scoreboard, so the caption is gentle, names the
# carrier (not the slacker), and treats imbalance as a season, never a verdict.

#: How far back the balance view looks by default.
BALANCE_WINDOW_DAYS = 30

#: A split at/above this top share reads as lopsided enough to gently name.
_LOPSIDED_SHARE = 75


def _share_members(counts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Attach a percentage share to each member's count; return ``(members, total)``."""
    total = sum(c["count"] for c in counts)
    members = [
        {
            "name": c["name"],
            "count": c["count"],
            "share": round(100 * c["count"] / total) if total else 0,
        }
        for c in counts
    ]
    return members, total


def balance_view(
    counts: list[dict[str, Any]], *, carrying: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Shape per-member counts into shares + a gentle caption (no judgment).

    Two facets of shared load (learning/parent-pack): ``counts`` is the **doing**
    tally — sheet edits, star awards, and chores actually completed in the window.
    ``carrying`` (optional) is the **accountability** tally — how many active
    routines each parent holds the mental load for (RACI "A"), a standing count
    rather than a windowed one. Doing and carrying are reported separately because
    they're different kinds of load: knocking out ten chores isn't the same as
    being the one answerable for the whole bedtime routine.

    Returns the doing facet at the top level (``total``/``members``/``caption``,
    unchanged) plus, when ``carrying`` is given, a ``carrying`` block with its own
    members/shares and caption.
    """
    members, total = _share_members(counts)
    view: dict[str, Any] = {
        "total": total,
        "members": members,
        "caption": balance_caption(members, total),
    }
    if carrying is not None:
        c_members, c_total = _share_members(carrying)
        view["carrying"] = {
            "total": c_total,
            "members": c_members,
            "caption": carrying_caption(c_members, c_total),
        }
    return view


def balance_caption(members: list[dict[str, Any]], total: int) -> str:
    """A warm one-liner for the doing split — affirming when even, gentle when not."""
    if total == 0 or not members:
        return "No shared work logged yet — nothing to compare. 💛"
    top = max(members, key=lambda m: m["share"])
    if top["share"] < _LOPSIDED_SHARE:
        return "You've been sharing the load pretty evenly lately — nice teamwork. 💛"
    return (
        f"{top['name']} has been carrying most of the doing lately. "
        "Some seasons just go that way — might be a nice week to trade a few things. "
        "No blame, just a gentle heads-up."
    )


def carrying_caption(members: list[dict[str, Any]], total: int) -> str:
    """A warm one-liner for the accountability split (who holds the mental load)."""
    if total == 0 or not members:
        return "No routines have an owner yet — assigning one gives it a home. 💛"
    top = max(members, key=lambda m: m["share"])
    if top["share"] < _LOPSIDED_SHARE:
        return "The mental load of your routines is split pretty evenly — that's the goal. 💛"
    return (
        f"{top['name']} is holding the mental load for most of your routines. "
        "That invisible work adds up — worth seeing if one could change hands."
    )


# --- recurring shared chores (pure) ------------------------------------------
#
# The active heart of shared load: a chore is one parent's recurring job (run
# the dishwasher, pack lunches) whose whole point is that forgetting it lands on
# the *other* parent. The notification flow reads a chore's schedule twice a day:
# a lead-time **reminder** to the owner before it's due, and — if the due time
# passes with the chore still undone — a gentle **miss-handoff** to the other
# parent so the slip isn't a morning surprise. The timing predicates below are
# pure (fed now/done/last-fired by the sweep), so "should this fire?" is
# unit-tested without a store or a clock, exactly like `prompt_due`/`checkin_due`.

#: Default lead time (minutes before the due time) for the owner's reminder.
DEFAULT_CHORE_REMIND_BEFORE_MINUTES = 30
#: Upper bound on the lead time (12h) — a reminder further out than that is a
#: config slip, not an intention, so we reject it rather than nudge at dawn.
MAX_CHORE_REMIND_BEFORE_MINUTES = 720

#: What a chore does while the household is "away" (vacation / travel). ``keep``
#: (the safe default) fires as normal — bills and meds don't pause for a trip.
#: ``suppress`` is a location-bound chore (trash, mail, plants) that simply can't
#: be done from afar, so it's silently skipped with a logged reason while away.
#: (``reassign`` is reserved for a later phase; not accepted yet.)
AWAY_BEHAVIORS: tuple[str, ...] = ("keep", "suppress")
DEFAULT_AWAY_BEHAVIOR = "keep"


def parse_chore_days(raw: Any) -> list[int]:
    """Parse a stored weekday CSV (``"0,2,4"``) into sorted ints, or ``[]`` (every day).

    Tolerant of whitespace and junk: only 0–6 survive, de-duped and sorted. An
    empty list is the "every day" sentinel (the schema's default ``days = ''``).
    """
    if isinstance(raw, (list, tuple, set)):
        parts: list[Any] = list(raw)
    else:
        parts = str(raw or "").split(",")
    days: set[int] = set()
    for p in parts:
        try:
            n = int(str(p).strip())
        except (ValueError, TypeError):
            continue
        if 0 <= n <= 6:
            days.add(n)
    return sorted(days)


def format_chore_days(days: Any) -> str:
    """Format a weekday list back to the stored CSV (``[0,2,4]`` → ``"0,2,4"``)."""
    return ",".join(str(d) for d in parse_chore_days(days))


def parse_month_days(raw: Any) -> list[int]:
    """Parse a stored day-of-month CSV (``"1,15"``) into sorted ints, or ``[]``.

    The month counterpart to :func:`parse_chore_days`: only 1–31 survive (junk and
    out-of-range dropped, de-duped, sorted). An empty list means "not month-scheduled"
    — the schedule falls back to :func:`parse_chore_days`' weekday behaviour.
    """
    if isinstance(raw, (list, tuple, set)):
        parts: list[Any] = list(raw)
    else:
        parts = str(raw or "").split(",")
    days: set[int] = set()
    for p in parts:
        try:
            n = int(str(p).strip())
        except (ValueError, TypeError):
            continue
        if 1 <= n <= 31:
            days.add(n)
    return sorted(days)


def format_month_days(days: Any) -> str:
    """Format a day-of-month list back to the stored CSV (``[15,1]`` → ``"1,15"``)."""
    return ",".join(str(d) for d in parse_month_days(days))


#: Short weekday labels for the human-readable schedule (Mon=0 … Sun=6).
_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _ordinal(n: int) -> str:
    """English ordinal for a day-of-month number (1 → "1st", 22 → "22nd", 31 → "31st")."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def describe_chore_days(days: Any) -> str:
    """A human schedule phrase: "every day", "weekdays", "weekends", or "Mon, Wed"."""
    parsed = parse_chore_days(days)
    if not parsed or parsed == list(range(7)):
        return "every day"
    if parsed == [0, 1, 2, 3, 4]:
        return "weekdays"
    if parsed == [5, 6]:
        return "weekends"
    return ", ".join(_WEEKDAY_LABELS[d] for d in parsed)


def describe_month_days(days: Any) -> str:
    """A human phrase for a day-of-month schedule: "the 1st & 15th", "" if none."""
    parsed = parse_month_days(days)
    if not parsed:
        return ""
    ordinals = [_ordinal(d) for d in parsed]
    if len(ordinals) == 1:
        joined = ordinals[0]
    else:
        joined = ", ".join(ordinals[:-1]) + " & " + ordinals[-1]
    return f"the {joined} of the month"


def describe_schedule(days: Any, month_days: Any = "") -> str:
    """The human schedule phrase, month-day-aware: month days win when set, else weekdays.

    Mirrors :func:`scheduled_on`'s precedence so a surface reads the same cadence the
    reminder sweep fires on — "the 1st & 15th of the month" when month days are set,
    otherwise the weekday phrase ("every day", "weekdays", "Mon, Wed", …).
    """
    md = describe_month_days(month_days)
    return md or describe_chore_days(days)


def fmt_time_12h(hhmm: Any) -> str:
    """Render a stored ``"HH:MM"`` as a friendly 12-hour time ("22:00" → "10:00pm")."""
    t = _parse_hhmm(hhmm)
    if t is None:
        return str(hhmm or "")
    return datetime(2000, 1, 1, t.hour, t.minute).strftime("%-I:%M%p").lower()  # tz-ok: renders a stored local "HH:MM" schedule


def normalize_chore(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a chore definition, returning ``(clean, None)`` or ``(None, error)``.

    ``clean`` carries a storage-ready shape: ``title`` (trimmed, non-empty),
    ``owner_id`` (int or ``None`` = either parent), ``days`` as a weekday CSV
    (empty = every day), ``month_days`` as a day-of-month CSV (empty = not
    month-scheduled; when set it takes precedence over ``days``), ``due_time`` as
    ``"HH:MM"``, ``remind_before`` (1 … :data:`MAX_CHORE_REMIND_BEFORE_MINUTES`
    minutes), ``impact`` (collapsed, length-capped, or ``None``), and ``enabled``.
    Membership of ``owner_id`` is the caller's to check — that needs the store.
    """
    if not isinstance(raw, dict):
        return None, "chore must be an object"
    title = re.sub(r"\s+", " ", str(raw.get("title") or "").strip())
    if not title:
        return None, "title must be non-empty"
    # due_time is optional: blank means "inherit the routine's schedule, or run
    # untimed" (a checklist chore with no reminder). A non-blank value must parse.
    due_raw = raw.get("due_time")
    if due_raw in (None, ""):
        due_time = ""
    else:
        due = _parse_hhmm(due_raw)
        if due is None:
            return None, "due_time must be 'HH:MM' (24-hour), e.g. '22:00', or blank"
        due_time = due.strftime("%H:%M")  # tz-ok: normalizes a local schedule "HH:MM"
    days = format_chore_days(raw.get("days", []))
    month_days = format_month_days(raw.get("month_days", []))
    remind_before = raw.get("remind_before", DEFAULT_CHORE_REMIND_BEFORE_MINUTES)
    try:
        remind_before = int(remind_before)
    except (ValueError, TypeError):
        return None, "remind_before must be a whole number of minutes"
    if not 1 <= remind_before <= MAX_CHORE_REMIND_BEFORE_MINUTES:
        return None, f"remind_before must be 1–{MAX_CHORE_REMIND_BEFORE_MINUTES} minutes"
    owner_id = raw.get("owner_id")
    if owner_id is not None:
        try:
            owner_id = int(owner_id)
        except (ValueError, TypeError):
            return None, "owner_id must be a member's user id, or null for either parent"
    impact = raw.get("impact")
    if impact is not None:
        impact = re.sub(r"\s+", " ", str(impact).strip())[:160] or None
    away_behavior = raw.get("away_behavior", DEFAULT_AWAY_BEHAVIOR)
    if away_behavior in (None, ""):
        away_behavior = DEFAULT_AWAY_BEHAVIOR
    away_behavior = str(away_behavior).strip().lower()
    if away_behavior not in AWAY_BEHAVIORS:
        return None, f"away_behavior must be one of {', '.join(AWAY_BEHAVIORS)}"
    return {
        "title": title,
        "owner_id": owner_id,
        "days": days,
        "month_days": month_days,
        "due_time": due_time,
        "remind_before": remind_before,
        "impact": impact,
        "enabled": bool(raw.get("enabled", True)),
        "away_behavior": away_behavior,
    }, None


def normalize_routine(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a routine definition, returning ``(clean, None)`` or ``(None, error)``.

    A routine groups chores and names one **accountable** owner (RACI "A" — the
    mental-load holder, distinct from the "responsible" doer of each chore). It
    carries the schedule its chores inherit: ``days`` (weekday CSV, empty = every
    day), ``month_days`` (day-of-month CSV, empty = not month-scheduled; when set it
    takes precedence over ``days``), and ``due_time`` (``"HH:MM"``, or empty = *not*
    time-tied — a plain grouping with no clock). ``accountable_id`` is an int member
    id or ``None`` (unassigned); membership is the caller's to check (needs the store).
    """
    if not isinstance(raw, dict):
        return None, "routine must be an object"
    title = re.sub(r"\s+", " ", str(raw.get("title") or "").strip())
    if not title:
        return None, "title must be non-empty"
    # due_time is optional for a routine: blank means "not time-tied".
    due_raw = raw.get("due_time")
    if due_raw in (None, ""):
        due_time = ""
    else:
        due = _parse_hhmm(due_raw)
        if due is None:
            return None, "due_time must be 'HH:MM' (24-hour), e.g. '07:30', or blank"
        due_time = due.strftime("%H:%M")  # tz-ok: normalizes a local schedule "HH:MM"
    accountable_id = raw.get("accountable_id")
    if accountable_id is not None:
        try:
            accountable_id = int(accountable_id)
        except (ValueError, TypeError):
            return None, "accountable_id must be a member's user id, or null"
    impact = raw.get("impact")
    if impact is not None:
        impact = re.sub(r"\s+", " ", str(impact).strip())[:160] or None
    return {
        "title": title,
        "accountable_id": accountable_id,
        "days": format_chore_days(raw.get("days", [])),
        "month_days": format_month_days(raw.get("month_days", [])),
        "due_time": due_time,
        "impact": impact,
        "enabled": bool(raw.get("enabled", True)),
    }, None


def effective_chore_schedule(
    chore: dict[str, Any], routine: dict[str, Any] | None = None
) -> tuple[str, str, str]:
    """The ``(days, month_days, due_time)`` a chore actually runs on (schedule inheritance).

    A chore that sets its **own** ``due_time`` overrides fully (its own days /
    month days + time win). Otherwise it **inherits** its routine's whole schedule
    (weekdays, days-of-month, and time together). A standalone chore with no time
    of its own is *untimed* — it never fires a reminder or miss-handoff; it's a
    checklist item you mark done (which still counts toward the "doing" balance).
    """
    own_due = _parse_hhmm(chore.get("due_time"))
    if own_due is not None:
        return (
            format_chore_days(chore.get("days")),
            format_month_days(chore.get("month_days")),
            own_due.strftime("%H:%M"),  # tz-ok: local schedule "HH:MM"
        )
    if routine is not None:
        return (
            format_chore_days(routine.get("days")),
            format_month_days(routine.get("month_days")),
            str(routine.get("due_time") or ""),
        )
    return (
        format_chore_days(chore.get("days")),
        format_month_days(chore.get("month_days")),
        "",
    )


def with_effective_schedule(
    chore: dict[str, Any], routine: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A copy of ``chore`` with ``days``/``month_days``/``due_time`` resolved via its routine."""
    days, month_days, due_time = effective_chore_schedule(chore, routine)
    return {**chore, "days": days, "month_days": month_days, "due_time": due_time}


def _month_day_matches(month_days: list[int], now_local: datetime) -> bool:
    """Whether ``now_local``'s day-of-month matches any chosen day (short-month clamped).

    A day past the current month's length (e.g. the 31st in February) fires on the
    *last* day instead of silently skipping — so "pay rent on the 31st" still lands
    at the end of a 28/30-day month.
    """
    last = calendar.monthrange(now_local.year, now_local.month)[1]
    return any(now_local.day == min(d, last) for d in month_days)


def scheduled_on(days: Any, month_days: Any, now_local: datetime) -> bool:
    """Whether a schedule runs on ``now_local`` — month days win when set, else weekdays.

    When ``month_days`` is non-empty the schedule is a monthly one (those calendar
    days, regardless of weekday). Otherwise it falls back to the weekday behaviour:
    the chosen weekdays, or every day when ``days`` is empty too.
    """
    md = parse_month_days(month_days)
    if md:
        return _month_day_matches(md, now_local)
    wd = parse_chore_days(days)
    return not wd or now_local.weekday() in wd


def _due_minute(chore: dict[str, Any]) -> int | None:
    """The chore's due time as a minute-of-day, or ``None`` if it can't be parsed."""
    due = _parse_hhmm(chore.get("due_time"))
    return due.hour * 60 + due.minute if due is not None else None


def reminder_due(
    chore: dict[str, Any],
    *,
    now_local: datetime,
    done_today: bool,
    last_reminded_on: str | None = None,
) -> bool:
    """Whether the owner's lead-time reminder should fire at ``now_local``.

    Fires once, on the first sweep inside the ``[due − remind_before, due)``
    window on a scheduled, still-undone day. Past the due time the reminder
    yields to :func:`miss_due` (so a long-down server jumps straight to the
    miss, never a stale "due soon"). ``last_reminded_on`` on today's date
    suppresses a repeat.
    """
    if not chore.get("enabled", True) or done_today:
        return False
    if not scheduled_on(chore.get("days"), chore.get("month_days"), now_local):
        return False
    due_min = _due_minute(chore)
    if due_min is None:
        return False
    if last_reminded_on == now_local.strftime("%Y-%m-%d"):
        return False
    now_min = now_local.hour * 60 + now_local.minute
    remind_min = max(0, due_min - int(chore.get("remind_before") or 0))
    return remind_min <= now_min < due_min


def miss_due(
    chore: dict[str, Any],
    *,
    now_local: datetime,
    done_today: bool,
    last_missed_on: str | None = None,
) -> bool:
    """Whether the miss-handoff should fire — due time passed with the chore undone.

    Fires once per local day (``last_missed_on`` guards the repeat), on a
    scheduled day, at or after the due minute. This is the notification that
    reaches the *other* parent so a slip isn't a morning surprise.
    """
    if not chore.get("enabled", True) or done_today:
        return False
    if not scheduled_on(chore.get("days"), chore.get("month_days"), now_local):
        return False
    due_min = _due_minute(chore)
    if due_min is None:
        return False
    if last_missed_on == now_local.strftime("%Y-%m-%d"):
        return False
    now_min = now_local.hour * 60 + now_local.minute
    return now_min >= due_min


def away_covers(window: dict[str, Any] | None, now_local: datetime) -> bool:
    """Whether ``now_local``'s local date falls within an away ``window`` (inclusive).

    ``window`` is the ``{"starts_on", "ends_on", "note"}`` shape from
    :meth:`HouseholdRepo.away_window` (or ``None`` = not away). Dates are local
    ``"YYYY-MM-DD"`` strings, compared lexically — which is a correct chronological
    compare for that fixed format. A malformed or half-set window is treated as
    "not away" (fail-open: we'd rather nudge than silently swallow a real chore).
    """
    if not window:
        return False
    start = window.get("starts_on")
    end = window.get("ends_on")
    if not start or not end:
        return False
    today = now_local.strftime("%Y-%m-%d")  # tz-ok: local calendar date
    return start <= today <= end


@dataclass(frozen=True)
class ChoreDecision:
    """A chore's context gate outcome for one sweep tick.

    ``action`` is ``"proceed"`` (let the normal reminder/miss predicates decide) or
    ``"suppress"`` (skip this chore today — it can't or shouldn't fire). ``reason``
    is a short human explanation, present only when suppressed, so the skip is
    legible in the sweep result rather than silent. (Later phases add ``"shift"`` /
    ``"reassign"``; this phase is proceed/suppress only.)
    """

    action: str
    reason: str = ""


def resolve_chore_context(
    chore: dict[str, Any],
    *,
    now_local: datetime,
    away_window: dict[str, Any] | None = None,
    all_members_away: bool = False,
) -> ChoreDecision:
    """Decide whether a chore should be suppressed by household context this tick.

    The single context gate that sits between "is this chore scheduled today?" and
    the timing predicates in :func:`run_chores_check`. It suppresses a chore marked
    ``away_behavior="suppress"`` (a location-bound chore — trash, mail, plants —
    that can't be done from afar) when nobody's home: either the household-wide
    **away window** is active, or **every** member has individually marked
    themselves away (``all_members_away``) — same effect, nobody to do it.
    Everything else proceeds normally, so bills and meds still fire on vacation.
    Only reports a suppression for a chore that is enabled and actually scheduled
    today, so the sweep result stays meaningful.
    """
    if not chore.get("enabled", True):
        return ChoreDecision("proceed")
    if not scheduled_on(chore.get("days"), chore.get("month_days"), now_local):
        return ChoreDecision("proceed")
    if chore.get("away_behavior") == "suppress":
        if away_covers(away_window, now_local):
            note = (away_window or {}).get("note")
            ends = (away_window or {}).get("ends_on")
            why = f" ({note})" if note else ""
            return ChoreDecision("suppress", f"household is away through {ends}{why}")
        if all_members_away:
            return ChoreDecision("suppress", "everyone is away")
    return ChoreDecision("proceed")


def _impact_clause(chore: dict[str, Any]) -> str:
    """" If it slips, <impact>." — the reason the chore matters, or "" if none set."""
    impact = chore.get("impact")
    return f" If it slips, {impact}." if impact else ""


def chore_reminder_message(chore: dict[str, Any]) -> str:
    """The owner's lead-time nudge — warm, with the "why" and a one-tap Done."""
    return (
        f"🧼 Time to {chore['title']} — ideally by {fmt_time_12h(chore.get('due_time'))}."
        f"{_impact_clause(chore)} Tap Done once it's sorted."
    )


def chore_missed_owner_message(chore: dict[str, Any]) -> str:
    """The still-not-done nudge to the owner once the due time has passed (no blame)."""
    return (
        f"🧼 “{chore['title']}” still isn't done — it was due by "
        f"{fmt_time_12h(chore.get('due_time'))}. No stress; do it when you can and tap Done."
    )


def chore_missed_partner_message(chore: dict[str, Any]) -> str:
    """The gentle heads-up to the *other* parent — so a slip isn't a morning surprise."""
    return (
        f"💛 Heads up — “{chore['title']}” didn't get done today (was due "
        f"{fmt_time_12h(chore.get('due_time'))}).{_impact_clause(chore)} "
        "Flagging it so it's not a morning surprise — no need to jump on it unless "
        "you want to. Tap Done if you pick it up."
    )


def chore_reminder_cover_message(chore: dict[str, Any], away_owner: str) -> str:
    """The reminder to the present partner when the owner is away — "can you cover?"."""
    return (
        f"🧼 {away_owner}'s away — can you {chore['title']} by "
        f"{fmt_time_12h(chore.get('due_time'))}?{_impact_clause(chore)} "
        "Tap Done once it's sorted."
    )


def chore_missed_cover_message(chore: dict[str, Any], away_owner: str) -> str:
    """The miss nudge to the present partner when the away owner's chore has slipped."""
    return (
        f"🧼 “{chore['title']}” still isn't done — it was {away_owner}'s, but they're "
        f"away (due by {fmt_time_12h(chore.get('due_time'))}). No stress; pick it up "
        "when you can and tap Done."
    )


def run_chores_check(
    store: MemoryStore,
    *,
    settings: Any,
    now: datetime | None = None,
    client: Any = None,
) -> list[dict[str, Any]]:
    """Fire any chore reminders / miss-handoffs due now, notifying the right parent(s).

    The single orchestration path behind both ``POST /webhooks/household/chores/check``
    and ``prefrontal household chores-check`` (like :func:`award_stars_and_notify` is
    the one path behind every way a star is given). For each enabled, still-undone
    chore scheduled today it sends either the owner's lead-time **reminder** or —
    once the due time has passed — the owner a still-not-done nudge **and** the
    *other* parent a gentle **heads-up**. Each stage stamps its per-day cursor so a
    check running every 15–30 min fires each exactly once. Delivery failures never
    raise (each transport swallows them); returns one entry per chore that fired.

    Args:
        store: A store scoped to a household member.
        settings: Operator settings (timezone + one-tap signing origin/secret).
        now: Optional naive-UTC "now" (defaults to :func:`prefrontal.impact.utcnow`).
        client: Optional :class:`DeliveryClient` (tests inject a mock transport).
    """
    from prefrontal.integrations.delivery import (
        deliver_to_member,
        household_chore_notice,
    )

    now = now or utcnow()
    now_local = local_datetime(now, settings.timezone)
    today = now_local.strftime("%Y-%m-%d")
    hid = store.household_id_or_none()
    members = [
        m for m in store.household_members(hid) if m.get("status") in (None, "active")
    ]
    done_ids = store.chore_ids_done_on(today)
    # A chore inherits its routine's schedule unless it sets its own (see
    # with_effective_schedule); resolve once so the pure timing predicates below
    # see the effective days/due_time. An untimed chore never fires.
    routines_by_id = {r["id"]: r for r in store.routines()}
    # The household's away window (vacation / travel), if any — resolved once so
    # the per-chore context gate below can silently skip location-bound chores.
    away = store.away_window()
    # Per-member away state: a single member who's marked themselves away isn't
    # nudged; their chores fall to whoever's present. If *everyone* is away it's
    # the same as household-away (nobody to do it) — the gate suppresses then too.
    away_member_ids = {
        m["id"] for m in members
        if away_covers(store.scoped(m["id"]).member_away_window(), now_local)
    }
    present_members = [m for m in members if m["id"] not in away_member_ids]
    all_members_away = bool(members) and not present_members
    deliver_kw = {
        "settings": settings,
        "client": client,
        "base_url": settings.oauth_base_url,
        "secret": settings.session_secret,
    }

    def _member_name(member: dict[str, Any]) -> str:
        return member.get("display_name") or member.get("handle") or "your co-parent"

    def _to_member(member: dict[str, Any], text: str, cid: int) -> dict[str, Any]:
        row = deliver_to_member(
            store.scoped(member["id"]), household_chore_notice(text, cid),
            handle=member["handle"], **deliver_kw,
        )
        return {"handle": member["handle"], "delivery": row}

    sent: list[dict[str, Any]] = []
    for raw_chore in store.chores():
        chore = with_effective_schedule(
            raw_chore, routines_by_id.get(raw_chore.get("routine_id"))
        )
        # Context gate: an active away window skips location-bound chores. Skipping
        # (vs. stamping a cursor) means the chore resumes cleanly once we're back —
        # no stale "you missed it" waiting on the far side of the trip.
        decision = resolve_chore_context(
            chore, now_local=now_local, away_window=away,
            all_members_away=all_members_away,
        )
        if decision.action == "suppress":
            sent.append({"chore_id": chore["id"], "title": chore["title"],
                         "stage": "suppressed", "reason": decision.reason})
            continue
        cid = chore["id"]
        done = cid in done_ids
        owner_id = chore.get("owner_id")
        owner = next((m for m in members if m["id"] == owner_id), None)
        # An away owner (with someone still home) hands off: the present partner is
        # nudged to cover, and the away owner stays silent. If the owner is present —
        # or everyone is away, so there's no one to hand off to — normal routing.
        owner_away = owner is not None and owner["id"] in away_member_ids
        cover = owner_away and bool(present_members)
        if reminder_due(
            chore, now_local=now_local, done_today=done,
            last_reminded_on=chore.get("last_reminded_on"),
        ):
            if cover:
                text = chore_reminder_cover_message(chore, _member_name(owner))
                notified = [_to_member(m, text, cid) for m in present_members]
            elif owner is not None:  # owner present (or all away → they still get it)
                notified = [_to_member(owner, chore_reminder_message(chore), cid)]
            else:  # unassigned — everyone present owns it (all, if nobody's home)
                text = chore_reminder_message(chore)
                notified = [_to_member(m, text, cid) for m in (present_members or members)]
            store.mark_chore_reminded(cid, today)
            sent.append({"chore_id": cid, "title": chore["title"],
                         "stage": "reminder", "notified": notified})
        elif miss_due(
            chore, now_local=now_local, done_today=done,
            last_missed_on=chore.get("last_missed_on"),
        ):
            notified = []
            if cover:
                # The away owner's chore slipped: the present partner picks it up;
                # the away owner isn't nagged on their trip.
                text = chore_missed_cover_message(chore, _member_name(owner))
                notified = [_to_member(m, text, cid) for m in present_members]
            else:
                # Normal miss handoff, but an away member gets neither the owner
                # nudge nor the heads-up. All-away keep chore → notify everyone.
                for member in (present_members or members):
                    if owner_id is None or member["id"] == owner_id:
                        text = chore_missed_owner_message(chore)
                    else:
                        text = chore_missed_partner_message(chore)
                    notified.append(_to_member(member, text, cid))
            store.mark_chore_missed(cid, today)
            sent.append({"chore_id": cid, "title": chore["title"],
                         "stage": "missed", "notified": notified})
    return sent


# --- routine completion (pure + notify) --------------------------------------
#
# A routine groups chores; it's "done for the day" once every one of its enabled
# chores has been logged done today. Finishing that last chore is a small shared
# win worth naming — so the done-path (endpoint, one-tap, CLI) runs the check and,
# on the first completion of the day, congratulates *both* parents (like a star
# goal) and the sheet highlights the routine. The completion test is pure (fed the
# routine's chores + today's done-ids); a per-day cursor (`last_completed_on`)
# dedups the celebration to once per local day.


def routine_chore_status(
    routine_id: int, chores: list[dict[str, Any]], done_ids: set[int]
) -> tuple[int, int]:
    """``(done, total)`` for a routine's **enabled** linked chores given today's done-ids."""
    linked = [
        c for c in chores
        if c.get("routine_id") == routine_id and c.get("enabled", True)
    ]
    done = sum(1 for c in linked if c["id"] in done_ids)
    return done, len(linked)


def routine_is_complete(
    routine_id: int, chores: list[dict[str, Any]], done_ids: set[int]
) -> bool:
    """Whether every enabled chore under ``routine_id`` is done today.

    A routine with no enabled chores is never "complete" — there's nothing to
    finish, so it never fires a hollow celebration.
    """
    done, total = routine_chore_status(routine_id, chores, done_ids)
    return total > 0 and done == total


def routine_complete_message(routine: dict[str, Any], total: int) -> str:
    """The warm "you finished the whole routine" note sent to both parents."""
    title = routine.get("title") or "that routine"
    holder = routine.get("accountable_name")
    count = f"all {total} " if total > 1 else "the "
    tail = f" Nice work, {holder} 💛" if holder else " Nice teamwork 💛"
    return f"🎉 “{title}” is done for today — {count}task{'s' if total != 1 else ''} sorted.{tail}"


def log_chore_done_and_celebrate(
    store: MemoryStore,
    *,
    chore_id: int,
    done_on: str,
    done_by: int | None,
    settings: Any = None,
    client: Any = None,
) -> dict[str, Any] | None:
    """Log a chore done, then congratulate the household if it finished a routine.

    The single path behind every "mark done" surface (the HTTP endpoint, the
    one-tap button, the CLI) — mirroring :func:`award_stars_and_notify` as the one
    place the "did this cross a line? then tell both parents" rule lives. Logs the
    completion (idempotent), and if this chore's routine just had its **last**
    enabled chore finished for the first time today, delivers an encouraging notice
    to both parents and stamps the routine's per-day cursor so it fires once.

    Returns ``None`` when the chore isn't in the store's household (so the caller
    can 404), otherwise the ``log_chore_done`` result dict plus a
    ``routine_completed`` key: ``None``, or ``{routine_id, title, notified}``.
    """
    result = store.log_chore_done(chore_id=chore_id, done_on=done_on, done_by=done_by)
    if result is None:
        return None
    result["routine_completed"] = _celebrate_routine_if_complete(
        store, chore_id, done_on, settings=settings, client=client
    )
    return result


def _celebrate_routine_if_complete(
    store: MemoryStore,
    chore_id: int,
    today: str,
    *,
    settings: Any = None,
    client: Any = None,
) -> dict[str, Any] | None:
    """Notify + stamp if ``chore_id``'s routine is now fully done today (else ``None``)."""
    chore = store.chore(chore_id)
    routine_id = chore.get("routine_id") if chore else None
    if not routine_id:
        return None
    routine = store.routine(routine_id)
    if routine is None or not routine.get("enabled"):
        return None
    if routine.get("last_completed_on") == today:
        return None  # already celebrated today — don't re-fire on a re-tap
    done_ids = store.chore_ids_done_on(today)
    _, total = routine_chore_status(routine_id, store.chores(), done_ids)
    if not routine_is_complete(routine_id, store.chores(), done_ids):
        return None
    store.mark_routine_completed(routine_id, today)
    # Lazy import: delivery pulls in webhooks.notify (see award_stars_and_notify).
    from prefrontal.integrations.delivery import deliver_to_household, household_notice

    notified = deliver_to_household(
        store,
        store.household_id_or_none(),
        household_notice(routine_complete_message(routine, total), channel="sound"),
        settings=settings,
        client=client,
    )
    return {"routine_id": routine_id, "title": routine["title"], "notified": notified}


# --- render ------------------------------------------------------------------


def _render_star_chart(
    structured: dict[str, Any],
    star_total: int = 0,
    next_g: dict[str, Any] | None = None,
) -> list[str]:
    """Render a star/points chart: a progress line, then thresholds → rewards."""
    thresholds = _star_thresholds(structured)
    if not thresholds:
        return []
    unit = structured.get("unit", "star")
    lines = []
    if star_total or next_g:
        head = f"  - **{star_total} {unit}s so far**"
        if next_g:
            head += f" · {next_g['remaining']} to go → {next_g['reward']}"
        else:
            head += " · all rewards earned! 🎉"
        lines.append(head)
    for n, reward in thresholds:
        lines.append(f"  - {n} {unit}s → {reward}")
    if structured.get("earn_only"):
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
        (
            sheet.recently_changed,
            sheet.per_child,
            sheet.pets,
            sheet.agreements,
            sheet.upcoming,
            sheet.shopping,
            sheet.chores,
            sheet.routines,
        )
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

    # 2c. Pets — the same fact grid (meds, vet/groomer, food) under their own
    # heading, so a dog's heartworm schedule doesn't hide among the kids' sizes.
    if sheet.pets:
        blocks_by_id = {b["child_id"]: b for b in sheet.per_pet}
        lines.append("## 🐾 Pets")
        for p in sheet.pets:
            species = f" · {p['species']}" if p.get("species") else ""
            lines.append(f"### {p['name']}{species}")
            block = blocks_by_id.get(p["id"])
            for cat in (block["categories"] if block else []):
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
                lines.extend(
                    _render_star_chart(
                        a["structured"], a.get("star_total", 0), a.get("next_goal")
                    )
                )
            lines.append("")

    # 4. Upcoming appointments.
    if sheet.upcoming:
        lines.append("## Upcoming appointments")
        for appt in sheet.upcoming:
            where = f" · {appt.location}" if appt.location else ""
            lines.append(f"- **{appt.when}** — {appt.title}{where}")
        lines.append("")

    # 5. Shopping — still-needed as unchecked boxes, bought ones ticked.
    if sheet.shopping:
        lines.append("## Shopping")
        for s in sheet.shopping:
            box = "x" if s.get("got") else " "
            detail = " · ".join(
                p for p in (s.get("spec"), s.get("where_to_buy")) if p
            )
            who = f" — {s['child_name']}" if s.get("child_name") else ""
            tail = f" ({detail})" if detail else ""
            lines.append(f"- [{box}] {s['item']}{tail}{who}")
        lines.append("")

    # 6. Routines — a grouping + its accountable owner (the mental-load holder).
    if sheet.routines:
        lines.append("## Routines")
        for r in sheet.routines:
            holder = r.get("accountable_name") or "unassigned"
            bits = [f"accountable: {holder}", describe_schedule(r.get("days"), r.get("month_days"))]
            bits.append(f"by {fmt_time_12h(r['due_time'])}" if r.get("due_time") else "no set time")
            total = r.get("chores_total") or 0
            if r.get("done_today"):
                bits.append("✅ all done today!")
            elif total:
                bits.append(f"{r.get('chores_done', 0)}/{total} done")
            if not r.get("enabled"):
                bits.append("paused")
            impact = f" — {r['impact']}" if r.get("impact") else ""
            done_mark = "🎉 " if r.get("done_today") else ""
            lines.append(f"- {done_mark}**{r['title']}** ({' · '.join(bits)}){impact}")
        lines.append("")

    # 7. Shared chores — today's status, whose job, when it's due (effective), why.
    if sheet.chores:
        lines.append("## Shared chores")
        for c in sheet.chores:
            box = "x" if c.get("done_today") else " "
            owner = c.get("owner_name") or "either"
            due = c.get("effective_due_time", c.get("due_time"))
            bits = [
                f"{owner}",
                describe_schedule(
                    c.get("effective_days", c.get("days")),
                    c.get("effective_month_days", c.get("month_days")),
                ),
                f"by {fmt_time_12h(due)}" if due else "untimed",
            ]
            if c.get("routine_title"):
                bits.append(c["routine_title"])
            if not c.get("enabled"):
                bits.append("paused")
            meta = " · ".join(bits)
            impact = f" — {c['impact']}" if c.get("impact") else ""
            lines.append(f"- [{box}] {c['title']} ({meta}){impact}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
